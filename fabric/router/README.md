# fabric router

This overlay builds an isolated OpenWrt image for the OpenWrt One. The
temporary degraded-hardware profile uses `radio0` as a 2.4 GHz upstream
station and logical WAN, `radio1` as the 5 GHz operator AP, and `eth1` as the
only fabric Ethernet link. The failed 2.5G `eth0` port remains physically
disconnected and unassigned. The LAN is the non-advertised `10.66.0.0/24`
root network plus the allocated `10.66.1.0/24` services network. During the
explicit `unmanaged-switch-flat-l2` migration profile, both prefixes live on
the same `br-lan`/`eth1` physical L2. This preserves the final service-node
addresses without pretending that unmanaged switches provide a security
boundary.

Build from the repository root:

```sh
scripts/build-router-image.sh fabric/router mediatek filogic openwrt_one
```

The fabric overlay independently selects OpenWrt 25.12.5 through
`openwrt-version`; other router overlays retain the build script's existing
default. The normal artifact for an OpenWrt One that already boots its factory
OpenWrt is:

```text
openwrt-25.12.5-mediatek-filogic-openwrt_one-squashfs-sysupgrade.itb
```

Its reviewed build hash is recorded in `sysupgrade-sha256`. The current image
was rebuilt twice from the pinned ImageBuilder and produced identical bytes;
verify that record from the artifact directory before any flash.

The image contains the SOPS-managed `fabric-observer` WPA3 credential and the
upstream WLAN credential and is therefore secret-bearing. Keep the generated
ITB mode `0600` on encrypted or tmpfs storage, never publish it as a CI
artifact, and remove the plaintext ImageBuilder tree after copying and
verifying the selected image. The encrypted source remains recoverable only
with Sam's offline age identity.

Install it without retaining factory settings (`sysupgrade -n`). Do not use a
`factory.ubi`, preloader, or bootloader artifact for an ordinary first
deployment. Those belong to the documented NAND/NOR recovery paths.

For an attended upgrade of an already commissioned fabric image, preserve the
existing SSH host identity and operator attachment with ordinary `sysupgrade`
(without `-n`) and require `sysupgrade -T` to accept the staged image. OpenWrt
preserves `/etc/config/network`, so the new first-boot default deletes every
saved network section and reconstructs only the reviewed loopback, globals,
LAN, WAN, disabled WAN6, and `br-lan`/`eth1` device sections. It asserts every
section, option, and value before configuring DHCP, HTTP, firewall, or Wi-Fi;
stale aliases, routes, policy rules, VLANs, and physical-device bindings cannot
survive. The globals section retains packet steering but deliberately has no
ULA prefix because the fabric router disables IPv6. Keep USB-C serial attached
throughout this attended transition and do not rely on the pre-upgrade network
path returning until first boot completes.

The image deliberately:

- uses `radio0` only as a 2.4 GHz station on the existing upstream WLAN and
  attaches that station only to the logical WAN;
- exposes one low-power 5 GHz WPA3-SAE/802.11w SSID on `radio1` named
  `fabric-observer`, bridged to the fabric LAN for the sole admitted desktop
  Wi-Fi MAC;
- caps the observer SSID at one association, removes every stock wireless
  interface, permits no wireless interface other than these two, and disables
  WPS, IPv6 prefix delegation, FRR, Tailscale and Tang;
- has no route advertisement or Tailscale-to-LAN forwarding;
- gives both LAN and WAN reject-by-default input/forward policies, with named
  IPv4 catch-all rejects for deterministic fw4 counters, no WAN-to-LAN
  forwarding, and no inherited stock WAN exceptions;
- blocks the roots and services from reaching RFC1918 or Tailscale/CGNAT
  `100.64.0.0/10` destinations through the WAN, while granting public egress
  to the three root addresses and TCP/80+443 public egress to only
  `10.66.1.10-10.66.1.11`;
- permits key-only OpenSSH from observer `10.66.0.2` and no other LAN source;
  password authentication and every SSH forwarding mode are disabled, and
  Dropbear is absent;
- permits router DNS, NTP, and pinned-asset HTTP on `10.66.0.1` only from the
  observer and three roots, and on `10.66.1.1` only from the two service nodes;
- disables LAN DHCPv4, DHCPv6, router advertisements, and NDP proxying; the
  `odhcpd` server is absent, and the router, observer, consensus, and service
  nodes use reviewed static addresses;
- pins IPv4 forwarding on while disabling IPv4/IPv6 redirect acceptance, IPv4
  redirect generation, source routes, and the IPv6 stack itself through the
  uniquely named image-owned `/etc/sysctl.d/90-fabric-flat-l2.conf`; IPv6
  `accept_source_route=-1` rejects Routing Header type 2, which kernel value
  `0` would still accept. An old preserved
  `/etc/sysctl.conf` cannot erase the policy during ordinary sysupgrade and
  same-interface routing cannot teach a node a route around fw4;
