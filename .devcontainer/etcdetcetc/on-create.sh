#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

sudo apt-get update && sudo apt-get install -y protobuf-compiler musl-tools

arch="$(uname -m)"
case "$arch" in
  x86_64)  kind_arch="amd64"; crane_arch="x86_64" ;;
  aarch64) kind_arch="arm64"; crane_arch="arm64" ;;
  *)
    echo "unsupported architecture: $arch" >&2
    exit 1
    ;;
esac

curl -fsSL "https://kind.sigs.k8s.io/dl/v0.27.0/kind-linux-${kind_arch}" -o /tmp/kind
sudo install -m 0755 /tmp/kind /usr/local/bin/kind

curl -fsSL https://raw.githubusercontent.com/tilt-dev/tilt/master/scripts/install.sh | bash

curl -fsSL "https://github.com/google/go-containerregistry/releases/latest/download/go-containerregistry_Linux_${crane_arch}.tar.gz" | sudo tar xz -C /usr/local/bin crane

curl -fsSL "https://github.com/derailed/k9s/releases/download/v0.50.18/k9s_Linux_${crane_arch}.tar.gz" | sudo tar xz -C /usr/local/bin k9s

rustup target add x86_64-unknown-linux-musl

cd "$REPO_ROOT/apps/etcdetcetc"
cargo build -p xtask
cargo build --target x86_64-unknown-linux-musl

# Wait for Docker-in-Docker, then run full dev-up + tilt ci to validate the
# pipeline and warm the Docker image cache (kindest/node, registry:2).
# Cluster state won't survive prebuild, but the image cache does.
timeout_seconds=30
elapsed=0
until docker info >/dev/null 2>&1; do
  if [ "$elapsed" -ge "$timeout_seconds" ]; then
    echo "docker daemon not ready after ${timeout_seconds}s" >&2
    exit 1
  fi
  sleep 2
  elapsed=$((elapsed + 2))
done

cargo xtask dev-up
tilt ci
