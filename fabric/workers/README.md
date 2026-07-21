# Fabric services workers

This tree manufactures the first two persistent K3s agent nodes for the
fabric root cluster:

| Node | Services address | Role |
| --- | --- | --- |
| `fabric-az1-svc1` | `10.66.1.10/24` | worker-hosted platform services |
| `fabric-az1-svc2` | `10.66.1.11/24` | worker-hosted platform services |

The profiles are intentionally independent from `fabric/butane`. They contain
no etcd binary, etcd CA or member identity, K3s server token, kube-vip image,
consensus recovery key, or root-node Ignition merge. The only cluster secret
needed here is a full, CA-pinned K3s **agent** or short-lived bootstrap token
supplied at render time from a protected file outside Git.

These are bootstrap workers, not hosted Kubernetes control-plane nodes. They
join the fabric root K3s cluster as agents and are labeled and tainted
`fabric.samcday.com/platform=true`. Flux, DNS, metrics-server, and future
hosted-control-plane Pods must explicitly select and tolerate that class.
The trusted precreated placeholder owns the `NoSchedule` taint because a
kubelet that adopts an existing Node UID does not reconcile
`registerWithTaints`. The agent profile retains the same registration taint as
a fail-safe fallback, but on the intended exact-UID path K3s agent
reconciliation contributes only the custom platform label. After the exact
placeholder UID is Ready, the trusted finalizer assigns the reserved
`node-role.kubernetes.io/worker=true` label while removing bootstrap metadata.

## Admission prerequisites and transitional flat L2

The addresses above are final even though the first physical attachment is
temporary. During induction, the root and services prefixes may share one
trusted Ethernet broadcast domain through daisy-chained unmanaged switches:

```text
OpenWrt eth1/br-lan (.0.1 and .1.1)
  -> unmanaged root switch (.0.0/24 roots)
       -> unmanaged services switch (.1.0/24 svc1/svc2)
```

This is still a routed two-prefix design. OpenWrt must own both
`10.66.0.1/24` and `10.66.1.1/24` on that LAN and permit the reviewed
same-interface hairpin flows. Each worker has an explicit
`10.66.0.0/24 via 10.66.1.1` route, uses `10.66.1.1` as its default gateway,
and rejects ICMP redirects so the shared wire cannot silently turn the root
prefix into an accidental direct route. The router must not emit redirects,
and the roots must likewise retain their routed path to `10.66.1.0/24`.

Before attaching either installed node, all of the following remain hard
gates:

- `10.66.1.1/24` exists on the router and serves the exact pinned public asset
  set at `http://10.66.1.1/static/`;
- router and root host policies admit only the reviewed K3s API through the
  VIP and all three supervisor-advertised server addresses, Flannel/VXLAN,
  kubelet, DNS/NTP, asset, and operator flows while continuing to deny services
  addresses access to root etcd;
- the candidate has a final reviewed inventory capture; and
- its matching file under `inventory/` has been changed from `pending` to
  `admitted` with exact evidence-derived values.

The flat L2 is transport, not a security boundary: a hostile peer can spoof an
admitted source, poison ARP, or communicate on the wire without traversing the
router. Until the managed switch enforces separate root and services VLANs,
svc1 and svc2 are limited to reviewed fabric plumbing such as Flux, CoreDNS,
and metrics-server. Do not place tenant/untrusted workloads, hosted child
etcd, child-cluster credentials, VMs, or LLM sandboxes on them during this
phase. The existing platform taint keeps ordinary workloads off; reconciliation
must not add the explicit placement/toleration needed by those deferred
workloads.

When the managed switch arrives, make the OpenWrt port a trunk and the two
switch uplinks access ports for their respective VLANs. The worker profiles,
node IPs, K3s identities, and explicit routed root-prefix path stay unchanged;
no node readdress or K3s re-registration is required.

The installed `fabric-services-route.service` verifies on every boot that the
final NetworkManager profile is active, `10.66.0.254` resolves through
`10.66.1.1`, the router remains directly connected, and IPv4 redirects are
disabled. K3s depends on that static path check, but the check deliberately
does not require the API to be up: the agent's normal retry loop must survive
service-first power restoration. During attended qualification, require the
equivalent live route to read:

```text
10.66.0.254 via 10.66.1.1 dev <admitted-nic> src 10.66.1.10  # svc1
10.66.0.254 via 10.66.1.1 dev <admitted-nic> src 10.66.1.11  # svc2
```

Then prove each joined Node has its exact `InternalIP`, a Pod on svc1 can reach
a Pod on svc2 over Flannel VXLAN, and metrics-server can scrape kubelet
`10250` on all five Nodes. Do not schedule a test Pod onto a sacred root just
to exercise VXLAN. Node readiness qualifies the API path; the service-to-
service Pod probe and metrics results qualify the active UDP `8472` and
kubelet paths which a static Ignition render cannot prove.

## Inventory admission

`inventory/fabric-az1-svc{1,2}.yaml` are fail-closed admission records, not
discovery results. Populate one only after preserving a reviewed final capture.
The committed record binds:

- chassis serial and DMI product UUID;
- permanent globally administered wired MAC;
- exact stable whole-disk `/dev/disk/by-id/` identity, serial, and byte size;
- usable TPM2 evidence; and
- SHA-256 of the reviewed inventory capture.

Do not set `state: admitted` to make a check pass. A changed chassis, NIC, TPM,
or disk requires a fresh capture and review.

## Render one sensitive Ignition