- leaves DHCP/TFTP PXE disabled until a worker-only, authenticated provisioner
  exists; the unused iPXE binaries are absent and the initial consensus
  Ignitions are carried offline.

The fabric overlay also subtracts common Tailscale, Tang, FRR, Dropbear,
`odhcpd`, iPhone tether, generic USB-Ethernet hotplug hooks, and the matching
CDC USB-network drivers at image-build time. Those packages and escape-path
hooks are absent, not merely inactive; USB storage remains for the pinned asset
device.
LuCI and the unused router Prometheus exporter are force-removed; bare `uhttpd`
binds only `10.66.0.1:80` and `10.66.1.1:80` and serves the pinned public files
beneath `/static/` with directory listings disabled and no web administration
surface.

This chassis has prior lightning damage and its labelled 2.5G `eth0` port is
treated as failed. Leave that jack empty and leave `eth0` outside every UCI
network, bridge, and firewall zone. The temporary uplink is the `radio0`
2.4 GHz station, which obtains the logical WAN address from the existing
upstream WLAN. Connect the labelled 1G `eth1` LAN port to the measured
five-port root switch. A second unmanaged five-port switch may be daisy-chained
from it for exactly `fabric-az1-svc1` and `fabric-az1-svc2`; both switches are
one broadcast domain. `radio1` remains the observer AP and must never join the
WAN.

Power the OpenWrt One through its USB-C power input from the measured extension
board. Do not use PoE on the damaged jack or connect a fabric switch port
directly to the existing hub/home LAN. Only the two reconstructible, trusted
platform agents are admitted during this flat-L2 exception; tenant, lab,
KubeVirt, and other untrusted nodes still wait for the managed-switch boundary.
This radio-WAN and flat-L2 layout is a temporary hardware exception, not the
permanent root-router design. Replace the lightning-damaged unit with
known-good wired hardware before fabric becomes authoritative; OpenWrt Two is
a candidate only after it is available, supported, and passes the same
commissioning and failure gates.

The firewall overlay deletes every inherited rule, forwarding, redirect, NAT,
and include before constructing this policy. Four explicit IPv4 catch-all
rejects mirror the zone defaults so WAN input, fabric-to-WAN, WAN-to-fabric,
and fabric router-input denials have named fw4 counters. IPv6 is disabled by
network configuration and sysctl and remains denied by the zone defaults as
defense in depth. The two IPv4 prefixes route through the same LAN zone; exact
address-pinned rules permit API TCP/6443 from the two service nodes only to the
VIP `10.66.0.254` and the three K3s server addresses `10.66.0.10-.12`, plus
bidirectional Flannel UDP/8472, bidirectional kubelet TCP/10250, and attended
observer `10.66.0.2` SSH to the two exact service addresses. The direct API
destinations are required because the K3s supervisor advertises its individual
server endpoints after an agent initially joins through the VIP. During one
attended install, those two service addresses may also fetch the live rootfs
from Bootie at exactly `10.66.0.2:80`. The etcdetcetc foundation exception adds
one earlier exact-source/exact-destination TCP/2379 client allow for only those
two service nodes and three roots, immediately followed by an exact reject for
the same endpoints on internal TCP/2380 and TCP/2381. Per-direction
cross-prefix catch-all rejects follow. IPv4 ICMP redirects are disabled so
ordinary hosts retain the router path. Same-subnet traffic still bypasses the
router and is constrained separately by the FCOS root and services host
tables. These rules admit or reject new routed flows: fw4's global
established/related path runs before `forward_lan`, and a reload does not
retroactively evict conntrack. The root `fabric_guard` host policy is the
enforcement layer that kills stale etcd flows; bring-up and incident gates must
not treat a router reload as session teardown.

The marker in `/etc/config/fabric` names this
`unmanaged-switch-flat-l2` profile and its
`managed-vlan-trunk-ready` removal gate. When the managed switch arrives, do
not retain the marker or same-zone rules: move `10.66.1.1/24` to its VLAN
interface, put services in a distinct firewall zone, and preserve the current
IP/port policy as inter-zone rules. No node readdressing is required.

OpenWrt 25.12 base-files materializes one `dhcp_default_duid` DUID-UUID in the
network globals after the image's `uci-defaults` scripts have completed. The
commissioning verifier admits only that exact post-boot option and validates
its type and lowercase hexadecimal shape. It does not relax the separate
absence checks for an IPv6 ULA, IPv6 addresses, routes, delegation, RA, or
DHCPv6 service.

The inherited common-router DNS default also pins the WAN resolver list to
Cloudflare rather than accepting the upstream DHCP server's resolver. The
verifier checks those exact IPv4 and IPv6 list values. The IPv6 entries are
dormant configuration: `wan6` remains disabled and the IPv6 stack, routes,
addresses, RA, DHCPv6, and delegation are independently required absent.

## Initial install and recovery boundary

