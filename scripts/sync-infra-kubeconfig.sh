#!/bin/bash

set -euo pipefail

oidc_issuer_url="${OIDC_ISSUER_URL:-https://dex.samcday.com}"
oidc_client_id="${OIDC_CLIENT_ID:-fastboop-kubectl}"
source_context="${HUB_CONTEXT:-}"
source_secret_name="${SOURCE_SECRET_NAME:-admin-kubeconfig-external}"
output_kubeconfig="${OUTPUT_KUBECONFIG:-$HOME/.kube/infra.generated}"

if ! command -v kubectl >/dev/null 2>&1; then
  echo "ERROR: kubectl is required" >&2
  exit 1
fi

kubectl_cmd=(kubectl)
if [[ -z "$source_context" ]]; then
  if kubectl config get-contexts default >/dev/null 2>&1; then
    source_context="default"
  else
    source_context="$(kubectl config current-context 2>/dev/null || true)"
  fi
fi

if [[ -n "$source_context" ]]; then
  kubectl_cmd+=(--context "$source_context")
fi

tmp_dir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

tmp_kubeconfig="$tmp_dir/infra.generated.tmp"
mkdir -p "$(dirname "$output_kubeconfig")"

mapfile -t namespaces < <(
  "${kubectl_cmd[@]}" get secret -A --field-selector "metadata.name=$source_secret_name" \
    -o jsonpath='{range .items[*]}{.metadata.namespace}{"\n"}{end}' | sort -u
)

if [[ ${#namespaces[@]} -eq 0 ]]; then
  echo "ERROR: no $source_secret_name secrets found" >&2
  exit 1
fi

for namespace in "${namespaces[@]}"; do
  secret_file="$tmp_dir/$namespace.kubeconfig"

  "${kubectl_cmd[@]}" -n "$namespace" get secret "$source_secret_name" -o jsonpath='{.data.value}' \
    | base64 -d > "$secret_file"

  cluster_name="$(KUBECONFIG="$secret_file" kubectl config view --raw -o jsonpath='{.clusters[0].name}')"
  server="$(KUBECONFIG="$secret_file" kubectl config view --raw -o jsonpath='{.clusters[0].cluster.server}')"
  ca_data="$(KUBECONFIG="$secret_file" kubectl config view --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')"

  if [[ -z "$cluster_name" || -z "$server" || -z "$ca_data" ]]; then
    echo "ERROR: could not parse kubeconfig in namespace $namespace" >&2
    exit 1
  fi

  ca_file="$tmp_dir/$cluster_name.ca.crt"
  printf '%s' "$ca_data" | base64 -d > "$ca_file"

  user_name="$cluster_name-oidc"

  kubectl config set-cluster "$cluster_name" \
    --server="$server" \
    --certificate-authority="$ca_file" \
    --embed-certs=true \
    --kubeconfig="$tmp_kubeconfig" >/dev/null

  kubectl config set-credentials "$user_name" \
    --exec-api-version=client.authentication.k8s.io/v1beta1 \
    --exec-command=kubectl \
    --exec-arg=oidc-login \
    --exec-arg=get-token \
    --exec-arg="--oidc-issuer-url=$oidc_issuer_url" \
    --exec-arg="--oidc-client-id=$oidc_client_id" \
    --exec-arg=--oidc-extra-scope=email \
    --kubeconfig="$tmp_kubeconfig" >/dev/null

  kubectl config set-context "$cluster_name" \
    --cluster="$cluster_name" \
    --user="$user_name" \
    --kubeconfig="$tmp_kubeconfig" >/dev/null
done

kubectl config view --raw --flatten --kubeconfig "$tmp_kubeconfig" > "$tmp_dir/infra.generated"
mv "$tmp_dir/infra.generated" "$output_kubeconfig"
