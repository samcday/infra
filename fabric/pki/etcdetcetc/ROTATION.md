# Fabric etcdetcetc client-CA rotation contract

Status: design contract only. The current renderer, rollout helper, and
foundation verifier support exactly one delegated client CA. Do not begin a
rotation until the implementation gate below passes from a clean, pushed
`main` checkout. Nothing in this document authorizes a live mutation.

The delegated CA is root-equivalent for etcd client authentication. etcd maps
the certificate common name directly to a user, so possession of any trusted
delegated CA key permits minting `CN=root`, `CN=fabric-root`, or any tenant
identity. Deleting the `fabric-etcdetcetc` user is therefore not CA revocation.
Containment is complete only when the compromised CA fingerprint is absent
from every live etcd client trust bundle and every old TLS session has been
closed.

This contract has two deliberately different paths:

- Routine rotation preserves service with an old/new trust overlap. It is
  valid only while the old CA's custody is believed intact and every intended
  consumer is controller-managed.
- Emergency trust removal assumes the old private key may have escaped. It
  fences the client network first, removes old trust immediately, and accepts
  an outage for all delegated clients.

The physical CA remains the only peer CA in every phase. No client-CA
transition may alter `--peer-trusted-ca-file=/etc/etcd/pki/ca.pem`, membership,
peer certificates, or the physical root/recovery credentials.

## Why rotation uses isolated, versioned ClusterIssuers

Never replace the certificate and key inside the currently referenced CA
Secret as a rotation mechanism. cert-manager does not automatically reissue
leaves when a CA Issuer's backing Secret changes. A versioned ClusterIssuer makes the
signing transition explicit: changing a `Certificate.spec.issuerRef` is a
cert-manager reissuance trigger and is visible in both the Certificate and its
CertificateRequest.

Each generation has immutable, versioned identities. For generation `<id>`
(a lowercase DNS label such as `2029q2`), use all of:

- public certificate `fabric/pki/etcdetcetc/client-ca-<id>.pem`;
- SOPS Secret manifest
  `fabric/cluster/etcdetcetc/client-ca-<id>.yaml`;
- Secret `fabric-etcd-client-<id>-ca` in `cert-manager`, outside every
  namespace readable by the etcdetcetc controller;
- ClusterIssuer manifest `fabric/cluster/etcdetcetc/issuer-<id>.yaml`;
- admission-gated ClusterIssuer `fabric-etcd-client-<id>`; and
- CA subject `CN=fabric-etcdetcetc-client-ca-<id>`.

Names and fingerprints are never reused. A generation helper must refuse to
overwrite any path or Kubernetes name, use the existing verified tmpfs
procedure, retain P-256/pathlen:0/clientAuth-only constraints, and encrypt the
private key to exactly the Fabric runtime and recovery age recipients.

There is no second admin Secret. The existing
`Certificate/etcdetcetc/fabric-etcdetcetc-admin` is reissued into the existing
`Secret/etcdetcetc/fabric-etcdetcetc-admin`. The EtcdCluster's
`spec.authSecretRef` is immutable and must remain unchanged. cert-manager's
`rotationPolicy: Always` keeps the old usable Secret until the replacement is
signed, then atomically publishes a new key and certificate. The controller's
15-second poll hashes the Secret data and rebuilds its etcd client.

Tenant leaves switch by changing only
`EtcdCluster/etcdetcetc/fabric-etcd.spec.tenantTls.issuerRef.name`. The
controller applies that ClusterIssuer to every owned Certificate. It polls tenant
artifacts every five minutes, verifies the replacement credential against
etcd, and only then republishes the stable tenant Secret.

The permitted client trust states are exact:

1. `physical + current` (normal);
2. `physical + current + next` (routine overlap);
3. `physical + next` (routine completion); and
4. `physical only` (emergency containment).

The renderer must reject duplicates, unknown certificates, unpinned
fingerprints, unsafe files, unexpected subjects or usages, expiry inside the
declared safety window, and any other certificate count or ordering.

## Implementation gate

Before the first rotation, one reviewed change must make the existing tooling
phase-aware. At minimum it changes or adds all of the following:

- `fabric/pki/etcdetcetc/generate-client-ca-rotation` creates a new versioned
  public artifact, SOPS Secret, and ClusterIssuer without modifying the active
  generation. Generation and validation are separate modes.
- `fabric/butane/etcd.yaml` renders each of the four exact trust states, pins
  every included fingerprint, and continues to render peer trust from only
  `/etc/etcd/pki/ca.pem`.
- `scripts/rollout-fabric-etcd-client-trust` derives its state only from the
  reviewed Git payload, hashes every public CA and installed file, keeps its
  one-voter-at-a-time persistent rollback, and proves the exact live bundle.
  It must support three-, two-, and one-certificate total bundles, not an
  arbitrary operator-supplied list.
