#!/bin/bash

set -euo pipefail

readonly HUB_SERVER_URL="https://10.0.1.254:6443"
readonly CACHE_ROOT="${XDG_CACHE_HOME:-$HOME/.cache}/infra-kube-auth"

declare -Ar CHILD_CLUSTER_NAMESPACES=(
  [cloud]="cloud-cluster"
)

usage() {
  cat <<'EOF'
Usage:
  kube-credential-helper.sh <cluster-name>
  kube-credential-helper.sh --init

Clusters:
  hub
  cloud
EOF
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

repo_root() {
  git rev-parse --show-toplevel
}

secure_delete() {
  local file_path="$1"

  [[ -f "$file_path" ]] || return 0

  if command -v shred >/dev/null 2>&1; then
    shred -u "$file_path"
  else
    rm -f "$file_path"
  fi
}

ensure_cache_dir() {
  local cluster="$1"

  mkdir -p "$CACHE_ROOT/$cluster"
  chmod 700 "$CACHE_ROOT/$cluster"
}

cache_cert_path() {
  local cluster="$1"
  printf '%s/%s/client.crt' "$CACHE_ROOT" "$cluster"
}

cache_key_path() {
  local cluster="$1"
  printf '%s/%s/client.key' "$CACHE_ROOT" "$cluster"
}

cache_server_ca_path() {
  local cluster="$1"
  printf '%s/%s/server-ca.crt' "$CACHE_ROOT" "$cluster"
}

cache_valid() {
  local cert_path="$1"
  local key_path="$2"

  [[ -s "$cert_path" && -s "$key_path" ]] || return 1
  openssl x509 -in "$cert_path" -checkend 300 -noout >/dev/null 2>&1
}

cert_expiration_timestamp() {
  local cert_path="$1"
  local end_date

  end_date="$(openssl x509 -in "$cert_path" -noout -enddate)"
  end_date="${end_date#notAfter=}"
  date -u -d "$end_date" '+%Y-%m-%dT%H:%M:%SZ'
}

issue_client_cert() {
  local ca_cert_path="$1"
  local ca_key_path="$2"
  local cert_path="$3"
  local key_path="$4"
  local tmp_dir="$5"
  local serial

  serial="$(date +%s)"

  openssl genrsa -out "$key_path" 2048 >/dev/null 2>&1

  openssl req \
    -new \
    -key "$key_path" \
    -subj '/CN=kubernetes-admin/O=system:masters' \
    -out "$tmp_dir/client.csr" >/dev/null 2>&1

  openssl x509 \
    -req \
    -in "$tmp_dir/client.csr" \
    -CA "$ca_cert_path" \
    -CAkey "$ca_key_path" \
    -days 1 \
    -set_serial "$serial" \
    -out "$cert_path" >/dev/null 2>&1

  chmod 600 "$key_path"
  chmod 644 "$cert_path"
}

ensure_hub_credentials() {
  local cert_path
  local key_path
  local root
  local ca_cert_path
  local ca_key_enc_path
  local tmp_dir
  local decrypted_key_path

  cert_path="$(cache_cert_path hub)"
  key_path="$(cache_key_path hub)"

  if cache_valid "$cert_path" "$key_path"; then
    return 0
  fi

  root="$(repo_root)"
  ca_cert_path="$root/hub/pki/k8s/ca.crt"
  ca_key_enc_path="$root/hub/pki/k8s/ca-key.pem.enc"

  [[ -s "$ca_cert_path" ]] || fail "missing hub CA cert: $ca_cert_path"
  [[ -s "$ca_key_enc_path" ]] || fail "missing encrypted hub CA key: $ca_key_enc_path"

  ensure_cache_dir hub

  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "$tmp_dir"' EXIT
  decrypted_key_path="$tmp_dir/ca-key.pem"

  sops -d "$ca_key_enc_path" > "$decrypted_key_path"
  issue_client_cert "$ca_cert_path" "$decrypted_key_path" "$cert_path" "$key_path" "$tmp_dir"

  secure_delete "$decrypted_key_path"
  rm -rf "$tmp_dir"
  trap - EXIT
}

hub_kubeconfig_for_child_flow() {
  local kubeconfig_path="$1"
  local root
  local hub_cert_path
  local hub_key_path
  local hub_server_ca_path

  root="$(repo_root)"
  hub_cert_path="$(cache_cert_path hub)"
  hub_key_path="$(cache_key_path hub)"
  hub_server_ca_path="$root/hub/pki/k8s/server-ca.crt"

  [[ -s "$hub_server_ca_path" ]] || fail "missing hub server CA cert for TLS verification: $hub_server_ca_path"

  cat > "$kubeconfig_path" <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: hub
    cluster:
      server: $HUB_SERVER_URL
      certificate-authority: $hub_server_ca_path
users:
  - name: hub-admin
    user:
      client-certificate: $hub_cert_path
      client-key: $hub_key_path
contexts:
  - name: hub
    context:
      cluster: hub
      user: hub-admin
current-context: hub
EOF
}

ensure_child_credentials() {
  local cluster="$1"
  local namespace
  local cert_path
  local key_path
  local server_ca_path
  local tmp_dir
  local hub_kubeconfig
  local child_ca_key_b64
  local child_ca_cert_b64
  local child_ca_key_path
  local child_ca_cert_path

  namespace="${CHILD_CLUSTER_NAMESPACES[$cluster]:-}"
  [[ -n "$namespace" ]] || fail "unsupported child cluster: $cluster"

  cert_path="$(cache_cert_path "$cluster")"
  key_path="$(cache_key_path "$cluster")"
  server_ca_path="$(cache_server_ca_path "$cluster")"

  local root
  root="$(repo_root)"
  local repo_server_ca_path="$root/hub/pki/k8s/${cluster}-server-ca.crt"
  local cache_is_valid="false"

  if cache_valid "$cert_path" "$key_path" && [[ -s "$server_ca_path" ]]; then
    cache_is_valid="true"
  fi

  if [[ "$cache_is_valid" == "true" ]] && [[ -s "$repo_server_ca_path" ]]; then
    return 0
  fi

  if [[ "$cache_is_valid" == "false" ]] && [[ ! -s "$repo_server_ca_path" ]]; then
    fail "${cluster} server CA not found. Run './scripts/kube-credential-helper.sh --init' first."
  fi

  ensure_hub_credentials
  ensure_cache_dir "$cluster"

  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "$tmp_dir"' EXIT
  hub_kubeconfig="$tmp_dir/hub.kubeconfig"
  child_ca_key_path="$tmp_dir/child-ca.key"
  child_ca_cert_path="$tmp_dir/child-ca.crt"

  hub_kubeconfig_for_child_flow "$hub_kubeconfig"

  child_ca_key_b64="$(kubectl --kubeconfig "$hub_kubeconfig" -n "$namespace" get secret ca -o jsonpath='{.data.tls\.key}')"
  child_ca_cert_b64="$(kubectl --kubeconfig "$hub_kubeconfig" -n "$namespace" get secret ca -o jsonpath='{.data.tls\.crt}')"

  [[ -n "$child_ca_key_b64" ]] || fail "missing tls.key in secret/ca for namespace $namespace"
  [[ -n "$child_ca_cert_b64" ]] || fail "missing tls.crt in secret/ca for namespace $namespace"

  printf '%s' "$child_ca_key_b64" | base64 -d > "$child_ca_key_path"
  printf '%s' "$child_ca_cert_b64" | base64 -d > "$child_ca_cert_path"
  cp "$child_ca_cert_path" "$server_ca_path"

  # Also write server CA to repo-relative path for kubeconfig certificate-authority
  cp "$child_ca_cert_path" "$root/hub/pki/k8s/${cluster}-server-ca.crt"

  issue_client_cert "$child_ca_cert_path" "$child_ca_key_path" "$cert_path" "$key_path" "$tmp_dir"

  secure_delete "$child_ca_key_path"
  secure_delete "$child_ca_cert_path"
  rm -rf "$tmp_dir"
  trap - EXIT
}

emit_exec_credential() {
  local cert_path="$1"
  local key_path="$2"
  local expiration_timestamp

  expiration_timestamp="$(cert_expiration_timestamp "$cert_path")"

  jq -n \
    --arg expirationTimestamp "$expiration_timestamp" \
    --arg clientCertificateData "$(<"$cert_path")" \
    --arg clientKeyData "$(<"$key_path")" \
    '{
      apiVersion: "client.authentication.k8s.io/v1",
      kind: "ExecCredential",
      status: {
        expirationTimestamp: $expirationTimestamp,
        clientCertificateData: $clientCertificateData,
        clientKeyData: $clientKeyData
      }
    }'
}

