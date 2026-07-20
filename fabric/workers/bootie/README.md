# Worker-only Bootie induction

The worker manufacturing path is now PXE-capable without weakening the
existing inventory and disk gates. The transitional attachment is an attended
external station at `10.66.0.2` on the same trusted Ethernet broadcast domain
as both prefixes. It still stops before starting a network service because the
station runner, per-node credential, and final inventory remain attended
artifacts rather than implicit repository state.

This is separate from the retired cp3 exception under `fabric/bootie`. That
station is hard-coded to one consensus Node, `fabric-observer`, the
`10.66.0.0/24` inventory address, and temporary cp3 RBAC. Do not rename or
reuse it for services workers.

## What is implemented

The committed admission record remains the authority for chassis, permanent
MAC, TPM2, destination disk, and evidence hash. The sensitive worker renderer
continues to accept only a full CA-pinned K3s agent or short-lived bootstrap
token from a mode-`0600` file outside Git.

After one worker is admitted and its Ignition has been rendered,
`scripts/prepare-fabric-worker-bootie-handoff` runs the canonical audited
installer builder in tmpfs, recovers the live Ignition that builder already
verified, extracts the exact NetworkManager keyfile from that customized ISO,
and wraps both into the pinned public PXE initramfs. It then unwraps both
payload classes and proves the Ignition is semantically identical and the
keyfile is byte-identical, MAC-bound, and addressed for the selected worker.
It also extracts and binds the canonical ISO's exact live-kernel-argument
delta, `coreos.inst.skip_reboot systemd.show_status=false`; neither argument
is carried by Ignition or the NetworkManager payload, and PXE must preserve
both explicitly. The manifest binds the signed kernel, public rootfs, embedded
network keyfile, and those lifecycle arguments from that same pinned FCOS ISO.
The resulting initramfs contains the same attended pre-install checks as the
local-media fallback:

- exact chassis serial, product UUID, TPM2, permanent MAC, disk by-id, serial,
  and byte size;
- exact static services address and NetworkManager profile;
- exact router asset manifest; and
- an exact console confirmation before the destination is touched.

The handoff contains only three mode-`0600` files: the customized initramfs,
one-record install policy, and a manifest binding their hashes to the admitted
inventory and Git commit. It is secret because the initramfs embeds the K3s
credential and destination Ignition. The helper refuses Git or durable output
and does not start DHCP/HTTP, mutate Kubernetes, arm an install, or write a
disk.

Bootie supports this artifact with
`BOOTIE_INSTALL_DELIVERY=custom-initramfs`. That mode fails closed unless all
of the following are also true:

- `BOOTIE_NODE_NAME` fixes one predeclared Node;
- `BOOTIE_ALLOW_NODE_CREATE=false`;
- `BOOTIE_REQUIRE_INSTALL_POLICY=true` and the mounted one-record policy
  matches the Node's exact boot-device annotation;
- `BOOTIE_REQUIRE_BOOTSTRAP_STATE=true` and the Node is explicitly
  `install-armed`; and
- `BOOTIE_CUSTOM_INITRAMFS_NAME` is a fresh 128-bit-or-stronger lowercase-hex
  capability name ending in `.img`;
- `BOOTIE_CUSTOM_FCOS_VERSION` exactly matches the FCOS version bound by the
  handoff; and
- `BOOTIE_CUSTOM_INITRAMFS_SHA256` matches the copied runtime snapshot; and
- `BOOTIE_CUSTOM_LIVE_KARGS` is exactly
  `coreos.inst.skip_reboot systemd.show_status=false`; and
- `BOOTIE_EXPECTED_NODE_UID` matches the placeholder UID recorded alongside
  the ServiceAccount/RBAC UID receipts at issuance.

Bootie atomically consumes the install annotation and advances the reviewed
bootstrap state only after testing both that UID and the current
`resourceVersion`, before returning that initramfs name. It does not append a
second CoreOS installer destination or create a redundant Ignition token. It
rejects a placeholder with stale Ignition authorization and tests the Node
resourceVersion in the same arm patch. The capability download itself remains
replayable to an observer until the station is stopped; only arm consumption
and name disclosure are one-use.

