# Fabric root cluster

This tree is the independent GitOps root for the bare-metal fabric cluster. It
is deliberately not referenced by `hub/cluster/flux-system`. The three
consensus Ignitions remain offline-rendered from `fabric/butane` and are not
copied into Kubernetes Secrets.

Phase two adds exactly two persistent K3s agents, `fabric-az1-svc1` and
`fabric-az1-svc2`, on the routed `10.66.1.0/24` service plane. They
carry `fabric.samcday.com/platform=true` as both a label and a NoSchedule
taint. CoreDNS, Flux controllers, metrics-server, and later hosted control
planes select and tolerate that boundary explicitly. The service nodes are
not etcd members and do not carry Kubernetes control-plane roles.

The first two agents are an explicit transitional exception: the root and
service prefixes retain separate addressing and router policy but temporarily
share one physical L2 across daisy-chained unmanaged switches. IP allow-lists
cannot stop source spoofing on that wire. Until a managed switch enforces the
declared VLANs, admit only these physically trusted machines and the reviewed
Flux, CoreDNS, metrics-server, cert-manager, and etcdetcetc foundation needed
to finish the platform. The dedicated etcdetcetc controller identity and one
retained-data smoke tenant are the sole temporary etcd-client exception. Do
not place a child control plane, other child etcd credentials, KubeVirt,
tenant VMs, LLM jobs, or other untrusted workloads there.

K3s keeps only Flannel and kube-proxy from its normal base. Its packaged
CoreDNS and metrics-server Addons remain disabled: this tree owns pinned,
two-replica copies on the service nodes. Traefik, ServiceLB, local-path storage,
and the embedded registry remain disabled. This tree does not install Cilium,
a load-balancer controller, storage operators, monitoring storage, KubeVirt,
or tenant workloads yet.

External etcd has a 2 GiB backend quota. Mutual TLS is enabled at bootstrap;
the guarded offline-root procedure then enables RBAC and confines K3s to
`/fabric-root/`, its `/bootstrap/` keys, and the read-only `/bootstrap` scan
required by K3s startup. Successful negative
authorization tests, a controlled K3s restart/rejoin and token-rotation test,
negative tests of the installed host guard, and a routed service-policy test
are hard gates before the first service node joins. Physical VLAN isolation
and its anti-spoofing tests remain hard gates before a child control-plane
identity or general tenant workload is created. The etcdetcetc foundation
exception requires separate positive and negative source, port, mTLS, and
prefix-RBAC qualification before its smoke identity is accepted.

All control planes admitted to the physical Fabric etcd are consequently one
trusted availability domain. EtcdTenant prefixes protect key confidentiality
and integrity, but etcd leases, the 2 GiB backend quota, request capacity,
alarms, and failure fate are shared. The controller accepts only the qualified
`3.6.13` server and requires each tenant to acknowledge that contract
explicitly. Do not admit an independently administered or hostile control
plane; that requires dedicated etcd or an identity-aware mediation layer.

The three root nodes remain consensus-only after workers arrive. Ceph,
KubeVirt/CDI, hosted control planes, monitoring storage, and applications are
worker workloads and are never scheduled on the root trio.

Remote administration enters through two userspace Headscale subnet routers,
one pinned to each service node. They advertise the exact root and service
prefixes, retain separate Kubernetes-backed node identities, and use Headscale
route approval and ACLs rather than installing Tailscale on a consensus root or
physical router. Default subnet-route SNAT deliberately makes admitted traffic
arrive from the reviewed service-node addresses; the destination host guards
remain the final port boundary. The routers do not accept Tailnet routes or act
as exit nodes. Their interactive first enrollment keeps reusable auth keys out
of Git and cluster Secrets.

## Reconciliation order

The one-time attended bootstrap is deliberately staged:

1. provision both service nodes from node-specific, confirmation-gated Bootie
   PXE payloads and verify their exact platform label and taint;
2. apply the fail-closed root-placement admission policy and worker-hosted
   CoreDNS;
3. install the vendored Flux `v2.9.2` four-controller surface;
4. seed the fabric SOPS identity from tmpfs; and
5. hand the `fabric-root`, `fabric-foundation`, and `fabric-platform`
   Kustomizations to the pushed `main` commit.

`scripts/bootstrap-fabric-flux` enforces that order. The platform child then
installs the official metrics-server chart with two hard-separated replicas
and verified APIService serving TLS. Neither the bootstrap helper nor Flux
modifies the root operating systems; root firewall changes remain attended,
offline-managed operations.

The initial chart-generated metrics-server serving certificate lasts one year
and is reused by Helm. Add expiry alerting when monitoring lands, and migrate
to cert-manager or perform a controlled Secret rotation well before its first
anniversary; a Ready HelmRelease alone does not prove renewal.
Kubelet scraping remains strict: metrics-server uses the projected K3s server
CA, which signs each node's IP-bearing kubelet serving certificate, and the
bootstrap rejects `--kubelet-insecure-tls` before accepting node samples.

The cert-manager webhook remains Pod-networked. K3s server connections to its
remote endpoints enter Flannel with the exact subnet-base host addresses
`172.22.0.0`, `172.22.1.0`, or `172.22.2.0`; its ingress NetworkPolicy admits
only those three `/32`s alongside the declared physical API endpoints. Do not
widen that exception to a root or service-plane PodCIDR.

## Provisioning boundary

Cp1 and cp2 were bootstrapped from node-local media and were never exposed to
Bootie. Cp3 was commissioned on 2026-07-18 with the bounded, temporary cp3-only
PXE station documented in `fabric/bootie/README.md`. The station and its API
access were torn down after qualification; all three roots now boot locally
and form the live consensus/control-plane set. The retained runbook is an
audit record, not an active provisioning service.

During that ceremony, discovery received only the non-installing guarded
inventory Ignition. The complete cp3 Ignition was mounted read-only from
verified tmpfs only after
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

This completed exception did not install a durable provisioner and does not make cp3
boot, etcd, or the root K3s control plane depend on Bootie. Before worker
induction, design a separate worker-only profile set, constrain Node creation,
and choose an authenticated transport. None of those later choices may make
Bootie a dependency of etcd or the root K3s control plane.
