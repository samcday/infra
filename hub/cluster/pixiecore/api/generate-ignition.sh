#!/bin/bash
set -ueo pipefail

mac_addr=$(basename "$REQUEST_URI" | tr '[:upper:]' '[:lower:]' | tr ':' '-')
node=$(kubectl get node -o name -l "samcday.com/mac=$mac_addr" | head -n1)

if [[ -z "$node" ]]; then
  echo "ignition request for unknown mac $mac_addr" >&2
  echo "Status: 404"
  echo
  exit
fi

bootprofiles="$(kubectl get "$node" -o jsonpath='{.metadata.annotations.samcday\.com/boot-profiles}')"

if [[ -z "$bootprofiles" ]]; then
  echo "ignition request for $node which is missing boot-profile annotations" >&2
  echo "Status: 404"
  echo
  exit
fi

echo "content-type: application/json"
echo

if kubectl get "$node" -o jsonpath='{.metadata.labels}' | jq -e '. | keys | any(. == "node-role.kubernetes.io/control-plane")' >/dev/null 2>&1; then
  merge+="
      - local: control-plane.ign"
fi

if kubectl get "$node" -o jsonpath='{.metadata.labels}' | jq -e '. | keys | any(. == "node-role.kubernetes.io/etcd")' >/dev/null 2>&1; then
  merge+="
      - local: etcd.ign"
fi

IFS=","; for n in $bootprofiles; do
  merge+="
      - local: $n.ign"
done

butane -d /ignition --strict <<HERE
variant: fcos
version: 1.5.0
ignition:
  config:
    merge:
      - local: base.ign
      $merge
HERE
