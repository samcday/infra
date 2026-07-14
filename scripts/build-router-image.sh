#!/bin/bash
set -ueo pipefail

config_dir=$1
platform=$2
target=$3
profile=$4

openwrt_version="24.10.2"

script_dir=$(cd "$(dirname "$0")" && pwd)
root_dir=$(dirname "$script_dir")
common_dir="$root_dir/common/router"
curl="curl --retry 2 --fail"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

read_single_record() {
  local record_file=$1
  local -a records=()

  mapfile -t records < "$record_file"
  ((${#records[@]} == 1)) ||
    die "$record_file must contain exactly one non-empty record"
  [[ -n "${records[0]}" ]] ||
    die "$record_file must contain exactly one non-empty record"
  record=${records[0]}
}

checksum_matches() {
  local expected_sha256=$1
  local path=$2

  [[ -f "$path" ]] && printf '%s  %s\n' "$expected_sha256" "$path" | sha256sum --status -c -
}

download_verified() {
  local url=$1
  local expected_sha256=$2
  local destination=$3
  local download_path="${destination}.tmp-${expected_sha256}"

  if checksum_matches "$expected_sha256" "$destination"; then
    printf '%s  %s\n' "$expected_sha256" "$destination" | sha256sum -c -
    return
  fi

  if [[ -f "$destination" ]]; then
    echo "refreshing stale cached $destination"
  else
    echo "downloading $url"
  fi

  if ! checksum_matches "$expected_sha256" "$download_path"; then
    if ! $curl --continue-at - -L -o "$download_path" "$url"; then
      # A complete or otherwise unusable partial cannot be resumed. Keep it for
      # inspection and download a fresh candidate alongside it.
      download_path="${download_path}.$$"
      $curl -L -o "$download_path" "$url"
    fi
  fi

  printf '%s  %s\n' "$expected_sha256" "$download_path" | sha256sum -c -
  # The old final remains in place until its verified replacement is ready.
  mv -f "$download_path" "$destination"
  printf '%s  %s\n' "$expected_sha256" "$destination" | sha256sum -c -
}

read_package_file() {
  local package_file=$1
  local line package extra
  local result=

  while IFS= read -r line || [[ -n "$line" ]]; do
    line=${line#"${line%%[![:space:]]*}"}
    line=${line%"${line##*[![:space:]]}"}
    [[ -z "$line" || "$line" == \#* ]] && continue
    read -r package extra <<< "$line"
    [[ "$package" =~ ^[A-Za-z0-9][A-Za-z0-9.+_-]*$ && -z "${extra:-}" ]] ||
      die "invalid package record in $package_file: $line"
    result+=" $package"
  done < "$package_file"

  [[ -n "$result" ]] || die "package file contains no packages: $package_file"
  printf '%s' "${result# }"
}

if [[ ! -d $config_dir ]]; then
  echo "ERROR: $config_dir does not exist"
  exit 1
fi

if [[ -f "$config_dir/openwrt-version" ]]; then
  read_single_record "$config_dir/openwrt-version"
  read -r openwrt_version extra <<< "$record"
  if [[ ! "$openwrt_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ || -n "${extra:-}" ]]; then
    echo "ERROR: $config_dir/openwrt-version must contain exactly one semantic release version"
    exit 1
  fi
fi

build_topdir="$root_dir/_build"
lock_dir="$build_topdir/.locks"
mkdir -p "$build_topdir" "$lock_dir"

build_dir="$build_topdir/${config_dir}/$platform/$target/$profile"
tarball=openwrt-imagebuilder-$openwrt_version-$platform-$target.Linux-x86_64.tar.zst
imagebuilder_sha256=""

if [[ -f "$config_dir/imagebuilder-sha256" ]]; then
  read_single_record "$config_dir/imagebuilder-sha256"
  read -r imagebuilder_sha256 pinned_tarball extra <<< "$record"
  if [[ ! "$imagebuilder_sha256" =~ ^[0-9a-f]{64}$ || "$pinned_tarball" != "$tarball" || -n "${extra:-}" ]]; then
    echo "ERROR: $config_dir/imagebuilder-sha256 must contain '<sha256>  $tarball'"
    exit 1
  fi
else
  echo "WARNING: $config_dir does not pin the OpenWrt ImageBuilder checksum" >&2
fi

exec {imagebuilder_lock_fd}> "$lock_dir/$tarball.lock"
flock "$imagebuilder_lock_fd"

imagebuilder_url="https://downloads.openwrt.org/releases/$openwrt_version/targets/$platform/$target/$tarball"
if [[ -n "$imagebuilder_sha256" ]]; then
  download_verified "$imagebuilder_url" "$imagebuilder_sha256" "$build_topdir/$tarball"
elif [[ ! -f "$build_topdir/$tarball" ]]; then
  $curl --continue-at - -o "$build_topdir/$tarball.tmp" -L "$imagebuilder_url"
  mv "$build_topdir/$tarball.tmp" "$build_topdir/$tarball"
fi
read -r actual_imagebuilder_sha256 _ < <(sha256sum "$build_topdir/$tarball")
[[ "$actual_imagebuilder_sha256" =~ ^[0-9a-f]{64}$ ]] ||
  die "could not checksum cached ImageBuilder: $build_topdir/$tarball"
flock -u "$imagebuilder_lock_fd"

mkdir -p "$build_dir"
exec {build_lock_fd}> "$build_dir/.build.lock"
flock "$build_lock_fd"

# Preserve same-ImageBuilder incremental state, but never overlay one OpenWrt
# release archive on generated state from another. The lock file itself must
# remain in place while the reset happens so another build cannot enter.
build_marker="$build_dir/.imagebuilder-source"
build_source_changed=false
if ! printf '%s  %s\n' "$actual_imagebuilder_sha256" "$tarball" | cmp --silent - "$build_marker"; then
  echo "resetting $build_dir for $tarball"
  find "$build_dir" -mindepth 1 ! -path "$build_dir/.build.lock" -delete
  build_source_changed=true
fi

# The overlay is always regenerated from a pristine files directory.
rm -rf "$build_dir/files"

tar --strip-components=1 -C "${build_dir}" --zstd -xf "$build_topdir/$tarball"

if $build_source_changed; then
  build_marker_tmp="${build_marker}.tmp.$$"
  printf '%s  %s\n' "$actual_imagebuilder_sha256" "$tarball" > "$build_marker_tmp"
  mv -f -- "$build_marker_tmp" "$build_marker"
fi

cp -a "$common_dir/files/." "$build_dir/files/"

while IFS= read -r -d '' f
do
  mkdir -p "$(dirname "$build_dir/$f")"
  cp "$config_dir/$f" "$build_dir/$f"
done < <(cd "$config_dir" && find files/ -type f -print0)

if [[ -d "$config_dir/files.enc" ]]; then
  while IFS= read -r -d '' f
  do
    dst=${f/files.enc/files}
    mkdir -p "$(dirname "$build_dir/$dst")"
    decrypted_tmp="$build_dir/$dst.tmp.$$"
    rm -f -- "$decrypted_tmp"
    if ! sops -d "$config_dir/$f" > "$decrypted_tmp"; then
      rm -f -- "$decrypted_tmp"
      die "could not decrypt router overlay file: $config_dir/$f"
    fi
    # SOPS protects content, not destination metadata. Preserve executable bits
    # and other reviewed source modes, then enforce the fabric credential
    # directory independently because Git does not retain mode 0600.
    chmod --reference="$config_dir/$f" "$decrypted_tmp"
    case $dst in
      files/etc/fabric/*) chmod 0600 "$decrypted_tmp" ;;
    esac
    mv -f -- "$decrypted_tmp" "$build_dir/$dst"
  done < <(cd "$config_dir" && find files.enc/ -type f -print0)
fi

# A target overlay can deliberately subtract unsafe common hooks. Entries are
# paths relative to the overlay root and must stay beneath files/. Process this
# after both overlays so a deletion cannot be undone by the common copy.
if [[ -f "$config_dir/remove-files" ]]; then
  while IFS= read -r removed_file || [[ -n "$removed_file" ]]; do
    [[ -z "$removed_file" || "$removed_file" == \#* ]] && continue
    [[ "$removed_file" =~ ^files/([A-Za-z0-9._+-]+/)*[A-Za-z0-9._+-]+$ ]] ||
      die "unsafe path in $config_dir/remove-files: $removed_file"
    rm -f -- "$build_dir/$removed_file"
  done < "$config_dir/remove-files"
fi

if [[ ! -f "$config_dir/disable-ipxe" ]]; then
  mkdir -p "$build_dir/_ipxe" "$build_dir/files/usr/share/tftp"
  ipxe_dir="$build_dir/_ipxe/"

  # These binaries execute before the installed OS and are part of the root of
  # trust. Never accept an unpinned mutable download, even over HTTPS.
  while read -r filename; do
    read -r url
    read -r sha256
    read -r _ || true

    [[ "$filename" == "ipxe.efi" || "$filename" == "undionly.kpxe" ]]
    [[ "$sha256" =~ ^[0-9a-f]{64}$ ]]

    download_verified "$url" "$sha256" "$ipxe_dir/$filename"
  done < "$common_dir/ipxe-files.txt"

  cp "$ipxe_dir/ipxe.efi" "$build_dir/files/usr/share/tftp/"
  cp "$ipxe_dir/undionly.kpxe" "$build_dir/files/usr/share/tftp/"
  cp "$ipxe_dir/undionly.kpxe" "$build_dir/files/usr/share/tftp/undionly.kpxe.0"
else
  # Profiles that categorically disable PXE should not carry mutable upstream
  # pre-OS binaries merely because the common router overlay supports them.
  rm -rf -- "$build_dir/_ipxe" "$build_dir/files/usr/share/tftp"
fi

packages=$(read_package_file "$common_dir/packages")
if [[ -f "$config_dir/packages" ]]; then
  packages+=" $(read_package_file "$config_dir/packages")"
fi
if [[ -f "$config_dir/remove-packages" ]]; then
  declare -A removed_packages=()
  declare -a removed_package_list=()
  while read -r package || [[ -n "$package" ]]; do
    [[ -z "$package" || "$package" == \#* ]] && continue
    [[ "$package" =~ ^[A-Za-z0-9][A-Za-z0-9.+_-]*$ ]] ||
      die "invalid package name in $config_dir/remove-packages: $package"
    [[ -z "${removed_packages[$package]:-}" ]] ||
      die "duplicate package in $config_dir/remove-packages: $package"
    removed_packages["$package"]=1
    removed_package_list+=("$package")
  done < "$config_dir/remove-packages"

  filtered_packages=""
  for package in $packages; do
    [[ -n "${removed_packages[$package]:-}" ]] && continue
    filtered_packages+=" $package"
  done
  packages=${filtered_packages# }

  # Removing a name from the explicit common list is insufficient when a
  # profile or another package can pull it back in. ImageBuilder's negative
  # package syntax makes the exclusion authoritative.
  for package in "${removed_package_list[@]}"; do
    packages+=" -$package"
  done
fi

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

if [[ -f "$config_dir/disabled-services" ]]; then
  DISABLED_SERVICES+=" $(xargs < "$config_dir/disabled-services")"
fi

mkdir -p "$build_dir/files/www"
ln -fs /mnt/data/www/ "$build_dir/files/www/static"

make -C "$build_dir" image PACKAGES="$PACKAGES"

if [[ -f "$config_dir/sysupgrade-sha256" ]]; then
  read_single_record "$config_dir/sysupgrade-sha256"
  read -r expected_sysupgrade_sha256 sysupgrade_filename extra <<< "$record"
  [[ "$expected_sysupgrade_sha256" =~ ^[0-9a-f]{64}$ &&
     "$sysupgrade_filename" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*$ &&
     -z "${extra:-}" ]] ||
    die "$config_dir/sysupgrade-sha256 must contain one '<sha256>  <artifact-basename>' record"
  [[ "$sysupgrade_filename" == "openwrt-$openwrt_version-$platform-$target-"* ]] ||
    die "$config_dir/sysupgrade-sha256 pins an artifact for a different release or target"

  sysupgrade_path="$build_dir/bin/targets/$platform/$target/$sysupgrade_filename"
  [[ -f "$sysupgrade_path" && ! -L "$sysupgrade_path" ]] ||
    die "pinned sysupgrade artifact is missing or not a regular file: $sysupgrade_path"
  printf '%s  %s\n' "$expected_sysupgrade_sha256" "$sysupgrade_path" | sha256sum -c - ||
    die "generated sysupgrade differs from the reviewed pin: $config_dir/sysupgrade-sha256"

  sysupgrade_manifest=${sysupgrade_filename%-squashfs-sysupgrade.itb}.manifest
  [[ "$sysupgrade_manifest" != "$sysupgrade_filename" ]] ||
    die 'pinned sysupgrade artifact must end in -squashfs-sysupgrade.itb'
  sysupgrade_manifest_path="$build_dir/bin/targets/$platform/$target/$sysupgrade_manifest"
  [[ -f "$sysupgrade_manifest_path" && ! -L "$sysupgrade_manifest_path" ]] ||
    die "generated package manifest is missing or not a regular file: $sysupgrade_manifest_path"

  if [[ -f "$config_dir/remove-packages" ]]; then
    for package in "${removed_package_list[@]}"; do
      if awk -v package="$package" '$1 == package { found = 1 } END { exit !found }' \
          "$sysupgrade_manifest_path"; then
        die "removed package remains in generated image: $package"
      fi
    done
  fi
else
  echo "WARNING: $config_dir does not pin a generated sysupgrade artifact" >&2
fi
flock -u "$build_lock_fd"

if [[ -f $config_dir/data-files.txt ]]; then
  data_dir="$build_topdir/data"

  mkdir -p "$data_dir"
  exec {data_lock_fd}> "$lock_dir/data.lock"
  flock "$data_lock_fd"

  declare -a data_filenames=()
  declare -a data_sources=()
  declare -a data_hashes=()
  declare -A seen_data_files=()
  record_number=0
  while IFS= read -r filename; do
    ((record_number += 1))
    IFS= read -r url || die "missing source for data record $record_number: $filename"
    IFS= read -r sha256 || die "missing SHA256 for data record $record_number: $filename"
    # The separator after the final record is optional.
    separator=""
    IFS= read -r separator || true

    [[ "$filename" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] ||
      die "unsafe filename in data record $record_number: $filename"
    [[ -n "$url" && "$url" != *[[:space:]]* ]] ||
      die "invalid source in data record $record_number: $filename"
    [[ "$url" == https://* || "$url" == oci://* ]] ||
      die "unsupported source scheme in data record $record_number: $filename"
    [[ "$sha256" =~ ^[0-9a-f]{64}$ ]] ||
      die "invalid SHA256 in data record $record_number: $filename"
    [[ -z "$separator" ]] ||
      die "expected blank separator after data record $record_number: $filename"
    [[ -z "${seen_data_files[$filename]:-}" ]] ||
      die "duplicate data filename: $filename"
    seen_data_files[$filename]=1

    data_filenames+=("$filename")
    data_sources+=("$url")
    data_hashes+=("$sha256")
  done <"$config_dir/data-files.txt"
  ((${#data_filenames[@]} > 0)) || die "data manifest is empty: $config_dir/data-files.txt"

  shasums_tmp=$(mktemp "$data_dir/.SHASUMS.txt.tmp.XXXXXXXX")
  cleanup_shasums_tmp() {
    [[ -z "${shasums_tmp:-}" ]] || rm -f -- "$shasums_tmp"
  }
  trap cleanup_shasums_tmp EXIT

  # Build the complete manifest alongside the published one. A late download
  # or image-stage failure must leave the last known-complete manifest intact.
  for ((index = 0; index < ${#data_filenames[@]}; index++)); do
    filename=${data_filenames[$index]}
    sha256=${data_hashes[$index]}
    printf '%s  %s\n' "$sha256" "$filename" >> "$shasums_tmp"
  done

  for ((index = 0; index < ${#data_filenames[@]}; index++)); do
    filename=${data_filenames[$index]}
    url=${data_sources[$index]}
    sha256=${data_hashes[$index]}

    if [[ "$url" == oci://* ]]; then
      "$root_dir/scripts/stage-fabric-airgap-images" \
        --filename "$filename" \
        --output-dir "$data_dir"
    else
      download_verified "$url" "$sha256" "$data_dir/$filename"
    fi
    printf '%s  %s\n' "$sha256" "$data_dir/$filename" | sha256sum -c -
  done

  (
    cd "$data_dir"
    sha256sum -c "$shasums_tmp"
  )
  mv -f -- "$shasums_tmp" "$data_dir/SHASUMS.txt"
  shasums_tmp=
  trap - EXIT

  (
    cd "$data_dir"
    sha256sum -c SHASUMS.txt
  )
  flock -u "$data_lock_fd"
fi
