# fabric router

This overlay builds an isolated OpenWrt image for the OpenWrt One.
Its WAN port obtains an address from the current upstream network; its LAN is
the non-advertised `10.66.0.0/24` fabric provisioning and management network.

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

The image contains the SOPS-managed `fabric-observer` WPA3 credential and is
therefore secret-bearing. Keep the generated ITB mode `0600` on encrypted or
tmpfs storage, never publish it as a CI artifact, and remove the plaintext
ImageBuilder tree after copying and verifying the selected image. The encrypted
source remains recoverable only with Sam's offline age identity.

Install it without retaining factory settings (`sysupgrade -n`). Do not use a
`factory.ubi`, preloader, or bootloader artifact for an ordinary first
deployment. Those belong to the documented NAND/NOR recovery paths.

The image deliberately:

- exposes one low-power 5 GHz WPA3-SAE/802.11w SSID named `fabric-observer`,
  bridged to the fabric LAN for the sole admitted desktop Wi-Fi MAC;
- caps that SSID at one association, disables stock/additional SSIDs, WPS,
  IPv6 prefix delegation, FRR, Tailscale and Tang;
- has no route advertisement or Tailscale-to-LAN forwarding;
- gives both LAN and WAN reject-by-default input/forward policies, with named
  IPv4 catch-all rejects for deterministic fw4 counters, no WAN-to-LAN
  forwarding, and no inherited stock WAN exceptions;
- blocks the three roots from reaching RFC1918 or Tailscale/CGNAT
  `100.64.0.0/10` destinations through the WAN, while granting public egress
  only to their three static addresses;
- permits key-only OpenSSH from observer `10.66.0.2` and no other LAN source;
  password authentication and every SSH forwarding mode are disabled, and
  Dropbear is absent;
- permits router DNS, NTP, and pinned-asset HTTP only from the observer and
  three roots;
- disables LAN DHCPv4, DHCPv6, router advertisements, and NDP proxying; the
  `odhcpd` server is absent, and the router, observer, and consensus nodes use
  reviewed static addresses while worker addressing waits for a separate
  VLAN/subnet;
- leaves DHCP/TFTP PXE disabled until a worker-only, authenticated provisioner
  exists; the unused iPXE binaries are absent and the initial consensus
  Ignitions are carried offline.

The fabric overlay also subtracts common Tailscale, Tang, FRR, Dropbear,
`odhcpd`, iPhone tether, generic USB-Ethernet hotplug hooks, and the matching
CDC USB-network drivers at image-build time. Those packages and escape-path
hooks are absent, not merely inactive; USB storage remains for the pinned asset
device.
LuCI and the unused router Prometheus exporter are force-removed; bare `uhttpd`
binds only `10.66.0.1:80` and serves the pinned public files beneath `/static/`
with directory listings disabled and no web administration surface.

Connect the labelled 2.5G port as WAN to the existing upstream network. Connect
the labelled 1G LAN port to the measured five-port fabric switch, then connect
only the three initial consensus nodes to that switch. Power the OpenWrt One
through its USB-C power input from the measured extension board; PoE on WAN
would bypass the intended power boundary. Do not connect a fabric LAN port
directly to the existing hub/home LAN, and do not add workers until the
documented VLAN/firewall gate passes.

The firewall overlay deletes every inherited rule, forwarding, redirect, NAT,
and include before constructing this policy. Four explicit IPv4 catch-all
rejects mirror the zone defaults so WAN input, fabric-to-WAN, WAN-to-fabric,
and fabric router-input denials have named fw4 counters. IPv6 remains denied by
the zone defaults and is checked separately by wired packet capture. Same-L2
root traffic bypasses the router and is constrained separately by the FCOS
`fabric_guard` host table.

## Initial install and recovery boundary

For normal operation leave the rear boot selector at `NAND`. The preferred
first deployment is a wired staging connection or USB-C serial console at
115200 8N1, followed by `sysupgrade -n` of the generated sysupgrade image.
Verify the artifact SHA-256, key-only SSH, LAN address `10.66.0.1`, WAN/LAN
port mapping, and isolation rules before moving the three nodes behind it.

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

