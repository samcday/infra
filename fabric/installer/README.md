# Fabric consensus installer media

## Read-only inventory live media

Before assigning a root disk, MAC address, or node name, build the reusable
inventory-only live ISO:

```sh
install -d -m 0700 /path/on/trusted-storage/fabric-inventory-media
install -d -m 0700 /path/on/trusted-storage/fabric-installer-cache
scripts/build-fabric-inventory-iso --check
scripts/build-fabric-inventory-iso \
  --output-dir /path/on/trusted-storage/fabric-inventory-media \
  --cache-dir /path/on/trusted-storage/fabric-installer-cache
```

The result is `fabric-inventory-live.iso` plus the exact adjacent
`fabric-inventory-live.iso.sha256` consumed by the guarded USB writer. The
builder refuses output inside this checkout, refuses existing outputs and
symlinks, verifies the pinned ISO digest and detached Fedora signature, and
customizes only with `--live-ignition` and the reviewed `--network-keyfile`.
It supplies no destination device, destination Ignition, installer config, or
install hooks. Run the builder without `sudo`. Existing output and cache
directories must be owned by the invoking user and mode `0700`; it creates
missing directories with that mode but never chmods an existing directory.
It also verifies the finished image has no `coreos.inst.*` kernel argument;
the live profile masks both `coreos-installer.service` and
`coreos-installer.target` as defense in depth.

Boot only one candidate at a time because every copy uses the isolated static
address `10.66.0.100/24`, with no gateway or DNS. Set the operator laptop to
`10.66.0.2/24` on the same L2 segment; the live firewall permits SSH only from
that source, disables forwarding and IPv6, and fails closed unless the actual
runtime address, routes, and DNS state match the isolated design.

Each FCOS live boot creates an ephemeral SSH host key. Use a fresh
candidate-specific `known_hosts` file, scan the ED25519 key, and compare its
SHA256 fingerprint with the value printed on the physical console before
strictly connecting. Do not use `StrictHostKeyChecking=no`:

```sh
candidate_dir=/secure/fabric-inventory/candidate-1
install -d -m 0700 "$candidate_dir"
known_hosts=$candidate_dir/known_hosts
test ! -e "$known_hosts"
umask 077
ssh-keyscan -T 5 -t ed25519 10.66.0.100 > "$known_hosts"
ssh-keygen -l -E sha256 -f "$known_hosts"
ssh -o UserKnownHostsFile="$known_hosts" \
  -o StrictHostKeyChecking=yes core@10.66.0.100
```

The existing read-only `scripts/inventory-fabric-node` runs as root only after
asserting `/run/ostree-live`. It leaves a core-owned, mode-`0600` report and
checksum at
`/var/home/core/fabric-inventory/fabric-candidate.txt{,.sha256}`. Copy both
files into a uniquely named local candidate directory using the same strict
host-key options, then verify them there:

```sh
scp -o UserKnownHostsFile="$known_hosts" \
  -o StrictHostKeyChecking=yes \
  core@10.66.0.100:fabric-inventory/fabric-candidate.txt \
  core@10.66.0.100:fabric-inventory/fabric-candidate.txt.sha256 \
  "$candidate_dir/"
(cd "$candidate_dir" && sha256sum --check --strict fabric-candidate.txt.sha256)
```

Collection is capped at 15 minutes. A timeout or command failure still
preserves and checksums the partial report, appends an explicit incomplete
status, and fails the collection unit. Copy that pair off the ephemeral live
system before power-off.
The FCOS live environment may lack optional tools such as `smartctl`; the
report calls that out explicitly, and the missing SMART capture remains a
separate disk-admission gate rather than being silently treated as success.
Identity evidence is similarly explicit: the report records the DMI product
UUID, chassis serial and asset tag, each NIC's current sysfs address beside its
`ethtool -P` permanent address, TPM2 properties and allocated PCR banks,
current PCR values, and public metadata for existing standard EK-certificate
NV indices and persistent objects. When an EK-certificate index already exists,
it also reads that certificate and records its subject, issuer, serial,
validity, and SHA-256 fingerprint. Every unavailable tool, absent object, and
failed query gets a visible evidence-status marker. The collector never creates
an EK, persists a TPM object, writes TPM NV, or enrolls a secret; an absent EK
public/certificate record remains a later admission issue rather than license
to mutate the candidate during discovery.

This ISO never installs or writes a target disk. Do not confuse it with the
later node-specific, confirmation-gated installer images described below.

The initial three consensus nodes use local Fedora CoreOS ISO media, not PXE
or Bootie. `fcos-live-iso.txt` pins the exact official x86_64 live ISO, its
SHA-256 digest, and detached signature URL. Changing the stream pointer is not
enough: update and review all four fields together.

