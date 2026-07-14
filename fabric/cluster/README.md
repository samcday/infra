# Fabric root cluster

This tree is the independent GitOps root for the bare-metal fabric cluster. It
is deliberately not referenced by `hub/cluster/flux-system`. Phase one does
not install Flux on the three consensus nodes; this directory is held for a
later worker-hosted bootstrap.

The future reconciliation surface is intentionally almost empty: it starts
with the `infra` GitRepository and fans out no workloads. The three consensus
Ignitions are rendered offline from `fabric/butane` and are not copied into
Kubernetes Secrets.

K3s keeps only Flannel and kube-proxy from its normal base. CoreDNS is also
disabled until workers exist, so phase one is deliberately DNS-less. Traefik,
ServiceLB, local-path storage, metrics-server, and the embedded registry are
disabled. This tree does not install Cilium, a load-balancer controller,
storage operators, monitoring, KubeVirt, or tenant workloads.

External etcd has a 2 GiB backend quota. Mutual TLS is enabled at bootstrap;
the guarded offline-root procedure then enables RBAC and confines K3s to
`/fabric-root/` plus K3s' required `/bootstrap/` prefix. Successful negative
authorization tests, a controlled K3s restart/rejoin and token-rotation test,
negative tests of the installed host guard, and a consensus/worker VLAN test
are hard gates before the first worker joins or a child prefix is created.

The three root nodes remain consensus-only after workers arrive. Ceph,
KubeVirt/CDI, hosted control planes, monitoring storage, and applications are
worker workloads and are never scheduled on the root trio.

## Provisioning boundary

Bootie and PXE are deliberately absent from initial consensus bootstrap. The
router USB serves pinned K3s assets, while each secret-bearing Ignition is
rendered on a trusted machine and carried offline. MAC addresses supplied in
an HTTP query are not hardware authentication, and a long-running provisioner
must never hold all three etcd member keys or LUKS recovery keys. Before
worker induction, design a separate worker-only profile set, publish a new
post-gating Bootie image, constrain Node creation, and choose an authenticated
transport. None of those later choices may make Bootie a dependency of etcd
or the root K3s control plane.