## Transitional station contract and remaining gates

The physical design for first induction is now selected. Daisy-chained
unmanaged switches carry root `10.66.0.0/24` and services `10.66.1.0/24` on
one broadcast domain. OpenWrt owns `.0.1` and `.1.1` on the surviving LAN port
and routes between the prefixes on that same interface. The attended station
stays at `10.66.0.2`; its exact-MAC DHCP/PXE exchange reaches the candidate as
a normal L2 broadcast, so no DHCP relay or temporary services address is
needed on the station.

PXE delivery completes while the live system still has its station-provided
address. The pre-install gate then activates the final `10.66.1.x` profile. If
live SSH is needed after that transition, the isolated station namespace needs
the explicit `10.66.1.0/24 via 10.66.0.1` route; it must not gain a default
route or an on-link shortcut to the services prefix. The worker reply then
returns through `10.66.1.1`, exercising the intended hairpin in both
directions.

The fetch order matters. Firmware first receives exact-MAC DHCP and TFTP from
the station. Signed GRUB then fetches `/boot.grub`, the signed kernel, and the
capability-named custom initramfs from `10.66.0.2` while the candidate still
owns its temporary `10.66.0.100` address. Only after those objects are in
memory does the kernel start and the embedded NetworkManager keyfile adopt
`10.66.1.10` or `.11`. Dracut subsequently fetches the public live rootfs from
the kernel's `coreos.live.rootfs_url=http://10.66.0.2/static/...`, so the
router must allow only the selected service addresses to reach exactly
`10.66.0.2:80`. Install-mode Ignition is already embedded in the custom
initramfs and has no later station URL. The pre-install gate finally fetches
the checksum-pinned asset manifest from `10.66.1.1:80` after the service
address is active. The station runner mounts an override that binds Nginx only
to `10.66.0.2:80` and refuses readiness if any other HTTP listener appears.

The three root nodes remain ineligible station hosts: they may run only etcd,
K3s control-plane components, and kube-vip. Svc1 and svc2 also cannot host the
provisioner which creates themselves. The runner must therefore execute from
the existing external observer/station attachment, bind only its isolated
fabric interface, and keep DHCP/TFTP/HTTP/FastCGI together until every
post-decision boot artifact has been delivered and the attended live system
has entered its pre-install gate.

This chooses attachment, not trust. The shared wire cannot enforce the final
services/consensus boundary, and both admission records are still
intentionally `pending`. Before arming a worker, `10.66.1.1/24` and its pinned
assets must be live, same-interface router and root-host rules must pass their
positive K3s and negative etcd probes, the station runner must be reviewed,
and that candidate's final inventory values must be admitted. Until a managed
switch enforces the two VLANs, provision only trusted Flux/CoreDNS/metrics
plumbing; do not schedule untrusted workloads or hosted child etcd.

The later VLAN cutover changes only switch/router membership. The worker
addresses, Bootie admission identities, and explicit
`10.66.0.0/24 via 10.66.1.1` routes stay unchanged.

Do not solve the chicken-and-egg problem by tolerating Bootie onto a root node,
putting the agent token in a Kubernetes Secret, exposing the custom initramfs
from durable storage, or restoring installer USB as the default path.

## Resumable ceremony after those facts exist

1. PXE-boot exactly one candidate into the public non-installing inventory
   profile and preserve its final capture.
2. Review the capture, change only that Node's Git admission from `pending` to
   `admitted`, merge it to pushed `main`, and verify the services firewall and
   etcd-denial probes.
3. Fetch the live CA-pinned agent token into verified tmpfs and render one
   sensitive destination Ignition as described in `fabric/workers/README.md`.
4. Prepare the unarmed PXE payload on tmpfs:

   ```sh
   scripts/prepare-fabric-worker-bootie-handoff \
     --node fabric-az1-svc1 \
     --ignition-dir /secure/fabric-worker-ignitions \
     --output-dir /dev/shm/fabric-az1-svc1-pxe \
     --cache-dir /secure/fabric-installer-cache \
     --disk /dev/disk/by-id/EXACT_ADMITTED_WHOLE_DISK
   ```

