use kube::{CELSchema, CustomResource};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

// ---------------------------------------------------------------------------
// Standard Kubernetes conditions
// ---------------------------------------------------------------------------

#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct Condition {
    pub type_: String,
    pub status: ConditionStatus,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub observed_generation: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub message: Option<String>,
    pub last_transition_time: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq)]
pub enum ConditionStatus {
    True,
    False,
    Unknown,
}

impl std::fmt::Display for ConditionStatus {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::True => write!(f, "True"),
            Self::False => write!(f, "False"),
            Self::Unknown => write!(f, "Unknown"),
        }
    }
}

/// Build a Ready condition, preserving `last_transition_time` when status is unchanged.
pub fn ready_condition_with_existing(
    ready: bool,
    reason: &str,
    message: &str,
    observed_generation: Option<i64>,
    existing_conditions: &[Condition],
) -> Condition {
    let desired_status = if ready {
        ConditionStatus::True
    } else {
        ConditionStatus::False
    };

    let last_transition_time = match existing_conditions
        .iter()
        .find(|condition| condition.type_ == "Ready")
    {
        Some(existing) if existing.status == desired_status => {
            existing.last_transition_time.clone()
        }
        _ => rfc3339_now(),
    };

    Condition {
        type_: "Ready".to_string(),
        status: desired_status,
        observed_generation,
        reason: Some(reason.to_string()),
        message: if message.is_empty() {
            None
        } else {
            Some(message.to_string())
        },
        last_transition_time,
    }
}

fn rfc3339_now() -> String {
    chrono::Utc::now().format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

// ---------------------------------------------------------------------------
// Shared types
// ---------------------------------------------------------------------------

/// Reference to a Secret in the same namespace.
#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
pub struct LocalSecretReference {
    pub name: String,
}

/// Reference to one key in a ConfigMap in the same namespace.
#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct LocalConfigMapKeyReference {
    pub name: String,
    /// Key containing the PEM-encoded CA used to verify the physical etcd
    /// server certificates.
    #[serde(default = "default_ca_crt_key")]
    #[schemars(default = "default_ca_crt_key")]
    pub key: String,
}

fn default_ca_crt_key() -> String {
    "ca.crt".to_string()
}

/// Cross-namespace reference to an EtcdCluster.
#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
pub struct ClusterReference {
    pub name: String,
    /// Explicit namespace of the EtcdCluster. It must differ from the
    /// EtcdTenant namespace so namespace garbage collection cannot remove the
    /// administrative cleanup path together with the tenant.
    #[schemars(required)]
    pub namespace: Option<String>,
}

/// Permitted cert-manager issuer scope for tenant client certificates.
#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
pub enum IssuerKind {
    /// A namespaced Issuer colocated with the EtcdCluster.
    #[default]
    Issuer,
    /// A ClusterIssuer protected by a fail-closed admission policy.
    ClusterIssuer,
}

impl IssuerKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Issuer => "Issuer",
            Self::ClusterIssuer => "ClusterIssuer",
        }
    }
}

/// Reference to an explicitly scoped cert-manager issuer.
///
/// The API group remains fixed. `ClusterIssuer` is intended for a signer key
/// isolated outside every namespace readable by this controller and must be
/// protected by admission policy.
#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct LocalIssuerReference {
    pub name: String,
    #[serde(default)]
    pub kind: IssuerKind,
}

/// Opt-in configuration for tenant mTLS client credentials.
#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct TenantTlsConfig {
    pub issuer_ref: LocalIssuerReference,
}

// ---------------------------------------------------------------------------
// EtcdCluster
// ---------------------------------------------------------------------------

