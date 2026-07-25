# Fabric Tailnet foothold

Two userspace Tailscale subnet routers provide the normal remote administration
path into Fabric. Each Deployment is fixed to one service node, retains its own
node identity in a narrowly writable Kubernetes Secret, and advertises the
same exact `10.66.0.0/24` and `10.66.1.0/24` routes for Headscale-managed
failover. They never accept Tailnet routes, run an exit node, use host network,
or schedule on a consensus root.

The first enrollment is deliberately interactive. No reusable Headscale auth
key is stored in Git or copied between clusters. After this Kustomization first
lands, run `scripts/register-fabric-tailnet` and enter a Headscale API key at
its silent prompt. The helper follows each live Pod's short-lived registration
ID and submits it through Headscale's public API; it does not need Hub cluster
DNS or an existing subnet route. The policy permits Sam to assign
`tag:fabric-subnet-router` and automatically approves only the two Fabric
routes. The readiness endpoint stays unhealthy until the node owns a Tailnet
address, so Flux cannot report this child Ready before enrollment succeeds.

Linux clients must accept subnet routes. Default subnet-route SNAT is retained:
Fabric hosts see forwarded sessions as originating from `10.66.1.10` or
`10.66.1.11`, and their existing host policy remains authoritative for ports.
The Tailnet route is suitable for carrying the offline recovery client's mTLS
session to physical etcd without copying a private key into Kubernetes.
