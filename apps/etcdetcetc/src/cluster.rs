//! EtcdCluster controller.

use std::{
    collections::{BTreeMap, BTreeSet, HashMap, HashSet, hash_map::DefaultHasher},
    hash::{Hash, Hasher},
    sync::{Arc, RwLock as StdRwLock},
    time::Duration,
};

use anyhow::anyhow;
use futures::StreamExt;
use k8s_openapi::api::core::v1::{ConfigMap, Secret};
use kube::{
    Api, Client, Resource, ResourceExt,
    api::{ObjectMeta, Patch, PatchParams, PostParams},
    runtime::{Controller, controller::Action, watcher},
};
use tokio::sync::RwLock;
use tracing::{info, warn};

use crate::crd::{ClusterMember, EtcdCluster, EtcdClusterSpec, EtcdClusterStatus, EtcdTenant};

const CLUSTER_FINALIZER: &str = "etcdetcetc.samcday.com/cluster";
const CLUSTER_ID_ACCEPTANCE_ANNOTATION: &str = "etcdetcetc.samcday.com/accept-cluster-id";
// Prefix-scoped tenants rely on the exact authorization behavior qualified by
// this controller, including nested transaction and populated-lease checks.
// Changing this allowlist requires source review plus the two-identity runtime
// qualification; accepting an arbitrary newer version would silently widen a
// security boundary.
pub(crate) const QUALIFIED_ETCD_VERSION: &str = "3.6.13";

/// Shared etcd client cache keyed by `(namespace, name)` of `EtcdCluster`.
pub type ClusterClients = Arc<RwLock<HashMap<(String, String), etcd_client::Client>>>;

pub type ConfigHashes = Arc<StdRwLock<HashMap<(String, String), u64>>>;

/// Shared context for the EtcdCluster controller.
#[derive(Clone)]
pub struct ClusterContext {
    /// Kubernetes API client.
    pub client: Client,
    /// Shared etcd client cache.
    pub clients: ClusterClients,
    /// Restricts reconciliation to these namespaces when non-empty.
    pub allowed_namespaces: Vec<String>,
    pub config_hashes: ConfigHashes,
    /// Cancels every in-flight reconcile at the hard leader-renew deadline.
    pub leadership: crate::leadership::LeadershipGuard,
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

    /// The process no longer holds a valid leader-election permit.
    #[error("leadership permit expired or was revoked")]
    LostLeadership,
}

/// Runs the EtcdCluster controller until the stream ends.
pub async fn run(context: ClusterContext) {
    let api = Api::<EtcdCluster>::all(context.client.clone());
    let context = Arc::new(context);

    info!("starting EtcdCluster controller without cluster-wide Secret watches");

    Controller::new(api, watcher::Config::default())
        .run(reconcile, error_policy, context)
        .for_each(|result| async move {
            if let Err(err) = result {
                warn!(
                    error = %err,
                    error_debug = ?err,
                    error_chain = %crate::format_error_chain(&err),
                    "EtcdCluster reconciliation error"
                );
            }
        })
        .await;
}

async fn reconcile(
    cluster: Arc<EtcdCluster>,
    context: Arc<ClusterContext>,
) -> Result<Action, ClusterError> {
    let mut leadership = context.leadership.clone();
    tokio::select! {
        biased;
        _ = leadership.wait_until_inactive_or_expired() => Err(ClusterError::LostLeadership),
        result = reconcile_while_leader(cluster, context) => result,
    }
}

async fn reconcile_while_leader(
    cluster: Arc<EtcdCluster>,
    context: Arc<ClusterContext>,
) -> Result<Action, ClusterError> {
    let result = reconcile_inner(cluster.clone(), context.clone()).await;
    if let Err(error) = &result
        && cluster.meta().deletion_timestamp.is_none()
        && let Some(namespace) = cluster.namespace()
    {
        let name = cluster.name_any();
        if let Err(status_error) =
            force_update_status(&cluster, &context.client, &namespace, &name, false).await
        {
            warn!(
                namespace,
                name,
                error = %status_error,
                original_error = %error,
                "failed to mark EtcdCluster disconnected after reconciliation error"
            );
        }
    }
    result
}

