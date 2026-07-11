# KubeVirt on hub

This directory installs the first deliberately small virtualization slice on
the physical hub cluster. Flux owns the full rollout; no existing workload is
rewired through KubeVirt.

## Layout

- `operator/` vendors the byte-for-byte upstream KubeVirt v1.8.4 operator
  release manifest and CRDs.
- `instance/` creates KubeVirt, keeps its control plane on the physical hub
  control-plane nodes, and runs `virt-handler` only on explicitly opted-in
  workers.
- `cdi-operator/` vendors the byte-for-byte upstream CDI v1.64.0 operator.
- `cdi/` creates CDI and pins scratch space to `ceph-block`.
- `canary/` imports one official Arch Linux cloud image into a 4 GiB Ceph RBD
  volume and runs it as `arch-canary`.

The five Flux Kustomizations form two parallel controller chains which meet at
the VM:

```text
kubevirt-operator -> kubevirt --+
                                +-> kubevirt-canary
cdi-operator      -> cdi -------+
rook-ceph -----------------------+
```

## Containment

- Only nodes labelled `samcday.com/kubevirt=true` run VM compute pods. The
  root control-plane/etcd nodes are not opted in.
- The VM receives 1 GiB of guest memory and at most one vCPU. Namespace quota
  allows one VM, one DataVolume, its 4 GiB persistent disk, and the temporary
  CDI scratch PVC needed while converting the qcow2 image. Scratch is deleted
  after import; the namespace storage ceiling is 10 GiB.
- Its only network is KubeVirt masquerade over the pod network. There is no
  Service, direct-LAN attachment, host device, Multus network, or GPU grant.
- The disk explicitly uses `ceph-block`; it does not depend on the hub's
  ambiguous pair of default StorageClasses.
- The imported image URL is the dated official Arch build
  `Arch-Linux-x86_64-cloudimg-20260701.551070.qcow2`, whose published SHA-256
  is `a99844dda491606f81f463ce96851ae03d90ca8fa727671cac2da86a07a7cd61`.

## Compatibility and current risk

KubeVirt v1.8.4 is the newest KubeVirt line compatible with the hub's
Kubernetes v1.33.3. Kubernetes 1.33 is now EOL, so upgrading the hub remains
important, but v1.33 is not an installation incompatibility for KubeVirt 1.8.
KubeVirt 1.9 must not be selected until Kubernetes is upgraded.

The hub API currently passes readiness checks, but kube-vip has an abnormal
restart and leader-transition history. Flux timeouts and dependency health
checks make partial installation visible and retryable; they do not make that
underlying instability acceptable. Fixing kube-vip and upgrading Kubernetes
remain follow-up platform work.

## Acceptance and shell access

The rollout is successful when KubeVirt and CDI are deployed, `arch-canary`
is Ready, its DataVolume import has succeeded, and an API-tunnelled SSH command
returns from inside the guest:

```sh
scripts/ik --context=hub -n kubevirt get kubevirt,pods
scripts/ik --context=hub get cdi,pods -n cdi
scripts/ik --context=hub -n kubevirt-canary get vm,vmi,dv,pvc,pod -o wide

virtctl --kubeconfig ./kubeconfig ssh \
  -i ~/.ssh/id_ed25519 \
  -c 'uname -a; id; cat /etc/arch-release' \
  arch@vm/arch-canary/kubevirt-canary
```

Use the v1.8.4 `virtctl` release binary so the client matches the installed
KubeVirt API. SSH is carried through the Kubernetes API; no inbound VM port is
published. Serial-console fallback is:

```sh
virtctl --kubeconfig ./kubeconfig console arch-canary -n kubevirt-canary
```