For normal operation leave the rear boot selector at `NAND`. The preferred
first deployment is a wired staging connection or USB-C serial console at
115200 8N1, followed by `sysupgrade -n` of the generated sysupgrade image.
Verify the artifact SHA-256, key-only SSH, LAN addresses `10.66.0.1` and
`10.66.1.1`, exact
degraded role mapping (`radio0` WAN station, `radio1` observer AP, `eth1`
fabric LAN, and unassigned `eth0`), and isolation rules before moving the three
nodes behind it.

The official reset-button USB workflow is also suitable. Put only the
sysupgrade image on an MBR/FAT32 stick and rename it exactly:

```text
openwrt-mediatek-filogic-openwrt_one-squashfs-sysupgrade.itb
```

Prepare that stick with the guarded helper rather than partitioning or copying
it by hand. Its default invocation is read-only and prints the exact
device-specific confirmation required for the destructive pass:

```sh
scripts/prepare-fabric-router-firmware-usb \
  --image /dev/shm/fabric-router-current-image/openwrt-25.12.5-mediatek-filogic-openwrt_one-squashfs-sysupgrade.itb \
  --device /dev/disk/by-id/usb-VENDOR_MODEL_SERIAL-0:0
```

The helper verifies the mode-0600 artifact against `sysupgrade-sha256`, accepts
only a stable removable USB whole-disk identity with a hardware serial, and
refuses mounted, held, read-only, non-USB, non-removable, partition, and system
disks. Physically compare its reported model, serial, size, and by-id path, then
copy the printed `sudo ... --apply --confirm ...` command exactly. Apply creates
one bootable FAT32 LBA partition in a DOS/MBR table, labels it `OPENWRT_FW`,
copies only the required reset-button filename, and unmounts before remounting
read-only to verify both the sole-file invariant and reviewed hash. It flushes
and leaves the device unmounted. Do not use `/dev/sdX`, weaken the removable
check, or reuse a stick that has ever held a secret fabric node installer. The
prepared firmware stick is itself secret-classified: the image contains the
decrypted fabric operator configuration. Keep it attended during the physical
flash and either retain it as protected recovery media or erase it afterward.

With power removed, insert the stick into the Type-A port, hold the rear Reset
button, apply power, and release Reset when all LEDs go dark. Wait for the
normal green indication before reconnecting. The OpenWrt One has one Type-A
host port, so this firmware stick and the ext4 fabric-data stick below are
separate roles: finish and verify the firmware installation, remove the FAT32
stick, then insert the data device.

The front button with the rear selector at `NAND` boots the built-in recovery
environment. Full NAND recovery uses the `NOR` selector and the official
preloader plus `factory.ubi` procedure; leave NOR untouched during ordinary
installation. Record the verified sysupgrade image hash with the physical
router inventory before flashing.

Use temporary bench power for flashing and staging, outside the candidate
SM-732-A and final extension board. Move the router into the measured domain
only for the attended full-domain test after the power record reaches
`assembly_qualified`; unattended or operational use waits for `accepted`.

## Pre-root network admission

The `fabric-observer` interface on `radio1` is a narrow operator attachment,
not a general management WLAN. It extends the trusted consensus L2 over the
air, so possession of the SAE secret plus MAC spoofing can still enable ARP
disruption. Keep it low-power, one-client, and time-bounded; do not associate
roots or workers. The `radio0` upstream station belongs only to the logical WAN
and must not be bridged to the observer or fabric LAN.

Before moving `sam-desktop`'s Wi-Fi radio, prove its wired home interface has
carrier and owns the only ordinary default route. Place the Wi-Fi PHY in a
dedicated network namespace, join `fabric-observer` with the permanent admitted
MAC, and assign only `10.66.0.2/24`. The namespace receives no gateway, DNS,
IPv6, default route, veth link, bridge, NAT, or Tailscale interface. Run fabric
SSH, SCP, curl, and the temporary observer inside that namespace. This remains
isolated even when unrelated host workloads require global IPv4 forwarding.

Do not use a normal dual-homed NetworkManager profile as the final boundary.
Before connecting, verify the root namespace has only the wired home default;
inside the observer namespace, `ip -4 route get 10.66.0.1` must select the Wi-Fi
interface with source `10.66.0.2`, while `ip -4 route get 1.1.1.1` must fail.
The SOPS-managed passphrase must be supplied without printing it or placing it
in shell history.

After first boot completes without a failed `uci-defaults` script, obtain the
router's ED25519 host-key fingerprint from the attended USB-C serial console:

```sh
ssh-keygen -E sha256 -lf /etc/ssh/ssh_host_ed25519_key.pub
```

Copy that complete `SHA256:...` value exactly. Do not enroll a key obtained only
with `ssh-keyscan`: the scan is unauthenticated until it matches the serial
fingerprint. From the desktop root namespace, re-verify the complete observer
boundary, then run the read-only commissioning snapshot inside it:

