#!/usr/bin/env bash
set -euo pipefail

umask 077

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck disable=SC1091
source "$script_dir/../../scripts/lib/fabric-secure-tempdir.sh"
workdir=$(fabric_secure_tmpdir fabric-butane-bootstrap 16777216)
trap 'rm -rf -- "$workdir"' EXIT
cp -a -- "$script_dir/files" "$workdir/files"

nodes=(fabric-az1-cp1 fabric-az1-cp2 fabric-az1-cp3)
common_profiles=(base firewall time etcd control-plane node-exporter observer-agent)
check_only=false

usage() {
  cat >&2 <<'EOF'
Usage:
  FABRIC_CP1_MAC=aa:bb:cc:dd:ee:01 \
  FABRIC_CP2_MAC=aa:bb:cc:dd:ee:02 \
  FABRIC_CP3_MAC=aa:bb:cc:dd:ee:03 \
    ./bootstrap.sh OUTPUT_DIRECTORY

  ./bootstrap.sh --check

The normal mode refuses plaintext source profiles, unresolved secret
placeholders, invalid MAC addresses, and existing output files.  Output
Ignition files contain recovery keys, cluster tokens, and private keys.
EOF
}

case $# in
  1)
    if [[ $1 == --check ]]; then
      check_only=true
      output_dir=
    else
      output_dir=$1
    fi
    ;;
  *)
    usage
    exit 2
    ;;
esac

for command in awk butane sha256sum sort wc yq; do
  command -v "$command" >/dev/null || {
    echo "$command is required" >&2
    exit 1
  }
done

