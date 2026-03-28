# AGENTS.md
## Purpose
- This repo is infra-as-code for multiple clusters/sites (`hub`, `cloud`, `simonet`, etc) and supporting images/charts/scripts.
- There is no traditional unit-test suite; validation is primarily render/lint/validate commands.
- Keep changes narrow, reversible, and validated only for touched subsystems.
## Repo Map
- `hub/cluster/`: Flux-managed Kubernetes manifests + cluster-specific OpenTofu modules.
- `hub/tofu/`: shared OpenTofu for Cloudflare, Tailscale, GitHub webhook, and secret propagation.
- `charts/`: local Helm charts (`k8s-control-plane`, `resources`, `tailscale-subnet-router`, `hcloud-cloud-controller-manager`).
- `*/butane/`: Fedora CoreOS Butane configs, often SOPS-encrypted.
- `*/router/` and `common/router/`: OpenWrt overlay files and package lists.
- `apps/bootie/`: PXE/Ignition helper container.
- `scripts/`: helper scripts, including OpenWrt image builds.
## Cursor/Copilot Rules
- `.cursorrules` not found.
- `.cursor/rules/` not found.
- `.github/copilot-instructions.md` not found.
## Wakeword Override
- If the prompt contains `#letjesustakethewheel`, switch to high-autonomy mode.
- In this mode, continuously rebase onto `origin/main`, continuously push toward `origin/main`, and drive live Flux reconciliation state to meet stated objectives.
- Stop only if an operation appears risky enough to cause data loss or a major cluster outage.
## Tooling Baseline
- Main tools: `kustomize`, `helm`, `tofu`, `sops`, `butane`, `bash`, `docker`/`podman`.
- Helpful validators: `shellcheck`, `shfmt`, `yamllint`, `kubeconform`.
- Use OpenTofu (`tofu`) for HCL in this repository.
## Build/Lint/Test Commands
### Router Image Build
- Generic:
```bash
scripts/build-router-image.sh <config_dir> <platform> <target> <profile>
```
- Examples:
```bash
scripts/build-router-image.sh hub/router ipq40xx generic avm_fritzbox-4040
scripts/build-router-image.sh simonet/router ramips mt7621 asus_rt-ax53u
```
### bootie Container Build
```bash
docker build -t bootie:dev apps/bootie
```
### Hub Bootstrap Ignition
```bash
./hub/butane/bootstrap.sh > /tmp/hub-bootstrap.ign
```
### Kustomize Validation
- Single overlay (single-test equivalent):
```bash
kustomize build --load-restrictor=LoadRestrictionsNone hub/cluster/simonet >/dev/null
```
- All kustomizations:
```bash
while IFS= read -r f; do
  kustomize build --load-restrictor=LoadRestrictionsNone "$(dirname "$f")" >/dev/null
done < <(git ls-files '*kustomization.yaml')
```
### Helm Validation
- Single chart lint:
```bash
helm lint charts/tailscale-subnet-router
```
- Single chart render:
```bash
helm template test charts/tailscale-subnet-router >/dev/null
```
- `k8s-control-plane` render with required values:
```bash
helm template test charts/k8s-control-plane --set clusterName=dev --set externalIP=10.0.0.10 --set serviceIP=10.96.0.1 --set serviceCIDRs[0]=10.96.0.0/12 --set clusterCIDRs[0]=10.244.0.0/16 --set etcd.endpoints[0]=https://127.0.0.1:2379 --set etcd.certIssuer.name=etcd --set etcd.certIssuer.kind=ClusterIssuer >/dev/null
```
### OpenTofu Validation
- Single module:
```bash
tofu -chdir=hub/tofu init -backend=false -input=false
tofu -chdir=hub/tofu validate
tofu -chdir=hub/tofu fmt -check
```
- Current module roots: `hub/tofu`, `hub/cluster/cloud-cluster`, `hub/cluster/headscale`, `hub/cluster/simonet`.
### Shell Validation
- Single Bash script syntax + lint:
```bash
bash -n scripts/build-router-image.sh
shellcheck scripts/build-router-image.sh
```
- OpenWrt shell snippet lint (`ash` style):
```bash
shellcheck -s sh common/router/files/etc/uci-defaults/dns
```
### Butane Validation
- Plaintext file:
```bash
butane --strict < common/butane/install.yaml >/dev/null
```
- Encrypted file:
```bash
sops -d hub/butane/base.yaml | butane --strict -d hub/butane >/dev/null
```
## What "Single Test" Means in This Repo
- There is no single `test` command.
- Use the narrowest validator for touched files:
  - Kustomize change -> one `kustomize build` for that path.
  - Helm change -> one `helm lint` or `helm template` for that chart.
  - Tofu change -> one `tofu validate` for that module.
  - Shell change -> `bash -n` + `shellcheck` on that script.
  - Butane change -> `butane --strict` (with `sops -d` for encrypted files).
## Code Style Guidelines
### General Editing
- Keep diffs minimal; never reformat unrelated files.
- Preserve each file family's existing indentation and formatting style.
- Keep list ordering stable when possible (resources, providers, env vars).
- Prefer lowercase kebab-case for filenames and Kubernetes resource names.
### Imports / Dependencies
- There are no language import blocks here; treat dependency declarations carefully instead.
- Relevant dependency surfaces: Helm values/schema, Kustomize resources/generators, OpenTofu providers/data/resources, shell runtime binaries.
- Add dependencies only when necessary and update adjacent docs/config in same change.
### YAML / Kubernetes / Kustomize
- Keep `apiVersion`, `kind`, and `metadata` explicit.
- Prefer one Kubernetes resource per file unless tightly coupled.
- Avoid gratuitous reordering in `kustomization.yaml` lists.
- Preserve `.kustomize-config` wiring where it is already referenced.
- Never commit plaintext secrets in cluster manifests.
### Helm Templates
- Keep render-time guards for required values (`required`, `fail`).
- Keep template logic readable; factor repeated logic into helper blocks.
- Preserve existing whitespace-control style (`{{-` / `-}}`).
- Put configurable behavior in `values.yaml` rather than hardcoding.
### OpenTofu / Terraform HCL
- Run `tofu fmt` in every touched module.
- Keep provider versions pinned in `required_providers`.
- Mark secret outputs with `sensitive = true`.
- Avoid renaming resources unless migration/replacement is explicitly intended.
- Keep changes idempotent for Flux tofu-controller reconciliation.
### Shell Scripts
- Bash scripts should use `#!/bin/bash` plus strict mode.
- OpenWrt runtime scripts under `files/etc/*` should remain POSIX/ash-compatible.
- Validate required inputs early; fail with clear messages.
- Quote expansions unless word splitting is intentional.
- Use `|| true` only for explicitly non-fatal operations.
### Types, Naming, and Error Handling
- Keep booleans/numbers as native YAML/HCL types unless target API requires strings.
- Env vars: uppercase snake case.
- Helm values keys: follow existing style (mostly lowerCamelCase).
- Terraform/OpenTofu labels: keep existing module conventions.
- Scripts should guard required files/env vars before mutating state.
- Prefer explicit validation failures over implicit defaults for critical infra values.
## Secrets and Encryption
- SOPS is mandatory for sensitive content.
- Follow `.sops.yaml` and preserve `# cryptme` markers where used.
- Never commit decrypted key material or plaintext secret artifacts.
- Do not print secret values in logs, templates, or comments.
## Agent Checklist
- Identify touched subsystem(s) first.
- Run single-target validation for each touched subsystem.
- Run broader checks only when changes span multiple subsystems.
- Re-check that no plaintext secret changes are included.
- Keep final diffs focused and rollout-safe.