The same guarded writer also accepts the non-installing
`fabric-inventory-live.iso` used for the first read-only hardware capture. It
retains the same source and USB-device checks, but its confirmation starts with
`WRITE-FABRIC-INVENTORY:` so that an inventory-image write cannot be confirmed
with an installer-image ceremony. Keep the inventory ISO and its adjacent
checksum outside Git as mode-`0600` regular files, inspect the USB in a dry run,
and copy only the exact confirmation emitted for that image and device.

The eventual installer artifacts are three separate secret-bearing images:

- `fabric-az1-cp1-installer.iso`
- `fabric-az1-cp2-installer.iso`
- `fabric-az1-cp3-installer.iso`

Each image embeds only its matching Ignition and one admitted whole-disk
identity. Cp2 and cp3 require SATA SSDs named by exact
`/dev/disk/by-id/ata-*` paths. Cp1 is bound to the exact NVMe EUI below.

Sam has explicitly repurposed `fabric-az1-cp1`'s inventoried internal 256 GB
Samsung NVMe as that node's root disk. Its only accepted identity is the exact
lowercase `/dev/disk/by-id/nvme-eui.002538839100c827`; do not substitute a
model/serial alias or a kernel name. Its existing EFI, boot, and encrypted-root
contents are authorized for destruction by this installation. The armed
pre-install gate must revalidate that exact identity and receive its
device-specific confirmation first.

One physical thumb drive may be used for inventory first and then rewritten
between node installers, but no ISO may contain multiple node Ignitions. After
the first installer write, classify the entire thumb drive as secret and never
return it to inventory or general use. The writer overwrites and verifies only
the ISO-length prefix; trailing bytes and flash-controller remapped cells are
not a secure erase. Generated ISOs and checksum files belong on trusted storage
outside this Git worktree and must remain mode `0600`; they contain etcd keys,
K3s tokens, and disk recovery material.

Retain the armed thumb drive in locked offline storage with the recovery
packet. Rewriting or reformatting does not declassify it. Decommission it only
after rotating every embedded cluster token, private key, and on-disk recovery
key, then physically destroy the device. Loss of the media is a compromise of
all installer secrets it may ever have carried, including earlier images whose
bytes are no longer visible through the current partition table.

Choose a USB thumb drive that exposes a non-empty hardware serial and keep its
physical label with the ceremony record. The writer refuses every media write,
including inventory media, on a serial-less device.

Do not generate a node's installation media until its firmware, UEFI, TPM,
RTC, memory, NIC, and disk changes and qualification are complete and that
candidate has a reviewed final recapture. It is not necessary to wait for the
other two candidates before manufacturing this node's media. Preserve every
initial discovery capture separately. Only a node's reviewed final capture may
assign its exact root device and permanent wired MAC address. Disconnect every
non-target internal disk before booting an armed installer. For the SATA
default this includes all NVMe devices; for `fabric-az1-cp1`, leave attached
only the exact target NVMe above and disconnect any other internal disk.

The local-console ceremony is deliberate:

1. Rewrite the installer thumb drive with exactly one node's customized ISO
   using `scripts/write-fabric-installer-usb`; its guarded apply path flushes
   the device and verifies exactly the ISO byte length against its recorded
   hash. This is integrity verification, not media sanitization. Never
   substitute a kernel path such as `/dev/sdX`.
2. Attach the USB HDMI adapter and keyboard, connect the intended wired NIC,
   and leave only the admitted root target installed.
3. Boot the USB device in UEFI mode. The pre-install gate must accept usable
   TPM2, a UTC clock inside the embedded etcd certificate validity window, the
   exact embedded NetworkManager profile UUID, static IP, and a usable globally
   administered unicast MAC, the same MAC as both the NIC's current address and
   `ethtool -P` permanent address, a 64 GiB-or-larger supported whole disk, and
   the router's exact seven-payload manifest. Confirm the displayed node name,
   complete stable disk by-id, disk model/serial, IP, and wired MAC.
4. Type the full device-specific confirmation only after physically comparing
   those values with the chassis label and inventory record.
5. A successful live install reports success and powers the machine off. Remove
   the armed installer, boot the installed root disk, and expect one later
   automatic reboot while the pinned K3s SELinux policy is layered. Before that reboot,
   the first-boot gate enrolls and verifies the node's offline root-volume
   recovery key, then deletes its installed copy. A failed install
   stops in the live emergency environment and the destination is not trusted.

The installer image also attempts to provide temporary diagnostic SSH as
`core` after the exact static profile has activated. It accepts only the
canonical operator key from `10.66.0.2`, disables forwarding, and is not copied
into the installed system. An SSH failure is deliberately non-fatal because
local admission and confirmation remain authoritative. Each boot generates an
ephemeral host key; compare the SHA256 fingerprint printed by the gate with a
fresh strict `known_hosts` scan before connecting. SSH cannot answer the
destructive confirmation: that read remains bound to `/dev/console`, so use
the physically attached keyboard or a locally verified USB-HID bridge.

