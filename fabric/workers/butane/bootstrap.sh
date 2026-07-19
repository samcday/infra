#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C
umask 077

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(git -C "$script_dir" rev-parse --show-toplevel)
# shellcheck disable=SC1091
source "$repo_root/scripts/lib/fabric-secure-tempdir.sh"
# shellcheck disable=SC1091
source "$repo_root/fabric/workers/lib/admission.sh"

nodes=(fabric-az1-svc1 fabric-az1-svc2)
common_profiles=(base firewall time k3s-agent)
check_only=false
selected_node=
output_dir=

usage() {
  cat >&2 <<'EOF'
Usage:
  fabric/workers/butane/bootstrap.sh --check

  FABRIC_AGENT_TOKEN_FILE=/secure/fabric/agent.token \
    fabric/workers/butane/bootstrap.sh \
      --node fabric-az1-svc1 OUTPUT_DIRECTORY

Normal mode renders exactly one admitted services worker. A full CA-pinned K3s
agent or bootstrap token must be supplied through a mode-0600 regular file
outside Git. The resulting Ignition is secret and the output directory must
also be outside Git.
EOF
}

case $# in
  1)
    [[ $1 == --check ]] || { usage; exit 2; }
    check_only=true
    ;;
  3)
    [[ $1 == --node ]] || { usage; exit 2; }
    selected_node=$2
    output_dir=$3
    case $selected_node in
      fabric-az1-svc1 | fabric-az1-svc2) ;;
      *) echo "unsupported fabric worker: $selected_node" >&2; exit 2 ;;
    esac
    ;;
  *)
    usage
    exit 2
    ;;
esac

for command in awk butane cp find git grep install jq mkdir realpath sed sha256sum stat tail wc yq; do
  command -v "$command" >/dev/null || {
    echo "missing required command: $command" >&2
    exit 1
  }
done

workdir=$(fabric_secure_tmpdir fabric-worker-butane 16777216)
trap 'find "$workdir" -mindepth 1 -delete 2>/dev/null || true' EXIT

agent_token=
if $check_only; then
  agent_token=K10$(printf '0%.0s' {1..64})::node:$(printf 'a%.0s' {1..64})
