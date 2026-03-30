use kube::CustomResource;
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
    let dur = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    let secs = dur.as_secs();
    let days = secs / 86400;
    let time_secs = secs % 86400;
    let hours = time_secs / 3600;
    let minutes = (time_secs % 3600) / 60;
    let seconds = time_secs % 60;

    // Days since epoch to Y-M-D (civil calendar)
    // Algorithm from http://howardhinnant.github.io/date_algorithms.html
    let z = days as i64 + 719468;
    let era = z / 146097;
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };

    format!("{y:04}-{m:02}-{d:02}T{hours:02}:{minutes:02}:{seconds:02}Z")
}

// ---------------------------------------------------------------------------
// Shared types
// ---------------------------------------------------------------------------

/// Reference to a Secret in the same namespace.
#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema)]
pub struct LocalSecretReference {
    pub name: String,
}

/// Cross-namespace reference to an EtcdCluster.
#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema)]
pub struct ClusterReference {
    pub name: String,
    /// Namespace of the EtcdCluster. Defaults to the tenant's namespace.
    #[serde(default)]
    pub namespace: Option<String>,
}

// ---------------------------------------------------------------------------
// EtcdCluster
// ---------------------------------------------------------------------------

/// An etcd cluster that tenants can be provisioned on.
///
/// The controller connects using root credentials from the referenced auth
/// Secret, which can hold either TLS certs (tls.crt, tls.key, ca.crt) or
/// basic auth (username, password, ca.crt).
#[derive(CustomResource, Clone, Debug, Deserialize, Serialize, JsonSchema)]
#[kube(
    group = "etcdetcetc.samcday.com",
    version = "v1alpha1",
    kind = "EtcdCluster",
    namespaced,
    status = "EtcdClusterStatus",
    printcolumn = r#"{"name": "Ready", "type": "string", "jsonPath": ".status.conditions[?(@.type==\"Ready\")].status"}"#,
    printcolumn = r#"{"name": "Version", "type": "string", "jsonPath": ".status.version"}"#,
    printcolumn = r#"{"name": "DB Size", "type": "string", "jsonPath": ".status.dbSize"}"#,
    printcolumn = r#"{"name": "Leader", "type": "string", "jsonPath": ".status.leader"}"#
)]
#[serde(rename_all = "camelCase")]
pub struct EtcdClusterSpec {
    /// etcd endpoint URLs (e.g. https://host:2379).
    pub endpoints: Vec<String>,

    /// Reference to a Secret with root/admin credentials.
    pub auth_secret_ref: LocalSecretReference,

    /// Namespaces allowed to consume this cluster's tenants.
    /// When empty, cluster-wide RBAC is used. When set, per-namespace
    /// Role/RoleBindings are created for each listed namespace.
    #[serde(default)]
    pub allowed_namespaces: Vec<String>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize, JsonSchema, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct EtcdClusterStatus {
    #[serde(default)]
    pub connected: bool,
    /// Standard Kubernetes conditions (supports `kubectl wait --for=condition=Ready`).
    #[serde(default)]
    pub conditions: Vec<Condition>,
    /// etcd server version
    pub version: Option<String>,
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
/// On deletion a finalizer purges the keyspace and removes RBAC entities.
#[derive(CustomResource, Clone, Debug, Deserialize, Serialize, JsonSchema)]
#[kube(
    group = "etcdetcetc.samcday.com",
    version = "v1alpha1",
    kind = "EtcdTenant",
    namespaced,
    status = "EtcdTenantStatus",
    printcolumn = r#"{"name": "Ready", "type": "string", "jsonPath": ".status.conditions[?(@.type==\"Ready\")].status"}"#,
    printcolumn = r#"{"name": "Prefix", "type": "string", "jsonPath": ".spec.prefix"}"#
)]
#[serde(rename_all = "camelCase")]
pub struct EtcdTenantSpec {
    /// Reference to the EtcdCluster to provision on.
    pub cluster_ref: ClusterReference,

    /// etcd key prefix for this tenant. Defaults to `/<name>/`.
    #[schemars(regex(pattern = r"^/[A-Za-z0-9/_-]*/$"))]
    #[serde(default)]
    pub prefix: Option<String>,

    /// Name of the output Secret. Defaults to `<name>-etcd`.
    #[serde(default)]
    pub secret_name: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize, JsonSchema)]
pub struct EtcdTenantStatus {
    /// Standard Kubernetes conditions (supports `kubectl wait --for=condition=Ready`).
    #[serde(default)]
    pub conditions: Vec<Condition>,
}
