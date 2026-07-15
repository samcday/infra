# Fabric root cluster

This is the additive replacement foundation for the current physical `hub`.
The existing hub remains authoritative until a separately bootstrapped fabric
cluster and a fresh managed hub have passed the failure, authorization,
network-isolation, and recovery gates below.

## Initial topology and sacred-node policy

Phase one contains exactly three physical consensus nodes,
`fabric-az1-cp1` through `fabric-az1-cp3`. Each runs one etcd member and one
root K3s server. Three voters are the preferred size: additional mini PCs are
more valuable as worker failure domains and tested cold spares. Five voters are
only justified by a requirement to survive two simultaneous physical-node
failures and five genuinely independent machines.

The selected provisional consensus hardware is three Lenovo ThinkCentre M710q
systems with Intel Core i3 CPUs. Fit each with at least 8 GiB RAM and one
admitted root SSD; 128/256 GB SATA devices remain the preferred default. Each
chassis is expected to expose TPM 2.0, but the model
name is not proof: presence, enabled state, usable capabilities, and firmware
posture remain per-machine inventory and admission gates. Stable wired
networking, memory health, temperature, and synchronous disk latency matter
more to etcd than matching CPU SKUs. Machine types, exact CPU models, NICs,
TPMs, firmware, MACs, RAM, and disk identities remain inventory inputs rather
than manifest assumptions.

The three nodes are sacred. Their permanent allow-list is etcd, the K3s API
server/controller-manager/scheduler, kube-vip, Flannel, kube-proxy,
and narrowly scoped node-level operational agents. Flux and Bootie do not run
there, including during bootstrap.
Ceph, KubeVirt/CDI, VM launchers, hosted control planes, LLM/GPU jobs, image
registries, monitoring databases, and application workloads never run on the
three root nodes.

The bootstrap config gives every root a dedicated
`fabric.samcday.com/root-consensus=true` label and NoSchedule taint in addition
to the normal control-plane taint. That makes accidental placement harder, but
taints are not an allow-list: a broadly tolerating DaemonSet can still land
there. Before Flux or arbitrary add-ons exist, enforce an admission policy that
rejects non-approved Pods selecting or tolerating the root-consensus nodes.

Phase one intentionally has no cluster DNS. The CoreDNS deployment bundled in
the pinned K3s release does not tolerate the dedicated root-consensus taint, so
bootstrap disables it instead of weakening the placement boundary. Before any
platform or application workload, deliberately deploy CoreDNS onto workers
with explicit worker placement and any required worker-taint tolerations, then
prove service and pod resolution and prove that no DNS Pod runs on a root.

Workers are phase two. Do not attach or join even the first worker until etcd
RBAC and the consensus VLAN/firewall gates have both passed. Grow to three
physical workers before trusting a managed hub. Workers eventually host Flux
platform controllers, monitoring, hosted control planes, KubeVirt/CDI, and
child worker VMs.

Three members tolerate one member failure. They do not tolerate loss of the
shared five-port switch, extension board, smart-plug relay, room, or upstream
mains. The dedicated router is intentionally outside etcd's peer reconnect
path, but the switch and measured power domain are common failure domains; this
is a well-instrumented single-site root, not multi-site disaster tolerance.

## Offline-first bootstrap boundary

The initial consensus Ignitions are rendered on a trusted machine from
`fabric/butane/bootstrap.sh`, written outside the Git worktree, and carried to
the three machines offline. After one candidate has a reviewed final inventory
capture, `--node fabric-az1-cp1` (or the matching cp2/cp3 name) renders only
that node and requires only its corresponding `FABRIC_CP*_MAC`. Omitting
`--node` preserves the original all-three render and requires all three MACs.
Each file contains that member's etcd private key, cluster tokens, and LUKS
recovery material. It is mode `0600` secret material, not an HTTP/PXE profile
and not a Kubernetes Secret.

Bootie and DHCP/TFTP PXE are disabled for this phase. Hardware discovery first
uses one separately built, non-installing `fabric-inventory-live.iso`. It has
no install destination or node secret, runs the read-only inventory collector,
and uses temporary static address `10.66.0.100/24` with no gateway or DNS. Boot
only one candidate at that address at a time. Initial installation then uses
the pinned Fedora CoreOS 44.20260621.3.1 x86_64 live ISO and three separately
customized images, each containing exactly one node's offline Ignition and its
physically verified supported stable destination identity. Preferred SATA
devices use `/dev/disk/by-id/ata-*`. For `fabric-az1-cp1` and
`fabric-az1-cp2`, Sam has explicitly repurposed the inventoried internal 256 GB
Samsung NVMes at exact lowercase
`/dev/disk/by-id/nvme-eui.002538839100c827` and
`/dev/disk/by-id/nvme-eui.002538bb71b4bb45`, respectively. A currently admitted
node may be manufactured independently with
`scripts/build-fabric-node-isos --node fabric-az1-cp1` and only `--cp1-disk`
(or the corresponding cp2 pair). Cp3 and the all-three build remain disabled
until cp3's reviewed exact ATA or lowercase NVMe-EUI identity is committed to
the disk policy.
Never use `/dev/sdX`. The same physical thumb drive may be used for inventory
first and then rewritten between individual nodes, but a multi-node image
containing all three Ignitions is forbidden. Once the first secret installer
is written, the entire device remains secret-classified and may never return
to inventory or general use: the writer verifies the ISO-length prefix but
cannot sanitize trailing bytes or flash-controller remapped cells.

