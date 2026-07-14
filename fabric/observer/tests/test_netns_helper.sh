#!/usr/bin/env bash

set -euo pipefail

cd -- "$(dirname -- "$0")/.."
helper=./fabric-observer-netns

bash -n "$helper"

# A function/subshell used directly as an `if` condition suppresses Bash's
# errexit behavior inside that function. Recovery must run as a standalone
# command, capture its status, and only then branch. Prove both the language
# behavior on this Bash and the helper's reviewed call shape.
probe_output=$(bash -c '
  set +e
  recovery_probe() {
    false
    printf BAD
  }
  (set -e; recovery_probe)
  probe_rc=$?
  printf "rc=%s" "$probe_rc"
')
[[ "$probe_output" == rc=1 ]]

if grep -Eq 'if[[:space:]]+\(set -e;[[:space:]]*resume_restoration' "$helper"; then
  printf 'conditional recovery invocation would suppress errexit\n' >&2
  exit 1
fi
grep -Fq '  (set -e; resume_restoration)' "$helper"
grep -Fq '  restore_rc=$?' "$helper"

# The control directory is pre-created root:root 0700. A GROUP directive makes
# wpa_supplicant widen group access even for gid 0; root-only wpa_cli calls need
# no group delegation.
grep -Fq "    printf 'ctrl_interface=DIR=%s\\n' \"\$supplicant_control\"" "$helper"
if grep -Eq "^[[:space:]]*printf ['\"]ctrl_interface=.*GROUP=" "$helper"; then
  printf 'observer supplicant configuration delegates group access\n' >&2
  exit 1
fi

# Observer setup/teardown and the attended router traffic matrix must serialize
# on one root-only lock. Admission checks are repeated while this lock is held,
# so a matrix cannot race setup into creating a competing 10.66.0.2 identity.
grep -Fq 'readonly fabric_network_lock=/run/lock/fabric-network-operation.lock' "$helper"
[[ $(grep -Fc '  acquire_fabric_network_lock' "$helper") -eq 2 ]]
grep -Fq "flock --exclusive --nonblock \"\$fabric_network_lock_fd\"" "$helper"

# A single `ip route` output line can still be ECMP and name more than one
# device. Admission must reject that shape rather than trusting the first dev.
(
  # shellcheck source=fabric/observer/fabric-observer-netns
  source "$helper"
  default_route_is_single_path eno1 \
    'default via 10.0.1.1 dev eno1 proto dhcp'
  if default_route_is_single_path eno1 \
    'default nexthop via 10.0.1.1 dev eno1 weight 1 nexthop via 10.0.1.2 dev wlp95s0 weight 1'; then
    printf 'ECMP default route incorrectly passed single-uplink admission\n' >&2
    exit 1
  fi
  if default_route_is_single_path eno1 \
    'default via 10.0.1.1 dev eno1 onlink dev eno1'; then
    printf 'duplicate-device default route incorrectly passed admission\n' >&2
    exit 1
  fi
)

# An `ip netns list` inspection error is not evidence that the namespace is
# absent. Cleanup must stop and retain state rather than silently skipping it.
set +e
namespace_probe=$(
  exec 2>&1
  # shellcheck source=fabric/observer/fabric-observer-netns
  source "$helper"
  ip() { return 42; }
  namespace_exists
  printf BAD
)
namespace_probe_rc=$?
set -e
[[ $namespace_probe_rc -eq 1 ]]
[[ "$namespace_probe" == 'ERROR: cannot enumerate named network namespaces' ]]

printf 'fabric-observer-netns rollback/default-route/netns regressions: PASS\n'
