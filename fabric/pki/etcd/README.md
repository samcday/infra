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

This command is only the first half of the admission gate. Afterwards restart
one K3s server at a time, confirm the API remains healthy, and prove a
fresh/rebuilt server can join with the retained token. Rehearse token rotation
before any child cluster shares this datastore. Do not create workers or a
child-cluster prefix until those checks pass.