Selected-node rendering and media creation do not define a one-member cluster.
Every node retains the fixed three-member etcd initial-cluster map and
`initial-cluster-state=new`. One installed voter alone cannot form etcd quorum,
and therefore cannot make the K3s API available; bring up at least two declared
members together without rewriting the membership around the first node.

Sam is the local console and remote hands: attach the USB HDMI adapter and
keyboard, disconnect every non-target internal disk, and select the USB device
with a one-time firmware boot. The inventory image must visibly identify
itself as non-installing and expose the live SSH host fingerprint before its
capture is accepted. Later, confirm the expected node name plus full stable
disk by-id before any installer is armed. Keep the three customized installer ISO
files and their hashes on trusted storage outside Git; each embeds private
keys and recovery material. Retain the armed USB in locked offline storage with
the recovery packet. Declassification requires rotation of every secret it may
have carried followed by physical destruction; rewriting it is insufficient.
Inside the installed config, FCOS'
`coreos-boot-disk` alias keeps the partition layout attached to the explicitly
selected installer target.

The guarded media writer accepts only a stable whole-device
`/dev/disk/by-id/usb-*` identity and verifies the exact ISO bytes again after
writing. At boot, the armed image refuses installation until UEFI, functional
TPM2 access, the permanent MAC, exact static IP, a minimum 64 GiB supported target,
and the router's exact seven-file asset manifest all pass. A successful install
powers the live environment off instead of rebooting; remove the armed USB
before the first disk boot. An install failure clears the destination partition
table by default and stops in the live emergency environment. Never treat a
failed target as bootable.

The router's USB filesystem supplies only pinned, checksum-verified public
assets: `k3s`, its matching air-gap image archive and installer, the pinned
kube-vip OCI archive, the CoreOS K3s SELinux policy RPM, the official etcd
3.6.13 Linux archive, and node_exporter 1.11.1. Ignition
verifies the same hashes while fetching them from
`http://10.66.0.1/static/` over the isolated LAN. No etcd key, LUKS key, token,
age identity, rendered Ignition, or mutable container tag belongs on the
router USB. The policy is layered before K3s is allowed to start and causes one
expected bootstrap reboot. The etcd binaries are installed locally before the
service starts; once K3s is installed, a router or WAN outage is not a core
etcd/K3s cold-boot dependency. Kube-vip is pinned to the linux/amd64 manifest
beneath a pinned GHCR index, materialized as a canonical OCI archive, and
imported by K3s from its local air-gap directory before the static manifest is
scheduled. Its Pod consumes the locally imported `v1.2.1` tag with
`imagePullPolicy: Never`; the archive and selected platform manifest, rather
than a registry lookup, are the immutable supply-chain boundary. Retain node-IP
API access as a recovery path independently of the VIP.

A future Bootie remains useful for repeatable worker discovery and induction,
but only after the hard gates pass. It needs a separate worker-only profile
set, an authenticated transport and constrained Node-creation policy, and a
worker placement. It must never receive the sacred-node Ignitions or become a
dependency of root etcd or K3s recovery.

## Network contract

The repository-wide source of truth is the [network CIDR
registry](../network-cidrs.md). Update it before assigning any worker, child,
VIP, or routed service range.

The dedicated OpenWrt One is the fabric gateway, DNS server, pinned-asset
server, and explicit service-publishing boundary. Its PXE hooks remain
fail-closed and LAN DHCP is disabled during the sacred-node phase. Put a wired
switch behind it so the three etcd peers remain on one L2 segment when the
router or WAN is restarted.
The present lightning-damaged chassis is a temporary degraded-hardware
exception: its failed labelled 2.5G `eth0` port stays disconnected and
unassigned, `radio0` is a 2.4 GHz station attached only to the logical WAN,
`radio1` supplies the 5 GHz observer AP, and the labelled 1G `eth1` port is the
only wired fabric link. Connect `eth1` to the fabric switch and power the router
through USB-C so it remains inside the measured root power domain. Do not use
PoE on the damaged jack.

