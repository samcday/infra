# App Debug

Debug an application's cluster resources. Pass the app name as argument (e.g., `/app-debug fastboop`).

## Reconciliation Architecture

App manifests live in each app's GitHub repo at `infra/k8s/`. Flux resources in the **hub** cluster's `cloud-cluster` namespace (GitRepository + Kustomization) watch these repos and apply manifests to the **cloud** cluster. The chain is:

```
push to app repo main
  -> Flux GitRepository (hub, ns: cloud-cluster) detects new revision
    -> Flux Kustomization applies infra/k8s/ to cloud cluster
      -> workloads reconcile in cloud cluster namespace
```

## Cluster Access

```bash
ik --context=hub <command>    # Flux control plane, GitRepositories, Kustomizations
ik --context=cloud <command>  # App workloads, pods, services, routes
```

(`ik` is at `scripts/ik` in this repo or `/var/home/sam/src/infra/scripts/ik`)

## App-to-Namespace Map

| App | Cloud Namespace | Infra Repo Path |
|-----|----------------|-----------------|
| fastboop | fastboop | hub/cluster/cloud-cluster/fastboop.yaml |
| fastboopmos | fastboopmos | hub/cluster/cloud-cluster/fastboopmos/ |
| rokkitpokkit | rokkitpokkit | hub/cluster/cloud-cluster/rokkitpokkit/ |
| gibblox | gibblox | hub/cluster/cloud-cluster/gibblox.yaml |

Note: This table is a convenience reference and may drift. Always also check `hub/cluster/cloud-cluster/` directly for the authoritative current app list.

## Checks (substitute $APP with the argument)

### 1. Flux Source and Reconciliation (Hub)
```bash
ik --context=hub get gitrepository $APP -n cloud-cluster -o wide
ik --context=hub get kustomization $APP -n cloud-cluster -o wide
```

### 2. All Resources in Namespace (Cloud)
```bash
ik --context=cloud get all -n $APP
ik --context=cloud get httproutes,certificates,configmaps,secrets -n $APP
```

### 3. Pod Details (if anything unhealthy)
```bash
ik --context=cloud get pods -n $APP -o wide
ik --context=cloud describe pod -n $APP <unhealthy-pod>
ik --context=cloud logs -n $APP --tail=50 <unhealthy-pod>
```

### 4. Events
```bash
ik --context=cloud get events -n $APP --sort-by=.lastTimestamp | tail -20
```

### 5. Corresponding Infra Repo Manifests
Read the Flux resources in this repo that control the app:
- Check `hub/cluster/cloud-cluster/` for the app's GitRepository + Kustomization YAML
- Check if the Kustomization has `targetNamespace`, `decryption`, or `dependsOn` set

## Output

Concise health report:
- Flux source: last revision, synced/stale/failed
- Flux kustomization: ready/reconciling/failed, last applied revision
- Pods: status, restarts, age
- Services and HTTPRoutes: endpoints, hostnames
- Any recent events worth noting
- If unhealthy: likely root cause and suggested next step
