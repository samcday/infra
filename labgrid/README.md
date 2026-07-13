# labgrid

The Hub runs a single labgrid 26.0 coordinator with its state on a Ceph RBD
volume. A userspace Tailscale proxy joins the repo-managed Headscale tailnet as
`labgrid-coordinator`, then forwards tailnet TCP port 20408 to the in-cluster
coordinator Service.

The resulting coordinator address is:

```text
labgrid-coordinator.tailnet.hub.samcday.com:20408
```

The coordinator is the control plane: it tracks resources, places and leases.
Console and fastboot traffic does not cross the coordinator. After a lease is
acquired, the client reaches the Steam Deck exporter over the same tailnet,
using SSH forwarding for exported resources.

## Client

`scripts/labgrid-client` runs the pinned official client image with host
networking, a stable client identity, Sam's SSH key, and the frankensargo tool
configuration. It derives the coordinator IP from local Tailscale peer state
when the host's MagicDNS integration is unhealthy. Start with:

```bash
scripts/labgrid-client resources
scripts/labgrid-client places
scripts/labgrid-client --place frankensargo show
```

The `frankensargo` place is matched to every resource in exporter group
`steamdeck/frankensargo`.

Acquire before touching hardware and release when finished:

```bash
scripts/labgrid-client --place frankensargo acquire
scripts/labgrid-client --place frankensargo console uart
scripts/labgrid-client --place frankensargo fastboot --name pocketboot-fastboot getvar product
scripts/labgrid-client --place frankensargo release
```

Use `--name abl-fastboot` instead when the phone is in ABL. Exactly one of the
two named fastboot resources is normally available. Labgrid has no first-class
ADB driver; acquire the place before using ADB directly on `steamdeck` so the
out-of-band command still respects the lease.

The coordinator uses insecure gRPC, with Headscale encryption and ACLs as its
security boundary. Sam's Headscale user can reach the coordinator; it is not
published on the public Internet or the separate SaaS Tailscale tailnet.

## frankensargo exporter

The exporter is a lingering user service on `steamdeck`. It runs the pinned
official exporter container with host udev/device visibility, host networking,
and SSH-isolated resource access. The service also holds a systemd sleep
inhibitor while active.

To install or refresh it on the Deck, copy this directory there and run:

```bash
./install-steamdeck.sh
systemctl --user status frankensargo-labgrid-exporter.service
journalctl --user -u frankensargo-labgrid-exporter.service -f
```

The fixed dock topology is part of the resource identity:

- FTDI UART: dock path `1.2`, 115200 baud. Its cloned serial is not unique, so
  the physical path is authoritative.
- Phone USB: adjacent dock path `1.3`, serial `99NAY1AZG1`; both ABL
  (`18d1:4ee0`) and PocketBoot (`1d6b:0104`) are represented.

There is not yet a remote power-button actuator. Cutting USB power cannot
reliably reset a battery-backed Pixel, so frankensargo is leaseable but not yet
self-recovering from every wedged state. A dry-contact relay across the power
button is the next hardware resource to add.