This exception does not change the permanent design preference for a known-good
wired router. Replace the damaged unit before fabric becomes authoritative.
OpenWrt Two may replace it later only after it is available, supported, and
passes the same image, isolation, power-loss, and recovery qualification; its
model name alone is not admission.

One low-power 5 GHz WPA3-SAE SSID on `radio1`, `fabric-observer`, is bridged
into that LAN only for `sam-desktop`'s admitted permanent Wi-Fi MAC at static
`10.66.0.2/24`. It has no DHCP, gateway, DNS, IPv6, or client forwarding and is
limited to one association. Because a bridged radio extends the consensus L2,
the desktop radio runs in a network namespace with no link to the host
namespace; the host's stable home uplink remains wired. Retire or rotate this
temporary radio credential after worker-era management ingress exists.
The `radio0` upstream station is never part of that bridge and receives only
the logical WAN policy.

The provisional, intentionally non-advertised consensus LAN is
`10.66.0.0/24`:

| Address | Purpose |
| --- | --- |
| `10.66.0.1` | `fabric-router` and pinned asset server |
| `10.66.0.10` | `fabric-az1-cp1` |
| `10.66.0.11` | `fabric-az1-cp2` |
| `10.66.0.12` | `fabric-az1-cp3` |
| `10.66.0.20-99` | reserved; not a worker pool on the consensus segment |
| `10.66.0.100` | ephemeral live inventory; exactly one candidate at a time |
| `10.66.0.254` | root Kubernetes API VIP |

The pod and service networks are provisionally `172.22.0.0/16` and
`172.21.0.0/16`. The service range deliberately avoids the existing
`edge-au-east` service range at `172.23.0.0/16`. Before first installation,
verify all three fabric ranges against the router WAN, Sam's laptop,
Tailscale, and the current home/hub networks.

Before allocating the first child cluster, extend the CIDR registry so it
continues covering physical LANs, Tailscale, every parent/child Pod and Service
range, and externally published VIP pools. The current cloud/ilumbaclusta
Pod-and-Service reuse recorded there is evidence that prose-local allocations
do not scale.

The consensus nodes use static NetworkManager addresses and `/etc/hosts` for
peer names. Router DNS and DHCP are not in the etcd reconnect path. Do not
advertise the LAN as a Tailscale subnet. Etcd ports 2379, 2380, and 2381 are
never forwarded from WAN; the Superloop static public address is an outer-edge
publishing concern, not a consensus endpoint.

The OpenWrt One synchronizes against the public OpenWrt NTP pool and serves NTP
only to observer `.2` and roots `.10-.12`. Those roots configure `.1` as their
sole chrony source. Initial bootstrap requires a successful time sample and
sane RTCs. Once synchronized, router or WAN loss leaves the roots on their own
RTCs and persisted chrony drift; etcd peer reconnect remains direct L2 and does
not wait for the router. Fit and verify the router's CR1220 RTC battery before
the whole-domain power-loss gate.

### Consensus isolation gate

The bootstrap LAN is not permission to leave workers and consensus on one
flat network. Before adding a worker, commit and test a VLAN or equivalent
physical segmentation design in which:

- the three root nodes remain together on the consensus segment;
- workers and future provisioning services use a different VLAN and subnet;
- etcd peer TCP/2380 accepts traffic only among the three root addresses;
- etcd client TCP/2379 initially accepts only the three root addresses, then
  only explicitly declared hosted-control-plane clients;
- metrics TCP/2381 and SSH are limited to declared operational sources; and
- WAN, home/hub, general worker, pod, and tenant sources are denied direct
  etcd access.

Enforce this at the inter-VLAN firewall and on each consensus host. If the
current switch cannot provide the required access-port/VLAN boundary, replace
it or use physical segmentation before worker induction. Prove from the future
worker segment that 2380 is unreachable and 2379 is denied without an explicit
rule, then power off the router and prove the three peers retain L2 consensus.
No child etcd prefix may be created before this gate passes.

The offline root profile already supplies the phase-one host half of this
gate: a separate late-priority `inet fabric_guard` table defaults input and
forwarding to drop, admits etcd/API/VXLAN only among `.10-.12`, and admits
SSH/API/metrics only from observer `.2`. Kube-proxy is fixed to the bundled
iptables implementation and NodePorts are unreachable on roots. The OpenWrt
policy likewise defaults LAN/WAN input and forwarding to reject, admits public
WAN egress only from `.10-.12`, and admits router SSH only from `.2`. These
rules deny observer access to TCP/2379 in steady state; an explicit per-member
helper can insert only `.2` into an in-memory, 15-minute maintenance set for
authorization or recovery operations. The address allow-lists are sound only
while the switch is a physically trusted
root-only segment; they do not prevent a future same-L2 worker from spoofing a
source address. The worker VLAN and its explicit API/VXLAN allowances remain
a separate mandatory change before attaching any worker.

