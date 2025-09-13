#!/bin/bash
set -uexo pipefail

cd "$(dirname "$0")"

# This should only be used when petting the first node of a new cluster.
workdir=$(mktemp -d)

configmerge=

profiles="base control-plane etcd install"

mkdir -p $workdir/common
for f in ../../common/butane/*.yaml; do
  name=$(basename $f)
  [[ "$name" == "kustomization.yaml" ]] && continue
  podman run --rm -i quay.io/coreos/butane:release > "$workdir/common/${name//.yaml}.ign" < $f
done

for f in $profiles; do
  sops -d "$f.yaml" | podman run -v "$workdir":/files --rm -i quay.io/coreos/butane:release -d /files > "$workdir/$f.ign"
  configmerge="$configmerge
    - local: $f.ign"
done

podman run -v "$workdir":/files --rm -i quay.io/coreos/butane:release -d /files <<HERE
variant: fcos
version: 1.5.0
ignition:
  config:
    merge: $configmerge
systemd:
  units:
    - name: etcd.service
      dropins:
        - name: newcluster.conf
          contents: |
            [Service]
            Environment=CLUSTER_STATE=new
HERE
