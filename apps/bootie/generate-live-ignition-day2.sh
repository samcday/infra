#!/bin/bash
set -euo pipefail

umask 077

refuse() {
  printf '%s\n' "$1" >&2
  printf 'Status: %s\ncontent-type: text/plain\n\n' "${2:-409 Conflict}"
  exit
}

for name in BOOTIE_IMAGE_DIGEST BOOTIE_IMAGE_REVISION BOOTIE_SESSION_NAMESPACE \
  BOOTIE_SESSION_SECRET; do
  [[ -n ${!name:-} ]] || refuse "$name is required"
done
[[ $BOOTIE_IMAGE_DIGEST =~ ^sha256:[0-9a-f]{64}$ ]] ||
  refuse 'BOOTIE_IMAGE_DIGEST is malformed'
[[ $BOOTIE_IMAGE_REVISION =~ ^[0-9a-f]{40}$ ]] ||
  refuse 'BOOTIE_IMAGE_REVISION is malformed'
[[ ${REQUEST_METHOD:-GET} == GET ]] || refuse 'the live Ignition endpoint is GET-only' '405 Method Not Allowed'

request_path=${REQUEST_URI%%\?*}
node=${request_path#/live-ignition/}
[[ $request_path == "/live-ignition/$node" && $node =~ ^fabric-az1-(cp[123]|svc[12])$ ]] ||
  refuse 'the live Ignition request path is invalid' '404 Not Found'

request_token=
IFS='&' read -r -a query_parts <<<"${QUERY_STRING:-}"
for part in "${query_parts[@]}"; do
  case ${part%%=*} in
    token)
      [[ -z $request_token ]] || refuse 'duplicate live Ignition token'
      request_token=${part#*=}
      ;;
    '') ;;
    *) refuse 'the live Ignition query is invalid' '403 Forbidden' ;;
  esac
done
[[ $request_token =~ ^[0-9a-f]{32}$ ]] ||
  refuse 'a valid live Ignition token is required' '403 Forbidden'

node_json=$(kubectl get "node/$node" --output=json 2>/dev/null) ||
  refuse 'the replacement Node is absent' '404 Not Found'
secret_json=$(kubectl --namespace "$BOOTIE_SESSION_NAMESPACE" get \
  "secret/$BOOTIE_SESSION_SECRET" --output=json 2>/dev/null) ||
  refuse 'the active Bootie session is absent'
session=$(jq -er '.data["session.json"] | @base64d' <<<"$secret_json" 2>/dev/null) ||
  refuse 'the active Bootie session metadata is malformed'

session_uid=$(jq -r '.metadata.uid // ""' <<<"$secret_json")
node_uid=$(jq -r '.metadata.uid // ""' <<<"$node_json")
role=$(jq -r '.role // ""' <<<"$session")
address=$(jq -r '.address // ""' <<<"$session")
mac=$(jq -r '.mac // ""' <<<"$session")
disk=$(jq -r '.bootDevice // ""' <<<"$session")
ignition_sha=$(jq -r '.ignitionSha256 // ""' <<<"$session")
policy_sha=$(jq -r '.installPolicySha256 // ""' <<<"$session")
repo_commit=$(jq -r '.repoCommit // ""' <<<"$session")
lock_uid=$(jq -r '.lockUid // ""' <<<"$session")

jq -e --arg node "$node" --arg session_uid "$session_uid" \
  --arg namespace "$BOOTIE_SESSION_NAMESPACE" --arg secret "$BOOTIE_SESSION_SECRET" '
  .metadata.name == $secret and .metadata.namespace == $namespace and
  .metadata.uid == $session_uid and .immutable == true and .type == "Opaque" and
  .metadata.labels["fabric.samcday.com/purpose"] == "node-day-two" and
  .metadata.labels["fabric.samcday.com/node"] == $node and
  (.data | keys | sort) ==
    (["session.json", "install-policy", ($node + ".ign")] | sort) and
  ($session_uid | test("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"))
' <<<"$secret_json" >/dev/null || refuse 'the active Bootie session Secret is not exact and immutable'

