# Plan: Migrate Postgres Clusters to SQLite (+ Litestream/LiteFS)

## Goal

Remove CloudNativePG-managed Postgres from this repo where practical, standardize on SQLite for app state, and add a clear durability/restore story using Litestream (default) or LiteFS (only where multi-replica semantics are required).

## Current Postgres Cluster Usage (Inventory)

### 1) Headscale database (`headscale` namespace)

- **Cluster CR**: `hub/cluster/headscale/postgres.yaml`
  - `Cluster` named `db`, `instances: 3`, `storage: 10Gi`
  - `ScheduledBackup` present
- **Consumer wiring**:
  - `hub/cluster/headscale/config.yaml`: `database.type: postgres`, host `db-rw`
  - `hub/cluster/headscale/helm-release.yaml`: `HEADSCALE_DATABASE_POSTGRES_PASS` from secret `db-app`
- **Important context**:
  - The checked-in Headscale config itself states Postgres is legacy and SQLite is the preferred path.
  - Current `/var/lib/headscale` volume is `emptyDir`, so SQLite migration must include persistent storage first.

### 2) Grafana database (`monitoring` namespace)

- **Cluster CR**: `hub/cluster/monitoring/grafana-postgres.yaml`
  - `Cluster` named `grafana-db-20250904`, `instances: 2`, `storage: 10Gi`
  - `ScheduledBackup` present
- **Consumer wiring**:
  - `hub/cluster/monitoring/grafana.yaml`: `grafana.ini.database.type: postgres`, host `grafana-db-20250904-rw`
  - `GF_DATABASE_USER`/`GF_DATABASE_PASSWORD` from secret `grafana-db-20250904-app`
  - `replicas: 2`
- **Important context**:
  - SQLite is straightforward for single-writer Grafana.
  - Current 2-replica setup implies shared external DB semantics today; moving to SQLite changes HA behavior unless LiteFS is introduced.

### 3) Shared Postgres platform footprint (CNPG)

- `hub/cluster/cnpg-system/cloudnative-pg-operator.yaml` (CNPG Helm release)
- `hub/cluster/cnpg-system/catalogs.yaml` + `hub/cluster/cnpg-system/catalogs/kustomization.yaml`
- `hub/cluster/cnpg-system/vpa.yaml`
- Flux wiring:
  - `hub/cluster/flux-system/kustomizations.yaml` includes `cnpg-system`
  - `hub/cluster/flux-system/helm-repos.yaml` includes `cloudnative-pg`
  - `hub/cluster/flux-system/namespaces.yaml` includes `cnpg-system`

Once Headscale + Grafana no longer depend on Postgres, this entire footprint can be removed.

## Proposed SQLite Architecture Pattern

### Default pattern (recommended): SQLite + Litestream

- App uses local SQLite file on PVC (single writer pod).
- Litestream sidecar continuously replicates WAL snapshots to object storage.
- Init container (or entrypoint wrapper) restores DB from object storage on cold start.
- Best fit: apps that are naturally single-writer (Headscale, likely Grafana if we run one replica).

### Optional pattern: SQLite + LiteFS

- Use when we need multiple app replicas with leader/follower behavior on SQLite.
- Higher operational complexity in Kubernetes (FUSE mount, lease/primary management, failover behavior testing).
- Use only if we must preserve multi-replica app topology and cannot accept single-replica writes.

## Per-Workload Migration Plan

### Headscale (migrate first)

### Target state

- `database.type: sqlite`
- DB at `/var/lib/headscale/db.sqlite` (already present in config template)
- `/var/lib/headscale` backed by PVC, not `emptyDir`
- Litestream sidecar enabled for backup/restore

### Steps

1. **Add durable storage**
   - Replace `emptyDir` data volume in `hub/cluster/headscale/helm-release.yaml` with PVC-backed storage.
2. **Add SQLite replication/restore**
   - Add Litestream sidecar + config, with bucket credentials via SOPS-managed secret.
   - Add startup restore flow before Headscale process starts.
