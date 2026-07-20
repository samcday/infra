# fabric

`fabric` is the physical root cluster that will manage hub and future child
clusters. It is additive: nothing in this tree is reconciled by the current hub
unless a bootstrap manifest explicitly says so.

Its first three nodes are an offline-bootstrapped, consensus-only trust root.
They never host Ceph, KubeVirt, or applications. Etcd RBAC and a dedicated
host firewall are part of bootstrap. The service subnet and its two platform
agents are declared in Git. During the explicitly temporary flat-L2 phase,
`10.66.0.0/24` and `10.66.1.0/24` share daisy-chained unmanaged switches while
retaining their final addresses. This is routing and policy separation, not a
security boundary: only the two trusted service nodes and reviewed foundation
workloads may use it. Child etcd identities, tenant workloads, KubeVirt, and
general worker induction remain gated on the managed-switch VLAN boundary.
Future Bootie provisioning remains worker-only and attended.

The design and induction gates live in
[`docs/plans/fabric-cluster.md`](../docs/plans/fabric-cluster.md).
The attended entry points are the [offline installer-media
runbook](installer/README.md), [pre-worker soak observer](observer/README.md),
and [root power-meter commissioning record](power/README.md).
