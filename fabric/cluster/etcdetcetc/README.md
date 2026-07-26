# etcdetcetc Fabric configuration

`fabric-etcdetcetc-config` deliberately reconciles this bootstrap directory
separately from `fabric-platform` and remains suspended until the attended
foundation ceremony starts. It depends on `fabric-platform` through the
unsuspended `fabric-etcdetcetc-policy` child, not on the controller: signer
admission, the CA, ClusterIssuers, and admin Certificate must exist before the
external etcd admin user can be provisioned.

The bootstrap Kustomization contains only API-compatible foundation objects:
the SOPS-encrypted online client-CA Secret isolated in `cert-manager`, its
versioned admission-gated ClusterIssuer, the tightly gated `selfsigned`
ClusterIssuer needed by `charts/k8s-control-plane`, the short-lived controller
client Certificate, and the public physical server-CA ConfigMap. It
does not contain the custom resources or require their CRDs. The separately
suspended `fabric-etcdetcetc-runtime` child owns `runtime/cluster.yaml` and
`runtime/smoke-tenant.yaml`; it depends on both bootstrap config and the
controller HelmRelease, which installs the CRDs. The runtime EtcdCluster's
explicit `serverCAConfigMapRef` is mandatory: cert-manager's `ca.crt` identifies
the delegated client issuer and must never be passed to an etcd client as the
physical server trust anchor.

The shared store is one trusted availability domain. The controller accepts
only the exactly qualified etcd `3.6.13` server version, and every Tenant must
carry
`etcdetcetc.samcday.com/shared-availability-risk: accepted-v1` before it can
pin an identity or touch etcd. Prefix RBAC protects key confidentiality and
integrity; it does not isolate native lease ownership, quota, alarms, request
capacity, or availability. This foundation is for Fabric-operated child
control planes only, not independently administered or hostile clients.

The `fabric-etcdetcetc-controller` Flux child and its enclosed HelmRelease are
independently suspended while the release points at the pre-hardening controller
image. Keeping that status-less HelmRelease outside the `wait: true` platform
child lets cert-manager and the rest of the platform become Ready independently.
After CI publishes the production controller, pin its exact digest and retain
both suspension gates until every preceding step below has passed. The pinned
release keeps `legacyClusterWideCoreWatches: false`; existing releases must
switch that compatibility value off only when they pin the namespaced-watch
controller image. The suspended Hub and Cloud legacy releases intentionally
omit Flux image-policy markers: image automation must not move either release
while the hardened artifact and its generated chart are being attested.

The controller runs two replicas with required hostname anti-affinity across
the two service nodes and a one-available PDB. Its node affinity pins the
scheduler to the exact `fabric-az1-svc1` and `fabric-az1-svc2` hostnames that
the router and root firewalls admit; a future generic platform label cannot
silently broaden that trust boundary. A declaratively created
`etcdetcetc-leader` Lease gates both controller streams: standbys never start a
reconciler, and a ten-second hard renew deadline revokes the in-process permit,
cancels every in-flight reconcile, and exits the process before another Pod can
take a record that remained unchanged for the full 30-second lease duration.
The exact 30-second duration is a fail-closed record invariant; changing it
requires an attended Lease migration while the controller is stopped, not a
mixed-timing rolling upgrade. Flux ignores `/spec` drift only on this Lease so
Helm owns its metadata without clearing the live holder record.

`/readyz` means a replica is a healthy election participant, so both the leader
and standby are Pod Ready and the two-replica HelmRelease can become Ready.
`/leaderz` is the separate leader-only signal; `/healthz` fails only when the
elector loop stalls. Kubernetes Lease election is coordination, not target-side
fencing: an etcd or Kubernetes request already accepted by a server can finish
after local cancellation. Per-boot identities, bounded calls, fatal leadership
loss, reconcile deadline guards, and the 20-second takeover margin narrow that
window; they do not justify a mathematical single-writer claim. Functional
acceptance still comes from current-generation EtcdCluster/EtcdTenant
conditions and the post-open qualification.

Use this exact activation order; do not collapse it into one Flux commit:

1. First deliver a safety-only revision that suspends the existing Hub
   `etcdetcetc` HelmRelease and both the Cloud `etcdetcetc` controller and
   `etcdetcetc-cloud-etcd` resource-owner HelmReleases. It must contain none of
   the shared chart or CRD changes. Observe all three releases suspended at
   that exact revision before continuing; `dependsOn` is not a freeze and the
   old controller must never see the new shared chart asynchronously.