5. Stage the public exact-MAC tree with
   `scripts/stage-fabric-worker-bootie-pxe`, then issue one four-hour identity
   with `scripts/issue-fabric-worker-bootie-access`. Build the Bootie image
   with OCI label `org.opencontainers.image.revision` equal to the full pushed
   commit recorded by the stage. Start
   `scripts/run-fabric-worker-bootie-station` directly; it re-execs itself as
   the exact node-specific transient systemd service required by the arm
   helper. The service re-verifies `handoff.json`, unwraps and checks the
   embedded network keyfile and exact live kernel arguments, snapshots the
   public kernel/rootfs, copies the initramfs into root-controlled tmpfs under
   a new 192-bit capability name, and serves that single-link mode-`0644` file
   read-only. The handoff helper itself refuses a dirty worktree, a non-`main`
   branch, or a `main` HEAD different from freshly fetched `origin/main`.
6. Run public discovery and destructive installation as separate station
   lifetimes and credentials. For installation, set the exact Node boot-device
   and `install-armed` state in one attended transaction. A successful
   `/boot.grub` response only consumes the decision; it is not permission to
   stop the station. Keep the unit active while GRUB fetches the kernel and
   custom initramfs and while dracut fetches the rootfs after adopting the
   final service address. The host station writes a container-inaccessible,
   root-only receipt at
   `/run/fabric-worker-bootie-svcN/evidence/rootfs-delivery-complete.json` only
   after logging an exact HTTP 200 GET whose source is the selected `.1.x`
   address and whose transmitted bytes equal the pinned rootfs size. Require
   both that receipt/the journal marker
   `ROOTFS-DELIVERY-COMPLETE:<node>:<source>:<bytes>` and physical console
   evidence that the live pre-install gate is running. Only then stop the
   transient unit, which tears DHCP/TFTP/HTTP/FastCGI down together. It first
   asks Podman to stop, force-removes the exact container if needed, and proves
   that the container plus all four listeners are absent before deleting the
   runtime snapshot. If any proof fails it leaves the complete
   `/run/fabric-worker-bootie-svcN` evidence intact and exits failed.
7. Run `revoke-fabric-worker-bootie-access` immediately. It deletes only the
   issued RBAC UIDs, zeroes the token, and preserves a non-secret Node-UID
   receipt when the response was issued. The source PXE handoff and runtime
   capability can now be destroyed; neither is needed for finalization. Boot
   the local disk, wait for the exact UID to become the Ready worker at its
   assigned InternalIP, then run `finalize-fabric-worker-bootstrap`. That
   command atomically proves the MAC, UID, name, IP, Ready condition, platform
   labels, and exact taint before removing bootstrap metadata and destroying
   the revoked tmpfs receipt.
8. If the response was consumed but physical console observation proves it
   never reached the candidate, ordinary revocation deliberately does not
   guess. After revocation, the separately confirmed
   `reset-fabric-worker-bootie-response` requires the exact UID to have no
   machine ID, InternalIP, or Ready condition before returning it to discovery;
   a completely fresh issue/arm ceremony is then required. Repeat independently
   for svc2.

An uncatchable SIGKILL can interrupt the service before its EXIT cleanup. On
the next attempt the runner refuses a stale transient unit, exact Podman
container, listener, or runtime path; it never unlinks that evidence on sight.
Use the unit journal to establish where teardown stopped, stop/force-remove
only `fabric-worker-bootie-svcN`, and independently prove ports 80, 9000, 67,
and 69 absent in `fabric-observer` before deleting the retained runtime. If
that absence cannot be proved, leave the runtime intact and do not revoke or
re-arm. The unit's 45-second stop timeout leaves margin for its ten-second
orderly Podman stop, force-removal fallback, and absence checks.

The selected topology now supplies the station address and routing shape. The
worker runner encodes the external station's isolated interface at
`10.66.0.2` and serves only the admitted MAC. The transitional OpenWrt profile
adds `10.66.1.1/24` to the existing fabric LAN, routes both prefixes on that
interface without redirects, retains the reviewed root-etcd denial, and admits
only `.1.10/.11` to `10.66.0.2:80` for the post-profile live-rootfs fetch. Its
exact rule and negative source/host/port matrix must pass before arming.
