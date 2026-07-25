#!/usr/bin/env bash

# Callers receive these values after fabric_load_node_inventory succeeds.
# shellcheck disable=SC2034
FABRIC_NODE_NAME=
FABRIC_NODE_ROLE=
FABRIC_NODE_ADDRESS=
FABRIC_NODE_MAC=
FABRIC_NODE_DISK=
FABRIC_NODE_SHORT=

fabric_validate_node_pod_cidr() {
  (($# == 1)) || {
    fabric_node_inventory_die 'fabric_validate_node_pod_cidr requires one CIDR'
    return 2
  }

  local pod_cidr=$1 subnet_octet
  [[ $pod_cidr =~ ^172\.22\.([0-9]{1,3})\.0/24$ ]] || {
    fabric_node_inventory_die "invalid Fabric node PodCIDR: $pod_cidr"
    return 1
  }
  subnet_octet=${BASH_REMATCH[1]}
  ((10#$subnet_octet <= 255)) || {
    fabric_node_inventory_die "Fabric node PodCIDR is outside 172.22.0.0/16: $pod_cidr"
    return 1
  }
}

fabric_node_inventory_die() {
  printf 'fabric node inventory: %s\n' "$*" >&2
  return 1
}

fabric_node_inventory_repo_root() {
  local library_dir
  library_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
  git -C "$library_dir" rev-parse --show-toplevel
}

fabric_validate_node_inventory() {
  local repo_root inventory count unique_count role prefix
  repo_root=$(fabric_node_inventory_repo_root) || return 1
  inventory=$repo_root/fabric/inventory/nodes.yaml

  command -v yq >/dev/null || {
    fabric_node_inventory_die 'yq is required'
    return 1
  }
  [[ -f $inventory && ! -L $inventory ]] || {
    fabric_node_inventory_die 'fabric/inventory/nodes.yaml is absent or unsafe'
    return 1
  }
  [[ $(yq -r '.schema' "$inventory") == 1 ]] || {
    fabric_node_inventory_die 'unsupported inventory schema'
    return 1
  }

  count=$(yq -r '.nodes | length' "$inventory") || return 1
  [[ $count == 5 ]] || {
    fabric_node_inventory_die 'inventory must contain exactly five nodes'
    return 1
  }
  for field in name address permanentMac rootDisk; do
    unique_count=$(FIELD="$field" yq -r '[.nodes[] | .[strenv(FIELD)]] | unique | length' "$inventory") ||
      return 1
    [[ $unique_count == "$count" ]] || {
      fabric_node_inventory_die "inventory field is not unique: $field"
      return 1
    }
  done

  while IFS=$'\t' read -r name role address mac disk; do
    [[ $name =~ ^fabric-az1-(cp[123]|svc[12])$ ]] || {
      fabric_node_inventory_die "invalid node name: $name"
      return 1
    }
    case $role in
      control-plane) prefix=10.66.0. ;;
      service) prefix=10.66.1. ;;
      *)
        fabric_node_inventory_die "invalid role for $name: $role"
        return 1
        ;;
    esac
    [[ $address =~ ^${prefix}[0-9]+$ ]] || {
      fabric_node_inventory_die "invalid role address for $name: $address"
      return 1
    }
    [[ $mac =~ ^([0-9a-f]{2}:){5}[0-9a-f]{2}$ && $mac != 00:00:00:00:00:00 ]] || {
      fabric_node_inventory_die "invalid permanent MAC for $name: $mac"
      return 1
    }
    local first_octet=${mac%%:*}
    (( (16#$first_octet & 3) == 0 )) || {
      fabric_node_inventory_die "permanent MAC is multicast or locally administered for $name"
      return 1
    }
    [[ $disk == /dev/disk/by-id/* && $disk != *-part[0-9]* ]] || {
      fabric_node_inventory_die "invalid whole-disk identity for $name: $disk"
      return 1
    }
  done < <(yq -r '.nodes[] | [.name, .role, .address, .permanentMac, .rootDisk] | @tsv' "$inventory")
}

fabric_load_node_inventory() {
  (($# == 1)) || {
    fabric_node_inventory_die 'fabric_load_node_inventory requires NODE'
    return 2
  }

  local node=$1 repo_root inventory record_count record
  fabric_validate_node_inventory || return 1
  repo_root=$(fabric_node_inventory_repo_root) || return 1
  inventory=$repo_root/fabric/inventory/nodes.yaml
  record_count=$(NODE="$node" yq -r '[.nodes[] | select(.name == strenv(NODE))] | length' "$inventory") ||
    return 1
  [[ $record_count == 1 ]] || {
    fabric_node_inventory_die "unknown fabric node: $node"
    return 1
  }
  record=$(NODE="$node" yq -r '.nodes[] | select(.name == strenv(NODE)) | [.name, .role, .address, .permanentMac, .rootDisk] | @tsv' "$inventory") ||
    return 1
  IFS=$'\t' read -r FABRIC_NODE_NAME FABRIC_NODE_ROLE FABRIC_NODE_ADDRESS \
    FABRIC_NODE_MAC FABRIC_NODE_DISK <<<"$record"
  FABRIC_NODE_SHORT=${FABRIC_NODE_NAME#fabric-az1-}

  case $FABRIC_NODE_ROLE in
    control-plane)
      # shellcheck source=scripts/lib/fabric-node-disk-policy.sh
      if ! declare -F fabric_validate_node_disk_destination >/dev/null; then
        # shellcheck disable=SC1091
        source "$repo_root/scripts/lib/fabric-node-disk-policy.sh"
      fi
      fabric_validate_node_disk_destination "$FABRIC_NODE_NAME" "$FABRIC_NODE_DISK" || {
        fabric_node_inventory_die "$FABRIC_NODE_NAME conflicts with the consensus disk policy"
        return 1
      }
      ;;
    service)
      # shellcheck source=fabric/workers/lib/admission.sh
      if ! declare -F fabric_worker_load_admission >/dev/null; then
        # shellcheck disable=SC1091
        source "$repo_root/fabric/workers/lib/admission.sh"
      fi
      fabric_worker_load_admission "$FABRIC_NODE_NAME" \
        "$repo_root/fabric/workers/inventory/$FABRIC_NODE_NAME.yaml"
      [[ $FABRIC_WORKER_MAC == "$FABRIC_NODE_MAC" ]] || {
        fabric_node_inventory_die "$FABRIC_NODE_NAME MAC conflicts with worker admission"
        return 1
      }
      [[ $FABRIC_WORKER_DISK == "$FABRIC_NODE_DISK" ]] || {
        fabric_node_inventory_die "$FABRIC_NODE_NAME disk conflicts with worker admission"
        return 1
      }
      ;;
  esac
}
