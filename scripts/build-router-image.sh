#!/bin/bash
set -uexo pipefail

root_dir=$1
platform=$2
target=$3
profile=$4

openwrt_version="24.10.2"

curl="curl --retry 2 --fail"

if [[ ! -d $root_dir ]]; then
  echo "ERROR: $root_dir does not exist"
  exit 1
fi

build_topdir="$root_dir/_build"
mkdir -p "$build_topdir"

build_dir="$build_topdir/${platform}-${target}-${openwrt_version}"
tarball=openwrt-imagebuilder-$openwrt_version-$platform-$target.Linux-x86_64.tar.zst

if [[ ! -f "$build_topdir/$tarball" ]]; then
  $curl -o "$build_topdir/$tarball.tmp" -L "https://downloads.openwrt.org/releases/$openwrt_version/targets/$platform/$target/$tarball"
  mv "$build_topdir/$tarball.tmp" "$build_topdir/$tarball"
fi

mkdir -p "$build_dir"

tar --strip-components=1 -C "${build_dir}" --zstd -xvf "$build_topdir/$tarball"

while IFS= read -r -d '' f
do
  mkdir -p "$(dirname "$build_dir/$f")"
  cp "$root_dir/$f" "$build_dir/$f"
done < <(cd "$root_dir" && find files/ -type f -print0)

while IFS= read -r -d '' f
do
  dst=${f/files.enc/files}
  mkdir -p "$(dirname "$build_dir/$dst")"
  sops -d "$root_dir/$f" > "$build_dir/$dst"
done < <(cd "$root_dir" && find files.enc/ -type f -print0)

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

packages=$(xargs < "$root_dir/packages")

# imagebuilder settings
export BIN_DIR="."
export FILES="files"
export DISABLED_SERVICES="dropbear" # using openssh-server instead
export PACKAGES=$packages
if [[ "$profile" != "-" ]]; then
  export PROFILE=$profile
else
  # x64/64 target has a default rootfs partsize that is much too shrimpy
  sed -i "$build_dir/.config" -e "s/CONFIG_TARGET_ROOTFS_PARTSIZE=.*/CONFIG_TARGET_ROOTFS_PARTSIZE=128/"
fi

mkdir -p "$build_dir/files/www"
ln -fs /mnt/data/www/ "$build_dir/files/www/static"

make -C "$build_dir" image PACKAGES="$PACKAGES"

if [[ -f $root_dir/data-files.txt ]]; then
  data_dir="$build_topdir/data"

  mkdir -p "$data_dir"

  echo > "$data_dir/SHASUMS.txt"

  while read -r filename; do
    read -r url
    read -r sha256
    read -r

    echo "$sha256  $filename" >> "$data_dir/SHASUMS.txt"

    if [[ ! -f "$data_dir/$filename" ]]; then
      echo "downloading $url"
      curl --fail --retry 2 -o "$data_dir/$filename" "$url"
    fi
  done <"$root_dir/data-files.txt"

  (
    cd "$data_dir"
    sha256sum -c SHASUMS.txt
  )
fi
