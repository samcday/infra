#!/bin/bash
set -uexo pipefail

# This should only be used when petting the first node of a new cluster.
workdir=$(mktemp -d)

configmerge=

profiles="base var etcd control-plane"

if [[ "${TPM2:-1}" -eq "1" ]]; then
  profiles="$profiles tpm"
fi

for f in $profiles; do
  sops -d --input-type=yaml --output-type=yaml "$f.bu" | podman run -v "$(pwd)/files":/files --rm -i quay.io/coreos/butane:release -d /files > "$workdir/$f.ign"
  configmerge="$configmerge
    - local: $f.ign"
done

podman run -v "$workdir":/files --rm -i quay.io/coreos/butane:release -d /files <<HERE
variant: fcos
version: 1.5.0
ignition:
  config:
    merge: $configmerge
HERE
