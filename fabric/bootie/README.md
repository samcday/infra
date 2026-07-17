# Temporary cp3 Bootie station

This directory defines a bounded network-boot exception for completing
`fabric-az1-cp3`. It is not the fabric's permanent provisioner and is not a
worker-enrollment design. Cp1 and cp2 stay untouched and never expose their
Ignitions or keys to Bootie.

The station runs on the operator machine inside the existing
`fabric-observer` network namespace at `10.66.0.2`. It is absent from the
router and from Kubernetes. While the station is active, it provides:

- exact-MAC DHCP for one address, `10.66.0.100`, with no router or DNS option;
- TFTP for the pinned FCOS shim, GRUB, and MokManager binaries;
- HTTP for Bootie, the pinned FCOS PXE artifacts, and a one-use Ignition
  response; and
- a four-hour Kubernetes identity that can get and patch only the predeclared
  `fabric-az1-cp3` Node. It cannot create or list Nodes.

The service holds `/run/lock/fabric-network-operation.lock` for its entire
lifetime and couples Bootie and dnsmasq teardown. Run it as a transient systemd
service, not from an interactive shell.

## Security boundary

The staging helper verifies the pinned FCOS ISO digest and extracts its
Microsoft-signed shim, Fedora-signed GRUB, and Fedora-signed kernel. This
preserves the firmware-to-kernel Secure Boot signature chain when the candidate
firmware trusts the Microsoft third-party UEFI CA.

It does **not** make this authenticated netboot. TFTP does not authenticate the
GRUB configuration, and HTTP does not authenticate Bootie's response, the
initramfs, rootfs, or Ignition transport end to end. A machine on the same L2
can spoof a MAC. The physically controlled, isolated fabric L2 and attended
console are therefore part of the trust boundary. Do not run this station on a
shared LAN, bridge the observer namespace elsewhere, or add an off-fabric
route while it is active.

The remaining limits reduce the consequence of a mistake or compromise:

- Only the reviewed, globally administered permanent cp3 MAC gets a lease.
- Node creation is disabled. Bootie is fixed to `fabric-az1-cp3`, and its
  projected token cannot read cp1, cp2, or the Node collection.
- Discovery serves only the guarded, non-installing inventory Ignition built
  from `fabric/installer/inventory-live.bu`. The public staging tree contains
  no node key, cluster token, recovery key, destination, or installer arm.
- The complete cp3 Ignition is one mode-`0600` file mounted read-only from
  tmpfs only for the final install. It is never copied to the staging tree,
  Git, a Kubernetes Secret, the router, or durable station storage.
- The exact stable whole-disk by-id must agree in both the cp3 Node annotation
  and a separate read-only install policy. Merely declaring a boot device does
  not arm installation.
- Bootie atomically consumes `samcday.com/install: "true"` before returning one
  installer boot response, then binds the Ignition response to a separate
  random one-use token. A failed or lost response requires an explicit new
  arm; it must never fall through into an automatic retry.
- Stopping the service removes DHCP, TFTP, and HTTP together. Revocation then
  removes the temporary ServiceAccount/RBAC, clears stale one-use annotations,
  and zeroes the local projected token.

The station needs the current two-member K3s API only during this attended
ceremony. Once cp3 is installed it boots from local disk and joins the static
three-member etcd map already present in its Ignition. Do not run `etcdctl
member add`. If either current voter is lost before cp3 joins, recover quorum;
Bootie is not an out-of-band replacement for consensus.

## 1. Identify cp3 without starting DHCP

Connect only the intended cp3 wired NIC and the attended console. Leave local
installer media disconnected. With the candidate powered off, verify the
observer namespace and start a passive capture:

```sh
sudo fabric/observer/fabric-observer-netns verify-isolation
sudo flock --exclusive /run/lock/fabric-network-operation.lock \
  ip netns exec fabric-observer \
    tcpdump -Z root -l -e -n -i wlp95s0 \
    'udp src port 68 and udp dst port 67'
```

Power the candidate and select its UEFI network boot entry. Record the source
MAC from DHCP discovery and compare it with firmware and physical inventory.
Stop if it is multicast, locally administered, all-zero, changes across boots,
or does not match the permanent wired identity. No DHCP service should be
running during this capture.

## 2. Stage the public PXE tree

Use the exact ISO named and hashed by `fabric/installer/fcos-live-iso.txt` and a
new output directory outside Git:

```sh
scripts/stage-fabric-bootie-pxe \
  --iso /trusted/cache/fedora-coreos-VERSION-live-iso.x86_64.iso \
  --mac "$CP3_PERMANENT_MAC" \
  --output-dir /trusted/fabric-cp3-pxe

(cd /trusted/fabric-cp3-pxe && sha256sum --check --strict SHA256SUMS)
```

The helper refuses an existing output directory, a mismatched ISO, a local or
multicast MAC, and output inside this worktree. The resulting tree is public
and non-arming: its `install-policy` is empty and its placeholder Node remains
in discovery mode. It retains the ISO's `EFI/BOOT` layout and also snapshots
GRUB, MokManager, and the MAC-bound GRUB configuration at the TFTP root because
PXE-loaded shim requests its peer files there.

