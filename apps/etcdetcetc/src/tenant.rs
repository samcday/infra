//! EtcdTenant controller.

use std::{collections::{BTreeMap, BTreeSet}, sync::Arc, time::Duration};

use futures::StreamExt;
use k8s_openapi::{
    ByteString,
    api::{
        core::v1::{ConfigMap, Secret},
        rbac::v1::{
            ClusterRole,
            ClusterRoleBinding,
            PolicyRule,
            Role,
            RoleBinding,
            RoleRef,
            Subject,
        },
    },
};
use kube::{
    Api, Client, Resource, ResourceExt,
    api::{DeleteParams, ListParams, ObjectMeta, Patch, PatchParams, PostParams},
    runtime::{
        Controller,
        controller::Action,
        watcher,
    },
};
use tracing::{error, info, warn};

use crate::{
    cluster::ClusterClients,
    crd::{EtcdCluster, EtcdTenant},
};

const TENANT_FINALIZER: &str = "etcdetcetc.samcday.com/tenant";

/// Shared context for the EtcdTenant controller.
#[derive(Clone)]
pub struct TenantContext {
    /// Kubernetes API client.
    pub client: Client,
    /// Shared EtcdCluster etcd client cache.
    pub clients: ClusterClients,
    /// Restricts reconciliation to these namespaces when non-empty.
    pub allowed_namespaces: Vec<String>,
}

