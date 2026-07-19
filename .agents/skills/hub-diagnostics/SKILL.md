---
name: hub-diagnostics
description: Perform compact, read-only diagnostics against this repository's hub, cloud, and fabric Kubernetes clusters. Use for Kubernetes inspection, Flux GitRepository, Kustomization, or HelmRelease failures, pod health, events, logs, Cilium, kube-vip, metrics-server, or etcd issues. Prefer the project's allowlisted Kubernetes MCP tools and use scripts/ik only for bounded reads the MCP cannot express.
---

# Infrastructure Cluster Diagnostics

Gather the smallest useful set of live-cluster evidence without changing cluster state.

## Select the context explicitly

Require `hub`, `cloud`, or `fabric` for every cluster read. Never rely on the kubeconfig current context or an MCP default.

- Use `hub` for the management cluster and its Flux objects.
- Use `cloud` for child-cluster workloads.
- Use `fabric` for the root-consensus cluster, its service nodes, and its own Flux objects.
- Treat other configured contexts, including `edge-au-east`, as outside this skill's scope.
- For an app, inspect its Flux objects on `hub` before inspecting the workload on `cloud`. Workload fan-out objects usually live in `cloud-cluster`; shared sources such as `GitRepository/infra` can live in `flux-system`. Follow the manifest's namespace.
- For fabric platform services, inspect Flux objects and workloads on `fabric`; do not route them through `hub`.
- Ask for the context when the request and repository layout do not make it unambiguous.
- Label every finding with its context and namespace.

## Keep diagnosis read-only

Prefer the project Kubernetes MCP and only these allowlisted tools:

- discovery and status: `configuration_contexts_list`, `namespaces_list`, `events_list`;
- pods: `pods_list_in_namespace`, `pods_get`, `pods_top`, `pods_log`;
- nodes: `nodes_top`, `nodes_stats_summary`;
- other objects: `resources_list`, `resources_get`.

Never invoke or request pod exec, run, delete, copy, attach, or port-forward; resource create, update, patch, delete, or scale; Helm mutation; or an arbitrary command. Never circumvent the deliberate exclusion of `configuration_view` and `nodes_log`.

Do not invoke MCP prompts such as `cluster-health-check`; the server's bundled prompt is not context-explicit or result-bounded. Use only the allowlisted tools above.

Do not use generic resource reads for `Secret` objects, credential-bearing resources, or secret values. Do not turn a Cilium or etcd investigation into an exec-based command. If status, probes, events, and logs cannot establish endpoint health, report the capability gap.

When a diagnosis leads to a fix, keep the live-cluster work read-only. Apply an authorized fix through repository manifests and Flux rather than direct cluster mutation.

## Bound and project results

Start with the narrowest query:

1. Set `context` and namespace.
2. Add a name, label selector, or field selector.
3. List only the necessary kind.
4. Fetch one complete object only when compact status is insufficient.
5. Return at most 20 rows unless the user requests more.

Project results instead of reproducing full YAML or JSON:

- Pods: name, phase, Ready state, restarts, waiting or termination reason, node, and age.
- Controllers: desired, updated, ready, and available replicas.
- Flux objects: kind/name, Ready condition, reason, shortened message, observed generation, revision, and suspension.
- Events: time, type, reason, involved object, count, and shortened message.
- Nodes: Ready and pressure conditions, schedulability, and relevant usage.

For logs, specify context, namespace, pod, and container when known. Use a small positive tail; default to `tail=80`, never `tail=0`. Use `previous=true` only when a container restarted. The generic MCP has no time or byte cap, so do not treat a line tail as a hard size guarantee.

Scope events to one namespace and report only the newest 20 relevant entries, preferring warnings. The MCP has no event time window or item limit; use the fallback below when that limitation would make the result excessive.

For `pods_top`, always set `all_namespaces=false` and an explicit namespace. Use `nodes_stats_summary` only for one named node and only when conditions and ordinary metrics are insufficient. Avoid cluster-wide Cilium endpoints, pod lists, Flux objects, and object dumps.

## Diagnose pod health

1. List pods in the explicit namespace, using the workload label when available.
2. Identify non-Ready containers, pending or failed pods, restart growth, and scheduling failures.
3. Fetch only an affected pod when conditions or container state need detail.
4. Inspect recent namespace events related to that pod or its controller.
5. Read a short current log tail; read previous logs only when restart state supports it.
6. Inspect the owning Deployment, StatefulSet, DaemonSet, or Job only when pod evidence points upward.

Separate observed facts from inferred causes.

## Diagnose Flux

On the owning context (`hub` or `fabric`), inspect the smallest relevant set of:

- `source.toolkit.fluxcd.io/v1` `GitRepository`;
- `kustomize.toolkit.fluxcd.io/v1` `Kustomization`;
- `helm.toolkit.fluxcd.io/v2` `HelmRelease`.

Check Ready conditions, reasons and messages, observed generation, applied or attempted revision, suspension, dependencies, and source readiness. Inspect Flux controller pods or short logs in `flux-system` only when object status does not explain the failure.