Build and test the pinned Bootie image before the ceremony, then make that
exact image available in root's Podman store as
`localhost/fabric-bootie:cp3`. Record its image ID with the evidence bundle.
Do not let a mutable registry pull occur when the station starts.

## 3. Issue bounded API access

Use a strictly pinned SSH host key for either healthy commissioned root. The
access output must be a new directory on tmpfs; the helper validates that
filesystem, the four-hour token claims, and negative authorization against
cp1 and Node listing before it succeeds.

```sh
sudo scripts/issue-fabric-bootie-access \
  --root-address 10.66.0.10 \
  --known-hosts /trusted/fabric-cp1-known-hosts \
  --identity /trusted/id_ed25519 \
  --stage-dir /trusted/fabric-cp3-pxe \
  --output-dir /dev/shm/fabric-cp3-bootie-access \
  --confirm ISSUE:fabric-az1-cp3-bootie:4h
```

This is an attended, direct Kubernetes mutation. It creates the temporary
RBAC objects and either creates or exactly reuses an unjoined cp3 placeholder
with the staged MAC. It refuses to adopt an existing real Node or a placeholder
with different identity. Do not issue access until the actual chassis and MAC
are known.

## 4. Run discovery

Start the station through a transient unit using absolute paths:

```sh
repo=$(git rev-parse --show-toplevel)
sudo systemd-run \
  --unit=fabric-cp3-bootie-station \
  --collect \
  --service-type=exec \
  --property=Restart=no \
  --property=MemoryMax=3G \
  --property=TasksMax=128 \
  "$repo/scripts/run-fabric-bootie-station" \
    --mode discovery \
    --stage-dir /trusted/fabric-cp3-pxe \
    --access-dir /dev/shm/fabric-cp3-bootie-access \
    --image localhost/fabric-bootie:cp3

sudo systemctl status fabric-cp3-bootie-station.service
sudo journalctl -fu fabric-cp3-bootie-station.service
```

The 3 GiB service ceiling leaves room for the runner's root-owned tmpfs
snapshot of the three FCOS artifacts it actually serves. This prevents the
caller-owned staging tree from changing underneath a live station.

Only after the log reports that the exact-MAC DHCP listener is ready should
the candidate boot its UEFI network entry. Secure Boot must remain enabled.
The candidate is reachable only inside `fabric-observer`; the root namespace
must not gain a route to it. From Sam's non-root shell, run the network clients
inside that namespace and immediately drop back to user `sam`, so the normal
SSH identity is used and every evidence file remains Sam-owned:

```sh
candidate_dir=/secure/fabric-inventory/fabric-az1-cp3-$(date -u +%Y%m%dT%H%M%SZ)
test ! -e "$candidate_dir"
install -d -m 0700 "$candidate_dir"
known_hosts=$candidate_dir/known_hosts
umask 077

fabric_as_sam=(
  sudo ip netns exec fabric-observer
  runuser --user sam --
)

"${fabric_as_sam[@]}" ssh-keyscan -T 5 -t ed25519 10.66.0.100 \
  > "$known_hosts"
test -s "$known_hosts"
ssh-keygen -l -E sha256 -f "$known_hosts"
```

Compare that fingerprint byte-for-byte with the ephemeral ED25519 fingerprint
shown on the attended physical console before making any SSH connection. Then
inspect progress, copy both inventory files, and verify the pair without
leaving the observer namespace:

```sh
"${fabric_as_sam[@]}" ssh \
  -o UserKnownHostsFile="$known_hosts" \
  -o StrictHostKeyChecking=yes \
  core@10.66.0.100 \
  'sudo systemctl status fabric-inventory.service'

"${fabric_as_sam[@]}" scp \
  -o UserKnownHostsFile="$known_hosts" \
  -o StrictHostKeyChecking=yes \
  core@10.66.0.100:fabric-inventory/fabric-candidate.txt \
  core@10.66.0.100:fabric-inventory/fabric-candidate.txt.sha256 \
  "$candidate_dir/"

(cd "$candidate_dir" && \
  sha256sum --check --strict fabric-candidate.txt.sha256)
test "$(stat -Lc '%U:%G:%a' "$candidate_dir"/fabric-candidate.txt*)" = \
  $'sam:sam:600\nsam:sam:600'
```

Review the report against every admission gate described in
`fabric/installer/README.md`. Copy the report before powering the live system
off; its host key and local copy are ephemeral.

Discovery has no destination and cannot install. Stop and revoke after the
report is copied, even if a final install will follow; issue a fresh bounded
token for the destructive ceremony:

```sh
sudo systemctl stop fabric-cp3-bootie-station.service

sudo scripts/revoke-fabric-bootie-access \
  --root-address 10.66.0.10 \
  --known-hosts /trusted/fabric-cp1-known-hosts \
  --identity /trusted/id_ed25519 \
  --access-dir /dev/shm/fabric-cp3-bootie-access \
  --confirm REVOKE:fabric-az1-cp3-bootie

sudo rm -rf -- /dev/shm/fabric-cp3-bootie-access
```