## Measured root power domain

Use the focused [power-meter commissioning runbook and non-secret
record](../../fabric/power/README.md) for the attended admission ceremony. The
record must reach `assembly_qualified` before the one attended full-domain
load/thermal test. No unattended or operational root-equipment connection is
allowed until an explicitly reviewed `accepted` decision.

The physical accounting boundary is:

```text
house mains
  -> candidate SM-732-A Matter plug
  -> one extension board
  -> OpenWrt One USB-C PSU + five-port switch PSU + cp1/cp2/cp3 PSUs
```

That is both the ongoing-cost boundary and, later, the whole measured
root-domain power-loss test boundary. Do not add workers, monitors, laptop
chargers, or unrelated devices to the board. Label the plug, board, five
downstream leads, and switch ports; record PSU nameplate ratings and the
board/plug electrical ratings.

The SM-732-A is a candidate meter, not yet accepted as one. Matter certification
does not itself prove electrical telemetry. Commission it into a controller
outside this power domain. Discover rather than assume its transport: provide
an always-on house/IoT Wi-Fi AP or Thread border router as appropriate. The
controller, persistent recorder, AP or border router must remain powered and
reachable while the OpenWrt One, current hub, future fabric, and measured board
are all off; none may be hosted by a cluster being measured. Keep its Matter
QR/passcode out of Git and SOPS-free documentation. Accept it for accounting
only if the local controller exposes instantaneous watts and cumulative Wh/kWh.
A reviewed local integrator may substitute for a cumulative entity only if it
detects telemetry gaps and preserves counter continuity. If it exposes only an
on/off entity, replace it with an explicitly metering-capable local device.

Treat its relay as a dangerous whole-consensus kill switch. Disable schedules,
voice-assistant exposure, automatic power cycling, and unattended firmware
updates. Before attaching the cluster, test a harmless load repeatedly and
prove that loss of the controller, its AP or Thread border router, and their
network cannot change the relay from ON. Prove separately that
loss/restoration of upstream mains returns the relay to ON. Configure each mini
PC firmware for `After Power Loss = Power On`. Verify Australian
compliance/rating labels, do not daisy-chain boards, and ensure the aggregate
PSU load fits the lowest-rated wall outlet, plug, board, and lead. Missing
RCM/rating/supplier evidence, physical damage, a loose fit, smell, or abnormal
heating fails admission. Validate reported energy against a known load;
consumer-plug telemetry is not assumed billing-grade.

Factory-reset the plug before commissioning, then prove that old Matter
fabrics, schedules, accounts, and vendor-cloud paths can no longer actuate it.
Keep one deliberately owned Matter fabric, disable cloud and voice control,
preserve local overload protection, and record non-secret manufacturer/model,
VID/PID, hardware/firmware version, transport, certificate identity, exposed
clusters, and entity units. Reject the plug if remote vendor-cloud actuation
cannot be removed. Apply firmware updates only during an attended maintenance
window, then repeat the relay, outage, restore, and counter-continuity tests.
Photographs containing the setup QR/passcode are secret recovery material and
stay encrypted outside Git.

Qualify the candidate with a safe fixed load for a recorded duration. Declare
the expected Wh and an acceptable error before the run (use a conservative
10% sanity tolerance unless a better reference meter supports a tighter one),
then record observed Wh. Repeat relay-off/on and upstream-mains-loss recovery
at least three times. Separately record what the cumulative counter does across
relay, controller, and mains restarts; a silent reset or unmarked telemetry gap
fails admission for unattended cost accounting.

Use an upstream reference meter to measure the plug's no-load standby draw.
Many load-side counters may omit the meter's own electronics; if this one does,
record that excluded overhead explicitly rather than presenting its downstream
counter as the complete wall-socket cost.

The authoritative observability path is an external local Matter controller
and persistent recorder (for example Home Assistant). It owns cumulative
counter continuity independently of every current or future cluster. A
sensor-only exporter may make the existing hub Prometheus and Grafana a
secondary telemetry consumer. The hub must receive metrics, not a Matter or
controller credential capable of switching the relay. Track plug availability,
relay state, instantaneous W, daily/monthly kWh, and usage cost as
`kWh * energy tariff`; do not attribute the household's fixed daily supply
charge to this domain. Monitoring failure must never affect etcd, K3s, routing,
or power state.

