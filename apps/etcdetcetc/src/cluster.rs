//! EtcdCluster controller.

use std::{
    collections::{BTreeMap, HashMap, hash_map::DefaultHasher},
    hash::{Hash, Hasher},
    sync::{Arc, RwLock as StdRwLock},
    time::Duration,
};

use anyhow::anyhow;
use futures::StreamExt;
use k8s_openapi::api::core::v1::{ConfigMap, Secret};
use kube::{
    Api, Client, Resource, ResourceExt,
    api::{ObjectMeta, Patch, PatchParams},
    runtime::{Controller, controller::Action, reflector::ObjectRef, watcher},
};
use tokio::sync::RwLock;
use tracing::{info, warn};

use crate::crd::{ClusterMember, EtcdCluster, EtcdClusterSpec, EtcdClusterStatus};

/// Shared etcd client cache keyed by `(namespace, name)` of `EtcdCluster`.
pub type ClusterClients = Arc<RwLock<HashMap<(String, String), etcd_client::Client>>>;

/// In-memory index from `(secret namespace, secret name)` to referencing EtcdClusters.
pub type SecretRefIndex = Arc<StdRwLock<HashMap<(String, String), Vec<(String, String)>>>>;

pub type ConfigHashes = Arc<StdRwLock<HashMap<(String, String), u64>>>;

/// Shared context for the EtcdCluster controller.
#[derive(Clone)]
pub struct ClusterContext {
    /// Kubernetes API client.
    pub client: Client,
    /// Shared etcd client cache.
    pub clients: ClusterClients,
    /// Index of which EtcdCluster references which auth Secret.
    pub secret_refs: SecretRefIndex,
    /// Restricts reconciliation to these namespaces when non-empty.
    pub allowed_namespaces: Vec<String>,
    pub config_hashes: ConfigHashes,
}