## 5. Prepare the one-use install handoff

Do not proceed until the inventory is reviewed, cp3 has passed its TPM,
firmware, memory, NIC, disk, thermal, RTC, and SMART gates, and every accepted
synchronous-write exception is separately preserved. The exact whole-disk
by-id must be committed to the cp3 disk policy. Disconnect every non-target
internal disk. Sam must authorize destruction of that exact path; model, serial
alias, `/dev/nvme0n1`, and `/dev/sdX` are not substitutes.

The admitted cp3 device is the 256 GB Samsung `MZVLW256HEHP-000L7`, serial
`S35ENX0JB81948`, with exact size `256060514304` and sole accepted identity
`/dev/disk/by-id/nvme-eui.002538bb71b4bb45`, now installed in the native-TPM2
replacement M710q chassis `S4FQ1133`. Its etcd-style synchronous-write run
recorded 77,563 samples and missed the deliberately strict synthetic 100,000
sample, 500-IOPS, 5 ms p95, and 10 ms p99 targets, but its flush trace,
kernel/SMART integrity, p99.9, and maximum-latency checks passed. Sam accepted
that result only through a separately preserved
external evidence exception. The exception admits this exact disk; it does not
authorize installation, replace the exact one-use `ARM:` ceremony below, or
waive the later power-loss/restore validation.

```sh
AUTHORIZED_CP3_DISK_BY_ID=/dev/disk/by-id/nvme-eui.002538bb71b4bb45
```

Create the final handoff with the reviewed helper. It renders cp3 in verified
tmpfs, calls the repository's canonical disk-policy validator, and produces
the required Ignition, one-record policy, and hash-binding manifest. It
refuses every disk except the committed cp3 identity above. This step contains
secrets but does not mutate Kubernetes, arm an install, or write a disk.

```sh
scripts/prepare-fabric-cp3-bootie-handoff \
  --stage-dir /trusted/fabric-cp3-pxe \
  --disk "$AUTHORIZED_CP3_DISK_BY_ID" \
  --output-dir /dev/shm/fabric-cp3-install
```

Issue a new four-hour root-owned access directory using the section 3 command
with output `/dev/shm/fabric-cp3-bootie-access-install`. Then start the station
in install mode. The runner validates the handoff again and snapshots it into
root-owned runtime tmpfs before mounting it; the caller-owned source is never
served directly.

```sh
sudo systemd-run \
  --unit=fabric-cp3-bootie-station \
  --collect \
  --service-type=exec \
  --property=Restart=no \
  --property=MemoryMax=3G \
  --property=TasksMax=128 \
  "$repo/scripts/run-fabric-bootie-station" \
    --mode install \
    --stage-dir /trusted/fabric-cp3-pxe \
    --access-dir /dev/shm/fabric-cp3-bootie-access-install \
    --image localhost/fabric-bootie:cp3 \
    --handoff-dir /dev/shm/fabric-cp3-install
```

Starting this service still does not install. It requires the Node's safe
discovery baseline and reports readiness only after the fixed profile, policy,
scoped API identity, FastCGI, HTTP, DHCP, and TFTP checks pass. Only then run
the destructive helper with the exact full by-id copied from Sam's
authorization:

```sh
sudo scripts/arm-fabric-cp3-bootie-install \
  --stage-dir /trusted/fabric-cp3-pxe \
  --access-dir /dev/shm/fabric-cp3-bootie-access-install \
  --handoff-dir /dev/shm/fabric-cp3-install \
  --confirm "ARM:fabric-az1-cp3:$AUTHORIZED_CP3_DISK_BY_ID"
```

The helper proves the running station mounted the same Ignition hash and
one-record policy, rechecks the permanent MAC and unjoined placeholder through
the resourceName-scoped token, then atomically clears discovery and adds the
exact device plus one install arm. Preserve its output and the original exact
destructive authorization with the commissioning evidence. Do not improvise
a broader ServiceAccount or bypass the two-source device check.

Boot the candidate's UEFI network entry once. Bootie removes the install arm
before returning installer kernel arguments and consumes the matching
Ignition token before serving the complete cp3 file. If either request is lost
or refused, stop and inspect the station and Node state; re-arming is a new
attended destructive operation.

## 6. Tear down and verify local boot

As soon as the installer succeeds, fails, or is abandoned:

1. Stop `fabric-cp3-bootie-station.service`.
2. Run `sudo scripts/revoke-fabric-bootie-access` against the install access
   directory with its exact confirmation.
3. Remove the tmpfs access and handoff directories and verify that neither the
   container nor DHCP/TFTP/HTTP listeners remain.
4. Remove network boot from the candidate's immediate boot order and boot its
   installed disk.

Verify all three static etcd members are healthy, the cluster has one leader,
all three K3s Nodes are Ready, and the API VIP survives a controlled holder
change. Cp3 must use the already-declared member name, address, certificates,
initial-cluster token, and fresh local `/var/lib/etcd`; never add a fourth etcd
member to compensate for a failed ceremony.

Token expiry is not teardown. Revocation and tmpfs removal are mandatory even
when no install response was emitted.