2. Merge the complete foundation, let the per-app CI build only the hardened
   controller, then pin its exact provenance-attested digest in a follow-up
   revision. Keep the config child, controller child, enclosed HelmRelease, and
   runtime child suspended throughout both reconciliations.
3. While the live router still has `Reject-services-to-root-etcd`, run
   `scripts/qualify-fabric-etcd-pod-sources`. Both ordinary Pods must fail on
   TCP/2379 while the router observes only the two exact node source addresses.
4. Remove only `fabric-etcdetcetc-config.spec.suspend`. Its policy dependency
   allows cert-manager to publish the two exact ClusterIssuers and the
   `fabric-etcdetcetc-admin` Certificate Secret. This bootstrap path contains
   no EtcdCluster/Tenant and needs no controller CRD, so require the config
   Kustomization, both ClusterIssuers, Certificate, and Secret all to become
   Ready.
5. Roll the public delegated client trust through `cp1`, then `cp2`, then
   `cp3`. Certificate issuance in step 4 is local signing and does not contact
   etcd, but no delegated credential may be used until all three voters prove
   the exact two-CA client bundle and physical-only peer trust.
6. Run the attended admin plan and provision the exact passwordless
   `fabric-etcdetcetc` user. Prove its one built-in `root` role and the unchanged
   `fabric-root` permissions. The controller remains suspended, so this new
   root-equivalent user cannot yet reconcile a tenant.
7. Roll `scripts/rollout-fabric-root-firewall` through the three roots, one
   member at a time, while the router still rejects service access to all three
   etcd ports.
8. Run `scripts/rollout-fabric-router-etcd-policy`. This is the final
   network-open gate: it admits only the two exact service-node sources to the
   three exact roots on TCP/2379 and retains an immediate TCP/2380-2381 reject.
   Its read-only preflight mechanically proves the exact Flux/config/PKI/image,
   legacy-release suspension, all-root delegated-trust/firewall, and delegated
   admin contracts. Its attended run holds both maintenance locks, repeats
   that proof, reruns the two ordinary-Pod source qualification under those
   continuously held locks, repeats the full proof before and after safely
   staging the router candidate, then freshly rechecks the pinned Git revision,
   Flux suspension state, and absence of every standard Pod-producing workload
   immediately before the packet-opening apply. Freeze all `main` pushes and
   activation overrides from the final check through the attended run. The
   earlier step-3 result is discovery evidence, not a substitute for this fresh
   locked proof.
9. Before enabling host monitoring or running final post-open qualification,
   roll the current `scripts/rollout-fabric-root-firewall` policy through cp1,
   cp2, and cp3 one at a time while the router still rejects TCP/2381. Only
   after all three roots pass should
   `scripts/rollout-fabric-router-monitoring-policy` open the routed aperture.
   The dedicated router helper accepts only the already-open exact TCP/2379
   state, removes TCP/2381 from the internal reject, and adds only the two exact
   service sources to root TCP/2112,2381,9100. TCP/2380 and both cross-prefix
   catch-alls remain denied. Both rollout paths are independently attended,
   hash-bound, locked, and rollback-guarded.
10. Remove both controller suspension gates in one reviewed change. The
   controller child depends on the Ready bootstrap config; wait for the
   HelmRelease, both CRDs, and the controller Deployment. No runtime custom
   resource exists yet.
11. Remove only `fabric-etcdetcetc-runtime.spec.suspend`. Wait first for the
   EtcdCluster, then for the one permanent smoke Tenant and its TLS Secret. Do
   not create another tenant. EtcdCluster acceptance must contact all three
   configured members and show exact server version `3.6.13` on each; tenant
   credential publication additionally proves that a populated admin lease
   outside its prefix cannot accept the tenant's nested-transaction attachment
   or be revoked by the tenant.
12. Run `scripts/qualify-fabric-etcd-post-open`. It must prove the real ordinary
   Pod path on both service nodes, TCP/2379 and TCP/2381 positive plus TCP/2380
   negative behavior, unauthenticated TLS rejection, and the permanent smoke
   identity's in-prefix read/write plus outside-prefix denial. It also requires
   exactly one physical EtcdCluster and one smoke EtcdTenant, all ten
   fail-closed signer policies and bindings with current API-server type checking and zero CEL
   warnings, a successful dry-run of the exact Flux-owned admin Certificate,
   and denial of an unauthorized signer request. Only that pass completes
   foundation acceptance. It participates in the same
   `kube-system/fabric-maintenance-lock` as the pre-open, root-policy,
   router, delegated-trust, and delegated-admin helpers.

