# AGENTS.md

## Repo Shape
- This is an infrastructure repo, not a single app workspace; there is no root build, lint, or test command.
- `hub/cluster/flux-system/kustomizations.yaml` is the hub Flux fan-out that creates Flux Kustomizations for `hub/cluster/*`, `common/butane`, `simonet/butane`, and `ilumbaclusta/butane` paths.
- `hub/cluster/cloud-cluster/` defines the Hetzner child cluster and app Flux sources; app workload manifests usually live in each app repo under `infra/k8s/`, not here.
- `charts/resources` is a helper chart: each values key renders one resource, `_` is merged into every resource, `metadata.name` defaults to the key, and `apiVersion` defaults to `v1`.
- Use `charts/resources` sparingly. Prefer direct manifests for normal Flux objects and workloads; reserve `charts/resources` for small glue resources that must be rendered from parent-cluster-only data such as SOPS secrets, generated values, or cross-cluster handoff inputs.
- `.kustomize-config` is intentionally included by top-level cluster kustomizations so Kustomize rewrites Secret/ConfigMap references inside HelmRelease values.

## Cluster Access
- Use `scripts/ik --context=hub ...` for the management cluster,
  `scripts/ik --context=cloud ...` for app workloads, and
  `scripts/ik --context=fabric ...` for the Fabric root cluster; it always uses
  the repo-local `kubeconfig`.
- `.mcp.json` configures a read-only Kubernetes MCP server against `./kubeconfig` with `core,config,helm` toolsets.
- For app debugging, check Flux `GitRepository`/`Kustomization` objects in the hub `cloud-cluster` namespace, then workloads in the same app namespace on the `cloud` context.
- Mutating cluster state directly is expressly forbidden unless Sam explicitly allows it for the specific operation. Make changes through Git and Flux reconciliation by default; use direct `kubectl`/`scripts/ik` mutation only when explicitly authorized.

## Component Commands
- `apps/etcdetcetc` is a Rust Kubernetes controller workspace; run commands from that directory.
- `cargo run -p etcdetcetc -- crds` prints CRDs, `cargo xtask dev-up` creates the kind dev cluster and `.dev/kubeconfig`, and `cargo xtask dev-down` removes them.
- `scripts/sync-chart-crds --check` proves the Helm CRDs match the Rust types.
  `scripts/lifecycle-integration --check` validates immutable prerequisites;
  `--run` creates and tears down a disposable kind API plus real etcd 3.6.13
  and exercises the delegated-CA tenant lifecycle without reading Fabric.
- `cargo xtask dev-up` requires `kind`, `docker`, `kubectl`, and `crane`; it also creates/reuses Docker registry `127.0.0.1:5001`.
- The `etcdetcetc` Tilt path uses `tilt up` or `tilt ci` and builds `x86_64-unknown-linux-musl`; local setup needs protobuf and musl tooling like the devcontainer installs.
- The shipped `apps/etcdetcetc` image strips `xtask` from `Cargo.toml`; do not rely on `xtask` existing in the runtime container.
- `credhelper` is a separate Rust crate; use `./scripts/credhelper <hub|cloud>` or `./scripts/credhelper --init`, and the wrapper rebuilds `credhelper/target/release/credhelper` when sources change.
- Router images are built with `scripts/build-router-image.sh <hub/router|simonet/router|ilumbaclusta/router> <platform> <target> <profile>`; it overlays `common/router`, decrypts `files.enc/` with SOPS, and caches under `_build/`.

## Secrets And Generated Files
- Use SOPS for secrets and never commit decrypted material; `.sops.yaml` encrypts `hub/cluster` `data`/`stringData`, Butane values marked by `# cryptme`, `hub/pki/k8s/*.enc`, and otherwise falls back to Sam's personal age key.
- Butane YAML under `hub/butane`, `common/butane`, `simonet/butane`, and `ilumbaclusta/butane` is packaged by Kustomize `secretGenerator`; bootie renders it to Ignition at runtime.
- CI builds `apps/bootie`, `apps/etcd-smoke`, `apps/etcdetcetc`,
  `apps/headscale-node-cleaner`, and `apps/node-joiner` images on `main`. Pushes build only changed app
  contexts and tag them as `YYYYMMDDHH.<workflow-run-id>`; manual dispatches
  rebuild all four.
- Flux image automation updates tags in `hub/cluster` using comments like
  `# {"$imagepolicy": "flux-system:bootie:tag"}`. Keep those comments on
  automated releases; deliberately frozen, digest-gated releases omit them.

## OpenTofu
- OpenTofu is reconciled by tofu-controller from `Terraform` CRs such as `hub/cluster/*/tofu.yaml`; these use `approvePlan: auto` and `disableDriftDetection: true`.
- Local `tofu plan` needs the same provider credentials the controller receives from Kubernetes Secrets, so prefer manifest review unless credentials are already available.

## Focused Verification
- Rust checks are per crate: use `cargo test --workspace --locked`,
  `cargo check --workspace --all-targets --locked`, and
  `cargo clippy --workspace --all-targets --locked -- -D warnings` in
  `apps/etcdetcetc`, or `cargo check --manifest-path credhelper/Cargo.toml` for
  `credhelper`.
- Focused suites include `fabric/router/tests/run`,
  `fabric/pki/etcd/tests/run`, `fabric/pki/etcdetcetc/tests/run`,
  `fabric/observer/tests/run`, and `charts/k8s-control-plane/tests/run`. Use
  Docker builds, Helm renders, strict Butane checks, or
  `kubectl kustomize <path>` for the remaining component surface.
- GitHub Actions do not run repo-wide validation; `.github/workflows/images.yaml`
  only builds/pushes the four app images listed above.