/// An etcd cluster that tenants can be provisioned on.
///
/// The controller connects using a root/admin TLS client certificate from the
/// referenced auth Secret (`tls.crt`, `tls.key`). Password-based admin auth is
/// intentionally unsupported.
#[derive(CustomResource, CELSchema, Clone, Debug, Deserialize, Serialize)]
#[kube(
    group = "etcdetcetc.samcday.com",
    version = "v1alpha1",
    kind = "EtcdCluster",
    namespaced,
    status = "EtcdClusterStatus",
    printcolumn = r#"{"name": "Ready", "type": "string", "jsonPath": ".status.conditions[?(@.type==\"Ready\")].status"}"#,
    printcolumn = r#"{"name": "Version", "type": "string", "jsonPath": ".status.version"}"#,
    printcolumn = r#"{"name": "DB Size", "type": "string", "jsonPath": ".status.dbSize"}"#,
    printcolumn = r#"{"name": "Leader", "type": "string", "jsonPath": ".status.leader"}"#,
    rule = kube::core::Rule::new("self.spec.endpoints == oldSelf.spec.endpoints").message("spec.endpoints is immutable; create a new EtcdCluster for a different physical etcd cluster"),
    rule = kube::core::Rule::new("self.spec.authSecretRef == oldSelf.spec.authSecretRef").message("spec.authSecretRef is immutable; rotate the referenced Secret contents in place"),
    rule = kube::core::Rule::new("(!has(self.spec.serverCAConfigMapRef) && !has(oldSelf.spec.serverCAConfigMapRef)) || (has(self.spec.serverCAConfigMapRef) && has(oldSelf.spec.serverCAConfigMapRef) && self.spec.serverCAConfigMapRef == oldSelf.spec.serverCAConfigMapRef)").message("spec.serverCAConfigMapRef is immutable; rotate the referenced ConfigMap contents in place")
)]
#[serde(rename_all = "camelCase")]
pub struct EtcdClusterSpec {
    /// etcd endpoint URLs (e.g. https://host:2379).
    pub endpoints: Vec<String>,

    /// Reference to a Secret with a root/admin TLS client certificate and key.
    pub auth_secret_ref: LocalSecretReference,

    /// Explicit physical etcd server CA. This is separate from any CA emitted
    /// by the tenant client-certificate Issuer. When omitted, `ca.crt` in
    /// `authSecretRef` is used for backward compatibility.
    #[serde(
        default,
        rename = "serverCAConfigMapRef",
        skip_serializing_if = "Option::is_none"
    )]
    #[schemars(rename = "serverCAConfigMapRef")]
    pub server_ca_config_map_ref: Option<LocalConfigMapKeyReference>,

    /// Cross-namespace tenant namespaces allowed to reference this cluster.
    /// Empty permits no tenants. Use `["*"]` to allow every namespace except
    /// the EtcdCluster's own namespace.
    #[serde(default)]
    pub allowed_namespaces: Vec<String>,

    /// Optional cert-manager Issuer used for TLS-mode tenants. A namespaced
    /// Issuer must be colocated with the EtcdCluster; a ClusterIssuer must be
    /// protected by an independent fail-closed admission policy. The
    /// Certificate and staging Secret are always created in the EtcdCluster
    /// namespace.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tenant_tls: Option<TenantTlsConfig>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize, JsonSchema, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct EtcdClusterStatus {
    #[serde(default)]
    pub connected: bool,
    /// Whether every configured endpoint freshly reports etcd RBAC auth
    /// enabled through the AuthStatus RPC.
    #[serde(default)]
    pub auth_enabled: bool,
    /// Standard Kubernetes conditions (supports `kubectl wait --for=condition=Ready`).
    #[serde(default)]
    pub conditions: Vec<Condition>,
    /// etcd server version
    pub version: Option<String>,
    /// Durable identity of the physical etcd cluster. The controller pins the
    /// first qualified nonzero cluster ID and never silently adopts another.
    pub cluster_id: Option<String>,
    /// Database size, human-readable (e.g. "24 MiB")
    pub db_size: Option<String>,
    /// Current leader member name
    pub leader: Option<String>,
    /// Cluster members
    #[serde(default)]
    pub members: Vec<ClusterMember>,
    /// Active alarms (e.g. "etcd-1: NOSPACE")
    #[serde(default)]
    pub alarms: Vec<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct ClusterMember {
    pub name: String,
    pub endpoint: String,
    #[serde(default)]
    pub is_learner: bool,
}