```sh
sudo fabric/observer/fabric-observer-netns verify
sudo ip netns exec fabric-observer \
  scripts/verify-fabric-router \
    --serial-ed25519-fingerprint 'SHA256:VALUE_FROM_SERIAL' \
    --identity /var/home/sam/.ssh/id_ed25519
```

The verifier refuses its first SSH connection until the scanned ED25519 key
matches the serial fingerprint. It uses only that host key and the named
mode-`0600` identity, asserts the reviewed UCI/package/service/firewall/mount
state without reading the wireless key, and retains protected evidence in
tmpfs. The live firewall gate rejects extra nftables tables, executable
automatic includes, and runtime fw4 UBus/include inputs; it also proves the
reviewed IPv4 rules' exact chain, order, terminal accept/reject jump, and
anonymous counter. The topology gate must also prove that `eth0` is unassigned,
`eth1` is the sole wired LAN bridge member, `radio0` is the sole logical-WAN
station, and `radio1` is the sole observer AP. It must also prove the exact two
LAN prefixes, migration marker, redirect sysctls, same-zone service/root rule
order, service WAN restriction, and absence of broad forwardings or redirects.
It derives the seven-file asset
manifest locally, checks exact remote membership and bytes, streams and hashes
every HTTP response,
requires exact answers for all six fabric DNS records over UDP and TCP, and
obtains an NTP sample. It does not trust the router data stick's own
`SHASUMS.txt` in isolation.

Fit and verify a CR1220 in the OpenWrt One RTC holder before the whole-domain
loss test. The roots use `10.66.0.1` as their only configured NTP source; after
they synchronize once, loss of the router or WAN leaves them on their own RTCs
and chrony drift state and does not enter the etcd peer reconnect path.

The source-role matrix remains a separate attended gate. Do not repurpose the
fixed observer helper or add ad-hoc routes to its namespace. With no real root
attached, a guarded, trap-restored procedure must exercise observer `.2`, each
admitted root address `.10` through `.12`, service addresses `10.66.1.10-.11`,
the active inventory address `10.66.0.100`, another unauthorized address in
each prefix, and a controlled WAN-side client. It must prove router-service
source restrictions, observer and unauthorized forwarding denial, exact
API/VXLAN/kubelet plus observer-to-service SSH same-interface forwarding, and
service-node TCP/80 to the exact Bootie observer address while adjacent ports,
hosts, and unauthorized sources fail. It must also prove explicit etcd denial,
root public egress, service-only public TCP/80+443 egress, private and CGNAT
denial, WAN-input denial, and WAN-to-fabric denial using known-live
destinations plus deltas on the corresponding named fw4 counters. Do not claim
a separate masquerade counter: successful active public probes together with
increased source allow counters prove the permitted NAT paths.
After restoration, rerun both the namespace verifier and the read-only
snapshot. Also record that the upstream/home network has no route to
`10.66.0.0/24` and no Tailscale subnet route advertises it. This gate remains
open until that evidence is captured; do not attach a consensus node first.

Run the fabric-source half of that matrix only with a dedicated,
identity-pinned, otherwise-idle physical Ethernet interface on the isolated
fabric switch. Use a separate controlled upstream-side client on the same
upstream/home L2 as the `radio0` station for the WAN-source half. That client
may be a dedicated wired interface if the AP bridges WLAN and Ethernet without
client isolation; otherwise use a separately administered upstream WLAN
station. Before accepting a WAN denial, prove with a router-side capture or the
named firewall-counter delta that the probe actually reached `radio0`.

The historical pre-install helper `scripts/qualify-fabric-service-sources` is
retired now that both service nodes are admitted. It simulated the service
addresses from a desktop NIC and required all three etcd ports to be rejected;
that result is phase-invalid once the desired policy admits exact-node
TCP/2379. The executable remains only as a fail-closed pointer to its
replacement and cannot emit a passing result.

Before installing the router and root TCP/2379 exceptions, run the guarded
replacement while the live router still carries the exact
`Reject-services-to-root-etcd` rule:

```sh
sudo scripts/qualify-fabric-etcd-pod-sources \
  --check \
  --serial-ed25519-fingerprint SHA256:VALUE_FROM_ROUTER_SERIAL \
  --identity /var/home/sam/.ssh/id_ed25519

# Copy the commit-bound confirmation printed by --check exactly.
sudo scripts/qualify-fabric-etcd-pod-sources \
  --run \
  --serial-ed25519-fingerprint SHA256:VALUE_FROM_ROUTER_SERIAL \
  --identity /var/home/sam/.ssh/id_ed25519 \
  --confirm 'QUALIFY-FABRIC-ETCD-POD-SOURCES:<pushed-main-commit>:svc1=10.66.1.10:svc2=10.66.1.11'
```

