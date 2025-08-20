#!/bin/bash
cd "$(dirname "$0")"
set -ueo pipefail

platform=$1
target=$2
profile=$3

openwrt_version="24.10.2"

curl="curl --retry 2 --fail"

build_dir="_build/${platform}-${target}-${openwrt_version}"
tarball=openwrt-imagebuilder-${openwrt_version}-${platform}-${target}.Linux-x86_64.tar.zst

if [[ ! -f "_build/${tarball}" ]]; then
  mkdir -p "_build"
  $curl -o "_build/$tarball" -L "https://downloads.openwrt.org/releases/${openwrt_version}/targets/${platform}/${target}/${tarball}"
fi

# rm -rf "$build_dir"
mkdir -p "$build_dir"

tar --strip-components=1 -C "${build_dir}" --zstd -xvf "_build/$tarball"

for f in $(find files/ -type f); do
  mkdir -p "$(dirname "$build_dir/$f")"
  cp "$f" "$build_dir/$f"
done

for src in $(find files.enc/ -type f); do
  dst=${src/files.enc/files}
  mkdir -p "$(dirname "$build_dir/$dst")"
  sops -d "$src" > "$build_dir/$dst"
done

mkdir -p "$build_dir/files/usr/share/tftp"
dst="$build_dir/files/usr/share/tftp/undionly.kpxe"
if [[ ! -f "$dst" ]]; then
  $curl -s -o "$dst" http://boot.ipxe.org/undionly.kpxe
fi
cp "$dst" "$dst.0"
dst="$build_dir/files/usr/share/tftp/ipxe.efi"
if [[ ! -f "$dst" ]]; then
  $curl -s -o "$dst" http://boot.ipxe.org/ipxe.efi
fi

packages=$(xargs < packages)

# imagebuilder settings
export BIN_DIR="."
export FILES="files"
export DISABLED_SERVICES="dropbear" # using openssh-server instead
export PACKAGES=$packages
export PROFILE=$profile

mkdir -p "$build_dir/files/www"
ln -fs /mnt/data/www/ "$build_dir/files/www/static"

make -C "$build_dir" image PACKAGES="$PACKAGES"
