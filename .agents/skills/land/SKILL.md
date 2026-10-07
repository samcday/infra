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
   - Kustomize: `kubectl kustomize <path>`; follow the pattern in
     `fabric/cluster/lab/tests/test_contract.py:15`.
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
   (`.github/workflows/images.yaml`), Flux must have consumed the landed commit
   (or a descendant containing it), and every active reconciler must be healthy.
   Follow the Hub and Fabric fan-outs, including child-cluster releases
   (`hub/cluster/flux-system/kustomizations.yaml`,
   `hub/cluster/cloud-cluster/flux-system/`, `fabric/cluster/flux-system/`).
   Include tofu-controller's Terraform resources. Require current desired-state
   readiness, not stale green status; list intentionally suspended exclusions.
   Use bounded, read-only checks through `scripts/ik --context=<context> -n
   <namespace> get ...` (`scripts/ik:16`). No live patches or forced reconciliation
   without Sam's specific permission.

5. **Sync parent.** After successful landing, ask the main/parent Delta thread to
   rebase its `infra` worktree on `origin/main`.

Do not stop at a successful push. If verification fails or times out, report the
blocker and distinguish “not pushed” from “pushed, but not successfully reconciled.”
