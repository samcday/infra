# etcdetcetc design

## Problem

Multi-tenant etcd clusters need per-tenant RBAC: users, roles, and
prefix-scoped permissions. Doing this by hand with `etcdctl` is error-prone,
not declarative, and makes credential lifecycle difficult to audit.

Here, "tenant" means a mutually trusted Fabric workload with a distinct key
prefix. It does not mean a hostile tenant or an independent availability
domain.

## Goals

1. Declare etcd tenancy as Kubernetes resources (CRDs).
2. Automate user/role/permission lifecycle against a live etcd cluster.
3. Revoke tenant access on deletion while deliberately retaining tenant data.
4. Emit a Secret per tenant with connection details for downstream consumers.
5. Support opt-in client certificates through an explicitly scoped
   cert-manager Issuer without conflating its client CA with the physical etcd
   server CA. Fabric isolates its signer as an admission-gated ClusterIssuer.

## Non-goals (v1alpha1)

- Managing etcd cluster deployment or lifecycle.
- Acting as a CA. cert-manager remains responsible for issuance and rotation.
- Multi-cluster scheduling or migration (future -- but the CRD shape
  accommodates it).
- Providing lease ownership, request fairness, per-tenant capacity, or
  availability isolation inside one physical etcd cluster.

## CRDs

### EtcdCluster

Represents a live etcd cluster the controller can connect to.

```yaml
apiVersion: etcdetcetc.samcday.com/v1alpha1
kind: EtcdCluster
metadata:
  name: hub-etcd
  namespace: etcd-system
spec:
  endpoints:
    - https://hub-az1-cp1.hub.internal:2379
    - https://hub-az1-cp2.hub.internal:2379
    - https://hub-az1-cp3.hub.internal:2379
  authSecretRef:
    name: hub-etcd-root
  serverCAConfigMapRef:
    name: hub-etcd-server-ca
    key: ca.crt
  tenantTls:
    issuerRef:
      kind: ClusterIssuer
      name: fabric-etcd-client-v1
  allowedNamespaces:
    - cloud-cluster
    - simonet
status:
  connected: false
```

**spec.endpoints**: etcd client URLs.

**spec.authSecretRef**: reference to a Secret (same namespace) holding a
root/admin TLS client identity in `tls.crt` and `tls.key`. Password-based admin
authentication is not supported: etcd auth mutations advance the global auth
revision, so a connected client's one-shot password token can become stale
during a multi-step tenant reconciliation. Tenant output may still use the
`Password` credential mode.

**spec.serverCAConfigMapRef**: preferred explicit source for the physical etcd
server CA. If omitted, `authSecretRef.data["ca.crt"]` is used only for backward
compatibility. The cluster connection ConfigMap and every tenant output use
this CA; cert-manager's client issuer `ca.crt` is never substituted for it.

`spec.endpoints`, `spec.authSecretRef`, and `spec.serverCAConfigMapRef` are
immutable. Credentials and CA material for the same physical cluster rotate by
changing the referenced Secret or ConfigMap contents in place. Changing an
endpoint set or reference requires a new EtcdCluster. The referenced objects
must not be repurposed to hide a different physical etcd cluster behind an
existing EtcdCluster identity.

**spec.tenantTls.issuerRef**: name and scope of a cert-manager signer. `Issuer`
is the backward-compatible default and must be in the EtcdCluster namespace.
`ClusterIssuer` is supported so the signer key can be isolated outside every
namespace readable by this controller, but its Certificate and
CertificateRequest surfaces must be independently constrained by fail-closed
admission. The API group is always `cert-manager.io`; tenants cannot select a
signer because the reference belongs to the EtcdCluster.

**spec.allowedNamespaces**: which tenant namespaces may reference this
EtcdCluster. Empty permits no tenants. Use `["*"]` to allow every namespace
except the EtcdCluster's own namespace. This is continuously enforced, not merely checked at admission:
removing a cross-namespace tenant actively deprovisions proven external access
and credentials while preserving its pinned identity and keyspace so it can be
reconciled if authorization is restored. Unproven legacy or partial state
blocks instead of inferring which identity is safe to remove.