The replacement creates two restricted, non-hostNetwork Pods in one temporary
namespace and binds one to each exact service node. Each Pod targets all three
exact roots on TCP/2379, and all six attempts must still fail. A temporary
nftables base chain at an earlier hook priority records the source address
independently for every service-node/root pair and has no packet verdict, so
the existing fw4 reject remains authoritative.
The router forward hook sees these packets only after Flannel/K3s has applied
service-node egress SNAT, which makes the recorded address the value the
router and root allow-lists will actually enforce. The named reject counter
must increase without any semantic fw4 change. Only `svc1=10.66.1.10` and
`svc2=10.66.1.11` pass;
any Pod address, different SNAT result, missing observation, or successful
connection stops the rollout. Never respond by adding a PodCIDR to either
router or root policy.

The helper owns both the host-local Fabric network-operation lock and the
cross-workstation `kube-system/fabric-maintenance-lock` ConfigMap and
refuses pre-existing objects. The global lock serializes it with delegated
trust/admin, root-policy, router, and post-open maintenance; a failed live run
retains that lock for inspection. Its EXIT trap UID-checks and removes the
Kubernetes namespace,
deletes the complete temporary nftables table, rechecks both absences, and
verifies the observer namespace before it can report `PASS`. It also normalizes
the before and after fw4 JSON by removing only rule handles and counter values;
every remaining semantic byte must match, and the exact old UCI/live rule is
checked again after the probes. A successful read-only `--check` removes its
temporary evidence; keep the protected evidence from the attended run with the
rollout record. This narrow check proves only the ordinary-Pod source
translation needed for the etcdetcetc exception; the full observer, root,
inventory, service, WAN, egress, and reboot matrix above remains a separate
commissioning obligation.

That standalone run is an early discovery gate, not a durable attestation.
The final router `--run` repeats the same two-Pod proof through a hidden
parent-only entry point while the router process continuously owns both locks.
The child proves the inherited flock descriptor plus the exact ConfigMap
holder, UID, and resourceVersion, performs and cleans the probes, and returns
those same identities in its protected result. It cannot acquire or release
the parent locks. The router then revalidates the result and both locks before
it can stage any packet-opening change.

The live router does not consume a later `uci-defaults` edit. After the
Pod-source qualification above passes, stage the config child with the
controller still doubly suspended, roll delegated client trust through `cp1`,
`cp2`, and `cp3`, provision the exact admin user, and roll the combined root
host policy one member at a time. Keep the router's old reject throughout all
of those steps. Only then perform the final network-open transition:

```sh
sudo scripts/rollout-fabric-router-etcd-policy \
  --check \
  --serial-ed25519-fingerprint SHA256:VALUE_FROM_ROUTER_SERIAL \
  --identity /var/home/sam/.ssh/id_ed25519

# Copy the revision-and-payload confirmation printed by --check exactly.
sudo scripts/rollout-fabric-router-etcd-policy \
  --run \
  --serial-ed25519-fingerprint SHA256:VALUE_FROM_ROUTER_SERIAL \
  --identity /var/home/sam/.ssh/id_ed25519 \
  --confirm 'ROLLOUT-FABRIC-ROUTER-ETCD-POLICY:<pushed-main-commit>:<payload-sha256>'
```

Both modes require a clean checkout exactly at fetched `origin/main`, run the
complete read-only `verify-fabric-etcd-pre-open` contract, prove the committed
and live root guards, verify observer isolation, pin the first SSH connection
to the ED25519 fingerprint read over serial, and require the exact legacy UCI
and live-fw4 rule. That pre-open contract requires the exact Flux revision and
config-active suspension phase, a registry- and source-tree-bound hardened
image, current dedicated issuers/admin leaf and fail-closed admission policy,
all three legacy releases suspended, exact delegated trust and root policy on
all voters, and the exact delegated etcd admin/fabric-root authorization
state. The run also owns `/run/lock/fabric-network-operation.lock` and the same
`kube-system/fabric-maintenance-lock` used by the pre-open, root-policy,
post-open, delegated-trust, and admin helpers. A stale shared lock is a hard
stop, including when it was left by a failed operation on another workstation.
Under both locks it reruns the complete pre-open contract before and after the
fresh Pod-source proof and compares all three normalized root guards with the
preflight snapshots. The ConfigMap lock is bound continuously to the exact UID
and resourceVersion returned by creation and is released only with Kubernetes
DELETE preconditions, so a same-name or same-holder replacement can neither be
adopted nor deleted. Only then does the helper derive a byte-exact UCI candidate
from `etcd-client-transition.uci` and rejects any pending UCI delta. After that
non-live candidate and rollback program are safely staged, it repeats the full
pre-open proof again. Immediately before arming and again immediately before
the packet-opening apply, a narrow activation fence freshly fetches
`origin/main`, requires both `HEAD` and the remote-tracking ref to remain the
confirmation-bound revision, rechecks the exact Flux source and all seven
activation Kustomizations, and proves the controller HelmRelease, CRDs, Pods,
and every standard Pod-producing workload kind remain absent. It stores the
exact old config in a root-only persistent directory, installs the reviewed
bidirectional TCP/2379 early-drop guard as the sole automatic nftables include,
and enables a reboot-surviving procd watchdog before any opening UCI mutation.
The guard remains effective through commit, reload, all remote proofs, SSH
loss, and reboot. Deadline or force atomically chooses a fenced terminal state;
the watchdog then retains or reloads the guard and restores the old reject. It
never opens packets merely because the main SSH process stopped.

