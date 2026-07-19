# fabric

`fabric` is the physical root cluster that will manage hub and future child
clusters. It is additive: nothing in this tree is reconciled by the current hub
unless a bootstrap manifest explicitly says so.

Its first three nodes are an offline-bootstrapped, consensus-only trust root.
They never host Ceph, KubeVirt, or applications. Etcd RBAC and a dedicated
host firewall are part of bootstrap. The service subnet and its two platform
agents are declared in Git, but a dedicated consensus/service VLAN or physical
boundary is still mandatory before either agent or any child etcd prefix can
exist. Future Bootie provisioning is worker-only and post-gating.

The design and induction gates live in
[`docs/plans/fabric-cluster.md`](../docs/plans/fabric-cluster.md).
The attended entry points are the [offline installer-media
runbook](installer/README.md), [pre-worker soak observer](observer/README.md),
and [root power-meter commissioning record](power/README.md).
