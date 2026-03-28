# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

Infrastructure-as-code for a multi-cluster Kubernetes setup. The **hub** cluster is the management plane that runs Flux CD and provisions child clusters (**cloud-cluster**, **simonet**, **ilumbaclusta**). Each site also has OpenWrt router configs and Fedora CoreOS (Butane) machine configs.

## Validation Commands

There is no single test suite. Use the narrowest validator for whatever files you touched:

**Kustomize** (single overlay):
```bash
kustomize build --load-restrictor=LoadRestrictionsNone hub/cluster/simonet >/dev/null
```

**Kustomize** (all overlays):
```bash
while IFS= read -r f; do
  kustomize build --load-restrictor=LoadRestrictionsNone "$(dirname "$f")" >/dev/null
done < <(git ls-files '*kustomization.yaml')
```

**Helm** (lint/render a chart):
```bash
helm lint charts/tailscale-subnet-router
helm template test charts/tailscale-subnet-router >/dev/null
```

**Helm** (k8s-control-plane requires values):
```bash
helm template test charts/k8s-control-plane \
  --set clusterName=dev --set externalIP=10.0.0.10 --set serviceIP=10.96.0.1 \
  --set serviceCIDRs[0]=10.96.0.0/12 --set clusterCIDRs[0]=10.244.0.0/16 \
  --set etcd.endpoints[0]=https://127.0.0.1:2379 \
  --set etcd.certIssuer.name=etcd --set etcd.certIssuer.kind=ClusterIssuer >/dev/null
```

**OpenTofu** (single module):
```bash
tofu -chdir=hub/tofu init -backend=false -input=false
tofu -chdir=hub/tofu validate
tofu -chdir=hub/tofu fmt -check
```
Tofu module roots: `hub/tofu`, `hub/cluster/cloud-cluster`, `hub/cluster/headscale`, `hub/cluster/simonet`.

**Shell scripts:**
```bash
bash -n scripts/build-router-image.sh
shellcheck scripts/build-router-image.sh
```
OpenWrt UCI scripts use ash: `shellcheck -s sh common/router/files/etc/uci-defaults/dns`

**Butane configs:**
```bash
butane --strict < common/butane/install.yaml >/dev/null
sops -d hub/butane/base.yaml | butane --strict -d hub/butane >/dev/null   # encrypted
```

**Router image build:**
```bash
scripts/build-router-image.sh <config_dir> <platform> <target> <profile>
# e.g. scripts/build-router-image.sh hub/router ipq40xx generic avm_fritzbox-4040
```

**Container image build:**
```bash
docker build -t bootie:dev apps/bootie
```

## Architecture

### Directory Layout

- `hub/` -- Primary management cluster (Flux source-of-truth, runs all operators)
  - `cluster/` -- Kubernetes manifests per namespace, reconciled by Flux Kustomizations
  - `tofu/` -- OpenTofu modules (Cloudflare, Tailscale, GitHub webhook, storage buckets)
  - `butane/` -- Fedora CoreOS Ignition configs for hub nodes
  - `router/` -- OpenWrt overlay for the hub router
- `simonet/`, `ilumbaclusta/` -- Child cluster + router configs (same structure: `butane/`, `router/`, optionally `cluster/`)
- `common/` -- Shared Butane and router configs inherited by all sites
- `charts/` -- Local Helm charts: `k8s-control-plane` (nested K8s control plane), `resources` (generic resource deployer), `tailscale-subnet-router`, `hcloud-cloud-controller-manager`
- `apps/` -- Apps that haven't made it to their own repo yet.
- `scripts/` -- Build and operational scripts
- `.kustomize-config/` -- Shared Kustomize transformer config referenced by overlays

### GitOps Flow

Flux CD watches this repo. `hub/cluster/flux-system/kustomizations.yaml` defines all Kustomizations as a dependency DAG. Each namespace directory under `hub/cluster/` is a Kustomization target. HelmReleases reference charts from `hub/cluster/flux-system/helm-repos.yaml` (23+ upstream repos) or the local `charts/` directory.

OpenTofu modules are applied in-cluster via Flux's tofu-controller, not manually.

### Child Clusters

All child clusters have their K8s control planes running inside the hub cluster via the `k8s-control-plane` Helm chart. Their configs live under `hub/cluster/<name>/`.

**cloud-cluster** is the active child cluster, running on Hetzner Cloud (hcloud). It has cloud-native infrastructure that the bare-metal sites don't: cluster-autoscaler (scaling cx33/cax21 VMs in nbg1), hcloud-cloud-controller-manager, hcloud-csi-driver for volumes, descheduler for utilization balancing, and KEDA-scaled Forgejo CI runners. Worker nodes are provisioned via cloud-init (Ubuntu + kubeadm join). It has its own Flux instance, Cilium, CoreDNS, cert-manager, external-dns, and Gateway API. OpenTofu in `hub/cluster/cloud-cluster/main.tf` manages the Hetzner network, firewall, and placement group. May expand to BinaryLane in the future.

**simonet** and **ilumbaclusta** are bare-metal sites (currently inactive). They use Fedora CoreOS nodes provisioned via Butane/bootie PXE and have OpenWrt router configs in their top-level directories.

### Secrets

SOPS with age encryption. Rules in `.sops.yaml`:
- `hub/cluster/**` -- encrypted to hub cluster key + personal key
- `*/butane/*.yaml` -- encrypted on lines marked `# cryptme`
- Everything else -- personal key only

Never commit plaintext secrets. Flux decrypts SOPS-encrypted manifests at reconciliation time.

### Networking

- Cilium CNI with Gateway API for in-cluster routing
- Cloudflare Tunnel for external ingress (managed by tofu)
- Tailscale mesh for inter-site connectivity
- External-DNS syncs DNS records to Cloudflare from Gateway HTTPRoutes/Services/Ingress
- cert-manager issues Let's Encrypt certs via Cloudflare DNS-01

## Code Style

- Lowercase kebab-case for filenames and Kubernetes resource names.
- Keep diffs minimal; don't reformat unrelated code.
- One Kubernetes resource per file unless tightly coupled.
- Avoid reordering items in `kustomization.yaml` resource lists.
- Helm: keep `required`/`fail` guards for required values; follow existing whitespace-control style (`{{-`/`-}}`).
- OpenTofu: always run `tofu fmt`; keep provider versions pinned; mark secret outputs `sensitive = true`.
- Bash: `#!/bin/bash` with strict mode; OpenWrt scripts under `files/etc/*` must be POSIX/ash-compatible.
- YAML types: keep booleans/numbers as native types unless the target API requires strings.

## Wakeword Override

If the prompt contains `#letjesustakethewheel`, switch to high-autonomy mode: continuously rebase onto `origin/main`, push, and drive Flux reconciliation to meet objectives. Stop only if an operation risks data loss or major outage.
