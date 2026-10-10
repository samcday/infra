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

Success means the declared scope reconciled, not that the whole fleet is green.

1. **Prepare.** Fetch `origin/main`, commit the requested changes, and rebase on
   `origin/main`. Resolve clear conflicts automatically; stop on ambiguous intent.
   Record the base and candidate SHAs.

2. **Scope.** Derive the checklist from the final diff, not the whole cluster.
   - Trace the diff's old and new sides through source/path references, shared
     Kustomize/Helm inputs, Terraform `path:` directories (Terraform CRs read
     `.tf` files directly) and generated fan-outs
     (`hub/cluster/flux-system/kustomizations.yaml`,
     `hub/cluster/cloud-cluster/flux-system/`). Resolve ownership from renders
     and live inventories, not directory prefixes, and stay inside the ownership
     boundary below.
   - Include affected reconcilers, their prerequisites and workload outcomes;
     identify remote target clusters separately. A shared parent, source or
     prerequisite does not pull in unrelated siblings.
   - Show the checklist: context/namespace/kind/name, why included, and the
     before/after check. List intentional suspended exclusions; ask if scope is
     uncertain.
   - Until decoupled, every infra push also includes HelmReleases
     `cloud-cluster/control-plane` (hub) and `edge/edge-au-east-control-plane`
     (cloud), plus the Jobs for their new release revision:
     `reconcileStrategy: Revision` upgrades them on every infra commit, which
     reruns bootstrap. Older revisions' Jobs are not gates.

3. **Preflight.** Everything required must pass before pushing.
   - Static: `git diff --check <base> <candidate>`, relevant component tests from
     `AGENTS.md`, and affected renders, including shared-input consumers. Use
     `kubectl kustomize <path>` for Kustomize; for Helm, render affected releases
     with their actual chart version, namespace and values (invocation pattern:
     `charts/k8s-control-plane/tests/test_external_etcd_handoff.py:43`).
     Documentation needs no app build.
   - Live: prove access to every context in scope (a fresh worktree lacks
     child-cluster CAs: run `scripts/credhelper --init`), then record readiness,
     observed generations and revisions. Existing targets must be healthy and
     current; new targets need healthy prerequisites. Missing access or
     unverifiable checks block the push.
   - Repair/removal: ask Sam to approve the named pre-existing failures; the
     declared postflight outcome is still required.

4. **Publish.** Push normally to `origin/main` and verify the remote contains the
   landed commit. No PR ceremony unless current repository rules require it.
   Never force-push; if main advances, rebase, recompute scope and repeat preflight.

5. **Verify the same scope.**
   - Each infra source root in scope (hub, and child copies such as
     `cloud/edge/GitRepository/infra`) consumed the landed commit or a
     descendant; hub consumption alone does not prove child convergence.
   - Each target reached its own desired revision, current generation and health,
     not merely source readiness or stale green. Verify planned
     removals/suspensions.
   - Relevant image builds passed (`.github/workflows/images.yaml`); for intended
     rollouts, follow the built tag/digest through automation into the workload.
   - Report the source roots checked and exclusions. Unrelated failures are
     non-blocking notes.

6. **Sync parent.** After successful landing, ask the main/parent Delta thread to
   rebase its `infra` worktree on `origin/main`.

Load `hub-diagnostics` for every cluster read (preflight and verification) and
follow its bounded, read-only rules. No live patches or forced reconciliation
without Sam's specific permission.

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
