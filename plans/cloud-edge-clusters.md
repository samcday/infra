# Plan: Regional Edge Clusters on cloud-cluster

## Goal

Turn `cloud-cluster` into the durable substrate for a fleet of regional hosted control planes that can scale worker capacity to zero and be treated as cattle by higher-level GitOps/controllers.

## Fixed Decisions

- `cloud-cluster` itself stays permanently rooted in `hub` etcd.
- The etcd we run inside `cloud-cluster` is only for regional edge clusters.
- Regional worker pools should be able to scale to zero.
- `cloud-cluster` should keep a minimum baseline of `3x cx23` nodes for now.
- US regions use Hetzner `cpx11` in `hil1` and `ash1`.
- Australia is split into `au-east` and `au-west`.
- Singapore is its own `ap-southeast` region.

## Regional Layout

| Region | Provider | Backing locations | Baseline worker floor |
| --- | --- | --- | --- |
| `us-east` | Hetzner | `ash1` | `0` |
| `us-west` | Hetzner | `hil1` | `0` |
| `au-east` | BinaryLane | `bne`, `syd`, `mel`, `adl` | `0` |
| `au-west` | BinaryLane | `per` | `0` |
| `ap-southeast` | BinaryLane | `sin` | `0` |

## Architecture

### Control-plane rooting

- `hub` remains the root of trust and the long-term home of `cloud-cluster` state.
- `cloud-cluster` continues using the shared `hub` etcd endpoints exactly as it does today.
- Regional edge clusters are hosted under `cloud-cluster`, not directly under `hub`.

### Regional etcd

- `cloud-cluster` gets its own dedicated etcd only for hosted regional clusters.
- `etcdetcetc` will carve out one tenant/prefix per regional cluster on that cloud-local etcd.
- We should keep the hosted regional control planes lightweight and standardized so that higher-level controllers can create, suspend, and retire them cheaply.

### Storage and DR

- Do not back the cloud-local etcd with hcloud PVCs.
- Assume local node disk for the first pass so we can evaluate how much regional footprint fits inside the minimum substrate.
- Follow-up DR work should replicate that cloud-local etcd into a follower in `hub`, where we can use Ceph RBD-backed storage and snapshot it into Ceph buckets.
- That DR/follower work is intentionally out of scope for this pass.

## Rollout Steps

### 1. Raise the cloud-cluster substrate floor

- Change the baseline autoscaled substrate to `3x cx23`.
- Remove the current `cax11` minimum so the floor is a single homogeneous amd64 pool.
- Keep the rest of the autoscaler behavior unchanged for now.

### 2. Add region-oriented worker pools in cloud-cluster

- Add Hetzner autoscaling groups for `us-east` (`ash1`, `cpx11`) and `us-west` (`hil1`, `cpx11`).
- Add BinaryLane-backed regional groups for `au-east`, `au-west`, and `ap-southeast`.
- Keep all regional pools at `minSize: 0`.
- Label and taint those nodes so hosted edge workloads can target their intended region cleanly.

### 3. Stand up cloud-local etcd for hosted regional clusters

- Run a small multi-member etcd inside `cloud-cluster`.
- Do not use PVC-backed storage in this first pass.
- Add a dedicated etcd CA/issuer for this regional-cluster backing store.

### 4. Bridge `etcdetcetc` and `k8s-control-plane`

- Extend `etcdetcetc` so a tenant can specify the client identity expected by `k8s-control-plane`.
- Extend `etcdetcetc` so a tenant can specify the exact etcd prefix instead of relying only on `{namespace}-{name}`.
- Add cert-manager integration so `etcdetcetc` can provision the `apiserver-etcd-client` secret used by the hosted control plane.
- Teach `charts/k8s-control-plane` to consume an existing etcd client secret instead of always minting its own.

### 5. Standardize a regional hosted-cluster template

- One namespace per regional cluster inside `cloud-cluster`.
- One `EtcdTenant` per regional cluster.
- One hosted control-plane release per regional cluster.
- One minimal add-on set per regional cluster: API exposure, Cilium, CoreDNS, metrics-server, and only the GitOps/runtime pieces needed for delegated management.

### 6. Prove the lifecycle model

- Bring up one regional cluster end to end.
- Validate cold start, workload scheduling, API reachability, and scale-to-zero worker behavior.
- Add the controller/GitOps layer that can suspend or wake regional footprints based on policy and local time.

## Validation Checklist

- `kustomize build --load-restrictor=LoadRestrictionsNone hub/cluster/cloud-cluster >/dev/null`
- `helm template test charts/k8s-control-plane --set clusterName=dev --set externalIP=10.0.0.10 --set serviceIP=10.96.0.1 --set 'serviceCIDRs[0]=10.96.0.0/12' --set 'clusterCIDRs[0]=10.244.0.0/16' --set 'etcd.endpoints[0]=https://127.0.0.1:2379' --set etcd.certIssuer.name=etcd --set etcd.certIssuer.kind=ClusterIssuer >/dev/null`

## Risks

- `cloud-cluster` remains operationally dependent on `hub`, by design.
- Running cloud-local regional etcd without PVCs increases failure sensitivity until follower-based DR lands.
- Provider-specific node provisioning still needs a clean common abstraction before this can scale to many regions.
- Hosted control-plane sprawl will be expensive to operate if we do not standardize the template and lifecycle hooks early.

## Immediate Next Move

- Execute Step 1 now: switch the baseline `cloud-cluster` substrate to a `3x cx23` minimum.