jq -e --arg node "$node" --arg node_uid "$node_uid" --arg session_uid "$session_uid" \
  --arg token "$request_token" --arg disk "$disk" --arg repo "$repo_commit" \
  --arg ignition_sha "$ignition_sha" --arg image_digest "$BOOTIE_IMAGE_DIGEST" \
  --arg image_revision "$BOOTIE_IMAGE_REVISION" --arg role "$role" '
  .metadata.uid == $node_uid and .metadata.deletionTimestamp == null and
  .metadata.labels["fabric.samcday.com/bootstrap-placeholder"] == "true" and
  .metadata.labels["samcday.com/discovery"] == "false" and
  .metadata.annotations["fabric.samcday.com/bootie-session-uid"] == $session_uid and
  .metadata.annotations["fabric.samcday.com/bootstrap-state"] == "install-response-issued" and
  .metadata.annotations["fabric.samcday.com/config-revision"] == $repo and
  .metadata.annotations["fabric.samcday.com/ignition-sha256"] == $ignition_sha and
  .metadata.annotations["fabric.samcday.com/bootie-image-digest"] == $image_digest and
  .metadata.annotations["fabric.samcday.com/bootie-image-revision"] == $image_revision and
  .metadata.annotations["samcday.com/ignition-token"] == $token and
  .metadata.annotations["samcday.com/ignition-mode"] == "install" and
  .metadata.annotations["samcday.com/boot-device"] == $disk and
  ((.metadata.annotations["samcday.com/install"] // "") == "") and
  ((.status.nodeInfo.machineID // "") == "") and
  (if $role == "control-plane" then
    .metadata.labels["fabric.samcday.com/root-consensus"] == "true"
   else
    ([.spec.taints[]? | select(.key == "fabric.samcday.com/platform" and
      .value == "true" and .effect == "NoSchedule")] | length) == 1
   end)
' <<<"$node_json" >/dev/null ||
  refuse 'the live Ignition token is stale or bound to another replacement'
jq -e --arg node "$node" --arg uid "$node_uid" --arg session_uid "$session_uid" \
  --arg role "$role" --arg address "$address" --arg mac "$mac" --arg disk "$disk" \
  --arg sha "$ignition_sha" --arg policy_sha "$policy_sha" --arg repo "$repo_commit" \
  --arg image_digest "$BOOTIE_IMAGE_DIGEST" --arg image_revision "$BOOTIE_IMAGE_REVISION" \
  --arg lock_uid "$lock_uid" '
  .schema == 1 and .purpose == "fabric-node-day-two-session" and
  .node == $node and .nodeUid == $uid and .role == $role and
  .address == $address and .mac == $mac and .bootDevice == $disk and
  .ignitionSha256 == $sha and .installPolicySha256 == $policy_sha and
  .repoCommit == $repo and .bootieImageDigest == $image_digest and
  .bootieImageRevision == $image_revision and .lockUid == $lock_uid and
  ($role == "control-plane" or $role == "service") and
  (if $role == "control-plane" then
     ($node | test("^fabric-az1-cp[123]$")) and
     ($address | test("^10\\.66\\.0\\.[0-9]{1,3}$"))
   else
     ($node | test("^fabric-az1-svc[12]$")) and
     ($address | test("^10\\.66\\.1\\.[0-9]{1,3}$"))
   end) and
  ($mac | test("^([0-9a-f]{2}:){5}[0-9a-f]{2}$")) and
  ($disk | startswith("/dev/disk/by-id/")) and
  ($uid | test("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")) and
  ($sha | test("^[0-9a-f]{64}$")) and ($policy_sha | test("^[0-9a-f]{64}$")) and
  ($repo | test("^[0-9a-f]{40}$")) and
  ($lock_uid | test("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"))
' <<<"$session" >/dev/null || refuse 'the active session does not match the replacement Node'

policy_b64=$(jq -er '.data["install-policy"]' <<<"$secret_json" 2>/dev/null) ||
  refuse 'the active Bootie install policy is absent'
policy=$(printf '%s' "$policy_b64" | base64 -d) ||
  refuse 'the active Bootie install policy is not valid base64'
[[ $(printf '%s' "$policy_b64" | base64 -d | sha256sum | awk '{print $1}') == "$policy_sha" &&
   $policy == "$node $disk" ]] || refuse 'the active Bootie install policy is invalid'

ignition_b64=$(jq -er --arg key "$node.ign" '.data[$key]' <<<"$secret_json" 2>/dev/null) ||
  refuse 'the active session has no destination Ignition'

runtime_dir=${BOOTIE_RUNTIME_DIR:-/run/bootie}
[[ -d $runtime_dir && ! -L $runtime_dir && -w $runtime_dir ]] ||
  refuse 'the Bootie runtime directory is unavailable'
work=$(mktemp -d -- "$runtime_dir/.live-ignition.XXXXXXXX")
chmod 0700 "$work"
cleanup() {
  find "$work" -xdev -mindepth 1 -delete 2>/dev/null || true
  rmdir "$work" 2>/dev/null || true
}
trap cleanup EXIT

config_count=1
printf '%s' "$ignition_b64" | base64 -d >"$work/config.0.ign" ||
  refuse 'the destination Ignition is not valid base64'
[[ $(sha256sum "$work/config.0.ign" | awk '{print $1}') == "$ignition_sha" ]] ||
  refuse 'the destination Ignition no longer matches its session digest'

decode_data_url() {
  local source=$1 compression=$2 output=$3
  [[ $source == data:\;base64,* ]] ||
    refuse 'the destination Ignition contains a non-embedded child'
  case $compression in
    '') printf '%s' "${source#data:;base64,}" | base64 -d >"$output" ;;
    gzip) printf '%s' "${source#data:;base64,}" | base64 -d | gzip -d >"$output" ;;
    *) refuse 'the destination Ignition contains unsupported compression' ;;
  esac || refuse 'an embedded destination Ignition object could not be decoded'
}

case $role in
  control-plane)
    network_path=/etc/NetworkManager/system-connections/fabric-static.nmconnection
    gateway=10.66.0.1
    expected_id=fabric-static
    ;;
  service)
    network_path=/etc/NetworkManager/system-connections/fabric-services.nmconnection
    gateway=10.66.1.1
    expected_id=fabric-services
    ;;
  *) refuse 'the session role is unsupported' ;;
