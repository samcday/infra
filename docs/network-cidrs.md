# Network CIDR registry

This is the repository source of truth for allocated and reserved network
space. It was audited from manifests, router overlays, and documentation on
2026-07-13. A bounded read-only runtime spot-check of hub, cloud, and the
operator workstation followed on 2026-07-14; its exact scope is recorded
below. In this document, **current** means that Git declares the range in a
reconciled path or router image source. Runtime evidence is called out
separately and never implies that an unqueried router or provider matches Git.

New physical LANs, Pod and Service networks, tailnet pools, API VIPs, and
load-balancer pools must be recorded here before use. Exact address intervals
take precedence over convenient CIDR shorthand.

## Current Git declarations

| Domain | Node or physical network | Pod CIDR | Service CIDR | API or publishing path |
| --- | --- | --- | --- | --- |
| `hub` | `10.0.1.0/24` | `172.30.0.0/16` | `172.31.0.0/16` | kube-vip `10.0.1.254`; Tailnet TCP proxy `hub-apiserver.tailnet.hub.samcday.com:6443` |
| `cloud-cluster` | Hetzner `172.29.0.0/16` | `172.28.0.0/16` | `172.27.0.0/16` | Headscale address expected at `100.64.0.3` |
| `edge-au-east` | provider-assigned; no CIDR in this repo | `172.24.0.0/16` | `172.23.0.0/16` | parent Service `172.27.23.43`; Headscale address expected at `100.64.0.64` |

The hub declarations come from the [router LAN and node
leases](../hub/router/files/etc/uci-defaults/system), [K3s
configuration](../hub/butane/control-plane.yaml), and [kube-vip
configuration](../hub/butane/control-plane.yaml). The router is `.1`, the
three current control-plane/etcd nodes are `.10-.12`, and the API VIP is `.254`.
The [hub API Tailnet proxy](../hub/cluster/headscale/hub-apiserver-tailnet.yaml)
publishes only TCP port 6443 and forwards it to the in-cluster Kubernetes
Service. It does not advertise the hub LAN or either Kubernetes CIDR.

`cloud-cluster` uses the Hetzner network aggregate `172.28.0.0/15`, divided
between the Pod range and the `172.29.0.0/16` provider node subnet. This is
declared by [OpenTofu](../hub/cluster/cloud-cluster/main.tf) and the [hosted
control-plane values](../hub/cluster/cloud-cluster/control-plane-values.yaml).
The aggregate containing its two disjoint `/16`s is intentional, not a
collision.

`edge-au-east` is declared by its [hosted control-plane
values](../hub/cluster/cloud-cluster/edge/cluster/edge-au-east-control-plane-values.yaml).
Its `172.27.23.43` address belongs to the parent `cloud-cluster` Service range;
it is not an address from the edge cluster's own Service range.

### Current publishing and overlay pools

| Owner | Range or interval | Purpose | Qualification |
| --- | --- | --- | --- |
| hub Cilium | `10.0.1.20-10.0.1.99` | L2-announced LoadBalancer Services | Intentionally inside the hub LAN and disjoint from the router, nodes, and API VIP. |
| hub Cilium | `10.0.2.1-10.0.2.127` | BGP-announced LoadBalancer Services | The exact interval is active in Git; its aggregate is not authoritatively declared. |
| Headscale | `100.64.0.0/10` | IPv4 tailnet allocation pool | Current Headscale configuration. |
| Headscale | `fd7a:115c:a1e0::/48` | IPv6 tailnet allocation pool | Current Headscale configuration. |

The Cilium intervals are defined in [the hub global load-balancer
manifest](../hub/cluster/global/cilium-lb.yaml). The Cilium comment describes
`10.0.2.0/25`, but the interval includes `.127`, while the router contains only
an inactive example for `10.0.2.0/24`. Until the intended routed aggregate and
endpoint semantics are committed, use the literal `.1-.127` interval and do
not allocate adjacent `10.0.2.x` space.

Headscale owns the full configured pools in [its
configuration](../hub/cluster/headscale/config.yaml). Repository consumers
hard-code `100.64.0.3` for the cloud API, `100.64.0.64` for the edge API, and
`100.64.0.8` for the Steam Deck client. These are address expectations, not
declarative Headscale reservations; the live Headscale state must be checked
before changing or reissuing them.

### Read-only runtime spot-check: 2026-07-14

The Kubernetes API reported the following without pod exec or node access:

- hub Node underlay addresses are inside `10.0.1.0/24`, and its six allocated
  Node Pod CIDRs are `172.30.0.0/24` through `172.30.5.0/24`;
- hub's `default/kubernetes` Service is `172.31.0.1`, while the live Cilium
  native-routing CIDR is `172.30.0.0/16`;
- the live hub Cilium LB pools match Git exactly at `10.0.1.20-.99` and
  `10.0.2.1-.127`;
- cloud Node underlay addresses are inside `172.29.0.0/16`, and its five
  allocated Node Pod CIDRs are inside `172.28.0.0/16`;
- cloud's `default/kubernetes` Service is `172.27.0.1`, while the live Cilium
  native-routing CIDR is `172.28.0.0/16`.

## Retired ranges: not available for reuse yet

The Fabric and lab definitions were removed from the repository on 2026-10-08.
Repository removal is not evidence that running hosts, routers, or Tailnet
registrations have been decommissioned. Keep these ranges reserved until their
separate live retirement is confirmed; see the [retirement
checklist](runbooks/cluster-retirement.md).

| Former owner | Physical networks | Pod CIDR | Service CIDR |
| --- | --- | --- | --- |
| Fabric | `10.66.0.0/24`, `10.66.1.0/24` | `172.22.0.0/16` | `172.21.0.0/16` |
| lab | `10.0.4.0/24` | `172.26.0.0/16` | `172.25.0.0/16` |

## Confirmed overlaps and sharp edges

- **Ambiguous boundary:** the hub BGP service interval is exactly
  `10.0.2.1-10.0.2.127`; repository comments disagree about whether its parent
  prefix is `/25` or `/24`. No adjacent `10.0.2.x` allocation is safe yet.
- **Expected containment:** the Hetzner `172.28.0.0/15` aggregate contains the
  cloud Pod and node `/16`s. This is not an independent third workload range.
- **Expected shared LAN:** the hub L2 pool is inside `10.0.1.0/24`. Its exact
  interval does not overlap the current static router, node, or API addresses.
- **External collision risk:** Headscale's `100.64.0.0/10` is carrier-grade NAT
  space. Whether an ISP or upstream router also presents that space cannot be
  determined from this repository.

## Unknown or deliberately unallocated

- Live hub/cloud Kubernetes allocations and the operator workstation's route
  tables were spot-checked as scoped above. Kubernetes-node kernel routes,
  DHCP leases, Headscale ownership, and running router configuration remain
  unqueried.
- The hub/home upstream LAN, Superloop public address and any
  ISP-side transit/CGNAT ranges are not declared in Git.
- `edge-au-east` worker/provider node networks are provider-assigned and absent
  from the manifests.
- The hub OpenWrt DHCP range is inherited rather than explicitly recorded here;
  inspect the running router before consuming additional addresses on that LAN.
- `10.244.0.0/16`, `10.96.0.0/12`, and `10.0.0.10` occur only in chart defaults
  or a Helm-render example; they are not repository allocations.
- `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `0.0.0.0/0`, and `::/0`
  occur as firewall match ranges, not as claims on those networks.
