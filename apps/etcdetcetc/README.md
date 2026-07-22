# etcdetcetc

**etcd external tenant controller, etc.**

A Kubernetes controller that manages prefix-scoped etcd access for mutually
trusted workloads. It automates the lifecycle of etcd users, roles, and
permissions without claiming that one physical etcd cluster is a hostile
multi-tenant availability boundary.

## CRDs

### EtcdCluster

Declares an etcd cluster the controller can manage tenants on.

```yaml
apiVersion: etcdetcetc.samcday.com/v1alpha1
kind: EtcdCluster
metadata:
  name: hub-etcd
spec:
  endpoints:
    - https://etcd1.example.com:2379
    - https://etcd2.example.com:2379
    - https://etcd3.example.com:2379
  authSecretRef:
    name: etcd-root-credentials
  # Explicitly the CA that signed the physical etcd server certificates.
  # This must not be the tenant client-certificate issuer CA.
  serverCAConfigMapRef:
    name: etcd-server-ca
    key: ca.crt
  tenantTls:
    issuerRef:
      # Fabric keeps this ClusterIssuer's key outside every namespace readable
      # by the controller and protects its use with fail-closed admission.
      kind: ClusterIssuer
      name: fabric-etcd-client-v1
  allowedNamespaces:
    - my-app
```

The referenced Secret must hold a root/admin TLS client identity in `tls.crt`
and `tls.key`. Password-based admin authentication is deliberately unsupported:
etcd auth mutations advance the global auth revision, so a one-shot password
token can become stale partway through a multi-step tenant reconciliation.
This restriction does not remove the `Password` tenant credential mode.

`serverCAConfigMapRef` is the preferred source of the physical etcd server CA.
For compatibility, omitting it falls back to `authSecretRef.data["ca.crt"]`.
`tenantTls.issuerRef.kind` is either `Issuer` (the backward-compatible default,
colocated with the EtcdCluster) or `ClusterIssuer`. The API group is fixed to
`cert-manager.io`. A ClusterIssuer is safe only when its signing-key Secret is
outside every namespace readable by this controller and an independent,
fail-closed admission policy restricts Certificate and CertificateRequest
shapes. Fabric uses that isolated ClusterIssuer pattern.

The physical-cluster references in `endpoints`, `authSecretRef`, and
`serverCAConfigMapRef` are immutable after creation. Rotate credentials or CA
material by updating the referenced Secret or ConfigMap in place. Create a new
EtcdCluster for different endpoints or references; do not repurpose the
referenced objects to put a different physical cluster behind an existing
EtcdCluster.

`allowedNamespaces` is a continuously enforced authorization boundary for
cross-namespace tenant references. Empty permits no tenants; use `["*"]` to
allow every namespace except the EtcdCluster's own namespace. Removing a tenant
namespace actively revokes proven tenant access and removes its credentials,
while retaining the EtcdTenant, its pinned identity, and all keys under its prefix. Restoring
authorization lets the same tenant reconcile again. If external ownership is
not proven, revocation stops with `NamespaceDeprovisionBlocked` instead of
guessing which etcd identity is safe to remove. EtcdCluster changes enqueue
exactly the Tenants whose spec or pinned identity references that object; the
five-minute Tenant poll remains a missed-watch/restart fallback and the
certificate/liveness cadence, not the normal revocation trigger.

`Ready=True` on an EtcdCluster means every configured endpoint is reachable,
maps one-to-one onto the complete voter-only member list, reports the
controller's exactly qualified etcd server version `3.6.13`, agrees on a
nonzero listed leader and one physical cluster ID, returns a consistent direct
`AuthStatus` proof with RBAC enabled and a positive auth revision, and reports
no active etcd alarms. The first qualified ID is durably pinned in
`status.clusterId`; a different physical cluster is never adopted. An upgraded
legacy EtcdCluster with referencing tenants requires attended inventory and
the exact annotation `etcdetcetc.samcday.com/accept-cluster-id: <observed-id>`
before the initial pin.
Other versions remain connected for status and proven cleanup but get
`Ready=False` with reason `UnsupportedVersion`; the controller will not
provision on them. This exact allowlist is intentional: etcd 3.6.8 and earlier
have authorization bypasses across Lease and other APIs, while 3.6.10 and
earlier have nested transaction `PrevKv` and lease-attachment RBAC bypasses.
Its `Ready` condition carries `observedGeneration`; consumers must require that
value to equal `metadata.generation`, rather than accepting a stale condition
from an earlier spec generation. Tenant provisioning has this same
current-generation, version, and alarm gate.

