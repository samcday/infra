#!/bin/bash
set -uexo pipefail

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

butane_dir="$SCRIPT_DIR/../../../ignition"

for f in "$butane_dir"/*.bu; do
    name=$(basename "$f")
    ignition_file="$SCRIPT_DIR/${name/.bu}.json"
    sops -d --input-type=yaml --output-type=yaml "$f" \
        | podman run --rm -i quay.io/coreos/butane:release \
        > "$ignition_file"

    sops --encrypt  --in-place "$ignition_file"
done
