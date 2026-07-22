//! EtcdTenant controller.

use std::{collections::BTreeMap, future::Future, sync::Arc, time::Duration};

use futures::StreamExt;
use k8s_openapi::{
    ByteString,
    api::core::v1::{ConfigMap, Namespace, Secret},
    apimachinery::pkg::apis::meta::v1::OwnerReference,
};
use kube::{
    Api, Client, Resource, ResourceExt,
    api::{
        ApiResource, DeleteParams, DynamicObject, GroupVersionKind, ObjectMeta, Patch, PatchParams,
        PostParams, Preconditions,
    },
    runtime::{Controller, controller::Action, reflector::ObjectRef, watcher},
};
use sha2::{Digest, Sha256};
use tracing::{error, info, warn};
use x509_parser::{parse_x509_certificate, pem::parse_x509_pem};

use crate::crd::{
    ConditionStatus, EtcdCluster, EtcdTenant, EtcdTenantStatus, PinnedClusterReference,
    TenantCredentialMode, TenantExternalAccessState, TenantExternalIdentity,
};

pub(crate) const TENANT_FINALIZER: &str = "etcdetcetc.samcday.com/tenant";
const TENANT_UID_LABEL: &str = "etcdetcetc.samcday.com/tenant-uid";
const TENANT_NAMESPACE_LABEL: &str = "etcdetcetc.samcday.com/tenant-namespace";
const TENANT_NAME_LABEL: &str = "etcdetcetc.samcday.com/tenant-name";
const SHARED_AVAILABILITY_RISK_ANNOTATION: &str = "etcdetcetc.samcday.com/shared-availability-risk";
const SHARED_AVAILABILITY_RISK_ACCEPTANCE: &str = "accepted-v1";
const FIELD_MANAGER: &str = "etcdetcetc";
const FLUX_WATCH_LABEL: &str = "reconcile.fluxcd.io/watch";
const TLS_CERTIFICATE_READY_MIN_VALIDITY: Duration = Duration::from_secs(60 * 60);

/// Shared context for the EtcdTenant controller.
#[derive(Clone)]
pub struct TenantContext {
    /// Kubernetes API client.
    pub client: Client,
    /// Restricts reconciliation to these namespaces when non-empty.
    pub allowed_namespaces: Vec<String>,
    /// Cancels every in-flight reconcile at the hard leader-renew deadline.
    pub leadership: crate::leadership::LeadershipGuard,
}

/// Errors produced by EtcdTenant reconciliation.
#[derive(Debug, thiserror::Error)]
pub enum TenantError {
    /// Kubernetes API error.
    #[error("kubernetes API error: {0}")]
    Kube(#[from] kube::Error),

    /// etcd API error.
    #[error("etcd API error: {0}")]
    Etcd(#[source] Box<etcd_client::Error>),

    /// Serialization error.
    #[error("serialization error: {0}")]
    Serde(#[from] serde_json::Error),

    /// Invalid or incomplete EtcdTenant/EtcdCluster object.
    #[error("invalid object: {0}")]
    Invalid(String),

    /// The process no longer holds a valid leader-election permit.
    #[error("leadership permit expired or was revoked")]
    LostLeadership,
}

impl From<etcd_client::Error> for TenantError {
    fn from(error: etcd_client::Error) -> Self {
        Self::Etcd(Box::new(error))
    }
}

/// Runs the EtcdTenant controller until the stream ends.
pub async fn run(context: TenantContext) {
    let api = Api::<EtcdTenant>::all(context.client.clone());
    let clusters = Api::<EtcdCluster>::all(context.client.clone());
    let context = Arc::new(context);

    info!("starting EtcdTenant controller with exact EtcdCluster reference watches");

    let controller = Controller::new(api, watcher::Config::default());
    let tenant_store = controller.store();
    controller
        .watches(clusters, watcher::Config::default(), move |cluster| {
            tenants_referencing_cluster(tenant_store.state(), &cluster)
        })
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
                    error!(
                        error = %err,
                        error_debug = ?err,
                        error_chain = %crate::format_error_chain(&err),
                        "EtcdTenant reconciliation failed"
                    );
                }
            }
        })
        .await;
}

fn tenants_referencing_cluster(
    tenants: Vec<Arc<EtcdTenant>>,
    cluster: &EtcdCluster,
) -> Vec<ObjectRef<EtcdTenant>> {
    let Some(cluster_namespace) = cluster.namespace() else {
        return Vec::new();
    };
    let cluster_name = cluster.name_any();
    let cluster_uid = cluster.uid();

    tenants
        .into_iter()
        .filter(|tenant| {
            let tenant_namespace = tenant.namespace().unwrap_or_default();
            let spec_namespace = tenant
                .spec
                .cluster_ref
                .namespace
                .as_deref()
                .unwrap_or(&tenant_namespace);
            let spec_matches =
                tenant.spec.cluster_ref.name == cluster_name && spec_namespace == cluster_namespace;
            let pinned_matches = tenant
                .status
                .as_ref()
                .and_then(|status| status.external_identity.as_ref())
                .is_some_and(|identity| {
                    identity.cluster_ref.name == cluster_name
                        && identity.cluster_ref.namespace == cluster_namespace
                        && cluster_uid
                            .as_deref()
                            .is_some_and(|uid| identity.cluster_ref.uid == uid)
                });
            spec_matches || pinned_matches
        })
        .map(|tenant| ObjectRef::from_obj(tenant.as_ref()))
        .collect()
}

async fn reconcile(
    tenant: Arc<EtcdTenant>,
    context: Arc<TenantContext>,
) -> Result<Action, TenantError> {
    let mut leadership = context.leadership.clone();
    tokio::select! {
        biased;
        _ = leadership.wait_until_inactive_or_expired() => Err(TenantError::LostLeadership),
        result = reconcile_while_leader(tenant, context) => result,
    }
}

async fn reconcile_while_leader(
    tenant: Arc<EtcdTenant>,
    context: Arc<TenantContext>,
) -> Result<Action, TenantError> {
    let result = reconcile_inner(tenant.clone(), context.clone()).await;

    if let Err(error) = &result {
        // Deletion has no useful Ready state, and a status failure must never
        // mask the original reconciliation error returned to the runtime.
        if tenant.meta().deletion_timestamp.is_none()
            && let Some(namespace) = tenant.namespace()
        {
            let name = tenant.name_any();
            if let Err(status_error) = update_ready_status(
                &context.client,
                &tenant,
                &namespace,
                &name,
                false,
                "ReconciliationFailed",
                &error.to_string(),
            )
            .await
            {
                warn!(
                    namespace,
                    name,
                    error = %status_error,
                    original_error = %error,
                    "failed to record EtcdTenant reconciliation failure"
                );
            }
        }
    }

    result
}

