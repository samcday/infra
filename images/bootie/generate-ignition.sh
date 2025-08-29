#!/bin/bash
set -ueo pipefail

name=$(basename "$REQUEST_URI" | tr '[:upper:]' '[:lower:]' | tr ':' '-')
node=$(kubectl get node -o name "$name" | head -n1)

if [[ -z "$node" ]]; then
  echo "ignition request for unknown node $node" >&2
  echo "Status: 404"
  echo
  exit
fi

echo "content-type: application/json"
echo

merge=""

if kubectl get "$node" -o jsonpath='{.metadata.labels}' | jq -e '. | keys | any(. == "node-role.kubernetes.io/control-plane")' >/dev/null 2>&1; then
  merge+="
      - local: control-plane.ign"
else
  merge+="
      - local: worker.ign"
fi

if kubectl get "$node" -o jsonpath='{.metadata.labels}' | jq -e '. | keys | any(. == "node-role.kubernetes.io/etcd")' >/dev/null 2>&1; then
  merge+="
      - local: etcd.ign"
fi

bootprofiles="$(kubectl get "$node" -o jsonpath='{.metadata.annotations.samcday\.com/boot-profiles}')"
IFS=","; for n in $bootprofiles; do
  merge+="
      - local: $n.ign"
done

if [[ -n "$(kubectl get "$node" -o jsonpath='{.metadata.annotations.samcday\.com/boot-device}')" ]]; then
  # a boot device is specified, include `install.ign`
  merge+="
      - local: install.ign"
fi

butane -d /ignition --strict <<HERE
variant: fcos
version: 1.5.0
ignition:
  config:
    merge:
      - local: base.ign
      $merge
storage:
  files:
    - path: /etc/hostname
      overwrite: true
      contents:
        inline: $name
HERE
