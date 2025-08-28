#!/bin/bash
set -ueo pipefail

function booterr() {
  echo $1 >&2
  cat <<HERE
{
  "ipxe-script": "#!ipxe\nexit"
}
HERE
  exit
}

if [[ -z "${FCOS_VERSION:-}" ]]; then
  FCOS_VERSION=$(curl -s --fail https://builds.coreos.fedoraproject.org/streams/stable.json | jq -r .architectures.x86_64.artifacts.metal.release)
fi

if [[ -z "${FCOS_BASE}" ]]; then
  FCOS_BASE="https://builds.coreos.fedoraproject.org/prod/streams/stable/builds/$FCOS_VERSION/x86_64"
fi

# https://stackoverflow.com/a/3919908
IFS='=&' read -r -a qs <<< "$QUERY_STRING"
for ((i=0; i<${#qs[@]}; i+=2))
do
    declare "qs_${qs[i]}"="${qs[i+1]}"
done

# try to lookup by mac first, fallback to serial
node=""

if [[ -n "${qs_mac:-}" ]]; then
  node=$(kubectl get node -o name -l "samcday.com/mac=$qs_mac" | head -n1)
fi

if [[ -z "$node" ]] && [[ -n "${qs_serial:-}" ]]; then
  node=$(kubectl get node -o name -l "samcday.com/serial=$qs_serial" | head -n1)
fi

if [[ -z "$node" ]]; then
  # unknown node, generate a name for it and create it now
  name=$(petname -u)
  node=$(
    kubectl apply -o name -f- <<HERE
apiVersion: v1
kind: Node
metadata:
  name: $name
  labels:
    samcday.com/mac: ${qs_mac:-}
    samcday.com/serial: ${qs_serial:-}
HERE
)
else
  boot_device=$(kubectl get "$node" -o jsonpath='{.metadata.annotations.samcday\.com/boot-device}')
fi

if [[ -z "${boot_device:-}" ]]; then
  boot_device="/dev/nvme0n1"
fi

echo content-type: text/plain
echo

cat <<HERE
#!ipxe

kernel $FCOS_BASE/fedora-coreos-$FCOS_VERSION-live-kernel.x86_64 initrd=main coreos.live.rootfs_url=$FCOS_BASE/fedora-coreos-$FCOS_VERSION-live-rootfs.x86_64.img coreos.inst.install_dev=$boot_device coreos.inst.ignition_url=http://$HTTP_HOST/ignition/${node/node\/}
initrd --name main $FCOS_BASE/fedora-coreos-$FCOS_VERSION-live-initramfs.x86_64.img

boot
HERE