**status.connected**, **status.authEnabled**, **status.clusterId**,
**status.alarms**, and **status.conditions**:
`connected` is true after the controller authenticates and pings the cluster,
but is not sufficient by itself for provisioning. `Ready=True` additionally
requires every configured endpoint to map one-to-one onto the complete
voter-only member list, the exactly qualified server version `3.6.13` on every
member, an agreed nonzero listed leader and physical cluster ID, consistent
direct AuthStatus responses with RBAC enabled and a positive auth revision,
and an empty etcd alarm list. The first qualified physical ID is durably pinned
as 16 lowercase hexadecimal digits; replacement IDs are rejected. An upgraded
object with referencing tenants requires attended inventory plus
`etcdetcetc.samcday.com/accept-cluster-id: <observed-id>` before its initial
pin. A reachable
different or mixed version reports
`UnsupportedVersion` and remains
available only for status and proven cleanup. Exact qualification avoids
silently accepting known authorization-bypass releases or unreviewed future
semantics. Every `Ready` condition records `observedGeneration`; a usable
cluster condition must match the EtcdCluster's current `metadata.generation`.

### EtcdTenant

Declares a tenant on an EtcdCluster. Namespace-scoped so it can live alongside
the workloads that consume it.

```yaml
apiVersion: etcdetcetc.samcday.com/v1alpha1
kind: EtcdTenant
metadata:
  name: cloud
  namespace: cloud-cluster
  annotations:
    etcdetcetc.samcday.com/shared-availability-risk: accepted-v1
spec:
  clusterRef:
    name: hub-etcd
    namespace: etcd-system
  secretName: cloud-etcd
  credentialMode: TLS
status:
  conditions:
    - type: Ready
      status: "False"
      reason: Pending
```

**spec.clusterRef**: cross-namespace reference to an EtcdCluster.
`namespace` is required and must differ from the EtcdTenant namespace. The CRD
requires an explicit non-empty value, and the controller rejects a
self-namespace value before identity pinning or finalizer creation. Keeping the
EtcdCluster, admin Secret, and physical server-CA source outside tenant
namespace garbage collection preserves the verified cleanup path when a tenant
namespace is deleted.

**metadata.annotations[`etcdetcetc.samcday.com/shared-availability-risk`]**:
must equal `accepted-v1` before normal identity planning or provisioning.
Deletion and EtcdCluster namespace deauthorization bypass this acknowledgement
so removing it cannot strand proven revocation. This is a versioned operator
acknowledgement of the shared failure domain, not an authorization control.

Password-mode etcd users and roles retain the legacy `{namespace}:{name}`
identity. TLS-mode users and certificate common names use
`etcdtenant:{metadata.uid}`; their roles use
`etcdtenant-role:{metadata.uid}`. The UID-derived TLS identity prevents a
deleted and recreated Kubernetes object from adopting credentials or RBAC left
by its predecessor.

In both modes, the etcd key prefix is the stable
`/{namespace}:{name}/` value. It is not user-configurable and deliberately
does not contain the UID: deleting and recreating the same logical tenant can
recover its retained data through a fresh external identity.

**spec.secretName**: name of the output Secret to create in the tenant's
namespace. Defaults to `<name>-etcd` if omitted.

**spec.credentialMode**: `Password` (default) or `TLS`. Together with
`clusterRef` and `secretName`, it is immutable. TLS uses the exact
`etcdtenant:{metadata.uid}` identity as its etcd user and certificate common
name, with `etcdtenant-role:{metadata.uid}` as its role. The fixed-width user
identity fits the X.509 common-name limit independently of the tenant namespace
and name. TLS certificates last 24 hours, renew eight hours before expiry, and
rotate their private key on every issuance.

**status.externalIdentity**: pins the resolved EtcdCluster UID, etcd
user/role/prefix, credential mode, and every managed resource name before any
external provisioning occurs. Reconciliation and deletion use the pin instead
of adopting a replacement cluster or identity. Before destructive cleanup,
the controller recomputes the deterministic identity from the immutable spec
and current EtcdCluster object and requires every pinned field to match.

**status.externalAccessState**: durable provenance for whether destructive
external cleanup is safe:

| State | Meaning and cleanup rule |
|-------|--------------------------|
| `Planned` | Identity is pinned and no etcd mutation has started. Deletion removes the finalizer without touching etcd. |
| `LegacyUnverified` | A pre-existing controller finalizer may represent legacy external state. Normal reconciliation must prove the exact user, role, and permission before cleanup is allowed. |
| `Provisioning` | The controller durably claimed the exact pinned identity before its first possible etcd mutation. Reconciliation may be partial; deletion or namespace revocation must perform exact pinned cleanup. |
| `Provisioned` | The exact pinned user, role, and permission were reconciled. Destructive identity revocation is allowed after the pin and cluster UID are revalidated. |
| `Deprovisioning` | Authorization-driven cleanup was durably claimed before the first revoke. Exact revocation and strict owned-artifact cleanup must finish even if namespace authorization is restored concurrently. |
| `Deprovisioned` | Exact access is revoked and owned credential artifacts are absent. Restored authorization transitions back to `Planned` so every collision preflight repeats before mutation. Deletion needs no further etcd cleanup. |

