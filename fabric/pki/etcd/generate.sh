#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$script_dir"
# shellcheck disable=SC1091
source "$script_dir/../../../scripts/lib/fabric-secure-tempdir.sh"
umask 077

replace=false
if [[ "${1:-}" == "--replace" ]]; then
  replace=true
  shift
fi
if (( $# != 0 )); then
  echo "usage: $0 [--replace]" >&2
  exit 2
fi

for command in cfssl cfssljson git openssl realpath sops; do
  command -v "$command" >/dev/null || {
    echo "missing required command: $command" >&2
    exit 1
  }
done

workdir=$(fabric_secure_tmpdir fabric-etcd-pki-generate 16777216)
trap 'rm -rf "$workdir"' EXIT
repo_root=$(git rev-parse --show-toplevel)
encrypted_dir=$workdir/encrypted
mkdir -p "$encrypted_dir"

artifacts=(
  ca.pem ca-key.pem
  fabric-az1-cp1.pem fabric-az1-cp1-key.pem
  fabric-az1-cp2.pem fabric-az1-cp2-key.pem
  fabric-az1-cp3.pem fabric-az1-cp3-key.pem
  fabric-root.pem fabric-root-key.pem
  fabric-observer.pem fabric-observer-key.pem
  root.pem root-key.pem
)

if ! $replace; then
  for artifact in "${artifacts[@]}"; do
    if [[ -e "$artifact.enc" ]]; then
      echo "refusing to replace existing PKI: $artifact.enc (use --replace deliberately)" >&2
      exit 1
    fi
  done
fi

encrypt() {
  local source=$1
  local destination=$2
  local final_path=$3
  local filename_override
  filename_override=$(realpath --relative-to="$repo_root" "$PWD/$final_path")
  sops --encrypt --filename-override "$filename_override" "$source" > "$destination"
}

(
  cd "$workdir"
  cfssl gencert -initca "$OLDPWD/ca-csr.json" | cfssljson -bare ca

  for node in fabric-az1-cp1 fabric-az1-cp2 fabric-az1-cp3; do
    cfssl gencert \
      -ca ca.pem \
      -ca-key ca-key.pem \
      -config "$OLDPWD/peer-config.json" \
      "$OLDPWD/$node-csr.json" | cfssljson -bare "$node"
  done

  for client in root fabric-root fabric-observer; do
    cfssl gencert \
      -ca ca.pem \
      -ca-key ca-key.pem \
      -config "$OLDPWD/client-config.json" \
      "$OLDPWD/$client-csr.json" | cfssljson -bare "$client"
  done
)

# `cfssl gencert -initca` takes the CA lifetime from ca-csr.json, not from the
# leaf signing config. Fail if a future edit silently returns to its 5-year
# default while the recovery documentation promises a 10-year CA.
openssl x509 -in "$workdir/ca.pem" -checkend 283824000 -noout >/dev/null || {
  echo "generated CA has less than nine years remaining" >&2
  exit 1
}

openssl x509 -in "$workdir/ca.pem" -noout -text |
  grep -Fq 'CA:TRUE, pathlen:0' || {
    echo "generated CA does not enforce pathlen:0" >&2
    exit 1
  }

for artifact in "${artifacts[@]}"; do
  encrypt \
    "$workdir/$artifact" \
    "$encrypted_dir/$artifact.enc" \
    "$artifact.enc"
  cmp \
    "$workdir/$artifact" \
    <(sops --decrypt --input-type json --output-type binary "$encrypted_dir/$artifact.enc")
done

# Only publish after every key/certificate has generated, encrypted, and
# round-tripped successfully. This avoids leaving a half-replaced CA set.
for artifact in "${artifacts[@]}"; do
  install -m 0644 "$encrypted_dir/$artifact.enc" "$artifact.enc"
done

echo 'generated encrypted fabric etcd PKI; plaintext existed only in verified tmpfs' >&2