### Foundation-only rollback

A plain revert of the foundation commit is not a rollback. `fabric-root` has
`prune: false`, so removing the child manifests from `children.yaml` leaves the
live Kustomizations behind. In particular, the policy child would keep its
last-applied Deny policies and then fail when its source path disappeared.

Before config activation, roll back only through these observed Git phases:

1. Keep all four etcdetcetc child Kustomizations declared and keep config,
   controller, runtime, and the inner HelmRelease suspended. Change
   `etcdetcetc-policy/kustomization.yaml` to `resources: []`; wait for its
   `prune: true` inventory to become empty and prove every policy and binding
   absent.
2. Remove only `cert-manager.yaml` from the platform Kustomization while
   retaining its Namespace and NetworkPolicies. Do not suspend the
   `cert-manager/cert-manager` HelmRelease before deleting it: the pinned Helm
   controller skips release uninstall when deleting a suspended release. Wait
   for the HelmRelease and OCIRepository to disappear and the Helm uninstall
   to finish. Prove all chart-owned workloads, generated ReplicaSets and Pods,
   Jobs, Services, ServiceAccounts, PDBs, namespaced and cluster RBAC, webhook
   configurations, and Helm storage records absent; require no matching
   cert-manager Pod to remain. The chart's six retained CRDs are expected to
   remain.
3. After the zero-Pod proof, change `network-policies.yaml` to retain exactly
   the three `default-deny` objects. Observe the platform child's `prune: true`
   removal of `cert-manager/allow-dns-and-kubernetes-api`,
   `cert-manager/allow-kubernetes-api-to-webhook`, and
   `etcdetcetc/allow-controller-egress`, and prove all three absent.
4. Keep all three Namespaces and their remaining default-deny NetworkPolicies
   as an inert rollback tombstone. cert-manager creates runtime objects that
   Helm does not own: Secret `cert-manager/cert-manager-webhook-ca` and Leases
   `kube-system/cert-manager-controller` and
   `kube-system/cert-manager-cainjector-leader-election`. Snapshot both Lease
   UIDs, resourceVersions, holders, renew times, and durations; do not call the
   tombstone inert until they remain unchanged for longer than their lease
   durations and both renew times are expired. The Secret has no cert-manager
   workload or chart-owned RBAC consumer after the proven uninstall and its
   Namespace remains default-denied. Removing these objects or deleting a
   Namespace is a separate attended cleanup: inventory both `cert-manager` and
   `kube-system`, prove no live holder/consumer, and obtain explicit mutation
   authorization. Never use Namespace deletion as a shortcut for an incomplete
   Helm uninstall or runtime-residue review.
5. Leave the now-inert child Kustomizations and their source directories in
   Git. Retire them later only with a separately reviewed root cleanup that
   explicitly handles the non-pruning parent; direct deletion still requires
   separate operator authorization.

After config activation, use the phase-specific rollback and revocation
contracts below instead. After the router opens, fence first: neither Flux
suspension nor a source revert terminates an already-running controller Pod.

Run the final gate first in read-only mode and copy only the confirmation it
prints into the attended run:

```sh
sudo scripts/qualify-fabric-etcd-post-open \
  --check \
  --serial-ed25519-fingerprint SHA256:VALUE_FROM_ROUTER_SERIAL \
  --wired-observer-interface enp50s0u2 \
  --wired-observer-permanent-mac 00:e0:4c:68:05:36 \
  --identity /var/home/sam/.ssh/id_ed25519

sudo scripts/qualify-fabric-etcd-post-open \
  --run \
  --serial-ed25519-fingerprint SHA256:VALUE_FROM_ROUTER_SERIAL \
  --wired-observer-interface enp50s0u2 \
  --wired-observer-permanent-mac 00:e0:4c:68:05:36 \
  --identity /var/home/sam/.ssh/id_ed25519 \
  --confirm 'QUALIFY-FABRIC-ETCD-POST-OPEN:<pushed-main-commit>'
```

