#!/bin/bash
set -euo pipefail

booterr() {
  printf '%s\n' "$1" >&2
  printf 'Status: 409 Conflict\ncontent-type: text/plain\n\n'
  if [[ ${BOOT_FORMAT:-grub} == grub ]]; then
    printf "echo 'Bootie refused the armed request; continuing to local disk.'\nsleep --interruptible 2\nexit\n"
  else
    printf '#!ipxe\n\necho Bootie refused the armed request; continuing to local disk.\nexit\n'
  fi
  exit
}

boot_local() {
  printf 'content-type: text/plain\n\n'
  if [[ ${BOOT_FORMAT:-grub} == grub ]]; then
    printf '# This admitted node is not armed. Continue to the next firmware device.\nexit\n'
  else
    printf '#!ipxe\n\n# This admitted node is not armed. Continue to the next firmware device.\nexit\n'
  fi
  exit
}

case ${BOOT_FORMAT:-grub} in grub | ipxe) ;; *) booterr 'unsupported boot response format' ;; esac
for name in BOOTIE_HOST_IP BOOTIE_IMAGE_DIGEST BOOTIE_IMAGE_REVISION \
  BOOTIE_NODE_INVENTORY_FILE BOOTIE_SESSION_NAMESPACE BOOTIE_SESSION_SECRET \
  BOOTIE_PUBLIC_ORIGIN FCOS_BASE FCOS_GRUB_BASE FCOS_VERSION; do
  [[ -n ${!name:-} ]] || booterr "$name is required"
done

