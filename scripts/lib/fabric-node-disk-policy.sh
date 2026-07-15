#!/usr/bin/env bash
# FABRIC_DISK_KIND and FABRIC_DISK_WWN are the function's sourced-library
# outputs and are consumed by the caller after validation.
# shellcheck disable=SC2034

# Exact, reviewed identities for admitted consensus-node disks. Cp3 has not
# completed inventory, so its empty pin deliberately rejects every destination
# until one exact ATA whole-disk identity is reviewed and committed here.
FABRIC_CP1_NVME_DESTINATION=/dev/disk/by-id/nvme-eui.002538839100c827
FABRIC_CP2_NVME_DESTINATION=/dev/disk/by-id/nvme-eui.002538bb71b4bb45
FABRIC_CP3_ATA_DESTINATION=
readonly FABRIC_CP1_NVME_DESTINATION FABRIC_CP2_NVME_DESTINATION
readonly FABRIC_CP3_ATA_DESTINATION

FABRIC_DISK_KIND=
FABRIC_DISK_WWN=

fabric_validate_node_disk_destination() {
  (($# == 2)) || {
    echo 'fabric_validate_node_disk_destination requires NODE and DISK' >&2
    return 2
  }

  local node=$1
  local disk=$2
  local disk_eui
  local expected_destination

  FABRIC_DISK_KIND=
  FABRIC_DISK_WWN=

  [[ $disk != *-part[0-9]* ]] || {
    echo "refusing an installer destination partition: $disk" >&2
    return 1
  }

  if [[ $disk =~ ^/dev/disk/by-id/ata-[A-Za-z0-9._:+-]+$ ]]; then
    case $node in
      fabric-az1-cp1)
        echo "fabric-az1-cp1 is bound to $FABRIC_CP1_NVME_DESTINATION" >&2
        return 1
        ;;
      fabric-az1-cp2)
        echo "fabric-az1-cp2 is bound to $FABRIC_CP2_NVME_DESTINATION" >&2
        return 1
        ;;
      fabric-az1-cp3)
        [[ -n $FABRIC_CP3_ATA_DESTINATION ]] || {
          echo 'fabric-az1-cp3 has no admitted disk; complete and commit its exact inventory first' >&2
          return 1
        }
        [[ $disk == "$FABRIC_CP3_ATA_DESTINATION" ]] || {
          echo "ATA destination is not admitted for fabric-az1-cp3: $disk" >&2
          return 1
        }
        FABRIC_DISK_KIND=ata
        return 0
        ;;
      *)
        echo "unsupported fabric node: $node" >&2
        return 1
        ;;
    esac
  fi

  if [[ $disk =~ ^/dev/disk/by-id/nvme-eui\.([0-9a-f]{16})$ ]]; then
    disk_eui=${BASH_REMATCH[1]}
    [[ $disk_eui != 0000000000000000 && $disk_eui != ffffffffffffffff ]] || {
      echo "refusing a placeholder NVMe EUI installer destination: $disk" >&2
      return 1
    }

    case $node in
      fabric-az1-cp1)
        expected_destination=$FABRIC_CP1_NVME_DESTINATION
        ;;
      fabric-az1-cp2)
        expected_destination=$FABRIC_CP2_NVME_DESTINATION
        ;;
      fabric-az1-cp3)
        echo "NVMe destination is not admitted for fabric-az1-cp3: $disk" >&2
        return 1
        ;;
      *)
        echo "unsupported fabric node: $node" >&2
        return 1
        ;;
    esac

    [[ $disk == "$expected_destination" ]] || {
      echo "NVMe destination is not admitted for $node: $disk" >&2
      return 1
    }

    FABRIC_DISK_KIND=nvme
    FABRIC_DISK_WWN=eui.$disk_eui
    return 0
  fi

  echo "refusing an unstable or unsupported installer destination: $disk" >&2
  return 1
}
