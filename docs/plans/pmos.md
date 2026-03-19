# pmos.samcday.com plan

## Goal

Replace the old GitHub-hosted override APK flow with a self-hosted repository at `https://pmos.samcday.com` that works for:

- `apk`
- `pmbootstrap`
- `build.postmarketos.org` workflows

This plan is for a follow-up session to implement end-to-end infra and publishing.

## Context observed in this repo

- Cluster state is Flux-managed under `hub/cluster/*`.
- `hub/cluster/cloud-cluster/` already includes `external-dns` and cert-manager-adjacent wiring patterns.
- DNS/TLS patterns already exist for other `*.samcday.com` services via Ingress + `cert-manager` + `external-dns`.
- We can follow the existing model rather than inventing a separate provisioning path.

## Target repository contract

`https://pmos.samcday.com` should expose a stable, APK-compatible layout:

- `aarch64/APKINDEX.tar.gz`
- `aarch64/*.apk`
- `master/aarch64/APKINDEX.tar.gz` (compat path for `edge` consumers)
- `master/aarch64/*.apk`
- `pmos.samcday.com.rsa.pub` (stable public key URL)

Optional:

- `noarch/` and `master/noarch/` if/when needed.

## Implementation plan (next session)

1. Choose hosting primitive in-cluster
   - Deploy a small static file service (nginx/caddy) with persistent storage.
   - Put manifests under a dedicated path (for example `hub/cluster/cloud-cluster/pmos-repo/`).
   - Add it to cloud-cluster Flux reconciliation via existing kustomization patterns.

2. Wire DNS and TLS
   - Add Ingress for `pmos.samcday.com`.
   - Use existing cert-manager issuer conventions for TLS.
   - Use external-dns annotations so DNS records stay declarative.

3. Define publish mechanism
   - Publish built APK payloads into a staging directory, then atomically promote.
   - Regenerate `APKINDEX.tar.gz` and sign it every publish.
   - Ensure both `aarch64/` and `master/aarch64/` paths are populated.

4. Signing key management
   - Keep one stable repo signing key for this endpoint.
   - Publish only the public key as `pmos.samcday.com.rsa.pub`.
   - Store private key material with existing secret handling conventions (SOPS/K8s secret flow), or keep signing out-of-cluster and only upload outputs.

5. CI integration follow-up
   - Update/confirm pmaports publish job target to `https://pmos.samcday.com`.
   - Keep/extend smoke checks to verify:
     - key URL reachable,
     - `APKINDEX.tar.gz` reachable,
     - key trusts index and at least one APK.

6. Consumer validation
   - Run one `pmbootstrap install --split` canary (`oneplus-fajita`).
   - Confirm `.../etc/apk/repositories` puts `_custom` before fallback mirror.
   - Confirm installed target package versions match hosted override index.

## Acceptance criteria

- `https://pmos.samcday.com` serves valid APK repo metadata over HTTPS.
- `build.postmarketos.org` can consume the repo without GitHub tarball/mirror shims.
- `pmbootstrap` accepts the hosted public key and installs override packages.
- CI smoke check passes on push with no references to the old GitHub APK repo.

## Risks / gotchas

- Non-atomic publish can leave index/package mismatch during update windows.
- Key rotation without coordinated bootstrap will break installs.
- Missing `master/aarch64` compatibility path can break `edge` consumers.
