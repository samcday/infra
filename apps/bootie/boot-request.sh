#!/bin/bash
set -ueo pipefail

function booterr() {
  echo "$1" >&2
  echo 'Status: 409 Conflict'
  echo content-type: text/plain
  echo
  cat <<HERE
#!ipxe

echo Bootie refused this request. Check the provisioning service log.
exit
HERE
  exit
}

if [[ -z "${FCOS_VERSION:-}" ]]; then
  FCOS_VERSION=$(curl -s --fail https://builds.coreos.fedoraproject.org/streams/stable.json | jq -r .architectures.x86_64.artifacts.metal.release)
fi

if [[ -z "${FCOS_BASE:-}" ]]; then
  FCOS_BASE="https://builds.coreos.fedoraproject.org/prod/streams/stable/builds/$FCOS_VERSION/x86_64"
fi

# iPXE supplies simple, non-percent-encoded identity values. Ignore every
# other query key instead of turning attacker-controlled keys into variables.
qs_mac=
qs_serial=
mac_seen=false
serial_seen=false
IFS='&' read -r -a query_parts <<< "${QUERY_STRING:-}"
for part in "${query_parts[@]}"; do
  key=${part%%=*}
  value=${part#*=}
  case "$key" in
    mac)
      $mac_seen && booterr 'duplicate mac query parameter'
      qs_mac=$value
      mac_seen=true
      ;;
    serial)
      $serial_seen && booterr 'duplicate serial query parameter'
      qs_serial=$value
      serial_seen=true
      ;;
  esac
done

# These values become Kubernetes label selectors and label values.  Accept
# only iPXE's hex-hyphen MAC form and Kubernetes' bounded label-value syntax;
# this also makes their later JSON and command-line interpolation inert.
if [[ -n "$qs_mac" ]] &&
    [[ ! "$qs_mac" =~ ^([[:xdigit:]]{2}-){5}[[:xdigit:]]{2}$ ]]; then
  booterr 'invalid mac query parameter'
fi
qs_mac=${qs_mac,,}
if [[ -n "$qs_serial" ]] &&
    [[ ! "$qs_serial" =~ ^[[:alnum:]]([[:alnum:]_.-]{0,61}[[:alnum:]])?$ ]]; then
  booterr 'invalid serial query parameter'
fi
if [[ -z "$qs_mac" && -z "$qs_serial" ]]; then
  booterr 'a valid mac or serial query parameter is required'
fi

# try to lookup by mac first, fallback to serial
node=""
node_created=false
ignition_token=$(tr -d - < /proc/sys/kernel/random/uuid)

if [[ -n "${qs_mac:-}" ]]; then
  node=$(kubectl get node -o name -l "samcday.com/mac=$qs_mac" | head -n1)
fi

if [[ -z "$node" ]] && [[ -n "${qs_serial:-}" ]]; then
  node=$(kubectl get node -o name -l "samcday.com/serial=$qs_serial" | head -n1)
fi

if [[ -z "$node" ]]; then
  # unknown node, generate a name for it and create it now
  name=$(petname -u)
  if ((${#name} > 63)) ||
      [[ ! "$name" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
    booterr 'petname returned an invalid Kubernetes Node name'
  fi
  node=$(
    jq -n \
      --arg name "$name" \
      --arg token "$ignition_token" \
      --arg mac "$qs_mac" \
      --arg serial "$qs_serial" \
      '{
        apiVersion: "v1",
        kind: "Node",
        metadata: {
          name: $name,
          annotations: {
            "samcday.com/ignition-token": $token,
            "samcday.com/ignition-mode": "live"
          },
          labels: {
            "samcday.com/discovery": "true",
            "samcday.com/mac": $mac,
            "samcday.com/serial": $serial
          }
        }
      }' |
      kubectl apply -o name -f-
  )
  node_created=true
else
  boot_device=$(kubectl get "$node" -o jsonpath='{.metadata.annotations.samcday\.com/boot-device}')
  install=$(kubectl get "$node" -o jsonpath='{.metadata.annotations.samcday\.com/install}')
  discovery=$(kubectl get "$node" -o jsonpath='{.metadata.labels.samcday\.com/discovery}')
fi

if [[ -n "${boot_device:-}" ]] && [[ "${install:-}" != "true" ]]; then
  echo content-type: text/plain
  echo
  cat <<HERE
#!ipxe

# Installation is not armed. Continue to the next firmware boot device.
exit
HERE
  exit
fi

ignition_url="http://$HTTP_HOST/ignition/${node/node\/}?token=$ignition_token"

kernel="$FCOS_BASE/fedora-coreos-$FCOS_VERSION-live-kernel.x86_64 initrd=main "
kernel+="coreos.live.rootfs_url=$FCOS_BASE/fedora-coreos-$FCOS_VERSION-live-rootfs.x86_64.img "

if [[ "${install:-}" == "true" ]]; then
  if [[ "${discovery:-}" == "true" ]]; then
    booterr "installation is armed for $node while samcday.com/discovery is still true"
  fi
  if [[ -z "${boot_device:-}" ]]; then
    booterr "installation is armed for $node but samcday.com/boot-device is empty"
  fi

  # Atomically consume the destructive authorization and bind this response to
  # a random, one-use Ignition token. Concurrent requests cannot both win.
  patch=$(jq -cn --arg token "$ignition_token" '[
    {"op":"test","path":"/metadata/annotations/samcday.com~1install","value":"true"},
    {"op":"remove","path":"/metadata/annotations/samcday.com~1install"},
    {"op":"add","path":"/metadata/annotations/samcday.com~1ignition-token","value":$token},
    {"op":"add","path":"/metadata/annotations/samcday.com~1ignition-mode","value":"install"}
  ]')
  if ! kubectl patch "$node" --type=json --patch "$patch" >/dev/null; then
    booterr "installation authorization for $node was already consumed or changed"
  fi
  ignition_url+="&install=1"
  kernel+="coreos.inst.install_dev=$boot_device coreos.inst.ignition_url=$ignition_url "
else
  # Known non-installing nodes need a token too. Unknown nodes received this
  # annotation in their atomic create above.
  if ! $node_created; then
    kubectl annotate "$node" \
      samcday.com/ignition-token="$ignition_token" \
      samcday.com/ignition-mode=live \
      --overwrite >/dev/null
  fi
  # No installation target is declared, so live-boot for inventory.
  kernel+="ignition.firstboot ignition.platform.id=metal ignition.config.url=$ignition_url "
fi

echo content-type: text/plain
echo

cat <<HERE
#!ipxe

kernel $kernel
initrd --name main $FCOS_BASE/fedora-coreos-$FCOS_VERSION-live-initramfs.x86_64.img

boot
HERE
