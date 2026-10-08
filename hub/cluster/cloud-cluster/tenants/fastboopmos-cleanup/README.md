# Temporary fastboopmos B2 cleanup

**Inventory only:** the initial Job performs no mutations. The Cloud fastboopmos
namespace and reconcilers have been removed, and its archived GitHub repository's
Actions secrets/variables cleared. B2 configuration remains until cleanup passes.
This is only for `samcday-fastboopmos`: never
fastboop proper, rokkitpokkit, shared infrastructure or the archived GitHub repo.

The existing `fastboopmos-tofu-vars` Secret in the Hub `cloud-cluster` namespace
supplies three environment variables through `secretKeyRef`. Do not copy,
decrypt or log it. No new key, RBAC or service-account token is needed. The Pod
does not call Kubernetes. It uses only Python's standard library and B2 Native
API v2, verifies TLS, rejects redirects, ignores proxy environment settings, and
accepts only `https://api.backblazeb2.com` / `https://api<digits>.backblazeb2.com`
origins (optional port 443). Unexpected API host formats fail closed.

## Attended cleanup sequence

1. Finish writer/reconciler gates and review the dormant render. Enable this
   directory in the parent tenants resource list **only after those gates**.
   The shipped Job is `fastboopmos-b2-inventory`, mode `inventory`, with no
   expected IDs. It makes **no mutation calls**.
2. Require a successful `inventory_complete` log. Independently check its
   `account_id` and `bucket_id` belong to the intended account and bucket.
   Inventory lists **every version**, including `upload`, `hide` and `start`,
   with both version cursor fields, and separately lists every unfinished
   large upload. Counts may overlap: a start record and an unfinished upload
   can describe the same upload. `version_bytes` is metadata content length,
   not a measurement of uploaded unfinished parts.
3. In a separate, explicit Git change, set `B2_CLEANUP_MODE` to `purge`, fill
   **both** `B2_EXPECTED_ACCOUNT_ID` and `B2_EXPECTED_BUCKET_ID` with those checked
   IDs, and change the Job name to `fastboopmos-b2-purge`. Jobs have immutable
   Pod templates: do not patch the existing inventory Job or enable Flux force.
   Purge freshly authorizes, rechecks bucket-only / no-prefix credentials,
   required capabilities, exact `listBuckets(bucketName=...)` agreement and
   both pinned IDs, then completes another inventory before its first mutation.
4. Require successful Job completion **and** `purge_verified_empty`. Purge
   deletes every completed version and hide marker and cancels unfinished
   uploads (including their parts). It never sets `bypassGovernance`, changes
   retention / legal holds, deletes a bucket, or changes a key. Its final full
   scan must find zero version entries **and** zero unfinished uploads.
5. **Only then**, remove `hub/tofu/fastboopmos.tf` so the existing
   Terraform controller can remove this Secret, application key and now-empty
   bucket. The pinned B2 provider `0.10.0` cannot purge a nonempty bucket; do not
   invent `force_destroy`. Remove this temporary Job/directory from GitOps as
   well. Preserve all protected tenants/shared resources and the archived repo.

The script has an 840-second monotonic deadline, 30-second bounded socket
timeouts, bounded response/page sizes, and at most four attempts per operation
for HTTP 429 / 5xx only. The Job has `backoffLimit: 0` and a 900-second hard
deadline. Numeric Retry-After is bounded to 30 seconds. Transport errors,
permission failures, retention/hold failures and uncertain mutations stop
immediately. Structured logs contain only IDs, aggregate counts/bytes, fixed
events and allowlisted error codes, never filenames, file contents, keys,
tokens, headers or raw server errors. A `stopped` event is **not completion**;
progress counts cover confirmed operations, while request counts also include
an operation that may have failed or had an unknown outcome. Do not assume a
failed request made no change. Review and rerun inventory before any explicitly
authorized retry under a new Job name.

No TTL or auto-recreation annotation is set. Do not manually delete a Job while
its manifest remains managed, because reconciliation could recreate it.
Script/config changes also change the generated ConfigMap hash and therefore
the immutable Pod template; an intentional rerun needs a new Job name. Without
writer gates, final empty scans are not a durable guarantee against new writes.

## Offline verification

From the repository root:

```sh
python3 -B -m unittest discover -s hub/cluster/cloud-cluster/tenants/fastboopmos-cleanup/tests -v
kubectl kustomize hub/cluster/cloud-cluster/tenants/fastboopmos-cleanup
```

Tests mock all HTTP and forbid real sockets. Rendering is local, not a cluster
operation. The official Python image index was resolved read-only with
`crane digest docker.io/library/python:3.13.12-slim-bookworm` to
`sha256:a58daefb915e1e03ad48f3ca4df8832065412c5c35cacb9d39f4229184de12b6`.