- `fabric/pki/etcdetcetc/verify-foundation` either delegates to a new
  phase-aware verifier or learns the normal, overlap, and physical-only
  contracts. It must not silently relax the initial fingerprint or object-set
  checks.
- `fabric/cluster/etcdetcetc-policy` admits both exact current and next
  ClusterIssuers during overlap, while continuing to reject arbitrary
  Certificates and CertificateRequests. cert-manager's `approveSignerNames`
  must list exactly those active admission-gated signer names.
- `scripts/qualify-fabric-etcd-post-open` resolves the active versioned public
  CA rather than always verifying against
  `fabric/pki/etcdetcetc/client-ca.pem`.
- A read-only rotation verifier inventories all managed Certificates,
  CertificateRequests, staging Secrets, and stable Secrets and emits a
  non-secret evidence record bound to the exact `origin/main` commit and both
  CA fingerprints.
- The Helm chart gains an exact `replicaCount` value so an incident commit can
  reconcile the controller Deployment to zero. Suspending a HelmRelease alone
  does not stop its existing Deployment.
- The router rollout gains an exact, payload-hashed `fenced` state that restores
  `Reject-services-to-root-etcd` for TCP/2379-2381. Emergency fencing must stay
  installed until explicitly recovered; it must not automatically roll back
  to an open policy.
- Automated warning for CA expiry now exists in `fabric/observer/alerts.yml` at
  exact, non-overlapping 90-, 30-, and 7-day thresholds. Its phase-labelled
  recording series is bound to the committed public CA's notAfter and SHA-256
  fingerprint. R1 must add the next generation before trust rollout, R5 must
  relabel current as `retiring`, and R6 may remove it only after old trust is
  absent. The same bounded observer now lists only Certificate
  metadata/spec/status in the `etcdetcetc` namespace and alerts on an invalid
  inventory, non-current/non-Ready status, a renewal time stale by more than
  fifteen minutes, and less than four hours of reported validity. It never
  receives Secret access: actual tenant leaf bytes and live prefix access stay
  enforced by the controller's five-minute probe, and actual admin usability
  stays enforced by the EtcdCluster controller's fifteen-second reconnect and
  health loop.
- `fabric/observer/collector.py` accepts issuer names through the exact
  `ETCD_ISSUER_ALLOWLIST`, never a version-like regular expression. The normal
  committed phase contains only current. R1 changes it to exact current plus
  the reviewed next name; arbitrary `v2`, `v999`, or syntactically valid but
  unreviewed generation IDs remain rejected. R5 removes current only after the
  no-old-reference inventory passes and simultaneously marks current's CA
  expiry recording series `lifecycle="retiring"`. R6 removes that retiring
  series only after old trust is proven absent. Each is part of its separate
  reviewed phase commit.

Every new live helper joins `kube-system/fabric-maintenance-lock`, requires a
clean freshly fetched `origin/main`, retains non-secret evidence, uses the
existing physical endpoint/member/alarm invariants, and refuses parallel root
changes. Tests must cover all four trust states, duplicate/unknown CA
rejection, peer-trust invariance, rollback from each state, and an old-leaf
negative probe after removal.

## Routine overlap rotation

Freeze tenant create/delete and scope changes for the rotation. Existing
renewals may continue. Capture the exact EtcdCluster UID, every TLS EtcdTenant
UID, every managed Certificate UID/revision/Secret name, and the old CA
fingerprint before starting. If that inventory changes during a phase, stop
and take a new plan; do not hold the global maintenance lock across the whole
overlap window.

Each numbered phase is a separate reviewed and pushed `main` commit. Do not
collapse adjacent phases.

### R0: generate an unused next generation

Run the versioned generator in verified tmpfs and review the new public
certificate, encrypted Secret, ClusterIssuer, admission changes, names,
recipients, lifetime, subject, SKI, and fingerprint. The Secret and
ClusterIssuer files may be committed, but they
must not yet appear in
`fabric/cluster/etcdetcetc/kustomization.yaml`. No live signer or Certificate
reference changes in R0.

### R1: trust next without permitting it to sign

Change `fabric/butane/etcd.yaml` to the exact
`physical + current + next` state. Update the phase-aware rollout and verifier
pins in the same commit. Extend the fail-closed Certificate/CertificateRequest
admission contract and cert-manager approval allowlist to exact current plus
next before publishing the next signer. In that same commit, add exactly the
reviewed next ClusterIssuer name to the observer's `ETCD_ISSUER_ALLOWLIST`;
never generalize it to a generation-name pattern. Leave the next
Secret/ClusterIssuer absent from the config Kustomization and leave both admin
and tenant issuer references on current.

