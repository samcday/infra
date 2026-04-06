#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

timeout_seconds=30
elapsed=0

until docker info >/dev/null 2>&1; do
  if [ "$elapsed" -ge "$timeout_seconds" ]; then
    echo "docker daemon was not ready within ${timeout_seconds}s" >&2
    exit 1
  fi
  sleep 2
  elapsed=$((elapsed + 2))
done

cd "$REPO_ROOT/apps/etcdetcetc"
cargo xtask dev-up
