# boot(**i**ncredibly **e**asily)

bootie is a simple container to facilitate PXE booting k8s nodes with FCOS.

It has two HTTP endpoints:

 * `/boot.ipxe?mac=&serial=`. iPXE clients chain this URL and receive
   instructions to boot FCOS. If no k8s Node yet exists matching the MAC or
   serial, a petname is generated and the Node is created.
 * `/ignition/<node>`. This generates the appropriate Ignition config to
   provision the Node. It's assumes that an `/ignition` directory exists
   which contains, at a minimum, a `base.ign` config. Additional profiles
   can be selected by annotating the node with `samcday.com/boot-profiles`.

## Installation safety

Bootie treats disk identity and permission to install as separate concerns:

* `samcday.com/boot-device` records the stable device path that an installation
  would target. Its presence alone never starts the CoreOS installer.
* `samcday.com/install: "true"` arms exactly one PXE response for installation.
  Bootie atomically removes this annotation before it returns installer kernel
  arguments. If the boot fails before installation completes, an operator must
  explicitly arm the Node again.

Every PXE response carries a random `samcday.com/ignition-token` bound to an
`install` or `live` mode. The Ignition endpoint atomically verifies and
consumes both before returning any profile. Appending `install=1` to a guessed
or live-mode URL therefore cannot bypass the install arm, and concurrent
requests cannot both consume the same authorization. This is a one-use safety
token, not a replacement for keeping Bootie on an isolated LAN.

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