Validate the guarded builder without downloading or producing secrets:

```sh
scripts/build-fabric-node-isos --check
```

This shallow check validates the public FCOS and seven-payload manifest
structure, pinned Fedora signing-key identity, canonical operator key, and
minimal live-only SSH profile. It deliberately does not inspect final
Ignitions, bind real MAC/disk identities, download the FCOS ISO, or build
installer media. Those hardware- and secret-bound checks run in normal mode
only, after the inventory and offline Ignitions exist.

After `fabric-az1-cp1` has a reviewed final capture, render only its Ignition
using the permanent wired MAC from that capture:

```sh
FABRIC_CP1_MAC=aa:bb:cc:dd:ee:01 \
  ./fabric/butane/bootstrap.sh --node fabric-az1-cp1 \
  /secure/fabric-bootstrap
```

Selected-node mode requires only the matching `FABRIC_CP*_MAC` and publishes
only that node's Ignition. Build only its installer with the matching disk
option and the exact stable identity from the same capture. For cp1, use the
explicitly admitted NVMe EUI identity:

```sh
scripts/build-fabric-node-isos \
  --node fabric-az1-cp1 \
  --ignition-dir /secure/fabric-bootstrap \
  --output-dir /secure/fabric-installers \
  --cache-dir /secure/fabric-installer-cache \
  --cp1-disk /dev/disk/by-id/nvme-eui.002538839100c827
```

The original all-three mode remains available: omit `--node`, provide all
three `FABRIC_CP*_MAC` variables when rendering, and provide `--cp1-disk`,
`--cp2-disk`, and `--cp3-disk` when building. Selected-node mode changes only
which secret artifact is manufactured. Every Ignition retains the fixed
declared `fabric-az1-cp1`/`cp2`/`cp3` etcd membership and
`initial-cluster-state=new`. A lone installed voter cannot form etcd quorum,
so the K3s API cannot become available until at least one other declared
member is brought up with it. Cp2 and cp3 use exact
`/dev/disk/by-id/ata-EXACT_DEVICE` paths from reviewed inventory.

After customization, normal mode re-opens every temporary ISO before it is
published. It verifies that the confirmation-gated pre-install unit runs
before and is required by `coreos-installer.service`, that the post-install
poweroff unit follows it, and that the embedded destination Ignition, offline
installer config, stable disk identity, NetworkManager keyfile, and live-only
SSH profile exactly match their sources. It also verifies that the SSH service
is pulled into the auto-installer dependency graph rather than relying on the
unused live `multi-user.target`. `coreos-installer` itself prints that an
embedded destination installs "without confirmation"; that refers to its
built-in prompt. The required confirmation is the separately verified
pre-install unit, and a failed or missing unit prevents the installer service
from starting.

After inventory admission and Ignition rendering, supply the selected node's
exact admitted by-id path, or all three paths in legacy all-member mode, as shown
by `scripts/build-fabric-node-isos --help`. The helper
verifies the ISO checksum and Fedora signature, refuses paths inside Git,
refuses unstable or unsupported destinations, extracts the expected permanent MAC
plus hostname and static IP from each Ignition, rejects locally administered,
multicast, and all-zero expected MACs, and embeds a pre-install network, TPM2,
router-asset, disk-size, and console challenge containing the full node-specific
disk identity. At boot that gate uses `ethtool -P` and requires the admitted
permanent MAC and current interface address to agree. The live ISO disables
CoreOS' automatic reboot and its post-install hook powers off only after
successful installation. Do not improvise an unattended
`coreos-installer` command or substitute a destination such as `/dev/sda`.

The public FCOS ISO cache defaults to `_build/fabric/installer`. Use
`--cache-dir /secure/fabric-installer-cache` when the checkout filesystem does
not have enough space. Keep the cache and the eventual secret installer
outputs on a trusted filesystem with roughly 8 GiB free; only the Ignitions,
customized ISOs, and their sidecars are secret-bearing. Transient decoded
Ignition and certificate work is restricted to verified tmpfs; the builder
requires at least 64 MiB free there.

The builder writes a mode-`0600` `<installer>.iso.sha256` beside every ISO.
Inspect a candidate USB disk without changing it first:

```sh
scripts/write-fabric-installer-usb \
  --iso /secure/fabric-installers/fabric-az1-cp1-installer.iso \
  --device /dev/disk/by-id/usb-VENDOR_MODEL_SERIAL-0:0
```

Physically compare the reported model, serial, size, and stable by-id path,
then copy the exact `sudo ... --apply --confirm ...` command printed by that
dry run. The writer refuses mounted, read-only, held, or system disks and will
not perform any block-device write without both apply arguments.