Treat the attended transition as a short repository write freeze: from the
final `--check` through the `--run` result, no operator or automation may push
`main`, move the local checkout, or reconcile an activation override. The
helper detects every such change it can observe before apply, but no distributed
check can prevent a push in the final instant after its last fetch.

Acceptance requires the installed config to match the staged candidate
byte-for-byte, `fw4 check`, exact target section and complete `forward_lan`
ordering, one anonymous counter on each new rule with no packet observed before
acceptance, a healthy Fabric API, and unchanged observer isolation. The main
process then creates the token-bound accepted terminal state while the guard is
still exact and persistent. The watchdog is the sole packet-opening writer: it
revalidates the contract, removes the persistent guard, reloads the candidate,
proves exact open state, records `accept-complete`, and continuously reconciles
that exact state until the caller obtains fresh proofs, stops it, disables it,
and proves exact open state again. Only then are the watchdog, backup, include,
and work directory removed. A crash cannot make open policy durable before
accepted authorization and exact-open completion. On failure, stop: use serial
to inspect `/etc/fabric-router-etcd-policy-rollback`,
`/etc/init.d/fabric-router-etcd-policy-rollback`, the live UCI sections, and
the live `forward_lan` chain. Do not delete stale evidence merely to retry.
After a pass, rerun the serial-pinned commissioning verifier; its desired
policy now requires the TCP/2379 allow followed immediately by the
TCP/2380-2381 reject.

This router operation is the final network-open gate. It does not issue a
certificate or activate the controller. Keep both controller suspension gates
closed until the staged delegated certificate and admin-user ceremony in
`fabric/cluster/etcdetcetc/README.md` is complete. After the controller installs
its CRDs and the separate runtime child activates the EtcdCluster and smoke
Tenant, `scripts/qualify-fabric-etcd-post-open` is the acceptance gate for the
real service-Pod, port, mTLS, and prefix-RBAC path; a static router pass is not
a substitute.

The post-open helper repeats the serial-pinned exact UCI/full-`forward_lan`
inspection before and after its probes and compares normalized fw4 semantics.
It installs a temporary verdict-free observer to distinguish both service-node
sources across TCP/2379, 2380, and 2381, then requires the exact named allow and
reject counter deltas. Its two restricted Pods live directly in
`etcdetcetc-smoke`: no temporary Namespace and no credential copy exist. Their
only temporary egress is router service address `10.66.1.1:80` for the
checksum-pinned etcdctl 3.6.13 archive and the three root /32s on TCP/2379-2381.
The attended `--run` confirmation is
`QUALIFY-FABRIC-ETCD-POST-OPEN:<pushed-main-commit>`; begin with `--check` and
the serial ED25519 fingerprint exactly as shown in the activation runbook.

### Persistent post-open etcd fence

Any controller, runtime, or post-open failure after the network-open gate
requires immediate containment before diagnosis. Do not rely on replacing the
TCP/2379 UCI allow with the old 'forward_lan' reject alone: fw4 accepts
established/related traffic earlier, so an existing etcd gRPC connection would
survive that reload. Start with the read-only check and copy its exact
revision-and-contract confirmation:

~~~sh
sudo scripts/rollout-fabric-router-etcd-policy \
  --fence-check \
  --serial-ed25519-fingerprint SHA256:VALUE_FROM_ROUTER_SERIAL \
  --identity /var/home/sam/.ssh/id_ed25519

sudo scripts/rollout-fabric-router-etcd-policy \
  --fence-run \
  --serial-ed25519-fingerprint SHA256:VALUE_FROM_ROUTER_SERIAL \
  --identity /var/home/sam/.ssh/id_ed25519 \
  --confirm 'FENCE-FABRIC-ROUTER-ETCD-POLICY:<pushed-main-commit>:<fence-contract-sha256>'
~~~

The fence accepts only the exact opened state. It restores the canonical
'Reject-services-to-root-etcd' UCI rule and installs the reviewed
'etcd-client-fence.nft' as the sole '/usr/share/nftables.d' include at
'chain-pre/forward'. Its request drop covers exactly svc1/svc2 to
cp1/cp2/cp3 TCP destination port 2379; its reverse drop covers the same
endpoints at TCP source port 2379. Acceptance proves both normalized live
expressions exactly, proves they are the first two executable 'forward' rules
and precede every accept, then re-proves the complete canonical
'forward_lan', all three exact root host guards, and observer isolation. API
readiness is captured as evidence but is intentionally advisory for emergency
containment: an unavailable control plane cannot be allowed to block its own
network fence.