### EtcdTenant

Carves out a keyspace on an EtcdCluster for a tenant.

```yaml
apiVersion: etcdetcetc.samcday.com/v1alpha1
kind: EtcdTenant
metadata:
  name: my-app
  namespace: my-app
  annotations:
    etcdetcetc.samcday.com/shared-availability-risk: accepted-v1
spec:
  clusterRef:
    name: hub-etcd
    namespace: etcd-system
  credentialMode: TLS
```

The controller creates the etcd user, role, and prefix permissions, then emits
a Secret with credentials and a ConfigMap with connection info. `Password` is
the backward-compatible default credential mode. `TLS` creates a client
Certificate and staging Secret in the EtcdCluster namespace, verifies the
credential's prefix isolation, then copies a stable Secret into the tenant
namespace. Tenant certificates last 24 hours, renew eight hours before expiry,
and rotate their private key on every issuance. Their `ca.crt` is always the
physical etcd server CA.

`clusterRef.namespace` is required and must differ from the EtcdTenant
namespace. This cross-namespace-only contract keeps the EtcdCluster, its admin
Secret, and its server-CA source outside tenant namespace garbage collection,
so deletion can still prove and revoke the exact etcd identity before releasing
the Tenant finalizer. Same-namespace tenancy is rejected before identity pinning
or finalizer creation.

Every non-deleting tenant must carry the exact, versioned
`etcdetcetc.samcday.com/shared-availability-risk: accepted-v1` annotation.
Without it, the controller performs no identity planning or etcd mutation and
reports `SharedAvailabilityRiskNotAccepted`, except that deletion and
EtcdCluster namespace deauthorization still perform proven revocation so the
annotation cannot strand access. The annotation is an operator anti-accident
acknowledgement, not an authorization mechanism; only trusted Flux/platform
identities may create tenants or read their credential Secrets.

The security boundary is deliberately narrow. Prefix RBAC protects key
confidentiality and integrity, and TLS credential publication proves in-prefix
access, outside-prefix denial, and denial of attaching to or revoking a
populated foreign lease, including the nested-transaction attachment path.
Native etcd leases have no owner, however: empty-lease races, lease TTL
metadata, backend capacity, request processing, alarms, and availability are
shared. A compromised tenant can deny service to every cluster even though it
cannot modify another populated prefix. Only mutually trusted,
Fabric-operated control planes are supported. Independently administered or
hostile tenants require dedicated etcd or an identity-aware proxy with lease
ownership and per-identity rate/capacity enforcement.

On deletion, credentials, user, role, and controller-owned Kubernetes artifacts
are removed, but all keys under the tenant prefix are deliberately retained. An
unreachable or missing EtcdCluster blocks finalizer removal because external cleanup cannot
be proven. Once external access has been revoked, deletion from a terminating
tenant namespace does not wait for tenant-local Secrets or ConfigMaps that
Kubernetes namespace garbage collection will remove.
After exact etcd revocation is proven, an exact-name Kubernetes artifact that
is demonstrably not controller-owned is retained rather than deleted and does
not wedge Tenant finalizer removal. Authorization-driven deprovisioning remains
stricter because that live Tenant can later be reauthorized.

`status.externalAccessState` records destructive-cleanup provenance. New
tenants progress through `Planned` (identity pinned; no etcd mutation),
`Provisioning` (the exact pinned identity is now controller-owned and an etcd
mutation may have started), and `Provisioned` (the exact user, role, and
permission have been reconciled). Authorization removal first records
`Deprovisioning`; that durable barrier forces exact revocation and strict
owned-artifact cleanup to finish even if the namespace is reauthorized
concurrently. Completion records `Deprovisioned`. Restored authorization then
returns the tenant to `Planned`, so all Kubernetes and etcd collision
preflights run again before RBAC can be recreated. A tenant discovered
with an older controller finalizer is marked `LegacyUnverified` until normal
reconciliation proves the exact external state. Deletion can remove a
`Planned` or `Deprovisioned` finalizer without touching etcd and revokes an
exact pinned identity in `Provisioning`, `Provisioned`, or `Deprovisioning`.
Deletion or namespace revocation in
`LegacyUnverified` or absent state fails closed and retains the finalizer for
attended recovery; never patch the state merely to bypass that guard.

