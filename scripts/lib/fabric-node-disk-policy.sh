#!/usr/bin/env bash
# FABRIC_DISK_KIND and FABRIC_DISK_WWN are the function's sourced-library
# outputs and are consumed by the caller after validation.
# shellcheck disable=SC2034

# Exact, reviewed identities for admitted consensus-node disks. Every pin is a
# stable whole-disk NVMe EUI captured from the node's final inventory.
FABRIC_CP1_NVME_DESTINATION=/dev/disk/by-id/nvme-eui.002538839100c827
FABRIC_CP2_NVME_DESTINATION=/dev/disk/by-id/nvme-eui.002538b971048a4f
FABRIC_CP3_DESTINATION=/dev/disk/by-id/nvme-eui.002538bb71b4bb45
readonly FABRIC_CP1_NVME_DESTINATION FABRIC_CP2_NVME_DESTINATION
readonly FABRIC_CP3_DESTINATION

FABRIC_DISK_KIND=
FABRIC_DISK_WWN=

fabric_validate_pinned_disk_destination() {
  (($# == 3)) || {
    echo 'fabric_validate_pinned_disk_destination requires LABEL, DISK, and EXPECTED_DISK' \
      >&2
    return 2
  }

  local label=$1
  local disk=$2
  local expected_destination=$3
  local disk_eui

  FABRIC_DISK_KIND=
  FABRIC_DISK_WWN=

  if [[ $label == fabric-az1-cp3 &&
        ($expected_destination == "$FABRIC_CP1_NVME_DESTINATION" ||
         $expected_destination == "$FABRIC_CP2_NVME_DESTINATION") ]]; then
    echo 'fabric-az1-cp3 disk identity collides with an admitted peer disk' >&2
    return 1
  fi

  [[ -n $expected_destination ]] || {
    echo "$label has no admitted disk; complete and commit its exact inventory first" >&2
    return 1
  }

  [[ $disk != *-part[0-9]* ]] || {
    echo "refusing an installer destination partition: $disk" >&2
    return 1
  }

  [[ $disk == "$expected_destination" ]] || {
    echo "destination is not admitted for $label: $disk" >&2
    return 1
  }

  if [[ $disk =~ ^/dev/disk/by-id/ata-[A-Za-z0-9._:+-]+$ ]]; then
    FABRIC_DISK_KIND=ata
    return 0
  fi

  if [[ $disk =~ ^/dev/disk/by-id/nvme-eui\.([0-9a-f]{16})$ ]]; then
    disk_eui=${BASH_REMATCH[1]}
    [[ $disk_eui != 0000000000000000 && $disk_eui != ffffffffffffffff ]] || {
      echo "refusing a placeholder NVMe EUI installer destination: $disk" >&2
      return 1
    }

    FABRIC_DISK_KIND=nvme
    FABRIC_DISK_WWN=eui.$disk_eui
    return 0
  fi

  echo "refusing an unstable or unsupported installer destination: $disk" >&2
  return 1
}

fabric_validate_node_disk_destination() {
  (($# == 2)) || {
    echo 'fabric_validate_node_disk_destination requires NODE and DISK' >&2
    return 2
  }

  local node=$1
  local disk=$2
  local expected_destination

  FABRIC_DISK_KIND=
  FABRIC_DISK_WWN=

  case $node in
    fabric-az1-cp1)
      expected_destination=$FABRIC_CP1_NVME_DESTINATION
      ;;
    fabric-az1-cp2)
      expected_destination=$FABRIC_CP2_NVME_DESTINATION
      ;;
    fabric-az1-cp3)
      expected_destination=$FABRIC_CP3_DESTINATION
      ;;
    *)
      echo "unsupported fabric node: $node" >&2
      return 1
      ;;
  esac

  fabric_validate_pinned_disk_destination "$node" "$disk" "$expected_destination"
}