initialize_child_cas() {
  local cluster

  for cluster in "${!CHILD_CLUSTER_NAMESPACES[@]}"; do
    ensure_child_credentials "$cluster"
  done

  echo "Initialized child cluster credentials in $CACHE_ROOT" >&2
}

main() {
  local cluster="${1:-}"
  local cert_path
  local key_path

  [[ -n "$cluster" ]] || {
    usage >&2
    exit 1
  }

  require_cmd git
  require_cmd jq
  require_cmd openssl
  require_cmd sops
  require_cmd kubectl
  require_cmd base64
  require_cmd date

  case "$cluster" in
    --help|-h)
      usage
      ;;
    --init)
      initialize_child_cas
      ;;
    hub)
      ensure_hub_credentials
      cert_path="$(cache_cert_path hub)"
      key_path="$(cache_key_path hub)"
      emit_exec_credential "$cert_path" "$key_path"
      ;;
    *)
      if [[ -n "${CHILD_CLUSTER_NAMESPACES[$cluster]:-}" ]]; then
        ensure_child_credentials "$cluster"
        cert_path="$(cache_cert_path "$cluster")"
        key_path="$(cache_key_path "$cluster")"
        emit_exec_credential "$cert_path" "$key_path"
      else
        fail "unknown cluster: $cluster"
      fi
      ;;
  esac
}

main "$@"