An absent state is treated like unverified legacy state. If a tenant reaches
deletion or authorization revocation while `LegacyUnverified` or absent, the
controller retains its finalizer and reports a blocked
condition. Attended recovery must inspect the pinned identity and actual etcd
and Kubernetes artifacts. Prefer restoring normal authorization, cluster
readiness, controller scope, and RBAC so reconciliation can finish and record
`Provisioned`; if deletion has already begun, perform and verify an explicitly
reviewed manual cleanup before deciding whether to remove the finalizer. Never
promote the state by hand merely to bypass the ownership proof.

The upgrade migration must happen before deletion or deauthorization. An
old-controller tenant can carry the controller finalizer without
`status.externalIdentity` or `status.externalAccessState`. While it is not
deleting, keep or temporarily restore its namespace authorization and allow the
hardened controller to pin it as `LegacyUnverified`, reconcile the exact
identity, advance to `Provisioned`, and publish a current-generation
`Ready=True` condition. Then remove authorization and observe normal
deprovisioning. Such an unmigrated finalizer continues to block deletion of the
referenced EtcdCluster, preserving the admin path needed for recovery. Once a
legacy tenant has a deletion timestamp, normal reconciliation cannot migrate it
in place; attended inspection and verified cleanup are required. Operators
must therefore migrate the legacy object before initiating deletion.

**status.conditions**: standard Kubernetes conditions array. The `Ready`
condition is `True` when user, role, and permissions are all provisioned in
etcd and credential publication is complete. Conditions include
`observedGeneration`; acceptance must require it to equal the tenant's current
`metadata.generation`. A plain `kubectl wait --for=condition=Ready` does not by
itself prove that generation match.

### Shared-etcd security contract

Prefix RBAC is a confidentiality and integrity boundary for keys. It is not an
availability boundary. Native etcd leases have no authenticated owner; an
empty lease can be observed by ID, renewed, revoked, or occupied before its
creator attaches the first key. TTL without returning keys exposes lease
metadata. Backend quota, proposal/request capacity, alarms, and the physical
failure domain are also shared. Once a lease contains a key outside another
tenant's prefix, etcd `3.6.13` denies that tenant's attach, renew, and revoke
operations; the TLS provisioning probe verifies the destructive revoke case
and nested-transaction attachment case against the configured server.

This design therefore supports only mutually trusted, Fabric-operated control
planes. Kubernetes Event objects are the stock apiserver path that uses native
etcd TTL leases; coordination `Lease` objects are ordinary prefixed keys. A
compromised control plane can still exhaust or disrupt the shared store.
Independently administered clients require a dedicated etcd cluster or an
identity-aware L7 proxy that owns the lease namespace and enforces per-identity
rate and space limits.

## Controller behaviour

### EtcdCluster reconciler

1. Read the auth Secret referenced by `spec.authSecretRef`.
2. Build an etcd client with the endpoints and credentials.
3. Ping the cluster (`etcdctl endpoint health` equivalent) and list alarms.
4. Update `status.connected`, `status.alarms`, and a current-generation `Ready`
   condition. Any server version except `3.6.13` sets `Ready=False` with reason
   `UnsupportedVersion`; any active alarm sets reason `AlarmsActive`.
5. Cache the client for use by the EtcdTenant reconciler.
6. Block EtcdCluster deletion while an EtcdTenant pins its UID.

Watches: EtcdCluster only. Namespaced Secret and ConfigMap reads are polled;
there are intentionally no cluster-wide core-resource watches.

The process-level `--allowed-namespace` filter scopes ordinary reconciliation;
the chart's `managedNamespaces` and `clusterNamespaces` values separately
grant namespaced tenant-artifact and cluster-input RBAC. Finalizer handling does
not make it safe to remove that scope or RBAC before an authorization-driven
deprovision has completed. Follow the two-phase scope-removal procedure below.

### EtcdTenant reconciler

**Create / Update:**