The run creates two temporary Pods and one label-scoped egress NetworkPolicy
inside `etcdetcetc-smoke`. No temporary Namespace is created and there is no
credential copy: each Pod mounts only the permanent `fabric-smoke-etcd` Secret.
The policy permits the checksum-pinned etcdctl archive from the router's
service-side `10.66.1.1:80` and the three exact roots on TCP/2379-2381; static
host aliases avoid adding DNS egress. The Pods are pinned one per service node,
run as restricted non-root ordinary Pods, and emit only fixed result markers.

Every possible probe write uses a short-TTL lease. Acceptance requires exact
`PermissionDenied` responses for both an outside-prefix read and write, while
the in-prefix put/get/delete succeeds. Router and root counters prove that both
service sources traversed TCP/2379 and TCP/2381 and that all TCP/2380 attempts
reached the named reject. On success, UID-aware cleanup removes both Pods, the temporary
NetworkPolicy, and the verdict-free router observer; it also proves the
permanent default-deny stayed unchanged. The run retains only non-secret
evidence. A failed run cleans only objects whose recorded UIDs still match and
retains `kube-system/fabric-maintenance-lock` for inspection.

The short leases used by qualification are cleanup bounds, not lease-owner
isolation. etcd leases are global and ownerless while empty; TTL metadata,
backend quota, alarms, request processing, and availability remain shared.
The controller's separate populated-foreign-lease guard proves the key
integrity behavior relied on from `3.6.13`, but no acceptance result permits an
untrusted control plane to share this physical store.

A failure stops the sequence. Before the router opens, keep every later
suspension gate closed, preserve the helper's rollback evidence and operation
lock, and use the documented attended rollback or admin revocation for the
phase that failed. After step 8 opens the router, failure handling is
fix-forward, re-suspend, and fence only: do not make another activation change,
run the router's attended persistent fence below immediately, then restore the
config/controller/runtime suspension gates declaratively before diagnosis.
Suspending a Flux Kustomization or HelmRelease does not stop an already-running
controller Pod; the router fence is the containment boundary.

Begin with the read-only fence check and copy only its revision-and-contract
confirmation:

~~~sh
sudo scripts/rollout-fabric-router-etcd-policy \
  --fence-check \
  --serial-ed25519-fingerprint SHA256:VALUE_FROM_ROUTER_SERIAL \
  --etcdctl /dev/shm/PRIVATE-DIR/etcdctl \
  --identity /var/home/sam/.ssh/id_ed25519

sudo scripts/rollout-fabric-router-etcd-policy \
  --fence-run \
  --serial-ed25519-fingerprint SHA256:VALUE_FROM_ROUTER_SERIAL \
  --etcdctl /dev/shm/PRIVATE-DIR/etcdctl \
  --identity /var/home/sam/.ssh/id_ed25519 \
  --confirm 'FENCE-FABRIC-ROUTER-ETCD-POLICY:<pushed-main-commit>:<fence-contract-sha256>'
~~~

The fence restores the exact legacy UCI reject and installs two persistent
fw4 'chain-pre/forward' drops for both directions of the exact
svc1/svc2-to-cp1/cp2/cp3 TCP/2379 path. Those drops are proved to precede every
forward accept, so established gRPC sessions cannot bypass containment. The
enabled router-local enforcer, include, and evidence deliberately remain after
success; ordinary '--check'/'--run' refuses that residue. There is no supported
in-place unfence. Keep the controller and runtime suspended and require a
separately reviewed serial or closed-policy reprovision procedure before
repeating every network-open and post-open gate.

The FENCE confirmation is also the break-glass authorization to continue if
the Fabric API is unavailable or the global maintenance ConfigMap already has
any holder. The helper records and preserves that holder; it never deletes,
adopts, or replaces it. If the API is unavailable it skips the ConfigMap
attempt, because control-plane failure must not block containment. Actual
router staging is serialized independently by an exact-owner volatile
`/tmp/fabric-router-etcd-policy-fence.operation` lock, so a concurrent or stale
same-revision workstation cannot delete another operation's `.staging` tree.

