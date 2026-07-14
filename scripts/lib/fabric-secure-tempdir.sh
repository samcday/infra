#!/usr/bin/env bash

# Sourced by offline fabric helpers that transiently handle plaintext keys,
# credentials, Ignitions, or installer inputs. Deleting a file from an SSD or
# CoW filesystem is not secure erasure, so fail closed unless the temporary
# directory is backed by tmpfs.

fabric_secure_tmpdir() {
  local prefix=${1:?temporary-directory prefix is required}
  local minimum_bytes=${2:-16777216}
  local root=${FABRIC_SECRET_TMPDIR:-/dev/shm}
  local available
  local directory

  [[ $prefix =~ ^[a-z0-9][a-z0-9-]*$ ]] || {
    echo "invalid secure temporary-directory prefix: $prefix" >&2
    return 1
  }
  [[ $minimum_bytes =~ ^[0-9]+$ ]] || {
    echo "invalid secure temporary-directory size floor: $minimum_bytes" >&2
    return 1
  }
  [[ $root == /* && -d $root && -w $root ]] || {
    echo "secure temporary root is not a writable absolute directory: $root" >&2
    return 1
  }
  [[ $(stat --file-system --format=%T -- "$root") == tmpfs ]] || {
    echo "refusing plaintext secret work on non-tmpfs storage: $root" >&2
    return 1
  }
  available=$(df --output=avail --block-size=1 -- "$root" | awk 'NR == 2 { print $1 }')
  [[ $available =~ ^[0-9]+$ ]] || {
    echo "cannot determine free space on secure temporary root: $root" >&2
    return 1
  }
  ((available >= minimum_bytes)) || {
    echo "secure temporary root has only $available bytes free; need $minimum_bytes" >&2
    return 1
  }

  directory=$(mktemp --directory -- "$root/$prefix.XXXXXXXX")
  chmod 0700 -- "$directory"
  printf '%s\n' "$directory"
}