The FENCE confirmation explicitly authorizes proceeding when the Fabric API is
unavailable or `kube-system/fabric-maintenance-lock` already has any holder. If
the API is ready, the helper attempts to create that lock; on conflict it
records and preserves the holder without deleting, replacing, or adopting it.
If the API is down it skips the ConfigMap operation. In both cases the host lock
still serializes local helpers, while the atomic router-local
`/tmp/fabric-router-etcd-policy-fence.operation` lock serializes actual router
staging across workstations. A second fence refuses while that exact-owner lock
exists. `/tmp` makes an untrapped stale lock reboot-volatile; never remove it by
hand while the router is running.

Payloads are built only under the root-owned `.staging` directory. The enabled
init script validates its exact owner, token, operation ID, payload manifests,
and installed bytes before atomically renaming `.staging` to the final work
path. An exact incomplete staging tree can be discarded and rebuilt only after
the router-local lock proves no other operation is active. An unknown tree,
completed work path, installed init residue, or modified marker is preserved
and requires serial inspection. The EXIT recovery path likewise validates all
completed staging hashes and the installed init before it can promote or arm
the enforcer; it never executes an unvalidated staged init.

A failed post-open qualifier can leave its exact six-rule, verdict-free
`fabric_etcd_post_open_probe` table. Fence pre-arm validation accepts that table
only in its complete exact shape, even if it appears between the initial table
inspection and the same remote validation. Once the persistent enforcer has
acknowledged the fence, the helper removes that exact table and proves absence.
Any extra table, rule, verdict, endpoint, port, or table-level object is a hard
stop and is preserved for inspection.

This is persistent containment, not a timed rollback. The enabled procd
enforcer checks the source hashes, sole-include provenance, exact live rule
expressions/order, and fenced UCI config on every loop and reapplies the
reviewed state after drift, firewall reload, or reboot. On a successful fence
the enforcer, include, work directory, and non-secret evidence deliberately
remain. On failure, any safely completed staging/enforcer artifacts and any
global lock acquired by this run remain for serial inspection; a run blocked
before staging may have no router artifact, and an API-down run acquires no
ConfigMap lock. A conflicting pre-existing holder always remains untouched.
Never delete, disable, or hand-edit containment artifacts to regain service.
The ordinary open '--check' and '--run' reject any fence residue, so they cannot
report an open policy while the earlier drops still exist.

There is intentionally no in-place unfence mode. First fix forward and restore
the config/controller/runtime suspension gates through Git; suspension does
not terminate an already-running Pod, which is why the network fence comes
first. Recovery then requires a separately reviewed serial procedure or a
closed-policy router reprovision, followed by the full commissioning,
network-open, and post-open gates from a state with no controller Pods. Never
roll hardened CRDs, chart state, or controller provenance back to the bootstrap
image as a recovery shortcut.

For the still-open full source-role matrix, the dedicated physical probe NIC
must not remain eligible for desktop auto-DHCP. Pin the exact reviewed
interface/USB-serial/permanent-MAC identity first, then make that device
persistently unmanaged without deleting its dormant connection profile:

```sh
sudo nmcli device set enp9s0u1u2 managed --permanent no
```

Any implementation of that physical matrix must require effective
`GENERAL.NM-MANAGED=no`, NetworkManager state `unmanaged`, and no active
connection before and after its trap-restored run. Its read-only check must not
raise an administratively-down NIC merely to test carrier; a confirmed live
run may raise it only after moving it into the probe namespace and must then
require physical carrier before assigning a source address. The attended
rollback, after the probe cable is removed, is
`sudo nmcli device set enp9s0u1u2 managed --permanent reset`; revalidate the
desktop default route immediately afterward.

The matrix must own separate ephemeral network namespaces where applicable,
hold `/run/lock/fabric-network-operation.lock`, and require the official
observer namespace to be absent. It must never seize the desktop's normal
wired uplink, observer Wi-Fi PHY, or Tailscale device. Treat a local tested path
as tested-path evidence, not proof that the upstream router has no static
route; an authoritative upstream configuration/route-table check is required
for the broader claim. Also prove that disabling or disassociating `radio0`
removes public egress without changing the fabric LAN, observer AP, or same-L2
root reachability, and that reassociation restores only the reviewed logical
WAN path.

This matrix validates the router's behavior for packets carrying those source
addresses. It does not authenticate a source on the shared LAN: a hostile peer
could spoof an admitted address, add an on-link route, or poison ARP, and
same-subnet peer traffic bypasses the router. Keep both unmanaged switches and
the two service nodes physically trusted. This profile is suitable only for
the reconstructible platform agents while their root and service host
firewalls remain active. Untrusted nodes still require the planned managed
VLAN switch and inter-zone admission boundary.

