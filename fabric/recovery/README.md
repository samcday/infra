# Fabric etcd snapshots

This directory provides a guarded, off-cluster snapshot workflow for the
physical fabric root. It does not mutate etcd and it is not connected to Flux.
Both helpers require the exact `etcdctl`/`etcdutl` 3.6.13 binaries from the
checksum-pinned archive listed in `fabric/router/data-files.txt`.

Run these commands on a trusted Linux operations machine directly connected to
the fabric LAN. The destination must be an encrypted off-cluster filesystem,
not a root node, the router, a Git worktree, or the observer laptop's ephemeral
disk. Create its dedicated directory before decrypting any identity:

```sh
install -d -m 0700 /media/encrypted/fabric-etcd-snapshots
install -d -m 0700 /secure/fabric-etcd-root
umask 077
```

Decrypt `ca.pem.enc`, `root.pem.enc`, and `root-key.pem.enc` from
`fabric/pki/etcd` into `/secure/fabric-etcd-root`. Never put plaintext PKI in
this repository. All three files must be owned by the caller and mode `0400`
or `0600`; the helper verifies their chain, client purpose, and key match
without printing their contents or the private-key fingerprint. It also
requires the dedicated `CN=root` recovery identity; the K3s `fabric-root`
identity is deliberately not accepted.

## Save through one member

Select one member explicitly. The helper accepts only the current member
addresses (`10.66.0.10`, `.11`, or `.12`) and never expands the request to the
cluster. The root host firewall normally denies observer TCP/2379. From an
attended SSH or console session on only the selected member, open its fixed
15-minute window before `--apply`, then close it immediately afterwards:

```sh
ssh sam@10.66.0.10 sudo /usr/local/sbin/fabric-etcd-maintenance-window \
  --open --confirm OPEN:fabric-etcd-observer:15m
```

The window is memory-only and expires automatically; it is unnecessary for
the local-only check below. First perform that validation:

```sh
./snapshot-etcd \
  --endpoint https://10.66.0.10:2379 \
  --cacert /secure/fabric-etcd-root/ca.pem \
  --cert /secure/fabric-etcd-root/root.pem \
  --key /secure/fabric-etcd-root/root-key.pem \
  --etcdctl /secure/etcd-v3.6.13/etcdctl \
  --etcdutl /secure/etcd-v3.6.13/etcdutl \
  --output-dir /media/encrypted/fabric-etcd-snapshots \
  --check
```

Omitting a mode is the same as `--dry-run`: local checks and a safe plan, with
no network connection and no files. To take the snapshot:

```sh
./snapshot-etcd \
  --endpoint https://10.66.0.10:2379 \
  --cacert /secure/fabric-etcd-root/ca.pem \
  --cert /secure/fabric-etcd-root/root.pem \
  --key /secure/fabric-etcd-root/root-key.pem \
  --etcdctl /secure/etcd-v3.6.13/etcdctl \
  --etcdutl /secure/etcd-v3.6.13/etcdutl \
  --output-dir /media/encrypted/fabric-etcd-snapshots \
  --apply --confirm SNAPSHOT:fabric-root-etcd
```

Close the selected member's window even if snapshot creation failed:

```sh
ssh sam@10.66.0.10 sudo /usr/local/sbin/fabric-etcd-maintenance-window \
  --close --confirm CLOSE:fabric-etcd-observer
```

`--apply` first requires one healthy response, one non-leaderless status, and
server version 3.6.13 from the selected endpoint. It then invokes exactly one
`etcdctl snapshot save`, validates the resulting database with `etcdutl`, and
creates a new mode-0700 timestamped bundle. A collision is an error; nothing is
overwritten. A failed or interrupted run removes its incomplete bundle.

Each successful bundle contains only mode-0600 files:

- `snapshot.db`;
- the selected endpoint's health and status JSON;
- `snapshot-status.json` produced by `etcdutl`;
- `evidence.json`, including UTC time, endpoint, pinned versions, non-secret
  CA/client-certificate fingerprints, and snapshot hash; and
- `SHA256SUMS`, covering the snapshot and every evidence file.

Copy the whole directory, preserving modes, to a second encrypted off-site or
otherwise independent store. Do not count a snapshot that exists only beside
the cluster as disaster-recovery evidence.

## Verify after copying

The independent verifier has no credentials and never contacts a network. A
cheap structural check does not read/hash the entire snapshot:

```sh
./verify-etcd-snapshot \
  --bundle /media/encrypted/fabric-etcd-snapshots/fabric-etcd-YYYYMMDDTHHMMSSZ \
  --etcdutl /secure/etcd-v3.6.13/etcdutl \
  --check
```

Full acceptance requires `--verify`; it recomputes all five hashes, asks the
pinned `etcdutl` to inspect `snapshot.db`, and requires that status to equal
the recorded status:

```sh
./verify-etcd-snapshot \
  --bundle /media/encrypted/fabric-etcd-snapshots/fabric-etcd-YYYYMMDDTHHMMSSZ \
  --etcdutl /secure/etcd-v3.6.13/etcdutl \
  --verify
```

This proves that the saved artifact remains internally readable and matches
its evidence. It does not prove that a three-member restore can elect a leader
or serve Kubernetes data. That remains a separate isolated restore rehearsal:
never restore over a live member directory, and do not treat these helpers as
authorization to start an etcd process or alter a cluster.