if ! $check_only; then
  repo_root=$(git -C "$script_dir" rev-parse --show-toplevel)
  output_dir=$(realpath -m -- "$output_dir")
  case $output_dir in
    "$repo_root" | "$repo_root"/*)
      echo "refusing to write sensitive Ignition inside the Git worktree: $output_dir" >&2
      exit 1
      ;;
  esac
fi

decrypt_profile() {
  local source=$1
  local destination=$2

  if grep -q '^sops:' "$source"; then
    command -v sops >/dev/null || {
      echo "sops is required to decrypt $source" >&2
      exit 1
    }
    sops --decrypt "$source" >"$destination"
  elif $check_only; then
    cp -- "$source" "$destination"
  else
    echo "refusing plaintext secret-bearing profile: $source" >&2
    echo "replace its FABRIC_REPLACE_* values and SOPS-encrypt it first" >&2
    exit 1
  fi
}

validate_mac() {
  local name=$1
  local mac=$2
  local first_octet

  if [[ ! $mac =~ ^([[:xdigit:]]{2}:){5}[[:xdigit:]]{2}$ ]]; then
    echo "$name is not a six-octet MAC address: $mac" >&2
    exit 1
  fi

  if [[ $mac == 00:00:00:00:00:00 ]]; then
    echo "$name is the all-zero placeholder and cannot identify a node NIC" >&2
    exit 1
  fi

  first_octet=${mac%%:*}
  if (( (16#$first_octet & 1) != 0 )); then
    echo "$name is multicast and cannot identify a node NIC: $mac" >&2
    exit 1
  fi
}

if $check_only; then
  cp1_mac=02:00:00:00:00:10
  cp2_mac=02:00:00:00:00:11
  cp3_mac=02:00:00:00:00:12
else
  : "${FABRIC_CP1_MAC:?set FABRIC_CP1_MAC from the inventory output}"
  : "${FABRIC_CP2_MAC:?set FABRIC_CP2_MAC from the inventory output}"
  : "${FABRIC_CP3_MAC:?set FABRIC_CP3_MAC from the inventory output}"
  cp1_mac=${FABRIC_CP1_MAC^^}
  cp2_mac=${FABRIC_CP2_MAC^^}
  cp3_mac=${FABRIC_CP3_MAC^^}
fi

validate_mac FABRIC_CP1_MAC "$cp1_mac"
validate_mac FABRIC_CP2_MAC "$cp2_mac"
validate_mac FABRIC_CP3_MAC "$cp3_mac"

if [[ $cp1_mac == "$cp2_mac" || $cp1_mac == "$cp3_mac" || $cp2_mac == "$cp3_mac" ]]; then
  echo "the three consensus nodes must have distinct permanent wired MAC addresses" >&2
  exit 1
fi

for profile in "${common_profiles[@]}"; do
  case $profile in
    firewall | node-exporter | time)
      # These profiles contain only public policy and pinned public assets.
      # Keep the normal-mode plaintext exception explicit so a future secret
      # profile cannot silently bypass the SOPS boundary.
      cp -- "$script_dir/$profile.yaml" "$workdir/$profile.yaml"
      ;;
    *)
      decrypt_profile "$script_dir/$profile.yaml" "$workdir/$profile.yaml"
      ;;
  esac
  butane --strict --files-dir "$workdir" --output "$workdir/$profile.ign" "$workdir/$profile.yaml"
done

if ! grep -Fq 'Environment=CLUSTER_STATE=new' "$workdir/etcd.yaml"; then
  echo "etcd.yaml no longer declares the fresh-cluster state; refusing bootstrap" >&2
  exit 1
fi

recovery_key_hashes=$workdir/recovery-key-hashes
: > "$recovery_key_hashes"

for index in "${!nodes[@]}"; do
  node=${nodes[$index]}
  case $index in
    0) mac=$cp1_mac ;;
    1) mac=$cp2_mac ;;
    2) mac=$cp3_mac ;;
  esac

  decrypt_profile "$script_dir/$node.yaml" "$workdir/$node.yaml"
  mapfile -t recovery_keys < <(yq -r '
    .storage.luks[].key_file.inline,
    (.storage.files[] | select(.path == "/etc/fabric/root-recovery.key") | .contents.inline)
  ' "$workdir/$node.yaml")
  ((${#recovery_keys[@]} == 3)) || {
    echo "$node must carry exactly root, etcd, and var recovery keys" >&2
    exit 1
  }
  for recovery_key in "${recovery_keys[@]}"; do
    [[ $recovery_key =~ ^[0-9a-f]{64}$ ]] || {
      echo "$node contains a malformed LUKS recovery key" >&2
      exit 1
    }
    printf '%s' "$recovery_key" | sha256sum | awk '{ print $1 }' >> "$recovery_key_hashes"
  done
  sed -i "s/@FABRIC_NODE_MAC@/$mac/g" "$workdir/$node.yaml"
  butane --strict --files-dir "$workdir" --output "$workdir/$node.ign" "$workdir/$node.yaml"

  if ! $check_only && grep -R -E -q 'FABRIC_REPLACE_|@FABRIC_NODE_MAC@' "$workdir"; then
    echo "unresolved secret or hardware placeholder remains while rendering $node" >&2
    exit 1
  fi

  cat >"$workdir/$node-final.yaml" <<EOF
variant: fcos
version: 1.5.0
ignition:
  config:
    merge:
      - local: base.ign
      - local: firewall.ign
      - local: time.ign
      - local: etcd.ign
      - local: control-plane.ign
      - local: node-exporter.ign
      - local: observer-agent.ign
      - local: $node.ign
EOF

  butane --strict --files-dir "$workdir" --output "$workdir/$node-final.ign" "$workdir/$node-final.yaml"

  if $check_only; then
    echo "validated $node initial Ignition" >&2
  fi
done

[[ $(wc -l < "$recovery_key_hashes") -eq 9 &&
   $(sort -u "$recovery_key_hashes" | wc -l) -eq 9 ]] || {
  echo 'the nine root, etcd, and var recovery keys must all be distinct' >&2
  exit 1
}

if ! $check_only; then
  for node in "${nodes[@]}"; do
    destination=$output_dir/$node.ign
    if [[ -e $destination ]]; then
      echo "refusing to overwrite existing output: $destination" >&2
      exit 1
    fi
  done

  mkdir -p -- "$output_dir"
  chmod 0700 -- "$output_dir"
  for node in "${nodes[@]}"; do
    destination=$output_dir/$node.ign
    install -m 0600 "$workdir/$node-final.ign" "$destination"
    sha256sum "$destination"
  done

  echo "wrote three sensitive bootstrap Ignitions to $output_dir" >&2
  echo "boot at least two members together; all three declare initial-cluster-state=new" >&2
fi
