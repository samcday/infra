#!/bin/bash
set -euo pipefail

refuse() {
  printf '%s\n' "$1" >&2
  printf 'Status: %s\n\n' "${2:-409 Conflict}"
  exit
}

for name in BOOTIE_SESSION_NAMESPACE BOOTIE_SESSION_SECRET; do
  [[ -n ${!name:-} ]] || refuse "$name is required"
done
request_path=${REQUEST_URI%%\?*}
node=${request_path#/ignition/}
[[ $request_path == "/ignition/$node" && $node =~ ^fabric-az1-(cp[123]|svc[12])$ ]] ||
  refuse 'the Ignition request path is invalid' '404 Not Found'

request_token=
install=false
IFS='&' read -r -a query_parts <<<"${QUERY_STRING:-}"
for part in "${query_parts[@]}"; do
  case ${part%%=*} in
    token) [[ -z $request_token ]] || refuse 'duplicate Ignition token'; request_token=${part#*=} ;;
    install) [[ ${part#*=} == 1 ]] && install=true ;;
  esac
done
[[ $install == true && -n $request_token ]] || refuse 'an install-mode Ignition token is required' '403 Forbidden'

node_json=$(kubectl get "node/$node" --output=json 2>/dev/null) ||
  refuse 'the replacement Node is absent' '404 Not Found'
secret_json=$(kubectl --namespace "$BOOTIE_SESSION_NAMESPACE" get \
  "secret/$BOOTIE_SESSION_SECRET" --output=json 2>/dev/null) ||
  refuse 'the active Bootie session is absent'
session=$(jq -er '.data["session.json"] | @base64d' <<<"$secret_json" 2>/dev/null) ||
  refuse 'the active Bootie session metadata is malformed'
session_uid=$(jq -r '.metadata.uid // ""' <<<"$secret_json")
node_uid=$(jq -r '.metadata.uid // ""' <<<"$node_json")
node_rv=$(jq -r '.metadata.resourceVersion // ""' <<<"$node_json")
ignition_sha=$(jq -r '.ignitionSha256 // ""' <<<"$session")

jq -e --arg node "$node" --arg node_uid "$node_uid" --arg session_uid "$session_uid" \
  --arg token "$request_token" '
  .metadata.uid == $node_uid and
  .metadata.annotations["fabric.samcday.com/bootie-session-uid"] == $session_uid and
  .metadata.annotations["fabric.samcday.com/bootstrap-state"] == "install-response-issued" and
  .metadata.annotations["samcday.com/ignition-token"] == $token and
  .metadata.annotations["samcday.com/ignition-mode"] == "install"
' <<<"$node_json" >/dev/null || refuse 'the Ignition token is stale or bound to another replacement'
jq -e --arg node "$node" --arg uid "$node_uid" --arg sha "$ignition_sha" '
  .schema == 1 and .purpose == "fabric-node-day-two-session" and
  .node == $node and .nodeUid == $uid and .ignitionSha256 == $sha and
  ($sha | test("^[0-9a-f]{64}$"))
' <<<"$session" >/dev/null || refuse 'the active session does not match the replacement Node'

ignition_b64=$(jq -er --arg key "$node.ign" '.data[$key]' <<<"$secret_json" 2>/dev/null) ||
  refuse 'the active session has no node Ignition'
[[ $(printf '%s' "$ignition_b64" | base64 -d | sha256sum | awk '{print $1}') == "$ignition_sha" ]] ||
  refuse 'the node Ignition no longer matches its session digest'
printf '%s' "$ignition_b64" | base64 -d | jq -e '
  type == "object" and (.ignition.version | test("^3\\.[0-9]+\\.[0-9]+$"))
' >/dev/null || refuse 'the node Ignition is not valid Ignition JSON'

patch=$(jq -cn --arg rv "$node_rv" --arg uid "$node_uid" --arg session_uid "$session_uid" \
  --arg token "$request_token" '[
  {"op":"test","path":"/metadata/resourceVersion","value":$rv},
  {"op":"test","path":"/metadata/uid","value":$uid},
  {"op":"test","path":"/metadata/annotations/fabric.samcday.com~1bootie-session-uid","value":$session_uid},
  {"op":"test","path":"/metadata/annotations/samcday.com~1ignition-token","value":$token},
  {"op":"test","path":"/metadata/annotations/samcday.com~1ignition-mode","value":"install"},
  {"op":"remove","path":"/metadata/annotations/samcday.com~1ignition-token"},
  {"op":"remove","path":"/metadata/annotations/samcday.com~1ignition-mode"},
  {"op":"remove","path":"/metadata/annotations/samcday.com~1boot-device"}
]')
kubectl patch "node/$node" --type=json --patch "$patch" >/dev/null ||
  refuse 'the Ignition token was already consumed or changed'

printf 'content-type: application/json\n\n'
printf '%s' "$ignition_b64" | base64 -d
