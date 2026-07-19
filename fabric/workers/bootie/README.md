# Worker-only Bootie induction

The worker manufacturing path is now PXE-capable without weakening the
existing inventory and disk gates. It deliberately stops before starting a
network service because the physical location of that service is not yet a
safe repository fact.

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
verified, and wraps it into the pinned public PXE initramfs while proving the
wrapped config is semantically identical. The manifest binds the signed
kernel and public rootfs from that same pinned FCOS ISO. The resulting
initramfs contains the same attended
pre-install checks as the local-media fallback:

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
- `BOOTIE_CUSTOM_INITRAMFS_SHA256` matches the copied runtime snapshot.

Bootie atomically consumes the install annotation and advances the reviewed
bootstrap state before returning that initramfs name. It does not append a
second CoreOS installer destination or create a redundant Ignition token. It
rejects a placeholder with stale Ignition authorization and tests the Node
resourceVersion in the same arm patch. The capability download itself remains
replayable to an observer until the station is stopped; only arm consumption
and name disclosure are one-use.

## Current hard blocker

There is no eligible host for the attended Bootie station yet. The three root
nodes may run only etcd, K3s control-plane components, and kube-vip; placing a
provisioner or its worker credential there would violate the root placement
boundary. Svc1 and svc2 cannot host the provisioner that creates themselves.
The old cp3 station cannot reach or safely describe the allocated services
segment.

Before a runnable station can be committed, choose and cable one of these
physical designs:

1. Give an external attended station a dedicated NIC on the services L2. This
   needs an explicit temporary address allocation (the low-address range is
   currently held back), exact interface identity, and an update to the live
   installer SSH source policy.
2. Keep the attended station at `10.66.0.2` and make the router relay only the
   exact-MAC worker DHCP/PXE exchange across the services boundary. This needs
   explicit OpenWrt relay and inter-segment firewall rules plus a negative
   proof that workers still cannot reach etcd.

The services boundary itself is also only allocated on paper today:
`10.66.1.1/24` is not live, the current unmanaged flat switch cannot enforce
it, and both admission records are intentionally `pending`. The missing facts
are therefore concrete, not software guesses: station attachment/interface,
managed VLAN versus dedicated NIC/switch, and each candidate's reviewed
inventory values.

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

5. A reviewed station runner must verify `handoff.json`, copy the initramfs to
   its own root-controlled tmpfs under a freshly generated capability name,
   expose only that single-link file mode `0644` to the unprivileged HTTP
   worker at `/pxe`, mount it read-only, and use resourceName-scoped,
   short-lived RBAC for only the selected placeholder Node. It must verify and
   snapshot the handoff's exact public kernel and rootfs hashes, pass the
   manifest's FCOS version and initramfs hash to Bootie, and pin the Bootie
   image to the same pushed Git commit. The handoff helper itself refuses a
   dirty worktree, a non-`main` branch, or a `main` HEAD different from freshly
   fetched `origin/main`.
6. Run public discovery and destructive installation as separate station
   lifetimes and credentials. For installation, set the exact Node boot-device
   and `install-armed` state in one attended transaction. Stop DHCP/TFTP/HTTP
   together immediately after the response, then revoke RBAC and destroy both
   tmpfs trees.
7. Boot local disk, verify the exact K3s Node identity/IP/labels/taint and host
   firewall, then repeat independently for svc2.

The final station runner and OpenWrt configuration are intentionally not
invented until the selected physical design supplies their bind interface,
address, and routing boundary.
