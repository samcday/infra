# KubeVirt operator v1.8.4 provenance

`kubevirt-operator.yaml` is the unmodified upstream GitHub release asset.

- Release: <https://github.com/kubevirt/kubevirt/releases/tag/v1.8.4>
- Asset: <https://github.com/kubevirt/kubevirt/releases/download/v1.8.4/kubevirt-operator.yaml>
- GitHub asset ID: `449378490`
- Annotated tag object: `220c77fe00d71f9cef9d91ccde74669b3652ae3a`
- Tagged commit: `dfaa398f086692d19e01be21d83a01036e07815f`
- Size: `490554` bytes
- SHA-256: `d1d8264eec5b802c122bec6c54d8c3b11e119ee2a5c75602aaa8b53ea3857eda`

The release asset is vendored because the installed Flux version cannot bind a
remote Kustomize resource URL to GitHub's published digest. Keep the YAML
byte-for-byte identical to upstream; apply any local changes as Kustomize
patches. Verify replacements with:

```sh
sha256sum -c kubevirt-operator.yaml.sha256
```