Before an upgrade removes authorization or starts deletion, migrate every
old-controller tenant that has the controller finalizer but no pinned status.
Keep, or temporarily restore, its namespace in the EtcdCluster's
`spec.allowedNamespaces`; while the object is not deleting, wait for the
hardened controller to pin it as `LegacyUnverified`, reconcile the exact
external identity, reach `Provisioned`, and report current-generation
`Ready=True`. Authorization can then be removed normally and the resulting
deprovisioning observed. The referenced EtcdCluster remains deletion-blocked
while an unmigrated legacy finalizer exists so the admin path needed for
recovery is not removed. A legacy object that already has a deletion timestamp
cannot be migrated in place; it requires attended inspection and cleanup. Do
not initiate its deletion before completing migration.

Defaults:

- Password `user` and `role`: auto-assigned as `{namespace}:{name}`
- TLS `user` and X.509 common name: auto-assigned as
  `etcdtenant:{metadata.uid}`
- TLS `role`: auto-assigned as `etcdtenant-role:{metadata.uid}`
- `prefix`: auto-assigned as `/{namespace}:{name}/`
- `secretName`: `<name>-etcd`

`clusterRef`, `secretName`, and `credentialMode` are immutable. The resolved
EtcdCluster UID and all external resource names are pinned in status before
provisioning. A TLS identity is derived from the immutable Kubernetes object
UID, so deleting and recreating an EtcdTenant with the same namespace and name
cannot adopt the previous TLS identity. Its key prefix deliberately remains
the stable `/{namespace}:{name}/` value so retained data is available again
when that logical tenant is recreated. Before any destructive cleanup, the
controller requires the entire status pin to exactly match the deterministic
identity for the immutable spec and current EtcdCluster UID; a corrupt pin
cannot redirect user or role deletion.

The controller's runtime `--allowed-namespace` filter limits ordinary
reconciliation, while the Helm chart's `managedNamespaces` and
`clusterNamespaces` values grant the namespaced artifact and cluster-input
RBAC. These controls cannot perform tenant revocation and must not be narrowed
first. To retire a tenant namespace:

1. Migrate any legacy finalizer-only tenants as described above. If the
   namespace is already disallowed, temporarily reauthorize it and wait for
   each non-deleting legacy tenant to reach `Provisioned` and
   current-generation `Ready=True`.
2. Remove it from the relevant EtcdCluster's `spec.allowedNamespaces` while
   leaving the controller runtime scope and all chart RBAC unchanged.
3. Wait for every affected tenant to report a current-generation
   `Ready=False` with reason `NamespaceNotAllowed`. That condition is written
   only after the controller proves provisioning never started or revokes
   proven external access, removes its credential artifacts, and records the
   durable `Deprovisioned` state. Stop for
   attended recovery if any tenant reports `NamespaceDeprovisionBlocked`.
4. Only then remove the no-longer-needed entries from the chart's
   `allowedNamespaces`, `managedNamespaces`, and, when retiring a cluster
   namespace, `clusterNamespaces` values.

Apply those phases in separate reconciliations. Removing runtime scope or RBAC
in the same change as authorization can strand the cleanup operation.

TLS output Secrets have type `kubernetes.io/tls` and contain `tls.crt`,
`tls.key`, `username`, and `ca.crt` (the physical etcd server CA, never the
client issuer CA). Even when the staged and published bytes are unchanged, the
five-minute reconciliation poll performs a live TLS-authenticated prefix read;
an expired leaf or broken trust therefore makes the tenant non-Ready instead
of trusting stale cert-manager status.