After the commit is the exact `origin/main`, roll
`scripts/rollout-fabric-etcd-client-trust` through `cp1`, `cp2`, then `cp3`.
After each voter, require exact three-member/no-learner membership, no alarms,
endpoint and API health, a fresh heartbeat, the three-certificate client
bundle in physical/current/next order, and physical-only peer trust. Do not
continue while any voter has the old two-certificate state.

### R2: publish the next signer, still unused

Add only `client-ca-<id>.yaml` and `issuer-<id>.yaml` to
`fabric/cluster/etcdetcetc/kustomization.yaml`. Wait for
`fabric-etcdetcetc-config` to report the exact R2 revision and for the next
ClusterIssuer's `Ready=True` condition to observe its current generation. Prove the
live CA Secret certificate/key match the reviewed next public certificate and
that the Secret exists only in `cert-manager`, is unreadable by the
etcdetcetc ServiceAccount, and is referenced only by the exact admission-gated
ClusterIssuer. No Certificate may reference next yet.

### R3: reissue the existing admin credential

Change only `.spec.issuerRef.name` in
`fabric/cluster/etcdetcetc/admin-certificate.yaml` from current to next. Keep
its name, Secret name, CN, duration, renewal window, usages, and private-key
policy unchanged. Never change
`EtcdCluster.spec.authSecretRef`.

Wait for the admin Certificate/CertificateRequest/Secret proof below. Then
require the EtcdCluster to remain current-generation Ready and make a fresh
connection with the replacement admin Secret to each of the three configured
endpoints using the physical server CA. This proves every voter accepts next
before tenant migration begins.

### R4: reissue every tenant credential

Change only
`.spec.tenantTls.issuerRef.name` in
`fabric/cluster/etcdetcetc/runtime/cluster.yaml` from current to next. Wait for
the controller to update and republish every TLS tenant credential. Apply the
full proof below to the exact R0 tenant inventory. Require a fresh in-prefix
put/get/delete and outside-prefix read/write denial using every replacement
credential; the probe uses a bounded TTL and exact cleanup. Run the permanent
Fabric smoke qualification with the active next public CA.

No Certificate may still reference current, no managed staging or stable
Secret may still contain a current-signed leaf, and there may be no pending
old-issuer CertificateRequest before R5.

### R5: stop old signing

Remove the current ClusterIssuer and current private-key Secret from
`fabric/cluster/etcdetcetc/kustomization.yaml`; keep the old public certificate
in the root trust payload. Do not remove next or change either active
Certificate reference. Wait for Flux pruning and prove the old ClusterIssuer
and CA Secret are both Gone. Record that observed time as `T_stop` in
non-secret evidence.

Repeat the complete Certificate/Secret inventory after pruning. If old CA
custody is no longer trusted, do not wait: switch to emergency containment.
Once that inventory proves there is no old issuer reference, remove the exact
current name from the observer's `ETCD_ISSUER_ALLOWLIST` in R5 and relabel its
static CA expiry series from `active` to `retiring`.

For an ordinary rotation, retain overlap until both conditions are true:

- current time is later than the greatest observed old-leaf `notAfter` plus
  five minutes; and
- current time is later than `T_stop + 24h + 5m`.

The second bound relies on the enforced 24-hour maximum for every supported
admin and tenant issuance path. Any unmanaged consumer, longer-lived leaf, or
uncertain key custody invalidates that assumption and requires the emergency
path.

### R6: remove old trust

Change the committed root policy to exact `physical + next`. Roll it through
`cp1`, `cp2`, then `cp3`. After each member, prove a fresh next-admin
connection to that exact endpoint, physical credential health, exact
membership/alarms/API state, old fingerprint absence from the installed and
runtime bundles, next fingerprint presence exactly once, and physical-only
peer trust.

After all three voters converge, repeat the full tenant inventory and smoke
qualification, then remove the retiring current-CA expiry recording series;
the observer issuer allowlist must already contain only next from R5. Old
public certificates may remain in a clearly retired audit
directory, but no active renderer, verifier, qualifier, Kustomization, or
ClusterIssuer may reference them. The versioned next names remain active; do not
rename them back to the old names and cause an unnecessary second reissuance.

## Exact cert-manager and Secret proof

Capture the pre-switch revision, serial, SPKI hash, issuer, `notBefore`, and
`notAfter` for every Certificate. Acceptance after R3 or R4 requires all of
the following for every admin and tenant Certificate:

1. `spec.issuerRef` is exactly the next admission-gated cert-manager
   ClusterIssuer;
   `commonName`, `secretName`, 24-hour duration, 8-hour `renewBefore`, ECDSA,
   `rotationPolicy: Always`, and usages `digital signature,client auth` match
   the pinned identity.
