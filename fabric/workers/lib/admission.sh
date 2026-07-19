#!/usr/bin/env bash

# Shared validation for the committed, non-secret worker admission records.
# Callers receive the FABRIC_WORKER_* variables below only after every field
# has passed validation.
# shellcheck disable=SC2034

FABRIC_WORKER_NODE=
FABRIC_WORKER_INVENTORY_SHA256=
FABRIC_WORKER_CHASSIS_SERIAL=
FABRIC_WORKER_PRODUCT_UUID=
FABRIC_WORKER_MAC=
FABRIC_WORKER_DISK=
FABRIC_WORKER_DISK_SERIAL=
FABRIC_WORKER_DISK_SIZE_BYTES=
FABRIC_WORKER_DISK_KIND=
FABRIC_WORKER_DISK_WWN=

fabric_worker_validate_record_shape() {
  (($# == 2)) || {
    echo 'fabric_worker_validate_record_shape requires NODE and RECORD' >&2
    return 2
  }
  local node=$1
  local record=$2

  [[ -f $record && ! -L $record ]] || {
    echo "worker admission record must be a regular non-symlink: $record" >&2
    return 1
  }
  FABRIC_EXPECTED_NODE=$node yq -e '
    (keys | sort | join(",")) == "evidence,identity,node,root_disk,state" and
    .node == strenv(FABRIC_EXPECTED_NODE) and
    (.state | type == "!!str") and
    (.evidence | keys | sort | join(",")) == "inventory_sha256" and
    (.evidence.inventory_sha256 | type == "!!str") and
    (.identity | keys | sort | join(",")) ==
      "chassis_serial,permanent_mac,product_uuid,tpm2_usable" and
    (.identity.chassis_serial | type == "!!str") and
    (.identity.product_uuid | type == "!!str") and
    (.identity.permanent_mac | type == "!!str") and
    (.identity.tpm2_usable | type == "!!bool") and
    (.root_disk | keys | sort | join(",")) == "by_id,serial,size_bytes" and
    (.root_disk.by_id | type == "!!str") and
    (.root_disk.serial | type == "!!str") and
    (.root_disk.size_bytes | type == "!!int")
  ' "$record" >/dev/null || {
    echo "worker admission record has an unexpected shape: $record" >&2
    return 1
  }
}

