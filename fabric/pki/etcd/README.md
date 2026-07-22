# fabric etcd PKI

This CA is independent from the current hub. Each physical etcd member has a
different peer/server key whose SANs contain only that node's stable fabric
name and address. K3s uses the `fabric-root` client identity under the
`/fabric-root` prefix. The `root` identity is reserved for recovery and etcd
administration. The separate `fabric-observer` client identity is admitted as
an authenticated etcd user with no key-space role; etcd 3.6 permits it to read
status and list alarms but not to read or write keys, deactivate an active
alarm, defragment, or take snapshots. An empty `etcdctl alarm disarm` is not a
valid negative test because the client sends no deactivation RPC when there is
nothing to disarm.

Certificates and keys are offline bootstrap material encrypted only to Sam's
personal age identity; the live fabric SOPS key cannot recover the CA or other
members' private keys from Git. The CA expires after ten years and leaf
certificates after five years. Expiry alerting and a rehearsed rotation must
exist well before the first leaf enters its final 90 days. Helpers that decrypt
PKI fail closed unless their temporary work directory is on verified tmpfs;
removing plaintext from an SSD or CoW filesystem is not secure erasure.

To create a brand-new PKI set:

```sh
./generate.sh
```

This is intentionally a create operation, not routine renewal. It refuses to
replace any existing artifact. `--replace` replaces the CA and every leaf;
review the diff and never reconcile a partial replacement into a running
cluster.

The installer extracts each rendered member certificate, verifies it against
the embedded CA and expected node name/address, and derives that ISO's live UTC
admission window directly from the certificate. A PKI replacement therefore
cannot silently retain stale hard-coded dates.

After an intentional replacement, synchronize the encrypted copies embedded
in the offline Butane profiles and verify them byte-for-byte:

```sh
./sync-butane --apply --confirm SYNC:fabric-etcd-pki
./sync-butane --check
```

The synchronizer knows only the twelve expected PKI fields, stages and
round-trips every SOPS file before publishing, and never writes plaintext to
the repository.

## Enable authorization after quorum

Mutual TLS rejects clients without a trusted certificate, but etcd key prefixes
are not authorization boundaries until authentication and RBAC are enabled.
Kube-apiserver stores Kubernetes objects under `/fabric-root/`; K3s itself also
stores one token-derived bootstrap object under `/bootstrap/`. Its datastore
identity needs read/write access to both prefixes for restart, server join, and
token rotation. Never add a synthetic bootstrap smoke-test key because K3s
expects exactly one bootstrap object.

Once K3s is healthy through the API VIP and all three etcd endpoints are
healthy, attach a trusted Linux machine directly to the fabric LAN. Put
`etcdctl` 3.6.13 from the checksum-pinned archive in
`fabric/router/data-files.txt` on `PATH`.

The phase-one host guard does not expose TCP/2379 to the observer in steady
state. Immediately before running `enable-auth`, open its fixed, memory-only
15-minute maintenance element on each member from an attended session:

```sh
for address in 10.66.0.10 10.66.0.11 10.66.0.12; do
  ssh "sam@$address" sudo /usr/local/sbin/fabric-etcd-maintenance-window \
    --open --confirm OPEN:fabric-etcd-observer:15m
done
```

Then run:

```sh
./enable-auth --apply --confirm ENABLE:fabric-root-etcd
```

Close all three windows afterwards, even if the auth helper failed:

```sh
for address in 10.66.0.10 10.66.0.11 10.66.0.12; do
  ssh "sam@$address" sudo /usr/local/sbin/fabric-etcd-maintenance-window \
    --close --confirm CLOSE:fabric-etcd-observer
done
```

The kernel expires an abandoned element after fifteen minutes and a reboot or
firewall reload clears it. This is an attended break-glass path, not permission
to add observer `.2` to the persistent etcd allow-list.

The guarded helper uses the offline `root` identity to create passwordless
TLS-CN users, grants only `/fabric-root/` and `/bootstrap/` to `fabric-root`,
leaves `fabric-observer` with zero roles, refuses to proceed unless exactly one
K3s bootstrap object already exists, enables auth, and verifies both identities'
positive and negative permissions.

