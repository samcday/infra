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
| `ilumbaclusta` | `10.0.3.0/24` | `172.28.0.0/16` | `172.27.0.0/16` | dynamically allocated from the hub BGP service interval |
| `edge-au-east` | provider-assigned; no CIDR in this repo | `172.24.0.0/16` | `172.23.0.0/16` | parent Service `172.27.23.43`; Headscale address expected at `100.64.0.64` |
| `lab` | no workers allocated | `172.26.0.0/16` | `172.25.0.0/16` | Fabric parent Service `172.21.0.25`; `lab-apiserver.tailnet.hub.samcday.com`, with its IPv4 assigned dynamically from Headscale |

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

`ilumbaclusta` is declared by its [router
configuration](../ilumbaclusta/router/files/etc/uci-defaults/system) and
[hosted control-plane
values](../hub/cluster/ilumbaclusta/control-plane-values.yaml).
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

All IPv4 route tables on `sam-desktop` were also checked. None contained a
more-specific entry naming `10.66.0.0/24`, `172.22.0.0/16`,
`172.21.0.0/16`, or an address inside those ranges. This is positive
non-overlap evidence for the operator workstation only: lookups for one
address in each proposed range selected the ordinary `10.0.1.1` default, not
a Tailscale or prefix-specific route. A default route is not an allocation
claim. The local Tailscale backend reported `Running`, with no primary routes
and no non-host `AllowedIPs` accepted from its peers. The check did not inspect
kernel routes inside Kubernetes nodes, a live OpenWrt router, Headscale's
server-side approvals, the home gateway, or the Superloop path.

The common OpenWrt overlay permits forwarding between Tailscale and LAN zones
but logs in without advertising a prefix, and the physical Fabric router image
explicitly removes Tailscale. Fabric instead runs two Kubernetes-hosted
userspace subnet routers, one on each service node, declaring
`10.66.0.0/24,10.66.1.0/24`; Headscale owns approval and ACL enforcement for
those routes. The earlier `sam-desktop` spot-check below predates that
declaration and is retained only as dated evidence, not current route state.

## Allocated fabric space

Fabric is additive and is not in the hub Flux fan-out. These ranges are
allocated by the bootstrap sources; the root ranges have been live since the
three-root qualification completed on 2026-07-18. They remain intentionally
non-advertised. During the 2026-07-20 transitional service-node induction,
the root and service IP prefixes deliberately share one physical broadcast
domain across daisy-chained unmanaged switches. Their separate prefixes and
router policy do not constitute VLAN or anti-spoofing isolation. Recheck new
home, hub, WAN, ISP, and Tailscale allocations before adding any route
advertisement or cross-cluster transport.

| Purpose | Range or address | Status |
| --- | --- | --- |
| Consensus and provisioning LAN | `10.66.0.0/24` | Live; no Git, live hub/cloud, or operator-route collision found; intentionally not advertised. |
| Service-node LAN | `10.66.1.0/24` | Live for persistent Fabric platform agents on the accepted temporary shared physical L2. |
| Fabric Pod network | `172.22.0.0/16` | Live in K3s configuration; no Git, live hub/cloud, or operator-route collision found. |
| Fabric Service network | `172.21.0.0/16` | Live in K3s configuration; no Git, live hub/cloud, or operator-route collision found. |
| Router and offline asset server | `10.66.0.1` | Live router address. |
| Operations laptop and temporary soak observer | `10.66.0.2` | Live attended observer attachment, without forwarding. |
| Low-address holdback | `10.66.0.3-10.66.0.9` | Unallocated; LAN DHCP is disabled. |
| Consensus nodes | `10.66.0.10-10.66.0.12` | Live, statically assigned cp1-cp3 roots; also direct K3s supervisor/API endpoints for the two admitted service nodes. |
| Post-consensus holdback | `10.66.0.13-10.66.0.19` | Unallocated; not a worker pool. |
| Consensus-segment holdback | `10.66.0.20-10.66.0.99` | Reserved; not a worker pool. |
| Ephemeral live inventory | `10.66.0.100` | One non-installing candidate at a time; no gateway or DNS. |
| Unallocated consensus-LAN holdback | `10.66.0.101-10.66.0.199` | Reserved; LAN DHCP is disabled. |
| High-address holdback | `10.66.0.200-10.66.0.253` | Unallocated; not a publishing pool. |
| Fabric API VIP | `10.66.0.254` | Live kube-vip API endpoint. |
| Service-plane router | `10.66.1.1` | Staged as a second address on the router's sole fabric link; pending live qualification. |
| Service-plane low-address holdback | `10.66.1.2-10.66.1.9` | Reserved; no DHCP allocation. |
| Persistent platform agents | `10.66.1.10-10.66.1.11` | Live on `fabric-az1-svc1` and `fabric-az1-svc2`. |
| Service-plane holdback | `10.66.1.12-10.66.1.99` | Reserved for future reviewed service nodes; not a child-cluster pool. |
| Service-plane candidate holdback | `10.66.1.100` | Held but unused during flat-L2 induction. Bootie discovery stays on `10.66.0.100`; the router grants `.1.100` no service and tests it as an unauthorized source. |
| Remaining service-plane holdback | `10.66.1.101-10.66.1.254` | Unallocated; no DHCP allocation or publishing pool. |
| Lab parent apiserver Service | `172.21.0.25` | Reserved inside the Fabric Service CIDR for the hosted `lab` apiserver. |
| Lab child Kubernetes Service | `172.25.0.1` | First address in the lab Service CIDR. |
| Lab child DNS Service | `172.25.0.10` | Reserved for future child CoreDNS; no child DNS workload is installed yet. |

