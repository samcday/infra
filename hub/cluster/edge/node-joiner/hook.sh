#!/bin/bash
set -euo pipefail

if [[ "${1:-}" == "--config" ]]; then
  cat <<'YAML'
configVersion: v1
kubernetes:
  - name: edge-nodes
    apiVersion: v1
    kind: Node
    labelSelector:
      matchExpressions:
        - key: blc.samcday.com/asg
          operator: Exists
    executeHookOnEvent: [Added, Modified, Deleted]
    executeHookOnSynchronization: true
    jqFilter: |
      {
        name:  .metadata.name,
        ip:    ((.status.addresses // []) | map(select(.type=="ExternalIP"))[0].address // ""),
        ready: ((.status.conditions // []) | any(.type=="Ready" and .status=="True")),
      }
YAML
  exit 0
fi

NAMESPACE=edge
TEMPLATE=/hooks/job-template.yaml

reconcile() {
  local op=$1 name=$2 ip=$3 ready=$4
  local job="join-$name"

  if [[ -z "$name" ]]; then
    return
  fi

  if [[ "$op" == "Deleted" || "$ready" == "true" ]]; then
    kubectl -n "$NAMESPACE" delete job "$job" --ignore-not-found
    return
  fi

  if [[ -z "$ip" ]]; then
    echo "node $name: no ExternalIP yet, waiting for next event"
    return
  fi

  if kubectl -n "$NAMESPACE" get job "$job" >/dev/null 2>&1; then
    return
  fi

  echo "creating join job for $name ($ip)"
  local template manifest
  template=$(<"$TEMPLATE")
  manifest=${template//\$\{NODE_NAME\}/$name}
  manifest=${manifest//\$\{NODE_IP\}/$ip}
  kubectl -n "$NAMESPACE" create -f - <<<"$manifest"
}

jq -c '.[]' "$BINDING_CONTEXT_PATH" | while read -r row; do
  type=$(jq -r '.type' <<<"$row")
  case "$type" in
    Synchronization)
      jq -c '.objects[]? // empty' <<<"$row" | while read -r obj; do
        reconcile "Synchronization" \
          "$(jq -r '.filterResult.name  // ""'   <<<"$obj")" \
          "$(jq -r '.filterResult.ip    // ""'   <<<"$obj")" \
          "$(jq -r '.filterResult.ready // false' <<<"$obj")"
      done
      ;;
    Event)
      reconcile \
        "$(jq -r '.watchEvent         // ""'    <<<"$row")" \
        "$(jq -r '.filterResult.name  // ""'    <<<"$row")" \
        "$(jq -r '.filterResult.ip    // ""'    <<<"$row")" \
        "$(jq -r '.filterResult.ready // false' <<<"$row")"
      ;;
  esac
done