Do not invent entity names before commissioning. Record the controller,
device/entity IDs, unit, cumulative-meter reset behavior, sampling interval,
and current electricity tariff schedule in a non-secret operations values file
only after the plug proves it exposes W and Wh/kWh. Alert separately on
telemetry absence and relay-off state; never automate the relay from an etcd
alert.

Once telemetry is qualified, take cumulative-energy deltas for a 24-hour
steady-state root-cluster baseline, the 48-hour failure soak, and a later
representative hosted workload. Preserve the start/end timestamps and meter
values so a counter reset is visible. Project a 30-day variable cost from the
measured interval as `delta_kWh / elapsed_hours * 24 * 30 * tariff` for a flat
tariff. For time-of-use billing, split energy by the actual tariff schedule
instead of applying one scalar. Keep projections separate from the actual
daily/monthly cumulative readings.

## Disk policy

The 128/256 GB 2.5-inch SATA SSDs are the preferred consensus-node devices if
they pass inventory and latency gates. Continue using exact
`/dev/disk/by-id/ata-*` identities for those default devices. Sam has explicitly
repurposed the inventoried internal 256 GB Samsung NVMes in
`fabric-az1-cp1` and `fabric-az1-cp2` as their root disks, using only the exact
lowercase stable identities `/dev/disk/by-id/nvme-eui.002538839100c827` and
`/dev/disk/by-id/nvme-eui.002538bb71b4bb45`, respectively. Do not substitute a
model/serial alias or kernel name. Sam has authorized installation over both
devices' existing contents. Cp1 and cp2 are each bound to their exact EUI;
cp3 remains unadmitted until its exact ATA or lowercase NVMe-EUI whole-disk
identity is reviewed and pinned. Installation requires the armed pre-install
gate to revalidate the node-specific identity and receive its device-specific
confirmation.
Disconnect every non-target internal disk before booting the armed installer.
Other approximately 1 TB NVMe devices remain reserved for fabric workers and
VM storage.

The install profile allocates a 16 GiB Fedora CoreOS root partition, a 32 GiB
encrypted ext4 filesystem mounted at `/var/lib/etcd`, and the remaining space
to encrypted `/var`. Separate filesystems on one selected root device provide
capacity and mount-failure isolation, not separate latency domains. Actual latency
isolation comes from the sacred-node allow-list: no Ceph OSD, VM image,
registry, or monitoring TSDB competes for durable writes. The etcd service
fails closed unless `/var/lib/etcd` is mounted.

Every node has separate root, etcd, and `/var` recovery keys; no recovery key
unlocks two machines. The installer wipes old signatures on the two reused
data partitions before creating LUKS. TPM2 is the normal local unlock path. On
first boot a fail-closed service enrolls and verifies the per-node root
recovery key, records completion on encrypted `/var`, and removes the installed
copy. Store all nine recovery keys in the encrypted offline recovery packet
and verify one manual recovery path before the power-loss test.

The etcd backend quota is deliberately 2 GiB despite the larger filesystem.
This caps runaway logical growth and leaves headroom for compaction,
defragmentation, and operational recovery. Track database size and `NOSPACE`
alarms; the 32 GiB partition is not a reason to increase the quota casually.

## Remote-hands inventory and root-disk admission

For each candidate mini PC:

1. On the trusted workstation, build `fabric-inventory-live.iso` with
   `scripts/build-fabric-inventory-iso` into protected storage outside Git.
   Inspect and write it with `scripts/write-fabric-installer-usb`; the required
   confirmation starts with `WRITE-FABRIC-INVENTORY:` and cannot authorize an
   armed installer image.
2. Install the candidate's intended root SSD and disconnect every non-target
   internal disk. Prefer an admitted SATA SSD for new selections. For cp1 and
   cp2, retain only that node's exact inventoried NVMe target declared above.
   Connect only this candidate to the isolated switch. Join the router's
   `fabric-observer` SSID from the isolated `sam-desktop` Wi-Fi namespace at
   static `10.66.0.2/24`.
3. Attach HDMI and keyboard, choose a one-time UEFI boot from the inventory USB,
   and verify the console says that it is inventory-only. Compare the displayed
   ephemeral SSH Ed25519 host fingerprint with the fingerprint seen by SSH;
   do not bypass a mismatch.
4. Copy the mode-`0600` capture and checksum from
   `core@10.66.0.100:/var/home/core/fabric-inventory/` into a mode-`0700`
   candidate-specific directory on trusted removable or encrypted storage
   outside the checkout. Preserve the two basenames and verify the checksum in
   that directory. If SSH is unavailable, the local console remains the
   recovery path; do not improvise routing or advertise this subnet through
   Tailscale.