query_mac=
IFS='&' read -r -a query_parts <<<"${QUERY_STRING:-}"
for part in "${query_parts[@]}"; do
  [[ ${part%%=*} == mac ]] || continue
  [[ -z $query_mac ]] || booterr 'duplicate mac query parameter'
  query_mac=${part#*=}
done
query_mac=${query_mac,,}
query_mac=${query_mac//:/-}
[[ $query_mac =~ ^([0-9a-f]{2}-){5}[0-9a-f]{2}$ ]] || boot_local

node=
role=
address=
mac=
boot_device=
records=0
while read -r candidate_node candidate_role candidate_address candidate_mac \
  candidate_disk _candidate_pxe extra; do
  [[ -n ${candidate_node:-} && $candidate_node != \#* ]] || continue
  [[ -z ${extra:-} ]] || booterr 'the node inventory is malformed'
  ((records += 1))
  if [[ ${candidate_mac//:/-} == "$query_mac" ]]; then
    [[ -z $node ]] || booterr 'the request MAC is duplicated in inventory'
    node=$candidate_node
    role=$candidate_role
    address=$candidate_address
    mac=$candidate_mac
    boot_device=$candidate_disk
  fi
done <"$BOOTIE_NODE_INVENTORY_FILE"
((records == 5)) || booterr 'the node inventory does not contain exactly five records'
[[ -n $node ]] || boot_local

node_json=$(kubectl get "node/$node" --output=json 2>/dev/null) || boot_local
secret_json=$(kubectl --namespace "$BOOTIE_SESSION_NAMESPACE" get \
  "secret/$BOOTIE_SESSION_SECRET" --output=json 2>/dev/null) || boot_local
session=$(jq -er '.data["session.json"] | @base64d' <<<"$secret_json" 2>/dev/null) ||
  booterr 'the active Bootie session metadata is malformed'
[[ $(jq -r '.node // ""' <<<"$session") == "$node" ]] || boot_local

node_uid=$(jq -r '.metadata.uid // ""' <<<"$node_json")
node_rv=$(jq -r '.metadata.resourceVersion // ""' <<<"$node_json")
session_uid=$(jq -r '.metadata.uid // ""' <<<"$secret_json")
ignition_sha=$(jq -r '.ignitionSha256 // ""' <<<"$session")
policy_sha=$(jq -r '.installPolicySha256 // ""' <<<"$session")
repo_commit=$(jq -r '.repoCommit // ""' <<<"$session")
lock_uid=$(jq -r '.lockUid // ""' <<<"$session")

jq -e --arg node "$node" --arg role "$role" --arg address "$address" \
  --arg mac "$mac" --arg disk "$boot_device" --arg node_uid "$node_uid" \
  --arg repo "$repo_commit" --arg ignition_sha "$ignition_sha" \
  --arg policy_sha "$policy_sha" --arg image_digest "$BOOTIE_IMAGE_DIGEST" \
  --arg image_revision "$BOOTIE_IMAGE_REVISION" --arg lock_uid "$lock_uid" '
  .schema == 1 and .purpose == "fabric-node-day-two-session" and
  .node == $node and .role == $role and .address == $address and .mac == $mac and
  .bootDevice == $disk and .nodeUid == $node_uid and .repoCommit == $repo and
  .ignitionSha256 == $ignition_sha and .installPolicySha256 == $policy_sha and
  .bootieImageDigest == $image_digest and .bootieImageRevision == $image_revision and
  .lockUid == $lock_uid and
  ($repo | test("^[0-9a-f]{40}$")) and
  ($ignition_sha | test("^[0-9a-f]{64}$")) and
  ($policy_sha | test("^[0-9a-f]{64}$")) and
  ($lock_uid | test("^[0-9a-f-]{36}$"))
' <<<"$session" >/dev/null || booterr 'the active Bootie session does not match inventory and image attestation'

policy_b64=$(jq -er '.data["install-policy"]' <<<"$secret_json" 2>/dev/null) ||
  booterr 'the active Bootie install policy is absent'
policy=$(printf '%s' "$policy_b64" | base64 -d) ||
  booterr 'the active Bootie install policy is not valid base64'
[[ $(printf '%s' "$policy_b64" | base64 -d | sha256sum | awk '{print $1}') == "$policy_sha" &&
   $policy == "$node $boot_device" ]] || booterr 'the active Bootie install policy is invalid'
ignition_b64=$(jq -er --arg key "$node.ign" '.data[$key]' <<<"$secret_json" 2>/dev/null) ||
  booterr 'the active Bootie Ignition is absent'
[[ $(printf '%s' "$ignition_b64" | base64 -d | sha256sum | awk '{print $1}') == "$ignition_sha" ]] ||
  booterr 'the active Bootie Ignition digest does not match the session'

mac_label=${mac//:/-}
jq -e --arg uid "$node_uid" --arg mac "$mac_label" --arg role "$role" \
  --arg disk "$boot_device" --arg session_uid "$session_uid" '
  .metadata.uid == $uid and .metadata.deletionTimestamp == null and
  .metadata.labels["samcday.com/mac"] == $mac and
  .metadata.labels["fabric.samcday.com/bootstrap-placeholder"] == "true" and
  .metadata.labels["samcday.com/discovery"] == "false" and
  .metadata.annotations["fabric.samcday.com/bootstrap-state"] == "install-armed" and
  .metadata.annotations["fabric.samcday.com/bootie-session-uid"] == $session_uid and
  .metadata.annotations["samcday.com/boot-device"] == $disk and
  .metadata.annotations["samcday.com/install"] == "true" and
  ((.status.nodeInfo.machineID // "") == "") and
  (if $role == "control-plane" then
    .metadata.labels["fabric.samcday.com/root-consensus"] == "true"
   else
    ([.spec.taints[]? | select(.key == "fabric.samcday.com/platform" and
      .value == "true" and .effect == "NoSchedule")] | length) == 1
   end)
' <<<"$node_json" >/dev/null || booterr 'the replacement Node is not at the exact armed placeholder state'

token=$(tr -d - </proc/sys/kernel/random/uuid)
patch=$(jq -cn --arg rv "$node_rv" --arg uid "$node_uid" --arg session_uid "$session_uid" \
  --arg token "$token" --arg repo "$repo_commit" --arg ignition_sha "$ignition_sha" \
  --arg image_digest "$BOOTIE_IMAGE_DIGEST" --arg image_revision "$BOOTIE_IMAGE_REVISION" '[
  {"op":"test","path":"/metadata/resourceVersion","value":$rv},
  {"op":"test","path":"/metadata/uid","value":$uid},
  {"op":"test","path":"/metadata/annotations/fabric.samcday.com~1bootie-session-uid","value":$session_uid},
  {"op":"test","path":"/metadata/annotations/fabric.samcday.com~1bootstrap-state","value":"install-armed"},
  {"op":"test","path":"/metadata/annotations/samcday.com~1install","value":"true"},
  {"op":"remove","path":"/metadata/annotations/samcday.com~1install"},
  {"op":"replace","path":"/metadata/annotations/fabric.samcday.com~1bootstrap-state","value":"install-response-issued"},
  {"op":"add","path":"/metadata/annotations/samcday.com~1ignition-token","value":$token},
  {"op":"add","path":"/metadata/annotations/samcday.com~1ignition-mode","value":"install"},
  {"op":"add","path":"/metadata/annotations/fabric.samcday.com~1config-revision","value":$repo},
  {"op":"add","path":"/metadata/annotations/fabric.samcday.com~1ignition-sha256","value":$ignition_sha},
  {"op":"add","path":"/metadata/annotations/fabric.samcday.com~1bootie-image-digest","value":$image_digest},
  {"op":"add","path":"/metadata/annotations/fabric.samcday.com~1bootie-image-revision","value":$image_revision}
]')
kubectl patch "node/$node" --type=json --patch "$patch" >/dev/null ||
  booterr 'the one-use Bootie arm was already consumed or changed'

ignition_url="$BOOTIE_PUBLIC_ORIGIN/ignition/$node?token=$token&install=1"
live_ignition_url="$BOOTIE_PUBLIC_ORIGIN/live-ignition/$node?token=$token"
kernel_args="coreos.live.rootfs_url=$FCOS_BASE/fedora-coreos-$FCOS_VERSION-live-rootfs.x86_64.img coreos.inst.install_dev=$boot_device coreos.inst.ignition_url=$ignition_url"
# coreos-installer-service persists recognized live network arguments into the
# one-shot ignition.firstboot file and prepends rd.neednet=1.  Carrier timeout
# is useful on both boots and avoids introducing a competing DHCP profile.
kernel_args+=" ignition.firstboot ignition.platform.id=metal ignition.config.url=$live_ignition_url rd.net.timeout.carrier=60 systemd.show_status=false"
if [[ $role == service ]]; then
  kernel_args+=" coreos.inst.skip_reboot"
fi
printf 'content-type: text/plain\n\n'
if [[ ${BOOT_FORMAT:-grub} == grub ]]; then
  printf 'linux %s/fedora-coreos-%s-live-kernel.x86_64 %s\n' "$FCOS_GRUB_BASE" "$FCOS_VERSION" "${kernel_args//&/\\&}"
  printf 'initrd %s/fedora-coreos-%s-live-initramfs.x86_64.img\nboot\n' "$FCOS_GRUB_BASE" "$FCOS_VERSION"
else
  printf '#!ipxe\n\nkernel %s/fedora-coreos-%s-live-kernel.x86_64 initrd=main %s\n' "$FCOS_BASE" "$FCOS_VERSION" "$kernel_args"
  printf 'initrd --name main %s/fedora-coreos-%s-live-initramfs.x86_64.img\nboot\n' "$FCOS_BASE" "$FCOS_VERSION"
fi
