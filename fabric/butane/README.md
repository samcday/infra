# Fabric consensus node Ignition

This directory is the self-contained Butane source for the three physical
members of the new fabric root cluster. It is additive: nothing here is
included in the current hub Flux fan-out, and nothing merges the current
`common/butane` profiles.

## Shape

- `base.yaml` supplies the administrator SSH key, hash-verified K3s and etcd
  release assets, cluster tokens, and static `/etc/hosts` peer mappings.
- `etcd.yaml` runs one physical etcd member per machine. The service has
  `RequiresMountsFor=/var/lib/etcd`, uses a 2 GiB backend quota, and every
  fresh member explicitly starts with `initial-cluster-state=new`.
- `control-plane.yaml` runs K3s against external etcd under `/fabric-root`.
  It keeps K3s Flannel and kube-proxy, while disabling CoreDNS, Traefik,
  ServiceLB, local-path storage, and metrics-server. The consensus-only phase
  intentionally has no in-cluster DNS. CoreDNS must later be deliberately
  deployed onto workers, with its worker scheduling and any required
  tolerations proven, before workloads. Each root node receives the
  `fabric.samcday.com/root-consensus=true` label and matching dedicated
  `NoSchedule` taint in addition to the K3s control-plane taint. Kube-vip is
  pinned to the dedicated label and tolerates the root taints; Flux waits for
  workers.
- `firewall.yaml` installs a root-only, default-drop nftables guard before
  etcd or K3s. It admits consensus traffic only among the three declared root
  addresses, admits operational/API metrics only from observer `10.66.0.2`,
  fixes kube-proxy to iptables, and makes NodePorts unreachable on roots. Its
  forwarding policy must be redesigned before any worker joins.
- `time.yaml` makes the dedicated router the sole configured chrony source.
  The roots retain their own RTC and persisted drift when the router or WAN is
  unavailable after initial synchronization.
- `node-exporter.yaml` installs the checksum-pinned node_exporter 1.11.1
  binary and exposes bounded host/disk/PSI metrics only on the node's fabric
  address. It has no TSDB and no dependency on etcd or K3s.
- `observer-agent.yaml` uses a dedicated zero-role etcd client certificate to
  list alarms through each member's loopback endpoint, then publishes only
  sanitized textfile metrics through node_exporter. The observer laptop never
  receives etcd network access or a datastore identity.
- `fabric-az1-cp{1,2,3}.yaml` supply the unique hostname, static address with
  IPv4 duplicate-address detection, permanent NIC MAC match, K3s node IP, and
  a distinct etcd member keypair. Each also lays out its selected root
  disk as a 16 GiB FCOS root, a 32 GiB encrypted `/var/lib/etcd`, and encrypted
  `/var` using the rest. Root, etcd, and var unlock through the local TPM, and
  every volume has a recovery key unique to both node and volume. On first
  boot, the root key is enrolled, verified, and then its installed copy is
  deleted; the authoritative copy remains only in SOPS and the encrypted
  offline recovery packet. There is no Tang dependency.
- `base.yaml` carries the host-side half of systemd fix `856ab04a29` for the
  pinned FCOS 44 image. Systemd 259.6 incorrectly puts
  `systemd-pcrnvdone.service` in the real-root graph after the late TPM setup,
  whose `/var/lib/systemd` requirement creates a cycle when `/var` itself is
  encrypted. Ignition cannot retrofit the stock initramfs, so this override
  prevents that misplaced real-root separator from entering the graph; these
  PCR-unbound Clevis policies do not depend on it. A future fixed FCOS
  initramfs will run the separator in the right phase. Remove the override only
  after that build is pinned and all three volumes pass a cold boot.
- `common/discovery.yaml` is an SSH-only, non-cluster discovery profile kept as
  a seed for a future worker provisioner. The three consensus nodes do not use
  it. It deliberately contains no Tailscale, relaxed-security, Tang, or old
  disk-layout profile.
- `kustomization.yaml` provides a structural render check and future profile
  packaging, but `fabric/butane` is not in the root Flux fan-out. Applying it
  online would place sacred-node material inside Bootie-facing Secrets and is
  forbidden.

The static member map is:

| Member | Address | Per-member PKI source |
| --- | --- | --- |
| `fabric-az1-cp1.fabric.internal` | `10.66.0.10` | `fabric/pki/etcd/fabric-az1-cp1.pem.enc` and `fabric-az1-cp1-key.pem.enc` |
| `fabric-az1-cp2.fabric.internal` | `10.66.0.11` | `fabric/pki/etcd/fabric-az1-cp2.pem.enc` and `fabric-az1-cp2-key.pem.enc` |
| `fabric-az1-cp3.fabric.internal` | `10.66.0.12` | `fabric/pki/etcd/fabric-az1-cp3.pem.enc` and `fabric-az1-cp3-key.pem.enc` |

The API VIP is `10.66.0.254` (`api.fabric.internal`). Peer reconnects use the
static node addresses and `/etc/hosts`; router DNS and DHCP are not part of
etcd consensus.

SATA SSDs identified by exact `/dev/disk/by-id/ata-*` paths remain the preferred
default root devices. Sam has explicitly repurposed the inventoried internal
256 GB Samsung NVMes in `fabric-az1-cp1` and `fabric-az1-cp2`. Their sole
accepted identities are, respectively,
`/dev/disk/by-id/nvme-eui.002538839100c827` and
`/dev/disk/by-id/nvme-eui.002538b971048a4f`. Sam has authorized installation
over both devices' existing contents. The armed gate must revalidate the
node-specific exact identity and receive the device-specific confirmation
first. Cp3 remains unadmitted until its reviewed exact ATA or lowercase
NVMe-EUI identity is committed; every non-target internal disk must be
disconnected.

