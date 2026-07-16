#!/bin/bash
set -ueo pipefail

BOOT_FORMAT=${BOOT_FORMAT:-ipxe}
case "$BOOT_FORMAT" in
  grub | ipxe) ;;
  *)
    echo "unsupported boot response format: $BOOT_FORMAT" >&2
    echo 'Status: 500 Internal Server Error'
    echo content-type: text/plain
    echo
    exit
    ;;
esac

function booterr() {
  echo "$1" >&2
  echo 'Status: 409 Conflict'
  echo content-type: text/plain
  echo
  if [[ $BOOT_FORMAT == grub ]]; then
    cat <<HERE
echo 'Bootie refused this request. Check the provisioning service log.'
sleep --interruptible 10
exit
HERE
  else
    cat <<HERE
#!ipxe

echo Bootie refused this request. Check the provisioning service log.
exit
HERE
  fi
  exit
}

function boot_local() {
  echo content-type: text/plain
  echo
  if [[ $BOOT_FORMAT == grub ]]; then
    cat <<HERE
# Installation is not armed. Continue to the next firmware boot device.
exit
HERE
  else
    cat <<HERE
#!ipxe

# Installation is not armed. Continue to the next firmware boot device.
exit
HERE
  fi
  exit
}

if [[ -z "${FCOS_VERSION:-}" ]]; then
  FCOS_VERSION=$(curl -s --fail https://builds.coreos.fedoraproject.org/streams/stable.json | jq -r .architectures.x86_64.artifacts.metal.release)
fi

if [[ -z "${FCOS_BASE:-}" ]]; then
  FCOS_BASE="https://builds.coreos.fedoraproject.org/prod/streams/stable/builds/$FCOS_VERSION/x86_64"
fi
if [[ $BOOT_FORMAT == grub && -z "${FCOS_GRUB_BASE:-}" ]]; then
  booterr 'FCOS_GRUB_BASE is required for a GRUB response'
fi
if [[ -n "${FCOS_GRUB_BASE:-}" && "$FCOS_GRUB_BASE" =~ [[:space:]] ]]; then
  booterr 'FCOS_GRUB_BASE contains whitespace'
fi
if [[ -z "${BOOTIE_PUBLIC_ORIGIN:-}" ||
      ! "$BOOTIE_PUBLIC_ORIGIN" =~ ^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$ ]]; then
  booterr 'BOOTIE_PUBLIC_ORIGIN must be an HTTP(S) origin without a path'
fi

case "${BOOTIE_REQUIRE_INSTALL_POLICY:-false}" in
  false | true) ;;
  *) booterr 'BOOTIE_REQUIRE_INSTALL_POLICY must be true or false' ;;
esac
case "${BOOTIE_REQUIRE_BOOTSTRAP_STATE:-false}" in
  false | true) ;;
  *) booterr 'BOOTIE_REQUIRE_BOOTSTRAP_STATE must be true or false' ;;
esac
if [[ "${BOOTIE_REQUIRE_INSTALL_POLICY:-false}" == true ]]; then
  if [[ -z "${BOOTIE_INSTALL_POLICY_FILE:-}" ||
        ! -f "$BOOTIE_INSTALL_POLICY_FILE" ||
        -L "$BOOTIE_INSTALL_POLICY_FILE" ]]; then
    booterr 'the required install policy is unavailable'
  fi
fi
case "${BOOTIE_ALLOW_NODE_CREATE:-true}" in
  false | true) ;;
  *) booterr 'BOOTIE_ALLOW_NODE_CREATE must be true or false' ;;