1. Require an explicit `clusterRef.namespace` different from the Tenant
   namespace before pinning identity or adding the finalizer, then resolve the
   referenced EtcdCluster. Existing finalizer-bearing same-namespace legacy
   objects continue only into fail-closed deprovisioning.
2. Authorize cross-namespace references using EtcdCluster
   `spec.allowedNamespaces`. If a previously authorized tenant is removed,
   first record `Deprovisioning`, then revoke its etcd identity and credentials,
   retain its finalizer, pinned identity, and prefix data, and record
   `Deprovisioned`. Once started, this cleanup finishes even if authorization
   is concurrently restored. Reauthorization returns `Deprovisioned` to
   `Planned` and repeats every artifact and etcd collision preflight before
   provisioning. This revocation takes precedence over the acknowledgement
   gate below. EtcdCluster watch events map through the in-memory Tenant store
   to exactly the spec- or pinned-identity references; the periodic Tenant poll
   is only a restart/missed-watch fallback and certificate/liveness cadence.
3. Require the exact shared-availability acknowledgement. If it is absent or
   changed, report `SharedAvailabilityRiskNotAccepted` without starting normal
   planning or provisioning.
4. Require `status.connected`, `status.authEnabled`, a durable physical
   `status.clusterId`, version `3.6.13`, no learners, no alarms, and
   `Ready=True` observed at the current EtcdCluster generation; otherwise
   requeue without starting tenant provisioning.
5. Compute scoped etcd identifiers:
   - Password user and role: `{namespace}:{name}`
   - TLS user and certificate common name: `etcdtenant:{metadata.uid}`
   - TLS role: `etcdtenant-role:{metadata.uid}`
   - prefix: `/{namespace}:{name}/`
6. Pin the resolved cluster UID and all external names in status as `Planned`
   before adding the finalizer. A pre-existing finalizer instead produces
   `LegacyUnverified`.
7. For a new `Planned` identity, prove that neither its etcd user nor role
   exists and that no foreign role range overlaps its prefix (except etcd's
   trusted built-in `root` role), then record the durable ownership claim
   `Provisioning` before the first etcd mutation.
8. Reconcile the user and role to exactly one read-write prefix permission,
   remove extra roles from the owned user, and revoke the owned role from every
   foreign user. Report any remaining foreign role overlap as an isolation
   conflict. Record `Provisioned` before publishing credentials.
9. For TLS, create a cluster-owned Certificate and labeled staging Secret in
   the EtcdCluster namespace, then compose the stable tenant Secret using the
   leaf key/cert plus the physical server-CA snapshot that passed the same
   reconcile's fresh admin mutation proof.
10. With a fresh TLS client, use a leased, collision-resistant sentinel and an
   atomic create guard inside the prefix, then require gRPC `PermissionDenied`
   for reads and writes outside it. Cleanup is conditional on the exact
   sentinel revision and value, so the probe cannot overwrite consumer data.
11. Using the admin client, create a second short lease containing an
   unguessable sentinel outside the tenant prefix. Require both a nested
   transaction that attaches an inside-prefix key and the tenant's revoke
   request to return gRPC `PermissionDenied`, prove that the exact sentinel and
   lease survived unchanged, then revoke it as admin. This catches populated
   foreign-lease and nested-transaction authorization regressions in the actual
   server; it does not claim ownership for empty leases.
12. Create or update the output ConfigMap, including a complete
   `charts/k8s-control-plane` values fragment, a SHA-256 revision of the
   published client `tls.crt` bytes, and a physical server-CA revision, then
   set a current-generation `Ready=True` condition. The public certificate
   revision never incorporates `tls.key`; it changes when cert-manager renews
   the leaf so Flux rolls kube-apiserver, which does not reliably reload its
   etcd client keypair in place. On unchanged TLS material, every five-minute
   poll still performs a live authenticated prefix read so stale Certificate
   status cannot hide an expired leaf. The ConfigMap is published only after
   the stable Secret and is built from the same verified endpoint/CA snapshot,
   rather than copying the asynchronously reconciled EtcdCluster ConfigMap.

Every EtcdCluster and EtcdTenant status mutation is a complete status PUT that
retains the reconciled object's UID and resourceVersion. A stale leader and its
successor cannot both commit from the same snapshot: one write advances the
resourceVersion and the other receives a conflict. Conflicts return to the
normal watch/re-fetch path; the controller never retries a stale status
decision in place. The explicit Tenant access-state transition graph also
rejects regressions across the durable `Deprovisioning` barrier.

**Delete (finalizer: `etcdetcetc.samcday.com/tenant`):**

