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

rustup target add x86_64-unknown-linux-musl

cd "$REPO_ROOT/apps/etcdetcetc"
cargo build --target x86_64-unknown-linux-musl
cargo build
