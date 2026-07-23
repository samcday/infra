# boot(**i**ncredibly **e**asily)

bootie is a simple container to facilitate PXE booting k8s nodes with FCOS.

Fabric day-two mode is the steady-state cluster deployment. One host-networked
Pod runs on either service node, serves only the five admitted hardware MACs,
and embeds the SHA-256-pinned FCOS live payload plus its signed shim and GRUB.
Every machine may therefore keep IPv4 PXE first in UEFI boot order. An ordinary
request receives only GRUB `exit` and continues to the local disk.

An installation response exists only while all of these agree: the exact
inventory record, the replacement Kubernetes Node UID, a short-lived
`bootie-session` Secret UID, the global maintenance-lock UID, the destination
disk, the Git revision, the Ignition digest, and the attested Bootie image
digest/revision. Bootie consumes the Node's install annotation before returning
installer arguments, and consumes the separate Ignition token before returning
the fixed profile. The session Secret is absent in steady state and is removed
after that one fetch. Set `BOOTIE_DAY2_MODE=true` only through
`/bin/run-cluster`; the original generic and attended-station modes remain for
offline recovery.

It has four HTTP endpoints:

 * `/boot.ipxe?mac=&serial=`. iPXE clients chain this URL and receive
   instructions to boot FCOS. If no k8s Node yet exists matching the MAC or
   serial, a petname is generated and the Node is created.
 * `/boot.grub?mac=&serial=`. A signed GRUB chain can request the same decision
   as GRUB commands. Set `FCOS_GRUB_BASE` to GRUB's network-device path, for
   example `(http,10.66.0.2)/static`; `FCOS_BASE` remains the ordinary URL used
   in the FCOS rootfs kernel argument.
 * `/ignition/<node>`. This generates the appropriate Ignition config to
   provision the Node. It's assumes that an `/ignition` directory exists
   which contains, at a minimum, a `base.ign` config. Additional profiles
   can be selected by annotating the node with `samcday.com/boot-profiles`.
 * `/custom-initramfs/<capability>`. A bounded worker-install ceremony uses
   this instead of `/ignition`: the handler revalidates and streams the exact
   customized PXE initramfs selected by the one-use boot response.

`BOOTIE_PUBLIC_ORIGIN` is required and supplies the fixed HTTP(S) origin in
every generated Ignition URL. Bootie never trusts the request's `Host` header
for this security-sensitive redirect.

## Installation safety

Bootie treats disk identity and permission to install as separate concerns:

* `samcday.com/boot-device` records the stable device path that an installation
  would target. Its presence alone never starts the CoreOS installer.
* `samcday.com/install: "true"` arms exactly one PXE response for installation.
  Bootie atomically removes this annotation before it returns installer kernel
  arguments. If the boot fails before installation completes, an operator must
  explicitly arm the Node again.
* Setting `BOOTIE_REQUIRE_INSTALL_POLICY=true` additionally requires
  `BOOTIE_INSTALL_POLICY_FILE`. Each non-comment record in that read-only file
  is exactly `NODE DEVICE`. An armed response is refused unless it finds one
  record for the Node and the annotated boot device matches it byte-for-byte.
* A bounded ceremony can set `BOOTIE_REQUIRE_BOOTSTRAP_STATE=true`. An armed
  response is then accepted only from
  `fabric.samcday.com/bootstrap-state=install-armed`; consuming the response
  atomically advances that state to `install-response-issued` alongside the
  one-use Ignition token.
* A single-machine station can set `BOOTIE_NODE_NAME` and
  `BOOTIE_ALLOW_NODE_CREATE=false`. Bootie then reads only that predeclared
  Node, verifies the request MAC or serial against its labels, and refuses
  unknown hardware without needing permission to list or create Nodes.
* That station can also set `BOOTIE_FIXED_IGNITION_FILE` to a mode-0600,
  read-only, complete Ignition JSON file. Bootie returns it only when the URL
  Node matches `BOOTIE_NODE_NAME`, discovery mode is cleared, and a valid
  one-use install token has already been consumed. Generic profile merging is
  unchanged when this setting is absent.