TLS tenant ConfigMaps also contain a complete `values.yaml` fragment for
`charts/k8s-control-plane`: endpoint list, prefix, external Secret name, and a
SHA-256 revision of both the published client leaf certificate and the
physical server-CA bundle. The revision hashes only public `tls.crt` bytes,
never `tls.key`. They carry
`reconcile.fluxcd.io/watch: Enabled`, so a referencing Flux HelmRelease reacts
immediately. The chart puts both revisions on the apiserver Pod template.
kube-apiserver does not reliably reload its etcd client keypair in place, so a
verified client-leaf renewal deliberately rolls every apiserver. A physical
server-CA bundle change also rolls every apiserver so its process-local root
pool is rebuilt. The stable Secret and values fragment are built serially from
the same server-CA snapshot that passed the fresh admin proof; the separately
reconciled EtcdCluster ConfigMap remains a compatibility output and is not
copied as the Tenant handoff source. Generated fragments set
`etcd.externalSecretRevisionsRequired: true`, making both revisions mandatory.
The chart's false default exists only so pre-handoff legacy releases can remain
running until migration; Fabric tenants must never override the generated flag.

```yaml
spec:
  valuesFrom:
    - kind: ConfigMap
      name: my-app-etcd
      valuesKey: values.yaml
```

Do not split `endpoints` through `targetPath`; the complete values document
avoids Helm `--set` comma parsing and keeps the trust revision coupled to the
external Secret contract.

## Development

### Qualification

Run the ordinary Rust gates from this directory and keep the generated chart
CRDs exact:

```sh
cargo fmt --all -- --check
cargo test --workspace --locked
cargo check --workspace --all-targets --locked
cargo clippy --workspace --all-targets --locked -- -D warnings
scripts/sync-chart-crds --check
```

The production lifecycle gate is separate from the broad Tilt development
environment:

```sh
scripts/lifecycle-integration --check
scripts/lifecycle-integration --run
```

`--run` creates and always tears down a disposable kind API, a checksum-pinned
real etcd 3.6.13 process, two controller processes, and a deterministic
Certificate emulator. It proves three healthy same-holder Lease renewals,
non-preemption by the standby, a full stable-record interval before takeover,
and two current-generation EtcdCluster reconciliations by the replacement. It
also compares the exact physical member contract before and after the complete
lifecycle. The fixture mirrors the production two-CA boundary: server and
retained `fabric-root` identities chain to the physical CA, while the controller
admin and tenant leaves chain to the delegated client-only CA. The pass then
requires exact prefix isolation, client-leaf rotation and handoff revisions,
unchanged `fabric-root` access, durable deauthorization through early
reauthorization, retained tenant data, and exact finalizer cleanup. It never
reads or changes Fabric.

### Runtime leadership and health

The chart runs two replicas, but only the holder of the pre-created
`etcdetcetc-leader` Lease starts the EtcdCluster and EtcdTenant controller
streams. Each process uses the Pod UID plus a random boot nonce as its identity.
A candidate observes one Lease UID/resourceVersion unchanged for the full
30-second duration before a resourceVersion-guarded takeover. The active
process must renew within a ten-second monotonic hard deadline; expiry or any
observed holder loss revokes a watch-based permit around every complete
reconcile, force-cancels both streams, and terminates the process. The Lease
duration is validated exactly, so changing these timings requires an attended
stopped-controller Lease migration.

`/healthz` reports elector-loop liveness. `/readyz` reports recent successful
Lease API participation and is intentionally true for a healthy standby, which
allows both Deployment replicas to become Ready. `/leaderz` is true only for
the current holder. Lease coordination is not target-side fencing: a request
already accepted by Kubernetes or etcd can still complete after local
cancellation, so the bounded calls and 20-second takeover margin are part of
the safety contract. Every CR status write is additionally a full
resourceVersion-bearing status replacement. A late old-leader status update
therefore conflicts with any newer write instead of regressing the durable
Tenant deprovisioning barrier; conflicts are re-fetched by a later reconcile,
never retried from the stale decision.

### Upgrading controllers that predate external-access provenance

The CRD/chart upgrade and controller image must be treated as one gated
transition. Older controller images delete tenant prefix data and do not hold
an EtcdCluster finalizer; the new retention and deletion-ordering guarantees
therefore do not exist merely because the CRD schema has been updated. Suspend
an existing HelmRelease before exposing it to this chart revision, inventory
legacy tenants, pin the hardened image by immutable registry digest, and only
then resume it. Do not delete an EtcdTenant or EtcdCluster during that window.