esac
if [[ -n "${BOOTIE_NODE_NAME:-}" ]] &&
    [[ ! "$BOOTIE_NODE_NAME" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ||
       ${#BOOTIE_NODE_NAME} -gt 63 ]]; then
  booterr 'BOOTIE_NODE_NAME is not a valid Kubernetes Node name'
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

# A single-node station can avoid cluster-wide Node list permission entirely.
# Otherwise, preserve the generic MAC-first, serial-second discovery behavior.
node=""
node_created=false
ignition_token=$(tr -d - < /proc/sys/kernel/random/uuid)

if [[ -n "${BOOTIE_NODE_NAME:-}" ]]; then
  node=node/$BOOTIE_NODE_NAME
  declared_mac=$(kubectl get "$node" -o jsonpath='{.metadata.labels.samcday\.com/mac}') ||
    booterr "the fixed Bootie Node is unavailable: $node"
  declared_serial=$(kubectl get "$node" -o jsonpath='{.metadata.labels.samcday\.com/serial}') ||
    booterr "the fixed Bootie Node identity is unavailable: $node"
  if [[ -n $qs_mac && $qs_mac != "$declared_mac" ]]; then
    booterr "the request MAC does not match $node"
  fi
  if [[ -n $qs_serial && $qs_serial != "$declared_serial" ]]; then
    booterr "the request serial does not match $node"
  fi
else
  if [[ -n "${qs_mac:-}" ]]; then
    node=$(kubectl get node -o name -l "samcday.com/mac=$qs_mac" | head -n1)
  fi

  if [[ -z "$node" ]] && [[ -n "${qs_serial:-}" ]]; then
    node=$(kubectl get node -o name -l "samcday.com/serial=$qs_serial" | head -n1)
  fi
fi

if [[ -z "$node" ]]; then
  if [[ "${BOOTIE_ALLOW_NODE_CREATE:-true}" != true ]]; then
    booterr 'no declared Node matches this request and Node creation is disabled'
  fi
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
  bootstrap_state=$(kubectl get "$node" \
    -o jsonpath='{.metadata.annotations.fabric\.samcday\.com/bootstrap-state}')
fi

if [[ -n "${boot_device:-}" ]] && [[ "${install:-}" != "true" ]]; then
  boot_local
fi

ignition_url="$BOOTIE_PUBLIC_ORIGIN/ignition/${node/node\/}?token=$ignition_token"

kernel_args="coreos.live.rootfs_url=$FCOS_BASE/fedora-coreos-$FCOS_VERSION-live-rootfs.x86_64.img "

if [[ "${install:-}" == "true" ]]; then
  if [[ "${discovery:-}" == "true" ]]; then
    booterr "installation is armed for $node while samcday.com/discovery is still true"
  fi
  if [[ -z "${boot_device:-}" ]]; then
    booterr "installation is armed for $node but samcday.com/boot-device is empty"
  fi
  if [[ "${BOOTIE_REQUIRE_BOOTSTRAP_STATE:-false}" == true &&
        "${bootstrap_state:-}" != install-armed ]]; then
    booterr "installation is armed for $node without the reviewed bootstrap state"
  fi

  if [[ -n "${BOOTIE_INSTALL_POLICY_FILE:-}" ]]; then
    policy_node=${node#node/}
    policy_device=
    policy_matches=0
    while read -r declared_node declared_device extra; do
      if [[ -z "${declared_node:-}" || $declared_node == \#* ]]; then
        continue
      fi
      if [[ -z "${declared_device:-}" || -n "${extra:-}" ]]; then
        booterr 'the install policy contains a malformed record'
      fi
      if [[ $declared_node == "$policy_node" ]]; then
        policy_device=$declared_device
        ((policy_matches += 1))
      fi
    done < "$BOOTIE_INSTALL_POLICY_FILE"
    if ((policy_matches != 1)); then
      booterr "the install policy does not contain exactly one record for $node"
    fi
    if [[ $boot_device != "$policy_device" ]]; then
      booterr "the declared boot device does not match install policy for $node"
    fi
  elif [[ "${BOOTIE_REQUIRE_INSTALL_POLICY:-false}" == true ]]; then
    booterr 'installation requires an exact device policy'
  fi

  # Atomically consume the destructive authorization and bind this response to
  # a random, one-use Ignition token. Concurrent requests cannot both win.
  if [[ "${BOOTIE_REQUIRE_BOOTSTRAP_STATE:-false}" == true ]]; then
    patch=$(jq -cn --arg token "$ignition_token" '[
      {"op":"test","path":"/metadata/annotations/samcday.com~1install","value":"true"},
      {"op":"test","path":"/metadata/annotations/fabric.samcday.com~1bootstrap-state","value":"install-armed"},
      {"op":"remove","path":"/metadata/annotations/samcday.com~1install"},
      {"op":"replace","path":"/metadata/annotations/fabric.samcday.com~1bootstrap-state","value":"install-response-issued"},
      {"op":"add","path":"/metadata/annotations/samcday.com~1ignition-token","value":$token},
      {"op":"add","path":"/metadata/annotations/samcday.com~1ignition-mode","value":"install"}
    ]')
  else
    patch=$(jq -cn --arg token "$ignition_token" '[
      {"op":"test","path":"/metadata/annotations/samcday.com~1install","value":"true"},
      {"op":"remove","path":"/metadata/annotations/samcday.com~1install"},
      {"op":"add","path":"/metadata/annotations/samcday.com~1ignition-token","value":$token},
      {"op":"add","path":"/metadata/annotations/samcday.com~1ignition-mode","value":"install"}
    ]')
  fi
  if ! kubectl patch "$node" --type=json --patch "$patch" >/dev/null; then
    booterr "installation authorization for $node was already consumed or changed"
  fi
  ignition_url+="&install=1"
  kernel_args+="coreos.inst.install_dev=$boot_device coreos.inst.ignition_url=$ignition_url "
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
  kernel_args+="ignition.firstboot ignition.platform.id=metal ignition.config.url=$ignition_url "
fi

echo content-type: text/plain
echo

if [[ $BOOT_FORMAT == grub ]]; then
  grub_kernel_args=${kernel_args//&/\\&}
  cat <<HERE
linux $FCOS_GRUB_BASE/fedora-coreos-$FCOS_VERSION-live-kernel.x86_64 $grub_kernel_args
initrd $FCOS_GRUB_BASE/fedora-coreos-$FCOS_VERSION-live-initramfs.x86_64.img
boot
HERE
else
  cat <<HERE
#!ipxe

kernel $FCOS_BASE/fedora-coreos-$FCOS_VERSION-live-kernel.x86_64 initrd=main $kernel_args
initrd --name main $FCOS_BASE/fedora-coreos-$FCOS_VERSION-live-initramfs.x86_64.img

boot
HERE
fi
