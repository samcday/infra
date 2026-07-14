#!/bin/bash
set -ueo pipefail

request_path=${REQUEST_URI%%\?*}
name=$(basename "$request_path" | tr '[:upper:]' '[:lower:]' | tr ':' '-')
node=$(kubectl get node -o name "$name" 2>/dev/null || true)

if [[ -z "$node" ]]; then
  echo "ignition request for unknown node $name" >&2
  echo "Status: 404 Not Found"
  echo
  exit
fi

install_request=false
request_token=
IFS='&' read -r -a query_parts <<< "${QUERY_STRING:-}"
for part in "${query_parts[@]}"; do
  key=${part%%=*}
  value=${part#*=}
  case "$key" in
    install)
      [[ "$value" == 1 ]] && install_request=true
      ;;
    token)
      request_token=$value
      ;;
  esac
done

expected_token=$(kubectl get "$node" -o jsonpath='{.metadata.annotations.samcday\.com/ignition-token}')
expected_mode=$(kubectl get "$node" -o jsonpath='{.metadata.annotations.samcday\.com/ignition-mode}')
if [[ -z "$request_token" || "$request_token" != "$expected_token" ]]; then
  echo "missing, stale, or incorrect ignition token for $node" >&2
  echo "Status: 403 Forbidden"
  echo
  exit
fi

request_mode=live
$install_request && request_mode=install
if [[ "$request_mode" != "$expected_mode" ]]; then
  echo "ignition mode does not match the issued token for $node" >&2
  echo "Status: 403 Forbidden"
  echo
  exit
fi

# Consume the token before returning any profile, including discovery. If the
# response is lost, PXE must request a fresh token (and an install must be
# explicitly re-armed).
patch=$(jq -cn --arg token "$request_token" --arg mode "$request_mode" '[
  {"op":"test","path":"/metadata/annotations/samcday.com~1ignition-token","value":$token},
  {"op":"test","path":"/metadata/annotations/samcday.com~1ignition-mode","value":$mode},
  {"op":"remove","path":"/metadata/annotations/samcday.com~1ignition-token"},
  {"op":"remove","path":"/metadata/annotations/samcday.com~1ignition-mode"}
]')
if ! kubectl patch "$node" --type=json --patch "$patch" >/dev/null; then
  echo "ignition token was already consumed for $node" >&2
  echo "Status: 409 Conflict"
  echo
  exit
fi

echo "content-type: application/json"
echo

if [[ "$(kubectl get "$node" -o jsonpath='{.metadata.labels.samcday\.com/discovery}')" == "true" ]]; then
  butane -d /ignition --strict <<HERE
variant: fcos
version: 1.5.0
ignition:
  config:
    merge:
      - local: common/discovery.ign
storage:
  files:
    - path: /etc/hostname
      overwrite: true
      contents:
        inline: ${name/node\/}
HERE
  exit
fi

merge=""

for config in ${EXTRA_BASE_CONFIGS:-}; do
  merge+="
      - local: $config.ign"
done

labels=$(kubectl get "$node" -o jsonpath='{.metadata.labels}')
if jq -e '. | keys | any(. == "node-role.kubernetes.io/control-plane")' <<< "$labels" >/dev/null 2>&1; then
  [[ -f /ignition/control-plane.ign ]] || {
    echo "control-plane profile is unavailable for $node" >&2
    echo "Status: 409 Conflict"
    echo
    exit
  }
  merge+="
      - local: control-plane.ign"
elif jq -e '. | keys | any(. == "node-role.kubernetes.io/worker")' <<< "$labels" >/dev/null 2>&1; then
  [[ -f /ignition/worker.ign ]] || {
    echo "worker profile is unavailable for $node" >&2
    echo "Status: 409 Conflict"
    echo
    exit
  }
  merge+="
      - local: worker.ign"
else
  echo "refusing $node without an explicit control-plane or worker role" >&2
  echo "Status: 409 Conflict"
  echo
  exit
fi

if kubectl get "$node" -o jsonpath='{.metadata.labels}' | jq -e '. | keys | any(. == "node-role.kubernetes.io/etcd")' >/dev/null 2>&1; then
  merge+="
      - local: etcd.ign"
fi

bootprofiles="$(kubectl get "$node" -o jsonpath='{.metadata.annotations.samcday\.com/boot-profiles}')"
IFS=","; for n in $bootprofiles; do
  [[ "$n" =~ ^[a-z0-9][a-z0-9-]*$ && -f "/ignition/$n.ign" ]] || {
    echo "invalid or unavailable boot profile '$n' for $node" >&2
    echo "Status: 409 Conflict"
    echo
    exit
  }
  merge+="
      - local: $n.ign"
done

if $install_request; then
  # boot-request only emits this flag after atomically consuming an explicit
  # install arm and binding this request to the one-use token above.
  merge+="
      - local: install.ign"
fi

butane -d /ignition --strict <<HERE
variant: fcos
version: 1.5.0
ignition:
  config:
    merge:
      - local: base.ign$merge
storage:
  files:
    - path: /etc/hostname
      overwrite: true
      contents:
        inline: $name
HERE
