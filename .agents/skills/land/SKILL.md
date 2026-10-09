---
name: land
description: >-
  Land changes in this infrastructure repository only when the user explicitly
  requests landing or merging. Not for review, preparation, passing checks, or
  skill installation.
disable-model-invocation: true
metadata:
  delta-action: land
---

# Land

An invocation is the landing request: proceed without asking again. Follow
`AGENTS.md`; preserve unrelated work.

1. **Prepare.** Fetch `origin/main`, commit the requested changes, and rebase on
   `origin/main`. Resolve clear conflicts automatically; stop on ambiguous intent.

2. **Check the final changes before pushing.** Run `git diff --check`, relevant
   existing component tests, and affected renders, including consumers of shared
   bases/charts. Keep checks proportional; documentation changes need no app build.
   - Kustomize: `kubectl kustomize <path>`.
   - Helm: render affected releases with their actual chart version, namespace,
     and values; the invocation pattern is in
     `charts/k8s-control-plane/tests/test_external_etcd_handoff.py:43`.
   - Use the component checks in `AGENTS.md` and their existing test runners.
   All applicable required checks must pass on the changes being landed.
   Missing, pending, failed, or unverifiable checks are blockers.

3. **Publish.** Push normally to `origin/main` and verify the remote contains the
   landed commit. No PR ceremony unless current repository rules require it.
   Never force-push; if main advances, rebase and repeat affected checks.

4. **Wait for green.** Relevant image builds must pass
   (`.github/workflows/images.yaml`). Each active infra source root and its
   infra-backed consumers must have consumed the landed commit (or a descendant
   containing it); Hub consumption alone does not prove child-source convergence.
   Every active **infra-owned** reconciler must be healthy. Use the ownership
   boundary below, not cluster-wide health.
   Follow the in-scope hub and cloud fan-outs, including child-cluster releases
   (`hub/cluster/flux-system/kustomizations.yaml`,
   `hub/cluster/cloud-cluster/flux-system/`).
   Include infra-owned Terraform resources, not every resource watched by
   tofu-controller. Require current desired-state readiness, not stale green
   status; report the source roots checked and intentionally suspended or
   external-repository exclusions. Load `hub-diagnostics` and follow its bounded,
   read-only access rules. No live patches or forced reconciliation without Sam's
   specific permission.

5. **Sync parent.** After successful landing, ask the main/parent Delta thread to
   rebase its `infra` worktree on `origin/main`.

Do not stop at a successful push. If verification fails or times out, report the
blocker and distinguish “not pushed” from “pushed, but not successfully reconciled.”

## Reconciliation ownership boundary

Ownership means **where a resource's desired-state manifest is defined**, not
which cluster, namespace, or controller it shares with infra:

- Start with GitRepository sources for this Git repository. Match repository
  identity, allowing equivalent SSH/HTTPS URLs, rather than just the name `infra`.
  Include independent copies such as `cloud/edge/GitRepository/infra`; they do not
  need an owner reference to `hub/flux-system/GitRepository/infra`.
- Follow Kustomizations applying this repository and the resources they apply,
  including HelmReleases and their rendered resources. An infra-owned
  HelmRelease remains in scope when it uses a third-party Helm chart. Continue
  through generated same-repository sources and fan-outs.
- Establish those edges from repository manifests/renders, Kustomization
  inventories and `kustomize.toolkit.fluxcd.io/{name,namespace}` tracking labels,
  or Helm release metadata. Preserve context, namespace, and resource kind at
  each step; account for remote targets and Helm target/storage namespaces.
  Kubernetes `ownerReferences` alone are not a Flux ownership graph. Do not read
  kubeconfig Secrets or credential-bearing release storage to establish edges.
- **Stop expansion at another Git repository.** GitRepository and Kustomization
  handoff objects defined/rendered by infra remain in scope, but resources applied
  by those Kustomizations from a different repository do not. Creating a tenant's
  Flux source does not make all of that tenant's workloads infra-owned. Check
  handoff readiness against its own source revision, not infra's commit SHA.
  A Terraform's module `sourceRef` likewise does not determine ownership of its
  manifest; trace who applied the Terraform resource.
- For example, infra owns `hub/cloud-cluster/HelmRelease/rokkitpokkit-tenant`
  and the tenant GitRepository/Kustomization it renders. It does **not** own
  `cloud/rokkitpokkit/Terraform/tofu`, whose manifest is applied from the
  rokkitpokkit repository. That Terraform's lock is not an infra landing blocker.
  Conversely, Terraform manifests defined here and the control-plane release
  sourced through `cloud/edge/GitRepository/infra` remain in scope.
- Excluding external descendants does not waive an in-scope reconciler's own
  readiness or explicit health checks/dependencies. If a required ownership edge
  cannot be established, report that specific verification gap instead of
  silently excluding it or falling back to an all-cluster green gate.