Capture a full, CA-pinned K3s agent token as described below, in a mode-`0600`
regular file outside this checkout. A full, CA-pinned token begins with `K10`
and includes the fabric server-CA hash; a bare credential is deliberately
rejected. Then render only the admitted node into an empty protected directory
outside Git:

```sh
FABRIC_AGENT_TOKEN_FILE=/secure/fabric/agent.token \
  fabric/workers/butane/bootstrap.sh \
    --node fabric-az1-svc1 \
    /secure/fabric-worker-ignitions
```

The renderer works in verified tmpfs, refuses pending inventory, validates the
token without printing it, embeds the inventory MAC, and writes the final
Ignition mode `0600`. Run the public structural check with:

```sh
fabric/workers/butane/bootstrap.sh --check
```

## Audited local-media fallback

After rendering, repeat the exact admitted disk identity explicitly:

```sh
scripts/build-fabric-worker-isos \
  --node fabric-az1-svc1 \
  --ignition-dir /secure/fabric-worker-ignitions \
  --output-dir /secure/fabric-worker-installers \
  --cache-dir /secure/fabric-installer-cache \
  --disk /dev/disk/by-id/REPLACE_FROM_ADMITTED_INVENTORY
```

The builder verifies the Fedora live image and signature, inspects the rendered
Ignition, compares the explicit disk to the committed admission, and embeds an
attended pre-install gate. The live gate revalidates DMI identity, TPM2, MAC,
disk identity/serial/size, static services address, and the router asset
manifest before displaying its exact destructive confirmation. A successful
install powers off. Before the first local-disk boot, remove AC power from only
that node for at least 30 seconds, then restore it; do not use Alt-P for this
transition. This gives the TPM a real platform reset before Ignition binds the
root volume.

Each ISO contains the K3s agent token and remains secret media. It installs an
encrypted FCOS root bound to the local TPM2. There is deliberately no recovery
key: these two nodes contain reconstructible platform runtime and are rebuilt
from Git plus root-cluster state if their TPM or disk is lost. Do not place
irreplaceable local state on them.

Sam's preferred induction path is Bootie/PXE. The durable design must therefore
run its boot service somewhere that does not depend on these two workers
already being alive, and must authenticate worker identity and one-use install
authorization. The ISO builder above is an audited fallback/manufacturing path,
not the promised default. The existing USB writer also needs a separately
reviewed basename extension before it will accept the new
`fabric-az1-svc*-installer.iso` outputs.

The PXE manufacturing adapter, one-use Bootie delivery contract, remaining
attended-station gates, and resumable ceremony are documented in
[`bootie/README.md`](bootie/README.md).

## Capture the current CA-pinned agent token without durable plaintext

K3s has normalized the live agent credential into a full token that pins the
fabric server CA. Fetch that normalized token from a root over the existing
host-key-pinned SSH path directly into verified tmpfs; do not print it or pass
it as a command-line argument:

```sh
source scripts/lib/fabric-secure-tempdir.sh
token_dir=$(fabric_secure_tmpdir fabric-worker-token 4096)
trap 'find "$token_dir" -mindepth 1 -delete 2>/dev/null || true; rmdir "$token_dir" 2>/dev/null || true' EXIT
scripts/fabric-ssh cp1 \
  sudo --non-interactive /usr/bin/cat \
    /var/lib/rancher/k3s/server/agent-token \
  >"$token_dir/agent.token"
chmod 0600 "$token_dir/agent.token"
grep -Eq '^K10[0-9a-f]{64}::node:[A-Za-z0-9]{16,512}$' \
  "$token_dir/agent.token" || {
    echo 'live agent token is not in the expected CA-pinned form' >&2
    exit 1
  }

FABRIC_AGENT_TOKEN_FILE="$token_dir/agent.token" \
  fabric/workers/butane/bootstrap.sh \
    --node fabric-az1-svc1 \
    /secure/fabric-worker-ignitions
```

The encrypted root bootstrap profile under `fabric/butane` deliberately retains
the bare credential required by the existing offline consensus recovery design.
That bare credential does **not** pin the server CA, is not valid worker
induction input, and must not be extracted for this renderer. For a future
worker-only Bootie flow, create one short-lived K3s bootstrap token per attended
induction and deliver only its full `K10<CA-hash>::<id>.<secret>` form.

## Reimage a stable worker name

K3s stores a per-node password locally and in a `kube-system` Secret after the
first successful join. Reimaging a disk loses the local copy; reusing
`fabric-az1-svc1` or `fabric-az1-svc2` while its old Node/password Secret still
exists is therefore expected to fail as a duplicate hostname.

Treat reimage as an attended decommission ceremony. First evacuate or accept the
loss of the node's reconstructible workloads and power the old installation
off. With explicit authorization for the live deletion, remove the exact Node,
then verify that Kubernetes garbage collection removed its exact K3s password
Secret before arming any installer:

```sh
node=fabric-az1-svc1 # or fabric-az1-svc2
scripts/ik --context=fabric delete node "$node"
if scripts/ik --context=fabric -n kube-system get \
    "secret/$node.node-password.k3s" >/dev/null 2>&1; then
  scripts/ik --context=fabric -n kube-system wait \
    --for=delete "secret/$node.node-password.k3s" --timeout=2m
fi
```

If the Secret remains after the Node is gone, stop. Inspect its owner reference
and perform a separately authorized deletion of only
`kube-system/$node.node-password.k3s`; never wildcard or bulk-delete node
password Secrets. This runbook is not needed for a candidate that has never
joined, and none of the render/build helpers mutate the live cluster.