5. Power the candidate off, remove the USB, and label the chassis with its
   candidate number, wired MAC, system serial, admitted root-disk identity, and switch port.
   Repeat with the next machine when ready, but never boot two inventory
   candidates concurrently; they intentionally share `10.66.0.100`.
6. Return each capture for review. Do not wipe, partition, benchmark, or install
   that node from its initial discovery capture.
7. After every firmware, UEFI, TPM, RTC, memory, NIC, and disk change or
   qualification step is complete, boot the inventory media again and save a
   new capture and checksum without overwriting the discovery capture. Only
   this reviewed final capture may supply that node's MAC, stable disk by-id,
   and other installer inputs. Once it is reviewed, that node's Ignition and
   installer may be manufactured with `--node` without waiting for final
   captures from the other two candidates.

The dedicated image executes the same collector that can be run manually from
another trusted live environment when needed:

   ```sh
   install -d -m 0700 /secure/fabric-inventory
   (umask 077; sudo ./scripts/inventory-fabric-node > /secure/fabric-inventory/fabric-candidate-N.txt)
   ```

The capture is read-only. It records stable device identities, SMART/NVMe
health when tools are installed, virtualization/IOMMU support, TPM presence,
current mounts, RTC state, firmware mode, and TPM2 capability. The capture
contains serials, MACs, routes, and firmware data and must not be committed. No
discovery Node object, Bootie request, or cluster join is involved.

Before admitting a chassis, finish firmware changes first: update firmware,
select UEFI-only boot and AHCI, enable TPM2, set one-time USB boot rather than
permanent USB-first boot, disable unneeded network boot, and configure
`After Power Loss = Power On`. Record the Secure Boot posture explicitly; do
not change it after TPM enrollment without expecting recovery-key intervention.
Replace a weak CMOS battery, set UTC, disconnect mains long enough to prove the
RTC retains sane time, and then recheck it. A years-stale clock can make the
fresh etcd TLS certificates unusable.

A disk is not admitted merely because SMART says `PASSED`. Review at least:

- exact model, firmware, serial, capacity, and stable by-id path;
- reallocated, pending, uncorrectable, CRC, and interface error counts;
- power-on hours, unsafe shutdowns, wear or lifetime-used indicators;
- whether write cache and discard are supported; and
- sustained temperature and self-test history.

Also complete a memory test, SMART long self-test, sustained thermal/fan soak,
and sustained Ethernet test while comparing link resets, CRC, and dropped/error
counters before and after. Record the SSD's volatile write-cache setting and
prove the kernel/device stack supports flush/FUA; a fast `fio --fsync=1` result
alone does not establish power-loss durability. Do not use the whole-domain
relay test as the first durability experiment.

After a disk is explicitly selected and its previous contents may be destroyed,
run a bounded synchronous-write latency test on that exact device/filesystem.
Record the fio job, kernel, disk firmware, throughput, IOPS, and
p50/p95/p99/p99.9 latency. The job and thresholds must be reviewed before the
same model is admitted for all three machines.

## Console break-glass boundary

The installed systems intentionally have no reusable local password. Sam's SSH
key is the normal administrative path, but the HDMI/keyboard arrangement must
also recover a node whose network or SSH configuration is broken. Before
workers depend on fabric, rehearse the Fedora CoreOS live-media recovery path:
boot the same verified FCOS release in UEFI mode, keep the installed disk
unmodified until its identity is checked, unlock root/etcd/var with the matching
offline recovery keys, mount read-only first, and repair through an explicit
change record. Do not add a shared emergency password to all three roots. Keep
the recovery media, its hash, the nine per-node LUKS keys, PKI, and this procedure
together in the encrypted offline packet, with a second copy outside the
measured power domain.

## Pre-worker soak observer

The roots do not host a monitoring database, and the first worker is forbidden
until after the soak. Use a temporary trusted observer inside a dedicated
`sam-desktop` network namespace. Its Wi-Fi is static `10.66.0.2/24` on
`fabric-observer`; it has no gateway, DNS, IPv6, default route, veth, bridge, or
host-namespace link. The desktop's ordinary management path remains wired in
the root namespace. Run a local pinned Prometheus in the observer namespace to
scrape each etcd metrics endpoint on TCP/2381 and each kube-vip endpoint on
TCP/2112. The initial static configuration, alert rules, non-routing checks,
bounded loopback API/Lease collector, short-lived Kubernetes credential
ceremony, and useful first-pass graphs are in
[`fabric/observer`](../../fabric/observer/README.md). Each root separately
lists etcd alarms through its loopback endpoint with a zero-role mTLS identity
and exports only sanitized textfile metrics; the laptop never receives an etcd
key or steady-state TCP/2379 access. A loopback-only Grafana is optional; it is
not part of the bootstrap pack. The observer may remote-write or expose its
own UI only through an attended namespace-local client; do not add a veth merely
to expose the UI. Store the 48-hour data and alert evidence off the desktop before
dismantling the observer, then revoke the unique API bindings and remove its
54-hour private key.