3. **Data migration from Postgres**
   - Perform one-time export/import during maintenance window.
   - Validate migrated data (users/nodes/routes/API behavior).
4. **Switch runtime config**
   - Update `hub/cluster/headscale/config.yaml` to `database.type: sqlite`.
   - Remove Postgres password env wiring from `hub/cluster/headscale/helm-release.yaml`.
5. **Cutover and verify**
   - Restart Headscale, verify control-plane operations and metrics.
6. **Remove Headscale Postgres resources**
   - Drop `hub/cluster/headscale/postgres.yaml` and remove its kustomization reference.

### Notes

- Headscale is the strongest candidate for immediate migration because upstream already optimizes for SQLite.

### Grafana (migrate second)

### Option A (recommended): single-replica Grafana + SQLite + Litestream

- Set `replicas: 1` in `hub/cluster/monitoring/grafana.yaml`.
- Enable Grafana persistence (PVC) and point SQLite DB to persistent path.
- Remove Postgres env vars and DB host config.
- Add Litestream sidecar for DR backups.

### Option B: keep multi-replica via LiteFS

- Introduce LiteFS sidecar/mount and leader election topology.
- Validate Grafana plugin behavior, migrations, and failover semantics under pod restarts.
- Higher implementation and operational complexity than Option A.

### Recommendation

- Start with **Option A** unless 2-replica Grafana is a hard requirement.
- Today’s `replicas: 2` likely provides availability, but this can be traded for simpler operations and lower infra cost if downtime tolerance is acceptable.

### Steps (for Option A)

1. Enable persistent storage for Grafana data.
2. Change DB config from Postgres to SQLite in `grafana.ini`.
3. Remove Postgres secret env references and Postgres cluster resource.
4. Add Litestream backup/restore path.
5. Reduce replicas to 1 and validate dashboards, auth, annotations, alerting state.

## Decommission CNPG Platform (after both migrations)

1. Remove `hub/cluster/cnpg-system` from Flux kustomizations.
2. Remove CNPG Helm repository declaration.
3. Remove `cnpg-system` namespace declaration.
4. Delete `hub/cluster/cnpg-system/*` manifests from repo.
5. Validate no remaining `postgresql.cnpg.io/*` resources or `type: postgres` configs.

## Sequencing and Milestones

1. **Milestone 1: Headscale complete**
   - Headscale on SQLite + Litestream, Postgres cluster removed.
2. **Milestone 2: Grafana complete**
   - Grafana on SQLite path (Option A or B), Postgres cluster removed.
3. **Milestone 3: CNPG removed**
   - Operator/catalogs/repo/namespace wiring deleted.

## Validation Checklist (repo-specific)

- For each touched workload kustomization:
  - `kustomize build --load-restrictor=LoadRestrictionsNone <path> >/dev/null`
- For Helm-managed app value changes:
  - render/lint of affected chart where applicable.
- Repo-wide grep checks before merge:
  - no remaining `postgresql.cnpg.io`
  - no remaining app-level `type: postgres` for migrated workloads
  - no plaintext secrets added for object-store creds

## Risks and Mitigations

- **Data migration correctness risk**
  - Mitigate with dry-run migration in non-prod namespace and scripted verification checks.
- **SQLite durability risk without persistent volume**
  - Mitigate by requiring PVC + Litestream restore path before cutover.
- **Grafana HA behavior change**
  - Mitigate by explicitly choosing Option A (accept single replica) vs Option B (LiteFS complexity).
- **Backup/restore operational drift**
  - Mitigate with periodic restore drill and checksum-based integrity validation.

## Rollback Strategy

- Keep Postgres manifests in git history and migration runbooks.
- During each cutover, keep last known good Postgres backup and do not remove Postgres resources until post-cutover validation passes.
- If rollback is needed, revert app config/env to Postgres and redeploy while preserving SQLite artifacts for forensic comparison.

## Open Decisions (to resolve early)

1. Grafana topology target: `replicas: 1` (Litestream) vs `replicas: 2` (LiteFS).
2. Object storage target and credential model for Litestream backups.
3. RPO/RTO expectations per workload to size backup interval and restore automation.