For child-cluster apps, finish the relevant `hub` reconciliation check before switching explicitly to `cloud` and the app namespace. Resolve each object's namespace from its manifest rather than assuming all Flux objects live in `cloud-cluster`.

For the fabric root, inspect `GitRepository/infra` and the relevant Kustomizations in `fabric` namespace `flux-system`, then inspect the resulting platform workload in its manifest namespace. Prove both Ready/current generation and the expected Git revision when a specific rollout is under diagnosis.

## Diagnose Cilium

Use the requested context and namespace `kube-system`.

1. Inspect the `cilium` DaemonSet, `cilium-operator` Deployment, and only relevant pods.
2. Compare desired and ready agents with the relevant node count.
3. Inspect warning events and a short log tail from only the affected agent or operator.
4. For load-balancer or routing issues, inspect the named Service and only relevant Cilium CRs such as `CiliumLoadBalancerIPPool`, `CiliumL2AnnouncementPolicy`, or `CiliumBGPPeeringPolicy`.
5. Do not enumerate all `CiliumEndpoint` objects by default.

For cloud Cilium reconciliation, also inspect the corresponding HelmRelease on `hub` in namespace `cloud-cluster`. Do not assume Cilium exists on `fabric`; establish the installed CNI from repository or bounded object evidence first.

## Diagnose kube-vip

Kube-vip can run on `hub` or `fabric`. Use the context named by the request or repository path and, in `kube-system`, inspect:

- DaemonSet `kube-vip-ds`;
- pods selected by `app.kubernetes.io/name=kube-vip-ds`;
- warning events;
- a short log tail from container `kube-vip`.

Compare scheduled and ready pods with control-plane nodes. Do not silently transpose this workflow to `cloud`.

On `fabric`, also verify kube-vip pods remain confined to nodes labeled `fabric.samcday.com/root-consensus=true`; report any service-node placement as a policy violation.

## Diagnose fabric platform services

On `fabric`, keep the root-consensus boundary explicit:

1. Inspect the named Deployment, DaemonSet, or HelmRelease and only its pods.
2. Confirm ordinary platform pods run only on nodes labeled `fabric.samcday.com/platform=true`, normally `fabric-az1-svc1` and `fabric-az1-svc2`.
3. Report any non-kube-vip pod assigned to a root-consensus node as a safety incident.
4. For metrics-server, inspect its HelmRelease, Deployment, APIService `v1beta1.metrics.k8s.io`, and a bounded `top` read. Do not treat an Available APIService alone as proof that every node has fresh metrics.
5. For CoreDNS, inspect its Deployment, Service, EndpointSlices only when routing is relevant, and pods selected by `k8s-app=kube-dns`.

## Diagnose etcd

For cloud etcd:

1. On `hub` in `cloud-cluster`, inspect Kustomization `etcd` and HelmReleases `etcdetcetc` and `etcdetcetc-cloud-etcd`.
2. On `cloud` in `etcd`, inspect StatefulSet `etcd`, pods selected by `app.kubernetes.io/name=etcd`, its Service, PDB, and `EtcdCluster/etcd` status, warning events, and short logs. Inspect EndpointSlices only when Service routing is relevant.
3. Report the Kubernetes-ready pod count and quorum risk. Never present that count as verified etcd membership or infer endpoint or linearizable-read health from Kubernetes readiness alone.

For hub etcd, inspect the `etcdetcetc` controller and `etcdetcetc.samcday.com/v1alpha1` `EtcdCluster` named `hub-etcd` in namespace `etcdetcetc`. Treat direct member health beyond exposed status, probes, events, and logs as a capability gap; never run `etcdctl` through pod exec.

For fabric etcd, remember that K3s uses three external TLS endpoints co-located on the root hosts; etcd is host-managed, not embedded in K3s and not represented by Pods. Use `fabric` to inspect the three root Node conditions, Leases, relevant `kube-system` events, and exposed API readiness only. Never claim member, endpoint, or linearizable-read health from Kubernetes Node or API readiness. The read-only MCP does not expose host journals or a safe etcd status operation; report that limitation instead of using SSH, host commands, or pod exec under this skill.

## Use shell fallback sparingly

Fall back to `scripts/ik --context=<hub|cloud|fabric>` only when an allowlisted MCP tool cannot express a necessary read, such as a hard time window, server-side result cap, or compact field projection. State the missing MCP capability before using the fallback.

Keep fallback queries read-only and bounded:

- Use only `get`, `describe`, `logs`, or `top`.
- Include the explicit context and namespace.
- Use names, selectors, `--tail`, `--since`, `custom-columns`, or JSONPath.
- Avoid full YAML, broad shell pipelines, cluster-wide results, and every mutation or exec operation.

Do not request blanket approval for `scripts/ik`, `kubectl`, or a shell. A skill does not grant permissions or suppress approval prompts.

## Report the diagnosis

Return the context and namespace inspected, compact evidence, the most likely cause marked as an inference, remaining uncertainty or MCP limitations, and the next safe read or Git-based fix when applicable.
