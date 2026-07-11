# Bare-metal fabric direction

The hub should become the small root cluster that survives everything built on
top of it. KubeVirt adds compute to that root; it must not turn the root's own
consensus into a circular dependency.

## Root boundary

- Keep the three physical hub control-plane/etcd nodes as the root consensus
  set. Run KubeVirt's API/controller components there, but never schedule VMs
  there.
- Opt physical compute nodes into KubeVirt with
  `samcday.com/kubevirt=true`. The KubeVirt CR deploys `virt-handler` only to
  that set, so an ordinary worker does not silently become a hypervisor.
- Continue using the existing `k8s-control-plane` and `etcdetcetc` machinery
  for child control planes and isolated etcd prefixes. Child-cluster workers
  can become KubeVirt VMs once the canary, storage, and lifecycle paths are
  proven.
- Do not move the root hub control plane into VMs in the first migration. A
  later design can add VM-hosted hub members, but physical quorum must remain
  able to recover KubeVirt, Flux, storage, and child control planes.

## Before adding any new bare-metal worker

Rook currently has both `useAllNodes: true` and `useAllDevices: true`, and OSDs
target every non-control-plane node. Joining `sam-desktop` as a normal worker
can therefore make its unused disks eligible for encrypted Ceph OSD creation.
Fence Rook with an explicit storage-node label and explicit device selection
before the desktop ever joins.

The induction sequence should then be:

1. Inventory disks, NICs, CPU virtualization, IOMMU groups, GPU PCI functions,
   and the boot path without changing the installed desktop.
2. Predeclare the Node by stable MAC/serial and choose a non-destructive Bootie
   flow. A `samcday.com/boot-device` annotation authorizes installation to that
   disk and can wipe it.
3. Enable VT-x/AMD-V and VT-d/IOMMU in firmware; verify `/dev/kvm`,
   `/dev/vhost-net`, `/dev/net/tun`, and isolated IOMMU groups on the intended
   host OS.
4. Join without the KubeVirt or Ceph storage labels. Observe networking,
   resource accounting, and node stability first.
5. Add `samcday.com/kubevirt=true` through Git. Add any storage role separately
   and only with an explicit disk allowlist.

## VM platform slices

The initial Arch canary installs CDI and proves one declarative image import
onto a persistent Ceph RBD `DataVolume`. After that path is boring:

1. Resolve the current double-default StorageClass state, then test Ceph RBD
   boot, snapshot, restore, and deletion. Test CephFS/RWX separately before
   promising live migration.
2. Add common instance types and a tenant namespace baseline: ResourceQuota,
   LimitRange, NetworkPolicy, narrow RBAC, and explicit host-device grants.
3. Prove cold restart and host maintenance before enabling automatic live
   migration or workload update methods.
4. Add Multus or direct-LAN attachment only when a child cluster actually needs
   it. The pod network with masquerade is the safer default.

## RTX 4090 boundary

The GeForce RTX 4090 is not on NVIDIA's supported vGPU list and does not
support MIG. The defensible KubeVirt path is whole-card VFIO passthrough to one
VM at a time:

- the GPU and associated PCI functions must be in safe IOMMU groups and bound
  to `vfio-pci` on the host;
- the host desktop and host CUDA workloads lose the card while it is bound to
  VFIO;
- the guest owns the NVIDIA driver and GPU monitoring;
- the VM is pinned to that node and cannot be live-migrated.

That VM can still multiplex many LLM jobs internally. NVIDIA time-slicing or
MPS can improve utilization for containers, but neither provides hard memory
or fault isolation and neither turns a 4090 into independently isolated vGPUs.
Do not add a KubeVirt `permittedHostDevices` entry until the desktop's exact PCI
IDs and IOMMU grouping have been recorded.
