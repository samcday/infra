#!/bin/bash
set -euo pipefail

refuse() {
  printf '%s\n' "$1" >&2
  printf 'Status: %s\ncontent-type: text/plain\n\n' "${2:-409 Conflict}"
  exit
}

for name in BOOTIE_NODE_INVENTORY_FILE BOOTIE_DISCOVERY_FILE \
  BOOTIE_DISCOVERY_IGNITION_FILE; do
  [[ -n ${!name:-} ]] || refuse "$name is required"
done
[[ ${REQUEST_METHOD:-GET} == GET ]] ||
  refuse 'the discovery Ignition endpoint is GET-only' '405 Method Not Allowed'
[[ -f $BOOTIE_DISCOVERY_IGNITION_FILE && ! -L $BOOTIE_DISCOVERY_IGNITION_FILE ]] ||
  refuse 'the discovery Ignition base is unavailable'
jq -e '
  type == "object" and (.ignition.version | type == "string" and startswith("3."))
' "$BOOTIE_DISCOVERY_IGNITION_FILE" >/dev/null ||
  refuse 'the discovery Ignition base is malformed'

request_path=${REQUEST_URI%%\?*}
node=${request_path#/discovery-ignition/}
[[ $request_path == "/discovery-ignition/$node" &&
   $node =~ ^fabric-az1-(cp[123]|svc[12])$ ]] ||
  refuse 'the discovery Ignition request path is invalid' '404 Not Found'

request_mac=
IFS='&' read -r -a query_parts <<<"${QUERY_STRING:-}"
for part in "${query_parts[@]}"; do
  case ${part%%=*} in
    mac)
      [[ -z $request_mac ]] || refuse 'duplicate discovery MAC'
      request_mac=${part#*=}
      ;;
    '') ;;
    *) refuse 'the discovery Ignition query is invalid' '403 Forbidden' ;;
  esac
done
request_mac=${request_mac,,}
request_mac=${request_mac//-/:}
[[ $request_mac =~ ^([0-9a-f]{2}:){5}[0-9a-f]{2}$ ]] ||
  refuse 'a valid discovery MAC is required' '403 Forbidden'

discovery_matches=0
discovery_mac=
while read -r candidate_node candidate_mac extra; do
  [[ -n ${candidate_node:-} && $candidate_node != \#* ]] || continue
  [[ -z ${extra:-} ]] || refuse 'the discovery inventory is malformed'
  if [[ $candidate_node == "$node" ]]; then
    ((discovery_matches += 1))
    discovery_mac=$candidate_mac
  fi
done <"$BOOTIE_DISCOVERY_FILE"
((discovery_matches == 1)) ||
  refuse 'the node has no exact discovery admission' '404 Not Found'
[[ $request_mac == "$discovery_mac" ]] ||
  refuse 'the discovery MAC does not match its admission' '403 Forbidden'

inventory_matches=0
role=
address=
while read -r candidate_node candidate_role candidate_address _candidate_mac \
  _candidate_disk _candidate_pxe extra; do
  [[ -n ${candidate_node:-} && $candidate_node != \#* ]] || continue
  [[ -z ${extra:-} ]] || refuse 'the node inventory is malformed'
  if [[ $candidate_node == "$node" ]]; then
    ((inventory_matches += 1))
    role=$candidate_role
    address=$candidate_address
  fi
done <"$BOOTIE_NODE_INVENTORY_FILE"
((inventory_matches == 1)) || refuse 'the discovery target is outside node inventory'

case $role in
  control-plane)
    connection_id=fabric-static
    network_path=/etc/NetworkManager/system-connections/fabric-static.nmconnection
    gateway=10.66.0.1
    ;;
  service)
    connection_id=fabric-services
    network_path=/etc/NetworkManager/system-connections/fabric-services.nmconnection
    gateway=10.66.1.1
    ;;
  *) refuse 'the discovery target has an unsupported role' ;;
esac

connection_digest=$(printf '%s' "fabric-day2-discovery:$node:$request_mac" |
  sha256sum | awk '{print $1}')
connection_uuid=${connection_digest:0:8}-${connection_digest:8:4}-4${connection_digest:13:3}-a${connection_digest:17:3}-${connection_digest:20:12}
network=$(cat <<EOF
[connection]
id=$connection_id
uuid=$connection_uuid
type=ethernet
autoconnect=true
autoconnect-priority=100

[ethernet]
mac-address=$request_mac

[ipv4]
method=manual
address1=$address/24,$gateway
may-fail=false

[ipv6]
method=disabled
EOF
)

base_source="data:;base64,$(base64 -w0 "$BOOTIE_DISCOVERY_IGNITION_FILE")"
network_source="data:;base64,$(printf '%s\n' "$network" | base64 -w0)"
printf 'content-type: application/json\n\n'
jq -n --arg base "$base_source" --arg path "$network_path" \
  --arg network "$network_source" '{
  ignition:{version:"3.5.0",config:{merge:[{source:$base}]}},
  storage:{files:[{
    path:$path,overwrite:true,mode:384,contents:{source:$network}
  }]}
}'
