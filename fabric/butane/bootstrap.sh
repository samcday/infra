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
mac_variables=(FABRIC_CP1_MAC FABRIC_CP2_MAC FABRIC_CP3_MAC)
common_profiles=(base firewall time etcd control-plane node-exporter observer-agent)
check_only=false
selected_node=

usage() {
  cat >&2 <<'EOF'
Usage:
  FABRIC_CP1_MAC="$CP1_INVENTORY_MAC" \
  FABRIC_CP2_MAC="$CP2_INVENTORY_MAC" \
  FABRIC_CP3_MAC="$CP3_INVENTORY_MAC" \
    ./bootstrap.sh OUTPUT_DIRECTORY

  FABRIC_CP1_MAC="$CP1_INVENTORY_MAC" \
    ./bootstrap.sh --node fabric-az1-cp1 OUTPUT_DIRECTORY

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
  3)
    if [[ $1 != --node ]]; then
      usage
      exit 2
    fi
    selected_node=$2
    output_dir=$3
    case $selected_node in
      fabric-az1-cp1 | fabric-az1-cp2 | fabric-az1-cp3) ;;
      *)
        echo "unknown fabric consensus node: $selected_node" >&2
        usage
        exit 2
        ;;
    esac
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
  if (( (16#$first_octet & 2) != 0 )); then
    echo "$name is locally administered and cannot identify a node NIC: $mac" >&2
    exit 1
  fi
}

declare -A node_macs=()
if $check_only; then
  check_macs=(00:00:5E:00:53:10 00:00:5E:00:53:11 00:00:5E:00:53:12)
  for index in "${!nodes[@]}"; do
    node=${nodes[$index]}
    mac_variable=${mac_variables[$index]}
    validate_mac "$mac_variable check identity" "${check_macs[$index]}"
    node_macs[$node]=${check_macs[$index]}
  done
else
  for index in "${!nodes[@]}"; do
    node=${nodes[$index]}
    mac_variable=${mac_variables[$index]}
    if [[ -n $selected_node && $node != "$selected_node" ]]; then
      continue
    fi
    if [[ ! -v $mac_variable || -z ${!mac_variable} ]]; then
      echo "set $mac_variable from the inventory output" >&2
      exit 1
    fi
    mac=${!mac_variable}
    mac=${mac^^}
    validate_mac "$mac_variable" "$mac"
    node_macs[$node]=$mac
  done

fi

if [[ -z $selected_node &&
      (${node_macs[${nodes[0]}]} == "${node_macs[${nodes[1]}]}" ||
       ${node_macs[${nodes[0]}]} == "${node_macs[${nodes[2]}]}" ||
       ${node_macs[${nodes[1]}]} == "${node_macs[${nodes[2]}]}") ]]; then
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
expected_initial_cluster='--initial-cluster=fabric-az1-cp1=https://fabric-az1-cp1.fabric.internal:2380,fabric-az1-cp2=https://fabric-az1-cp2.fabric.internal:2380,fabric-az1-cp3=https://fabric-az1-cp3.fabric.internal:2380'
initial_cluster_records=$(grep -Fc -- "$expected_initial_cluster" "$workdir/etcd.yaml" || true)
if [[ $initial_cluster_records -ne 1 ]]; then
  echo "etcd.yaml must retain the fixed three-member initial cluster map" >&2
  exit 1
fi

quorum_wait_path=/usr/local/sbin/wait-for-fabric-etcd-quorum
quorum_wait_records=$(yq -r '
  [.storage.files[]
    | select(.path == "/usr/local/sbin/wait-for-fabric-etcd-quorum")]
  | length
' "$workdir/control-plane.yaml")
[[ $quorum_wait_records == 1 ]] || {
  echo 'control-plane.yaml must carry exactly one local etcd-quorum wait helper' >&2
  exit 1
}
quorum_wait_script=$workdir/wait-for-fabric-etcd-quorum
yq -r '
  .storage.files[]
  | select(.path == "/usr/local/sbin/wait-for-fabric-etcd-quorum")
  | .contents.inline
' "$workdir/control-plane.yaml" >"$quorum_wait_script"
bash -n "$quorum_wait_script"
if ! grep -Fq 'metrics_url=http://127.0.0.1:2381/metrics' "$quorum_wait_script" ||
  ! grep -Fq 'etcd_server_has_leader' "$quorum_wait_script"; then
  echo 'the K3s start gate must query the local etcd leader metric' >&2
  exit 1
fi
quorum_dropin_records=$(yq -r '
  [.systemd.units[]
    | select(.name == "k3s.service")
    | .dropins[]
    | select(.name == "20-etcd-quorum.conf")]
  | length
' "$workdir/control-plane.yaml")
[[ $quorum_dropin_records == 1 ]] || {
  echo 'k3s.service must carry exactly one etcd-quorum start-gate drop-in' >&2
  exit 1
}
yq -r '
  .systemd.units[]
  | select(.name == "k3s.service")
  | .dropins[]
  | select(.name == "20-etcd-quorum.conf")
  | .contents
' "$workdir/control-plane.yaml" |
  grep -Fxq "ExecStartPre=$quorum_wait_path" || {
  echo 'k3s.service does not execute the reviewed etcd-quorum helper' >&2
  exit 1
}

recovery_key_hashes=$workdir/recovery-key-hashes
: > "$recovery_key_hashes"

for node in "${nodes[@]}"; do
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
  mac_placeholder_count=$(awk '{ count += gsub(/@FABRIC_NODE_MAC@/, "") } END { print count + 0 }' "$workdir/$node.yaml")
  if [[ $mac_placeholder_count -ne 1 ]]; then
    echo "$node must carry exactly one wired MAC placeholder" >&2
    exit 1
  fi
done

[[ $(wc -l < "$recovery_key_hashes") -eq 9 &&
   $(sort -u "$recovery_key_hashes" | wc -l) -eq 9 ]] || {
  echo 'the nine root, etcd, and var recovery keys must all be distinct' >&2
  exit 1
}

if ! $check_only && grep -E -q 'FABRIC_REPLACE_' "$workdir"/*.yaml; then
  echo "unresolved secret placeholder remains in the bootstrap profiles" >&2
  exit 1
fi

render_nodes=("${nodes[@]}")
if [[ -n $selected_node ]]; then
  render_nodes=("$selected_node")
fi

for node in "${render_nodes[@]}"; do
  cp -- "$workdir/$node.yaml" "$workdir/$node-render.yaml"
  sed -i "s/@FABRIC_NODE_MAC@/${node_macs[$node]}/g" "$workdir/$node-render.yaml"
  if grep -q '@FABRIC_NODE_MAC@' "$workdir/$node-render.yaml"; then
    echo "unresolved hardware placeholder remains while rendering $node" >&2
    exit 1
  fi
  if ! $check_only && grep -q 'FABRIC_REPLACE_' "$workdir/$node-render.yaml"; then
    echo "unresolved secret placeholder remains while rendering $node" >&2
    exit 1
  fi
  butane --strict --files-dir "$workdir" --output "$workdir/$node.ign" "$workdir/$node-render.yaml"

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

if ! $check_only; then
  for node in "${render_nodes[@]}"; do
    destination=$output_dir/$node.ign
    if [[ -e $destination ]]; then
      echo "refusing to overwrite existing output: $destination" >&2
      exit 1
    fi
  done

  mkdir -p -- "$output_dir"
  chmod 0700 -- "$output_dir"
  for node in "${render_nodes[@]}"; do
    destination=$output_dir/$node.ign
    install -m 0600 "$workdir/$node-final.ign" "$destination"
    sha256sum "$destination"
  done

  if [[ -n $selected_node ]]; then
    echo "wrote the sensitive bootstrap Ignition for $selected_node to $output_dir" >&2
  else
    echo "wrote three sensitive bootstrap Ignitions to $output_dir" >&2
  fi
  echo "boot at least two members together; all three declare initial-cluster-state=new" >&2
fi
