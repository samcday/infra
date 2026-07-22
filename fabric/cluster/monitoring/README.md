# Fabric monitoring

This is the small, local failure detector for the Fabric root cluster. It is
deliberately independent of the future remote Mimir stack: two Prometheus and
two Alertmanager replicas run across `fabric-az1-svc1` and
`fabric-az1-svc2`, retain only 48 hours of bounded ephemeral state, and keep
enough local evidence to page when the root cluster or its GitOps loop wobbles.
Grafana, ingress, remote write, and the Prometheus admin API stay disabled.

The stock `KubeClientCertificateExpiration` rule is disabled because every
operator request made through `scripts/ik` deliberately presents a bounded
15-minute client certificate. The API server's aggregate histogram cannot
identify which client certificate it observed, so its stock 24-hour critical
threshold would continuously page on healthy operator access. Fabric's local
rules retain the identity-specific etcd alarm-observer certificate check; the
remaining K3s PKI needs fixed-identity file or collector metrics rather than
this unlabelled API-server histogram.

Prometheus and node-exporter stay on the Pod network. Fabric's separately
qualified Flannel egress path SNATs routed root scrapes to the hosting service
node, so the root and router policy still admits only `10.66.1.10/32` and
`10.66.1.11/32` to cp1-cp3 TCP/2112,2381,9100 without exposing monitoring
listeners on the service hosts. Until a managed VLAN provides anti-spoofing,
this is a trusted-node boundary rather than hostile-workload isolation.
Alertmanager's writable API is separately limited by NetworkPolicy to the
Prometheus Pods and Alertmanager mesh.

The Prometheus operator is installed separately so its CRDs exist before this
release. The chart's cluster-wide controller RBAC is disabled: direct Roles
confine Secret, ConfigMap, StatefulSet, and monitoring-CR access to this
namespace, while a small ClusterRole retains only node, namespace, and
StorageClass discovery. Its kubelet Service access is separately confined to
`kube-system`. Prometheus Operator v0.81 performs a cluster-wide event-write
preflight and has no event-disable flag; that permission is intentionally
denied, so it logs one startup warning and runs with Kubernetes event emission
disabled rather than gaining cluster-wide write access.

## Staged first activation

The first reconciliation intentionally has no outbound Alertmanager receiver.
`pagerduty.yaml` and its SOPS Secret are committed but omitted from
`kustomization.yaml`, preventing installation noise from paging.

1. Reconcile the operator and monitoring children and inspect all targets and
   active critical alerts.
2. Roll `scripts/rollout-fabric-root-firewall` through cp1, cp2, and cp3 one at
   a time while the router still blocks the new aperture.
3. Run `scripts/rollout-fabric-router-monitoring-policy` last, then prove all
   nine root targets are healthy and the root visibility alerts have cleared.
4. Create the unique `fabric` Dead Man's Snitch, encrypt its callback into this
   namespace, and add its Watchdog route together with `pagerduty.yaml` and
   `pagerduty-secret.yaml` to `kustomization.yaml`.
5. Prove both Alertmanager replicas can notify, the Watchdog check-in is fresh,
   and no unexpected critical alert is firing before calling activation done.

Do not create the snitch substantially before step 4: its hourly missed-check
clock begins at creation. Never open the root/router metrics aperture before
all three root guards have accepted the same pushed policy revision.