esac

network_count=0
for ((config_index = 0; config_index < config_count; config_index += 1)); do
  ((config_count <= 64)) || refuse 'the destination Ignition graph exceeds 64 configs'
  config=$work/config.$config_index.ign
  jq -e '
    type == "object" and
    (.ignition.version | type == "string" and startswith("3.")) and
    ((.ignition.config.replace // null) == null) and
    ((.ignition.config.merge // []) | type == "array") and
    (all(.ignition.config.merge[]?;
      type == "object" and (.source | type == "string") and
      ((.compression // "") == "" or .compression == "gzip"))) and
    ((.storage.files // []) | type == "array") and
    (all(.storage.files[]?; type == "object" and (.path | type == "string")))
  ' "$config" >/dev/null || refuse 'the destination Ignition graph is malformed'

  while IFS= read -r encoded; do
    record=$(printf '%s' "$encoded" | base64 -d) ||
      refuse 'a network-file record is malformed'
    path=$(jq -er '.path' <<<"$record") || refuse 'a network-file record is malformed'
    [[ $path == "$network_path" ]] ||
      refuse 'the destination Ignition contains an unexpected NetworkManager profile'
    ((network_count += 1))
    ((network_count == 1)) || refuse 'the destination Ignition contains duplicate network profiles'
    source=$(jq -er '.contents.source' <<<"$record") || refuse 'the network profile has no contents'
    compression=$(jq -r '.contents.compression // ""' <<<"$record")
    decode_data_url "$source" "$compression" "$work/network.nmconnection"
  done < <(jq -r '
    .storage.files[]?
    | select(.path | startswith("/etc/NetworkManager/system-connections/"))
    | select(.path | endswith(".nmconnection"))
    | @base64
  ' "$config")

  while IFS= read -r encoded; do
    ((config_count += 1))
    ((config_count <= 64)) || refuse 'the destination Ignition graph exceeds 64 configs'
    record=$(printf '%s' "$encoded" | base64 -d) ||
      refuse 'an Ignition merge record is malformed'
    source=$(jq -er '.source' <<<"$record") || refuse 'an Ignition merge record is malformed'
    compression=$(jq -r '.compression // ""' <<<"$record")
    decode_data_url "$source" "$compression" "$work/config.$((config_count - 1)).ign"
  done < <(jq -r '.ignition.config.merge[]? | @base64' "$config")
done
((network_count == 1)) || refuse 'the destination Ignition has no exact network profile'

ini_values() {
  local section=$1 key=$2
  awk -v wanted_section="$section" -v wanted_key="$key" '
    /^\[[^]]+\]$/ { section=substr($0, 2, length($0) - 2); next }
    index($0, "=") {
      key=substr($0, 1, index($0, "=") - 1)
      if (section == wanted_section && key == wanted_key)
        print substr($0, index($0, "=") + 1)
    }
  ' "$work/network.nmconnection"
}
require_ini_value() {
  local section=$1 key=$2 expected=$3
  local -a values=()
  mapfile -t values < <(ini_values "$section" "$key")
  [[ ${#values[@]} == 1 && ${values[0],,} == "${expected,,}" ]] ||
    refuse "the destination network profile has an invalid $section.$key"
}
require_ini_value connection id "$expected_id"
mapfile -t uuid_values < <(ini_values connection uuid)
[[ ${#uuid_values[@]} == 1 &&
   ${uuid_values[0]} =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]] ||
  refuse 'the destination network profile has an invalid connection.uuid'
require_ini_value connection type ethernet
require_ini_value connection autoconnect true
require_ini_value connection autoconnect-priority 100
require_ini_value ethernet mac-address "$mac"
require_ini_value ipv4 method manual
require_ini_value ipv4 address1 "$address/24,$gateway"
require_ini_value ipv4 may-fail false
require_ini_value ipv6 method disabled

cat >"$work/installer.yaml" <<'EOF'
copy-network: true
network-dir: /etc/NetworkManager/system-connections
EOF
chmod 0600 "$work/installer.yaml"

network_data=$(base64 -w0 "$work/network.nmconnection")
installer_data=$(base64 -w0 "$work/installer.yaml")
service_merge='[]'
if [[ $role == service ]]; then
  service_live=${BOOTIE_SERVICE_LIVE_IGNITION_FILE:-/pxe/fabric-day2-service-live.ign}
  [[ -f $service_live && ! -L $service_live ]] ||
    refuse 'the service-node install poweroff Ignition is unavailable'
  jq -e '.ignition.version | startswith("3.")' "$service_live" >/dev/null ||
    refuse 'the service-node install poweroff Ignition is malformed'
  service_data=$(base64 -w0 "$service_live")
  service_merge=$(jq -cn --arg source "data:;base64,$service_data" '[{source:$source}]')
fi

jq -n --arg network_path "$network_path" \
  --arg network_source "data:;base64,$network_data" \
  --arg installer_source "data:;base64,$installer_data" \
  --argjson merge "$service_merge" '{
    ignition:{version:"3.5.0", config:{merge:$merge}},
    storage:{files:[
      {path:$network_path, overwrite:true, mode:384,
       contents:{source:$network_source}},
      {path:"/etc/coreos/installer.d/0000-fabric-day2-network.yaml",
       overwrite:true, mode:384, contents:{source:$installer_source}}
    ]}
  }' >"$work/live.ign"
jq -e '
  .ignition.version == "3.5.0" and
  ([.storage.files[].path] | length) == 2 and
  ([.storage.files[] | select(.path ==
    "/etc/coreos/installer.d/0000-fabric-day2-network.yaml")] | length) == 1
' "$work/live.ign" >/dev/null || refuse 'the live installer Ignition could not be generated'

printf 'content-type: application/json\n\n'
cat "$work/live.ign"