That command is only the first half of the original cluster admission gate.
Afterwards restart one K3s server at a time, confirm the API remains healthy,
and prove a fresh/rebuilt server can join with the retained token. Rehearse
token rotation before any child cluster shares this datastore.

## Add the delegated etcdetcetc client CA

The online etcdetcetc CA is deliberately trusted for **client certificates
only**. The physical CA remains the sole peer CA. The committed `etcd.service`
therefore changes `--trusted-ca-file` to a runtime bundle containing the
physical CA followed by the delegated public CA, while
`--peer-trusted-ca-file=/etc/etcd/pki/ca.pem` remains unchanged. Never put the
delegated CA in peer trust.

The installed roots do not consume later Butane edits automatically. After the
client CA and Butane policy are reviewed, merged, and pushed to `main`, obtain
the confirmation hash without contacting a node:

```sh
scripts/rollout-fabric-etcd-client-trust --check
```

Then roll exactly one voter at a time. Substitute the revision and payload hash
printed by `--check`; do not prepare all three commands in parallel:

```sh
scripts/rollout-fabric-etcd-client-trust --node cp1 \
  --confirm ROLLOUT-FABRIC-ETCD-CLIENT-TRUST:fabric-az1-cp1:<main-commit>:<payload-sha256>
scripts/rollout-fabric-etcd-client-trust --node cp2 \
  --confirm ROLLOUT-FABRIC-ETCD-CLIENT-TRUST:fabric-az1-cp2:<main-commit>:<payload-sha256>
scripts/rollout-fabric-etcd-client-trust --node cp3 \
  --confirm ROLLOUT-FABRIC-ETCD-CLIENT-TRUST:fabric-az1-cp3:<main-commit>:<payload-sha256>
```

Each invocation refuses a dirty, unpushed, or non-`main` checkout; takes the
shared cluster-wide `fabric-maintenance-lock`; stages and hashes only
public policy material; and arms a persistent, reboot-surviving target-local
five-minute rollback. Candidate installation and rollback share one
target-local state lock, recheck the exact deadline before mutation, use
same-directory atomic promotion, and durably verify bytes, metadata, and
SELinux context. The helper restarts one etcd member and proves the exact
three-member/no-learner/no-alarm consensus state, target-local and VIP API
readiness, and a fresh node heartbeat. Because K3s requires the host etcd
service, both the candidate and rollback paths explicitly restore K3s and
refuse acknowledgement until its local API is ready and its Node Lease has
advanced. Rollback loads the restored etcd dependency graph before stopping
the candidate trust renderer, proves it matches the pre-rollout graph, and
explicitly restores etcd and K3s after any resulting stop propagation. The
helper also checks exact reviewed systemd fragments and drop-ins, both live CA
fingerprints, the exact two-certificate runtime bundle, and the live etcd
client/peer trust arguments. Acceptance is persisted only while holding that
same lock after a fresh deadline check and complete target, live-state, and
filesystem-durability proof.
The admin helper and every root, router, pre-open, and post-open foundation
operation take that same ConfigMap lock, so an etcd restart or auth mutation
cannot overlap a network-policy change or acceptance probe.

A failure intentionally leaves the ConfigMap lock and target evidence in place.
Do not delete the lock or start another member merely to retry. First establish
whether `fabric-etcd-client-trust-rollback.timer` ran, inspect
`/var/lib/fabric-etcd-client-trust-rollback`, and re-prove all three endpoints and
the API. Removing a stale lock is an attended cluster mutation and requires the
same explicit authorization as the rollout.

This step does not issue an etcdetcetc certificate, create its etcd user, or
open service-node network access. Those remain separate gates.

## Provision or revoke the delegated administrator

Once all three members have the client bundle, use the offline recovery
identity to create the exact passwordless TLS-CN user expected by the
controller certificate. First validate the static inputs:

```sh
./manage-etcdetcetc-admin --check
```

Install `etcdctl` 3.6.13 from the checksum-pinned archive documented above,
then open the same memory-only observer maintenance window on all three roots
used by `enable-auth`. From the trusted machine with Sam's recovery age key,
run the live read-only plan and copy its exact one-state confirmation:

```sh
./manage-etcdetcetc-admin --plan-provision
./manage-etcdetcetc-admin --provision \
  --confirm PROVISION:fabric-etcdetcetc-admin:<main-commit>:<prestate-sha256>
```

