# Fabric and lab retirement

The 2026-10-08 repository cleanup retires both clusters' desired configuration,
provisioning and access tooling. It preserves hub, cloud, edge-au-east, the
shared Bootie/controller/chart implementations, and NGINX Gateway Fabric
(an unrelated upstream product).

## Publication has live effects

Publishing this change is a separate operation requiring Sam's authorization.
Hub's Headscale Terraform reconciler uses automatic plan approval. This patch
removes the lab user, its three pre-auth keys, and their three Kubernetes
handoff Secrets. The Headscale ACL patch also removes the retired clusters'
tags, lab access rules, and Fabric subnet auto-approvals.

Before publication, establish whether lab-owned Headscale devices remain:
the provider may refuse to delete a user that still owns devices. Any device
deregistration or other direct mutation needs approval for the specific target
and operation. Do not use a Terraform state edit or force-unlock as a shortcut.
Removing an auto-approval rule does not prove an existing approved route or
device has been removed.

Fabric has its own Flux source. Deleting its source paths from Git is not a
reliable live teardown mechanism: a missing Kustomization path can leave its
previous resources running. Do not claim the cluster is gone from a clean
repository or failed reconciliation.

## Separate live decommissioning

- Confirm the exact hosts, routers, external identities, and stored data to
  retire before any wipe, shutdown, route change, or credential revocation.
- Inventory and retire only the Fabric/lab Tailnet registrations and access
  identities. Preserve all hub/cloud/edge identities and shared credentials.
- Confirm whether any data must be retained before wiping disks or etcd data.
- Local caches, decrypted material, router images, installer media, and backups
  are not removed by this patch. The root `.gitignore` deliberately continues
  ignoring the retired directories so those files cannot accidentally enter Git.
- Keep the former ranges in the [CIDR registry](../network-cidrs.md) reserved
  until live decommissioning is proven.

The repository patch does not establish completion of any live teardown step.
Historical definitions remain in Git history; no history rewrite is required.
