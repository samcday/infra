# Fabric consensus node Ignition

This directory is the self-contained Butane source for the three physical
members of the new fabric root cluster. It is additive: nothing here is
included in the current hub Flux fan-out, and nothing merges the current
`common/butane` profiles.

## Shape

- `base.yaml` supplies the administrator SSH key, hash-verified K3s and etcd
  release assets, cluster tokens, and static `/etc/hosts` peer mappings.
- `network.yaml` keeps each dedicated static root link configured across
  physical carrier loss. This prevents kube-vip address updates from replacing
  `fabric-static` with a transient, externally assumed NetworkManager profile.
  It also pins K3s-required IPv4 forwarding while rejecting IPv4 redirects,
  redirect transmission, and source routes on every present and future
  interface (and rejects the corresponding IPv6 inputs). With current IPv4
  forwarding, `all.accept_redirects=0` already suppresses receipt, but it does
  not suppress redirect transmission; leaving the default and interface
  values enabled would also reactivate receipt after a host-mode transition.
- `etcd.yaml` runs one physical etcd member per machine. The service has
  `RequiresMountsFor=/var/lib/etcd`, uses a 2 GiB backend quota, and every
  fresh member explicitly starts with `initial-cluster-state=new`. Its client
  trust renderer concatenates the offline physical CA with the public,
  client-only etcdetcetc CA in `/run`; peer trust remains the physical CA
  alone. Existing installed voters receive this public policy only through
  `scripts/rollout-fabric-etcd-client-trust`, one member at a time with a
  cluster lock, target-local rollback, and consensus/API acceptance proofs.
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
  and stages API, kubelet, Flannel, plus TCP/2379 client access for exactly the
  two declared service-node addresses. It separately admits those same exact
  sources to host metrics TCP/2112,2381,9100, while never admitting them to etcd
  peer TCP/2380. It fixes kube-proxy to iptables, keeps new
  root forwarding denied, and makes NodePorts unreachable on roots. Roll the
  combined root policy one member at a time only after the ordinary-Pod source
  qualification passes, and while the live router's legacy rule still rejects
  all three etcd ports. The guarded router transition is the final network-open
  gate and replaces that legacy rule with exact-node TCP/2379 allow followed by
  a TCP/2380 reject. The monitoring aperture has a separate attended router
  rollout because the live router is already beyond the one-time etcd gate.
  During the temporary flat-L2 exception those IP rules
  constrain trusted nodes but are not anti-spoofing isolation; the
  managed-switch VLAN remains mandatory for broader workloads.
  `scripts/rollout-fabric-root-firewall` treats this guard and the routed-host
  sysctl payload as one combined, hash-confirmed root network policy. It rolls
  one member at a time with the shared
  `kube-system/fabric-maintenance-lock`, exact route and interface
  checks, a persistent reboot-surviving five-minute local rollback, exact live
  nftables service-source/port/order proof, token-bound two-phase disarm, and
  API/etcd/heartbeat acceptance checks; the old firewall-only confirmation is
  intentionally no longer valid.
  A failure retains that global lock, preventing delegated trust/admin,
  pre-open, another root rollout, router transition, or post-open work from
  overlapping stale rollback state. Inspect the persistent target evidence at
  `/var/lib/fabric-root-network-policy-rollback` before any attended recovery.
  If that inspection proves the owner is gone and the retained rollback is no
  longer active, `scripts/clear-stale-fabric-maintenance-lock --check` prints a
  commit-, UID-, resourceVersion-, and canonical-state-bound confirmation for
  the only supported removal path. Its live pass requires clean pushed `main`,
  refuses locks younger than thirty minutes and a still-live local holder PID,
  rereads the exact object, and uses both Kubernetes deletion preconditions.
  It cannot prove a remote holder dead; copying its confirmation is the
  operator's explicit attestation that the retained evidence was inspected.
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
  a seed for a future worker provisioner. The temporary cp3 consensus ceremony
  does not publish this profile; it compiles the more restrictive
  `fabric/installer/inventory-live.bu` as its discovery Ignition. This seed
  deliberately contains no Tailscale, relaxed-security, Tang, or old
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
256 GB Samsung NVMes in all three initial members. Their sole accepted
identities are, respectively,
`/dev/disk/by-id/nvme-eui.002538839100c827` and
`/dev/disk/by-id/nvme-eui.002538b971048a4f` for cp1 and cp2, and
`/dev/disk/by-id/nvme-eui.002538bb71b4bb45` for cp3. Cp3's device is model
`MZVLW256HEHP-000L7`, serial `S35ENX0JB81948`, exact size `256060514304`, in
the native-TPM2 replacement M710q chassis `S4FQ1133`. Its synthetic
synchronous-write targets were accepted only through a separately preserved
external evidence exception after flush/FUA and integrity checks passed. That
exception admits the exact disk but does not replace the separate one-use
installation arm. Sam has authorized installation over the cp1 and cp2
devices' existing contents; every node's armed gate must revalidate its exact
identity and receive its device-specific confirmation first. Every non-target
internal disk must be disconnected.