// ---------------------------------------------------------------------------
// EtcdTenant
// ---------------------------------------------------------------------------

/// A tenant on an EtcdCluster. The controller provisions the etcd user, role,
/// and prefix-scoped permissions, then emits a Secret with connection details.
///
/// On deletion a finalizer removes the tenant identity and credentials but
/// deliberately retains all keys under the tenant prefix.
#[derive(CustomResource, CELSchema, Clone, Debug, Deserialize, Serialize)]
#[kube(
    group = "etcdetcetc.samcday.com",
    version = "v1alpha1",
    kind = "EtcdTenant",
    namespaced,
    status = "EtcdTenantStatus",
    printcolumn = r#"{"name": "Ready", "type": "string", "jsonPath": ".status.conditions[?(@.type==\"Ready\")].status"}"#,
    rule = kube::core::Rule::new("self.spec.clusterRef == oldSelf.spec.clusterRef").message("spec.clusterRef is immutable"),
    rule = kube::core::Rule::new("self.spec.clusterRef.namespace != ''").message("spec.clusterRef.namespace must be non-empty"),
    rule = kube::core::Rule::new("(!has(self.spec.secretName) && !has(oldSelf.spec.secretName)) || (has(self.spec.secretName) && has(oldSelf.spec.secretName) && self.spec.secretName == oldSelf.spec.secretName)").message("spec.secretName is immutable"),
    rule = kube::core::Rule::new("self.spec.credentialMode == oldSelf.spec.credentialMode").message("spec.credentialMode is immutable")
)]
#[serde(rename_all = "camelCase")]
pub struct EtcdTenantSpec {
    /// Reference to the EtcdCluster to provision on.
    pub cluster_ref: ClusterReference,

    /// Name of the output Secret. Defaults to `<name>-etcd`.
    #[serde(default)]
    pub secret_name: Option<String>,

    /// Credential type emitted in `secretName`. Password preserves the legacy
    /// username/password contract; TLS requests a client certificate.
    #[serde(default)]
    #[schemars(default)]
    pub credential_mode: TenantCredentialMode,
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
pub enum TenantCredentialMode {
    #[default]
    Password,
    #[serde(rename = "TLS")]
    Tls,
}

/// Immutable resolved reference to the physical EtcdCluster used by a tenant.
#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct PinnedClusterReference {
    pub name: String,
    pub namespace: String,
    pub uid: String,
}