1. Resolve and verify the status-pinned EtcdCluster UID, then require the whole
   pin to exactly match the deterministic identity for the immutable spec;
   missing, unreachable, replacement, or mismatched clusters block deletion.
2. If state is `Planned` or `Deprovisioned`, remove the finalizer without
   touching etcd. If state is `Provisioning`, `Provisioned`, or
   `Deprovisioning`, delete the exact pinned user and then its role.
   `LegacyUnverified` or absent state blocks for attended recovery rather than
   guessing whether a legacy identity is controller-owned.
3. Do **not** delete any key under the tenant prefix.
4. Delete the output Secret/ConfigMap, password Secret or TLS Certificate and
   staging Secret, and wait until they are absent. If the tenant namespace is
   terminating, clean cluster-namespace artifacts but do not wait for
   tenant-local objects that Kubernetes namespace garbage collection owns. If
   an exact-name artifact is demonstrably foreign, retain it: once exact etcd
   revocation has succeeded, deleting foreign data is unsafe and retaining it
   cannot preserve access. This exception applies only to Tenant deletion;
   authorization-driven deprovisioning keeps strict ownership checks because
   the live Tenant can later be reauthorized.
5. Remove the finalizer.

### Namespace authorization and runtime scope removal

EtcdCluster `spec.allowedNamespaces` is the authorization and deprovisioning
control. Helm `allowedNamespaces` is only the controller's process scope;
`managedNamespaces` and `clusterNamespaces` are RBAC controls. Retire scope in
two separately reconciled phases:

1. Inventory old-controller tenants with the finalizer but no pinned status.
   Keep or temporarily restore their EtcdCluster namespace authorization and,
   before initiating deletion, wait for `externalAccessState=Provisioned` and
   current-generation `Ready=True`.
2. Keep all Helm scope and RBAC entries in place. Remove the tenant namespace
   from every relevant EtcdCluster `spec.allowedNamespaces`.
3. Wait for all affected tenants to report current-generation `Ready=False`
   with reason `NamespaceNotAllowed`. That terminal deauthorization condition
   means the controller proved provisioning never started or successfully
   revoked the user and role, removed credential artifacts, and durably
   recorded `Deprovisioned`; prefix data remains. `NamespaceDeprovisionBlocked`
   is a hard stop requiring the attended provenance recovery described above.
4. After no affected tenant still needs cleanup, remove the applicable Helm
   `allowedNamespaces` and `managedNamespaces` entries. Remove a
   `clusterNamespaces` entry only after every EtcdCluster and cluster-namespace
   staging artifact there is retired as well.

Combining the authorization and Helm-scope changes can make the controller
lose visibility or permissions before it proves cleanup, so they must not be
delivered in one rollout.

### Upgrade ordering from the legacy controller

Updating the CRD alone does not provide the hardened deletion contract. The
legacy binary can still purge tenant prefix data and does not protect an
EtcdCluster with a finalizer. Existing HelmReleases must first be suspended in
a separately delivered and observed Git revision. While suspended, inventory
legacy tenants and publish the hardened controller image. Pin that image by
immutable registry digest, deliver the chart/CRD change, and resume only after
the migration prerequisites above are satisfied. A separate HelmRelease that
owns an EtcdCluster must be suspended in the same safety revision; a dependency
edge provides ordering, not a freeze. Directly Kustomized EtcdCluster resources
must remain unchanged. No EtcdTenant or EtcdCluster deletion is permitted from
the first suspension until the hardened controller is running and
current-generation readiness has been qualified.

The pre-resume inventory must query live EtcdTenant and EtcdCluster objects,
including objects sourced from other repositories. For a controller that was
previously cluster-wide, repository-local manifests are not proof that the new
runtime and RBAC namespace lists are complete. Every live tenant namespace must
remain in runtime scope and have tenant-artifact RBAC; every referenced cluster
namespace must have cluster-input/staging RBAC. An incomplete list makes legacy
objects invisible and can strand finalizers, so resume is forbidden until this
mapping is explicit and reviewed.

