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

Cp1 and cp2 were bootstrapped from node-local media and are never exposed to
Bootie. Completing the already-declared third member may use the bounded,
temporary cp3-only PXE station documented in `fabric/bootie/README.md`. That
station runs on the observer machine, not in this cluster or on the router. It
accepts one predeclared Node and one exact permanent MAC, cannot create or list
Nodes, and receives a four-hour ServiceAccount token that can get and patch
only `fabric-az1-cp3`.

Discovery receives only the non-installing guarded inventory Ignition. The
complete cp3 Ignition may be mounted read-only from verified tmpfs only after
the candidate has passed inventory, its exact whole-disk by-id has been
reviewed, and Sam has authorized that device for destruction. The exact device
must agree in the Node annotation and a separate read-only install policy; an
install arm is consumed before Bootie emits one installer response. Cp1 and
cp2 Ignitions, etcd member keys, and LUKS recovery keys are never available to
the station.

The pinned FCOS shim, GRUB, and kernel retain their Secure Boot signature
chain. This is not authenticated netboot: the TFTP GRUB configuration and the
HTTP configuration, initramfs, rootfs, and Ignition transport are not protected
end to end. Exact-MAC DHCP narrows accidental exposure but is not hardware
authentication. The isolated, physically trusted fabric L2 is therefore part
of the security boundary. Stop the station, revoke its RBAC, zero its projected
token, and discard the tmpfs handoff after the single cp3 ceremony.

This exception does not install a durable provisioner and does not make cp3
boot, etcd, or the root K3s control plane depend on Bootie. Before worker
induction, design a separate worker-only profile set, constrain Node creation,
and choose an authenticated transport. None of those later choices may make
Bootie a dependency of etcd or the root K3s control plane.