async fn reconcile_inner(
    cluster: Arc<EtcdCluster>,
    context: Arc<ClusterContext>,
) -> Result<Action, ClusterError> {
    let namespace = cluster
        .namespace()
        .ok_or_else(|| ClusterError::Invalid(format!("{} has no namespace", cluster.name_any())))?;
    let name = cluster.name_any();

    let key = (namespace.clone(), name.clone());

    if cluster.meta().deletion_timestamp.is_some() {
        if !has_cluster_finalizer(&cluster) {
            return Ok(Action::await_change());
        }

        let references = referencing_tenants(&context.client, &cluster).await?;
        if references.is_empty() {
            context.clients.write().await.remove(&key);
            context
                .config_hashes
                .write()
                .unwrap_or_else(|p| p.into_inner())
                .remove(&key);
            remove_cluster_finalizer(&context.client, &cluster, &namespace).await?;
            return Ok(Action::await_change());
        }

        warn!(
            namespace,
            name,
            tenants = ?references,
            "EtcdCluster deletion blocked while EtcdTenants reference its UID"
        );
        // Continue through normal observation so the pinned physical identity
        // remains current while tenant cleanup proceeds with independently
        // verified admin clients.
    } else {
        if !context.allowed_namespaces.is_empty()
            && !context.allowed_namespaces.iter().any(|ns| ns == &namespace)
        {
            warn!(
                namespace,
                name, "cluster namespace not in allowedNamespaces, skipping"
            );
            return Ok(Action::await_change());
        }

        if ensure_cluster_finalizer(&context.client, &cluster, &namespace).await? {
            return Ok(Action::await_change());
        }
    }

    let auth_secret_name = cluster.spec.auth_secret_ref.name.clone();
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

    let physical_server_ca = match read_physical_server_ca(&context.client, &cluster, &secret).await
    {
        Ok(ca) => ca,
        Err(err) => {
            warn!(
                namespace,
                name,
                error = %err,
                "physical etcd server CA is unavailable"
            );
            context.clients.write().await.remove(&key);
            context
                .config_hashes
                .write()
                .unwrap_or_else(|p| p.into_inner())
                .remove(&key);
            update_status(&cluster, &context.client, &namespace, &name, false).await?;
            return Ok(Action::requeue(Duration::from_secs(15)));
        }
    };

    let config_hash = compute_config_hash(&cluster.spec, &secret, &physical_server_ca);
    let cached_hash = context
        .config_hashes
        .read()
        .unwrap_or_else(|p| p.into_inner())
        .get(&key)
        .copied();

    let mut client = if cached_hash == Some(config_hash) {
        context.clients.read().await.get(&key).cloned()
    } else {
        // Never expose a client built from changed credentials, trust, or
        // endpoints until every endpoint and response header has been
        // validated against the durable physical-cluster identity.
        context.clients.write().await.remove(&key);
        context
            .config_hashes
            .write()
            .unwrap_or_else(|p| p.into_inner())
            .remove(&key);
        None
    };
    if client.is_none() {
        info!(namespace, name, "building staged etcd client");
        client =
            match crate::etcd::build_client(&cluster.spec.endpoints, &secret, &physical_server_ca)
                .await
            {
                Ok(client) => Some(client),
                Err(err) => {
                    warn!(
                        namespace,
                        name,
                        error = %err,
                        error_debug = ?err,
                        error_chain = %crate::format_error_chain(err.as_ref()),
                        "failed to build etcd client"
                    );
                    update_status(&cluster, &context.client, &namespace, &name, false).await?;
                    return Ok(Action::requeue(Duration::from_secs(15)));
                }
            };
    }

    let mut client = client.expect("a staged or cached client was just selected");
    match fetch_cluster_status(
        &mut client,
        &cluster,
        &secret,
        &physical_server_ca,
        &context.client,
    )
    .await
    {
        Ok(desired) => {
            // The handoff ConfigMap is part of cluster readiness. Reconcile and
            // verify its ownership before publishing either the client or a
            // Ready condition, so an exact-name collision cannot create a
            // transient tenant-provisioning window.
            ensure_cluster_configmap(&cluster, &context.client, &namespace, &physical_server_ca)
                .await?;
            // Publishing only after full observation and handoff creation
            // prevents a concurrently starting tenant controller from using
            // stale persisted Ready state with an unvalidated replacement
            // client or missing connection artifact.
            context.clients.write().await.insert(key.clone(), client);
            context
                .config_hashes
                .write()
                .unwrap_or_else(|p| p.into_inner())
                .insert(key.clone(), config_hash);
            patch_status_if_changed(&cluster, &context.client, &namespace, &name, desired).await?;
        }
        Err(err) => {
            warn!(
                namespace,
                name,
                error = %err,
                error_debug = ?err,
                error_chain = %crate::format_error_chain(&err),
                "health or physical-identity check failed, marking disconnected"
            );
            context.clients.write().await.remove(&key);
            context
                .config_hashes
                .write()
                .unwrap_or_else(|p| p.into_inner())
                .remove(&key);
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
    warn!(
        error = %error,
        error_debug = ?error,
        error_chain = %crate::format_error_chain(error),
        "applying EtcdCluster error policy"
    );
    Action::requeue(Duration::from_secs(60))
}

async fn referencing_tenants(
    client: &Client,
    cluster: &EtcdCluster,
) -> Result<Vec<String>, ClusterError> {
    let cluster_namespace = cluster
        .namespace()
        .ok_or_else(|| ClusterError::Invalid(format!("{} has no namespace", cluster.name_any())))?;
    let cluster_name = cluster.name_any();
    let cluster_uid = cluster.uid().ok_or_else(|| {
        ClusterError::Invalid(format!(
            "{cluster_namespace}/{cluster_name} has no metadata.uid"
        ))
    })?;
    let tenants = Api::<EtcdTenant>::all(client.clone())
        .list(&Default::default())
        .await?;
    Ok(tenants
        .items
        .into_iter()
        .filter_map(|tenant| {
            let tenant_namespace = tenant.namespace()?;
            let references_cluster =
                tenant_references_cluster(&tenant, &cluster_namespace, &cluster_name, &cluster_uid);
            references_cluster.then(|| format!("{tenant_namespace}/{}", tenant.name_any()))
        })
        .collect())
}

fn tenant_references_cluster(
    tenant: &EtcdTenant,
    cluster_namespace: &str,
    cluster_name: &str,
    cluster_uid: &str,
) -> bool {
    if let Some(status) = tenant.status.as_ref()
        && let Some(identity) = status.external_identity.as_ref()
    {
        return identity.cluster_ref.uid == cluster_uid
            && identity.cluster_ref.namespace == cluster_namespace
            && identity.cluster_ref.name == cluster_name;
    }

    // The old controller wrote its finalizer before it had a status identity,
    // and a namespace that was once authorized may have been removed from
    // allowedNamespaces without the old controller revoking its external
    // access. Preserve deletion ordering for that exact legacy shape even
    // when it is currently unauthorized. New raw spec references without the
    // controller finalizer still cannot hold a cluster hostage.
    let Some(tenant_namespace) = tenant.namespace() else {
        return false;
    };
    let legacy_finalizer = tenant.meta().finalizers.as_ref().is_some_and(|finalizers| {
        finalizers
            .iter()
            .any(|finalizer| finalizer == crate::tenant::TENANT_FINALIZER)
    });
    let referenced_namespace = tenant
        .spec
        .cluster_ref
        .namespace
        .as_deref()
        .unwrap_or(&tenant_namespace);
    legacy_finalizer
        && referenced_namespace == cluster_namespace
        && tenant.spec.cluster_ref.name == cluster_name
}

async fn ensure_cluster_finalizer(
    client: &Client,
    cluster: &EtcdCluster,
    namespace: &str,
) -> Result<bool, ClusterError> {
    if has_cluster_finalizer(cluster) {
        return Ok(false);
    }
    let resource_version = cluster.meta().resource_version.as_deref().ok_or_else(|| {
        ClusterError::Invalid(format!(
            "{} has no metadata.resourceVersion",
            cluster.name_any()
        ))
    })?;
    let api = Api::<EtcdCluster>::namespaced(client.clone(), namespace);
    let patch: json_patch::Patch = if cluster.meta().finalizers.is_some() {
        serde_json::from_value(serde_json::json!([
            { "op": "test", "path": "/metadata/resourceVersion", "value": resource_version },
            { "op": "add", "path": "/metadata/finalizers/-", "value": CLUSTER_FINALIZER }
        ]))
        .expect("cluster finalizer JSON patch is valid")
    } else {
        serde_json::from_value(serde_json::json!([
            { "op": "test", "path": "/metadata/resourceVersion", "value": resource_version },
            { "op": "add", "path": "/metadata/finalizers", "value": [CLUSTER_FINALIZER] }
        ]))
        .expect("cluster finalizer JSON patch is valid")
    };
    api.patch(
        &cluster.name_any(),
        &PatchParams::default(),
        &Patch::Json::<()>(patch),
    )
    .await?;
    Ok(true)
}

async fn remove_cluster_finalizer(
    client: &Client,
    cluster: &EtcdCluster,
    namespace: &str,
) -> Result<(), ClusterError> {
    let Some(index) = cluster.meta().finalizers.as_ref().and_then(|finalizers| {
        finalizers
            .iter()
            .position(|finalizer| finalizer == CLUSTER_FINALIZER)
    }) else {
        return Ok(());
    };
    let path = format!("/metadata/finalizers/{index}");
    let resource_version = cluster.meta().resource_version.as_deref().ok_or_else(|| {
        ClusterError::Invalid(format!(
            "{} has no metadata.resourceVersion",
            cluster.name_any()
        ))
    })?;
    let patch: json_patch::Patch = serde_json::from_value(serde_json::json!([
        { "op": "test", "path": "/metadata/resourceVersion", "value": resource_version },
        { "op": "test", "path": &path, "value": CLUSTER_FINALIZER },
        { "op": "remove", "path": &path }
    ]))
    .expect("cluster finalizer JSON patch is valid");
    let api = Api::<EtcdCluster>::namespaced(client.clone(), namespace);
    api.patch(
        &cluster.name_any(),
        &PatchParams::default(),
        &Patch::Json::<()>(patch),
    )
    .await?;
    Ok(())
}

fn has_cluster_finalizer(cluster: &EtcdCluster) -> bool {
    cluster
        .meta()
        .finalizers
        .as_ref()
        .is_some_and(|finalizers| {
            finalizers
                .iter()
                .any(|finalizer| finalizer == CLUSTER_FINALIZER)
        })
}

async fn read_physical_server_ca(
    client: &Client,
    cluster: &EtcdCluster,
    auth_secret: &Secret,
) -> Result<Vec<u8>, ClusterError> {
    if let Some(reference) = &cluster.spec.server_ca_config_map_ref {
        let namespace = cluster.namespace().ok_or_else(|| {
            ClusterError::Invalid(format!("{} has no namespace", cluster.name_any()))
        })?;
        let configmaps = Api::<ConfigMap>::namespaced(client.clone(), &namespace);
        let configmap = configmaps.get(&reference.name).await?;
        let value = configmap
            .data
            .as_ref()
            .and_then(|data| data.get(&reference.key))
            .map(|value| value.as_bytes().to_vec())
            .or_else(|| {
                configmap
                    .binary_data
                    .as_ref()
                    .and_then(|data| data.get(&reference.key))
                    .map(|value| value.0.clone())
            })
            .ok_or_else(|| {
                ClusterError::Invalid(format!(
                    "server CA ConfigMap {}/{} missing key {}",
                    namespace, reference.name, reference.key
                ))
            })?;
        if value.is_empty() {
            return Err(ClusterError::Invalid(format!(
                "server CA ConfigMap {}/{} key {} is empty",
                namespace, reference.name, reference.key
            )));
        }
        return Ok(value);
    }

    let ca = auth_secret
        .data
        .as_ref()
        .and_then(|data| data.get("ca.crt"))
        .ok_or_else(|| {
            ClusterError::Invalid(format!(
                "auth Secret {} missing fallback key ca.crt",
                auth_secret.name_any()
            ))
        })?;
    if ca.0.is_empty() {
        return Err(ClusterError::Invalid(format!(
            "auth Secret {} fallback key ca.crt is empty",
            auth_secret.name_any()
        )));
    }
    Ok(ca.0.clone())
}

fn compute_config_hash(spec: &EtcdClusterSpec, secret: &Secret, physical_server_ca: &[u8]) -> u64 {
    let mut hasher = DefaultHasher::new();

    for endpoint in &spec.endpoints {
        endpoint.hash(&mut hasher);
    }

    spec.auth_secret_ref.name.hash(&mut hasher);
    physical_server_ca.hash(&mut hasher);

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

struct EtcdObservation {
    endpoint_statuses: Vec<(String, etcd_client::StatusResponse)>,
    members: etcd_client::MemberListResponse,
    alarms: etcd_client::AlarmResponse,
    cluster_id: u64,
    auth_enabled: bool,
}

async fn observe_etcd(
    client: &mut etcd_client::Client,
    cluster: &EtcdCluster,
    auth_secret: &Secret,
    physical_server_ca: &[u8],
) -> Result<EtcdObservation, ClusterError> {
    let (endpoint_statuses, endpoint_auth_statuses) = tokio::try_join!(
        fetch_endpoint_statuses(cluster, auth_secret, physical_server_ca),
        fetch_endpoint_auth_statuses(cluster, auth_secret, physical_server_ca),
    )?;
    let members = client
        .member_list()
        .await
        .map_err(|err| ClusterError::Etcd(anyhow!(err.to_string())))?;
    let cluster_id = validate_endpoint_statuses(&endpoint_statuses, &members)?;
    let auth_enabled =
        validate_auth_statuses(&endpoint_statuses, &endpoint_auth_statuses, cluster_id)?;
    let alarms = client
        .alarm(
            etcd_client::AlarmAction::Get,
            etcd_client::AlarmType::None,
            None,
        )
        .await
        .map_err(|err| ClusterError::Etcd(anyhow!(err.to_string())))?;
    validate_alarm_response(&endpoint_statuses, &members, &alarms)?;

    Ok(EtcdObservation {
        endpoint_statuses,
        members,
        alarms,
        cluster_id,
        auth_enabled,
    })
}

async fn fetch_cluster_status(
    client: &mut etcd_client::Client,
    cluster: &EtcdCluster,
    auth_secret: &Secret,
    physical_server_ca: &[u8],
    kube_client: &Client,
) -> Result<EtcdClusterStatus, ClusterError> {
    let observation = observe_etcd(client, cluster, auth_secret, physical_server_ca).await?;
    let observed_cluster_id = format_cluster_id(observation.cluster_id);
    let current_cluster_id = cluster
        .status
        .as_ref()
        .and_then(|status| status.cluster_id.as_deref());
    if let Some(pinned) = current_cluster_id
        && pinned != observed_cluster_id
    {
        return Err(ClusterError::Invalid(format!(
            "observed physical etcd cluster ID {observed_cluster_id} differs from durable status.clusterId {pinned}; create a new EtcdCluster instead of repointing this object"
        )));
    }

    let mut desired = to_status(
        true,
        observation.auth_enabled,
        observation
            .endpoint_statuses
            .first()
            .map(|(_, status)| status),
        Some(
            &observation
                .endpoint_statuses
                .iter()
                .map(|(_, status)| status.version().to_string())
                .collect::<Vec<_>>(),
        ),
        Some(&observation.members),
        Some(&observation.alarms),
        current_cluster_id.map(str::to_string),
        cluster.meta().generation,
        cluster.status.as_ref(),
    );

    let otherwise_ready = desired.conditions.iter().any(|condition| {
        condition.type_ == "Ready" && condition.status == crate::crd::ConditionStatus::True
    });
    if current_cluster_id.is_none() && otherwise_ready {
        let references = referencing_tenants(kube_client, cluster).await?;
        if references.is_empty() || accepts_cluster_id(cluster, &observed_cluster_id) {
            desired.cluster_id = Some(observed_cluster_id);
        } else {
            desired.conditions = vec![crate::crd::ready_condition_with_existing(
                false,
                "ClusterIdentityAcceptanceRequired",
                &format!(
                    "qualified physical etcd cluster ID {observed_cluster_id} cannot be pinned while legacy/proven tenants reference this object; set metadata.annotations[{CLUSTER_ID_ACCEPTANCE_ANNOTATION:?}] to that exact value after attended inventory"
                ),
                cluster.meta().generation,
                cluster
                    .status
                    .as_ref()
                    .map(|status| status.conditions.as_slice())
                    .unwrap_or(&[]),
            )];
        }
    }

    Ok(desired)
}

pub(crate) async fn verify_cluster_for_mutation(
    client: &mut etcd_client::Client,
    cluster: &EtcdCluster,
    auth_secret: &Secret,
    physical_server_ca: &[u8],
    require_ready: bool,
) -> Result<(), ClusterError> {
    let pinned = cluster
        .status
        .as_ref()
        .and_then(|status| status.cluster_id.as_deref())
        .ok_or_else(|| {
            ClusterError::Invalid(
                "status.clusterId is not durably pinned; refusing external auth mutation"
                    .to_string(),
            )
        })?;
    let observation = observe_etcd(client, cluster, auth_secret, physical_server_ca).await?;
    let observed = format_cluster_id(observation.cluster_id);
    if observed != pinned {
        return Err(ClusterError::Invalid(format!(
            "fresh physical etcd cluster ID {observed} differs from durable status.clusterId {pinned}; refusing external auth mutation"
        )));
    }
    if !observation.auth_enabled {
        return Err(ClusterError::Invalid(
            "fresh AuthStatus proof reports etcd RBAC authentication disabled; refusing external auth mutation"
                .to_string(),
        ));
    }
    if require_ready {
        let versions = observation
            .endpoint_statuses
            .iter()
            .map(|(_, status)| status.version())
            .collect::<BTreeSet<_>>();
        if versions.len() != 1 || !versions.contains(QUALIFIED_ETCD_VERSION) {
            return Err(ClusterError::Invalid(format!(
                "fresh physical etcd version observation {versions:?} is not exactly {QUALIFIED_ETCD_VERSION}"
            )));
        }
        if !observation.alarms.alarms().is_empty() {
            return Err(ClusterError::Invalid(
                "fresh physical etcd observation reports active alarms".to_string(),
            ));
        }
        if observation
            .members
            .members()
            .iter()
            .any(|member| member.is_learner())
        {
            return Err(ClusterError::Invalid(
                "fresh physical etcd membership observation includes a learner; refusing new provisioning"
                    .to_string(),
            ));
        }
    }
    Ok(())
}

fn format_cluster_id(cluster_id: u64) -> String {
    format!("{cluster_id:016x}")
}

fn accepts_cluster_id(cluster: &EtcdCluster, cluster_id: &str) -> bool {
    cluster
        .metadata
        .annotations
        .as_ref()
        .and_then(|annotations| annotations.get(CLUSTER_ID_ACCEPTANCE_ANNOTATION))
        .is_some_and(|accepted| accepted == cluster_id)
}

async fn fetch_endpoint_statuses(
    cluster: &EtcdCluster,
    auth_secret: &Secret,
    physical_server_ca: &[u8],
) -> Result<Vec<(String, etcd_client::StatusResponse)>, ClusterError> {
    let checks = cluster.spec.endpoints.iter().map(|endpoint| async move {
        let mut endpoint_client = crate::etcd::build_client(
            std::slice::from_ref(endpoint),
            auth_secret,
            physical_server_ca,
        )
        .await
        .map_err(|error| {
            ClusterError::Etcd(anyhow!(
                "failed to connect directly to configured endpoint {endpoint}: {error:#}"
            ))
        })?;
        let status = endpoint_client.status().await.map_err(|error| {
            ClusterError::Etcd(anyhow!(
                "status failed for configured endpoint {endpoint}: {error}"
            ))
        })?;
        Ok::<_, ClusterError>((endpoint.clone(), status))
    });

    futures::future::try_join_all(checks).await
}

async fn fetch_endpoint_auth_statuses(
    cluster: &EtcdCluster,
    auth_secret: &Secret,
    physical_server_ca: &[u8],
) -> Result<Vec<(String, crate::etcd::AdminAuthStatus)>, ClusterError> {
    let checks = cluster.spec.endpoints.iter().map(|endpoint| async move {
        let status = crate::etcd::fetch_auth_status(endpoint, auth_secret, physical_server_ca)
            .await
            .map_err(|error| {
                ClusterError::Etcd(anyhow!(
                    "failed direct AuthStatus proof for configured endpoint {endpoint}: {error:#}"
                ))
            })?;
        Ok::<_, ClusterError>((endpoint.clone(), status))
    });

    futures::future::try_join_all(checks).await
}

fn validate_auth_statuses(
    statuses: &[(String, etcd_client::StatusResponse)],
    auth_statuses: &[(String, crate::etcd::AdminAuthStatus)],
    expected_cluster_id: u64,
) -> Result<bool, ClusterError> {
    if auth_statuses.len() != statuses.len() {
        return Err(ClusterError::Invalid(format!(
            "received {} AuthStatus responses for {} configured endpoints",
            auth_statuses.len(),
            statuses.len()
        )));
    }
    let by_endpoint = auth_statuses
        .iter()
        .map(|(endpoint, status)| (endpoint, status))
        .collect::<BTreeMap<_, _>>();
    if by_endpoint.len() != auth_statuses.len() {
        return Err(ClusterError::Invalid(
            "received duplicate AuthStatus endpoint observations".to_string(),
        ));
    }

    let mut enabled_values = BTreeSet::new();
    let mut auth_revisions = BTreeSet::new();
    for (endpoint, status) in statuses {
        let auth = by_endpoint.get(endpoint).ok_or_else(|| {
            ClusterError::Invalid(format!(
                "configured endpoint {endpoint} has no matching AuthStatus response"
            ))
        })?;
        let status_header = status.header().ok_or_else(|| {
            ClusterError::Invalid(format!(
                "configured endpoint {endpoint} returned status without a header"
            ))
        })?;
        if auth.cluster_id == 0 || auth.member_id == 0 || auth.revision <= 0 {
            return Err(ClusterError::Invalid(format!(
                "configured endpoint {endpoint} returned AuthStatus with a zero identity or non-positive revision"
            )));
        }
        if auth.cluster_id != expected_cluster_id
            || auth.cluster_id != status_header.cluster_id()
            || auth.member_id != status_header.member_id()
        {
            return Err(ClusterError::Invalid(format!(
                "configured endpoint {endpoint} AuthStatus response does not match its direct status response and physical cluster identity"
            )));
        }
        if auth.enabled && auth.auth_revision == 0 {
            return Err(ClusterError::Invalid(format!(
                "configured endpoint {endpoint} reports auth enabled with a zero auth revision"
            )));
        }
        enabled_values.insert(auth.enabled);
        auth_revisions.insert(auth.auth_revision);
    }
    if enabled_values.len() != 1 {
        return Err(ClusterError::Invalid(
            "configured endpoints disagree whether etcd RBAC authentication is enabled".to_string(),
        ));
    }
    if auth_revisions.len() != 1 {
        return Err(ClusterError::Invalid(
            "configured endpoints disagree on the etcd auth revision".to_string(),
        ));
    }
    Ok(enabled_values.contains(&true))
}

fn validate_endpoint_statuses(
    statuses: &[(String, etcd_client::StatusResponse)],
    members: &etcd_client::MemberListResponse,
) -> Result<u64, ClusterError> {
    if statuses.is_empty() {
        return Err(ClusterError::Invalid(
            "EtcdCluster has no configured endpoint status".to_string(),
        ));
    }

    let mut cluster_ids = HashSet::new();
    let mut endpoint_member_ids = HashSet::new();
    let mut leader_ids = HashSet::new();
    for (endpoint, status) in statuses {
        let header = status.header().ok_or_else(|| {
            ClusterError::Invalid(format!(
                "configured endpoint {endpoint} returned status without a header"
            ))
        })?;
        if header.cluster_id() == 0 || header.member_id() == 0 {
            return Err(ClusterError::Invalid(format!(
                "configured endpoint {endpoint} returned a zero cluster or member ID"
            )));
        }
        if status.leader() == 0 {
            return Err(ClusterError::Invalid(format!(
                "configured endpoint {endpoint} reports no etcd leader"
            )));
        }
        // StatusResponse.errors is not an independent health gate in etcd
        // 3.6.13: it repeats every active alarm (and the no-leader error just
        // checked above). The Alarm RPC below remains the authoritative alarm
        // observation so NOSPACE/CORRUPT stays visible as AlarmsActive rather
        // than being flattened into Disconnected.
        cluster_ids.insert(header.cluster_id());
        leader_ids.insert(status.leader());
        if !endpoint_member_ids.insert(header.member_id()) {
            return Err(ClusterError::Invalid(format!(
                "configured endpoints do not resolve to distinct etcd members; member {} was observed more than once",
                header.member_id()
            )));
        }
    }
    if cluster_ids.len() != 1 {
        return Err(ClusterError::Invalid(
            "configured endpoints returned status from different etcd cluster IDs".to_string(),
        ));
    }
    if leader_ids.len() != 1 {
        return Err(ClusterError::Invalid(
            "configured endpoints disagree about the current etcd leader".to_string(),
        ));
    }

    let expected_cluster_id = *cluster_ids
        .iter()
        .next()
        .expect("one endpoint cluster ID was just proven");
    let member_header = members
        .header()
        .ok_or_else(|| ClusterError::Invalid("member-list response has no header".to_string()))?;
    if member_header.cluster_id() != expected_cluster_id {
        return Err(ClusterError::Invalid(format!(
            "member-list response cluster ID {} differs from configured endpoint cluster ID {expected_cluster_id}",
            member_header.cluster_id()
        )));
    }
    if member_header.member_id() == 0 || !endpoint_member_ids.contains(&member_header.member_id()) {
        return Err(ClusterError::Invalid(format!(
            "member-list response came from unconfigured member ID {}",
            member_header.member_id()
        )));
    }

    let listed_member_ids = members
        .members()
        .iter()
        .map(|member| member.id())
        .collect::<HashSet<_>>();
    if listed_member_ids.contains(&0) || listed_member_ids.len() != members.members().len() {
        return Err(ClusterError::Invalid(
            "member-list response contains a zero or duplicate member ID".to_string(),
        ));
    }
    if endpoint_member_ids != listed_member_ids {
        return Err(ClusterError::Invalid(format!(
            "configured endpoint member IDs {endpoint_member_ids:?} do not exactly match member-list IDs {listed_member_ids:?}"
        )));
    }
    if !leader_ids.is_subset(&listed_member_ids) {
        return Err(ClusterError::Invalid(
            "configured endpoints report a leader absent from the etcd member list".to_string(),
        ));
    }

    Ok(expected_cluster_id)
}

fn validate_alarm_response(
    statuses: &[(String, etcd_client::StatusResponse)],
    members: &etcd_client::MemberListResponse,
    alarms: &etcd_client::AlarmResponse,
) -> Result<(), ClusterError> {
    let expected_cluster_id = statuses
        .first()
        .and_then(|(_, status)| status.header())
        .map(|header| header.cluster_id())
        .filter(|cluster_id| *cluster_id != 0)
        .ok_or_else(|| {
            ClusterError::Invalid(
                "cannot validate alarm response without a configured endpoint cluster ID"
                    .to_string(),
            )
        })?;
    let configured_member_ids = statuses
        .iter()
        .filter_map(|(_, status)| status.header().map(|header| header.member_id()))
        .collect::<HashSet<_>>();
    let listed_member_ids = members
        .members()
        .iter()
        .map(|member| member.id())
        .collect::<HashSet<_>>();
    let alarm_header = alarms
        .header()
        .ok_or_else(|| ClusterError::Invalid("alarm response has no header".to_string()))?;
    if alarm_header.cluster_id() != expected_cluster_id {
        return Err(ClusterError::Invalid(format!(
            "alarm response cluster ID {} differs from configured endpoint cluster ID {expected_cluster_id}",
            alarm_header.cluster_id()
        )));
    }
    if alarm_header.member_id() == 0 || !configured_member_ids.contains(&alarm_header.member_id()) {
        return Err(ClusterError::Invalid(format!(
            "alarm response came from unconfigured member ID {}",
            alarm_header.member_id()
        )));
    }
    if let Some(member_id) = alarms
        .alarms()
        .iter()
        .map(|alarm| alarm.member_id())
        .find(|member_id| *member_id == 0 || !listed_member_ids.contains(member_id))
    {
        return Err(ClusterError::Invalid(format!(
            "alarm response names unknown member ID {member_id}"
        )));
    }
    Ok(())
}

async fn ensure_cluster_configmap(
    cluster: &EtcdCluster,
    client: &Client,
    namespace: &str,
    physical_server_ca: &[u8],
) -> Result<(), ClusterError> {
    let ca_crt = String::from_utf8(physical_server_ca.to_vec())
        .map_err(|_| ClusterError::Invalid("ca.crt is not valid UTF-8".to_string()))?;

    let owner_reference = cluster_owner_reference(cluster)?;

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
    match configmaps.get(&configmap_name).await {
        Ok(existing) => {
            ensure_configmap_owned_by_cluster(&existing.metadata, &configmap_name, cluster)?
        }
        Err(kube::Error::Api(ae)) if ae.code == 404 => {}
        Err(error) => return Err(error.into()),
    }
    configmaps
        .patch(
            &configmap_name,
            &PatchParams::apply("etcdetcetc").force(),
            &Patch::Apply(&configmap),
        )
        .await?;

    Ok(())
}

fn cluster_owner_reference(
    cluster: &EtcdCluster,
) -> Result<k8s_openapi::apimachinery::pkg::apis::meta::v1::OwnerReference, ClusterError> {
    let mut owner_reference = cluster.controller_owner_ref(&()).ok_or_else(|| {
        ClusterError::Invalid(format!(
            "{} cannot produce controller owner reference",
            cluster.name_any()
        ))
    })?;
    // The controller intentionally has no delete permission on EtcdCluster.
    // Its finalizer supplies lifecycle ordering, so dependents need not request
    // blockOwnerDeletion and trigger OwnerReferencesPermissionEnforcement.
    owner_reference.block_owner_deletion = Some(false);
    Ok(owner_reference)
}

fn ensure_configmap_owned_by_cluster(
    metadata: &ObjectMeta,
    name: &str,
    cluster: &EtcdCluster,
) -> Result<(), ClusterError> {
    let cluster_uid = cluster.uid().ok_or_else(|| {
        ClusterError::Invalid(format!("{} has no metadata.uid", cluster.name_any()))
    })?;
    let owned = metadata.owner_references.as_ref().is_some_and(|owners| {
        owners.iter().any(|owner| {
            owner.uid == cluster_uid
                && owner.kind == "EtcdCluster"
                && owner.api_version == "etcdetcetc.samcday.com/v1alpha1"
                && owner.controller == Some(true)
        })
    });
    if !owned {
        return Err(ClusterError::Invalid(format!(
            "refusing to manage ConfigMap {name:?}: it is not controlled by EtcdCluster UID {cluster_uid}"
        )));
    }
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

// Keeping each independently observed etcd signal explicit makes it harder to
// accidentally reuse stale state while constructing a fail-closed status.
#[allow(clippy::too_many_arguments)]
fn to_status(
    connected: bool,
    auth_enabled: bool,
    status: Option<&etcd_client::StatusResponse>,
    observed_versions: Option<&[String]>,
    members: Option<&etcd_client::MemberListResponse>,
    alarms: Option<&etcd_client::AlarmResponse>,
    cluster_id: Option<String>,
    observed_generation: Option<i64>,
    current_status: Option<&EtcdClusterStatus>,
) -> EtcdClusterStatus {
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

    let version = if connected {
        observed_versions
            .and_then(summarize_observed_versions)
            .or_else(|| status.map(|response| response.version().to_owned()))
    } else {
        None
    };
    let has_learners = member_list.iter().any(|member| member.is_learner);
    let (ready, reason, message) = readiness_for_state(
        connected,
        auth_enabled,
        version.as_deref(),
        &alarm_list,
        has_learners,
    );

    EtcdClusterStatus {
        connected,
        auth_enabled,
        conditions: vec![crate::crd::ready_condition_with_existing(
            ready,
            reason,
            &message,
            observed_generation,
            existing_conditions,
        )],
        version,
        cluster_id,
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

fn summarize_observed_versions(versions: &[String]) -> Option<String> {
    let unique = versions.iter().map(String::as_str).collect::<BTreeSet<_>>();
    match unique.len() {
        0 => None,
        1 => unique.first().map(|version| (*version).to_string()),
        _ => Some(format!(
            "mixed:{}",
            unique.into_iter().collect::<Vec<_>>().join(",")
        )),
    }
}

fn readiness_for_state(
    connected: bool,
    auth_enabled: bool,
    version: Option<&str>,
    alarms: &[String],
    has_learners: bool,
) -> (bool, &'static str, String) {
    if !connected {
        (
            false,
            "Disconnected",
            "etcd cluster is not reachable".to_string(),
        )
    } else if !auth_enabled {
        (
            false,
            "AuthDisabled",
            "etcd RBAC authentication is disabled".to_string(),
        )
    } else if version != Some(QUALIFIED_ETCD_VERSION) {
        (
            false,
            "UnsupportedVersion",
            format!(
                "etcd server version {} is not the qualified version {QUALIFIED_ETCD_VERSION}",
                version.unwrap_or("unknown")
            ),
        )
    } else if !alarms.is_empty() {
        (
            false,
            "AlarmsActive",
            format!("etcd reports active alarms: {}", alarms.join(", ")),
        )
    } else if has_learners {
        (
            false,
            "LearnersPresent",
            "etcd membership includes at least one learner".to_string(),
        )
    } else {
        (true, "Connected", String::new())
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

    replace_cluster_status_snapshot(current, client, namespace, name, desired).await
}

async fn update_status(
    current: &EtcdCluster,
    client: &Client,
    namespace: &str,
    name: &str,
    connected: bool,
) -> Result<(), ClusterError> {
    let desired = to_status(
        connected,
        false,
        None,
        None,
        None,
        None,
        current
            .status
            .as_ref()
            .and_then(|status| status.cluster_id.clone()),
        current.meta().generation,
        current.status.as_ref(),
    );
    patch_status_if_changed(current, client, namespace, name, desired).await
}

async fn force_update_status(
    current: &EtcdCluster,
    client: &Client,
    namespace: &str,
    name: &str,
    connected: bool,
) -> Result<(), ClusterError> {
    let desired = to_status(
        connected,
        false,
        None,
        None,
        None,
        None,
        current
            .status
            .as_ref()
            .and_then(|status| status.cluster_id.clone()),
        current.meta().generation,
        current.status.as_ref(),
    );
    replace_cluster_status_snapshot(current, client, namespace, name, desired).await
}

async fn replace_cluster_status_snapshot(
    current: &EtcdCluster,
    client: &Client,
    namespace: &str,
    name: &str,
    desired: EtcdClusterStatus,
) -> Result<(), ClusterError> {
    let replacement = cluster_status_replacement_snapshot(current, namespace, name, desired)?;
    let body = serde_json::to_vec(&replacement).map_err(|error| {
        ClusterError::Invalid(format!(
            "could not serialize EtcdCluster status replacement: {error}"
        ))
    })?;
    Api::<EtcdCluster>::namespaced(client.clone(), namespace)
        .replace_status(name, &PostParams::default(), body)
        .await?;
    Ok(())
}

fn cluster_status_replacement_snapshot(
    current: &EtcdCluster,
    namespace: &str,
    name: &str,
    desired: EtcdClusterStatus,
) -> Result<EtcdCluster, ClusterError> {
    if current.namespace().as_deref() != Some(namespace) || current.name_any() != name {
        return Err(ClusterError::Invalid(
            "status replacement target differs from the reconciled EtcdCluster snapshot"
                .to_string(),
        ));
    }
    current.uid().ok_or_else(|| {
        ClusterError::Invalid(format!(
            "EtcdCluster {namespace}/{name} has no metadata.uid"
        ))
    })?;
    current.meta().resource_version.as_deref().ok_or_else(|| {
        ClusterError::Invalid(format!(
            "EtcdCluster {namespace}/{name} has no metadata.resourceVersion"
        ))
    })?;

    let mut replacement = current.clone();
    replacement.status = Some(desired);
    Ok(replacement)
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::{Path, PathBuf};

    use super::*;
    use crate::crd::{
        ClusterReference, EtcdClusterSpec, EtcdTenantSpec, EtcdTenantStatus, LocalSecretReference,
        PinnedClusterReference, TenantCredentialMode, TenantExternalAccessState,
        TenantExternalIdentity,
    };

    #[test]
    fn controller_excludes_physical_member_lifecycle_rpcs() {
        let source_root = Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
        let production_sources = rust_source_paths(&source_root);
        assert!(
            !production_sources.is_empty(),
            "no production Rust sources found under {}",
            source_root.display()
        );

        let member_word = ["mem", "ber"].concat();

        for operation in ["add", "remove", "update", "promote"] {
            let camel_member = title_case(&member_word);
            let camel_operation = title_case(operation);
            for fixture in [
                format!("{member_word}_{operation}"),
                format!("{camel_member}{camel_operation}Request"),
                format!("/etcdserverpb.Cluster/{camel_member}{camel_operation}"),
                format!("{member_word}/{operation}"),
                format!("{member_word}{operation}"),
            ] {
                assert!(
                    source_mentions_member_mutation(&fixture, &member_word, operation),
                    "source matcher did not recognize forbidden physical etcd lifecycle spelling {fixture}"
                );
            }

            for path in &production_sources {
                let source = fs::read_to_string(path)
                    .unwrap_or_else(|error| panic!("cannot read {}: {error}", path.display()));
                assert!(
                    !source_mentions_member_mutation(&source, &member_word, operation),
                    "{} contains a forbidden physical etcd member {operation} API symbol or path",
                    path.display()
                );
            }
        }

        let non_mutation = format!("{member_word}_address");
        assert!(!source_mentions_member_mutation(
            &non_mutation,
            &member_word,
            "add"
        ));
    }

    fn rust_source_paths(root: &Path) -> Vec<PathBuf> {
        let mut directories = vec![root.to_path_buf()];
        let mut sources = Vec::new();

        while let Some(directory) = directories.pop() {
            let entries = fs::read_dir(&directory)
                .unwrap_or_else(|error| panic!("cannot read {}: {error}", directory.display()));
            for entry in entries {
                let entry = entry.unwrap_or_else(|error| {
                    panic!(
                        "cannot read an entry under {}: {error}",
                        directory.display()
                    )
                });
                let path = entry.path();
                let file_type = entry
                    .file_type()
                    .unwrap_or_else(|error| panic!("cannot inspect {}: {error}", path.display()));
                assert!(
                    !file_type.is_symlink(),
                    "production source traversal refuses symlink {}",
                    path.display()
                );
                if file_type.is_dir() {
                    directories.push(path);
                } else if file_type.is_file()
                    && path.extension().is_some_and(|extension| extension == "rs")
                {
                    sources.push(path);
                }
            }
        }

        sources.sort();
        sources
    }

    fn source_mentions_member_mutation(source: &str, member: &str, operation: &str) -> bool {
        let words = source_contract_words(source);
        let joined = format!("{member}{operation}");
        words
            .windows(2)
            .any(|pair| pair[0] == member && pair[1] == operation)
            || words.iter().any(|word| word == &joined)
    }

    fn source_contract_words(source: &str) -> Vec<String> {
        let characters: Vec<_> = source.chars().collect();
        let mut words = Vec::new();
        let mut word = String::new();

        for (index, character) in characters.iter().copied().enumerate() {
            if !character.is_ascii_alphanumeric() {
                if !word.is_empty() {
                    words.push(std::mem::take(&mut word));
                }
                continue;
            }

            let previous = index
                .checked_sub(1)
                .and_then(|offset| characters.get(offset));
            let next = characters.get(index + 1);
            let camel_case_boundary = character.is_ascii_uppercase()
                && !word.is_empty()
                && (previous
                    .is_some_and(|value| value.is_ascii_lowercase() || value.is_ascii_digit())
                    || (previous.is_some_and(|value| value.is_ascii_uppercase())
                        && next.is_some_and(|value| value.is_ascii_lowercase())));
            if camel_case_boundary {
                words.push(std::mem::take(&mut word));
            }
            word.push(character.to_ascii_lowercase());
        }

        if !word.is_empty() {
            words.push(word);
        }
        words
    }

    fn title_case(value: &str) -> String {
        let mut characters = value.chars();
        characters
            .next()
            .map(|first| first.to_ascii_uppercase().to_string() + characters.as_str())
            .unwrap_or_default()
    }

    fn cluster_fixture() -> EtcdCluster {
        let mut cluster = EtcdCluster::new(
            "physical",
            EtcdClusterSpec {
                endpoints: vec!["https://etcd.invalid:2379".to_string()],
                auth_secret_ref: LocalSecretReference {
                    name: "root".to_string(),
                },
                server_ca_config_map_ref: None,
                allowed_namespaces: Vec::new(),
                tenant_tls: None,
            },
        );
        cluster.metadata.namespace = Some("controller".to_string());
        cluster.metadata.uid = Some("11111111-1111-1111-1111-111111111111".to_string());
        cluster
    }

    fn tenant_fixture() -> EtcdTenant {
        let mut tenant = EtcdTenant::new(
            "api",
            EtcdTenantSpec {
                cluster_ref: ClusterReference {
                    name: "physical".to_string(),
                    namespace: Some("controller".to_string()),
                },
                secret_name: None,
                credential_mode: TenantCredentialMode::Password,
            },
        );
        tenant.metadata.namespace = Some("child".to_string());
        tenant
    }

    fn pinned_identity(uid: &str) -> TenantExternalIdentity {
        TenantExternalIdentity {
            cluster_ref: PinnedClusterReference {
                name: "physical".to_string(),
                namespace: "controller".to_string(),
                uid: uid.to_string(),
            },
            credential_mode: TenantCredentialMode::Password,
            etcd_user: "child:api".to_string(),
            etcd_role: "child:api".to_string(),
            prefix: "/child:api/".to_string(),
            output_secret_name: "api-etcd".to_string(),
            config_map_name: "api-etcd".to_string(),
            password_secret_name: Some("api-etcd-password".to_string()),
            certificate_name: None,
            staging_secret_name: None,
        }
    }

    #[test]
    fn cluster_status_replacement_preserves_uid_and_resource_version() {
        let mut cluster = cluster_fixture();
        cluster.metadata.resource_version = Some("73".to_string());
        let desired = EtcdClusterStatus {
            connected: true,
            cluster_id: Some("0123456789abcdef".to_string()),
            ..EtcdClusterStatus::default()
        };

        let replacement = cluster_status_replacement_snapshot(
            &cluster,
            "controller",
            "physical",
            desired.clone(),
        )
        .unwrap();
        assert_eq!(replacement.uid(), cluster.uid());
        assert_eq!(
            replacement.meta().resource_version,
            cluster.meta().resource_version
        );
        assert_eq!(replacement.status, Some(desired));

        cluster.metadata.resource_version = None;
        let error = cluster_status_replacement_snapshot(
            &cluster,
            "controller",
            "physical",
            EtcdClusterStatus::default(),
        )
        .unwrap_err();
        assert!(error.to_string().contains("metadata.resourceVersion"));
    }

    #[test]
    fn cluster_deletion_requires_pinned_external_provenance() {
        let mut tenant = tenant_fixture();
        assert!(!tenant_references_cluster(
            &tenant,
            "controller",
            "physical",
            "cluster-uid"
        ));

        tenant.metadata.finalizers = Some(vec![crate::tenant::TENANT_FINALIZER.to_string()]);
        // A legacy finalizer is fail-closed even when the namespace is no
        // longer authorized: the old controller may have left live access.
        assert!(tenant_references_cluster(
            &tenant,
            "controller",
            "physical",
            "cluster-uid"
        ));

        tenant.status = Some(EtcdTenantStatus {
            conditions: Vec::new(),
            external_identity: Some(pinned_identity("cluster-uid")),
            external_access_state: Some(TenantExternalAccessState::Planned),
        });
        assert!(tenant_references_cluster(
            &tenant,
            "controller",
            "physical",
            "cluster-uid"
        ));

        tenant.status.as_mut().unwrap().external_access_state =
            Some(TenantExternalAccessState::Provisioning);
        assert!(tenant_references_cluster(
            &tenant,
            "controller",
            "physical",
            "cluster-uid"
        ));
        assert!(!tenant_references_cluster(
            &tenant,
            "different-name-and-namespace",
            "different-name",
            "cluster-uid"
        ));
        assert!(!tenant_references_cluster(
            &tenant,
            "controller",
            "physical",
            "replacement-uid"
        ));
    }

    #[test]
    fn active_alarms_make_a_connected_cluster_not_ready() {
        assert_eq!(
            readiness_for_state(true, true, Some(QUALIFIED_ETCD_VERSION), &[], false),
            (true, "Connected", String::new())
        );
        let alarms = vec!["fabric-az1-cp2: NOSPACE".to_string()];
        assert_eq!(
            readiness_for_state(true, true, Some(QUALIFIED_ETCD_VERSION), &alarms, false,),
            (
                false,
                "AlarmsActive",
                "etcd reports active alarms: fabric-az1-cp2: NOSPACE".to_string()
            )
        );
    }

    #[test]
    fn auth_disabled_or_learners_block_new_provisioning() {
        assert_eq!(
            readiness_for_state(true, false, Some(QUALIFIED_ETCD_VERSION), &[], false),
            (
                false,
                "AuthDisabled",
                "etcd RBAC authentication is disabled".to_string()
            )
        );
        assert_eq!(
            readiness_for_state(true, true, Some(QUALIFIED_ETCD_VERSION), &[], true),
            (
                false,
                "LearnersPresent",
                "etcd membership includes at least one learner".to_string()
            )
        );
    }

    #[test]
    fn only_the_qualified_etcd_version_is_ready() {
        assert_eq!(
            readiness_for_state(true, true, Some("3.6.10"), &[], false),
            (
                false,
                "UnsupportedVersion",
                "etcd server version 3.6.10 is not the qualified version 3.6.13".to_string()
            )
        );
        assert_eq!(
            readiness_for_state(true, true, None, &[], false),
            (
                false,
                "UnsupportedVersion",
                "etcd server version unknown is not the qualified version 3.6.13".to_string()
            )
        );
        assert_eq!(
            summarize_observed_versions(&[
                "3.6.13".to_string(),
                "3.6.10".to_string(),
                "3.6.13".to_string(),
            ]),
            Some("mixed:3.6.10,3.6.13".to_string())
        );
    }

    #[test]
    fn physical_cluster_ids_have_an_exact_durable_representation() {
        assert_eq!(format_cluster_id(1), "0000000000000001");
        assert_eq!(format_cluster_id(u64::MAX), "ffffffffffffffff");

        let mut cluster = cluster_fixture();
        assert!(!accepts_cluster_id(&cluster, "0000000000000001"));
        cluster.metadata.annotations = Some(BTreeMap::from([(
            CLUSTER_ID_ACCEPTANCE_ANNOTATION.to_string(),
            "0000000000000001".to_string(),
        )]));
        assert!(accepts_cluster_id(&cluster, "0000000000000001"));
        assert!(!accepts_cluster_id(&cluster, "0000000000000002"));
    }

    #[test]
    fn cluster_configmap_owner_is_non_blocking_and_must_already_match() {
        let cluster = cluster_fixture();
        let owner = cluster_owner_reference(&cluster).unwrap();
        assert_eq!(owner.controller, Some(true));
        assert_eq!(owner.block_owner_deletion, Some(false));

        let owned = ObjectMeta {
            owner_references: Some(vec![owner]),
            ..ObjectMeta::default()
        };
        assert!(ensure_configmap_owned_by_cluster(&owned, "physical-etcd", &cluster).is_ok());

        let error =
            ensure_configmap_owned_by_cluster(&ObjectMeta::default(), "physical-etcd", &cluster)
                .unwrap_err();
        assert!(error.to_string().contains("refusing to manage ConfigMap"));
    }
}
