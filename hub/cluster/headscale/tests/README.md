# Services-subnet router checks

From the repository root (Python 3, PyYAML, and `kubectl` required):

```sh
python3 -B hub/cluster/headscale/tests/test_services_subnet_router.py
```

This renders the actual Headscale Kustomization and checks the advertised prefix,
route acceptance, narrowly scoped auto-approval, enrollment/Secret/RBAC wiring,
persistent per-replica identities, placement, and container isolation.

Validate provider references without connecting to the cluster or applying:

```sh
tofu -chdir=hub/cluster/headscale init -backend=false -lockfile=readonly -input=false
tofu -chdir=hub/cluster/headscale fmt -check -diff
tofu -chdir=hub/cluster/headscale validate
```

Validate policy using the deployed Headscale release, offline:

```sh
podman run --rm --network none \
  -v "$PWD/hub/cluster/headscale:/etc/headscale:ro,z" \
  ghcr.io/juanfont/headscale:v0.28.0 policy check --file /etc/headscale/acls.json
```

These checks do **not** prove live routing or BGP/Gateway health. After an
approved rollout, follow [the runbook](../../../../docs/runbooks/services-subnet-router.md).
