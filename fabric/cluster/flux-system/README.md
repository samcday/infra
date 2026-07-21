# Fabric Flux root

This directory is the self-managing reconciliation root for the fabric
cluster. Flux runs only on nodes labeled
`fabric.samcday.com/platform=true` and tainted
`fabric.samcday.com/platform=true:NoSchedule`; it must never be made schedulable
on the three consensus roots.

`components/gotk-components.yaml` is the canonical four-controller export from
Flux `v2.9.2` (source, kustomize, helm, and notification controllers). Its
SHA-256 is:

```text
ed307189fd1f9e49819a50843bb6f3c9257fe6d4d8359d1950b38207c26c3854
```

Regenerate it only from a checksum-verified Flux `v2.9.2` binary:

```bash
flux install --version=v2.9.2 \
  --components=source-controller,kustomize-controller,helm-controller,notification-controller \
  --export >components/gotk-components.yaml
```

The adjacent Kustomization preserves those release tags but binds all four
controller images to reviewed registry digests. It also uses guarded JSON
patches to replace the export's default `cluster.local` controller addresses
with fabric's `cluster.fabric.internal` domain while preserving this canonical
file and checksum. After a deliberate Flux upgrade, resolve each official tag
with `skopeo inspect`, update the digest overrides, and verify the rendered
images remain `tag@sha256`; do not deploy a tag-only controller image. A failed
domain-patch `test` means the new export's arguments must be reviewed rather
than patched by position blindly.

The repository is public, so the `infra` source deliberately uses HTTPS and no
long-lived GitHub credential. Runtime SOPS material is decrypted with the
cluster-local `flux-system/age-key` Secret; Kubernetes secret encryption is
enabled on the fabric K3s servers.

The first reconciliation still needs reviewed service-node public egress for
the Git source, the official metrics-server chart, and the four digest-pinned
controller images. The service-router policy must reject RFC1918 and Tailnet
destinations before allowing only `10.66.1.10/32` and `10.66.1.11/32` to the
public WAN. Do not bootstrap Flux while that routed egress path is merely an
allocation on paper.

The attended one-time handoff is `scripts/bootstrap-fabric-flux`. It requires
both service nodes to be Ready with the exact platform label/taint, applies the
root-placement guard and CoreDNS first, installs the pinned controllers, seeds
the SOPS identity from tmpfs, and then lets the pushed `main` commit reconcile
this root and its children.

The root deliberately uses `prune: false`, and the root-placement policies and
bindings additionally disable Flux pruning. Removing YAML from Git therefore
does not decommission those objects. Retire a child by first suspending it,
reviewing its inventory, and explicitly deleting it in an attended operation;
retire or replace a safety binding only after an admission dry-run proves the
replacement. A kube-vip upgrade is a two-commit ceremony: admit both the old
and proposed exact Pod identities, roll and prove the DaemonSet, then remove
the old identity. Never change the DaemonSet image/spec first.