2. `Ready=True` has `observedGeneration == metadata.generation`; Issuing is
   false or absent; `.status.revision` is greater than the captured revision;
   and status `notBefore`/`notAfter` describe the published leaf.
3. The latest Certificate-owned CertificateRequest has the same revision
   annotation, exact next `issuerRef`, correct owner UID, approved and
   `Ready=True`, and no denial/failure condition.
4. The issued Secret has non-empty `tls.crt` and `tls.key`; certificate and
   key public hashes match; the leaf is `CA:FALSE`, has only client-auth EKU,
   has the exact expected CN, has a new serial and SPKI, and does not outlive
   the next CA.
5. The leaf verifies for SSL client use against next and fails verification
   against current. Its AKI matches next's SKI. The Secret's issuer `ca.crt`,
   where cert-manager publishes it, is byte-identical to next.
6. The admin Secret retains its exact name and is accepted on a fresh TLS
   connection to all three endpoints. EtcdCluster Ready must observe its
   current generation after that Secret is published.
7. For every tenant, the controller-owned staging and stable Secret
   `tls.crt`/`tls.key` values are byte-identical. The stable Secret's
   `username` remains the pinned etcd user and its `ca.crt` remains the
   physical server CA, never the client Issuer CA.
8. TLS EtcdTenant UIDs and their owned Certificate/staging/stable Secret UIDs
   form an exact one-to-one inventory. There are no extra managed
   Certificates, missing tenants, old ClusterIssuer references, or pending old
   requests.
9. A fresh credential probe succeeds inside the pinned prefix and gets exact
   `PermissionDenied` outside it before the stable Secret is accepted.

Tenant `Ready=True` alone is insufficient evidence during rotation: its
generation need not change when cert-manager renews a staging Secret. Require
the new-chain bytes in both Secrets and the fresh access probe.

## Emergency compromised-CA containment

Emergency containment is destructive to delegated client availability and
requires Sam's explicit incident authorization. It does not broaden authority
to modify etcd membership, physical PKI, or tenant data.

Use a clean reviewed incident commit and serial/physical recovery access. The
order is security-critical:

1. Fence `svc1` and `svc2` from root TCP/2379-2381 with the exact committed
   router `fenced` state. Prove the reject is live and keep it closed. The
   existing flat-L2 anti-spoofing limitation means the physical incident scope
   must also be assessed. Do not start a root trust rollout before this fence.
2. Reconcile the controller Deployment to zero replicas; merely suspending its
   HelmRelease or Flux Kustomization does not stop the running Pod. Suspend the
   runtime child after the scale-down so its object set cannot change.
3. Remove the compromised ClusterIssuer, its private-key Secret, and the admin
   Certificate from the active config Kustomization and let Flux prune them.
   Do not suspend the config child before that deletion has reconciled. Secret
   deletion stops in-cluster signing but is not containment if the key was
   copied.
4. While the network fence remains closed, use the guarded root rollout to
   install exact `physical only` on `cp1`, `cp2`, then `cp3`. Restarting each
   member is required to close established old-CA TLS sessions. A member
   failure may roll back to its previous trust only because the network fence
   prevents that temporary restoration from being exploitable.
5. On every member prove the compromised fingerprint absent from both disk
   and the live runtime bundle, exact physical fingerprint present once,
   physical-only peer trust unchanged, the live etcd PID restarted, a fresh
   unexpired old-leaf connection rejected, physical recovery credentials
   accepted, and exact membership/health/alarms/API heartbeat preserved.
6. Only after all three voters reject the compromised CA may the
   `fabric-etcdetcetc` user be revoked. That revocation is cleanup, not the
   containment boundary. Inventory users, roles, revisions, alarms, audit
   evidence, and a recovery snapshot using only offline physical credentials;
   assume a compromised CA could already have acted as `root`.
7. Keep the network fence and controller scale-down until a clean replacement
   CA has been generated, trusted without the compromised CA, its admin and
   tenant credentials have passed the complete proof, and state integrity has
   been reviewed. Never re-add the compromised fingerprint for an overlap.

If the incident may include the Fabric SOPS runtime age identity, cert-manager
cert-manager controller, Flux decryption identity, or recovery custodian,
rotate or
rebuild that trust domain before publishing a replacement CA. Git history
contains the encrypted old CA Secret; deleting it from the branch is not a
substitute for rotating a compromised decryption identity.

Primary cert-manager behavior relied upon by this contract:

- <https://cert-manager.io/docs/configuration/ca/>
- <https://cert-manager.io/docs/usage/certificate/>
- <https://cert-manager.io/docs/reference/api-docs/>