## Offline secret boundary

The secret-bearing checked-in profiles are hydrated from fabric-only material
and SOPS encrypted only to Sam's personal recovery identity. The explicitly
public `firewall.yaml`, `network.yaml`, `node-exporter.yaml`, and `time.yaml`
profiles contain only policy and checksum-pinned public assets and remain
plaintext for review. These are offline bootstrap inputs, not fabric Flux
inputs: the live fabric SOPS identity must never be able to recover every etcd
member key and LUKS recovery key from Git.

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
CoW filesystem is not treated as secure erasure. All three consensus Ignitions
must still be rendered by this offline path; do not publish the constituent
consensus profiles through Kubernetes Secrets or a general-purpose Bootie
instance.

Cp1 and cp2 carry their complete rendered Ignitions on node-local installer
media. For the bounded cp3-only exception in `fabric/bootie/README.md`, one
complete `fabric-az1-cp3.ign` may instead be mounted read-only from verified
tmpfs into the temporary observer-hosted station. That file is served only
after discovery has been cleared, the exact disk has been independently
authorized, and a one-use install arm has been consumed. It must never be
copied to Git, a Kubernetes Secret, the router, the PXE staging tree, or
durable station storage. Cp1 and cp2 material must never be mounted there.

The Ignitions fetch only pinned public assets from the router USB-backed
`/static/` endpoint: `k3s`, its matching air-gap images and installer, the
pinned kube-vip v1.2.1 OCI archive, the CoreOS K3s SELinux policy RPM, and the
official etcd 3.6.13 and node_exporter 1.11.1 archives. Every source has an Ignition SHA-256 verification
hash. K3s imports both image archives from its agent image directory. The
kube-vip archive carries the exact `ghcr.io/kube-vip/kube-vip:v1.2.1` tag used
by the DaemonSet, whose `imagePullPolicy: Never` prevents a GHCR fallback
during bootstrap. Cp1 and cp2 receive their rendered Ignitions through their
node-local media; cp3 may use the temporary network handoff above. In either
case, invoke the installer only against the physically verified stable
root-disk by-id. For the preferred/default SATA devices this is an exact
`/dev/disk/by-id/ata-*` path; cp1, cp2, and cp3 use their respective exact
admitted `nvme-eui` paths above.
The router and persistent PXE staging tree never store rendered Ignitions or
any keys. First boot layers the policy and reboots once before K3s may start.

All three initial members are members of one declared static cluster, so boot
at least two of them together to form quorum. Do not pet a lone first member
and later change the other initial members to `existing`; replacement-member
induction is a different procedure and is intentionally not encoded here.

The 32 GiB etcd partition provides mount and capacity isolation, not a second
physical latency domain: it still shares the selected root device with root and
`/var`.
Inventory, SMART health, and a synchronous-write test remain hard gates before
authorizing a destructive installation.

The root host guard is part of every rendered root Ignition. After quorum,
etcd auth/RBAC and negative host-policy tests are hard gates before the two
named service agents use the bounded flat-L2 exception. The worker/inter-VLAN
consensus boundary remains a hard gate before any other worker, child etcd
identity, or tenant workload.
The K3s client is limited to `/fabric-root/`, its required `/bootstrap/` keys,
and K3s's read-only `/bootstrap` scan range; TCP/2380 is peer-only.
A controlled K3s restart, server rejoin, and token-rotation test must pass after
authorization is enabled. Ceph, KubeVirt, hosted control planes, and
applications never run on these three nodes.