Perform one attended reboot from the serial console and rerun the same snapshot
with the same serial-pinned host-key fingerprint. Require a changed `BOOT_ID`
and an identical `stable-invariants.sha256`, alongside the unchanged host key
and asset evidence. Require `radio0` to reassociate and reacquire its logical
WAN lease without moving `radio1` or `eth1` between zones. A wired capture on
the fabric switch must also show no router advertisements or DHCPv6 responses;
the IPv6-disabled observer namespace cannot prove their absence by itself.

## Prepare the data USB device

The router image intentionally does not embed the K3s and etcd release
payloads. OpenWrt mounts an ext4 filesystem labeled `data` read-only with
`nodev,nosuid,noexec,noatime` at `/mnt/data`, and the built-in web server exposes
`/mnt/data/www/` at `/static/`. The OpenWrt One's
USB device serves pinned `k3s`, its matching air-gap image archive and
installer, a pinned kube-vip OCI archive, the CoreOS K3s SELinux policy RPM,
the official etcd 3.6.13 archive, and node_exporter 1.11.1 referenced by the
offline-rendered initial-consensus Ignitions. It never stores those Ignitions or
their secrets. DHCP/TFTP PXE remains disabled.

Building the image resolves and verifies the pinned sources from
`fabric/router/data-files.txt` into the shared `_build/data/` cache. Ordinary
HTTPS records are downloaded directly. An `oci://` record is materialized by
`scripts/stage-fabric-airgap-images` according to the matching
`fabric/router/data-images.txt` entry: the OCI index digest, linux/amd64 child
manifest digest, full archive reference, every content blob, and canonical
archive checksum must all agree. The resulting archive preserves the registry
manifest digest for K3s/containerd's offline import. Prepare a dedicated USB
device from a Linux machine only after that build succeeds:

```sh
scripts/prepare-fabric-router-data-usb \
  --device /dev/disk/by-id/usb-VENDOR_MODEL_SERIAL-0:0
```

That command is a read-only dry run. It accepts only a stable whole-disk
`/dev/disk/by-id/` symlink, verifies the cached payloads against the fabric
manifest, reports the device's model/serial/size, and prints a device-specific
destructive command. Physically compare that report with the USB device before
copying and running the printed command with `sudo`.

The helper refuses mounted devices, partitions, the current system disk,
active mapped/RAID devices, and non-USB disks. Some USB-to-SATA bridges report
the kernel removable bit as `0`; after physically verifying such a bridge, add
`--allow-non-removable-usb` to both the dry run and the destructive invocation.
This override does not weaken the stable-path, mount, system-disk, or exact
confirmation checks. A non-empty hardware serial is mandatory even when this
override is used.

The destructive operation replaces the selected device's partition table,
creates one ext4 filesystem labeled `data`, copies the pinned files into
`www/`, and verifies their checksums again from the mounted filesystem. Never
use `/dev/sdX`, and do not reuse a device containing data that matters. Neither
partitioning nor formatting sanitizes old bytes, flash-controller remapped
cells, or wear-levelled storage. A USB device that has ever held a rendered
fabric node installer remains secret-classified and must never be reused as
the router data device. Disable desktop automounting for the preparation
ceremony; the helper fails if the newly formatted filesystem is mounted by
another process and performs a final mount/holder check before reporting
success.

After inserting it into the router, verify over the isolated fabric LAN that
the mount and one pinned payload are available through both prefixes before
installing or rebuilding nodes:

```sh
ssh root@10.66.0.1 'mount | grep "on /mnt/data " && (cd /mnt/data/www && sha256sum -c SHASUMS.txt)'
curl --fail --head http://10.66.0.1/static/k3s
curl --fail --head http://10.66.1.1/static/k3s
```

The router itself keeps DHCP/TFTP PXE disabled. The separately authenticated,
attended Bootie worker station may temporarily serve one MAC-pinned service
candidate on this trusted L2; it must live off the consensus nodes, possess no
root Ignition, and tear down after each one-use install. Broadcast DHCP on this
flat domain is an attended migration exception, not a permanent router role.
GRUB, the kernel, and the capability-named custom initramfs are fetched while
the candidate still owns `10.66.0.100`. The embedded network profile then
adopts `10.66.1.10` or `.11`; the only remaining station request is dracut's
`coreos.live.rootfs_url` fetch from `10.66.0.2:80`. Install Ignition is embedded
in that custom initramfs, and later pinned assets come from `10.66.1.1:80`, so
no other post-profile Bootie allowance is required. The router rule is an
exact L3/L4 source, destination, protocol, and port admission; Bootie's
one-use lifecycle and HTTP configuration remain responsible for URL paths.

The image is not ready to flash merely because it builds: first verify the
router hardware revision and SHA256 of the selected artifact, then follow a
model-specific recovery/factory-install procedure with a wired client.

The OpenWrt ImageBuilder archive is independently pinned by
`fabric/router/imagebuilder-sha256`; the build verifies both new downloads and
an existing cache entry before extracting it.
