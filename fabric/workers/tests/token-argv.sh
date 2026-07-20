#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C
umask 077

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
worker_dir=$(cd -- "$script_dir/.." && pwd)
repo_root=$(git -C "$worker_dir" rev-parse --show-toplevel)
# shellcheck disable=SC1091
source "$repo_root/scripts/lib/fabric-secure-tempdir.sh"

for command in grep strace; do
  command -v "$command" >/dev/null || {
    echo "token argv regression requires $command" >&2
    exit 1
  }
done

workdir=$(fabric_secure_tmpdir fabric-worker-token-argv 1048576)
cleanup() {
  find "$workdir" -mindepth 1 -delete 2>/dev/null || true
  rmdir "$workdir" 2>/dev/null || true
}
trap cleanup EXIT

# bootstrap.sh --check deliberately uses this structurally valid fake token.
# Keep the pattern in a file so the regression itself does not put it in the
# argv of the grep process used to inspect the execve trace.
token_pattern=$workdir/token.pattern
{
  printf 'K10'
  printf '0%.0s' {1..64}
  printf '::node:'
  printf 'a%.0s' {1..64}
  printf '\n'
} >"$token_pattern"
chmod 0600 "$token_pattern"

trace=$workdir/execve.trace
strace --follow-forks --quiet --string-limit=4096 --trace=execve \
  --output="$trace" "$worker_dir/butane/bootstrap.sh" --check >/dev/null

if grep --fixed-strings --file="$token_pattern" "$trace" >/dev/null; then
  echo 'worker agent token appeared in a spawned process argv' >&2
  exit 1
fi

echo 'validated that the worker renderer does not expose its agent token in child argv'
