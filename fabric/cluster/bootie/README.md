# Fabric Bootie

Bootie is a steady-state, single-replica workload on the two service nodes.
All five machines keep their admitted IPv4 PXE entry first in UEFI order.
Bootie offers each MAC its own temporary address, signed shim/GRUB, and an HTTP
decision. With no active session the decision is only `exit`, so UEFI continues
to the local FCOS disk.

The destructive response requires one global maintenance lock and one
short-lived `bootie/bootie-session` Secret. The Secret binds an exact
replacement Node UID, its inventoried disk and MAC, one Ignition digest, the
lock UID, repository revision, and the pinned provenance-attested Bootie image.
Bootie may get only that Secret and may get/patch only the five named Nodes. It
cannot create or delete either resource. The Secret is absent at rest and is
deleted after the one-use Ignition fetch.

The Pod uses host networking and bounded network/user-switching capabilities
for DHCP, TFTP, Nginx, and FastCGI. It is scheduled only on platform-tainted
service nodes. Draining the hosting service node moves the Deployment to the
other service node before the target is stopped. `Recreate` prevents two DHCP
servers during image rollout. If Bootie is unavailable, firmware times out and
continues to the local disk; full-cluster recovery retains the offline station.

The current flat L2 lets a root temporarily PXE with a `10.66.1.10x` address.
When hardware VLAN separation arrives, preserve this API and add DHCP relay or
one serving endpoint in each provisioning broadcast domain.