async fn reconcile_inner(
    tenant: Arc<EtcdTenant>,
    context: Arc<TenantContext>,
) -> Result<Action, TenantError> {
    let namespace = tenant
        .namespace()
        .ok_or_else(|| TenantError::Invalid(format!("{} has no namespace", tenant.name_any())))?;
    let name = tenant.name_any();

    info!(namespace, name, "reconciling EtcdTenant");

    // Runtime scoping controls normal reconciliation only. An object that
    // already carries our finalizer must remain deletable after the scope is
    // narrowed.
    if tenant.meta().deletion_timestamp.is_some() {
        return reconcile_delete(tenant, context, &namespace, &name).await;
    }

    if !context.allowed_namespaces.is_empty()
        && !context.allowed_namespaces.iter().any(|ns| ns == &namespace)
    {
        warn!(
            namespace,
            name, "tenant namespace not in allowedNamespaces, skipping"
        );
        return Ok(Action::await_change());
    }

    let cluster_namespace = tenant
        .spec
        .cluster_ref
        .namespace
        .clone()
        .unwrap_or_else(|| namespace.clone());
    if !cluster_reference_is_cross_namespace(&tenant, &namespace) && !has_finalizer(&tenant) {
        update_ready_status(
            &context.client,
            &tenant,
            &namespace,
            &name,
            false,
            "CrossNamespaceClusterRefRequired",
            "spec.clusterRef.namespace must be explicit and differ from the EtcdTenant namespace; same-namespace tenancy cannot preserve its verified cleanup path during namespace deletion",
        )
        .await?;
        return Ok(Action::await_change());
    }
    let cluster_name = tenant.spec.cluster_ref.name.clone();
    let clusters = Api::<EtcdCluster>::namespaced(context.client.clone(), &cluster_namespace);
    let cluster = match clusters.get(&cluster_name).await {
        Ok(cluster) => cluster,
        Err(kube::Error::Api(ae)) if ae.code == 404 => {
            update_ready_status(
                &context.client,
                &tenant,
                &namespace,
                &name,
                false,
                "ClusterNotFound",
                "referenced EtcdCluster does not exist",
            )
            .await?;
            return Ok(Action::requeue(Duration::from_secs(30)));
        }
        Err(err) => return Err(err.into()),
    };

    let external_access_state = tenant
        .status
        .as_ref()
        .and_then(|status| status.external_access_state);
    let namespace_allowed = cluster_allows_tenant_namespace(
        &cluster_namespace,
        &namespace,
        &cluster.spec.allowed_namespaces,
    );
    // Once deprovisioning has been durably claimed, it must finish before a
    // restored authorization can start a new provisioning cycle. Otherwise a
    // same-UID TLS leaf retained by a failed strict artifact cleanup could
    // become live again as soon as RBAC is recreated.
    if !namespace_allowed || deprovisioning_must_complete(external_access_state) {
        return reconcile_namespace_not_allowed(&tenant, &context, &cluster, &namespace, &name)
            .await;
    }

    // Prefix RBAC protects key confidentiality and integrity, but every
    // tenant still shares etcd leases, capacity, request processing, alarms,
    // and availability. Require an explicit, versioned acknowledgement before
    // normal identity planning or etcd provisioning. Deletion is handled at
    // the top of this function, and namespace deauthorization above must take
    // precedence so removing the annotation cannot strand required revocation.
    if !accepts_shared_availability_risk(&tenant) {
        update_ready_status(
            &context.client,
            &tenant,
            &namespace,
            &name,
            false,
            "SharedAvailabilityRiskNotAccepted",
            &format!(
                "metadata.annotations[{SHARED_AVAILABILITY_RISK_ANNOTATION:?}] must equal {SHARED_AVAILABILITY_RISK_ACCEPTANCE:?}"
            ),
        )
        .await?;
        // Exact EtcdCluster reference events normally enqueue this Tenant.
        // Keep a bounded retry as a recovery path while acknowledgement is
        // absent so missed/restarted watches cannot strand live access.
        return Ok(Action::requeue(Duration::from_secs(30)));
    }

    if cluster.meta().deletion_timestamp.is_some() {
        update_ready_status(
            &context.client,
            &tenant,
            &namespace,
            &name,
            false,
            "ClusterDeleting",
            "referenced EtcdCluster is being deleted",
        )
        .await?;
        return Ok(Action::requeue(Duration::from_secs(30)));
    }

    if !cluster_is_ready_for_tenant_provisioning(&cluster) {
        update_ready_status(
            &context.client,
            &tenant,
            &namespace,
            &name,
            false,
            "ClusterNotReady",
            "referenced EtcdCluster is disconnected, has RBAC auth disabled, is unsupported, is alarmed, lacks a durable physical cluster ID, or has not reported current-generation Ready",
        )
        .await?;
        return Ok(Action::requeue(Duration::from_secs(30)));
    }

    let desired_identity = build_external_identity(&tenant, &cluster)?;
    match tenant
        .status
        .as_ref()
        .and_then(|status| status.external_identity.as_ref())
    {
        None if external_access_state.is_some() => {
            update_ready_status(
                &context.client,
                &tenant,
                &namespace,
                &name,
                false,
                "ExternalIdentityMissing",
                "status.externalAccessState exists without status.externalIdentity; refusing to infer ownership",
            )
            .await?;
            return Ok(Action::await_change());
        }
        None => {
            pin_external_identity(
                &context.client,
                &tenant,
                &namespace,
                &name,
                &desired_identity,
            )
            .await?;
            return Ok(Action::await_change());
        }
        Some(pinned) if pinned != &desired_identity => {
            update_ready_status(
                &context.client,
                &tenant,
                &namespace,
                &name,
                false,
                "ExternalIdentityMismatch",
                "resolved identity differs from status.externalIdentity; refusing to move external resources",
            )
            .await?;
            return Ok(Action::await_change());
        }
        Some(_) => {}
    }

    let identity = desired_identity;
    if let Err(err) = validate_external_identity(&tenant, &identity) {
        update_ready_status(
            &context.client,
            &tenant,
            &namespace,
            &name,
            false,
            "InvalidExternalIdentity",
            &err.to_string(),
        )
        .await?;
        return Ok(Action::await_change());
    }
    if identity.credential_mode == TenantCredentialMode::Tls {
        let Some(tls) = cluster.spec.tenant_tls.as_ref() else {
            update_ready_status(
                &context.client,
                &tenant,
                &namespace,
                &name,
                false,
                "TLSNotConfigured",
                "referenced EtcdCluster has no spec.tenantTls issuer",
            )
            .await?;
            return Ok(Action::requeue(Duration::from_secs(30)));
        };
        if tls.issuer_ref.name.is_empty() {
            update_ready_status(
                &context.client,
                &tenant,
                &namespace,
                &name,
                false,
                "TLSNotConfigured",
                "referenced EtcdCluster spec.tenantTls.issuerRef.name is empty",
            )
            .await?;
            return Ok(Action::requeue(Duration::from_secs(30)));
        }
    }

    if ensure_finalizer(&context.client, &tenant, &namespace).await? {
        // Identity planning is Kubernetes-only. Observe the cleanup finalizer
        // before recording that etcd provisioning may begin.
        return Ok(Action::await_change());
    }

    if external_access_state.is_none() {
        mark_external_access_state(
            &context.client,
            &tenant,
            &namespace,
            &name,
            TenantExternalAccessState::LegacyUnverified,
        )
        .await?;
        return Ok(Action::await_change());
    }

    if external_access_state == Some(TenantExternalAccessState::Deprovisioned) {
        // Reauthorization starts from the same no-mutation provenance as a
        // new identity. The existing Planned preflights below must rediscover
        // any exact-name Kubernetes artifact or external etcd collision before
        // the controller can claim and mutate the identity again.
        mark_external_access_state(
            &context.client,
            &tenant,
            &namespace,
            &name,
            TenantExternalAccessState::Planned,
        )
        .await?;
        return Ok(Action::await_change());
    }

    let cluster = match refetch_provisioning_preconditions(&context.client, &tenant, &cluster).await
    {
        Ok(cluster) => cluster,
        Err(error) => {
            update_ready_status(
                &context.client,
                &tenant,
                &namespace,
                &name,
                false,
                "ClusterMutationPreconditionFailed",
                &format!(
                    "fresh Kubernetes authorization/lifecycle proof failed before external access mutation: {error}"
                ),
            )
            .await?;
            return Ok(Action::requeue(Duration::from_secs(30)));
        }
    };

    if external_access_state == Some(TenantExternalAccessState::Planned)
        && let Some(collision) =
            preflight_kubernetes_artifacts(&context.client, &tenant, &identity).await?
    {
        update_ready_status(
            &context.client,
            &tenant,
            &namespace,
            &name,
            false,
            "ArtifactCollision",
            &format!(
                "planned Kubernetes credential/configuration names must all be absent before provisioning: {collision}"
            ),
        )
        .await?;
        return Ok(Action::requeue(Duration::from_secs(30)));
    }

    let (mut etcd_client, physical_server_ca) =
        match build_verified_admin_client(&context.client, &cluster, true).await {
            Ok(verified) => verified,
            Err(error) => {
                warn!(
                    tenant_namespace = namespace,
                    tenant_name = name,
                    cluster_namespace = identity.cluster_ref.namespace,
                    cluster_name = identity.cluster_ref.name,
                    error = %error,
                    "fresh physical etcd identity/readiness proof failed, requeueing"
                );
                update_ready_status(
                    &context.client,
                    &tenant,
                    &namespace,
                    &name,
                    false,
                    "ClusterMutationIdentityUnverified",
                    &format!("fresh physical etcd identity/readiness proof failed: {error}"),
                )
                .await?;
                return Ok(Action::requeue(Duration::from_secs(30)));
            }
        };

    if external_access_state == Some(TenantExternalAccessState::Planned) {
        if let Some(collision) = external_identity_collision(
            &mut etcd_client,
            &identity.etcd_user,
            &identity.etcd_role,
            &identity.prefix,
        )
        .await?
        {
            update_ready_status(
                &context.client,
                &tenant,
                &namespace,
                &name,
                false,
                "ExternalIdentityCollision",
                &format!(
                    "refusing to adopt or overlap an external identity without provenance: {collision}"
                ),
            )
            .await?;
            return Ok(Action::requeue(Duration::from_secs(30)));
        }
        mark_external_access_state(
            &context.client,
            &tenant,
            &namespace,
            &name,
            TenantExternalAccessState::Provisioning,
        )
        .await?;
        return Ok(Action::await_change());
    }

    let password = match identity.credential_mode {
        TenantCredentialMode::Password => {
            let password_secret_name =
                identity.password_secret_name.as_deref().ok_or_else(|| {
                    TenantError::Invalid(
                        "password identity has no password Secret name".to_string(),
                    )
                })?;
            Some(
                ensure_password_secret(&context.client, &tenant, &namespace, password_secret_name)
                    .await?,
            )
        }
        TenantCredentialMode::Tls => None,
    };
    let credential = match password.as_deref() {
        Some(password) => TenantCredential::Password(password),
        None => TenantCredential::Tls,
    };
    ensure_tenant_rbac(
        &mut etcd_client,
        &identity.etcd_user,
        &identity.etcd_role,
        &identity.prefix,
        credential,
    )
    .await?;
    if external_access_state != Some(TenantExternalAccessState::Provisioned) {
        mark_external_access_state(
            &context.client,
            &tenant,
            &namespace,
            &name,
            TenantExternalAccessState::Provisioned,
        )
        .await?;
        return Ok(Action::await_change());
    }

    // A claimed identity is sanitized before unrelated authorization drift is
    // reported. In particular, no foreign user may retain the owned role while
    // a separate overlapping role keeps Ready=False.
    if let Some(collision) = external_isolation_collision(
        &mut etcd_client,
        &identity.etcd_user,
        &identity.etcd_role,
        &identity.prefix,
    )
    .await?
    {
        update_ready_status(
            &context.client,
            &tenant,
            &namespace,
            &name,
            false,
            "ExternalIsolationConflict",
            &format!(
                "foreign etcd authorization overlaps this tenant's claimed identity or prefix: {collision}"
            ),
        )
        .await?;
        return Ok(Action::requeue(Duration::from_secs(30)));
    }

    let client_certificate_revision = match identity.credential_mode {
        TenantCredentialMode::Password => {
            let password = password.as_deref().ok_or_else(|| {
                TenantError::Invalid("password credential was not loaded".to_string())
            })?;
            ensure_password_output_secret(
                &context.client,
                &tenant,
                &namespace,
                &identity.output_secret_name,
                &identity.etcd_user,
                password,
            )
            .await?;
            None
        }
        TenantCredentialMode::Tls => {
            let Some(client_certificate_revision) = ensure_tls_credentials(
                &context.client,
                &mut etcd_client,
                &tenant,
                &cluster,
                &identity,
                &namespace,
                &physical_server_ca,
            )
            .await?
            else {
                update_ready_status(
                    &context.client,
                    &tenant,
                    &namespace,
                    &name,
                    false,
                    "CertificateNotReady",
                    "tenant TLS Certificate has not reached Ready for its current generation",
                )
                .await?;
                return Ok(Action::requeue(Duration::from_secs(30)));
            };
            Some(client_certificate_revision)
        }
    };

    ensure_tenant_configmap(
        &context.client,
        &tenant,
        &namespace,
        &cluster,
        &identity,
        &physical_server_ca,
        client_certificate_revision.as_deref(),
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

    // Core resources are deliberately not watched cluster-wide. This bounded
    // poll picks up cert-manager renewals and referenced admin-Secret or
    // physical-server-CA ConfigMap rotation.
    Ok(Action::requeue(Duration::from_secs(5 * 60)))
}

async fn refetch_provisioning_preconditions(
    client: &Client,
    tenant: &EtcdTenant,
    expected_cluster: &EtcdCluster,
) -> Result<EtcdCluster, TenantError> {
    let tenant_namespace = tenant
        .namespace()
        .ok_or_else(|| TenantError::Invalid(format!("{} has no namespace", tenant.name_any())))?;
    let tenant_uid = tenant.uid().ok_or_else(|| {
        TenantError::Invalid(format!(
            "{tenant_namespace}/{} has no metadata.uid",
            tenant.name_any()
        ))
    })?;
    let fresh_tenant = Api::<EtcdTenant>::namespaced(client.clone(), &tenant_namespace)
        .get(&tenant.name_any())
        .await?;
    if fresh_tenant.uid().as_deref() != Some(tenant_uid.as_str())
        || fresh_tenant.meta().deletion_timestamp.is_some()
        || !accepts_shared_availability_risk(&fresh_tenant)
    {
        return Err(TenantError::Invalid(
            "EtcdTenant UID, deletion state, or shared-availability acknowledgement changed"
                .to_string(),
        ));
    }

    let cluster_namespace = expected_cluster.namespace().ok_or_else(|| {
        TenantError::Invalid(format!("{} has no namespace", expected_cluster.name_any()))
    })?;
    let expected_uid = expected_cluster.uid().ok_or_else(|| {
        TenantError::Invalid(format!(
            "{cluster_namespace}/{} has no metadata.uid",
            expected_cluster.name_any()
        ))
    })?;
    let fresh_cluster = Api::<EtcdCluster>::namespaced(client.clone(), &cluster_namespace)
        .get(&expected_cluster.name_any())
        .await?;
    if fresh_cluster.uid().as_deref() != Some(expected_uid.as_str()) {
        return Err(TenantError::Invalid(
            "EtcdCluster UID changed before external mutation".to_string(),
        ));
    }
    if fresh_cluster.meta().deletion_timestamp.is_some() {
        return Err(TenantError::Invalid(
            "EtcdCluster entered deletion before external mutation".to_string(),
        ));
    }
    if !cluster_allows_tenant_namespace(
        &cluster_namespace,
        &tenant_namespace,
        &fresh_cluster.spec.allowed_namespaces,
    ) {
        return Err(TenantError::Invalid(
            "EtcdCluster no longer authorizes the tenant namespace".to_string(),
        ));
    }
    if !cluster_is_ready_for_tenant_provisioning(&fresh_cluster) {
        return Err(TenantError::Invalid(
            "EtcdCluster no longer has current, mutation-qualified Ready state".to_string(),
        ));
    }
    Ok(fresh_cluster)
}

async fn preflight_kubernetes_artifacts(
    client: &Client,
    tenant: &EtcdTenant,
    identity: &TenantExternalIdentity,
) -> Result<Option<String>, TenantError> {
    let tenant_namespace = tenant
        .namespace()
        .ok_or_else(|| TenantError::Invalid(format!("{} has no namespace", tenant.name_any())))?;
    let source_config_map_name = format!("{}-etcd", identity.cluster_ref.name);
    if tenant_namespace == identity.cluster_ref.namespace
        && identity.config_map_name == source_config_map_name
    {
        return Ok(Some(format!(
            "tenant ConfigMap {tenant_namespace}/{:?} collides with its EtcdCluster connection source",
            identity.config_map_name
        )));
    }

    let tenant_secrets = Api::<Secret>::namespaced(client.clone(), &tenant_namespace);
    if api_object_exists(&tenant_secrets, &identity.output_secret_name).await? {
        return Ok(Some(format!(
            "Secret {tenant_namespace}/{:?} already exists",
            identity.output_secret_name
        )));
    }
    if let Some(password_secret_name) = &identity.password_secret_name
        && api_object_exists(&tenant_secrets, password_secret_name).await?
    {
        return Ok(Some(format!(
            "Secret {tenant_namespace}/{password_secret_name:?} already exists"
        )));
    }

    let tenant_configmaps = Api::<ConfigMap>::namespaced(client.clone(), &tenant_namespace);
    if api_object_exists(&tenant_configmaps, &identity.config_map_name).await? {
        return Ok(Some(format!(
            "ConfigMap {tenant_namespace}/{:?} already exists",
            identity.config_map_name
        )));
    }

    if let Some(certificate_name) = &identity.certificate_name {
        let certificates = certificate_api(client.clone(), &identity.cluster_ref.namespace);
        if api_object_exists(&certificates, certificate_name).await? {
            return Ok(Some(format!(
                "Certificate {}/{certificate_name:?} already exists",
                identity.cluster_ref.namespace
            )));
        }
    }
    if let Some(staging_secret_name) = &identity.staging_secret_name {
        let staging = Api::<Secret>::namespaced(client.clone(), &identity.cluster_ref.namespace);
        if api_object_exists(&staging, staging_secret_name).await? {
            return Ok(Some(format!(
                "staging Secret {}/{staging_secret_name:?} already exists",
                identity.cluster_ref.namespace
            )));
        }
    }
    Ok(None)
}

async fn api_object_exists<K>(api: &Api<K>, name: &str) -> Result<bool, TenantError>
where
    K: Clone + serde::de::DeserializeOwned + std::fmt::Debug,
{
    match api.get(name).await {
        Ok(_) => Ok(true),
        Err(kube::Error::Api(error)) if error.code == 404 => Ok(false),
        Err(error) => Err(error.into()),
    }
}

async fn reconcile_namespace_not_allowed(
    tenant: &EtcdTenant,
    context: &TenantContext,
    cluster: &EtcdCluster,
    namespace: &str,
    name: &str,
) -> Result<Action, TenantError> {
    let status = tenant.status.as_ref();
    let Some(identity) = status.and_then(|status| status.external_identity.as_ref()) else {
        let (reason, message) = if has_finalizer(tenant) {
            (
                "NamespaceDeprovisionBlocked",
                format!(
                    "tenant namespace {namespace} is no longer allowed, but a pre-existing finalizer has no pinned ownership proof; attended legacy recovery is required"
                ),
            )
        } else {
            (
                "NamespaceNotAllowed",
                format!(
                    "tenant namespace {namespace} is not in EtcdCluster allowedNamespaces; no external provisioning was started"
                ),
            )
        };
        update_ready_status(
            &context.client,
            tenant,
            namespace,
            name,
            false,
            reason,
            &message,
        )
        .await?;
        // Exact EtcdCluster reference events normally enqueue this Tenant;
        // retain a bounded retry as watch-recovery fallback.
        return Ok(Action::requeue(Duration::from_secs(30)));
    };

    match status.and_then(|status| status.external_access_state) {
        Some(TenantExternalAccessState::Provisioning)
        | Some(TenantExternalAccessState::Provisioned) => {
            // Persist the cleanup barrier before the first revoke. A
            // concurrent allowedNamespaces restoration cannot bypass this
            // state on the next reconciliation.
            mark_external_access_state(
                &context.client,
                tenant,
                namespace,
                name,
                TenantExternalAccessState::Deprovisioning,
            )
            .await?;
            return Ok(Action::await_change());
        }
        Some(TenantExternalAccessState::Deprovisioning) => {}
        Some(TenantExternalAccessState::Deprovisioned) => {
            update_ready_status(
                &context.client,
                tenant,
                namespace,
                name,
                false,
                "NamespaceNotAllowed",
                &format!(
                    "tenant namespace {namespace} is not in EtcdCluster allowedNamespaces; access is revoked and prefix data is retained"
                ),
            )
            .await?;
            return Ok(Action::requeue(Duration::from_secs(30)));
        }
        Some(TenantExternalAccessState::Planned) => {
            update_ready_status(
                &context.client,
                tenant,
                namespace,
                name,
                false,
                "NamespaceNotAllowed",
                &format!(
                    "tenant namespace {namespace} is not in EtcdCluster allowedNamespaces; external provisioning had not started"
                ),
            )
            .await?;
            return Ok(Action::requeue(Duration::from_secs(30)));
        }
        Some(TenantExternalAccessState::LegacyUnverified) | None => {
            update_ready_status(
                &context.client,
                tenant,
                namespace,
                name,
                false,
                "NamespaceDeprovisionBlocked",
                "external ownership predates the durable provisioning claim; refusing inferred cleanup and requiring attended recovery",
            )
            .await?;
            return Ok(Action::requeue(Duration::from_secs(30)));
        }
    }

    let current_uid = cluster.uid().ok_or_else(|| {
        TenantError::Invalid(format!(
            "{}/{} has no metadata.uid",
            cluster.namespace().unwrap_or_default(),
            cluster.name_any()
        ))
    })?;
    if identity.cluster_ref.name != cluster.name_any()
        || cluster.namespace().as_deref() != Some(identity.cluster_ref.namespace.as_str())
        || identity.cluster_ref.uid != current_uid
    {
        update_ready_status(
            &context.client,
            tenant,
            namespace,
            name,
            false,
            "NamespaceDeprovisionBlocked",
            "the status-pinned EtcdCluster no longer matches the referenced object; refusing cleanup against a replacement cluster",
        )
        .await?;
        return Ok(Action::requeue(Duration::from_secs(30)));
    }
    if !external_identity_matches_desired(tenant, cluster, identity)? {
        update_ready_status(
            &context.client,
            tenant,
            namespace,
            name,
            false,
            "NamespaceDeprovisionBlocked",
            "status.externalIdentity does not exactly match the deterministic identity for this tenant; refusing destructive cleanup",
        )
        .await?;
        return Ok(Action::requeue(Duration::from_secs(30)));
    }

    let (mut etcd_client, _physical_server_ca) = match build_verified_admin_client(
        &context.client,
        cluster,
        false,
    )
    .await
    {
        Ok(verified) => verified,
        Err(error) => {
            update_ready_status(
                    &context.client,
                    tenant,
                    namespace,
                    name,
                    false,
                    "NamespaceDeprovisionBlocked",
                    &format!(
                        "tenant namespace authorization was revoked, but fresh physical etcd identity proof failed: {error}"
                    ),
                )
                .await?;
            return Ok(Action::requeue(Duration::from_secs(30)));
        }
    };

    info!(
        tenant_namespace = namespace,
        tenant_name = name,
        etcd_user = identity.etcd_user,
        etcd_role = identity.etcd_role,
        prefix = identity.prefix,
        "revoking tenant access after EtcdCluster namespace authorization was removed"
    );
    revoke_external_tenant_access(&mut etcd_client, identity).await?;

    let namespace_terminating = namespace_is_terminating(&context.client, namespace).await?;
    if !cleanup_kubernetes_artifacts(
        &context.client,
        tenant,
        identity,
        namespace,
        namespace_terminating,
        ArtifactCleanupMode::RequireOwned,
    )
    .await?
    {
        update_ready_status(
            &context.client,
            tenant,
            namespace,
            name,
            false,
            "NamespaceDeprovisioning",
            "external access is revoked; waiting for Kubernetes credential artifacts to disappear",
        )
        .await?;
        return Ok(Action::requeue(Duration::from_secs(5)));
    }

    mark_external_access_state(
        &context.client,
        tenant,
        namespace,
        name,
        TenantExternalAccessState::Deprovisioned,
    )
    .await?;
    Ok(Action::requeue(Duration::from_secs(5)))
}

fn cluster_allows_tenant_namespace(
    cluster_namespace: &str,
    tenant_namespace: &str,
    allowed_namespaces: &[String],
) -> bool {
    cluster_namespace != tenant_namespace
        && allowed_namespaces
            .iter()
            .any(|allowed| allowed == "*" || allowed == tenant_namespace)
}

fn deprovisioning_must_complete(state: Option<TenantExternalAccessState>) -> bool {
    state == Some(TenantExternalAccessState::Deprovisioning)
}

fn cluster_reference_is_cross_namespace(tenant: &EtcdTenant, tenant_namespace: &str) -> bool {
    tenant
        .spec
        .cluster_ref
        .namespace
        .as_deref()
        .is_some_and(|cluster_namespace| {
            !cluster_namespace.is_empty() && cluster_namespace != tenant_namespace
        })
}

fn cluster_is_ready_for_tenant_provisioning(cluster: &EtcdCluster) -> bool {
    let Some(generation) = cluster.meta().generation else {
        return false;
    };
    cluster.status.as_ref().is_some_and(|status| {
        status.connected
            && status.auth_enabled
            && status.version.as_deref() == Some(crate::cluster::QUALIFIED_ETCD_VERSION)
            && status.cluster_id.as_deref().is_some_and(valid_cluster_id)
            && status.alarms.is_empty()
            && status.conditions.iter().any(|condition| {
                condition.type_ == "Ready"
                    && condition.status == ConditionStatus::True
                    && condition.observed_generation == Some(generation)
            })
    })
}

fn valid_cluster_id(cluster_id: &str) -> bool {
    cluster_id.len() == 16
        && cluster_id
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn error_policy(
    _tenant: Arc<EtcdTenant>,
    error: &TenantError,
    _context: Arc<TenantContext>,
) -> Action {
    warn!(
        error = %error,
        error_debug = ?error,
        error_chain = %crate::format_error_chain(error),
        "applying EtcdTenant error policy"
    );
    Action::requeue(Duration::from_secs(60))
}

#[derive(Debug, PartialEq)]
enum TenantDeletionAccess<'a> {
    Planned,
    Provisioned(&'a TenantExternalIdentity),
    Unproven,
}

fn tenant_deletion_access(tenant: &EtcdTenant) -> TenantDeletionAccess<'_> {
    let Some(status) = tenant.status.as_ref() else {
        return TenantDeletionAccess::Unproven;
    };
    let Some(identity) = status.external_identity.as_ref() else {
        return TenantDeletionAccess::Unproven;
    };
    match status.external_access_state {
        Some(TenantExternalAccessState::Planned)
        | Some(TenantExternalAccessState::Deprovisioned) => TenantDeletionAccess::Planned,
        Some(TenantExternalAccessState::Provisioning)
        | Some(TenantExternalAccessState::Provisioned)
        | Some(TenantExternalAccessState::Deprovisioning) => {
            TenantDeletionAccess::Provisioned(identity)
        }
        Some(TenantExternalAccessState::LegacyUnverified) | None => TenantDeletionAccess::Unproven,
    }
}

async fn reconcile_delete(
    tenant: Arc<EtcdTenant>,
    context: Arc<TenantContext>,
    namespace: &str,
    name: &str,
) -> Result<Action, TenantError> {
    if !has_finalizer(&tenant) {
        return Ok(Action::await_change());
    }

    let identity = match tenant_deletion_access(&tenant) {
        TenantDeletionAccess::Planned => {
            // Planned proves no provisioning mutation started. Deprovisioned
            // proves exact revocation and artifact cleanup completed. Neither
            // state needs another etcd mutation during deletion.
            info!(
                namespace,
                name, "deleting a planned tenant without touching unprovisioned external access"
            );
            remove_finalizer(&context.client, &tenant, namespace).await?;
            return Ok(Action::await_change());
        }
        TenantDeletionAccess::Provisioned(identity) => identity.clone(),
        TenantDeletionAccess::Unproven => {
            warn!(
                namespace,
                name,
                "external access is absent or only partially proven during deletion; refusing inferred cleanup and keeping the finalizer for attended recovery"
            );
            return Ok(Action::requeue(Duration::from_secs(30)));
        }
    };

    let clusters =
        Api::<EtcdCluster>::namespaced(context.client.clone(), &identity.cluster_ref.namespace);
    let cluster = match clusters.get(&identity.cluster_ref.name).await {
        Ok(cluster) => cluster,
        Err(kube::Error::Api(ae)) if ae.code == 404 => {
            warn!(
                namespace,
                name, "pinned EtcdCluster missing during cleanup; keeping finalizer"
            );
            return Ok(Action::requeue(Duration::from_secs(30)));
        }
        Err(err) => return Err(err.into()),
    };
    if cluster.uid().as_deref() != Some(identity.cluster_ref.uid.as_str()) {
        warn!(
            namespace,
            name,
            pinned_cluster_uid = identity.cluster_ref.uid,
            current_cluster_uid = cluster.uid().unwrap_or_default(),
            "EtcdCluster UID changed during cleanup; keeping finalizer"
        );
        return Ok(Action::requeue(Duration::from_secs(30)));
    }
    match external_identity_matches_desired(&tenant, &cluster, &identity) {
        Ok(true) => {}
        Ok(false) => {
            warn!(
                namespace,
                name,
                "status.externalIdentity does not exactly match the deterministic tenant identity; refusing cleanup and keeping finalizer"
            );
            return Ok(Action::requeue(Duration::from_secs(30)));
        }
        Err(error) => {
            warn!(
                namespace,
                name,
                error = %error,
                "could not verify status.externalIdentity before cleanup; keeping finalizer"
            );
            return Ok(Action::requeue(Duration::from_secs(30)));
        }
    }

    let (mut etcd_client, _physical_server_ca) =
        match build_verified_admin_client(&context.client, &cluster, false).await {
            Ok(verified) => verified,
            Err(error) => {
                warn!(
                    namespace,
                    name,
                    error = %error,
                    "fresh physical etcd identity proof failed during cleanup; keeping finalizer"
                );
                return Ok(Action::requeue(Duration::from_secs(30)));
            }
        };

    info!(
        namespace,
        name,
        prefix = identity.prefix,
        "removing tenant RBAC and credentials while retaining all prefix data"
    );
    revoke_external_tenant_access(&mut etcd_client, &identity).await?;

    let namespace_terminating = namespace_is_terminating(&context.client, namespace).await?;
    if !cleanup_kubernetes_artifacts(
        &context.client,
        &tenant,
        &identity,
        namespace,
        namespace_terminating,
        ArtifactCleanupMode::RetainForeign,
    )
    .await?
    {
        return Ok(Action::requeue(Duration::from_secs(5)));
    }

    remove_finalizer(&context.client, &tenant, namespace).await?;
    Ok(Action::await_change())
}

fn build_external_identity(
    tenant: &EtcdTenant,
    cluster: &EtcdCluster,
) -> Result<TenantExternalIdentity, TenantError> {
    let namespace = tenant
        .namespace()
        .ok_or_else(|| TenantError::Invalid(format!("{} has no namespace", tenant.name_any())))?;
    let name = tenant.name_any();
    let tenant_uid = tenant
        .uid()
        .ok_or_else(|| TenantError::Invalid(format!("{namespace}/{name} has no metadata.uid")))?;
    let cluster_namespace = cluster
        .namespace()
        .ok_or_else(|| TenantError::Invalid(format!("{} has no namespace", cluster.name_any())))?;
    let referenced_cluster_namespace = tenant
        .spec
        .cluster_ref
        .namespace
        .as_deref()
        .unwrap_or(&namespace);
    if cluster_namespace != referenced_cluster_namespace
        || cluster.name_any() != tenant.spec.cluster_ref.name
    {
        return Err(TenantError::Invalid(format!(
            "resolved EtcdCluster {cluster_namespace}/{} does not match spec.clusterRef {referenced_cluster_namespace}/{}",
            cluster.name_any(),
            tenant.spec.cluster_ref.name
        )));
    }
    let cluster_uid = cluster.uid().ok_or_else(|| {
        TenantError::Invalid(format!(
            "{cluster_namespace}/{} has no metadata.uid",
            cluster.name_any()
        ))
    })?;
    let stable_name = format!("{namespace}:{name}");

    let output_secret_name = tenant
        .spec
        .secret_name
        .clone()
        .unwrap_or_else(|| format!("{name}-etcd"));
    let (etcd_user, etcd_role, password_secret_name, certificate_name, staging_secret_name) =
        match tenant.spec.credential_mode {
            TenantCredentialMode::Password => {
                let password_secret_name = format!("{name}-etcd-password");
                (
                    stable_name.clone(),
                    stable_name.clone(),
                    Some(password_secret_name),
                    None,
                    None,
                )
            }
            TenantCredentialMode::Tls => {
                let certificate_name = format!("etcdtenant-{tenant_uid}");
                let staging_secret_name = format!("{certificate_name}-tls");
                (
                    format!("etcdtenant:{tenant_uid}"),
                    format!("etcdtenant-role:{tenant_uid}"),
                    None,
                    Some(certificate_name),
                    Some(staging_secret_name),
                )
            }
        };

    Ok(TenantExternalIdentity {
        cluster_ref: PinnedClusterReference {
            name: cluster.name_any(),
            namespace: cluster_namespace,
            uid: cluster_uid,
        },
        credential_mode: tenant.spec.credential_mode,
        etcd_user,
        etcd_role,
        prefix: format!("/{stable_name}/"),
        output_secret_name,
        config_map_name: format!("{name}-etcd"),
        password_secret_name,
        certificate_name,
        staging_secret_name,
    })
}

fn external_identity_matches_desired(
    tenant: &EtcdTenant,
    cluster: &EtcdCluster,
    identity: &TenantExternalIdentity,
) -> Result<bool, TenantError> {
    Ok(identity == &build_external_identity(tenant, cluster)?)
}

fn tenant_owner_reference(tenant: &EtcdTenant) -> Result<OwnerReference, TenantError> {
    let mut owner = tenant.controller_owner_ref(&()).ok_or_else(|| {
        TenantError::Invalid(format!(
            "{} cannot produce controller owner reference",
            tenant.name_any()
        ))
    })?;
    // OwnerReferencesPermissionEnforcement requires owner-deletion privileges
    // when this is true. Tenant finalizers already provide lifecycle ordering,
    // so dependents do not need to block deletion of their owner.
    owner.block_owner_deletion = Some(false);
    Ok(owner)
}

fn cluster_owner_reference(cluster: &EtcdCluster) -> Result<OwnerReference, TenantError> {
    let mut owner = cluster.controller_owner_ref(&()).ok_or_else(|| {
        TenantError::Invalid(format!(
            "{} cannot produce controller owner reference",
            cluster.name_any()
        ))
    })?;
    owner.block_owner_deletion = Some(false);
    Ok(owner)
}

fn validate_external_identity(
    tenant: &EtcdTenant,
    identity: &TenantExternalIdentity,
) -> Result<(), TenantError> {
    let tenant_namespace = tenant
        .namespace()
        .ok_or_else(|| TenantError::Invalid(format!("{} has no namespace", tenant.name_any())))?;
    match identity.credential_mode {
        TenantCredentialMode::Password => {
            if identity.password_secret_name.as_deref()
                == Some(identity.output_secret_name.as_str())
            {
                return Err(TenantError::Invalid(format!(
                    "output Secret {:?} collides with the internal password Secret",
                    identity.output_secret_name
                )));
            }
        }
        TenantCredentialMode::Tls => {
            if identity.etcd_user.len() > 64 {
                return Err(TenantError::Invalid(format!(
                    "TLS tenant identity {:?} is {} bytes; X.509 commonName is limited to 64",
                    identity.etcd_user,
                    identity.etcd_user.len()
                )));
            }
            if tenant_namespace == identity.cluster_ref.namespace
                && identity.staging_secret_name.as_deref()
                    == Some(identity.output_secret_name.as_str())
            {
                return Err(TenantError::Invalid(format!(
                    "output Secret {:?} collides with the TLS staging Secret",
                    identity.output_secret_name
                )));
            }
        }
    }
    Ok(())
}

async fn build_verified_admin_client(
    client: &Client,
    cluster: &EtcdCluster,
    require_ready: bool,
) -> Result<(etcd_client::Client, ByteString), TenantError> {
    let namespace = cluster
        .namespace()
        .ok_or_else(|| TenantError::Invalid(format!("{} has no namespace", cluster.name_any())))?;
    let secrets = Api::<Secret>::namespaced(client.clone(), &namespace);
    let auth_secret = secrets.get(&cluster.spec.auth_secret_ref.name).await?;
    let physical_server_ca = read_physical_server_ca(client, cluster).await?;
    let mut admin =
        crate::etcd::build_client(&cluster.spec.endpoints, &auth_secret, &physical_server_ca.0)
            .await
            .map_err(|error| {
                TenantError::Invalid(format!(
                    "failed to connect to EtcdCluster {namespace}/{}: {error:#}",
                    cluster.name_any()
                ))
            })?;
    crate::cluster::verify_cluster_for_mutation(
        &mut admin,
        cluster,
        &auth_secret,
        &physical_server_ca.0,
        require_ready,
    )
    .await
    .map_err(|error| {
        TenantError::Invalid(format!(
            "EtcdCluster {namespace}/{} failed fresh mutation identity proof: {error}",
            cluster.name_any()
        ))
    })?;
    Ok((admin, physical_server_ca))
}

async fn revoke_external_tenant_access(
    client: &mut etcd_client::Client,
    identity: &TenantExternalIdentity,
) -> Result<(), TenantError> {
    let mut auth = client.auth_client();

    // Deleting a user removes all of its role memberships. Calling
    // user_revoke_role first is not idempotent: etcd returns RoleNotGranted
    // when provisioning stopped after user creation but before the grant.
    ignore_not_found(auth.user_delete(&identity.etcd_user).await)?;
    ignore_not_found(auth.role_delete(&identity.etcd_role).await)?;
    Ok(())
}

async fn namespace_is_terminating(client: &Client, namespace: &str) -> Result<bool, TenantError> {
    let namespaces = Api::<Namespace>::all(client.clone());
    match namespaces.get(namespace).await {
        Ok(namespace) => Ok(namespace.metadata.deletion_timestamp.is_some()),
        // A namespaced Tenant normally cannot outlive its Namespace object,
        // but absence still proves namespace GC owns all local artifacts.
        Err(kube::Error::Api(ae)) if ae.code == 404 => Ok(true),
        Err(err) => Err(err.into()),
    }
}

async fn external_identity_collision(
    client: &mut etcd_client::Client,
    user_name: &str,
    role_name: &str,
    desired_prefix: &str,
) -> Result<Option<String>, TenantError> {
    let mut auth = client.auth_client();
    match auth.user_get(user_name).await {
        Ok(_) => {
            return Ok(Some(format!(
                "planned etcd user {user_name:?} already exists"
            )));
        }
        Err(error) if is_not_found_error(&error) => {}
        Err(error) => return Err(error.into()),
    }
    match auth.role_get(role_name).await {
        Ok(_) => {
            return Ok(Some(format!(
                "planned etcd role {role_name:?} already exists"
            )));
        }
        Err(error) if is_not_found_error(&error) => {}
        Err(error) => return Err(error.into()),
    }

    if let Some(collision) =
        scan_external_isolation(&mut auth, user_name, role_name, desired_prefix).await?
    {
        return Ok(Some(collision));
    }
    Ok(None)
}

async fn external_isolation_collision(
    client: &mut etcd_client::Client,
    user_name: &str,
    role_name: &str,
    desired_prefix: &str,
) -> Result<Option<String>, TenantError> {
    scan_external_isolation(
        &mut client.auth_client(),
        user_name,
        role_name,
        desired_prefix,
    )
    .await
}

async fn scan_external_isolation(
    auth: &mut etcd_client::AuthClient,
    user_name: &str,
    role_name: &str,
    desired_prefix: &str,
) -> Result<Option<String>, TenantError> {
    let roles = auth.role_list().await?;
    for existing_role in roles.roles() {
        if !external_role_requires_overlap_scan(existing_role, role_name) {
            continue;
        }
        let existing = auth.role_get(existing_role).await?;
        if existing
            .permissions()
            .iter()
            .any(|permission| permission_overlaps_prefix(permission, desired_prefix.as_bytes()))
        {
            return Ok(Some(format!(
                "existing etcd role {existing_role:?} grants a key range overlapping desired prefix {desired_prefix:?}"
            )));
        }
    }

    let users = auth.user_list().await?;
    for existing_user in users.users() {
        if existing_user == user_name {
            continue;
        }
        let existing = auth.user_get(existing_user).await?;
        if existing.roles().iter().any(|role| role == role_name) {
            return Ok(Some(format!(
                "existing etcd user {existing_user:?} is bound to owned role {role_name:?}"
            )));
        }
    }
    Ok(None)
}

fn external_role_requires_overlap_scan(existing_role: &str, owned_role: &str) -> bool {
    // etcd's built-in root role has an implicit all-keys grant. It is the
    // administrative trust root this controller already depends on, rather
    // than a competing tenant identity. No other full-range role is trusted.
    existing_role != owned_role && existing_role != "root"
}

fn permission_overlaps_prefix(permission: &etcd_client::Permission, desired_prefix: &[u8]) -> bool {
    let desired_end = prefix_range_end(desired_prefix);
    let permission_start = permission.key();

    if permission.is_from_key() {
        return permission_start == [b'\0'] || permission_start < desired_end.as_slice();
    }
    let computed_prefix_end;
    let permission_end = if permission.is_prefix() {
        computed_prefix_end = prefix_range_end(permission_start);
        computed_prefix_end.as_slice()
    } else {
        permission.range_end()
    };
    if permission_end.is_empty() {
        return permission_start >= desired_prefix && permission_start < desired_end.as_slice();
    }
    permission_start < desired_end.as_slice() && desired_prefix < permission_end
}

fn prefix_range_end(prefix: &[u8]) -> Vec<u8> {
    let mut end = prefix.to_vec();
    for index in (0..end.len()).rev() {
        if end[index] < 0xff {
            end[index] += 1;
            end.truncate(index + 1);
            return end;
        }
    }
    vec![0]
}

async fn ensure_tenant_rbac(
    client: &mut etcd_client::Client,
    user_name: &str,
    role_name: &str,
    prefix: &str,
    credential: TenantCredential<'_>,
) -> Result<(), TenantError> {
    let mut auth = client.auth_client();
    let desired_permission = etcd_client::Permission::read_write(prefix).with_prefix();
    let existing_user = match auth.user_get(user_name).await {
        Ok(user) => Some(user),
        Err(error) if is_not_found_error(&error) => None,
        Err(error) => return Err(error.into()),
    };
    let existing_role = match auth.role_get(role_name).await {
        Ok(role) => Some(role),
        Err(error) if is_not_found_error(&error) => None,
        Err(error) => return Err(error.into()),
    };
    let user_exact = existing_user.as_ref().is_some_and(|user| {
        user.roles().len() == 1 && user.roles().first().map(String::as_str) == Some(role_name)
    });
    let role_exact = existing_role.as_ref().is_some_and(|role| {
        role.permissions().len() == 1 && role.permissions().first() == Some(&desired_permission)
    });

    let mut user_recreated = false;
    if !user_exact || !role_exact {
        // Deleting the user atomically revokes every current membership and
        // credential before the role is changed. This makes legacy drift
        // fail closed: an old Password or TLS credential cannot retain root
        // or broad desired-role access across a partial reconciliation.
        if existing_user.is_some() {
            info!(
                user_name,
                "temporarily removing drifted etcd tenant user before role sanitization"
            );
            auth.user_delete(user_name).await?;
        }

        revoke_owned_role_from_foreign_users(&mut auth, user_name, role_name).await?;

        if existing_role.is_none() {
            info!(role_name, "creating etcd tenant role");
            auth.role_add(role_name).await?;
        }
        let role = auth.role_get(role_name).await?;
        for permission in stale_permissions(role.permissions(), &desired_permission) {
            info!(
                role_name,
                prefix, "revoking stale permission from detached tenant role"
            );
            auth.role_revoke_permission(
                role_name,
                permission.key().to_vec(),
                revoke_permission_options(&permission),
            )
            .await?;
        }
        let role = auth.role_get(role_name).await?;
        if !role
            .permissions()
            .iter()
            .any(|permission| permission == &desired_permission)
        {
            info!(
                role_name,
                prefix, "granting prefix permission to detached tenant role"
            );
            auth.role_grant_permission(role_name, desired_permission.clone())
                .await?;
        }

        info!(user_name, "creating sanitized etcd tenant user");
        match credential {
            TenantCredential::Password(password) => {
                auth.user_add(user_name, password, None).await?;
            }
            TenantCredential::Tls => {
                auth.user_add(
                    user_name,
                    "",
                    Some(etcd_client::UserAddOptions::new().with_no_pwd()),
                )
                .await?;
            }
        }
        user_recreated = true;
        auth.user_grant_role(user_name, role_name).await?;
    } else {
        revoke_owned_role_from_foreign_users(&mut auth, user_name, role_name).await?;
    }

    let final_role = auth.role_get(role_name).await?;
    if final_role.permissions().len() != 1
        || final_role.permissions().first() != Some(&desired_permission)
    {
        return Err(TenantError::Invalid(format!(
            "etcd role {role_name:?} did not converge to exactly one desired prefix permission"
        )));
    }
    let final_user = auth.user_get(user_name).await?;
    if final_user.roles().len() != 1
        || final_user.roles().first().map(String::as_str) != Some(role_name)
    {
        return Err(TenantError::Invalid(format!(
            "etcd user {user_name:?} did not converge to exactly role {role_name:?}"
        )));
    }

    // Exact roles are now proven. Avoid changing an already-correct password:
    // every auth metadata mutation advances etcd's auth revision and can
    // invalidate unrelated long-lived Password-tenant tokens. Authenticate is
    // a read-only proof. A newly created user already has the desired value;
    // an exact existing user is changed only on the precise bad-credential
    // response, then verified.
    if let TenantCredential::Password(password) = credential
        && !user_recreated
    {
        match auth
            .authenticate(user_name.to_string(), password.to_string())
            .await
        {
            Ok(_) => {}
            Err(error) if is_invalid_credentials_error(&error) => {
                auth.user_change_password(user_name, password).await?;
                auth.authenticate(user_name.to_string(), password.to_string())
                    .await?;
            }
            Err(error) => return Err(error.into()),
        }
    }

    Ok(())
}

async fn revoke_owned_role_from_foreign_users(
    auth: &mut etcd_client::AuthClient,
    owned_user: &str,
    owned_role: &str,
) -> Result<(), TenantError> {
    let users = auth.user_list().await?;
    for user_name in users.users() {
        if user_name == owned_user {
            continue;
        }
        let user = match auth.user_get(user_name).await {
            Ok(user) => user,
            Err(error) if is_not_found_error(&error) => continue,
            Err(error) => return Err(error.into()),
        };
        if !user.roles().iter().any(|role| role == owned_role) {
            continue;
        }
        info!(
            user_name,
            owned_role, "revoking controller-owned tenant role from foreign etcd user"
        );
        match auth.user_revoke_role(user_name, owned_role).await {
            Ok(_) => {}
            Err(error) if is_not_found_error(&error) || is_role_not_granted_error(&error) => {}
            Err(error) => return Err(error.into()),
        }
    }
    Ok(())
}

#[derive(Clone, Copy)]
enum TenantCredential<'a> {
    Password(&'a str),
    Tls,
}

fn stale_permissions(
    permissions: Vec<etcd_client::Permission>,
    desired: &etcd_client::Permission,
) -> Vec<etcd_client::Permission> {
    permissions
        .into_iter()
        .filter(|permission| permission != desired)
        .collect()
}

fn revoke_permission_options(
    permission: &etcd_client::Permission,
) -> Option<etcd_client::RoleRevokePermissionOptions> {
    if permission.is_prefix() {
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
    }
}

async fn ensure_password_output_secret(
    client: &Client,
    tenant: &EtcdTenant,
    tenant_namespace: &str,
    secret_name: &str,
    tenant_name: &str,
    password: &str,
) -> Result<(), TenantError> {
    let owner_reference = tenant_owner_reference(tenant)?;

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
    ensure_secret_is_tenant_owned(&secrets, secret_name, tenant).await?;
    secrets
        .patch(
            secret_name,
            &PatchParams::apply(FIELD_MANAGER),
            &Patch::Apply(&secret),
        )
        .await?;

    Ok(())
}

async fn ensure_tls_credentials(
    client: &Client,
    admin_client: &mut etcd_client::Client,
    tenant: &EtcdTenant,
    cluster: &EtcdCluster,
    identity: &TenantExternalIdentity,
    tenant_namespace: &str,
    physical_server_ca: &ByteString,
) -> Result<Option<String>, TenantError> {
    let tls = cluster.spec.tenant_tls.as_ref().ok_or_else(|| {
        TenantError::Invalid(format!(
            "TLS tenant {tenant_namespace}/{} references EtcdCluster {}/{} without spec.tenantTls",
            tenant.name_any(),
            identity.cluster_ref.namespace,
            identity.cluster_ref.name,
        ))
    })?;
    let certificate_name = identity
        .certificate_name
        .as_deref()
        .ok_or_else(|| TenantError::Invalid("TLS identity has no Certificate name".to_string()))?;
    let staging_secret_name = identity.staging_secret_name.as_deref().ok_or_else(|| {
        TenantError::Invalid("TLS identity has no staging Secret name".to_string())
    })?;
    let tenant_uid = tenant.uid().ok_or_else(|| {
        TenantError::Invalid(format!(
            "{tenant_namespace}/{} has no uid",
            tenant.name_any()
        ))
    })?;

    let mut labels = BTreeMap::new();
    labels.insert(TENANT_UID_LABEL.to_string(), tenant_uid.clone());
    labels.insert(
        TENANT_NAMESPACE_LABEL.to_string(),
        tenant_namespace.to_string(),
    );
    labels.insert(TENANT_NAME_LABEL.to_string(), tenant.name_any());

    let certificates = certificate_api(client.clone(), &identity.cluster_ref.namespace);
    match certificates.get(certificate_name).await {
        Ok(existing) => ensure_labeled_for_tenant(
            &existing.metadata,
            certificate_name,
            "Certificate",
            &tenant_uid,
        )?,
        Err(kube::Error::Api(ae)) if ae.code == 404 => {}
        Err(err) => return Err(err.into()),
    }

    let cluster_owner = cluster_owner_reference(cluster)?;
    let certificate = build_certificate_manifest(
        certificate_name,
        staging_secret_name,
        identity,
        &tls.issuer_ref,
        &labels,
        cluster_owner,
    );
    let certificate = certificates
        .patch(
            certificate_name,
            &PatchParams::apply(FIELD_MANAGER),
            &Patch::Apply(&certificate),
        )
        .await?;
    if !certificate_is_current_and_ready(&certificate) {
        return Ok(None);
    }

    let staging_secrets =
        Api::<Secret>::namespaced(client.clone(), &identity.cluster_ref.namespace);
    let staging = match staging_secrets.get(staging_secret_name).await {
        Ok(secret) => secret,
        Err(kube::Error::Api(ae)) if ae.code == 404 => return Ok(None),
        Err(err) => return Err(err.into()),
    };
    ensure_labeled_for_tenant(
        &staging.metadata,
        staging_secret_name,
        "staging Secret",
        &tenant_uid,
    )?;

    // cert-manager's staging ca.crt identifies the client issuer and is not
    // the CA used to verify the physical etcd server certificates. Use the
    // same server-CA snapshot that passed the fresh admin mutation proof.
    let data = compose_tls_secret_data(&staging, physical_server_ca, &identity.etcd_user)?;
    validate_tls_leaf_for_ready(&data["tls.crt"].0, chrono::Utc::now().timestamp())?;

    let owner_reference = tenant_owner_reference(tenant)?;
    let output = Secret {
        metadata: ObjectMeta {
            name: Some(identity.output_secret_name.clone()),
            namespace: Some(tenant_namespace.to_string()),
            owner_references: Some(vec![owner_reference]),
            ..ObjectMeta::default()
        },
        data: Some(data),
        type_: Some("kubernetes.io/tls".to_string()),
        ..Secret::default()
    };
    let output_secrets = Api::<Secret>::namespaced(client.clone(), tenant_namespace);
    let existing_output =
        get_tenant_owned_secret(&output_secrets, &identity.output_secret_name, tenant).await?;
    let already_verified = tenant_is_ready(tenant)
        && existing_output
            .as_ref()
            .is_some_and(|existing| existing.type_ == output.type_ && existing.data == output.data);
    if already_verified {
        run_tls_liveness_probe(
            &cluster.spec.endpoints,
            &output,
            physical_server_ca,
            &identity.prefix,
        )
        .await?;
    } else {
        run_tls_access_smoke(
            admin_client,
            &cluster.spec.endpoints,
            &output,
            physical_server_ca,
            identity,
            &tenant_uid,
        )
        .await?;
    }
    // Publish the stable consumer Secret only after the fresh credential has
    // proven both inside-prefix access and outside-prefix denial. Derive the
    // public rollout revision from the API server's acknowledged Secret, then
    // allow its ConfigMap handoff to be published.
    let published = output_secrets
        .patch(
            &identity.output_secret_name,
            &PatchParams::apply(FIELD_MANAGER),
            &Patch::Apply(&output),
        )
        .await?;
    let client_certificate_revision = published_client_certificate_revision(&published)?;

    Ok(Some(client_certificate_revision))
}

async fn run_tls_liveness_probe(
    endpoints: &[String],
    output_secret: &Secret,
    physical_server_ca: &ByteString,
    prefix: &str,
) -> Result<(), TenantError> {
    let mut tenant_client = tokio::time::timeout(
        Duration::from_secs(10),
        crate::etcd::build_client(endpoints, output_secret, &physical_server_ca.0),
    )
    .await
    .map_err(|_| {
        TenantError::Invalid("timed out connecting with current tenant TLS Secret".to_string())
    })?
    .map_err(|error| {
        TenantError::Invalid(format!(
            "current tenant TLS Secret could not connect to etcd: {error:#}"
        ))
    })?;
    bounded_etcd(
        "read tenant prefix with current TLS credential",
        tenant_client.get(
            prefix,
            Some(etcd_client::GetOptions::new().with_prefix().with_limit(1)),
        ),
    )
    .await?;
    Ok(())
}

async fn run_tls_access_smoke(
    admin_client: &mut etcd_client::Client,
    endpoints: &[String],
    output_secret: &Secret,
    physical_server_ca: &ByteString,
    identity: &TenantExternalIdentity,
    tenant_uid: &str,
) -> Result<(), TenantError> {
    let mut tenant_client = tokio::time::timeout(
        Duration::from_secs(10),
        crate::etcd::build_client(endpoints, output_secret, &physical_server_ca.0),
    )
    .await
    .map_err(|_| {
        TenantError::Invalid("timed out connecting with composed tenant TLS Secret".to_string())
    })?
    .map_err(|err| {
        TenantError::Invalid(format!(
            "composed tenant TLS Secret could not connect to etcd: {err:#}"
        ))
    })?;

    // Every probe gets an unguessable key. Creation is compare-and-put only,
    // so no tenant-owned value can be overwritten, and the short lease bounds
    // residue even if the controller dies after an ambiguous response.
    let nonce = access_probe_nonce();
    let (inside_key, outside_key) = access_probe_keys(identity, tenant_uid, &nonce);
    let probe_value = format!("tenant-access-probe:{tenant_uid}:{nonce}");
    let lease = bounded_etcd(
        "grant tenant access-probe lease",
        tenant_client.lease_grant(30, None),
    )
    .await?;
    let lease_id = lease.id();
    if lease_id == 0 {
        return Err(TenantError::Invalid(
            "etcd returned a zero lease ID for the tenant access probe".to_string(),
        ));
    }

    let probe_result = async {
        let create = etcd_client::Txn::new()
            .when(vec![etcd_client::Compare::version(
                inside_key.clone(),
                etcd_client::CompareOp::Equal,
                0,
            )])
            .and_then(vec![etcd_client::TxnOp::put(
                inside_key.clone(),
                probe_value.clone(),
                Some(etcd_client::PutOptions::new().with_lease(lease_id)),
            )]);
        let created = bounded_etcd(
            "create unique inside-prefix access sentinel",
            tenant_client.txn(create),
        )
        .await?;
        if !created.succeeded() {
            return Err(TenantError::Invalid(
                "random tenant access-probe key already existed; no value was overwritten"
                    .to_string(),
            ));
        }
        let created_revision = created
            .header()
            .map(|header| header.revision())
            .filter(|revision| *revision > 0)
            .ok_or_else(|| {
                TenantError::Invalid(
                    "tenant access-probe transaction returned no positive revision".to_string(),
                )
            })?;

        let response = bounded_etcd(
            "get inside tenant prefix",
            tenant_client.get(inside_key.as_str(), None),
        )
        .await?;
        let observed = response.kvs();
        if observed.len() != 1
            || observed[0].key() != inside_key.as_bytes()
            || observed[0].value() != probe_value.as_bytes()
            || observed[0].create_revision() != created_revision
            || observed[0].mod_revision() != created_revision
            || observed[0].version() != 1
            || observed[0].lease() != lease_id
        {
            return Err(TenantError::Invalid(
                "tenant access probe did not read back the exact newly leased sentinel".to_string(),
            ));
        }

        let delete = etcd_client::Txn::new()
            .when(vec![
                etcd_client::Compare::mod_revision(
                    inside_key.clone(),
                    etcd_client::CompareOp::Equal,
                    created_revision,
                ),
                etcd_client::Compare::value(
                    inside_key.clone(),
                    etcd_client::CompareOp::Equal,
                    probe_value.clone(),
                ),
                etcd_client::Compare::lease(
                    inside_key.clone(),
                    etcd_client::CompareOp::Equal,
                    lease_id,
                ),
            ])
            .and_then(vec![etcd_client::TxnOp::delete(inside_key.clone(), None)]);
        let deleted = bounded_etcd(
            "delete unchanged inside-prefix access sentinel",
            tenant_client.txn(delete),
        )
        .await?;
        if !deleted.succeeded() {
            return Err(TenantError::Invalid(
                "tenant access-probe sentinel changed before conditional cleanup".to_string(),
            ));
        }

        if !expect_permission_denied(
            "read outside tenant prefix",
            tenant_client.get(outside_key.as_str(), None),
        )
        .await?
        {
            return Err(TenantError::Invalid(
                "tenant TLS identity unexpectedly read outside its prefix".to_string(),
            ));
        }

        let outside_create = etcd_client::Txn::new()
            .when(vec![etcd_client::Compare::version(
                outside_key.clone(),
                etcd_client::CompareOp::Equal,
                0,
            )])
            .and_then(vec![etcd_client::TxnOp::put(
                outside_key.clone(),
                probe_value.clone(),
                Some(etcd_client::PutOptions::new().with_lease(lease_id)),
            )]);
        if !expect_permission_denied(
            "write outside tenant prefix",
            tenant_client.txn(outside_create),
        )
        .await?
        {
            return Err(TenantError::Invalid(
                "tenant TLS identity unexpectedly wrote outside its prefix".to_string(),
            ));
        }

        Ok(())
    }
    .await;

    let revoke_result = bounded_etcd(
        "revoke tenant access-probe lease",
        tenant_client.lease_revoke(lease_id),
    )
    .await;
    let tenant_probe_result = match (probe_result, revoke_result) {
        (Ok(()), Ok(_)) => Ok(()),
        (Ok(()), Err(error)) => Err(error),
        (Err(error), Ok(_)) => Err(error),
        (Err(error), Err(cleanup_error)) => {
            warn!(
                error = %cleanup_error,
                lease_id,
                "tenant access probe failed and its short lease could not be revoked immediately"
            );
            Err(error)
        }
    };
    tenant_probe_result?;

    run_populated_foreign_lease_guard(
        admin_client,
        &mut tenant_client,
        tenant_uid,
        &nonce,
        &inside_key,
    )
    .await
}

/// Proves the server rejects cross-prefix revocation of a populated lease.
///
/// etcd leases do not have owners, so this is deliberately not described as
/// lease isolation. It is a narrow integrity regression guard: the admin
/// client creates a short-lived lease containing an unguessable key outside
/// the tenant prefix, and the tenant must receive gRPC PermissionDenied when
/// trying to revoke it. The admin then proves the exact sentinel survived and
/// revokes the lease. A 30-second TTL bounds residue after controller failure
/// or an ambiguous response.
async fn run_populated_foreign_lease_guard(
    admin_client: &mut etcd_client::Client,
    tenant_client: &mut etcd_client::Client,
    tenant_uid: &str,
    nonce: &str,
    tenant_inside_key: &str,
) -> Result<(), TenantError> {
    let key = populated_foreign_lease_probe_key(tenant_uid, nonce);
    let value = format!("foreign-lease-integrity-probe:{tenant_uid}:{nonce}");
    let lease = bounded_etcd(
        "grant populated foreign-lease integrity-probe lease",
        admin_client.lease_grant(30, None),
    )
    .await?;
    let lease_id = lease.id();
    if lease_id == 0 {
        return Err(TenantError::Invalid(
            "etcd returned a zero lease ID for the foreign-lease integrity probe".to_string(),
        ));
    }

    let probe_result = async {
        let create = etcd_client::Txn::new()
            .when(vec![etcd_client::Compare::version(
                key.clone(),
                etcd_client::CompareOp::Equal,
                0,
            )])
            .and_then(vec![etcd_client::TxnOp::put(
                key.clone(),
                value.clone(),
                Some(etcd_client::PutOptions::new().with_lease(lease_id)),
            )]);
        let created = bounded_etcd(
            "create unique populated foreign-lease integrity sentinel",
            admin_client.txn(create),
        )
        .await?;
        if !created.succeeded() {
            return Err(TenantError::Invalid(
                "random foreign-lease integrity-probe key already existed; no value was overwritten"
                    .to_string(),
            ));
        }
        let created_revision = created
            .header()
            .map(|header| header.revision())
            .filter(|revision| *revision > 0)
            .ok_or_else(|| {
                TenantError::Invalid(
                    "foreign-lease integrity-probe transaction returned no positive revision"
                        .to_string(),
                )
            })?;

        // Use a nested transaction because older etcd 3.6 patch releases
        // checked the Put key but failed to apply the populated-lease
        // authorization check inside Txn. The key is inside this tenant's
        // prefix, so denial proves the lease's existing foreign key is also
        // part of authorization rather than merely re-testing the Put key.
        let foreign_attach = etcd_client::Txn::new()
            .when(vec![etcd_client::Compare::version(
                tenant_inside_key,
                etcd_client::CompareOp::Equal,
                0,
            )])
            .and_then(vec![etcd_client::TxnOp::put(
                tenant_inside_key,
                value.clone(),
                Some(etcd_client::PutOptions::new().with_lease(lease_id)),
            )]);
        if !expect_permission_denied(
            "attach an inside-prefix key to a populated lease outside the tenant prefix",
            tenant_client.txn(foreign_attach),
        )
        .await?
        {
            return Err(TenantError::Invalid(
                "tenant TLS identity unexpectedly attached its key to a populated lease outside its prefix"
                    .to_string(),
            ));
        }

        if !expect_permission_denied(
            "revoke a populated lease outside the tenant prefix",
            tenant_client.lease_revoke(lease_id),
        )
        .await?
        {
            return Err(TenantError::Invalid(
                "tenant TLS identity unexpectedly revoked a populated lease outside its prefix"
                    .to_string(),
            ));
        }

        let attached = bounded_etcd(
            "verify denied foreign-lease attachment created no tenant key",
            admin_client.get(tenant_inside_key, None),
        )
        .await?;
        if !attached.kvs().is_empty() {
            return Err(TenantError::Invalid(
                "denied foreign-lease attachment unexpectedly created the tenant probe key"
                    .to_string(),
            ));
        }

        let response = bounded_etcd(
            "verify populated foreign-lease integrity sentinel",
            admin_client.get(key.as_str(), None),
        )
        .await?;
        let observed = response.kvs();
        if observed.len() != 1
            || observed[0].key() != key.as_bytes()
            || observed[0].value() != value.as_bytes()
            || observed[0].create_revision() != created_revision
            || observed[0].mod_revision() != created_revision
            || observed[0].version() != 1
            || observed[0].lease() != lease_id
        {
            return Err(TenantError::Invalid(
                "populated foreign-lease integrity sentinel did not survive the denied revocation unchanged"
                    .to_string(),
            ));
        }

        Ok(())
    }
    .await;

    let cleanup_result = bounded_etcd(
        "revoke populated foreign-lease integrity-probe lease",
        admin_client.lease_revoke(lease_id),
    )
    .await;
    match (probe_result, cleanup_result) {
        (Ok(()), Ok(_)) => Ok(()),
        (Ok(()), Err(error)) => Err(error),
        (Err(error), Ok(_)) => Err(error),
        (Err(error), Err(cleanup_error)) => {
            warn!(
                error = %cleanup_error,
                lease_id,
                "foreign-lease integrity probe failed and its short lease could not be revoked immediately"
            );
            Err(error)
        }
    }
}

fn access_probe_nonce() -> String {
    use rand::RngCore;

    let mut bytes = [0_u8; 16];
    rand::thread_rng().fill_bytes(&mut bytes);
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn populated_foreign_lease_probe_key(tenant_uid: &str, nonce: &str) -> String {
    format!("/.etcdetcetc-controller-smoke/lease-guard/{tenant_uid}/{nonce}")
}

fn accepts_shared_availability_risk(tenant: &EtcdTenant) -> bool {
    tenant
        .annotations()
        .get(SHARED_AVAILABILITY_RISK_ANNOTATION)
        .is_some_and(|value| value == SHARED_AVAILABILITY_RISK_ACCEPTANCE)
}

fn access_probe_keys(
    identity: &TenantExternalIdentity,
    tenant_uid: &str,
    nonce: &str,
) -> (String, String) {
    (
        format!("{}.etcdetcetc-controller-smoke/{nonce}", identity.prefix),
        format!("/.etcdetcetc-controller-smoke/{tenant_uid}/{nonce}"),
    )
}

async fn bounded_etcd<T, F>(operation: &str, future: F) -> Result<T, TenantError>
where
    F: Future<Output = Result<T, etcd_client::Error>>,
{
    tokio::time::timeout(Duration::from_secs(5), future)
        .await
        .map_err(|_| TenantError::Invalid(format!("timed out during etcd probe: {operation}")))?
        .map_err(TenantError::from)
}

async fn expect_permission_denied<T, F>(operation: &str, future: F) -> Result<bool, TenantError>
where
    F: Future<Output = Result<T, etcd_client::Error>>,
{
    let result = tokio::time::timeout(Duration::from_secs(5), future)
        .await
        .map_err(|_| TenantError::Invalid(format!("timed out during etcd probe: {operation}")))?;
    match result {
        Ok(_) => Ok(false),
        Err(etcd_client::Error::GRpcStatus(status))
            if status.code() == tonic::Code::PermissionDenied =>
        {
            Ok(true)
        }
        Err(err) => Err(err.into()),
    }
}

fn build_certificate_manifest(
    certificate_name: &str,
    staging_secret_name: &str,
    identity: &TenantExternalIdentity,
    issuer_ref: &crate::crd::LocalIssuerReference,
    labels: &BTreeMap<String, String>,
    cluster_owner: k8s_openapi::apimachinery::pkg::apis::meta::v1::OwnerReference,
) -> serde_json::Value {
    serde_json::json!({
        "apiVersion": "cert-manager.io/v1",
        "kind": "Certificate",
        "metadata": {
            "name": certificate_name,
            "namespace": &identity.cluster_ref.namespace,
            "labels": &labels,
            "ownerReferences": [cluster_owner],
        },
        "spec": {
            "commonName": &identity.etcd_user,
            "duration": "24h",
            "issuerRef": {
                "name": &issuer_ref.name,
                "kind": issuer_ref.kind.as_str(),
                "group": "cert-manager.io",
            },
            "privateKey": {
                "algorithm": "ECDSA",
                "rotationPolicy": "Always",
            },
            "renewBefore": "8h",
            "secretName": staging_secret_name,
            "secretTemplate": {
                "labels": &labels,
            },
            "usages": ["digital signature", "client auth"],
        },
    })
}

async fn read_physical_server_ca(
    client: &Client,
    cluster: &EtcdCluster,
) -> Result<ByteString, TenantError> {
    let namespace = cluster
        .namespace()
        .ok_or_else(|| TenantError::Invalid(format!("{} has no namespace", cluster.name_any())))?;
    if let Some(reference) = &cluster.spec.server_ca_config_map_ref {
        let configmaps = Api::<ConfigMap>::namespaced(client.clone(), &namespace);
        let configmap = configmaps.get(&reference.name).await?;
        let value = configmap
            .data
            .as_ref()
            .and_then(|data| data.get(&reference.key))
            .map(|value| ByteString(value.as_bytes().to_vec()))
            .or_else(|| {
                configmap
                    .binary_data
                    .as_ref()
                    .and_then(|data| data.get(&reference.key))
                    .cloned()
            })
            .ok_or_else(|| {
                TenantError::Invalid(format!(
                    "physical server CA ConfigMap {}/{} is missing key {}",
                    namespace, reference.name, reference.key
                ))
            })?;
        if value.0.is_empty() {
            return Err(TenantError::Invalid(format!(
                "physical server CA ConfigMap {}/{} key {} is empty",
                namespace, reference.name, reference.key
            )));
        }
        return Ok(value);
    }

    let secrets = Api::<Secret>::namespaced(client.clone(), &namespace);
    let auth_secret = secrets.get(&cluster.spec.auth_secret_ref.name).await?;
    let value = auth_secret
        .data
        .as_ref()
        .and_then(|data| data.get("ca.crt"))
        .cloned()
        .ok_or_else(|| {
            TenantError::Invalid(format!(
                "EtcdCluster auth Secret {}/{} is missing fallback ca.crt",
                namespace, cluster.spec.auth_secret_ref.name
            ))
        })?;
    if value.0.is_empty() {
        return Err(TenantError::Invalid(format!(
            "EtcdCluster auth Secret {}/{} fallback ca.crt is empty",
            namespace, cluster.spec.auth_secret_ref.name
        )));
    }
    Ok(value)
}

fn certificate_api(client: Client, namespace: &str) -> Api<DynamicObject> {
    let gvk = GroupVersionKind::gvk("cert-manager.io", "v1", "Certificate");
    let resource = ApiResource::from_gvk_with_plural(&gvk, "certificates");
    Api::<DynamicObject>::namespaced_with(client, namespace, &resource)
}

fn certificate_is_current_and_ready(certificate: &DynamicObject) -> bool {
    let Some(generation) = certificate.metadata.generation else {
        return false;
    };
    certificate
        .data
        .pointer("/status/conditions")
        .and_then(serde_json::Value::as_array)
        .is_some_and(|conditions| {
            conditions.iter().any(|condition| {
                condition.get("type").and_then(serde_json::Value::as_str) == Some("Ready")
                    && condition.get("status").and_then(serde_json::Value::as_str) == Some("True")
                    && condition
                        .get("observedGeneration")
                        .and_then(serde_json::Value::as_i64)
                        == Some(generation)
            })
        })
}

fn validate_tls_leaf_for_ready(tls_crt: &[u8], now_unix_seconds: i64) -> Result<(), TenantError> {
    // cert-manager writes the leaf first in tls.crt, followed by any issuer
    // chain. Parse that first PEM block rather than trusting Certificate
    // status timestamps, which can be stale relative to Secret material.
    let (_, pem) = parse_x509_pem(tls_crt).map_err(|error| {
        TenantError::Invalid(format!(
            "tenant TLS Secret tls.crt does not contain a parseable PEM block: {error}"
        ))
    })?;
    if pem.label != "CERTIFICATE" {
        return Err(TenantError::Invalid(format!(
            "tenant TLS Secret tls.crt begins with PEM block {:?}, not CERTIFICATE",
            pem.label
        )));
    }
    let (trailing_der, leaf) = parse_x509_certificate(&pem.contents).map_err(|error| {
        TenantError::Invalid(format!(
            "tenant TLS Secret tls.crt leaf is not a parseable X.509 certificate: {error}"
        ))
    })?;
    if !trailing_der.is_empty() {
        return Err(TenantError::Invalid(
            "tenant TLS Secret tls.crt leaf has trailing data inside its PEM block".to_string(),
        ));
    }

    let not_before = leaf.validity().not_before.timestamp();
    if now_unix_seconds < not_before {
        return Err(TenantError::Invalid(format!(
            "tenant TLS Secret tls.crt leaf is not valid yet (notBefore={not_before}, now={now_unix_seconds})"
        )));
    }

    let required_through = now_unix_seconds
        .checked_add(TLS_CERTIFICATE_READY_MIN_VALIDITY.as_secs() as i64)
        .ok_or_else(|| {
            TenantError::Invalid(
                "current time overflowed while checking tenant TLS certificate validity"
                    .to_string(),
            )
        })?;
    let not_after = leaf.validity().not_after.timestamp();
    if not_after <= required_through {
        return Err(TenantError::Invalid(format!(
            "tenant TLS Secret tls.crt leaf expires too soon for Ready: notAfter={not_after}, requiredAfter={required_through} (minimum remaining validity is {} seconds)",
            TLS_CERTIFICATE_READY_MIN_VALIDITY.as_secs()
        )));
    }

    Ok(())
}

fn compose_tls_secret_data(
    staging: &Secret,
    physical_server_ca: &ByteString,
    tenant_name: &str,
) -> Result<BTreeMap<String, ByteString>, TenantError> {
    let staging_data = staging.data.as_ref().ok_or_else(|| {
        TenantError::Invalid("cert-manager staging Secret has no data".to_string())
    })?;
    let tls_crt = staging_data
        .get("tls.crt")
        .filter(|value| !value.0.is_empty())
        .ok_or_else(|| {
            TenantError::Invalid(
                "cert-manager staging Secret is missing non-empty tls.crt".to_string(),
            )
        })?;
    let tls_key = staging_data
        .get("tls.key")
        .filter(|value| !value.0.is_empty())
        .ok_or_else(|| {
            TenantError::Invalid(
                "cert-manager staging Secret is missing non-empty tls.key".to_string(),
            )
        })?;
    if physical_server_ca.0.is_empty() {
        return Err(TenantError::Invalid(
            "physical etcd server ca.crt is empty".to_string(),
        ));
    }

    let mut data = BTreeMap::new();
    data.insert("tls.crt".to_string(), tls_crt.clone());
    data.insert("tls.key".to_string(), tls_key.clone());
    data.insert("ca.crt".to_string(), physical_server_ca.clone());
    data.insert(
        "username".to_string(),
        ByteString(tenant_name.as_bytes().to_vec()),
    );
    Ok(data)
}

async fn ensure_tenant_configmap(
    client: &Client,
    tenant: &EtcdTenant,
    tenant_namespace: &str,
    cluster: &EtcdCluster,
    identity: &TenantExternalIdentity,
    physical_server_ca: &ByteString,
    client_certificate_revision: Option<&str>,
) -> Result<(), TenantError> {
    let cluster_uid = cluster.uid().ok_or_else(|| {
        TenantError::Invalid(format!(
            "EtcdCluster {}/{} has no metadata.uid",
            cluster.namespace().unwrap_or_default(),
            cluster.name_any()
        ))
    })?;
    if cluster.name_any() != identity.cluster_ref.name
        || cluster.namespace().as_deref() != Some(identity.cluster_ref.namespace.as_str())
        || cluster_uid != identity.cluster_ref.uid
    {
        return Err(TenantError::Invalid(
            "cannot publish tenant ConfigMap from an EtcdCluster other than the pinned identity"
                .to_string(),
        ));
    }
    let ca_crt = String::from_utf8(physical_server_ca.0.clone()).map_err(|_| {
        TenantError::Invalid("physical etcd server ca.crt is not valid UTF-8".to_string())
    })?;

    let owner_reference = tenant_owner_reference(tenant)?;
    // Build the handoff directly from the exact cluster/CA snapshot used by
    // the fresh admin proof and stable Secret publication. The independently
    // reconciled EtcdCluster ConfigMap is not a safe source during CA rotation.
    let mut data = BTreeMap::from([
        ("endpoints".to_string(), cluster.spec.endpoints.join(",")),
        ("ca.crt".to_string(), ca_crt),
    ]);
    data.insert("prefix".to_string(), identity.prefix.clone());
    if identity.credential_mode == TenantCredentialMode::Tls {
        let client_certificate_revision = client_certificate_revision.ok_or_else(|| {
            TenantError::Invalid(
                "TLS tenant has no published client certificate revision".to_string(),
            )
        })?;
        data.insert(
            "values.yaml".to_string(),
            build_control_plane_etcd_values(
                &cluster.spec.endpoints,
                physical_server_ca,
                identity,
                client_certificate_revision,
            )?,
        );
    }
    let target = ConfigMap {
        metadata: ObjectMeta {
            name: Some(identity.config_map_name.clone()),
            namespace: Some(tenant_namespace.to_string()),
            owner_references: Some(vec![owner_reference]),
            labels: Some(BTreeMap::from([(
                FLUX_WATCH_LABEL.to_string(),
                "Enabled".to_string(),
            )])),
            ..ObjectMeta::default()
        },
        data: Some(data),
        ..ConfigMap::default()
    };

    let target_configmaps = Api::<ConfigMap>::namespaced(client.clone(), tenant_namespace);
    ensure_configmap_is_tenant_owned(&target_configmaps, &identity.config_map_name, tenant).await?;
    target_configmaps
        .patch(
            &identity.config_map_name,
            &PatchParams::apply(FIELD_MANAGER),
            &Patch::Apply(&target),
        )
        .await?;

    Ok(())
}

fn build_control_plane_etcd_values(
    endpoints: &[String],
    physical_server_ca: &ByteString,
    identity: &TenantExternalIdentity,
    client_certificate_revision: &str,
) -> Result<String, TenantError> {
    if endpoints.is_empty() || endpoints.iter().any(String::is_empty) {
        return Err(TenantError::Invalid(
            "EtcdCluster endpoints must be non-empty strings".to_string(),
        ));
    }
    if physical_server_ca.0.is_empty() {
        return Err(TenantError::Invalid(
            "physical etcd server ca.crt is empty".to_string(),
        ));
    }
    let trust_revision = sha256_revision(&physical_server_ca.0);
    let values = serde_json::json!({
        "etcd": {
            "clientCertificateRevision": client_certificate_revision,
            "clientSecret": {
                "create": false,
                "name": identity.output_secret_name,
            },
            "endpoints": endpoints,
            "externalSecretRevisionsRequired": true,
            "prefix": identity.prefix,
            "serverCATrustRevision": trust_revision,
        }
    });
    Ok(serde_json::to_string_pretty(&values)?)
}

fn sha256_revision(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}

fn published_client_certificate_revision(secret: &Secret) -> Result<String, TenantError> {
    let tls_crt = secret
        .data
        .as_ref()
        .and_then(|data| data.get("tls.crt"))
        .filter(|value| !value.0.is_empty())
        .ok_or_else(|| {
            TenantError::Invalid("published tenant Secret is missing non-empty tls.crt".to_string())
        })?;
    Ok(sha256_revision(&tls_crt.0))
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ArtifactOwnership {
    Owned,
    Foreign,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ArtifactCleanupMode {
    /// Authorization-driven deprovisioning keeps the Tenant live and therefore
    /// requires every exact-name artifact to remain controller-owned before the
    /// namespace can later be reauthorized safely.
    RequireOwned,
    /// Tenant deletion permanently retires the pinned identity. Once exact etcd
    /// access has been revoked, retain a foreign colliding object rather than
    /// deleting it or allowing it to wedge finalizer removal.
    RetainForeign,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ArtifactCleanupDisposition {
    DeleteOwned,
    RejectForeign,
    RetainForeign,
}

fn artifact_cleanup_disposition(
    ownership: ArtifactOwnership,
    mode: ArtifactCleanupMode,
) -> ArtifactCleanupDisposition {
    match (ownership, mode) {
        (ArtifactOwnership::Owned, _) => ArtifactCleanupDisposition::DeleteOwned,
        (ArtifactOwnership::Foreign, ArtifactCleanupMode::RequireOwned) => {
            ArtifactCleanupDisposition::RejectForeign
        }
        (ArtifactOwnership::Foreign, ArtifactCleanupMode::RetainForeign) => {
            ArtifactCleanupDisposition::RetainForeign
        }
    }
}

fn tenant_artifact_ownership(
    metadata: &ObjectMeta,
    tenant: &EtcdTenant,
) -> Result<ArtifactOwnership, TenantError> {
    let tenant_uid = tenant.uid().ok_or_else(|| {
        TenantError::Invalid(format!("{} has no metadata.uid", tenant.name_any()))
    })?;
    let owned = metadata.owner_references.as_ref().is_some_and(|owners| {
        owners.iter().any(|owner| {
            owner.uid == tenant_uid
                && owner.kind == "EtcdTenant"
                && owner.api_version == "etcdetcetc.samcday.com/v1alpha1"
                && owner.controller == Some(true)
        })
    });
    Ok(if owned {
        ArtifactOwnership::Owned
    } else {
        ArtifactOwnership::Foreign
    })
}

fn labeled_artifact_ownership(metadata: &ObjectMeta, tenant_uid: &str) -> ArtifactOwnership {
    let actual = metadata
        .labels
        .as_ref()
        .and_then(|labels| labels.get(TENANT_UID_LABEL));
    if actual.map(String::as_str) == Some(tenant_uid) {
        ArtifactOwnership::Owned
    } else {
        ArtifactOwnership::Foreign
    }
}

fn ensure_owned_by_tenant(
    metadata: &ObjectMeta,
    name: &str,
    kind: &str,
    tenant: &EtcdTenant,
) -> Result<(), TenantError> {
    let tenant_uid = tenant.uid().ok_or_else(|| {
        TenantError::Invalid(format!("{} has no metadata.uid", tenant.name_any()))
    })?;
    if tenant_artifact_ownership(metadata, tenant)? != ArtifactOwnership::Owned {
        return Err(TenantError::Invalid(format!(
            "refusing to manage {kind} {name:?}: it is not controlled by EtcdTenant UID {tenant_uid}"
        )));
    }
    Ok(())
}

fn ensure_labeled_for_tenant(
    metadata: &ObjectMeta,
    name: &str,
    kind: &str,
    tenant_uid: &str,
) -> Result<(), TenantError> {
    let actual = metadata
        .labels
        .as_ref()
        .and_then(|labels| labels.get(TENANT_UID_LABEL));
    if labeled_artifact_ownership(metadata, tenant_uid) != ArtifactOwnership::Owned {
        return Err(TenantError::Invalid(format!(
            "refusing to manage {kind} {name:?}: label {TENANT_UID_LABEL} is {:?}, expected {tenant_uid:?}",
            actual
        )));
    }
    Ok(())
}

async fn ensure_secret_is_tenant_owned(
    secrets: &Api<Secret>,
    name: &str,
    tenant: &EtcdTenant,
) -> Result<(), TenantError> {
    match secrets.get(name).await {
        Ok(secret) => ensure_owned_by_tenant(&secret.metadata, name, "Secret", tenant),
        Err(kube::Error::Api(ae)) if ae.code == 404 => Ok(()),
        Err(err) => Err(err.into()),
    }
}

async fn get_tenant_owned_secret(
    secrets: &Api<Secret>,
    name: &str,
    tenant: &EtcdTenant,
) -> Result<Option<Secret>, TenantError> {
    match secrets.get(name).await {
        Ok(secret) => {
            ensure_owned_by_tenant(&secret.metadata, name, "Secret", tenant)?;
            Ok(Some(secret))
        }
        Err(kube::Error::Api(ae)) if ae.code == 404 => Ok(None),
        Err(err) => Err(err.into()),
    }
}

fn tenant_is_ready(tenant: &EtcdTenant) -> bool {
    tenant.status.as_ref().is_some_and(|status| {
        status.conditions.iter().any(|condition| {
            condition.type_ == "Ready" && condition.status == ConditionStatus::True
        })
    })
}

async fn ensure_configmap_is_tenant_owned(
    configmaps: &Api<ConfigMap>,
    name: &str,
    tenant: &EtcdTenant,
) -> Result<(), TenantError> {
    match configmaps.get(name).await {
        Ok(configmap) => ensure_owned_by_tenant(&configmap.metadata, name, "ConfigMap", tenant),
        Err(kube::Error::Api(ae)) if ae.code == 404 => Ok(()),
        Err(err) => Err(err.into()),
    }
}

async fn cleanup_kubernetes_artifacts(
    client: &Client,
    tenant: &EtcdTenant,
    identity: &TenantExternalIdentity,
    tenant_namespace: &str,
    tenant_namespace_terminating: bool,
    mode: ArtifactCleanupMode,
) -> Result<bool, TenantError> {
    let mut all_absent = true;
    let tenant_uid = tenant.uid().ok_or_else(|| {
        TenantError::Invalid(format!("{} has no metadata.uid", tenant.name_any()))
    })?;

    if !tenant_namespace_terminating {
        let tenant_secrets = Api::<Secret>::namespaced(client.clone(), tenant_namespace);
        all_absent &= delete_tenant_secret_if_present(
            &tenant_secrets,
            &identity.output_secret_name,
            tenant,
            mode,
        )
        .await?;
        if let Some(password_secret_name) = &identity.password_secret_name {
            all_absent &= delete_tenant_secret_if_present(
                &tenant_secrets,
                password_secret_name,
                tenant,
                mode,
            )
            .await?;
        }

        let tenant_configmaps = Api::<ConfigMap>::namespaced(client.clone(), tenant_namespace);
        all_absent &= delete_tenant_configmap_if_present(
            &tenant_configmaps,
            &identity.config_map_name,
            tenant,
            mode,
        )
        .await?;
    }

    // If Certificate staging shares a terminating tenant namespace, namespace
    // GC owns those artifacts too. Otherwise the controller must remove them
    // explicitly because their EtcdCluster owner remains live.
    if !tenant_namespace_terminating || identity.cluster_ref.namespace != tenant_namespace {
        if let Some(certificate_name) = &identity.certificate_name {
            let certificates = certificate_api(client.clone(), &identity.cluster_ref.namespace);
            all_absent &= delete_labeled_certificate_if_present(
                &certificates,
                certificate_name,
                &tenant_uid,
                mode,
            )
            .await?;
        }
        if let Some(staging_secret_name) = &identity.staging_secret_name {
            let staging_secrets =
                Api::<Secret>::namespaced(client.clone(), &identity.cluster_ref.namespace);
            all_absent &= delete_labeled_secret_if_present(
                &staging_secrets,
                staging_secret_name,
                &tenant_uid,
                mode,
            )
            .await?;
        }
    }

    Ok(all_absent)
}

async fn delete_tenant_secret_if_present(
    secrets: &Api<Secret>,
    name: &str,
    tenant: &EtcdTenant,
    mode: ArtifactCleanupMode,
) -> Result<bool, TenantError> {
    match secrets.get(name).await {
        Ok(secret) => {
            match artifact_cleanup_disposition(
                tenant_artifact_ownership(&secret.metadata, tenant)?,
                mode,
            ) {
                ArtifactCleanupDisposition::DeleteOwned => {
                    delete_secret(secrets, name, &secret.metadata).await?;
                    Ok(false)
                }
                ArtifactCleanupDisposition::RejectForeign => {
                    ensure_owned_by_tenant(&secret.metadata, name, "Secret", tenant)?;
                    unreachable!("foreign ownership must have been rejected")
                }
                ArtifactCleanupDisposition::RetainForeign => {
                    warn!(
                        name,
                        kind = "Secret",
                        "retaining foreign exact-name artifact after verified etcd access revocation"
                    );
                    Ok(true)
                }
            }
        }
        Err(kube::Error::Api(ae)) if ae.code == 404 => Ok(true),
        Err(err) => Err(err.into()),
    }
}

async fn delete_labeled_secret_if_present(
    secrets: &Api<Secret>,
    name: &str,
    tenant_uid: &str,
    mode: ArtifactCleanupMode,
) -> Result<bool, TenantError> {
    match secrets.get(name).await {
        Ok(secret) => {
            match artifact_cleanup_disposition(
                labeled_artifact_ownership(&secret.metadata, tenant_uid),
                mode,
            ) {
                ArtifactCleanupDisposition::DeleteOwned => {
                    delete_secret(secrets, name, &secret.metadata).await?;
                    Ok(false)
                }
                ArtifactCleanupDisposition::RejectForeign => {
                    ensure_labeled_for_tenant(
                        &secret.metadata,
                        name,
                        "staging Secret",
                        tenant_uid,
                    )?;
                    unreachable!("foreign ownership must have been rejected")
                }
                ArtifactCleanupDisposition::RetainForeign => {
                    warn!(
                        name,
                        kind = "staging Secret",
                        "retaining foreign exact-name artifact after verified etcd access revocation"
                    );
                    Ok(true)
                }
            }
        }
        Err(kube::Error::Api(ae)) if ae.code == 404 => Ok(true),
        Err(err) => Err(err.into()),
    }
}

async fn delete_secret(
    secrets: &Api<Secret>,
    name: &str,
    metadata: &ObjectMeta,
) -> Result<(), TenantError> {
    match secrets
        .delete(name, &delete_params_for(metadata, name, "Secret")?)
        .await
    {
        Ok(_) => Ok(()),
        Err(kube::Error::Api(ae)) if ae.code == 404 => Ok(()),
        Err(err) => Err(err.into()),
    }
}

async fn delete_tenant_configmap_if_present(
    configmaps: &Api<ConfigMap>,
    name: &str,
    tenant: &EtcdTenant,
    mode: ArtifactCleanupMode,
) -> Result<bool, TenantError> {
    match configmaps.get(name).await {
        Ok(configmap) => {
            match artifact_cleanup_disposition(
                tenant_artifact_ownership(&configmap.metadata, tenant)?,
                mode,
            ) {
                ArtifactCleanupDisposition::DeleteOwned => match configmaps
                    .delete(
                        name,
                        &delete_params_for(&configmap.metadata, name, "ConfigMap")?,
                    )
                    .await
                {
                    Ok(_) => Ok(false),
                    Err(kube::Error::Api(ae)) if ae.code == 404 => Ok(true),
                    Err(err) => Err(err.into()),
                },
                ArtifactCleanupDisposition::RejectForeign => {
                    ensure_owned_by_tenant(&configmap.metadata, name, "ConfigMap", tenant)?;
                    unreachable!("foreign ownership must have been rejected")
                }
                ArtifactCleanupDisposition::RetainForeign => {
                    warn!(
                        name,
                        kind = "ConfigMap",
                        "retaining foreign exact-name artifact after verified etcd access revocation"
                    );
                    Ok(true)
                }
            }
        }
        Err(kube::Error::Api(ae)) if ae.code == 404 => Ok(true),
        Err(err) => Err(err.into()),
    }
}

async fn delete_labeled_certificate_if_present(
    certificates: &Api<DynamicObject>,
    name: &str,
    tenant_uid: &str,
    mode: ArtifactCleanupMode,
) -> Result<bool, TenantError> {
    match certificates.get(name).await {
        Ok(certificate) => {
            match artifact_cleanup_disposition(
                labeled_artifact_ownership(&certificate.metadata, tenant_uid),
                mode,
            ) {
                ArtifactCleanupDisposition::DeleteOwned => match certificates
                    .delete(
                        name,
                        &delete_params_for(&certificate.metadata, name, "Certificate")?,
                    )
                    .await
                {
                    Ok(_) => Ok(false),
                    Err(kube::Error::Api(ae)) if ae.code == 404 => Ok(true),
                    Err(err) => Err(err.into()),
                },
                ArtifactCleanupDisposition::RejectForeign => {
                    ensure_labeled_for_tenant(
                        &certificate.metadata,
                        name,
                        "Certificate",
                        tenant_uid,
                    )?;
                    unreachable!("foreign ownership must have been rejected")
                }
                ArtifactCleanupDisposition::RetainForeign => {
                    warn!(
                        name,
                        kind = "Certificate",
                        "retaining foreign exact-name artifact after verified etcd access revocation"
                    );
                    Ok(true)
                }
            }
        }
        Err(kube::Error::Api(ae)) if ae.code == 404 => Ok(true),
        Err(err) => Err(err.into()),
    }
}

fn delete_params_for(
    metadata: &ObjectMeta,
    name: &str,
    kind: &str,
) -> Result<DeleteParams, TenantError> {
    let uid = metadata
        .uid
        .clone()
        .ok_or_else(|| TenantError::Invalid(format!("{kind} {name:?} has no metadata.uid")))?;
    let resource_version = metadata.resource_version.clone().ok_or_else(|| {
        TenantError::Invalid(format!("{kind} {name:?} has no metadata.resourceVersion"))
    })?;
    Ok(DeleteParams {
        preconditions: Some(Preconditions {
            uid: Some(uid),
            resource_version: Some(resource_version),
        }),
        ..DeleteParams::default()
    })
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
        tenant.meta().generation,
        existing_conditions,
    );
    if existing_conditions.len() == 1 && existing_conditions[0] == condition {
        return Ok(());
    }
    let mut desired = tenant.status.clone().unwrap_or(EtcdTenantStatus {
        conditions: Vec::new(),
        external_identity: None,
        external_access_state: None,
    });
    desired.conditions = vec![condition];
    replace_tenant_status_snapshot(&api, tenant, namespace, name, desired).await?;

    Ok(())
}

async fn pin_external_identity(
    client: &Client,
    tenant: &EtcdTenant,
    namespace: &str,
    name: &str,
    identity: &TenantExternalIdentity,
) -> Result<(), TenantError> {
    let api = Api::<EtcdTenant>::namespaced(client.clone(), namespace);
    let existing_conditions = tenant
        .status
        .as_ref()
        .map(|status| status.conditions.as_slice())
        .unwrap_or(&[]);
    // New objects are pinned before this controller adds its finalizer, so
    // Planned proves no etcd mutation has started. A pre-existing finalizer
    // may belong to the legacy controller and therefore means external state
    // is unknown until exact RBAC reconciliation succeeds.
    let (external_access_state, reason, message) = if has_finalizer(tenant) {
        (
            TenantExternalAccessState::LegacyUnverified,
            "LegacyExternalAccessUnverified",
            "pre-existing finalizer may represent legacy external access; exact reconciliation is required",
        )
    } else {
        (
            TenantExternalAccessState::Planned,
            "IdentityPinned",
            "external identity pinned; provisioning has not started",
        )
    };
    let condition = crate::crd::ready_condition_with_existing(
        false,
        reason,
        message,
        tenant.meta().generation,
        existing_conditions,
    );
    let mut desired = tenant.status.clone().unwrap_or(EtcdTenantStatus {
        conditions: Vec::new(),
        external_identity: None,
        external_access_state: None,
    });
    if desired.external_identity.is_some() || desired.external_access_state.is_some() {
        return Err(TenantError::Invalid(
            "refusing to overwrite an existing external identity or access-state pin".to_string(),
        ));
    }
    desired.conditions = vec![condition];
    desired.external_identity = Some(identity.clone());
    desired.external_access_state = Some(external_access_state);
    replace_tenant_status_snapshot(&api, tenant, namespace, name, desired).await?;
    Ok(())
}

async fn mark_external_access_state(
    client: &Client,
    tenant: &EtcdTenant,
    namespace: &str,
    name: &str,
    state: TenantExternalAccessState,
) -> Result<(), TenantError> {
    let api = Api::<EtcdTenant>::namespaced(client.clone(), namespace);
    let current_state = tenant
        .status
        .as_ref()
        .and_then(|status| status.external_access_state);
    if !external_access_transition_allowed(current_state, state) {
        return Err(TenantError::Invalid(format!(
            "illegal external access-state transition from {current_state:?} to {state:?}"
        )));
    }
    let existing_conditions = tenant
        .status
        .as_ref()
        .map(|status| status.conditions.as_slice())
        .unwrap_or(&[]);
    let (reason, message) = match state {
        TenantExternalAccessState::Planned => (
            "IdentityPinned",
            "external identity pinned; provisioning has not started",
        ),
        TenantExternalAccessState::LegacyUnverified => (
            "LegacyExternalAccessUnverified",
            "pinned legacy external access requires exact reconciliation before ownership is proven",
        ),
        TenantExternalAccessState::Provisioning => (
            "ExternalAccessProvisioning",
            "external access reconciliation may have started",
        ),
        TenantExternalAccessState::Provisioned => (
            "ExternalAccessProvisioned",
            "external access reconciled; credential publication has not completed",
        ),
        TenantExternalAccessState::Deprovisioning => (
            "ExternalAccessDeprovisioning",
            "authorization-driven revocation and owned-artifact cleanup must complete before reprovisioning",
        ),
        TenantExternalAccessState::Deprovisioned => (
            "ExternalAccessDeprovisioned",
            "exact external access is revoked and owned credential artifacts are absent",
        ),
    };
    let condition = crate::crd::ready_condition_with_existing(
        false,
        reason,
        message,
        tenant.meta().generation,
        existing_conditions,
    );
    let mut desired = tenant.status.clone().unwrap_or(EtcdTenantStatus {
        conditions: Vec::new(),
        external_identity: None,
        external_access_state: None,
    });
    desired.conditions = vec![condition];
    desired.external_access_state = Some(state);
    replace_tenant_status_snapshot(&api, tenant, namespace, name, desired).await?;
    Ok(())
}

fn external_access_transition_allowed(
    current: Option<TenantExternalAccessState>,
    desired: TenantExternalAccessState,
) -> bool {
    current == Some(desired)
        || matches!(
            (current, desired),
            (None, TenantExternalAccessState::LegacyUnverified)
                | (
                    Some(TenantExternalAccessState::Deprovisioned),
                    TenantExternalAccessState::Planned
                )
                | (
                    Some(TenantExternalAccessState::Planned),
                    TenantExternalAccessState::Provisioning
                )
                | (
                    Some(TenantExternalAccessState::LegacyUnverified),
                    TenantExternalAccessState::Provisioned
                )
                | (
                    Some(TenantExternalAccessState::Provisioning),
                    TenantExternalAccessState::Provisioned
                )
                | (
                    Some(TenantExternalAccessState::Provisioning),
                    TenantExternalAccessState::Deprovisioning
                )
                | (
                    Some(TenantExternalAccessState::Provisioned),
                    TenantExternalAccessState::Deprovisioning
                )
                | (
                    Some(TenantExternalAccessState::Deprovisioning),
                    TenantExternalAccessState::Deprovisioned
                )
        )
}

async fn replace_tenant_status_snapshot(
    api: &Api<EtcdTenant>,
    tenant: &EtcdTenant,
    namespace: &str,
    name: &str,
    desired: EtcdTenantStatus,
) -> Result<(), TenantError> {
    let replacement = tenant_status_replacement_snapshot(tenant, namespace, name, desired)?;
    api.replace_status(
        name,
        &PostParams::default(),
        serde_json::to_vec(&replacement)?,
    )
    .await?;
    Ok(())
}

fn tenant_status_replacement_snapshot(
    tenant: &EtcdTenant,
    namespace: &str,
    name: &str,
    desired: EtcdTenantStatus,
) -> Result<EtcdTenant, TenantError> {
    if tenant.namespace().as_deref() != Some(namespace) || tenant.name_any() != name {
        return Err(TenantError::Invalid(
            "status replacement target differs from the reconciled EtcdTenant snapshot".to_string(),
        ));
    }
    tenant.uid().ok_or_else(|| {
        TenantError::Invalid(format!("EtcdTenant {namespace}/{name} has no metadata.uid"))
    })?;
    tenant.meta().resource_version.as_deref().ok_or_else(|| {
        TenantError::Invalid(format!(
            "EtcdTenant {namespace}/{name} has no metadata.resourceVersion"
        ))
    })?;

    let mut replacement = tenant.clone();
    replacement.status = Some(desired);
    Ok(replacement)
}

async fn ensure_finalizer(
    client: &Client,
    tenant: &EtcdTenant,
    namespace: &str,
) -> Result<bool, TenantError> {
    if has_finalizer(tenant) {
        return Ok(false);
    }

    let resource_version = tenant.meta().resource_version.as_deref().ok_or_else(|| {
        TenantError::Invalid(format!(
            "{} has no metadata.resourceVersion",
            tenant.name_any()
        ))
    })?;

    let api = Api::<EtcdTenant>::namespaced(client.clone(), namespace);
    let patch: json_patch::Patch = if tenant.meta().finalizers.is_some() {
        serde_json::from_value(serde_json::json!([
            { "op": "test", "path": "/metadata/resourceVersion", "value": resource_version },
            { "op": "add", "path": "/metadata/finalizers/-", "value": TENANT_FINALIZER }
        ]))
        .unwrap()
    } else {
        serde_json::from_value(serde_json::json!([
            { "op": "test", "path": "/metadata/resourceVersion", "value": resource_version },
            { "op": "add", "path": "/metadata/finalizers", "value": [TENANT_FINALIZER] }
        ]))
        .unwrap()
    };

    api.patch(
        &tenant.name_any(),
        &PatchParams::default(),
        &Patch::Json::<()>(patch),
    )
    .await?;

    Ok(true)
}

async fn remove_finalizer(
    client: &Client,
    tenant: &EtcdTenant,
    namespace: &str,
) -> Result<(), TenantError> {
    let Some(index) = tenant
        .meta()
        .finalizers
        .as_ref()
        .and_then(|finalizers| finalizers.iter().position(|f| f == TENANT_FINALIZER))
    else {
        return Ok(());
    };

    let api = Api::<EtcdTenant>::namespaced(client.clone(), namespace);
    let path = format!("/metadata/finalizers/{index}");
    let resource_version = tenant.meta().resource_version.as_deref().ok_or_else(|| {
        TenantError::Invalid(format!(
            "{} has no metadata.resourceVersion",
            tenant.name_any()
        ))
    })?;
    let patch: json_patch::Patch = serde_json::from_value(serde_json::json!([
        { "op": "test", "path": "/metadata/resourceVersion", "value": resource_version },
        { "op": "test", "path": &path, "value": TENANT_FINALIZER },
        { "op": "remove", "path": &path }
    ]))
    .unwrap();

    api.patch(
        &tenant.name_any(),
        &PatchParams::default(),
        &Patch::Json::<()>(patch),
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
    password_secret_name: &str,
) -> Result<String, TenantError> {
    let secrets = Api::<Secret>::namespaced(client.clone(), namespace);

    match secrets.get(password_secret_name).await {
        Ok(secret) => {
            ensure_owned_by_tenant(
                &secret.metadata,
                password_secret_name,
                "password Secret",
                tenant,
            )?;
            let password = secret
                .data
                .as_ref()
                .and_then(|data| data.get("password"))
                .ok_or_else(|| {
                    TenantError::Invalid(format!(
                        "password Secret {namespace}/{password_secret_name} missing required key password"
                    ))
                })?;

            String::from_utf8(password.0.clone()).map_err(|_| {
                TenantError::Invalid(format!(
                    "password Secret {namespace}/{password_secret_name} contains non-UTF-8 password"
                ))
            })
        }
        Err(kube::Error::Api(ae)) if ae.code == 404 => {
            let owner_reference = tenant_owner_reference(tenant)?;

            let password = generate_password();
            let mut data = BTreeMap::new();
            data.insert(
                "password".to_string(),
                ByteString(password.as_bytes().to_vec()),
            );

            let secret = Secret {
                metadata: ObjectMeta {
                    name: Some(password_secret_name.to_string()),
                    namespace: Some(namespace.to_string()),
                    owner_references: Some(vec![owner_reference]),
                    ..ObjectMeta::default()
                },
                data: Some(data),
                type_: Some("Opaque".to_string()),
                ..Secret::default()
            };

            secrets
                .patch(
                    password_secret_name,
                    &PatchParams::apply(FIELD_MANAGER),
                    &Patch::Apply(&secret),
                )
                .await?;
            Ok(password)
        }
        Err(err) => Err(err.into()),
    }
}

fn ignore_not_found<T>(result: Result<T, etcd_client::Error>) -> Result<(), TenantError> {
    match result {
        Ok(_) => Ok(()),
        Err(err) if is_not_found_error(&err) => Ok(()),
        Err(err) => Err(err.into()),
    }
}

fn is_not_found_error(error: &etcd_client::Error) -> bool {
    match error {
        etcd_client::Error::GRpcStatus(status) => {
            // NOT_FOUND (5) is the standard code for missing keys.
            // etcd may also use FAILED_PRECONDITION (9) for missing auth entities
            // (users, roles); we only treat it as "not found" when the message says so.
            let code = status.code() as i32;
            code == 5 || (code == 9 && status.message().to_ascii_lowercase().contains("not found"))
        }
        _ => false,
    }
}

fn is_role_not_granted_error(error: &etcd_client::Error) -> bool {
    matches!(
        error,
        etcd_client::Error::GRpcStatus(status)
            if status.code() == tonic::Code::FailedPrecondition
                && status.message() == "etcdserver: role is not granted to the user"
    )
}

fn is_invalid_credentials_error(error: &etcd_client::Error) -> bool {
    match error {
        etcd_client::Error::GRpcStatus(status) => {
            status.code() == tonic::Code::InvalidArgument
                && status.message()
                    == "etcdserver: authentication failed, invalid user ID or password"
        }
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::crd::{
        ClusterReference, EtcdClusterSpec, EtcdClusterStatus, EtcdTenantSpec, EtcdTenantStatus,
        LocalIssuerReference, LocalSecretReference, TenantTlsConfig,
    };
    use chrono::{TimeZone, Utc};

    const TEST_LEAF_CERTIFICATE: &[u8] = br#"-----BEGIN CERTIFICATE-----
MIIFWzCCBEOgAwIBAgISAyBIAwu7NBD5CTxX8suDCMgFMA0GCSqGSIb3DQEBCwUA
MEoxCzAJBgNVBAYTAlVTMRYwFAYDVQQKEw1MZXQncyBFbmNyeXB0MSMwIQYDVQQD
ExpMZXQncyBFbmNyeXB0IEF1dGhvcml0eSBYMzAeFw0xOTA3MTIxMTEyMzBaFw0x
OTEwMTAxMTEyMzBaMB0xGzAZBgNVBAMTEmxpc3RzLmZvci1vdXIuaW5mbzCCASIw
DQYJKoZIhvcNAQEBBQADggEPADCCAQoCggEBAMVoti34X46DaI2nX24C+aZ2Ofkm
hKbidiXiRTon1MLSMGl1oNW9MyRyYYCzP4j6DNKChJnr8ZnVShh2oZD+yHWP9lpn
XMGkbsUxejRMU9hnaAB50pXRIDAzavkVFCguFlJ8nKkv/Y1Avlw7tc2aZOd3lOZB
Er8gJ8mRDGqqsNU+Z12I6slEstzGMpsq6AewCVw4lMjdWWgugzUrxQTRAsG87on6
gOiQH2cMODN3L7Fq4KOLQIjb3/luQhAQhpdKmEGFLin3c+f5or3thCDuwwDtOU1l
Zf+8t9S8pZPLrZrIs6H2xjXqCRuUY7iRNbO18Ukc6rlDYhBj9LT+cpmBbHECAwEA
AaOCAmYwggJiMA4GA1UdDwEB/wQEAwIFoDAdBgNVHSUEFjAUBggrBgEFBQcDAQYI
KwYBBQUHAwIwDAYDVR0TAQH/BAIwADAdBgNVHQ4EFgQUJj2pvRtl3GloH3He6FX1
ds3X0VEwHwYDVR0jBBgwFoAUqEpqYwR93brm0Tm3pkVl7/Oo7KEwbwYIKwYBBQUH
AQEEYzBhMC4GCCsGAQUFBzABhiJodHRwOi8vb2NzcC5pbnQteDMubGV0c2VuY3J5
cHQub3JnMC8GCCsGAQUFBzAChiNodHRwOi8vY2VydC5pbnQteDMubGV0c2VuY3J5
cHQub3JnLzAdBgNVHREEFjAUghJsaXN0cy5mb3Itb3VyLmluZm8wTAYDVR0gBEUw
QzAIBgZngQwBAgEwNwYLKwYBBAGC3xMBAQEwKDAmBggrBgEFBQcCARYaaHR0cDov
L2Nwcy5sZXRzZW5jcnlwdC5vcmcwggEDBgorBgEEAdZ5AgQCBIH0BIHxAO8AdgAp
PFGWVMg5ZbqqUPxYB9S3b79Yeily3KTDDPTlRUf0eAAAAWvmGV7yAAAEAwBHMEUC
ICQL2Sm14aCMLxX9a9RbySgyBfichMRdbu6QA2Mbrl4eAiEA1vgJ7snqUWCgoqEE
3SEfK3ioMopzWBsPvG6LdCuCMRAAdQBvU3asMfAxGdiZAKRRFf93FRwR2QLBACkG
jbIImjfZEwAAAWvmGV9oAAAEAwBGMEQCIExGqw3Lo0nSCyUuTRf92FgGASwWYji5
UGnXuYnpJrAvAiBw8AWVag8fzZ4ogAhY9EFRNdLrUcBjStipL888vyuxKzANBgkq
hkiG9w0BAQsFAAOCAQEAF8BBLDvSWZg57B6aDtzfUTSGetCYs3k0vJqCJlL+Pz7/
UruCSsojQzp5R6jvvgYQ83MaIdwe2mgt+OCQB5v7ylctyBzBmYIw9nPnxEC7HlcJ
L2K/k5ZjJFRnv4kV1Si8+TIpEAV0ksf39KGKemG8kGi4GXV1v03zSv0p8aCarpuo
SKBJ4qlB0CvmS2MqV4KnzO0O2h0c/ZQ4jg7l53eiN7VPdRMMO1DRw+MaW6I/hEZp
+oZQ7hhKXgKUBvF4IGwyrfyIZ8AeWKG4IP98COgyRbz7qtrAVevRKCM0ZC2t04A2
Fcix40FKEeiE093Aj3cweMYxNLPgwgQP8Xu3kA5QEw==
-----END CERTIFICATE-----
"#;

    fn test_unix(year: i32, month: u32, day: u32, hour: u32, minute: u32, second: u32) -> i64 {
        Utc.with_ymd_and_hms(year, month, day, hour, minute, second)
            .single()
            .unwrap()
            .timestamp()
    }

    fn cluster_fixture() -> EtcdCluster {
        let mut cluster = EtcdCluster::new(
            "physical",
            EtcdClusterSpec {
                endpoints: vec!["https://etcd.example:2379".to_string()],
                auth_secret_ref: LocalSecretReference {
                    name: "admin".to_string(),
                },
                server_ca_config_map_ref: None,
                allowed_namespaces: vec!["*".to_string()],
                tenant_tls: Some(TenantTlsConfig {
                    issuer_ref: LocalIssuerReference {
                        name: "tenant-client".to_string(),
                        kind: crate::crd::IssuerKind::Issuer,
                    },
                }),
            },
        );
        cluster.metadata.namespace = Some("controller".to_string());
        cluster.metadata.uid = Some("11111111-1111-1111-1111-111111111111".to_string());
        cluster.status = Some(EtcdClusterStatus {
            cluster_id: Some("0123456789abcdef".to_string()),
            ..EtcdClusterStatus::default()
        });
        cluster
    }

    fn tenant_fixture(
        namespace: &str,
        name: &str,
        credential_mode: TenantCredentialMode,
    ) -> EtcdTenant {
        let mut tenant = EtcdTenant::new(
            name,
            EtcdTenantSpec {
                cluster_ref: ClusterReference {
                    name: "physical".to_string(),
                    namespace: Some("controller".to_string()),
                },
                secret_name: None,
                credential_mode,
            },
        );
        tenant.metadata.namespace = Some(namespace.to_string());
        tenant.metadata.uid = Some("22222222-2222-2222-2222-222222222222".to_string());
        tenant
    }

    #[test]
    fn cluster_watch_maps_exact_spec_and_pinned_references() {
        let cluster = cluster_fixture();
        let spec_tenant = tenant_fixture("spec-child", "spec", TenantCredentialMode::Tls);

        let mut pinned_tenant = tenant_fixture("pinned-child", "pinned", TenantCredentialMode::Tls);
        pinned_tenant.metadata.uid = Some("33333333-3333-3333-3333-333333333333".to_string());
        let pinned_identity = build_external_identity(&pinned_tenant, &cluster).unwrap();
        pinned_tenant.spec.cluster_ref.name = "corrupt-or-migrated-ref".to_string();
        pinned_tenant.status = Some(EtcdTenantStatus {
            conditions: Vec::new(),
            external_identity: Some(pinned_identity),
            external_access_state: Some(TenantExternalAccessState::Provisioned),
        });

        let mut unrelated =
            tenant_fixture("unrelated-child", "unrelated", TenantCredentialMode::Tls);
        unrelated.spec.cluster_ref.name = "other".to_string();

        let mut mapped = tenants_referencing_cluster(
            vec![
                Arc::new(unrelated),
                Arc::new(pinned_tenant),
                Arc::new(spec_tenant),
            ],
            &cluster,
        )
        .into_iter()
        .map(|reference| (reference.namespace.unwrap_or_default(), reference.name))
        .collect::<Vec<_>>();
        mapped.sort();

        assert_eq!(
            mapped,
            vec![
                ("pinned-child".to_string(), "pinned".to_string()),
                ("spec-child".to_string(), "spec".to_string()),
            ]
        );
    }

    #[test]
    fn external_access_state_graph_rejects_barrier_regressions() {
        use TenantExternalAccessState as State;

        let allowed = [
            (None, State::LegacyUnverified),
            (Some(State::Deprovisioned), State::Planned),
            (Some(State::Planned), State::Provisioning),
            (Some(State::LegacyUnverified), State::Provisioned),
            (Some(State::Provisioning), State::Provisioned),
            (Some(State::Provisioning), State::Deprovisioning),
            (Some(State::Provisioned), State::Deprovisioning),
            (Some(State::Deprovisioning), State::Deprovisioned),
        ];
        let states = [
            State::Planned,
            State::LegacyUnverified,
            State::Provisioning,
            State::Provisioned,
            State::Deprovisioning,
            State::Deprovisioned,
        ];

        for current in std::iter::once(None).chain(states.map(Some)) {
            for desired in states {
                let expected = current == Some(desired) || allowed.contains(&(current, desired));
                assert_eq!(
                    external_access_transition_allowed(current, desired),
                    expected,
                    "transition {current:?} -> {desired:?}"
                );
            }
        }
        assert!(!external_access_transition_allowed(
            Some(State::Deprovisioning),
            State::Provisioned
        ));
    }

    #[test]
    fn tenant_status_replacement_preserves_uid_and_resource_version() {
        let mut tenant = tenant_fixture("child", "api", TenantCredentialMode::Tls);
        tenant.metadata.resource_version = Some("41".to_string());
        let desired = EtcdTenantStatus {
            conditions: Vec::new(),
            external_identity: Some(build_external_identity(&tenant, &cluster_fixture()).unwrap()),
            external_access_state: Some(TenantExternalAccessState::Deprovisioning),
        };

        let replacement =
            tenant_status_replacement_snapshot(&tenant, "child", "api", desired.clone()).unwrap();
        assert_eq!(replacement.uid(), tenant.uid());
        assert_eq!(
            replacement.meta().resource_version,
            tenant.meta().resource_version
        );
        assert_eq!(replacement.status, Some(desired));

        tenant.metadata.resource_version = None;
        let error = tenant_status_replacement_snapshot(
            &tenant,
            "child",
            "api",
            EtcdTenantStatus {
                conditions: Vec::new(),
                external_identity: None,
                external_access_state: None,
            },
        )
        .unwrap_err();
        assert!(error.to_string().contains("metadata.resourceVersion"));
    }

    #[test]
    fn tls_external_identity_is_uid_scoped_with_stable_prefix() {
        let tenant = tenant_fixture("child", "apiserver", TenantCredentialMode::Tls);
        let identity = build_external_identity(&tenant, &cluster_fixture()).unwrap();

        assert_eq!(
            identity.etcd_user,
            "etcdtenant:22222222-2222-2222-2222-222222222222"
        );
        assert_eq!(
            identity.etcd_role,
            "etcdtenant-role:22222222-2222-2222-2222-222222222222"
        );
        assert_eq!(identity.etcd_user.len(), 47);
        assert_eq!(identity.prefix, "/child:apiserver/");
        assert_eq!(identity.output_secret_name, "apiserver-etcd");
        assert_eq!(
            identity.certificate_name.as_deref(),
            Some("etcdtenant-22222222-2222-2222-2222-222222222222")
        );
        assert_ne!(
            identity.output_secret_name,
            identity.staging_secret_name.unwrap()
        );
    }

    #[test]
    fn tls_common_name_accepts_64_bytes_and_rejects_65() {
        let tenant = tenant_fixture("child", "api", TenantCredentialMode::Tls);
        let mut identity = build_external_identity(&tenant, &cluster_fixture()).unwrap();
        identity.etcd_user = "u".repeat(64);
        assert!(validate_external_identity(&tenant, &identity).is_ok());

        identity.etcd_user = "u".repeat(65);
        let error = validate_external_identity(&tenant, &identity).unwrap_err();
        assert!(error.to_string().contains("65 bytes"));
        assert!(error.to_string().contains("commonName is limited to 64"));
    }

    #[test]
    fn destructive_cleanup_requires_the_exact_deterministic_identity() {
        let tenant = tenant_fixture("child", "api", TenantCredentialMode::Tls);
        let cluster = cluster_fixture();
        let identity = build_external_identity(&tenant, &cluster).unwrap();
        assert!(external_identity_matches_desired(&tenant, &cluster, &identity).unwrap());

        let mut corrupted = identity.clone();
        corrupted.etcd_user = "root".to_string();
        assert!(!external_identity_matches_desired(&tenant, &cluster, &corrupted).unwrap());

        let mut other_cluster = cluster.clone();
        other_cluster.metadata.name = Some("other-physical".to_string());
        other_cluster.metadata.uid = Some("33333333-3333-3333-3333-333333333333".to_string());
        let mut redirected = identity;
        redirected.cluster_ref.name = "other-physical".to_string();
        redirected.cluster_ref.uid = "33333333-3333-3333-3333-333333333333".to_string();
        let error =
            external_identity_matches_desired(&tenant, &other_cluster, &redirected).unwrap_err();
        assert!(error.to_string().contains("does not match spec.clusterRef"));
    }

    #[test]
    fn destructive_cleanup_requires_provisioned_controller_state() {
        let mut tenant = tenant_fixture("child", "api", TenantCredentialMode::Tls);
        let identity = build_external_identity(&tenant, &cluster_fixture()).unwrap();
        assert_eq!(
            tenant_deletion_access(&tenant),
            TenantDeletionAccess::Unproven
        );

        tenant.status = Some(EtcdTenantStatus {
            conditions: Vec::new(),
            external_identity: Some(identity.clone()),
            external_access_state: Some(TenantExternalAccessState::Planned),
        });
        assert_eq!(
            tenant_deletion_access(&tenant),
            TenantDeletionAccess::Planned
        );

        tenant.status.as_mut().unwrap().external_access_state =
            Some(TenantExternalAccessState::LegacyUnverified);
        assert_eq!(
            tenant_deletion_access(&tenant),
            TenantDeletionAccess::Unproven
        );
        tenant.status.as_mut().unwrap().external_access_state =
            Some(TenantExternalAccessState::Provisioning);
        assert_eq!(
            tenant_deletion_access(&tenant),
            TenantDeletionAccess::Provisioned(&identity)
        );

        tenant.status.as_mut().unwrap().external_access_state =
            Some(TenantExternalAccessState::Provisioned);
        assert_eq!(
            tenant_deletion_access(&tenant),
            TenantDeletionAccess::Provisioned(&identity)
        );

        tenant.status.as_mut().unwrap().external_access_state =
            Some(TenantExternalAccessState::Deprovisioning);
        assert_eq!(
            tenant_deletion_access(&tenant),
            TenantDeletionAccess::Provisioned(&identity)
        );

        tenant.status.as_mut().unwrap().external_access_state =
            Some(TenantExternalAccessState::Deprovisioned);
        assert_eq!(
            tenant_deletion_access(&tenant),
            TenantDeletionAccess::Planned
        );
    }

    #[test]
    fn deprovisioning_is_a_durable_reauthorization_barrier() {
        assert!(!deprovisioning_must_complete(None));
        assert!(!deprovisioning_must_complete(Some(
            TenantExternalAccessState::Planned
        )));
        assert!(!deprovisioning_must_complete(Some(
            TenantExternalAccessState::Provisioning
        )));
        assert!(!deprovisioning_must_complete(Some(
            TenantExternalAccessState::Provisioned
        )));
        assert!(deprovisioning_must_complete(Some(
            TenantExternalAccessState::Deprovisioning
        )));
        assert!(!deprovisioning_must_complete(Some(
            TenantExternalAccessState::Deprovisioned
        )));
    }

    #[test]
    fn tenant_provisioning_requires_current_unalarmed_cluster_readiness() {
        let mut cluster = cluster_fixture();
        cluster.metadata.generation = Some(7);
        cluster.status = Some(EtcdClusterStatus {
            connected: true,
            auth_enabled: true,
            version: Some(crate::cluster::QUALIFIED_ETCD_VERSION.to_string()),
            cluster_id: Some("0123456789abcdef".to_string()),
            conditions: vec![crate::crd::ready_condition_with_existing(
                true,
                "Connected",
                "",
                Some(6),
                &[],
            )],
            ..EtcdClusterStatus::default()
        });
        assert!(!cluster_is_ready_for_tenant_provisioning(&cluster));

        cluster.status.as_mut().unwrap().conditions[0].observed_generation = Some(7);
        assert!(cluster_is_ready_for_tenant_provisioning(&cluster));

        cluster.status.as_mut().unwrap().cluster_id = None;
        assert!(!cluster_is_ready_for_tenant_provisioning(&cluster));
        cluster.status.as_mut().unwrap().cluster_id = Some("0123456789abcdeF".to_string());
        assert!(!cluster_is_ready_for_tenant_provisioning(&cluster));
        cluster.status.as_mut().unwrap().cluster_id = Some("0123456789abcdef".to_string());

        cluster.status.as_mut().unwrap().auth_enabled = false;
        assert!(!cluster_is_ready_for_tenant_provisioning(&cluster));
        cluster.status.as_mut().unwrap().auth_enabled = true;

        cluster.status.as_mut().unwrap().version = Some("3.6.10".to_string());
        assert!(!cluster_is_ready_for_tenant_provisioning(&cluster));
        cluster.status.as_mut().unwrap().version =
            Some(crate::cluster::QUALIFIED_ETCD_VERSION.to_string());

        cluster.status.as_mut().unwrap().alarms = vec!["physical: NOSPACE".to_string()];
        assert!(!cluster_is_ready_for_tenant_provisioning(&cluster));
    }

    #[test]
    fn dependent_owner_references_do_not_require_owner_deletion_permission() {
        let tenant = tenant_fixture("child", "api", TenantCredentialMode::Tls);
        let cluster = cluster_fixture();

        let tenant_owner = tenant_owner_reference(&tenant).unwrap();
        assert_eq!(tenant_owner.controller, Some(true));
        assert_eq!(tenant_owner.block_owner_deletion, Some(false));

        let cluster_owner = cluster_owner_reference(&cluster).unwrap();
        assert_eq!(cluster_owner.controller, Some(true));
        assert_eq!(cluster_owner.block_owner_deletion, Some(false));
    }

    #[test]
    fn deletion_retains_foreign_artifacts_but_deprovisioning_rejects_them() {
        let tenant = tenant_fixture("child", "api", TenantCredentialMode::Tls);
        let owned = ObjectMeta {
            owner_references: Some(vec![tenant_owner_reference(&tenant).unwrap()]),
            ..ObjectMeta::default()
        };
        let foreign = ObjectMeta::default();
        assert_eq!(
            tenant_artifact_ownership(&owned, &tenant).unwrap(),
            ArtifactOwnership::Owned
        );
        assert_eq!(
            tenant_artifact_ownership(&foreign, &tenant).unwrap(),
            ArtifactOwnership::Foreign
        );
        assert_eq!(
            artifact_cleanup_disposition(
                ArtifactOwnership::Foreign,
                ArtifactCleanupMode::RetainForeign,
            ),
            ArtifactCleanupDisposition::RetainForeign
        );
        assert_eq!(
            artifact_cleanup_disposition(
                ArtifactOwnership::Foreign,
                ArtifactCleanupMode::RequireOwned,
            ),
            ArtifactCleanupDisposition::RejectForeign
        );
        assert_eq!(
            artifact_cleanup_disposition(
                ArtifactOwnership::Owned,
                ArtifactCleanupMode::RetainForeign,
            ),
            ArtifactCleanupDisposition::DeleteOwned
        );
    }

    #[test]
    fn cluster_namespace_artifact_labels_are_classified_by_exact_tenant_uid() {
        let tenant_uid = "22222222-2222-2222-2222-222222222222";
        let owned = ObjectMeta {
            labels: Some(BTreeMap::from([(
                TENANT_UID_LABEL.to_string(),
                tenant_uid.to_string(),
            )])),
            ..ObjectMeta::default()
        };
        assert_eq!(
            labeled_artifact_ownership(&owned, tenant_uid),
            ArtifactOwnership::Owned
        );
        assert_eq!(
            labeled_artifact_ownership(&owned, "33333333-3333-3333-3333-333333333333"),
            ArtifactOwnership::Foreign
        );
        assert_eq!(
            labeled_artifact_ownership(&ObjectMeta::default(), tenant_uid),
            ArtifactOwnership::Foreign
        );
    }

    #[test]
    fn tls_configmap_emits_complete_control_plane_values_and_ca_revision() {
        let tenant = tenant_fixture("child", "api", TenantCredentialMode::Tls);
        let cluster = cluster_fixture();
        let identity = build_external_identity(&tenant, &cluster).unwrap();
        let client_revision = sha256_revision(b"published client leaf");
        let endpoints = vec![
            "https://etcd-a:2379".to_string(),
            "https://etcd-b:2379".to_string(),
        ];
        let physical_server_ca = ByteString(b"physical server ca".to_vec());

        let rendered = build_control_plane_etcd_values(
            &endpoints,
            &physical_server_ca,
            &identity,
            &client_revision,
        )
        .unwrap();
        let values: serde_json::Value = serde_json::from_str(&rendered).unwrap();
        assert_eq!(
            values["etcd"]["endpoints"],
            serde_json::json!(["https://etcd-a:2379", "https://etcd-b:2379"])
        );
        assert_eq!(values["etcd"]["prefix"], "/child:api/");
        assert_eq!(values["etcd"]["clientSecret"]["create"], false);
        assert_eq!(values["etcd"]["clientSecret"]["name"], "api-etcd");
        assert_eq!(values["etcd"]["externalSecretRevisionsRequired"], true);
        assert_eq!(values["etcd"]["clientCertificateRevision"], client_revision);
        let revision = values["etcd"]["serverCATrustRevision"].as_str().unwrap();
        assert!(revision.starts_with("sha256:"));
        assert_eq!(revision.len(), 71);

        let rotated_physical_server_ca = ByteString(b"rotated physical ca".to_vec());
        let rotated_ca_values: serde_json::Value = serde_json::from_str(
            &build_control_plane_etcd_values(
                &endpoints,
                &rotated_physical_server_ca,
                &identity,
                &client_revision,
            )
            .unwrap(),
        )
        .unwrap();
        assert_ne!(
            values["etcd"]["serverCATrustRevision"],
            rotated_ca_values["etcd"]["serverCATrustRevision"]
        );
        assert_eq!(
            values["etcd"]["clientCertificateRevision"],
            rotated_ca_values["etcd"]["clientCertificateRevision"]
        );

        let rotated_client: serde_json::Value = serde_json::from_str(
            &build_control_plane_etcd_values(
                &endpoints,
                &rotated_physical_server_ca,
                &identity,
                &sha256_revision(b"renewed client leaf"),
            )
            .unwrap(),
        )
        .unwrap();
        assert_ne!(
            rotated_ca_values["etcd"]["clientCertificateRevision"],
            rotated_client["etcd"]["clientCertificateRevision"]
        );
    }

    #[test]
    fn password_identity_keeps_legacy_resource_names() {
        let tenant = tenant_fixture("child", "api", TenantCredentialMode::Password);
        let identity = build_external_identity(&tenant, &cluster_fixture()).unwrap();

        assert_eq!(identity.etcd_user, "child:api");
        assert_eq!(identity.etcd_role, "child:api");
        assert_eq!(identity.prefix, "/child:api/");
        assert_eq!(identity.output_secret_name, "api-etcd");
        assert_eq!(identity.config_map_name, "api-etcd");
        assert_eq!(
            identity.password_secret_name.as_deref(),
            Some("api-etcd-password")
        );
        assert!(identity.certificate_name.is_none());
        assert!(identity.staging_secret_name.is_none());
    }

    #[test]
    fn tls_output_uses_physical_server_ca_not_issuer_ca() {
        let mut staging_data = BTreeMap::new();
        staging_data.insert("tls.crt".to_string(), ByteString(b"leaf".to_vec()));
        staging_data.insert("tls.key".to_string(), ByteString(b"key".to_vec()));
        staging_data.insert(
            "ca.crt".to_string(),
            ByteString(b"CLIENT ISSUER CA".to_vec()),
        );
        let staging = Secret {
            data: Some(staging_data),
            ..Secret::default()
        };

        let data = compose_tls_secret_data(
            &staging,
            &ByteString(b"PHYSICAL SERVER CA".to_vec()),
            "child:api",
        )
        .unwrap();

        assert_eq!(data["tls.crt"].0, b"leaf");
        assert_eq!(data["tls.key"].0, b"key");
        assert_eq!(data["ca.crt"].0, b"PHYSICAL SERVER CA");
        assert_eq!(data["username"].0, b"child:api");
        assert!(!data.contains_key("client-ca.crt"));
    }

    #[test]
    fn client_certificate_revision_hashes_published_certificate_not_private_key() {
        let published = |private_key: &[u8]| Secret {
            data: Some(BTreeMap::from([
                (
                    "tls.crt".to_string(),
                    ByteString(b"published leaf certificate bytes".to_vec()),
                ),
                ("tls.key".to_string(), ByteString(private_key.to_vec())),
            ])),
            ..Secret::default()
        };
        let first = published(b"private key generation one");
        let second = published(b"private key generation two");
        let revision = published_client_certificate_revision(&first).unwrap();

        assert_eq!(revision.len(), 71);
        assert!(revision.starts_with("sha256:"));
        assert_eq!(
            revision,
            published_client_certificate_revision(&second).unwrap()
        );
        let rotated = Secret {
            data: Some(BTreeMap::from([(
                "tls.crt".to_string(),
                ByteString(b"renewed leaf certificate bytes".to_vec()),
            )])),
            ..Secret::default()
        };
        assert_ne!(
            revision,
            published_client_certificate_revision(&rotated).unwrap()
        );
    }

    #[test]
    fn client_certificate_revision_rejects_missing_published_leaf() {
        let error = published_client_certificate_revision(&Secret::default()).unwrap_err();
        assert!(error.to_string().contains("missing non-empty tls.crt"));
    }

    #[test]
    fn tls_output_rejects_incomplete_staging_secret() {
        let staging = Secret {
            data: Some(BTreeMap::from([(
                "tls.crt".to_string(),
                ByteString(b"leaf".to_vec()),
            )])),
            ..Secret::default()
        };
        let error = compose_tls_secret_data(
            &staging,
            &ByteString(b"PHYSICAL SERVER CA".to_vec()),
            "child:api",
        )
        .unwrap_err();
        assert!(error.to_string().contains("tls.key"));
    }

    #[test]
    fn certificate_ready_must_match_current_generation() {
        let current: DynamicObject = serde_json::from_value(serde_json::json!({
            "apiVersion": "cert-manager.io/v1",
            "kind": "Certificate",
            "metadata": {"name": "tenant", "namespace": "controller", "generation": 2},
            "status": {"conditions": [{
                "type": "Ready", "status": "True", "observedGeneration": 2
            }]}
        }))
        .unwrap();
        assert!(certificate_is_current_and_ready(&current));

        let stale: DynamicObject = serde_json::from_value(serde_json::json!({
            "apiVersion": "cert-manager.io/v1",
            "kind": "Certificate",
            "metadata": {"name": "tenant", "namespace": "controller", "generation": 3},
            "status": {"conditions": [{
                "type": "Ready", "status": "True", "observedGeneration": 2
            }]}
        }))
        .unwrap();
        assert!(!certificate_is_current_and_ready(&stale));
    }

    #[test]
    fn tls_leaf_ready_gate_uses_actual_not_after_with_one_hour_margin() {
        // The fixture is valid from 2019-07-12 11:12:30Z through
        // 2019-10-10 11:12:30Z. One second beyond the minimum margin passes.
        validate_tls_leaf_for_ready(TEST_LEAF_CERTIFICATE, test_unix(2019, 10, 10, 10, 12, 29))
            .unwrap();

        let error =
            validate_tls_leaf_for_ready(TEST_LEAF_CERTIFICATE, test_unix(2019, 10, 10, 10, 12, 30))
                .unwrap_err();
        assert!(error.to_string().contains("expires too soon for Ready"));
        assert!(error.to_string().contains("3600 seconds"));

        let chain = [TEST_LEAF_CERTIFICATE, TEST_LEAF_CERTIFICATE].concat();
        validate_tls_leaf_for_ready(&chain, test_unix(2019, 10, 10, 10, 12, 29)).unwrap();
    }

    #[test]
    fn tls_leaf_ready_gate_rejects_not_yet_valid_and_malformed_material() {
        let error =
            validate_tls_leaf_for_ready(TEST_LEAF_CERTIFICATE, test_unix(2019, 7, 12, 11, 12, 29))
                .unwrap_err();
        assert!(error.to_string().contains("is not valid yet"));

        let error = validate_tls_leaf_for_ready(b"not a certificate", 0).unwrap_err();
        assert!(error.to_string().contains("parseable PEM block"));

        let mislabeled = String::from_utf8(TEST_LEAF_CERTIFICATE.to_vec())
            .unwrap()
            .replace("CERTIFICATE", "PRIVATE KEY");
        let error = validate_tls_leaf_for_ready(mislabeled.as_bytes(), 0).unwrap_err();
        assert!(error.to_string().contains("not CERTIFICATE"));
    }

    #[test]
    fn certificate_is_staged_with_namespaced_issuer_and_rotating_key() {
        let tenant = tenant_fixture("child", "api", TenantCredentialMode::Tls);
        let cluster = cluster_fixture();
        let identity = build_external_identity(&tenant, &cluster).unwrap();
        let labels = BTreeMap::from([(TENANT_UID_LABEL.to_string(), tenant.uid().unwrap())]);
        let manifest = build_certificate_manifest(
            identity.certificate_name.as_deref().unwrap(),
            identity.staging_secret_name.as_deref().unwrap(),
            &identity,
            &cluster.spec.tenant_tls.as_ref().unwrap().issuer_ref,
            &labels,
            cluster_owner_reference(&cluster).unwrap(),
        );

        assert_eq!(manifest["metadata"]["namespace"], "controller");
        assert_eq!(manifest["spec"]["issuerRef"]["kind"], "Issuer");
        assert_eq!(manifest["spec"]["issuerRef"]["group"], "cert-manager.io");
        assert_eq!(
            manifest["spec"]["secretName"],
            identity.staging_secret_name.clone().unwrap()
        );
        assert_eq!(manifest["spec"]["privateKey"]["rotationPolicy"], "Always");
        assert_eq!(manifest["spec"]["duration"], "24h");
        assert_eq!(manifest["spec"]["renewBefore"], "8h");
        assert_eq!(
            manifest["metadata"]["ownerReferences"][0]["blockOwnerDeletion"],
            false
        );
        assert_eq!(
            manifest["spec"]["commonName"],
            "etcdtenant:22222222-2222-2222-2222-222222222222"
        );

        let mut cluster_issuer = cluster.spec.tenant_tls.unwrap().issuer_ref;
        cluster_issuer.kind = crate::crd::IssuerKind::ClusterIssuer;
        cluster_issuer.name = "fabric-etcd-client-v1".to_string();
        let cluster_scoped = build_certificate_manifest(
            "etcdtenant-22222222-2222-2222-2222-222222222222",
            "etcdtenant-22222222-2222-2222-2222-222222222222-tls",
            &identity,
            &cluster_issuer,
            &labels,
            cluster_owner_reference(&cluster_fixture()).unwrap(),
        );
        assert_eq!(cluster_scoped["spec"]["issuerRef"]["kind"], "ClusterIssuer");
        assert_eq!(
            cluster_scoped["spec"]["issuerRef"]["name"],
            "fabric-etcd-client-v1"
        );
    }

    #[test]
    fn exact_rbac_helpers_select_all_drift() {
        let desired = etcd_client::Permission::read_write("/child:api/").with_prefix();
        let stale = stale_permissions(
            vec![
                desired.clone(),
                etcd_client::Permission::read("/child:api/").with_prefix(),
                etcd_client::Permission::read_write("/child:other/").with_prefix(),
                etcd_client::Permission::read_write("").with_all_keys(),
            ],
            &desired,
        );
        assert_eq!(stale.len(), 3);
        assert!(stale.iter().all(|permission| permission != &desired));
    }

    #[test]
    fn planned_identity_detects_every_permission_range_overlapping_its_prefix() {
        let desired = b"/child:api/";
        assert!(permission_overlaps_prefix(
            &etcd_client::Permission::read("/child:api/key"),
            desired
        ));
        assert!(!permission_overlaps_prefix(
            &etcd_client::Permission::read("/child:other/key"),
            desired
        ));
        assert!(permission_overlaps_prefix(
            &etcd_client::Permission::read("/child:").with_prefix(),
            desired
        ));
        assert!(permission_overlaps_prefix(
            &etcd_client::Permission::read("/child:api/sub/").with_prefix(),
            desired
        ));
        assert!(!permission_overlaps_prefix(
            &etcd_client::Permission::read("/child:other/").with_prefix(),
            desired
        ));
        assert!(permission_overlaps_prefix(
            &etcd_client::Permission::read("/child:aaa").with_range_end("/child:z"),
            desired
        ));
        assert!(!permission_overlaps_prefix(
            &etcd_client::Permission::read("/child:aaa").with_range_end("/child:api/"),
            desired
        ));
        assert!(!permission_overlaps_prefix(
            &etcd_client::Permission::read("/child:api0").with_range_end("/child:z"),
            desired
        ));
        assert!(permission_overlaps_prefix(
            &etcd_client::Permission::read("/child:aaa").with_from_key(),
            desired
        ));
        assert!(!permission_overlaps_prefix(
            &etcd_client::Permission::read("/child:z").with_from_key(),
            desired
        ));
        assert!(permission_overlaps_prefix(
            &etcd_client::Permission::read("").with_all_keys(),
            desired
        ));
    }

    #[test]
    fn overlap_scan_exempts_only_the_builtin_root_and_owned_roles() {
        assert!(!external_role_requires_overlap_scan("root", "tenant-role"));
        assert!(!external_role_requires_overlap_scan(
            "tenant-role",
            "tenant-role"
        ));
        assert!(external_role_requires_overlap_scan(
            "another-all-keys-role",
            "tenant-role"
        ));
    }

    #[test]
    fn cross_namespace_authorization_is_explicit_and_revocable() {
        let mut tenant = tenant_fixture("child", "api", TenantCredentialMode::Tls);
        assert!(cluster_reference_is_cross_namespace(&tenant, "child"));
        tenant.spec.cluster_ref.namespace = None;
        assert!(!cluster_reference_is_cross_namespace(&tenant, "child"));
        tenant.spec.cluster_ref.namespace = Some("child".to_string());
        assert!(!cluster_reference_is_cross_namespace(&tenant, "child"));

        assert!(!cluster_allows_tenant_namespace(
            "controller",
            "controller",
            &[]
        ));
        assert!(!cluster_allows_tenant_namespace("controller", "child", &[]));
        assert!(cluster_allows_tenant_namespace(
            "controller",
            "child",
            &["child".to_string()]
        ));
        assert!(cluster_allows_tenant_namespace(
            "controller",
            "other",
            &["*".to_string()]
        ));
    }

    #[test]
    fn role_not_granted_is_not_misclassified_as_missing() {
        let missing = etcd_client::Error::GRpcStatus(tonic::Status::failed_precondition(
            "etcdserver: user name not found",
        ));
        assert!(is_not_found_error(&missing));

        let role_not_granted = etcd_client::Error::GRpcStatus(tonic::Status::failed_precondition(
            "etcdserver: role is not granted to the user",
        ));
        assert!(!is_not_found_error(&role_not_granted));
        assert!(is_role_not_granted_error(&role_not_granted));

        let other_failed_precondition = etcd_client::Error::GRpcStatus(
            tonic::Status::failed_precondition("etcdserver: user name not found"),
        );
        assert!(!is_role_not_granted_error(&other_failed_precondition));
    }

    #[test]
    fn password_rotation_only_accepts_the_exact_bad_credentials_error() {
        let bad_credentials = etcd_client::Error::GRpcStatus(tonic::Status::invalid_argument(
            "etcdserver: authentication failed, invalid user ID or password",
        ));
        assert!(is_invalid_credentials_error(&bad_credentials));

        let unrelated_invalid_argument = etcd_client::Error::GRpcStatus(
            tonic::Status::invalid_argument("etcdserver: user name is empty"),
        );
        assert!(!is_invalid_credentials_error(&unrelated_invalid_argument));

        let permission_denied = etcd_client::Error::GRpcStatus(tonic::Status::permission_denied(
            "etcdserver: permission denied",
        ));
        assert!(!is_invalid_credentials_error(&permission_denied));
    }

    #[test]
    fn access_probe_keys_are_inside_and_outside_the_tenant_prefix() {
        let tenant = tenant_fixture("child", "api", TenantCredentialMode::Tls);
        let identity = build_external_identity(&tenant, &cluster_fixture()).unwrap();
        let nonce = "0123456789abcdef0123456789abcdef";
        let (inside, outside) = access_probe_keys(&identity, &tenant.uid().unwrap(), nonce);

        assert!(inside.starts_with(&identity.prefix));
        assert!(!outside.starts_with(&identity.prefix));
        assert_eq!(
            inside,
            "/child:api/.etcdetcetc-controller-smoke/0123456789abcdef0123456789abcdef"
        );
        assert_eq!(
            outside,
            "/.etcdetcetc-controller-smoke/22222222-2222-2222-2222-222222222222/0123456789abcdef0123456789abcdef"
        );
        assert_eq!(
            populated_foreign_lease_probe_key(&tenant.uid().unwrap(), nonce),
            "/.etcdetcetc-controller-smoke/lease-guard/22222222-2222-2222-2222-222222222222/0123456789abcdef0123456789abcdef"
        );
        let generated = access_probe_nonce();
        assert_eq!(generated.len(), 32);
        assert!(generated.bytes().all(|byte| byte.is_ascii_hexdigit()));
    }

    #[test]
    fn shared_availability_risk_acceptance_is_exact() {
        let mut tenant = tenant_fixture("child", "api", TenantCredentialMode::Tls);
        assert!(!accepts_shared_availability_risk(&tenant));

        tenant.metadata.annotations = Some(BTreeMap::from([(
            SHARED_AVAILABILITY_RISK_ANNOTATION.to_string(),
            "accepted".to_string(),
        )]));
        assert!(!accepts_shared_availability_risk(&tenant));

        tenant.metadata.annotations.as_mut().unwrap().insert(
            SHARED_AVAILABILITY_RISK_ANNOTATION.to_string(),
            SHARED_AVAILABILITY_RISK_ACCEPTANCE.to_string(),
        );
        assert!(accepts_shared_availability_risk(&tenant));
    }

    #[tokio::test]
    async fn permission_denied_probe_uses_grpc_status_code() {
        let denied = expect_permission_denied("test", async {
            Err::<(), _>(etcd_client::Error::GRpcStatus(
                tonic::Status::permission_denied("not authorized"),
            ))
        })
        .await
        .unwrap();
        assert!(denied);

        let allowed = expect_permission_denied("test", async { Ok::<(), etcd_client::Error>(()) })
            .await
            .unwrap();
        assert!(!allowed);

        let unavailable = expect_permission_denied("test", async {
            Err::<(), _>(etcd_client::Error::GRpcStatus(tonic::Status::unavailable(
                "transport down",
            )))
        })
        .await;
        assert!(matches!(unavailable, Err(TenantError::Etcd(_))));
    }
}