/// Errors produced by EtcdCluster reconciliation.
#[derive(Debug, thiserror::Error)]
pub enum ClusterError {
    /// Kubernetes API error.
    #[error("kubernetes API error: {0}")]
    Kube(#[from] kube::Error),

    /// Etcd client creation or connectivity error.
    #[error("etcd error: {0}")]
    Etcd(#[from] anyhow::Error),

    /// Invalid or incomplete EtcdCluster object.
    #[error("invalid EtcdCluster: {0}")]
    Invalid(String),
}

/// Runs the EtcdCluster controller until the stream ends.
pub async fn run(context: ClusterContext) {
    let api = Api::<EtcdCluster>::all(context.client.clone());
    let secret_api = Api::<Secret>::all(context.client.clone());
    let secret_refs = context.secret_refs.clone();
    let context = Arc::new(context);

    info!("starting EtcdCluster controller");

    Controller::new(api, watcher::Config::default())
        .watches(secret_api, watcher::Config::default(), move |secret| {
            let Some(namespace) = secret.namespace() else {
                return Vec::new();
            };
            let secret_name = secret.name_any();
            let index_key = (namespace, secret_name);

            let refs = secret_refs
                .read()
                .unwrap_or_else(|poisoned| poisoned.into_inner());

            refs.get(&index_key)
                .into_iter()
                .flat_map(|clusters| clusters.iter())
                .map(|(cluster_namespace, cluster_name)| {
                    ObjectRef::new(cluster_name).within(cluster_namespace)
                })
                .collect::<Vec<_>>()
        })
        .run(reconcile, error_policy, context)
        .for_each(|result| async move {
            if let Err(err) = result {
                warn!(error = %err, "EtcdCluster reconciliation error");
            }
        })
        .await;
}

async fn reconcile(
    cluster: Arc<EtcdCluster>,
    context: Arc<ClusterContext>,
) -> Result<Action, ClusterError> {
    let namespace = cluster
        .namespace()
        .ok_or_else(|| ClusterError::Invalid(format!("{} has no namespace", cluster.name_any())))?;
    let name = cluster.name_any();

    if !context.allowed_namespaces.is_empty()
        && !context.allowed_namespaces.iter().any(|ns| ns == &namespace)
    {
        warn!(
            namespace,
            name, "cluster namespace not in allowedNamespaces, skipping"
        );
        return Ok(Action::await_change());
    }

    let key = (namespace.clone(), name.clone());

    if cluster.meta().deletion_timestamp.is_some() {
        context.clients.write().await.remove(&key);
        context
            .config_hashes
            .write()
            .unwrap_or_else(|p| p.into_inner())
            .remove(&key);
        {
            let mut refs = context
                .secret_refs
                .write()
                .unwrap_or_else(|p| p.into_inner());
            for clusters in refs.values_mut() {
                clusters.retain(|existing| existing != &key);
            }
            refs.retain(|_, clusters| !clusters.is_empty());
        }
        update_status(&cluster, &context.client, &namespace, &name, false).await?;
        return Ok(Action::await_change());
    }

    let auth_secret_name = cluster.spec.auth_secret_ref.name.clone();
    index_secret_ref(
        &context.secret_refs,
        (&namespace, &name),
        (&namespace, &auth_secret_name),
    );

    let secrets = Api::<Secret>::namespaced(context.client.clone(), &namespace);
    let secret = match secrets.get(&auth_secret_name).await {
        Ok(s) => s,
        Err(kube::Error::Api(ae)) if ae.code == 404 => {
            warn!(namespace, name, auth_secret_name, "auth secret not found");
            context.clients.write().await.remove(&key);
            context
                .config_hashes
                .write()
                .unwrap_or_else(|p| p.into_inner())
                .remove(&key);
            update_status(&cluster, &context.client, &namespace, &name, false).await?;
            return Ok(Action::requeue(Duration::from_secs(15)));
        }
        Err(err) => return Err(err.into()),
    };

    let config_hash = compute_config_hash(&cluster.spec, &secret);
    let cached_hash = context
        .config_hashes
        .read()
        .unwrap_or_else(|p| p.into_inner())
        .get(&key)
        .copied();

    if cached_hash != Some(config_hash) {
        info!(namespace, name, "config changed, rebuilding etcd client");
        match crate::etcd::build_client(&cluster.spec.endpoints, &secret).await {
            Ok(client) => {
                context.clients.write().await.insert(key.clone(), client);
                context
                    .config_hashes
                    .write()
                    .unwrap_or_else(|p| p.into_inner())
                    .insert(key.clone(), config_hash);
            }
            Err(err) => {
                warn!(namespace, name, error = %err, "failed to build etcd client");
                context.clients.write().await.remove(&key);
                context
                    .config_hashes
                    .write()
                    .unwrap_or_else(|p| p.into_inner())
                    .remove(&key);
                update_status(&cluster, &context.client, &namespace, &name, false).await?;
                return Ok(Action::requeue(Duration::from_secs(15)));
            }
        }
    }

    let client = context.clients.read().await.get(&key).cloned();
    match client {
        Some(mut client) => {
            if let Err(err) =
                fetch_and_update_status(&mut client, &cluster, &context.client, &namespace, &name)
                    .await
            {
                warn!(namespace, name, error = %err, "health check failed, marking disconnected");
                context.clients.write().await.remove(&key);
                context
                    .config_hashes
                    .write()
                    .unwrap_or_else(|p| p.into_inner())
                    .remove(&key);
                update_status(&cluster, &context.client, &namespace, &name, false).await?;
            } else {
                ensure_cluster_configmap(&cluster, &context.client, &namespace, &secret).await?;
            }
        }
        None => {
            update_status(&cluster, &context.client, &namespace, &name, false).await?;
        }
    }

    Ok(Action::requeue(Duration::from_secs(15)))
}

fn error_policy(
    _cluster: Arc<EtcdCluster>,
    error: &ClusterError,
    _context: Arc<ClusterContext>,
) -> Action {
    warn!(error = %error, "applying EtcdCluster error policy");
    Action::requeue(Duration::from_secs(60))
}

fn compute_config_hash(spec: &EtcdClusterSpec, secret: &Secret) -> u64 {
    let mut hasher = DefaultHasher::new();

    for endpoint in &spec.endpoints {
        endpoint.hash(&mut hasher);
    }

    spec.auth_secret_ref.name.hash(&mut hasher);

    if let Some(data) = &secret.data {
        let mut entries: Vec<_> = data.iter().collect();
        entries.sort_by(|a, b| a.0.cmp(b.0));
        for (k, v) in entries {
            k.hash(&mut hasher);
            v.0.hash(&mut hasher);
        }
    }

    hasher.finish()
}

async fn fetch_and_update_status(
    client: &mut etcd_client::Client,
    cluster: &EtcdCluster,
    kube_client: &Client,
    namespace: &str,
    name: &str,
) -> Result<(), ClusterError> {
    let status_resp = client
        .status()
        .await
        .map_err(|err| ClusterError::Etcd(anyhow!(err.to_string())))?;
    let members_resp = client
        .member_list()
        .await
        .map_err(|err| ClusterError::Etcd(anyhow!(err.to_string())))?;
    let alarm_resp = client
        .alarm(
            etcd_client::AlarmAction::Get,
            etcd_client::AlarmType::None,
            None,
        )
        .await
        .map_err(|err| ClusterError::Etcd(anyhow!(err.to_string())))?;

    let desired = to_status(
        true,
        Some(&status_resp),
        Some(&members_resp),
        Some(&alarm_resp),
        cluster.status.as_ref(),
    );

    patch_status_if_changed(cluster, kube_client, namespace, name, desired).await
}

async fn ensure_cluster_configmap(
    cluster: &EtcdCluster,
    client: &Client,
    namespace: &str,
    secret: &Secret,
) -> Result<(), ClusterError> {
    let ca_crt = secret
        .data
        .as_ref()
        .and_then(|data| data.get("ca.crt"))
        .ok_or_else(|| {
            ClusterError::Invalid(format!(
                "auth secret {}/{} missing required key ca.crt",
                namespace, cluster.spec.auth_secret_ref.name
            ))
        })?;
    let ca_crt = String::from_utf8(ca_crt.0.clone())
        .map_err(|_| ClusterError::Invalid("ca.crt is not valid UTF-8".to_string()))?;

    let owner_reference = cluster.controller_owner_ref(&()).ok_or_else(|| {
        ClusterError::Invalid(format!(
            "{} cannot produce controller owner reference",
            cluster.name_any()
        ))
    })?;

    let mut data = BTreeMap::new();
    data.insert("endpoints".to_string(), cluster.spec.endpoints.join(","));
    data.insert("ca.crt".to_string(), ca_crt);

    let configmap_name = format!("{}-etcd", cluster.name_any());
    let configmap = ConfigMap {
        metadata: ObjectMeta {
            name: Some(configmap_name.clone()),
            namespace: Some(namespace.to_string()),
            owner_references: Some(vec![owner_reference]),
            ..ObjectMeta::default()
        },
        data: Some(data),
        ..ConfigMap::default()
    };

    let configmaps = Api::<ConfigMap>::namespaced(client.clone(), namespace);
    configmaps
        .patch(
            &configmap_name,
            &PatchParams::apply("etcdetcetc").force(),
            &Patch::Apply(&configmap),
        )
        .await?;

    Ok(())
}

fn format_bytes(bytes: i64) -> String {
    let bytes_f = bytes as f64;
    if bytes_f >= 1_073_741_824.0 {
        format!("{:.1} GiB", bytes_f / 1_073_741_824.0)
    } else if bytes_f >= 1_048_576.0 {
        format!("{:.1} MiB", bytes_f / 1_048_576.0)
    } else if bytes_f >= 1024.0 {
        format!("{:.1} KiB", bytes_f / 1024.0)
    } else {
        format!("{} B", bytes)
    }
}

fn to_status(
    connected: bool,
    status: Option<&etcd_client::StatusResponse>,
    members: Option<&etcd_client::MemberListResponse>,
    alarms: Option<&etcd_client::AlarmResponse>,
    current_status: Option<&EtcdClusterStatus>,
) -> EtcdClusterStatus {
    let (ready, reason, message) = if connected {
        (true, "Connected", "")
    } else {
        (false, "Disconnected", "etcd cluster is not reachable")
    };

    let existing_conditions: &[crate::crd::Condition] = current_status
        .map(|status| status.conditions.as_slice())
        .unwrap_or(&[]);

    let member_list = members
        .map(|resp| {
            resp.members()
                .iter()
                .map(|member| ClusterMember {
                    name: member.name().to_string(),
                    endpoint: member.client_urls().first().cloned().unwrap_or_default(),
                    is_learner: member.is_learner(),
                })
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();

    let leader = match (status, members) {
        (Some(status_resp), Some(members_resp)) => {
            let leader_id = status_resp.leader();
            members_resp
                .members()
                .iter()
                .find(|member| member.id() == leader_id)
                .map(|member| member.name().to_string())
                .or_else(|| Some(format!("unknown-{leader_id}")))
        }
        _ => None,
    };

    let alarm_list = match (alarms, members) {
        (Some(alarm_resp), Some(members_resp)) => alarm_resp
            .alarms()
            .iter()
            .map(|alarm| {
                let member_name = members_resp
                    .members()
                    .iter()
                    .find(|member| member.id() == alarm.member_id())
                    .map(|member| member.name().to_string())
                    .unwrap_or_else(|| format!("member-{}", alarm.member_id()));

                let alarm_type = match alarm.alarm() {
                    etcd_client::AlarmType::Nospace => "NOSPACE",
                    etcd_client::AlarmType::Corrupt => "CORRUPT",
                    _ => "UNKNOWN",
                };

                format!("{member_name}: {alarm_type}")
            })
            .collect(),
        _ => Vec::new(),
    };

    EtcdClusterStatus {
        connected,
        conditions: vec![crate::crd::ready_condition_with_existing(
            ready,
            reason,
            message,
            existing_conditions,
        )],
        version: if connected {
            status.map(|s| s.version().to_owned())
        } else {
            None
        },
        db_size: if connected {
            status.map(|s| format_bytes(s.db_size()))
        } else {
            None
        },
        leader,
        members: member_list,
        alarms: alarm_list,
    }
}

async fn patch_status_if_changed(
    current: &EtcdCluster,
    client: &Client,
    namespace: &str,
    name: &str,
    desired: EtcdClusterStatus,
) -> Result<(), ClusterError> {
    if current.status.as_ref() == Some(&desired) {
        return Ok(());
    }

    let api = Api::<EtcdCluster>::namespaced(client.clone(), namespace);
    let patch = serde_json::json!({
        "status": desired,
    });

    api.patch_status(name, &PatchParams::default(), &Patch::Merge(&patch))
        .await?;

    Ok(())
}

async fn update_status(
    current: &EtcdCluster,
    client: &Client,
    namespace: &str,
    name: &str,
    connected: bool,
) -> Result<(), ClusterError> {
    let desired = to_status(connected, None, None, None, current.status.as_ref());
    patch_status_if_changed(current, client, namespace, name, desired).await
}

fn index_secret_ref(index: &SecretRefIndex, cluster_key: (&str, &str), secret_key: (&str, &str)) {
    let cluster_key = (cluster_key.0.to_owned(), cluster_key.1.to_owned());
    let secret_key = (secret_key.0.to_owned(), secret_key.1.to_owned());

    let mut refs = index
        .write()
        .unwrap_or_else(|poisoned| poisoned.into_inner());

    for clusters in refs.values_mut() {
        clusters.retain(|existing| existing != &cluster_key);
    }

    refs.entry(secret_key).or_default().push(cluster_key);

    refs.retain(|_, clusters| !clusters.is_empty());
}