The enforcer is assembled under a root-owned `.staging` path and promoted to
the final work path atomically by a hash-validating init service. An exact
incomplete staging tree is rebuildable only while the router-local operation
lock is held and proves the earlier writer inactive. Unknown partial state,
completed state, init residue, or marker/hash disagreement is preserved for
serial inspection. The emergency EXIT path externally validates completed
staging and the installed init before executing it. A failed post-open run's
exact verdict-free observer table is tolerated during pre-arm validation and
removed only after the persistent fence is acknowledged; any modified or extra
observer object remains a hard stop.

Once hardened CRDs, provenance state, or controller behavior has landed, never
roll the shared CRDs/chart or controller back to the pre-hardening bootstrap
image to recover availability. Fix forward while fenced and suspended. Never
broaden the router/root source list, issue an alternate credential, unsuspend
through a failing gate, or bypass the smoke qualification merely to make
reconciliation Ready.

The smoke Tenant deterministically owns `/etcdetcetc-smoke:fabric-smoke/`.
Deleting it revokes its user and role but intentionally retains that prefix;
recreation reattaches the retained data with a newly issued certificate.

The runtime also runs `CronJob/fabric-etcd-smoke` every five minutes. Each Job
mounts only the Tenant-owned TLS Secret and generated contract ConfigMap,
selects a reachable physical endpoint, and performs a lease-backed
put/get/delete under `.periodic-smoke/<Pod UID>`. The 90-second lease bounds any
write left by a crashed Job. Its label-scoped NetworkPolicy permits only the
three physical root `/32`s on TCP/2379; it has no ServiceAccount token or DNS
egress. A successful run emits one non-secret `FABRIC_ETCD_SMOKE=pass` marker,
while failed Jobs remain available to the standard Kubernetes Job alerts.

## Ongoing tenant lifecycle

Fabric qualifies only cross-namespace tenants. Every EtcdTenant must set an
explicit `clusterRef.namespace: etcdetcetc` and must live outside the
`etcdetcetc` namespace. The CRD requires an explicit namespace and the
controller rejects a self-namespace reference; this keeps the admin Secret and
server-CA source outside tenant namespace garbage collection so finalizer
cleanup can still prove exact revocation.

Treat EtcdCluster readiness as an alarm gate, not merely a connectivity bit.
The controller provisions a tenant only when the cluster is connected, reports
exactly etcd `3.6.13` at every distinct voter, has no learners or active etcd
alarms, directly proves RBAC enabled with a consistent positive auth revision,
and has a durable 16-hex `status.clusterId` plus `Ready=True` with
`observedGeneration` equal to the EtcdCluster's current generation. A legacy
object with referencing tenants requires the exact attended
`etcdetcetc.samcday.com/accept-cluster-id` annotation before the initial pin. A
different version is `UnsupportedVersion`; do not weaken the allowlist. The tenant's own
accepted `Ready` condition must likewise match its current generation, and its
shared-availability acknowledgement must remain exact.

Tenant provisioning moves from `Planned` to `Provisioning` to `Provisioned`.
`Planned` proves no etcd mutation started; `Provisioning` is the durable claim
of the exact pinned identity written before the first possible mutation; and
`Provisioned` proves its user, role, and permission were reconciled. Both
claimed states are exact-cleanup provenance. Namespace authorization removal
records `Deprovisioning` before revocation and `Deprovisioned` only after exact
access revocation and strict owned-artifact cleanup. A concurrent
reauthorization cannot bypass `Deprovisioning`; after cleanup it sends
`Deprovisioned` back through `Planned`, repeating all collision preflights
before RBAC is restored. `LegacyUnverified` or a missing
`externalAccessState` is deliberately not enough provenance. If deletion or namespace revocation reports
`NamespaceDeprovisionBlocked`, retain the finalizer, keep controller scope and
RBAC in place, and perform attended inspection of the pinned identity and exact
etcd state. Do not patch the state or finalizer just to make reconciliation
advance.

Upgrade legacy tenants before starting either deletion or namespace
retirement. A tenant created by the old controller can have its finalizer but
no pinned status. If its namespace is already disallowed, temporarily restore
that namespace to the Fabric EtcdCluster's `spec.allowedNamespaces` while all
Helm scope/RBAC remains. For every non-deleting legacy tenant, wait until the
hardened controller pins and exactly reconciles it to
`externalAccessState=Provisioned` and current-generation `Ready=True`. Only
then remove authorization again and observe normal deprovisioning. The
EtcdCluster intentionally remains deletion-blocked by an unmigrated legacy
finalizer. A legacy tenant that already has a deletion timestamp cannot be
migrated in place and needs attended inspection and verified cleanup, so do not
initiate deletion before this migration.