fabric_worker_validate_pending_record() {
  (($# == 2)) || return 2
  local node=$1
  local record=$2

  fabric_worker_validate_record_shape "$node" "$record" || return
  yq -e '
    .state == "pending" and
    .evidence.inventory_sha256 == "FABRIC_REPLACE_FROM_REVIEWED_FINAL_CAPTURE" and
    .identity.chassis_serial == "FABRIC_REPLACE_FROM_INVENTORY" and
    .identity.product_uuid == "FABRIC_REPLACE_FROM_INVENTORY" and
    .identity.permanent_mac == "FABRIC_REPLACE_FROM_INVENTORY" and
    .identity.tpm2_usable == false and
    .root_disk.by_id == "FABRIC_REPLACE_FROM_INVENTORY" and
    .root_disk.serial == "FABRIC_REPLACE_FROM_INVENTORY" and
    .root_disk.size_bytes == 0
  ' "$record" >/dev/null || {
    echo "pending worker admission contains partial or unexpected facts: $record" >&2
    return 1
  }
}

fabric_worker_load_admission() {
  (($# == 2)) || {
    echo 'fabric_worker_load_admission requires NODE and RECORD' >&2
    return 2
  }
  local node=$1
  local record=$2
  local first_octet
  local disk_eui

  FABRIC_WORKER_NODE=
  FABRIC_WORKER_INVENTORY_SHA256=
  FABRIC_WORKER_CHASSIS_SERIAL=
  FABRIC_WORKER_PRODUCT_UUID=
  FABRIC_WORKER_MAC=
  FABRIC_WORKER_DISK=
  FABRIC_WORKER_DISK_SERIAL=
  FABRIC_WORKER_DISK_SIZE_BYTES=
  FABRIC_WORKER_DISK_KIND=
  FABRIC_WORKER_DISK_WWN=

  case $node in
    fabric-az1-svc1 | fabric-az1-svc2) ;;
    *)
      echo "unsupported fabric worker: $node" >&2
      return 1
      ;;
  esac

  fabric_worker_validate_record_shape "$node" "$record" || return
  [[ $(yq -r '.state' "$record") == admitted ]] || {
    echo "$node is not admitted; preserve and review final inventory first" >&2
    return 1
  }

  FABRIC_WORKER_NODE=$node
  FABRIC_WORKER_INVENTORY_SHA256=$(yq -r '.evidence.inventory_sha256' "$record")
  FABRIC_WORKER_CHASSIS_SERIAL=$(yq -r '.identity.chassis_serial' "$record")
  FABRIC_WORKER_PRODUCT_UUID=$(yq -r '.identity.product_uuid | downcase' "$record")
  FABRIC_WORKER_MAC=$(yq -r '.identity.permanent_mac | downcase' "$record")
  FABRIC_WORKER_DISK=$(yq -r '.root_disk.by_id' "$record")
  FABRIC_WORKER_DISK_SERIAL=$(yq -r '.root_disk.serial' "$record")
  FABRIC_WORKER_DISK_SIZE_BYTES=$(yq -r '.root_disk.size_bytes' "$record")

  [[ $FABRIC_WORKER_INVENTORY_SHA256 =~ ^[0-9a-f]{64}$ ]] || {
    echo "$node inventory evidence does not have one lowercase SHA-256" >&2
    return 1
  }
  [[ $FABRIC_WORKER_CHASSIS_SERIAL =~ ^[A-Za-z0-9._:+-]{2,128}$ ]] || {
    echo "$node chassis serial is absent or unsafe" >&2
    return 1
  }
  [[ $FABRIC_WORKER_PRODUCT_UUID =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ &&
     $FABRIC_WORKER_PRODUCT_UUID != 00000000-0000-0000-0000-000000000000 ]] || {
    echo "$node DMI product UUID is absent or invalid" >&2
    return 1
  }
  [[ $FABRIC_WORKER_MAC =~ ^([0-9a-f]{2}:){5}[0-9a-f]{2}$ ]] || {
    echo "$node permanent MAC is malformed" >&2
    return 1
  }
  first_octet=${FABRIC_WORKER_MAC%%:*}
  (((16#$first_octet & 3) == 0)) && [[ $FABRIC_WORKER_MAC != 00:00:00:00:00:00 ]] || {
    echo "$node permanent MAC is not a globally administered unicast address" >&2
    return 1
  }
  [[ $(yq -r '.identity.tpm2_usable' "$record") == true ]] || {
    echo "$node has not admitted a usable TPM2" >&2
    return 1
  }
  [[ $FABRIC_WORKER_DISK_SERIAL =~ ^[A-Za-z0-9._:+/-]{2,128}$ ]] || {
    echo "$node disk serial is absent or unsafe" >&2
    return 1
  }
  if [[ ! $FABRIC_WORKER_DISK_SIZE_BYTES =~ ^[0-9]+$ ]] ||
      ((FABRIC_WORKER_DISK_SIZE_BYTES < 68719476736)); then
    echo "$node admitted disk is smaller than the 64 GiB floor" >&2
    return 1
  fi
  [[ $FABRIC_WORKER_DISK != *-part[0-9]* ]] || {
    echo "$node admitted disk is a partition, not a whole device" >&2
    return 1
  }

  if [[ $FABRIC_WORKER_DISK =~ ^/dev/disk/by-id/ata-[A-Za-z0-9._:+-]+$ ]]; then
    FABRIC_WORKER_DISK_KIND=ata
  elif [[ $FABRIC_WORKER_DISK =~ ^/dev/disk/by-id/nvme-eui\.([0-9a-f]{16})$ ]]; then
    disk_eui=${BASH_REMATCH[1]}
    [[ $disk_eui != 0000000000000000 && $disk_eui != ffffffffffffffff ]] || {
      echo "$node admitted NVMe EUI is a placeholder" >&2
      return 1
    }
    FABRIC_WORKER_DISK_KIND=nvme
    FABRIC_WORKER_DISK_WWN=eui.$disk_eui
  else
    echo "$node admitted disk is not an exact ATA identity or lowercase NVMe EUI" >&2
    return 1
  fi
}