At minimum graph and alert on WAL fsync and backend commit latency histograms,
leader changes, proposals pending/failed/committed/applied, slow applies, DB and
DB-in-use size, alarms, process restarts, API/VIP reachability, node disk/CPU/
memory pressure, and scrape absence. A narrowly scoped node exporter or textfile
collector is allowed only after review; a TSDB is not. The Matter controller
remains external and supplies W/Wh independently. Replace this temporary path
with worker-hosted monitoring only after worker induction.

## Etcd authorization gate

Mutual TLS is an authentication boundary, not a prefix authorization boundary.
The initial quorum starts before etcd auth is enabled so it can bootstrap, but
that state may not survive into worker induction. Once all three endpoints are
healthy, use the guarded `fabric/pki/etcd/enable-auth` helper with the offline
`root` identity to:

- enable etcd authentication;
- reserve the `root` identity for offline recovery and administration;
- grant the K3s `fabric-root` identity read/write access only below the API
  server prefix `/fabric-root/` and K3s' required server-bootstrap prefix
  `/bootstrap/`; and
- verify that `fabric-root` cannot read or write outside those two prefixes.

Record the successful negative tests as acceptance evidence. Every future
child receives a distinct identity and prefix-scoped role; it does not reuse
`fabric-root`. Do not add workers or create child prefixes until this gate and
the consensus isolation gate have both passed.

## Recovery, token, and version gates

An etcd snapshot is accepted only when `etcdctl snapshot save` completes through
an explicitly selected healthy endpoint, `etcdutl snapshot status` verifies it,
and its hash plus status record are copied to encrypted off-cluster storage.
Use the guarded single-endpoint creator and independent offline verifier in
[`fabric/recovery`](../../fabric/recovery/README.md); neither helper authorizes
a restore into a live member directory.
Keep at least the latest known-good snapshot, one daily generation, and one
pre-change generation; define exact retention after observing real size. A
recovery rehearsal restores the same snapshot into three empty directories with
the exact member names, initial peer URLs, and initial-cluster token, then boots
an isolated three-member quorum and verifies Kubernetes keys before deletion.
Never copy live member data directories as a snapshot and never restore over an
existing member directory.

K3s server and agent token rotation is one transaction with the offline source
of truth: rotate through supported K3s tooling, update the SOPS-encrypted Butane
values, refresh the encrypted recovery packet, render fresh Ignitions/media,
and prove a rebuilt server joins. Old secret-bearing ISOs must be destroyed or
clearly revoked after their hashes are recorded. A live-only rotation that
leaves Git and recovery media stale does not pass the gate.

The initial compatibility set is K3s `v1.36.2+k3s1` with external etcd
`v3.6.13`. K3s vendors the same etcd 3.6 line; nevertheless, external-etcd
certification can lag releases, so the exact pair must pass the full soak,
snapshot, restore, restart, and auth tests here. Upgrades are staged one layer
at a time: snapshot first, test on an isolated restore, upgrade one etcd follower
at a time then its leader, and only then consider K3s/FCOS. Never perform an
in-place etcd downgrade; use the version's documented rollback/restore path.
Zincati remains disabled until this cadence and rollback evidence exist.

## Bootstrap invariants

- Fabric receives fresh K3s tokens, etcd CA and certificates, LUKS recovery
  material, and router SSH host keys. It reuses no hub cryptographic identity
  and has no Tang dependency. The reserved Flux identity remains offline until
  Flux is placed on workers.
- All three virgin members receive the same static initial-cluster map and
  token, distinct member keys, distinct per-node LUKS recovery keys, and
  `initial-cluster-state=new`. Start at least two close together so the declared
  three-voter cluster can elect a leader.
- Once a member has initialized its durable data directory, a normal restart
  uses that membership; replacement-member induction is a separate operation.
- Root etcd and root Kubernetes remain physical. KubeVirt cannot be required to
  recover either one.
- Root recovery works from Git, offline SOPS/PKI material, the router image and
  asset manifest, kubeconfig, and an off-cluster etcd snapshot while hub and
  every child cluster are unavailable.
- No current hub manifest or live object is repointed at fabric during
  bootstrap.

## Acceptance sequence

1. Build and flash the OpenWrt One from the pinned `mediatek/filogic`
   `openwrt_one` image. Prepare its `data` USB device with
   `scripts/prepare-fabric-router-data-usb` and verify every pinned asset from
   the isolated LAN. Use temporary bench power outside the candidate meter;
   final measured-domain placement follows the power admission stages. Confirm
   LAN DHCP and DHCP/TFTP PXE remain disabled.