## Offline secret boundary

The secret-bearing checked-in profiles are hydrated from fabric-only material
and SOPS encrypted only to Sam's personal recovery identity. The explicitly
public `firewall.yaml` and `node-exporter.yaml` profiles contain only policy and
checksum-pinned public assets and remain plaintext for review. These are
offline bootstrap inputs, not fabric Flux inputs: the live fabric SOPS identity
must never be able to recover every etcd member key and LUKS recovery key from
Git.

Required inputs are:

- K3s server and agent tokens;
- nine distinct LUKS recovery keys—one each for root, etcd, and var on every
  member—copied to offline recovery media;
- the fabric etcd CA;
- one K3s etcd client certificate and private key;
- the three distinct etcd member certificate/private-key pairs listed above;
- the reserved fabric Flux age identity and deploy key remain offline for the
  later worker bootstrap; they are not embedded in these Ignitions.

The per-member certificates must cover their short name, FQDN, and static IP
and be suitable for both etcd peer and client TLS. Do not substitute a shared
peer private key.

## Rendering the initial quorum

First validate all fragments and final merge structure without producing
usable Ignition:

```sh
./fabric/butane/bootstrap.sh --check
kubectl kustomize fabric/butane >/dev/null
```

`--check` substitutes distinct, globally administered test MACs while it
validates the merge structure, but it does not write or publish Ignition
output. Those structural check identities are never used by normal mode.

After inventory has confirmed the intended root target and permanent wired
MAC for one machine, that node can be rendered independently into a directory
outside the repository:

```sh
FABRIC_CP1_MAC="$CP1_INVENTORY_MAC" \
  ./fabric/butane/bootstrap.sh --node fabric-az1-cp1 \
  /secure/path/fabric-bootstrap
```

Selected-node mode requires only that node's corresponding `FABRIC_CP*_MAC`
variable and publishes only its Ignition. It still decrypts and validates all
nine recovery keys and merges the same fixed three-member etcd map. This is a
manufacturing convenience, not a single-member cluster mode: the resulting
member still declares `initial-cluster-state=new` and must not be started as a
lone cluster.

K3s has an `ExecStartPre` gate against the local unauthenticated metrics
listener and starts only after `etcd_server_has_leader` is exactly `1`. This
keeps a deliberately lone first voter from repeatedly opening client
connections and restarting K3s while it waits for the second voter. Etcd peer
startup itself is not gated, so bringing up either matching remaining member
still forms the initial two-of-three quorum.

The original all-member mode remains available after inventory has confirmed
the permanent wired MAC for all three machines:

```sh
FABRIC_CP1_MAC="$CP1_INVENTORY_MAC" \
FABRIC_CP2_MAC="$CP2_INVENTORY_MAC" \
FABRIC_CP3_MAC="$CP3_INVENTORY_MAC" \
  ./fabric/butane/bootstrap.sh /secure/path/fabric-bootstrap
```

The renderer refuses plaintext secret-bearing profiles, unresolved
placeholders, locally administered or multicast MACs, duplicate MACs in
all-member mode, output paths inside the Git worktree, and existing output
files. It forces the output directory to mode `0700`; its Ignition files
contain private keys and remain mode `0600` on trusted storage. Decryption and
merge work occurs only in a verified tmpfs directory; deletion on an SSD or
CoW filesystem is not treated as secure erasure. The initial three consensus
machines must use these offline-rendered Ignitions; do not load the consensus
profiles into online Bootie.

The Ignitions fetch only pinned public assets from the router USB-backed
`/static/` endpoint: `k3s`, its matching air-gap images and installer, the
pinned kube-vip v1.2.1 OCI archive, the CoreOS K3s SELinux policy RPM, and the
official etcd 3.6.13 and node_exporter 1.11.1 archives. Every source has an Ignition SHA-256 verification
hash. K3s imports both image archives from its agent image directory. The
kube-vip archive carries the exact `ghcr.io/kube-vip/kube-vip:v1.2.1` tag used
by the DaemonSet, whose `imagePullPolicy: Never` prevents a GHCR fallback
during bootstrap. Carry each rendered Ignition to its machine offline and
invoke the installer against the physically verified stable root-disk by-id.
For the preferred/default SATA devices this is an exact
`/dev/disk/by-id/ata-*` path; cp1 and cp2 use their respective exact admitted
`nvme-eui` paths above.
The router never stores rendered Ignitions or any keys. First boot layers the
policy and reboots once before K3s may start.

All three initial members are members of one declared static cluster, so boot
at least two of them together to form quorum. Do not pet a lone first member
and later change the other initial members to `existing`; replacement-member
induction is a different procedure and is intentionally not encoded here.

The 32 GiB etcd partition provides mount and capacity isolation, not a second
physical latency domain: it still shares the selected root device with root and
`/var`.
Inventory, SMART health, and a synchronous-write test remain hard gates before
authorizing a destructive installation.

The phase-one host guard is part of every rendered root Ignition. After quorum,
etcd auth/RBAC, negative host-policy tests, and the separate worker/inter-VLAN
consensus boundary are hard gates before adding any worker or child prefix. The K3s client is limited
to `/fabric-root/` and its required `/bootstrap/` prefix; TCP/2380 is peer-only.
A controlled K3s restart, server rejoin, and token-rotation test must pass after
authorization is enabled. Ceph, KubeVirt, hosted control planes, and
applications never run on these three nodes.