Evidence is in the [fabric plan](plans/fabric-cluster.md), [router
configuration](../fabric/router/files/etc/uci-defaults/10-system), [node
profiles](../fabric/butane/fabric-az1-cp1.yaml), and [K3s
configuration](../fabric/butane/control-plane.yaml). The temporary flat-L2
realization permits only the two named, physically trusted platform agents and
the reviewed infrastructure-owned `lab` control plane after router, host,
mTLS, prefix-RBAC, and NetworkPolicy qualification. It remains insufficient for
independently administered or untrusted tenants. No externally published
Fabric load-balancer pool has been allocated.

## Confirmed overlaps and sharp edges

- **Conflict:** `cloud-cluster` and `ilumbaclusta` both claim
  `172.28.0.0/16` for Pods and `172.27.0.0/16` for Services. They cannot be
  given unqualified routes to one another. Allocate new, disjoint ranges before
  the fabric or Tailscale carries Pod or Service routes between them.
- **Ambiguous boundary:** the hub BGP service interval is exactly
  `10.0.2.1-10.0.2.127`; repository comments disagree about whether its parent
  prefix is `/25` or `/24`. No adjacent `10.0.2.x` allocation is safe yet.
- **Expected containment:** the Hetzner `172.28.0.0/15` aggregate contains the
  cloud Pod and node `/16`s. This is not an independent third workload range.
- **Expected shared LAN:** the hub L2 pool is inside `10.0.1.0/24`. Its exact
  interval does not overlap the current static router, node, or API addresses.
- **No repository collision:** the Fabric and lab ranges are disjoint
  from every current Pod, Service, physical LAN, and publishing range listed
  above. This says nothing about networks absent from Git.
- **External collision risk:** Headscale's `100.64.0.0/10` is carrier-grade NAT
  space. Whether an ISP or upstream router also presents that space cannot be
  determined from this repository.

## Unknown or deliberately unallocated

- Live hub/cloud Kubernetes allocations and the operator workstation's route
  tables were spot-checked as scoped above. Kubernetes-node kernel routes,
  DHCP leases, Headscale ownership, and running router configuration remain
  unqueried.
- The hub/home upstream LAN, fabric WAN lease, Superloop public address and any
  ISP-side transit/CGNAT ranges are not declared in Git.
- `edge-au-east` worker/provider node networks are provider-assigned and absent
  from the manifests.
- The hub and ilumbaclusta OpenWrt DHCP ranges are inherited rather than
  explicitly recorded here; inspect the running routers before consuming
  additional addresses on those LANs.
- The allocated fabric service subnet still needs its permanent managed-switch
  VLAN realization. Its temporary same-wire realization is deliberately not
  an anti-spoofing boundary and is an accepted risk only for the trusted
  platform and `lab` control plane. Future worker subnets, further child
  Pod/Service blocks, and published VIP pools require new entries before
  deployment.
- `10.244.0.0/16`, `10.96.0.0/12`, and `10.0.0.10` occur only in chart defaults
  or a Helm-render example; they are not repository allocations.
- `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `0.0.0.0/0`, and `::/0`
  occur as firewall match ranges, not as claims on those networks.
