# Lab control-plane foundation

This directory prepares the Fabric-hosted `lab` control plane without creating
workers or child-cluster add-ons. It provisions the etcdetcetc tenant, the
Tailnet TCP proxy, and an endpoint publisher that writes the proxy's dynamically
assigned CGNAT IPv4 into `lab-control-plane-endpoint`.

Initial activation is deliberately attended and split across two Flux stages:

1. Reconcile the Hub Headscale Terraform until its
   `headscale/lab-apiserver-ts-auth` Secret exists.
2. Run `scripts/handoff-lab-apiserver-auth` from the repository root.
3. Add `apiserver-ts-auth.sops.yaml` to this directory's `kustomization.yaml`.
   Never add a plaintext or placeholder auth key.
4. Unsuspend `fabric-lab-foundation` and wait for its EtcdTenant and both
   Deployments to report Ready. The publisher fails closed unless Headscale
   reports exactly `lab-apiserver.tailnet.hub.samcday.com` and an IPv4 inside
   `100.64.0.0/10`.
5. Only then unsuspend `fabric-lab-control-plane`.

The empty Tailnet state Secret is declarative. The Tailnet pod alone may update
it; the endpoint publisher may only read it and patch the pre-created endpoint
ConfigMap with server-side apply.