The `fabric-observer` radio is a narrow operator attachment, not a general
management WLAN. It extends the trusted consensus L2 over the air, so possession
of the SAE secret plus MAC spoofing can still enable ARP disruption. Keep it
low-power, one-client, and time-bounded; do not associate roots or workers.

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
anonymous counter. It derives the seven-file asset manifest locally, checks
exact remote membership and bytes, streams and hashes every HTTP response,
requires exact answers for all four fabric DNS records over UDP and TCP, and
obtains an NTP sample. It does not trust the router data stick's own
`SHASUMS.txt` in isolation.

Fit and verify a CR1220 in the OpenWrt One RTC holder before the whole-domain
loss test. The roots use `10.66.0.1` as their only configured NTP source; after
they synchronize once, loss of the router or WAN leaves them on their own RTCs
and chrony drift state and does not enter the etcd peer reconnect path.

The source-role matrix remains a separate attended gate. Do not repurpose the
fixed observer helper or add ad-hoc routes to its namespace. With no real root
attached, a guarded, trap-restored procedure must exercise observer `.2`, each
admitted root address `.10` through `.12`, an unauthorized `.20`, and a
controlled WAN-side client. It must prove router-service source restrictions,
observer and unauthorized forwarding denial, root public egress, private and
CGNAT denial, WAN-input denial, and WAN-to-fabric denial using known-live
destinations plus deltas on the corresponding named fw4 counters. Do not claim
a separate masquerade counter: a successful active public probe together with
an increased `Allow-roots-to-public-WAN` counter proves the permitted NAT path.
After restoration, rerun both the namespace verifier and the read-only
snapshot. Also record that the upstream/home network has no route to
`10.66.0.0/24` and no Tailscale subnet route advertises it. This gate remains
open until that evidence is captured; do not attach a consensus node first.

Run that matrix only with two dedicated, identity-pinned, otherwise-idle
physical Ethernet test interfaces: one on the isolated fabric switch and one
on the existing WAN/upstream L2. The matrix must own separate ephemeral network
namespaces, hold `/run/lock/fabric-network-operation.lock`, and require the
official observer namespace to be absent; it must never seize the desktop's
normal wired uplink, Wi-Fi PHY, or Tailscale device. Treat a local tested path
as tested-path evidence, not proof that the upstream router has no static
route; an authoritative upstream configuration/route-table check is required
for the broader claim.

This matrix validates the router's behavior for packets carrying those source
addresses. It does not authenticate a source on the shared LAN: a hostile peer
could spoof an admitted address or poison ARP, and same-L2 peer traffic bypasses
the router. Keep the pre-worker LAN physically trusted. Untrusted nodes still
require the planned VLAN, switch, and host-firewall admission boundary.

Perform one attended reboot from the serial console and rerun the same snapshot
with the same serial-pinned host-key fingerprint. Require a changed `BOOT_ID`
and an identical `stable-invariants.sha256`, alongside the unchanged host key
and asset evidence. A wired packet capture must also show no router
advertisements or DHCPv6 responses; the IPv6-disabled observer namespace cannot
prove their absence by itself.

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
the mount and one pinned payload are available before installing the consensus
nodes:

```sh
ssh root@10.66.0.1 'mount | grep "on /mnt/data " && (cd /mnt/data/www && sha256sum -c SHASUMS.txt)'
curl --fail --head http://10.66.0.1/static/k3s
```

PXE is a later, worker-only facility. Do not enable it until etcd RBAC and the
consensus VLAN/host-firewall gates pass, and a separately authenticated worker
profile service has been reviewed. The future provisioner must live off the
consensus nodes and must never receive their rendered Ignitions.

The image is not ready to flash merely because it builds: first verify the
router hardware revision and SHA256 of the selected artifact, then follow a
model-specific recovery/factory-install procedure with a wired client.

The OpenWrt ImageBuilder archive is independently pinned by
`fabric/router/imagebuilder-sha256`; the build verifies both new downloads and
an existing cache entry before extracting it.
