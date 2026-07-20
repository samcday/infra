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
case "${BOOTIE_INSTALL_DELIVERY:-ignition}" in
  ignition | custom-initramfs) ;;
  *) booterr 'BOOTIE_INSTALL_DELIVERY must be ignition or custom-initramfs' ;;
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
if [[ "${BOOTIE_INSTALL_DELIVERY:-ignition}" == custom-initramfs ]]; then
  if [[ -z "${BOOTIE_NODE_NAME:-}" ||
        "${BOOTIE_ALLOW_NODE_CREATE:-true}" != false ||
        "${BOOTIE_REQUIRE_INSTALL_POLICY:-false}" != true ||
        "${BOOTIE_REQUIRE_BOOTSTRAP_STATE:-false}" != true ]]; then
    booterr 'custom-initramfs delivery requires a fixed predeclared Node and both install gates'
  fi
  if [[ -z "${BOOTIE_CUSTOM_INITRAMFS_NAME:-}" ||
        ! "$BOOTIE_CUSTOM_INITRAMFS_NAME" =~ ^[0-9a-f]{32,64}\.img$ ]]; then
    booterr 'BOOTIE_CUSTOM_INITRAMFS_NAME must be a 128-bit-or-stronger capability filename'
  fi
  if [[ -z "${BOOTIE_CUSTOM_FCOS_VERSION:-}" ||
        "$BOOTIE_CUSTOM_FCOS_VERSION" != "$FCOS_VERSION" ]]; then
    booterr 'BOOTIE_CUSTOM_FCOS_VERSION must exactly match FCOS_VERSION'
  fi
  if [[ "${BOOTIE_CUSTOM_LIVE_KARGS:-}" != \
        'coreos.inst.skip_reboot systemd.show_status=false' ]]; then
    booterr 'BOOTIE_CUSTOM_LIVE_KARGS must preserve the exact reviewed installer lifecycle arguments'
  fi
  if [[ -z "${BOOTIE_EXPECTED_NODE_UID:-}" ||
        ! "$BOOTIE_EXPECTED_NODE_UID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
    booterr 'BOOTIE_EXPECTED_NODE_UID must be one exact Kubernetes UUID'
  fi
  if [[ -z "${BOOTIE_CUSTOM_INITRAMFS_SHA256:-}" ||
        ! "$BOOTIE_CUSTOM_INITRAMFS_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
    booterr 'BOOTIE_CUSTOM_INITRAMFS_SHA256 must be one lowercase SHA-256'
  fi
  custom_initramfs_file=${BOOTIE_CUSTOM_INITRAMFS_FILE:-/pxe/$BOOTIE_CUSTOM_INITRAMFS_NAME}
  if [[ $custom_initramfs_file != /* ||
        ${custom_initramfs_file##*/} != "$BOOTIE_CUSTOM_INITRAMFS_NAME" ||
        ! -f $custom_initramfs_file || -L $custom_initramfs_file ]]; then
    booterr 'the customized PXE initramfs is unavailable or does not match its capability name'
  fi
  if [[ $(stat -Lc '%a:%h' "$custom_initramfs_file") != 644:1 ]]; then
    booterr 'the customized PXE initramfs must be a single-link runtime snapshot'
  fi
  actual_custom_initramfs_sha256=$(sha256sum "$custom_initramfs_file" | awk '{print $1}')
  if [[ $actual_custom_initramfs_sha256 != "$BOOTIE_CUSTOM_INITRAMFS_SHA256" ]]; then
    booterr 'the customized PXE initramfs does not match its expected SHA-256'
  fi
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
node_snapshot=

if [[ -n "${BOOTIE_NODE_NAME:-}" ]]; then
  node=node/$BOOTIE_NODE_NAME
  if [[ "${BOOTIE_INSTALL_DELIVERY:-ignition}" == custom-initramfs ]]; then
    node_snapshot=$(kubectl get "$node" -o json) ||
      booterr "the fixed Bootie Node is unavailable: $node"
    jq -e --arg name "$BOOTIE_NODE_NAME" '
      .apiVersion == "v1" and .kind == "Node" and .metadata.name == $name
    ' <<<"$node_snapshot" >/dev/null ||
      booterr "the fixed Bootie Node snapshot is malformed: $node"
    custom_node_uid=$(jq -r '.metadata.uid // ""' <<<"$node_snapshot")
    if [[ $custom_node_uid != "$BOOTIE_EXPECTED_NODE_UID" ]]; then
      booterr "the fixed Bootie Node UID does not match the issued identity: $node"
    fi
    declared_mac=$(jq -r '.metadata.labels["samcday.com/mac"] // ""' <<<"$node_snapshot")
    declared_serial=$(jq -r '.metadata.labels["samcday.com/serial"] // ""' <<<"$node_snapshot")
  else
    declared_mac=$(kubectl get "$node" -o jsonpath='{.metadata.labels.samcday\.com/mac}') ||
      booterr "the fixed Bootie Node is unavailable: $node"
    declared_serial=$(kubectl get "$node" -o jsonpath='{.metadata.labels.samcday\.com/serial}') ||
      booterr "the fixed Bootie Node identity is unavailable: $node"
  fi
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
  if [[ "${BOOTIE_INSTALL_DELIVERY:-ignition}" == custom-initramfs ]]; then
    boot_device=$(jq -r '.metadata.annotations["samcday.com/boot-device"] // ""' \
      <<<"$node_snapshot")
    install=$(jq -r '.metadata.annotations["samcday.com/install"] // ""' \
      <<<"$node_snapshot")
    discovery=$(jq -r '.metadata.labels["samcday.com/discovery"] // ""' \
      <<<"$node_snapshot")
    bootstrap_state=$(jq -r \
      '.metadata.annotations["fabric.samcday.com/bootstrap-state"] // ""' \
      <<<"$node_snapshot")
    custom_node_resource_version=$(jq -r '.metadata.resourceVersion // ""' \
      <<<"$node_snapshot")
    stale_ignition_token=$(jq -r \
      '.metadata.annotations["samcday.com/ignition-token"] // ""' \
      <<<"$node_snapshot")
    stale_ignition_mode=$(jq -r \
      '.metadata.annotations["samcday.com/ignition-mode"] // ""' \
      <<<"$node_snapshot")
    [[ -n $custom_node_resource_version ]] ||
      booterr "the fixed Bootie Node has no resourceVersion: $node"
    if [[ -n $stale_ignition_token || -n $stale_ignition_mode ]]; then
      booterr "the fixed Bootie Node has stale Ignition authorization: $node"
    fi
  else
    boot_device=$(kubectl get "$node" -o jsonpath='{.metadata.annotations.samcday\.com/boot-device}')
    install=$(kubectl get "$node" -o jsonpath='{.metadata.annotations.samcday\.com/install}')
    discovery=$(kubectl get "$node" -o jsonpath='{.metadata.labels.samcday\.com/discovery}')
    bootstrap_state=$(kubectl get "$node" \
      -o jsonpath='{.metadata.annotations.fabric\.samcday\.com/bootstrap-state}')
  fi
fi

if [[ -n "${boot_device:-}" ]] && [[ "${install:-}" != "true" ]]; then
  boot_local
fi

ignition_url="$BOOTIE_PUBLIC_ORIGIN/ignition/${node/node\/}?token=$ignition_token"
initramfs_url="$FCOS_BASE/fedora-coreos-$FCOS_VERSION-live-initramfs.x86_64.img"
grub_initramfs_url="${FCOS_GRUB_BASE:-}/fedora-coreos-$FCOS_VERSION-live-initramfs.x86_64.img"

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

  # Atomically consume the destructive authorization. The legacy delivery
  # binds a following Ignition response to a one-use token. A customized PXE
  # initramfs already carries the live and destination Ignitions, so it must
  # not leave a second, independently usable Ignition token behind.
  if [[ "${BOOTIE_INSTALL_DELIVERY:-ignition}" == custom-initramfs ]]; then
    patch=$(jq -cn --arg resource_version "$custom_node_resource_version" \
      --arg uid "$custom_node_uid" '[
      {"op":"test","path":"/metadata/resourceVersion","value":$resource_version},
      {"op":"test","path":"/metadata/uid","value":$uid},
      {"op":"test","path":"/metadata/annotations/samcday.com~1install","value":"true"},
      {"op":"test","path":"/metadata/annotations/fabric.samcday.com~1bootstrap-state","value":"install-armed"},
      {"op":"remove","path":"/metadata/annotations/samcday.com~1install"},
      {"op":"replace","path":"/metadata/annotations/fabric.samcday.com~1bootstrap-state","value":"install-response-issued"}
    ]')
  elif [[ "${BOOTIE_REQUIRE_BOOTSTRAP_STATE:-false}" == true ]]; then
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
  if [[ "${BOOTIE_INSTALL_DELIVERY:-ignition}" == custom-initramfs ]]; then
    initramfs_url="$BOOTIE_PUBLIC_ORIGIN/custom-initramfs/$BOOTIE_CUSTOM_INITRAMFS_NAME"
    if [[ $BOOT_FORMAT == grub ]]; then
      [[ $FCOS_GRUB_BASE == */static ]] ||
        booterr 'custom-initramfs GRUB delivery requires FCOS_GRUB_BASE ending in /static'
      grub_initramfs_url="${FCOS_GRUB_BASE%/static}/custom-initramfs/$BOOTIE_CUSTOM_INITRAMFS_NAME"
    fi
    kernel_args+="ignition.firstboot ignition.platform.id=metal $BOOTIE_CUSTOM_LIVE_KARGS "
  else
    ignition_url+="&install=1"
    kernel_args+="coreos.inst.install_dev=$boot_device coreos.inst.ignition_url=$ignition_url "
  fi
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
initrd $grub_initramfs_url
boot
HERE
else
  cat <<HERE
#!ipxe

kernel $FCOS_BASE/fedora-coreos-$FCOS_VERSION-live-kernel.x86_64 initrd=main $kernel_args
initrd --name main $initramfs_url

boot
HERE
fi
