#!/bin/bash
set -ueo pipefail

refuse() {
  echo "$1" >&2
  echo 'Status: 404 Not Found'
  echo 'Cache-Control: no-store'
  echo content-type: text/plain
  echo
  exit
}

[[ ${BOOTIE_INSTALL_DELIVERY:-ignition} == custom-initramfs ]] ||
  refuse 'customized PXE initramfs delivery is disabled'
[[ ${REQUEST_METHOD:-GET} == GET ]] ||
  refuse 'customized PXE initramfs request is not a GET'

request_path=${REQUEST_URI%%\?*}
request_name=${request_path##*/}
[[ $request_path == "/custom-initramfs/$request_name" &&
   -n ${BOOTIE_CUSTOM_INITRAMFS_NAME:-} &&
   $request_name == "$BOOTIE_CUSTOM_INITRAMFS_NAME" &&
   $request_name =~ ^[0-9a-f]{32,64}\.img$ ]] ||
  refuse 'customized PXE initramfs capability is absent or incorrect'

# BOOTIE_CUSTOM_INITRAMFS_FILE is the object this endpoint itself streams, not
# merely an object inspected before redirecting Nginx to a different path.
# The station normally leaves it unset and mounts /pxe/<capability>; the
# explicit path remains useful for hermetic tests and is safe because it is
# also the served inode.
custom_initramfs_file=${BOOTIE_CUSTOM_INITRAMFS_FILE:-/pxe/$request_name}
[[ $custom_initramfs_file == /* &&
   ${custom_initramfs_file##*/} == "$request_name" &&
   -f $custom_initramfs_file && ! -L $custom_initramfs_file ]] ||
  refuse 'customized PXE initramfs runtime snapshot is unavailable'
[[ -n ${BOOTIE_CUSTOM_INITRAMFS_SHA256:-} &&
   $BOOTIE_CUSTOM_INITRAMFS_SHA256 =~ ^[0-9a-f]{64}$ ]] ||
  refuse 'customized PXE initramfs expected SHA-256 is unavailable'

exec {initramfs_fd}<"$custom_initramfs_file" ||
  refuse 'customized PXE initramfs runtime snapshot could not be opened'
[[ $(stat -Lc '%a:%h' "/proc/self/fd/$initramfs_fd") == 644:1 ]] ||
  refuse 'customized PXE initramfs runtime snapshot has unsafe metadata'
actual_sha256=$(sha256sum "/proc/self/fd/$initramfs_fd" | awk '{print $1}')
[[ $actual_sha256 == "$BOOTIE_CUSTOM_INITRAMFS_SHA256" ]] ||
  refuse 'customized PXE initramfs runtime snapshot changed'
content_length=$(stat -Lc '%s' "/proc/self/fd/$initramfs_fd")
[[ $content_length =~ ^[1-9][0-9]*$ ]] ||
  refuse 'customized PXE initramfs runtime snapshot is empty'

echo 'Cache-Control: no-store'
echo 'Content-Type: application/octet-stream'
echo "Content-Length: $content_length"
echo
cat <&"$initramfs_fd"