For a Git/Flux rollout, deliver and observe the suspension as a safety-only
revision before delivering the shared chart/CRD changes. Putting suspension
and the schema change in one revision leaves an asynchronous source/reconcile
race in which the old image can briefly consume the new chart. Suspend any
separate HelmRelease that owns EtcdCluster resources at the same gate;
`dependsOn` does not freeze it. Directly Kustomized EtcdCluster manifests must
remain unchanged and must not be deleted until the hardened controller is
qualified.

Record the HelmRelease, Deployment, EtcdCluster, and CRD identities before
that revision. Do not deliver the chart/CRD revision until the GitRepository
and every owning Kustomization report the exact safety commit, every targeted
HelmRelease has `spec.suspend: true`, and no release reports an active
`Reconciling` condition. Because suspension does not cancel an already-started
Helm action, require its history, last-deployed time, attempted revision, and
config digest to remain stable for at least the configured Helm action timeout
(five minutes when unset). The controller Deployment UID, Pod template, image,
and availability; every owned EtcdCluster UID, generation, and spec; and the
EtcdCluster/EtcdTenant CRD generations and stored versions must also remain
unchanged. Repeat these checks after the full revision reaches the source:
HelmChart artifacts may advance, but suspended releases, workloads, custom
resources, and installed CRDs must not.

Before resuming a legacy release that previously ran cluster-wide, inventory
the live cluster—not only this repository—for every EtcdTenant, its namespace,
its referenced EtcdCluster namespace, finalizer, and pinned/legacy state. Add
the complete tenant and cluster namespace sets to chart `allowedNamespaces`,
`managedNamespaces`, and `clusterNamespaces` as appropriate. A repository
search cannot prove that app repositories or retained live objects do not add
tenants. Resuming with an incomplete list makes those objects invisible to
normal reconciliation and can strand their finalizers.

Suspending a HelmRelease freezes Helm reconciliation; it does not stop the
already-installed legacy controller Pod. Immediately before an eventual
resume, use a separately authorized attended operation to scale the legacy
`etcdetcetc` Deployment to zero and prove every old controller Pod is gone.
Take the authoritative live EtcdTenant/EtcdCluster inventory again only after
that proof (or first freeze every source capable of creating a Tenant), and
update the three namespace/RBAC lists from that final snapshot. Otherwise the
legacy cluster-wide controller can finalize a new Tenant between an early
inventory and its shutdown.
Before unsuspending, also prove the Flux-generated HelmChart artifact resolves
the exact reviewed hardened Git revision; an old chart ignores `image.digest`
and hardcodes the singleton, so a stale artifact could recreate the old binary
after scale-to-zero. Set `legacyClusterWideCoreWatches: false` with the
digest-pinned hardened release, then resume it. This prevents the
pre-leader-election singleton from overlapping the new Lease participants
during the Deployment migration. Do not rely on the new replicas' Lease to
fence an old binary that never joined it.

Keep any separate EtcdCluster-owner HelmRelease suspended after the hardened
controller starts. Wait until the controller has added its EtcdCluster
finalizer. For a legacy cluster with referencing tenants, capture the exact
observed 16-hex cluster ID from the resulting acceptance-required status, add
that value as `etcdetcetc.samcday.com/accept-cluster-id` through the owner
manifest, and only then resume the owner release. Do not let the resource owner
race the controller's first durable identity claim.

After the hardened controller has written `externalIdentity`,
`externalAccessState`, or a cluster identity, rollback to the legacy chart or
binary is forbidden. An old CRD can prune the new durable status fields and the
old binary restores prefix-purging deletion semantics. On a failed upgrade,
re-suspend and fix forward with the hardened schema/image; never use Helm
rollback to the pre-provenance release.

The same pre-resume inventory must prove that every tenant carries the exact
shared-availability acknowledgement and that every referenced physical etcd
reports `3.6.13`. In this repository the legacy Hub etcd (`3.6.4`) and Cloud
etcd (`3.6.10`) therefore remain non-provisioning migration debt: keep their
controller releases suspended until those physical stores are upgraded and
qualified. Never weaken the controller's version allowlist to make an old
release Ready.

The controller is the source of truth for the CRDs shipped by the Helm chart.
After changing either CustomResource type, regenerate and verify the chart
copy from this directory:

```sh
scripts/sync-chart-crds --apply
scripts/sync-chart-crds --check
```

## Future

- Multiple EtcdCluster support with pluggable tenant scheduling
- Tenant migration between clusters