Close every maintenance window immediately afterwards, including after a
failure. The helper refuses to adopt a pre-existing `fabric-etcdetcetc` user,
because it could not prove that user's passwordless origin. A fresh provision
uses `user add --no-password`, grants exactly the built-in `root` role, and
marks provisioning in progress before sending the add RPC. If that RPC commits
but its response is lost, failure cleanup queries the reserved name and removes
only a zero-role or root-only state attributable to this invocation; ambiguity
or an unexpected role binding leaves the global lock for recovery. Before and
after the mutation it proves that `fabric-root` still has exactly one role with only the
`/bootstrap/` and `/fabric-root/` read/write permissions. It also proves the
reserved `root` user, auth state, cluster ID, member IDs, endpoint health, and
an exactly empty alarm list. The normalized empty alarm state is part of the
confirmation pre-state and is read back after mutation.
The confirmation is bound to the freshly fetched main commit and that exact
normalized pre-state, so any intervening authorization or membership change
requires a new plan. Mutation also holds the global Kubernetes
`fabric-maintenance-lock`, shared by trust, root-policy, router, pre-open, and
post-open operations. Failure leaves the lock for attended inspection rather
than inviting a blind retry.

After provisioning, use the live online credential for the routine read-only
proof. This mode does not need an observer maintenance window and never opens
the offline recovery identity:

```sh
./manage-etcdetcetc-admin --verify-remote
```

The verifier requires a clean `main` exactly matching a fresh fetch of
`origin/main`, takes one atomic snapshot of
`Secret/etcdetcetc/fabric-etcdetcetc-admin`, and validates its exact metadata,
CA, client-only leaf, CN, key match, and total 24-hour validity (including only
the explicitly bounded issuance skew) on local verified tmpfs. It
also requires the live physical-server-CA ConfigMap to be byte-identical to
Git and to have the pinned physical CA fingerprint; the Secret's delegated
`ca.crt` is never used as server trust.

No credential crosses SSH. The helper starts one foreground, supervised SSH
child to cp1 with an empty SSH config, the pinned host key, the repo proxy,
agent forwarding disabled, no control socket, `ExitOnForwardFailure`, and
three literal loopback-only local forwards—one to each physical member's
TCP/2379. The resolved local `etcdctl` must be byte-identical to the binary in
the checksum-pinned 3.6.13 Linux/amd64 archive. It runs with an empty
environment and every
endpoint, server CA, leaf, and key argument explicit, so ambient
`ETCDCTL_USER`, password, endpoint, or TLS settings cannot replace the
delegated TLS-CN identity. The helper checks that the same SSH child remains
alive around every health, status, member, alarm, auth, user, and role query.
Its trap ignores a second ordinary signal while terminating and waiting for
that child and removing the local mode-0600 credentials. Finally it re-reads
the Secret and server-CA ConfigMap and requires the same UID, resourceVersion,
and data hash before printing its single revision-bound PASS line. No
Kubernetes, etcd, or root-node file is mutated.

Etcd's `UserGet` response exposes role bindings but not whether the stored user
record also contains a password hash. The verifier therefore proves the live
TLS-CN credential authenticates as `fabric-etcdetcetc` with only the built-in
`root` role, but passwordless provenance remains an attended-history assumption
from the successful `--provision` ceremony. If that history is unavailable,
revoke and freshly provision instead of claiming the stronger property.

The built-in `root` grant is intentional: etcdetcetc must create and revoke
arbitrary tenant users and prefix roles. Consequently the online client CA,
its isolated admission-gated ClusterIssuer, and the controller identity are all
root-equivalent assets. The signer key must remain only in the `cert-manager`
namespace, must never be exposed through an unrestricted Issuer or
ClusterIssuer, and must never be copied into a tenant namespace.

Emergency revocation is explicit and accepts only the expected one-role user:

```sh
./manage-etcdetcetc-admin --plan-revoke
./manage-etcdetcetc-admin --revoke \
  --confirm REVOKE:fabric-etcdetcetc-admin:<main-commit>:<prestate-sha256>
```

Revoking the user does not remove the delegated CA from trust and does not
delete tenant data. Restore controller service only by a fresh, separately
reviewed provision after the incident is understood.