`HelmRelease.spec.suspend` does not terminate its existing workload. Before a
legacy release is resumed, an attended and separately authorized operation
must scale its old controller Deployment to zero and prove that no legacy Pod
remains. Repeat the authoritative live inventory after that proof, or first
freeze every Tenant-producing source; the old cluster-wide binary can finalize
a newly introduced Tenant between an earlier inventory and shutdown. Update
the runtime/RBAC namespace lists from this final snapshot. Prove the
Flux-generated HelmChart artifact is already at the exact
reviewed hardened Git revision before unsuspending: the old chart ignores an
image digest and can recreate its singleton even after the manual scale-down.
The resumed release must pin the hardened digest and disable
`legacyClusterWideCoreWatches`, then create only Lease-participating replicas.
A rolling overlap is unsafe because the legacy singleton does not observe the
new coordination Lease.

A separately managed EtcdCluster owner remains suspended while the hardened
controller starts and adds the cluster finalizer. If legacy tenants reference
an unpinned cluster, use its acceptance-required observation to commit the
exact 16-hex `etcdetcetc.samcday.com/accept-cluster-id` annotation through that
owner, then resume it. The owner must not race the controller's first durable
identity claim.

The transition is one-way once hardened provenance or cluster identity has
been written. Rolling back the chart can remove those status fields from the
served schema, and an old controller can prune them while restoring destructive
prefix deletion. Failure recovery is re-suspend plus fix-forward only; a Helm
rollback to the legacy chart/image is prohibited.

### Output Secret

Created in the same namespace as the EtcdTenant, owned by it.

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: <secretName>
  namespace: <tenant namespace>
  ownerReferences:
    - apiVersion: etcdetcetc.samcday.com/v1alpha1
      kind: EtcdTenant
      name: <tenant>
      controller: true
type: Opaque # Password mode
data:
  username: <base64 {tenant namespace}:{tenant name}>
  password: <base64 generated password>
```

TLS mode emits `type: kubernetes.io/tls` with `tls.crt`, `tls.key`,
`username`, and `ca.crt`. `ca.crt` is the physical server CA. Certificate
issuance uses a distinct staging Secret so cert-manager can never overwrite the
consumer's server trust bundle.

### Output ConfigMap

Mirrored from the EtcdCluster's ConfigMap into the tenant's namespace, owned
by the EtcdTenant.

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: <tenant>-etcd
  namespace: <tenant namespace>
  labels:
    reconcile.fluxcd.io/watch: Enabled
  ownerReferences:
    - apiVersion: etcdetcetc.samcday.com/v1alpha1
      kind: EtcdTenant
      name: <tenant>
      controller: true
data:
  endpoints: "https://host1:2379,https://host2:2379"
  prefix: "/<tenant namespace>:<tenant name>/"
  ca.crt: |
    -----BEGIN CERTIFICATE-----
    ...
    -----END CERTIFICATE-----
  values.yaml: |
    {
      "etcd": {
        "clientCertificateRevision": "sha256:<published-client-tls.crt-hash>",
        "clientSecret": {"create": false, "name": "<secretName>"},
        "endpoints": ["https://host1:2379", "https://host2:2379"],
        "externalSecretRevisionsRequired": true,
        "prefix": "/<tenant namespace>:<tenant name>/",
        "serverCATrustRevision": "sha256:<physical-server-ca-bundle-hash>"
      }
    }
```

Downstream consumers mount the Secret for credentials. A Flux HelmRelease for
`charts/k8s-control-plane` should consume the ConfigMap with
`valuesKey: values.yaml`; the watch label triggers immediate reconciliation,
and a client-leaf or server-CA change rolls the apiserver through the respective
Pod-template revision. cert-manager issues and rotates TLS credentials; this
controller owns their identity, staging, live verification, stable copy, and
public renewal signal.

The generated handoff always sets
`etcd.externalSecretRevisionsRequired: true`; the control-plane chart then
fails closed unless both canonical revisions are present. Its false default is
a migration-only compatibility path for existing external Secrets that predate
the handoff and cannot yet publish a renewal revision.

## Future roadmap

### Multiple EtcdCluster support

The architecture already supports this -- EtcdTenant references a specific
EtcdCluster. Future work:

- **Scheduling**: when `clusterRef` is omitted, a scheduler picks the best
  cluster based on capacity, locality, or policy. Pluggable via a trait /
  interface, similar to kube-scheduler's framework.
- **Capacity tracking**: EtcdCluster status reports tenant count, key count,
  and storage usage.

### Tenant migration

Move a tenant's keyspace from one EtcdCluster to another:

1. Snapshot keys under the prefix on the source cluster.
2. Restore them on the destination cluster with a new user/role.
3. Update the output Secret to point to the new cluster.
4. Purge the source keyspace.

This enables rebalancing, cluster decommissioning, and disaster recovery.