/// Errors produced by EtcdTenant reconciliation.
#[derive(Debug, thiserror::Error)]
pub enum TenantError {
    /// Kubernetes API error.
    #[error("kubernetes API error: {0}")]
    Kube(#[from] kube::Error),

    /// etcd API error.
    #[error("etcd API error: {0}")]
    Etcd(#[from] etcd_client::Error),

    /// Serialization error.
    #[error("serialization error: {0}")]
    Serde(#[from] serde_json::Error),

    /// Invalid or incomplete EtcdTenant/EtcdCluster object.
    #[error("invalid object: {0}")]
    Invalid(String),
}

/// Runs the EtcdTenant controller until the stream ends.
pub async fn run(context: TenantContext) {
    let api = Api::<EtcdTenant>::all(context.client.clone());
    let secret_api = Api::<Secret>::all(context.client.clone());
    let configmap_api = Api::<ConfigMap>::all(context.client.clone());
    let context = Arc::new(context);

    info!("starting EtcdTenant controller");

    Controller::new(api, watcher::Config::default())
        .owns(secret_api, watcher::Config::default())
        .owns(configmap_api, watcher::Config::default())
        .run(reconcile, error_policy, context)
        .for_each(|result| async move {
            match result {
                Ok((obj_ref, action)) => {
                    info!(
                        namespace = obj_ref.namespace.as_deref().unwrap_or_default(),
                        name = obj_ref.name,
                        ?action,
                        "reconciled EtcdTenant"
                    );
                }
                Err(err) => {
                    error!(error = %err, "EtcdTenant reconciliation failed");
                }
            }
        })
        .await;
}

async fn reconcile(tenant: Arc<EtcdTenant>, context: Arc<TenantContext>) -> Result<Action, TenantError> {
    let namespace = tenant
        .namespace()
        .ok_or_else(|| TenantError::Invalid(format!("{} has no namespace", tenant.name_any())))?;
    let name = tenant.name_any();

    if !context.allowed_namespaces.is_empty()
        && !context.allowed_namespaces.iter().any(|ns| ns == &namespace)
    {
        warn!(namespace, name, "tenant namespace not in allowedNamespaces, skipping");
        return Ok(Action::await_change());
    }

    let prefix = tenant
        .spec
        .prefix
        .clone()
        .unwrap_or_else(|| format!("/{name}/"));

    info!(namespace, name, "reconciling EtcdTenant");

    if tenant.meta().deletion_timestamp.is_some() {
        return reconcile_delete(tenant, context, &namespace, &name, &prefix).await;
    }

    ensure_finalizer(&context.client, &tenant, &namespace).await?;

    let cluster_namespace = tenant.spec.cluster_ref.namespace.clone().unwrap_or_else(|| namespace.clone());
    let cluster_name = tenant.spec.cluster_ref.name.clone();
    let clusters = Api::<EtcdCluster>::namespaced(context.client.clone(), &cluster_namespace);
    let cluster = match clusters.get(&cluster_name).await {
        Ok(cluster) => cluster,
        Err(kube::Error::Api(ae)) if ae.code == 404 => {
            warn!(
                tenant_namespace = namespace,
                tenant_name = name,
                cluster_namespace,
                cluster_name,
                "referenced EtcdCluster not found, requeueing"
            );
            update_ready_status(
                &context.client,
                &tenant,
                &namespace,
                &name,
                false,
                "ClusterNotFound",
                "referenced EtcdCluster not found",
            )
            .await?;
            return Ok(Action::requeue(Duration::from_secs(30)));
        }
        Err(err) => return Err(err.into()),
    };

    let mut etcd_client = match get_cluster_client(&context.clients, &cluster_namespace, &cluster_name).await {
        Some(client) => client,
        None => {
            warn!(
                tenant_namespace = namespace,
                tenant_name = name,
                cluster_namespace,
                cluster_name,
                "referenced EtcdCluster not connected yet, requeueing"
            );
            update_ready_status(
                &context.client,
                &tenant,
                &namespace,
                &name,
                false,
                "ClusterNotConnected",
                "referenced EtcdCluster not connected yet",
            )
            .await?;
            return Ok(Action::requeue(Duration::from_secs(30)));
        }
    };

    let secret_name = tenant
        .spec
        .secret_name
        .clone()
        .unwrap_or_else(|| format!("{name}-etcd"));

    let password = ensure_password_secret(&context.client, &tenant, &namespace, &name).await?;

    ensure_tenant_rbac(&mut etcd_client, &name, &prefix, &password).await?;
    ensure_output_secret(
        &context.client,
        &tenant,
        &namespace,
        &secret_name,
        &name,
        &password,
    )
    .await?;
    ensure_config_mirror(
        &context.client,
        &tenant,
        &namespace,
        &name,
    )
    .await?;
    ensure_k8s_rbac(
        &context.client,
        &cluster,
        &namespace,
        &name,
        &secret_name,
    )
    .await?;
    update_ready_status(
        &context.client,
        &tenant,
        &namespace,
        &name,
        true,
        "Provisioned",
        "",
    )
    .await?;

    Ok(Action::requeue(Duration::from_secs(5 * 60)))
}

fn error_policy(_tenant: Arc<EtcdTenant>, error: &TenantError, _context: Arc<TenantContext>) -> Action {
    warn!(error = %error, "applying EtcdTenant error policy");
    Action::requeue(Duration::from_secs(60))
}

async fn reconcile_delete(
    tenant: Arc<EtcdTenant>,
    context: Arc<TenantContext>,
    namespace: &str,
    name: &str,
    prefix: &str,
) -> Result<Action, TenantError> {
    if !has_finalizer(&tenant) {
        return Ok(Action::await_change());
    }

    let cluster_namespace = tenant.spec.cluster_ref.namespace.clone().unwrap_or_else(|| namespace.to_owned());
    let cluster_name = tenant.spec.cluster_ref.name.clone();

    if let Some(mut etcd_client) = get_cluster_client(&context.clients, &cluster_namespace, &cluster_name).await {
        info!(namespace, name, prefix, "cleaning up tenant data and RBAC");

        etcd_client
            .delete(prefix, Some(etcd_client::DeleteOptions::new().with_prefix()))
            .await?;

        let mut auth = etcd_client.auth_client();
        ignore_not_found(auth.user_revoke_role(name, name).await)?;
        ignore_not_found(auth.user_delete(name).await)?;
        ignore_not_found(auth.role_delete(name).await)?;
    } else {
        let clusters = Api::<EtcdCluster>::namespaced(context.client.clone(), &cluster_namespace);
        match clusters.get(&cluster_name).await {
            Ok(_) => {
                warn!(
                    namespace,
                    name,
                    cluster_namespace,
                    cluster_name,
                    "cluster client unavailable during cleanup; keeping finalizer and requeueing"
                );
                return Ok(Action::requeue(Duration::from_secs(30)));
            }
            Err(kube::Error::Api(ae)) if ae.code == 404 => {
                warn!(
                    namespace,
                    name,
                    cluster_namespace,
                    cluster_name,
                    "referenced EtcdCluster is deleted; skipping etcd cleanup and removing finalizer"
                );
            }
            Err(err) => return Err(err.into()),
        }
    }

    cleanup_k8s_rbac(&context.client, namespace, name).await?;
    remove_finalizer(&context.client, &tenant, namespace).await?;
    Ok(Action::await_change())
}

async fn get_cluster_client(
    clients: &ClusterClients,
    cluster_namespace: &str,
    cluster_name: &str,
) -> Option<etcd_client::Client> {
    let key = (cluster_namespace.to_owned(), cluster_name.to_owned());
    let clients = clients.read().await;
    clients.get(&key).cloned()
}

async fn ensure_tenant_rbac(
    client: &mut etcd_client::Client,
    name: &str,
    prefix: &str,
    password: &str,
) -> Result<(), TenantError> {
    let mut auth = client.auth_client();

    if let Err(err) = auth.user_get(name).await {
        if is_not_found_error(&err) {
            info!(name, "creating etcd tenant user");
            auth.user_add(name, password, None).await?;
        } else {
            return Err(TenantError::Etcd(err));
        }
    }

    // TODO: this is best-effort — for mTLS clusters the password is irrelevant and this
    // call may fail harmlessly. For non-mTLS (basic auth) clusters a failure here means
    // the output Secret password diverges from etcd. We need proper handling once we
    // solidify non-mTLS support (detect auth mode, propagate error for basic-auth clusters).
    if let Err(err) = auth.user_change_password(name, password).await {
        warn!(name, error = %err, "failed to sync etcd user password");
    }

    if let Err(err) = auth.role_get(name).await {
        if is_not_found_error(&err) {
            info!(name, "creating etcd tenant role");
            auth.role_add(name).await?;
        } else {
            return Err(TenantError::Etcd(err));
        }
    }

    let desired_permission = etcd_client::Permission::read_write(prefix).with_prefix();
    let role = auth.role_get(name).await?;
    for permission in role.permissions() {
        if permission != desired_permission {
            info!(name, prefix, "revoking stale prefix permission from tenant role");
            let options = if permission.is_prefix() {
                Some(etcd_client::RoleRevokePermissionOptions::new().with_prefix())
            } else if permission.is_from_key() {
                Some(etcd_client::RoleRevokePermissionOptions::new().with_from_key())
            } else if permission.range_end().is_empty() {
                None
            } else {
                Some(
                    etcd_client::RoleRevokePermissionOptions::new()
                        .with_range_end(permission.range_end().to_vec()),
                )
            };

            auth.role_revoke_permission(name, permission.key().to_vec(), options)
                .await?;
        }
    }

    let role = auth.role_get(name).await?;
    if !role.permissions().iter().any(|perm| perm == &desired_permission) {
        info!(name, prefix, "granting prefix permission to tenant role");
        auth.role_grant_permission(name, desired_permission).await?;
    }

    let user = auth.user_get(name).await?;
    if !user.roles().iter().any(|role| role == name) {
        info!(name, "granting role to tenant user");
        auth.user_grant_role(name, name).await?;
    }

    Ok(())
}

async fn ensure_output_secret(
    client: &Client,
    tenant: &EtcdTenant,
    tenant_namespace: &str,
    secret_name: &str,
    tenant_name: &str,
    password: &str,
) -> Result<(), TenantError> {
    let owner_reference = tenant.controller_owner_ref(&()).ok_or_else(|| {
        TenantError::Invalid(format!(
            "{} cannot produce controller owner reference",
            tenant.name_any()
        ))
    })?;

    let mut data = BTreeMap::new();
    data.insert(
        "username".to_string(),
        ByteString(tenant_name.as_bytes().to_vec()),
    );
    data.insert(
        "password".to_string(),
        ByteString(password.as_bytes().to_vec()),
    );

    let secret = Secret {
        metadata: ObjectMeta {
            name: Some(secret_name.to_string()),
            namespace: Some(tenant_namespace.to_string()),
            owner_references: Some(vec![owner_reference]),
            ..ObjectMeta::default()
        },
        data: Some(data),
        type_: Some("Opaque".to_string()),
        ..Secret::default()
    };

    let secrets = Api::<Secret>::namespaced(client.clone(), tenant_namespace);
    secrets
        .patch(
            secret_name,
            &PatchParams::apply("etcdetcetc").force(),
            &Patch::Apply(&secret),
        )
        .await?;

    Ok(())
}

async fn ensure_config_mirror(
    client: &Client,
    tenant: &EtcdTenant,
    tenant_namespace: &str,
    tenant_name: &str,
) -> Result<(), TenantError> {
    let cluster_namespace = tenant.spec.cluster_ref.namespace.as_deref()
        .unwrap_or(tenant_namespace);
    let cluster_name = tenant.spec.cluster_ref.name.as_str();
    let source_name = format!("{cluster_name}-etcd");

    let source_configmaps = Api::<ConfigMap>::namespaced(client.clone(), cluster_namespace);
    let source = match source_configmaps.get(&source_name).await {
        Ok(source) => source,
        Err(kube::Error::Api(ae)) if ae.code == 404 => {
            warn!(
                tenant_namespace,
                tenant_name,
                cluster_namespace,
                cluster_name,
                source_configmap = source_name,
                "source EtcdCluster ConfigMap not found yet, will retry on next reconcile"
            );
            return Ok(());
        }
        Err(err) => return Err(err.into()),
    };

    let owner_reference = tenant.controller_owner_ref(&()).ok_or_else(|| {
        TenantError::Invalid(format!(
            "{} cannot produce controller owner reference",
            tenant.name_any()
        ))
    })?;
    let target_name = format!("{tenant_name}-etcd");
    let target = ConfigMap {
        metadata: ObjectMeta {
            name: Some(target_name.clone()),
            namespace: Some(tenant_namespace.to_string()),
            owner_references: Some(vec![owner_reference]),
            ..ObjectMeta::default()
        },
        data: source.data,
        ..ConfigMap::default()
    };

    let target_configmaps = Api::<ConfigMap>::namespaced(client.clone(), tenant_namespace);
    target_configmaps
        .patch(
            &target_name,
            &PatchParams::apply("etcdetcetc").force(),
            &Patch::Apply(&target),
        )
        .await?;

    Ok(())
}

async fn ensure_k8s_rbac(
    client: &Client,
    cluster: &EtcdCluster,
    tenant_namespace: &str,
    tenant_name: &str,
    secret_name: &str,
) -> Result<(), TenantError> {
    if cluster.spec.allowed_namespaces.is_empty() {
        ensure_clusterwide_k8s_rbac(client, tenant_namespace, tenant_name, secret_name).await?;
        cleanup_namespaced_k8s_rbac(client, tenant_namespace, tenant_name, &BTreeSet::new()).await?;
    } else {
        let desired_namespaces: BTreeSet<String> = cluster
            .spec
            .allowed_namespaces
            .iter()
            .filter(|namespace| !namespace.is_empty())
            .cloned()
            .collect();
        ensure_namespaced_k8s_rbac(
            client,
            tenant_namespace,
            tenant_name,
            secret_name,
            &desired_namespaces,
        )
        .await?;
        cleanup_clusterwide_k8s_rbac(client, tenant_namespace, tenant_name).await?;
        cleanup_namespaced_k8s_rbac(client, tenant_namespace, tenant_name, &desired_namespaces).await?;
    }

    Ok(())
}

async fn ensure_clusterwide_k8s_rbac(
    client: &Client,
    tenant_namespace: &str,
    tenant_name: &str,
    secret_name: &str,
) -> Result<(), TenantError> {
    let name = format!("etcdetcetc:{tenant_namespace}:{tenant_name}");
    let labels = tenant_rbac_labels(tenant_namespace, tenant_name);

    let cluster_role = ClusterRole {
        metadata: ObjectMeta {
            name: Some(name.clone()),
            labels: Some(labels.clone()),
            ..ObjectMeta::default()
        },
        rules: Some(vec![PolicyRule {
            api_groups: Some(vec!["".to_string()]),
            resources: Some(vec!["secrets".to_string()]),
            resource_names: Some(vec![secret_name.to_string()]),
            verbs: vec!["get".to_string()],
            ..PolicyRule::default()
        }]),
        ..ClusterRole::default()
    };

    let cluster_roles = Api::<ClusterRole>::all(client.clone());
    cluster_roles
        .patch(
            &name,
            &PatchParams::apply("etcdetcetc").force(),
            &Patch::Apply(&cluster_role),
        )
        .await?;

    let cluster_role_binding = ClusterRoleBinding {
        metadata: ObjectMeta {
            name: Some(name.clone()),
            labels: Some(labels),
            ..ObjectMeta::default()
        },
        role_ref: RoleRef {
            api_group: "rbac.authorization.k8s.io".to_string(),
            kind: "ClusterRole".to_string(),
            name: name.clone(),
        },
        subjects: Some(vec![Subject {
            api_group: Some("rbac.authorization.k8s.io".to_string()),
            kind: "Group".to_string(),
            name: "system:serviceaccounts".to_string(),
            namespace: None,
        }]),
        ..ClusterRoleBinding::default()
    };

    let cluster_role_bindings = Api::<ClusterRoleBinding>::all(client.clone());
    cluster_role_bindings
        .patch(
            &name,
            &PatchParams::apply("etcdetcetc").force(),
            &Patch::Apply(&cluster_role_binding),
        )
        .await?;

    Ok(())
}

async fn ensure_namespaced_k8s_rbac(
    client: &Client,
    tenant_namespace: &str,
    tenant_name: &str,
    secret_name: &str,
    desired_namespaces: &BTreeSet<String>,
) -> Result<(), TenantError> {
    let name = format!("etcdetcetc:{tenant_name}");

    for namespace in desired_namespaces {
        let mut labels = tenant_rbac_labels(tenant_namespace, tenant_name);
        labels.insert("etcdetcetc.samcday.com/consumer-namespace".to_string(), namespace.clone());

        let role = Role {
            metadata: ObjectMeta {
                name: Some(name.clone()),
                namespace: Some(namespace.clone()),
                labels: Some(labels.clone()),
                ..ObjectMeta::default()
            },
            rules: Some(vec![PolicyRule {
                api_groups: Some(vec!["".to_string()]),
                resources: Some(vec!["secrets".to_string()]),
                resource_names: Some(vec![secret_name.to_string()]),
                verbs: vec!["get".to_string()],
                ..PolicyRule::default()
            }]),
            ..Role::default()
        };

        let roles = Api::<Role>::namespaced(client.clone(), namespace);
        roles
            .patch(
                &name,
                &PatchParams::apply("etcdetcetc").force(),
                &Patch::Apply(&role),
            )
            .await?;

        let role_binding = RoleBinding {
            metadata: ObjectMeta {
                name: Some(name.clone()),
                namespace: Some(namespace.clone()),
                labels: Some(labels),
                ..ObjectMeta::default()
            },
            role_ref: RoleRef {
                api_group: "rbac.authorization.k8s.io".to_string(),
                kind: "Role".to_string(),
                name: name.clone(),
            },
            subjects: Some(vec![Subject {
                api_group: Some("rbac.authorization.k8s.io".to_string()),
                kind: "Group".to_string(),
                name: format!("system:serviceaccounts:{namespace}"),
                namespace: None,
            }]),
            ..RoleBinding::default()
        };

        let role_bindings = Api::<RoleBinding>::namespaced(client.clone(), namespace);
        role_bindings
            .patch(
                &name,
                &PatchParams::apply("etcdetcetc").force(),
                &Patch::Apply(&role_binding),
            )
            .await?;
    }

    Ok(())
}

async fn cleanup_clusterwide_k8s_rbac(
    client: &Client,
    tenant_namespace: &str,
    tenant_name: &str,
) -> Result<(), TenantError> {
    let name = format!("etcdetcetc:{tenant_namespace}:{tenant_name}");

    let cluster_roles = Api::<ClusterRole>::all(client.clone());
    match cluster_roles.delete(&name, &DeleteParams::default()).await {
        Ok(_) => {}
        Err(kube::Error::Api(ae)) if ae.code == 404 => {}
        Err(err) => return Err(err.into()),
    }

    let cluster_role_bindings = Api::<ClusterRoleBinding>::all(client.clone());
    match cluster_role_bindings
        .delete(&name, &DeleteParams::default())
        .await
    {
        Ok(_) => {}
        Err(kube::Error::Api(ae)) if ae.code == 404 => {}
        Err(err) => return Err(err.into()),
    }

    Ok(())
}

async fn cleanup_namespaced_k8s_rbac(
    client: &Client,
    tenant_namespace: &str,
    tenant_name: &str,
    desired_namespaces: &BTreeSet<String>,
) -> Result<(), TenantError> {
    let name = format!("etcdetcetc:{tenant_name}");

    let roles = Api::<Role>::all(client.clone());
    let existing_roles = roles.list(&ListParams::default()).await?;
    for role in existing_roles.items {
        if role.name_any() != name {
            continue;
        }
        if !is_tenant_rbac_object(&role.metadata.labels, tenant_namespace, tenant_name) {
            continue;
        }
        let Some(namespace) = role.namespace() else {
            continue;
        };
        if desired_namespaces.contains(&namespace) {
            continue;
        }

        let namespaced_roles = Api::<Role>::namespaced(client.clone(), &namespace);
        match namespaced_roles.delete(&name, &DeleteParams::default()).await {
            Ok(_) => {}
            Err(kube::Error::Api(ae)) if ae.code == 404 => {}
            Err(err) => return Err(err.into()),
        }
    }

    let role_bindings = Api::<RoleBinding>::all(client.clone());
    let existing_role_bindings = role_bindings.list(&ListParams::default()).await?;
    for role_binding in existing_role_bindings.items {
        if role_binding.name_any() != name {
            continue;
        }
        if !is_tenant_rbac_object(&role_binding.metadata.labels, tenant_namespace, tenant_name) {
            continue;
        }
        let Some(namespace) = role_binding.namespace() else {
            continue;
        };
        if desired_namespaces.contains(&namespace) {
            continue;
        }

        let namespaced_role_bindings = Api::<RoleBinding>::namespaced(client.clone(), &namespace);
        match namespaced_role_bindings
            .delete(&name, &DeleteParams::default())
            .await
        {
            Ok(_) => {}
            Err(kube::Error::Api(ae)) if ae.code == 404 => {}
            Err(err) => return Err(err.into()),
        }
    }

    Ok(())
}

async fn cleanup_k8s_rbac(client: &Client, tenant_namespace: &str, tenant_name: &str) -> Result<(), TenantError> {
    cleanup_clusterwide_k8s_rbac(client, tenant_namespace, tenant_name).await?;
    cleanup_namespaced_k8s_rbac(client, tenant_namespace, tenant_name, &BTreeSet::new()).await?;
    Ok(())
}

fn tenant_rbac_labels(tenant_namespace: &str, tenant_name: &str) -> BTreeMap<String, String> {
    BTreeMap::from([
        ("app.kubernetes.io/managed-by".to_string(), "etcdetcetc".to_string()),
        (
            "etcdetcetc.samcday.com/tenant-namespace".to_string(),
            tenant_namespace.to_string(),
        ),
        (
            "etcdetcetc.samcday.com/tenant-name".to_string(),
            tenant_name.to_string(),
        ),
    ])
}

fn is_tenant_rbac_object(
    labels: &Option<BTreeMap<String, String>>,
    tenant_namespace: &str,
    tenant_name: &str,
) -> bool {
    let Some(labels) = labels else {
        return false;
    };

    labels
        .get("app.kubernetes.io/managed-by")
        .is_some_and(|value| value == "etcdetcetc")
        && labels
            .get("etcdetcetc.samcday.com/tenant-namespace")
            .is_some_and(|value| value == tenant_namespace)
        && labels
            .get("etcdetcetc.samcday.com/tenant-name")
            .is_some_and(|value| value == tenant_name)
}

async fn update_ready_status(
    client: &Client,
    tenant: &EtcdTenant,
    namespace: &str,
    name: &str,
    ready: bool,
    reason: &str,
    message: &str,
) -> Result<(), TenantError> {
    let api = Api::<EtcdTenant>::namespaced(client.clone(), namespace);
    let existing_conditions: &[crate::crd::Condition] = tenant
        .status
        .as_ref()
        .map(|status| status.conditions.as_slice())
        .unwrap_or(&[]);

    let condition = crate::crd::ready_condition_with_existing(
        ready,
        reason,
        message,
        existing_conditions,
    );
    let patch = serde_json::json!({
        "status": {
            "conditions": [condition],
        }
    });

    api.patch_status(name, &PatchParams::default(), &Patch::Merge(&patch))
        .await?;

    Ok(())
}

async fn ensure_finalizer(client: &Client, tenant: &EtcdTenant, namespace: &str) -> Result<(), TenantError> {
    if has_finalizer(tenant) {
        return Ok(());
    }

    let mut finalizers = tenant.meta().finalizers.clone().unwrap_or_default();
    finalizers.push(TENANT_FINALIZER.to_string());

    let api = Api::<EtcdTenant>::namespaced(client.clone(), namespace);
    let patch = serde_json::json!({
        "metadata": {
            "finalizers": finalizers,
        }
    });

    api.patch(
        &tenant.name_any(),
        &PatchParams::default(),
        &Patch::Merge(&patch),
    )
    .await?;

    Ok(())
}

async fn remove_finalizer(client: &Client, tenant: &EtcdTenant, namespace: &str) -> Result<(), TenantError> {
    let mut finalizers = tenant.meta().finalizers.clone().unwrap_or_default();
    finalizers.retain(|f| f != TENANT_FINALIZER);

    let api = Api::<EtcdTenant>::namespaced(client.clone(), namespace);
    let patch = serde_json::json!({
        "metadata": {
            "finalizers": finalizers,
        }
    });

    api.patch(
        &tenant.name_any(),
        &PatchParams::default(),
        &Patch::Merge(&patch),
    )
    .await?;

    Ok(())
}

fn has_finalizer(tenant: &EtcdTenant) -> bool {
    tenant
        .meta()
        .finalizers
        .as_ref()
        .is_some_and(|finalizers| finalizers.iter().any(|f| f == TENANT_FINALIZER))
}

fn generate_password() -> String {
    use rand::Rng;

    const CHARSET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
    let mut rng = rand::thread_rng();
    (0..32)
        .map(|_| {
            let idx = rng.gen_range(0..CHARSET.len());
            CHARSET[idx] as char
        })
        .collect()
}

async fn ensure_password_secret(
    client: &Client,
    tenant: &EtcdTenant,
    namespace: &str,
    name: &str,
) -> Result<String, TenantError> {
    let password_secret_name = format!("{name}-etcd-password");
    let secrets = Api::<Secret>::namespaced(client.clone(), namespace);

    match secrets.get(&password_secret_name).await {
        Ok(secret) => {
            let password = secret
                .data
                .as_ref()
                .and_then(|data| data.get("password"))
                .ok_or_else(|| {
                    TenantError::Invalid(format!(
                        "password secret {namespace}/{password_secret_name} missing required key password"
                    ))
                })?;

            String::from_utf8(password.0.clone()).map_err(|_| {
                TenantError::Invalid(format!(
                    "password secret {namespace}/{password_secret_name} contains non-UTF-8 password"
                ))
            })
        }
        Err(kube::Error::Api(ae)) if ae.code == 404 => {
            let owner_reference = tenant.controller_owner_ref(&()).ok_or_else(|| {
                TenantError::Invalid(format!(
                    "{} cannot produce controller owner reference",
                    tenant.name_any()
                ))
            })?;

            let password = generate_password();
            let mut data = BTreeMap::new();
            data.insert(
                "password".to_string(),
                ByteString(password.as_bytes().to_vec()),
            );

            let secret = Secret {
                metadata: ObjectMeta {
                    name: Some(password_secret_name.clone()),
                    namespace: Some(namespace.to_string()),
                    owner_references: Some(vec![owner_reference]),
                    ..ObjectMeta::default()
                },
                data: Some(data),
                type_: Some("Opaque".to_string()),
                ..Secret::default()
            };

            secrets.create(&PostParams::default(), &secret).await?;
            Ok(password)
        }
        Err(err) => Err(err.into()),
    }
}

fn ignore_not_found<T>(result: Result<T, etcd_client::Error>) -> Result<(), TenantError> {
    match result {
        Ok(_) => Ok(()),
        Err(err) if is_not_found_error(&err) => Ok(()),
        Err(err) => Err(TenantError::Etcd(err)),
    }
}

fn is_not_found_error(error: &etcd_client::Error) -> bool {
    match error {
        etcd_client::Error::GRpcStatus(status) => {
            // NOT_FOUND (5) is the standard code for missing keys.
            // FAILED_PRECONDITION (9) is what etcd returns for missing auth entities
            // (users, roles) — or when auth is not enabled.
            let code = status.code() as i32;
            code == 5 || (code == 9 && status.message().to_ascii_lowercase().contains("not found"))
        }
        _ => false,
    }
}
