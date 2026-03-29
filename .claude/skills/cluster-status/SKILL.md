# Cluster Status

Run a comprehensive health check across the multi-cluster infrastructure. Report a concise summary, not raw command output.

## Cluster Access

Use the `ik` wrapper (at `scripts/ik` in this repo, or `/var/home/sam/src/infra/scripts/ik`):
```bash
ik --context=hub <command>
ik --context=cloud <command>
```

Contexts:
- **hub** — Management cluster (10.0.1.254). Runs Flux CD, cert-manager, monitoring, storage, and child cluster control planes.
- **cloud** — Hetzner Cloud child cluster (100.64.0.3 via Tailscale). Runs app workloads (fastboop, rokkitpokkit, fastboopmos, gibblox, wasmcloud).

## Checks

### 1. Hub Nodes and Core Services
```bash
ik --context=hub get nodes -o wide
ik --context=hub get pods -n kube-system --no-headers | grep -v Running
ik --context=hub get pods -n flux-system --no-headers
```

### 2. Flux Reconciliation (Hub)
```bash
ik --context=hub get kustomizations -A --no-headers
ik --context=hub get helmreleases -A --no-headers | grep -v "True"
```

### 3. Cloud Cluster Nodes and Unhealthy Pods
```bash
ik --context=cloud get nodes -o wide
ik --context=cloud get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded --no-headers
```

### 4. App Namespace Summary
```bash
for ns in fastboop fastboopmos rokkitpokkit gibblox wasmcloud; do
  echo "--- $ns ---"
  ik --context=cloud get pods -n $ns --no-headers 2>/dev/null || echo "(empty)"
done
```

### 5. Recent Warnings
```bash
ik --context=hub get events -A --field-selector=type=Warning --sort-by=.lastTimestamp 2>/dev/null | tail -10
ik --context=cloud get events -A --field-selector=type=Warning --sort-by=.lastTimestamp 2>/dev/null | tail -10
```

## Output

Summarize by cluster:
- Node status (count, Ready/NotReady)
- Flux: total kustomizations/helmreleases, any not Ready
- Unhealthy pods (if any)
- Per-app-namespace pod count
- Notable warnings
- Overall verdict: healthy / degraded / unhealthy