else
  token_input=${FABRIC_AGENT_TOKEN_FILE:-}
  [[ -n $token_input ]] || {
    echo 'FABRIC_AGENT_TOKEN_FILE is required in normal mode' >&2
    exit 1
  }
  [[ -f $token_input && ! -L $token_input ]] || {
    echo 'agent-token input must be a regular non-symlink' >&2
    exit 1
  }
  token_file=$(realpath -e -- "$token_input")
  case $token_file in
    "$repo_root" | "$repo_root"/*)
      echo "refusing an agent-token file inside Git: $token_file" >&2
      exit 1
      ;;
  esac
  [[ $(stat -Lc '%a' -- "$token_file") == 600 ]] || {
    echo 'agent-token input mode must be exactly 0600' >&2
    exit 1
  }
  [[ $(wc -l < "$token_file") == 1 && $(tail -c 1 "$token_file" | wc -l) == 1 ]] || {
    echo 'agent-token input must contain exactly one newline-terminated record' >&2
    exit 1
  }
  agent_token=$(<"$token_file")
fi

if [[ ! $agent_token =~ ^K10[0-9a-f]{64}::node:[A-Za-z0-9]{16,512}$ &&
      ! $agent_token =~ ^K10[0-9a-f]{64}::[a-z0-9]{6}\.[a-z0-9]{16}$ ]]; then
  echo 'agent token is not a CA-pinned K3s agent or bootstrap token' >&2
  exit 1
fi

cp -- "$script_dir/base.yaml" "$workdir/base.yaml"
[[ $(grep -o '@FABRIC_AGENT_TOKEN@' "$workdir/base.yaml" | wc -l) == 1 ]] || {
  echo 'base profile must contain exactly one agent-token placeholder' >&2
  exit 1
}
token_sed=$workdir/replace-agent-token.sed
# printf is a shell builtin, so the credential never becomes an external
# process argument or environment value.  sed receives only the path to this
# protected program in tmpfs.
printf 's|@FABRIC_AGENT_TOKEN@|%s|\n' "$agent_token" >"$token_sed"
chmod 0600 "$token_sed"
unset agent_token
sed --file="$token_sed" --in-place "$workdir/base.yaml"
find "$token_sed" -maxdepth 0 -type f -delete
for profile in firewall time k3s-agent; do
  cp -- "$script_dir/$profile.yaml" "$workdir/$profile.yaml"
done

for profile in "${common_profiles[@]}"; do
  butane --strict --files-dir "$workdir" \
    --output "$workdir/$profile.ign" "$workdir/$profile.yaml"
done

render_nodes=("${nodes[@]}")
if [[ -n $selected_node ]]; then
  render_nodes=("$selected_node")
fi

for node in "${render_nodes[@]}"; do
  admission=$repo_root/fabric/workers/inventory/$node.yaml
  cp -- "$script_dir/$node.yaml" "$workdir/$node.yaml"

  if $check_only; then
    admission_state=$(yq -r '.state' "$admission")
    if [[ $admission_state == pending ]]; then
      fabric_worker_validate_pending_record "$node" "$admission"
      case $node in
        fabric-az1-svc1) node_mac=00:00:5e:00:53:71 ;;
        fabric-az1-svc2) node_mac=00:00:5e:00:53:72 ;;
      esac
    elif [[ $admission_state == admitted ]]; then
      fabric_worker_load_admission "$node" "$admission"
      node_mac=$FABRIC_WORKER_MAC
    else
      echo "$node admission state is neither pending nor admitted" >&2
      exit 1
    fi
  else
    fabric_worker_load_admission "$node" "$admission"
    node_mac=$FABRIC_WORKER_MAC
  fi

  [[ $(grep -o '@FABRIC_NODE_MAC@' "$workdir/$node.yaml" | wc -l) == 2 ]] || {
    echo "$node profile must contain exactly two MAC placeholders" >&2
    exit 1
  }
  sed -i "s/@FABRIC_NODE_MAC@/$node_mac/" "$workdir/$node.yaml"
  ignore_carrier=$(yq -er '
    .storage.files[]
    | select(.path == "/etc/NetworkManager/conf.d/90-fabric-services-ignore-carrier.conf")
    | .contents.inline
  ' "$workdir/$node.yaml")
  grep -Fxq "match-device=mac:$node_mac" <<<"$ignore_carrier" || {
    echo "$node ignore-carrier policy is not bound to its admitted MAC" >&2
    exit 1
  }
  butane --strict --files-dir "$workdir" \
    --output "$workdir/$node.ign" "$workdir/$node.yaml"

  cat >"$workdir/$node-final.yaml" <<EOF
variant: fcos
version: 1.5.0
ignition:
  config:
    merge:
      - local: base.ign
      - local: firewall.ign
      - local: time.ign
      - local: k3s-agent.ign
      - local: $node.ign
EOF
  butane --strict --files-dir "$workdir" \
    --output "$workdir/$node-final.ign" "$workdir/$node-final.yaml"

  jq -e '
    .ignition.version | startswith("3.")
  ' "$workdir/$node-final.ign" >/dev/null
  if jq -r '.. | strings' "$workdir/$node-final.ign" |
      grep -Eqi '(^|/)(etc|var/lib)/etcd|kube-vip|server\.token'; then
    echo "$node worker Ignition unexpectedly references root/etcd material" >&2
    exit 1
  fi
  if grep -R -Eqi '(^|/)(etc|var/lib)/etcd|kube-vip|server\.token|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|fabric-root-key|peer-key' \
      "$workdir"/*.yaml "$workdir"/*.ign; then
    echo "$node worker render contains a forbidden private-key marker" >&2
    exit 1
  fi

  if $check_only; then
    echo "validated $admission_state admission and $node worker profile" >&2
  fi
done

if $check_only; then
  exit 0
fi

output_dir=$(realpath -m -- "$output_dir")
case $output_dir in
  "$repo_root" | "$repo_root"/*)
    echo "refusing sensitive worker Ignition output inside Git: $output_dir" >&2
    exit 1
    ;;
esac
destination=$output_dir/$selected_node.ign
checksum=$destination.sha256
[[ ! -e $destination && ! -e $checksum ]] || {
  echo "refusing to overwrite worker Ignition output: $destination" >&2
  exit 1
}
mkdir -p -- "$output_dir"
chmod 0700 -- "$output_dir"
install -m 0600 "$workdir/$selected_node-final.ign" "$destination"
sha256=$(sha256sum "$destination" | awk '{ print $1 }')
printf '%s  %s\n' "$sha256" "${destination##*/}" >"$checksum"
chmod 0600 -- "$checksum"
printf '%s  %s\n' "$sha256" "$destination"
echo "wrote one sensitive admitted-worker Ignition for $selected_node" >&2