/// External identity and Kubernetes resource names pinned before provisioning.
/// Deletion uses this record rather than mutable desired-state fields.
#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct TenantExternalIdentity {
    pub cluster_ref: PinnedClusterReference,
    pub credential_mode: TenantCredentialMode,
    pub etcd_user: String,
    pub etcd_role: String,
    pub prefix: String,
    pub output_secret_name: String,
    pub config_map_name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub password_secret_name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub certificate_name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub staging_secret_name: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize, JsonSchema, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct EtcdTenantStatus {
    /// Standard Kubernetes conditions (supports `kubectl wait --for=condition=Ready`).
    #[serde(default)]
    pub conditions: Vec<Condition>,

    /// Controller-pinned identity and artifact names. Once set, reconciliation
    /// refuses to silently move the tenant to a different external identity.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub external_identity: Option<TenantExternalIdentity>,

    /// Durable controller ownership state for the pinned etcd user and role.
    /// `Provisioning` is written before the first possible external mutation,
    /// and `Deprovisioning` is written before authorization-driven cleanup.
    /// Those states must finish their exact operation even if authorization
    /// changes concurrently. Absence and `LegacyUnverified` remain unknown
    /// legacy state.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub external_access_state: Option<TenantExternalAccessState>,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
pub enum TenantExternalAccessState {
    /// Identity is pinned, but no etcd mutation has been attempted.
    Planned,
    /// A finalizer from an older controller was present before identity
    /// pinning, so existing external state must be reconciled before cleanup
    /// can be considered controller-owned.
    LegacyUnverified,
    /// The controller claimed the exact pinned identity before its first etcd
    /// mutation. Reconciliation may be partial, but exact cleanup is owned and
    /// required on deletion or namespace revocation.
    Provisioning,
    /// The exact pinned etcd user, role, and permission were reconciled.
    Provisioned,
    /// Authorization-driven cleanup has been durably claimed. Exact access
    /// revocation and owned-artifact removal must finish before this tenant can
    /// be provisioned again, even if namespace authorization is restored.
    Deprovisioning,
    /// Exact external access was revoked and owned credential artifacts are
    /// absent. Restored authorization begins a fresh `Planned` cycle and must
    /// repeat all collision preflights before any external mutation.
    Deprovisioned,
}

#[cfg(test)]
mod tests {
    use super::*;
    use kube::CustomResourceExt;

    #[test]
    fn password_mode_is_backward_compatible_default() {
        let tenant: EtcdTenant = serde_json::from_value(serde_json::json!({
            "apiVersion": "etcdetcetc.samcday.com/v1alpha1",
            "kind": "EtcdTenant",
            "metadata": {"name": "api", "namespace": "child"},
            "spec": {"clusterRef": {"name": "physical", "namespace": "controller"}}
        }))
        .unwrap();
        assert_eq!(tenant.spec.credential_mode, TenantCredentialMode::Password);
    }

    #[test]
    fn server_ca_config_map_key_defaults_to_ca_crt() {
        let cluster: EtcdCluster = serde_json::from_value(serde_json::json!({
            "apiVersion": "etcdetcetc.samcday.com/v1alpha1",
            "kind": "EtcdCluster",
            "metadata": {"name": "physical", "namespace": "controller"},
            "spec": {
                "endpoints": ["https://etcd.example:2379"],
                "authSecretRef": {"name": "admin"},
                "serverCAConfigMapRef": {"name": "physical-etcd-server-ca"}
            }
        }))
        .unwrap();
        let reference = cluster.spec.server_ca_config_map_ref.unwrap();
        assert_eq!(reference.name, "physical-etcd-server-ca");
        assert_eq!(reference.key, "ca.crt");
    }

    #[test]
    fn generated_crd_contains_defaults_and_immutability_rules() {
        let crd = serde_json::to_string(&EtcdTenant::crd()).unwrap();
        assert!(crd.contains(r#""credentialMode":{"default":"Password""#));
        assert!(crd.contains("spec.clusterRef is immutable"));
        assert!(crd.contains("spec.clusterRef.namespace must be non-empty"));
        assert!(crd.contains("spec.secretName is immutable"));
        assert!(crd.contains("spec.credentialMode is immutable"));

        let cluster_crd = serde_json::to_string(&EtcdCluster::crd()).unwrap();
        assert!(cluster_crd.contains("serverCAConfigMapRef"));
        assert!(cluster_crd.contains(r#""key":{"default":"ca.crt""#));
        assert!(cluster_crd.contains("tenantTls"));
        assert!(cluster_crd.contains("clusterId"));
        assert!(cluster_crd.contains("spec.endpoints is immutable"));
        assert!(cluster_crd.contains("spec.authSecretRef is immutable"));
        assert!(cluster_crd.contains("spec.serverCAConfigMapRef is immutable"));
        assert!(cluster_crd.contains("observedGeneration"));
        assert!(crd.contains("externalAccessState"));
        assert!(crd.contains("LegacyUnverified"));
        assert!(crd.contains("observedGeneration"));

        let tenant_crd = serde_json::to_value(EtcdTenant::crd()).unwrap();
        let cluster_ref_required = tenant_crd
            .pointer("/spec/versions/0/schema/openAPIV3Schema/properties/spec/properties/clusterRef/required")
            .and_then(serde_json::Value::as_array)
            .unwrap();
        assert!(
            cluster_ref_required
                .iter()
                .any(|field| field == "namespace")
        );
    }
}
