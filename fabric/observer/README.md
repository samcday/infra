# Temporary pre-worker fabric observer

The root trio deliberately does not host a monitoring database, and workers do
not exist until after the initial soak. This directory supplies the static
Prometheus scrape configuration, focused alert rules, and a loopback-only
API/Lease collector for a temporary trusted operations laptop. It does not put
a TSDB or an observer workload on the root trio.

The roots run only the bounded host exporter defined in
`fabric/butane/node-exporter.yaml`; it has no TSDB and no dependency on etcd or
K3s. The profile expects the [official node_exporter 1.11.1 linux/amd64
archive](https://github.com/prometheus/node_exporter/releases/tag/v1.11.1)
with SHA-256
`9f5ea48e5bc7b656f8a91a32e7d7deb89f70f73dabd0d974418aca15f37d6810`.
It binds each node's fabric name on TCP/9100, runs as a dynamic user without
capabilities, and enables only the collectors used by this pack.

Use the official Prometheus 3.13.1 linux/amd64 archive pinned in
`artifacts.txt`, verify its SHA-256 before extracting it, and record that digest
with the soak evidence. Keep its TSDB and any generated files on trusted
storage outside this Git worktree. Monitoring availability must not gate root
startup, so this observer binary is deliberately not served by the fabric
router or fetched by root Ignition.

## Network boundary

`sam-desktop` is not a dual-homed router. Its normal wired interface remains in
the root network namespace and owns the only home default route and DNS. Move
the entire Wi-Fi PHY into a dedicated `fabric-observer` network namespace,
associate it only with the router's low-power WPA3-SAE `fabric-observer` SSID,
and assign `10.66.0.2/24` there.

The observer namespace has only loopback and that Wi-Fi interface. It receives
no veth, macvlan, bridge, gateway, DHCP, configured IP DNS, IPv6, default
route, Tailscale device, connection sharing, NAT, or proxy ARP. Its per-netns
`nsswitch.conf` permits only local files for conventional libc hostname
resolution, and verification proves a public name does not resolve through
`getent`. This structural network boundary remains closed even when unrelated
host workloads require `net.ipv4.ip_forward=1` in the root namespace.

This is a trusted operator/monitoring path, not a sandbox for untrusted
processes. A network namespace does not isolate the host filesystem, pathname
AF_UNIX sockets, or system D-Bus. A deliberately invoked `resolvectl` can still
reach the host resolver through those shared paths. Run only the reviewed SSH,
curl, Prometheus, and collector processes here; adding a mount namespace that
masks host service sockets is a separate hardening step. Do not replace the
network boundary with an ordinary dual-homed NetworkManager profile plus
hopeful firewall state.

Run SSH, SCP, Prometheus, the API/Lease collector, and all admission probes with
`ip netns exec fabric-observer`; these are trusted commands under the residual
host-filesystem boundary above. Start Prometheus with the explicit namespace
loopback listener `--web.listen-address=127.0.0.1:9090`; do not add a veth just
to expose its UI. Copy retained data through an attended stop-and-export step
after disconnecting the fabric radio.

Before moving the radio, verify wired carrier and that the root namespace's
only default route uses the home Ethernet interface. Supply the SOPS-managed
SAE passphrase without printing it or placing it in shell history, and force
the admitted permanent Wi-Fi MAC rather than NetworkManager's randomized home
profile MAC. Before starting the soak, verify:

The repository helper makes that boundary reproducible and fail-closed. It
does not decrypt credentials, accept the SAE passphrase in an argument or
environment variable, create a veth, or alter the host during its `check`
action. First prepare the decrypted 48-character passphrase through the
approved attended SOPS workflow at the exact path
`/run/fabric-observer-input/sae-password`. Its non-symlink parent must be
root-owned mode `0700`, and the single-link file must be root-owned mode `0600`.
Do not put the secret in the command line, shell history, Git worktree, or a
persistent filesystem. Then run the read-only gate with explicit interface
names:

Mutating setup and teardown hold the root-owned
`/run/lock/fabric-network-operation.lock`. Any attended fabric network test
that temporarily claims `10.66.0.2` must hold that same lock, so it cannot race
observer setup into creating a competing address or poisoning the resulting
ARP and firewall-counter evidence.

```sh
sudo ./fabric-observer-netns check \
  --uplink eno1 \
  --wifi wlp95s0 \
  --psk-file /run/fabric-observer-input/sae-password
```

`check` must report wired carrier, exactly one ordinary IPv4 default across all
root policy-routing tables through that non-Wi-Fi uplink, the admitted permanent MAC
`d8:80:83:81:cb:f7`, and a valid root-only credential. It deliberately refuses
the current unsafe shape if Wi-Fi still owns the home default or Ethernet has
no carrier. Copy its device-specific `SETUP:` confirmation into the attended
setup command only after reviewing those facts:

```sh
sudo ./fabric-observer-netns setup \
  --uplink eno1 \
  --wifi wlp95s0 \
  --psk-file /run/fabric-observer-input/sae-password \
  --confirm 'SETUP:fabric-observer:eno1:wlp95s0:phy0'
```

The PHY name in that example is illustrative; copy the exact value printed by
`check`. Setup releases Wi-Fi from NetworkManager, checks the wired default
again, fixes the admitted MAC, and moves the complete PHY into the namespace.
It uses a root-only wpa_supplicant configuration on `/run`, associates with
WPA3-SAE and mandatory management-frame protection, assigns only
`10.66.0.2/24`, installs an empty per-namespace resolver plus a files-only NSS
policy, and then runs a full verification. A failed partial setup runs the same
resumable restoration machine as teardown. It persists
`restoring-phy`, `phy-in-root`, or `cleanup` before each irreversible boundary
and retains that precise phase until root PHY presence, NetworkManager
registration with `managed=yes` and `autoconnect=no`, namespace deletion, and
configuration cleanup have all been proven.

Re-run the non-mutating runtime assertions at any point:

```sh
sudo ./fabric-observer-netns verify
```

When the attended fabric session is over, remove it with the explicit guard:

```sh
sudo ./fabric-observer-netns teardown \
  --confirm 'TEARDOWN:fabric-observer'
```

Teardown refuses an unexpected namespace shape, stops the dedicated
wpa_supplicant, flushes the static address, returns the whole PHY to the root
namespace, and hands it back to NetworkManager. Stop Prometheus, SSH sessions,
shells, and one-shot probes first: teardown enumerates namespace PIDs and
refuses to proceed if anything other than its exact tracked wpa_supplicant is
still present. It does not explicitly connect a saved home profile; normal
NetworkManager control is restored with device autoconnect disabled, and the
operator decides when to reconnect it. Remove the separate credential input
file and its empty parent directory after teardown; the helper removes only
its own runtime copy.

The manual commands below remain useful as independent evidence rather than a
replacement for the helper's checks:

```sh
ip -4 route show default
ip link show
sudo ip netns exec fabric-observer ip -brief link
sudo ip netns exec fabric-observer ip -4 address show
sudo ip netns exec fabric-observer ip -4 route get 10.66.0.10
! sudo ip netns exec fabric-observer ip -4 route get 1.1.1.1
sudo ip netns exec fabric-observer sysctl net.ipv4.ip_forward
sudo ip netns exec fabric-observer sysctl net.ipv6.conf.all.forwarding
```

The root namespace must not contain the Wi-Fi PHY or a route to `10.66.0.0/24`.
The observer namespace must select Wi-Fi with source `10.66.0.2` for fabric,
have no default route, and report both namespace forwarding values as `0`.
Inspect its complete link and route tables and reject any extra interface.

The metrics listeners are intentionally plaintext on this isolated L2. Do not
publish TCP/2381, TCP/2112, or TCP/9100 outside the observer namespace. The bootstrap
root host firewall accepts those metrics only from observer address
`10.66.0.2`. Check all nine endpoints
directly from the laptop:

```sh
for address in 10.66.0.10 10.66.0.11 10.66.0.12; do
  curl --fail --silent --show-error --output /dev/null "http://$address:2381/metrics"
  curl --fail --silent --show-error --output /dev/null "http://$address:2112/metrics"
  curl --fail --silent --show-error --output /dev/null "http://$address:9100/metrics"
done
```

## API and Lease collector

`collector.py` is deliberately fabric-specific: its API VIP, three direct node
addresses, Lease namespace/name, permitted holders, and 30-second duration are
fixed in code. It performs CA-verified mTLS requests to `/readyz` through the
VIP and every node, then GETs only `kube-system/plndr-cp-lock`. Unexpected TLS,
HTTP, JSON, holder, duration, or timing state emits failure metrics. It listens
only on IPv4 loopback and serves only `/metrics`.

`access.yaml` defines two inert roles: GET `/readyz`, and GET that one named
Lease. Immediately before the 48-hour soak, issue a unique requested 54-hour
client identity into a new directory on trusted encrypted storage:

```sh
./provision-kubernetes-access \
  --issue \
  --root-address 10.66.0.10 \
  --output-dir /secure/fabric-soak/kubernetes \
  --confirm ISSUE:fabric-observer-kubernetes:54h
```

Review the selected root's SSH host key out of band first. The helper generates
the private key directly on observer storage and sends only its public CSR and
public bindings through attended SSH. The root's admin kubeconfig and CA keys
never leave it. The returned leaf is checked against the public client CA; the
separate public server CA is retained for endpoint TLS. The issued CN has a
random 128-bit suffix, no groups or SANs, and exactly the client-auth EKU. The
helper proves positive access and a high-impact negative permission matrix and
rejects a signer that does not honor the bounded lifetime.

Install the collector and supplied service at the paths declared in the unit,
create a locked `fabric-observer` system user/group, copy the example config to
`/etc/fabric-observer/config.json`, and copy `client.pem`, `client-key.pem`, and
`ca.crt` from the issued directory to its declared credential paths. Make the
directory root-owned and traversable by the service group; use mode `0640` for
the root-owned private key and `0644` for public certificates. No secret may
enter Git, Prometheus, shell arguments, or retained evidence. Issue the
credential immediately before the soak: 54 hours leaves only six hours beyond
its 48-hour acceptance window.

Validate before starting Prometheus:

```sh
./tests/run
/opt/fabric-observer/collector.py once \
  --config /etc/fabric-observer/config.json |
  promtool check metrics
```

All four API readiness series, the Lease query and validity series, and
client-certificate validity must be `1`. Exactly three holder series must be
present, with exactly one equal to `1`. After preserving non-secret evidence,
revoke this exact session and securely remove its key:

```sh
./provision-kubernetes-access \
  --revoke \
  --root-address 10.66.0.10 \
  --credential-dir /secure/fabric-soak/kubernetes \
  --confirm REVOKE:fabric-observer-kubernetes
```

Revocation removes the unique RBAC bindings and proves the still-live leaf gets
HTTP 403 for the named Lease. Kubernetes client certificates have no CRL in
this workflow, and the default public-info binding may continue to expose
`/readyz`; the unique CN prevents a later soak binding from restoring the old
leaf's Lease access.

Authenticated etcd alarm collection is separate and stays root-local. Each
root uses the zero-role `fabric-observer` etcd CN to write strict node_exporter
textfile metrics. The laptop never receives that etcd key, and TCP/2379 remains
denied from `10.66.0.2` in steady state.

## Prometheus configuration

Run validation from this directory so the relative rule-file path resolves:

```sh
cd fabric/observer
promtool check rules alerts.yml
promtool check config prometheus.yml
```

Start the separately installed, reviewed Prometheus build with this config, a
TSDB path outside Git, at least 72 hours of retention, and a loopback-only web
listener (`--web.listen-address=127.0.0.1:9090`). The exact service/container
mechanism belongs to the laptop and is not defined here. Confirm these target
sets in the Prometheus UI before timing the soak:

- `fabric-etcd`: three endpoints on TCP/2381;
- `fabric-kube-vip`: three endpoints on TCP/2112; and
- `fabric-node`: three endpoints on TCP/9100;
- `fabric-observer`: loopback TCP/19101.

Before starting the clock, also prove that every deliberately selected
collector succeeds and that the kernel exports the required PSI and filesystem
series. A reachable exporter with an absent pressure collector is not valid
soak evidence:

```promql
node_scrape_collector_success{job="fabric-node"} != 1
```

```promql
count by (node) (node_pressure_cpu_waiting_seconds_total{job="fabric-node"})
```

```promql
count by (node) (node_pressure_memory_stalled_seconds_total{job="fabric-node"})
```

```promql
count by (node) (node_pressure_io_stalled_seconds_total{job="fabric-node"})
```

The first query must be empty, each pressure count must be exactly one, and
each node must expose `/`, `/var`, and `/var/lib/etcd` through
`node_filesystem_avail_bytes` and `node_filesystem_readonly`.

The rules cover etcd target loss, leader absence and churn, failed, slow,
stuck, or unapplied proposals, WAL fsync and backend commit p99 latency,
backend quota and fragmentation pressure, etcd process restarts, authenticated
alarm failure/staleness/active state, plus kube-vip target loss and leadership.
Host rules cover exporter and collector loss, sustained
CPU and memory exhaustion, selected root filesystem capacity and read-only
state, CPU/memory/I/O pressure stalls, and sustained physical-disk busy time.
Laptop rules cover fresh collection, VIP/direct API readiness, the
authoritative Lease, disagreement between that Lease and kube-vip's own leader
metric, and short-lived client-certificate validity and expiry.
The 10 ms WAL and 25 ms backend commit thresholds follow
the [etcd performance
guidance](https://etcd.io/docs/v3.6/faq/#what-does-the-etcd-warning-apply-entries-took-too-long-mean).
The metrics and health endpoint behavior is described in the [etcd monitoring
guide](https://etcd.io/docs/v3.6/op-guide/monitoring/).

The Prometheus expression browser is enough for the first soak. Keep these
graphs open per node; all durations are seconds and database values are bytes:

```promql
histogram_quantile(0.99, sum by (node, le) (rate(etcd_disk_wal_fsync_duration_seconds_bucket{job="fabric-etcd"}[5m])))
```

```promql
histogram_quantile(0.99, sum by (node, le) (rate(etcd_disk_backend_commit_duration_seconds_bucket{job="fabric-etcd"}[5m])))
```

```promql
etcd_server_proposals_pending{job="fabric-etcd"}
```

```promql
rate(etcd_server_proposals_committed_total{job="fabric-etcd"}[5m])
```

```promql
rate(etcd_server_proposals_applied_total{job="fabric-etcd"}[5m])
```

```promql
etcd_mvcc_db_total_size_in_bytes{job="fabric-etcd"}
```

```promql
etcd_mvcc_db_total_size_in_use_in_bytes{job="fabric-etcd"}
```

```promql
increase(etcd_server_leader_changes_seen_total{job="fabric-etcd"}[15m])
```

```promql
fabric_etcd_alarm_list_success{job="fabric-node"}
```

```promql
fabric_etcd_active_alarms{job="fabric-node"}
```

```promql
fabric_observer_kube_api_ready{job="fabric-observer"}
```

```promql
fabric_observer_kube_vip_lease_holder{job="fabric-observer"}
```

Host utilization and pressure graphs use the `fabric-node` job. PSI rates are
fractions of wall time, so `0.20` means that at least one task was waiting for
that resource during twenty percent of the interval:

```promql
100 * (1 - avg by (node) (rate(node_cpu_seconds_total{job="fabric-node",mode="idle"}[5m])))
```

```promql
100 * node_memory_MemAvailable_bytes{job="fabric-node"} / node_memory_MemTotal_bytes{job="fabric-node"}
```

```promql
100 * node_filesystem_avail_bytes{job="fabric-node",mountpoint=~"/|/var|/var/lib/etcd"} / node_filesystem_size_bytes{job="fabric-node",mountpoint=~"/|/var|/var/lib/etcd"}
```

```promql
rate(node_pressure_cpu_waiting_seconds_total{job="fabric-node"}[5m])
```

```promql
rate(node_pressure_memory_waiting_seconds_total{job="fabric-node"}[5m])
```

```promql
rate(node_pressure_memory_stalled_seconds_total{job="fabric-node"}[5m])
```

```promql
rate(node_pressure_io_waiting_seconds_total{job="fabric-node"}[5m])
```

```promql
rate(node_pressure_io_stalled_seconds_total{job="fabric-node"}[5m])
```

```promql
100 * rate(node_disk_io_time_seconds_total{job="fabric-node",device!~"^(dm-|loop|ram|zram).*$"}[5m])
```

```promql
rate(node_disk_io_time_weighted_seconds_total{job="fabric-node",device!~"^(dm-|loop|ram|zram).*$"}[5m])
```

For average kernel-observed I/O completion time, ignore intervals with no
completed operations:

```promql
(
  rate(node_disk_read_time_seconds_total{job="fabric-node",device!~"^(dm-|loop|ram|zram).*$"}[5m])
    /
  rate(node_disk_reads_completed_total{job="fabric-node",device!~"^(dm-|loop|ram|zram).*$"}[5m])
)
and
rate(node_disk_reads_completed_total{job="fabric-node",device!~"^(dm-|loop|ram|zram).*$"}[5m]) > 0
```

```promql
(
  rate(node_disk_write_time_seconds_total{job="fabric-node",device!~"^(dm-|loop|ram|zram).*$"}[5m])
    /
  rate(node_disk_writes_completed_total{job="fabric-node",device!~"^(dm-|loop|ram|zram).*$"}[5m])
)
and
rate(node_disk_writes_completed_total{job="fabric-node",device!~"^(dm-|loop|ram|zram).*$"}[5m]) > 0
```

This pack intentionally does not install Alertmanager, Grafana,
kube-state-metrics, or a TSDB on the root trio. Before timing the formal soak,
prove all four observer API targets, exactly one matching kube-vip source
leader and authoritative Lease holder, successful fresh authenticated alarm
collection on all roots, and zero active alarms.

Keep at least 48 continuous hours, including the planned follower and leader
reboots. Before dismantling the laptop observer, copy the Prometheus version and
digest, these exact configs, TSDB/evidence, alert history, and power-meter data
to encrypted storage outside the measured power domain.