The Fabric Helm release has three separate scope controls:

- `allowedNamespaces` selects which EtcdCluster and EtcdTenant namespaces the
  controller reconciles.
- `managedNamespaces` grants access to tenant-facing Secret and ConfigMap
  artifacts.
- `clusterNamespaces` grants access to EtcdCluster inputs and cluster-side
  certificate staging outside the release namespace.

None of these values revokes a tenant. To remove a tenant namespace, first
migrate all legacy finalizer-only tenants as above. Then remove the namespace
only from `EtcdCluster.spec.allowedNamespaces`, leaving all three Helm
scope/RBAC lists unchanged. Wait for every affected tenant to report a
current-generation `Ready=False` with reason `NamespaceNotAllowed`, which means
the controller proved provisioning never started or completed revocation,
artifact cleanup, and the durable `Deprovisioned` transition; stop on
`NamespaceDeprovisionBlocked`. Only in a later Flux
reconciliation may the now-unused Helm `allowedNamespaces` and
`managedNamespaces` entries be removed. Remove a `clusterNamespaces` entry
only after the cluster namespace and all of its tenants and staging artifacts
have been retired. Combining these phases can remove the visibility or RBAC
required to finish external revocation.

The Fabric EtcdCluster's `endpoints`, `authSecretRef`, and
`serverCAConfigMapRef` are immutable. Rotate the contents of the existing
Secret or ConfigMap for the same physical cluster. A different endpoint set or
reference requires a new EtcdCluster; never redirect the existing referenced
objects at another physical cluster.

## First child control-plane handoff

This section defines the future handoff; it is not permission to create a child
on the temporary flat L2. The managed-switch VLAN and anti-spoof gates in
`fabric/cluster/README.md` remain mandatory. Before the first child, extend
`charts/k8s-control-plane` with one exact svc1/svc2 placement contract shared
by every apiserver/controller-manager/scheduler Deployment and every bootstrap
Job/CronJob, plus hard hostname spreading. Fabric also needs PodMonitor output
disabled and all three VPAs disabled until those CRDs/controllers are installed,
then disjoint child CIDRs and an API publishing address/mechanism. The current
chart's generated TLS-etcd handoff renders correctly, but those parent-cluster
scheduling and API-exposure prerequisites are intentionally still absent.

Create the child namespace through Flux before its HelmRelease, and label it
for the fail-closed control-plane PKI policies:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: svc1-child
  labels:
    fabric.samcday.com/k8s-control-plane: "true"
```

In an earlier reconciliation, add that namespace to the Fabric EtcdCluster's
`allowedNamespaces` and to the controller Helm values
`allowedNamespaces`/`managedNamespaces`. Then create a TLS EtcdTenant in it
with explicit `clusterRef.namespace: etcdetcetc`, the shared-availability
acknowledgement, and the external Secret name the control-plane chart will
mount. Wait for current-generation `Ready=True`.

The Tenant-owned `<tenant>-etcd` ConfigMap is the direct Flux handoff. Merge it
after the child's ordinary values so its exact endpoint list, prefix, Secret
name, and physical server-CA revision win:

```yaml
spec:
  valuesFrom:
    - kind: ConfigMap
      name: control-plane-values
    - kind: ConfigMap
      name: apiserver-etcd
      valuesKey: values.yaml
```

Do not use individual `targetPath` injections for the comma-separated endpoint
field. The generated complete values document avoids Helm `--set` parsing and
is labelled `reconcile.fluxcd.io/watch: Enabled`. A verified client-leaf renewal
alters `etcd.clientCertificateRevision`; kube-apiserver does not reliably
reload `--etcd-certfile`/`--etcd-keyfile` in place, so that public-certificate
revision rolls its Pod template. It hashes only the published `tls.crt`, never
the private key. A physical server-CA bundle change separately alters
`etcd.serverCATrustRevision` and rolls the Pod template so its process-local
root pool is rebuilt. During physical CA rotation, retain the old/new overlap
until every child apiserver rollout is Ready and has reconnected successfully;
only then remove the old CA. The generated document also fixes
`etcd.externalSecretRevisionsRequired: true`, so either missing revision blocks
a Fabric child render. Do not disable it; the chart's false default is solely
for legacy pre-handoff releases awaiting migration.