2. Commission and qualify the candidate Matter plug outside the measured
   domain. Prove W/Wh telemetry or replace it, prove restore-to-ON behavior,
   then label and assemble the exact measured power domain.
3. Verify the isolated LAN, DNS, NTP, static addressing, logical-WAN RFC1918
   block, and wired L2 behavior without any Tailscale subnet route. Prove the
   degraded role map explicitly: failed `eth0` is disconnected and unassigned,
   `eth1` is the sole wired fabric member, `radio0` is the sole 2.4 GHz
   upstream-WAN station, and `radio1` is the sole 5 GHz observer AP. Exercise
   WAN-side denials from a controlled client on the upstream/home L2 and use a
   router capture or named firewall-counter delta to prove each denied probe
   reached `radio0`; do not mistake upstream WLAN client isolation for a router
   firewall result. Then disassociate and reassociate `radio0` and prove that
   only public egress is lost and restored while fabric same-L2 traffic remains
   available.
4. Build and verify the non-installing inventory ISO, write it with its distinct
   inventory-media confirmation, then mediate one candidate boot and capture at
   a time. Finish firmware/UEFI/TPM/RTC settings, select each root device, and
   pass memory, SMART-long, thermal, Ethernet, flush/write-cache, and destructive
   synchronous-write qualification with explicit authorization. Recapture each
   candidate afterward; only that node's reviewed final capture may feed its
   installer MAC and disk values, but it need not wait for the other captures.
5. Generate independent fabric PKI/secrets. Render and build one node at a time;
   for example, use `fabric/butane/bootstrap.sh --node fabric-az1-cp1` for cp1
   and pass exact `/dev/disk/by-id/nvme-eui.002538839100c827` to the builder's
   `--cp1-disk`. Retain the legacy all-three mode by omitting `--node` after all
   inputs are available. Keep the sensitive Ignitions and per-node customized
   FCOS ISOs on trusted storage outside Git. Use
   `scripts/write-fabric-installer-usb` and its device-specific confirmation to
   reimage the installer thumb drive for exactly one node at a time. With only
   its selected root disk attached, locally verify the displayed node, static
   IP, TPM2, router manifest, and full disk by-id, then install. The live
   installer powers off on success; remove the USB, boot the installed root
   disk, and expect one later automatic reboot while the pinned K3s SELinux
   policy becomes active. A first voter may be installed independently, but it
   will not provide etcd quorum or a K3s API until a second declared voter is
   online.
6. Bring up at least two declared members together, then the third. Verify
   three healthy members, one leader, no alarms, the 2 GiB quota, API VIP
   failover, and all three K3s servers. Flux is intentionally absent.
7. Enable etcd auth/RBAC and record positive `/fabric-root/` and `/bootstrap/`
   tests plus negative out-of-prefix tests. Perform a controlled K3s restart,
   then validate server rejoin and token rotation before declaring the role
   complete.
8. Implement the worker/consensus VLAN and inter-VLAN policy, extend the
   installed host guard only for the declared worker API/VXLAN flows, and
   retain the etcd/SSH/metrics denials. Record denied worker/WAN tests and
   successful peer consensus with the router off. Use an unjoined test host on
   the future worker segment for the denial evidence; joining the first worker
   remains forbidden until the soak and recovery gates pass.
9. Attach the non-routing temporary operations-laptop observer and soak for at
   least 48 hours while observing WAL fsync, backend commit,
   proposals, slow applies, elections, database size, disk latency, and I/O
   pressure, authenticated alarm state, API VIP readiness, authoritative
   Lease/self-reported kube-vip agreement, plus whole-domain W/kWh. Reboot one
   follower and then the leader.
10. Create and validate an off-cluster snapshot and perform a rehearsed restore
   before anything depends on fabric.
11. After a clean shutdown baseline and successful restore rehearsal, perform
    one attended abrupt downstream-domain loss/recovery test with the meter's
    relay. Verify all machines power on, quorum recovers, and no alarms remain.
    Then perform one attended upstream-mains interruption and repeat those
    checks; a relay cut does not exercise loss of power to the meter itself.
12. Only after steps 7-11 pass, add a separately segmented worker. Deliberately
    deploy CoreDNS on workers, prove its worker placement and DNS resolution,
    and only then install Flux or other workloads. Flux remains on workers,
    never on the root trio; then grow to three workers.
13. Design the authenticated worker-only Bootie path if desired. Install
    KubeVirt/CDI and hosted workloads only on workers, prove a disposable child
    cluster with its own etcd identity/prefix, and migrate hub in a later phase.
