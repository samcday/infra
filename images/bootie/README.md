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