* A confirmation-gated installer manufactured into a customized FCOS PXE
  initramfs uses `BOOTIE_INSTALL_DELIVERY=custom-initramfs`. This mode is
  accepted only with a fixed Node, disabled Node creation, the exact-device
  install policy, and the reviewed bootstrap-state gate all enabled. Set
  `BOOTIE_CUSTOM_INITRAMFS_NAME` to the random 32-to-64-lowercase-hex
  capability plus `.img` that the station placed under `/pxe`; Bootie emits
  it only after atomically consuming the install arm and does not create a
  redundant Ignition token or installer kernel argument. The corresponding
  `/pxe` file must be a regular, single-link mode-`0644` runtime snapshot; its
  parent remains a root-controlled tmpfs and the filename is the capability.
  `BOOTIE_CUSTOM_FCOS_VERSION` must exactly match `FCOS_VERSION`, and
  `BOOTIE_CUSTOM_INITRAMFS_SHA256` must match the runtime snapshot before the
  request is inspected. `BOOTIE_CUSTOM_LIVE_KARGS` must be exactly
  `coreos.inst.skip_reboot systemd.show_status=false`; Bootie appends those
  reviewed ISO lifecycle controls to both GRUB and iPXE custom-initramfs
  responses. `BOOTIE_EXPECTED_NODE_UID` must be the exact UUID captured when
  the placeholder and scoped identity were issued. Bootie compares it with
  the fixed Node snapshot and includes it in the atomic install-consumption
  patch, so a same-name replacement cannot inherit the custom initramfs. A
  fixed Node with a stale Ignition token or mode is
  refused; the arm patch tests the Node `resourceVersion` so a concurrent
  mutation cannot cross that check.

The boot response names `/custom-initramfs/<capability>`, whose FastCGI
handler reopens, rechecks, and directly streams the same configured file.
It never validates one path and redirects Nginx to a different `/pxe` inode.
FastCGI buffering and access logging are disabled for this capability route.
Nginx separately returns 404 for the exact lowercase 32-to-64-hex `.img`
capability shape beneath `/static/`, preventing the shared `/pxe` alias from
becoming an unchecked alternate route while preserving ordinary FCOS assets.

Every ordinary PXE response carries a random `samcday.com/ignition-token`
bound to an `install` or `live` mode. The Ignition endpoint atomically verifies
and consumes both before returning any profile. Appending `install=1` to a
guessed or live-mode URL therefore cannot bypass the install arm, and
concurrent requests cannot both consume the same authorization. Customized
initramfs delivery instead consumes the arm before disclosing its random
static capability name. Only the arm and name disclosure are one-use: the
static object can be replayed by a party that observes the capability until
the attended station is torn down. These controls are not replacements for
keeping Bootie on an isolated, attended LAN.

The container also serves a mounted `/pxe` directory beneath `/static/`. This
is intended for the pinned public kernel, stock initramfs and rootfs; directory
listings are disabled. Bootie does not download or verify those artifacts
itself. A custom initramfs contains the destination Ignition and its cluster
credential: mount it from tmpfs under a fresh random capability name, expose
it only through `/custom-initramfs/` for the attended one-node ceremony, then
stop the station and destroy the snapshot.

A declared Node with a boot device but no install arm receives an iPXE `exit`
and continues to its local boot device. An unknown Node, or a declared Node
without a boot device, continues to live-boot FCOS for non-destructive hardware
inventory.

Unknown Nodes are created with `samcday.com/discovery=true`. Their Ignition is
limited to `common/discovery.ign`, which provides Sam's SSH key but does not
install or start k3s or etcd. Clear this label deliberately when declaring the
machine's real role. Bootie refuses to arm installation while discovery mode is
still set.

Declared machines must carry an explicit control-plane or worker role label,
and the corresponding compiled profile must exist. Bootie fails closed rather
than treating every non-control-plane Node as a worker. Additional boot-profile
names are restricted to simple local filenames and must exist.
