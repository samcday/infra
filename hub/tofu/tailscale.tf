resource "tailscale_oauth_client" "tailscale-operator" {
  description = "hub ts-operator"
  scopes      = ["devices:core", "auth_keys"]
  tags        = ["tag:k8s-operator"]
}

resource "kubernetes_secret" "tailscale-operator-oauth" {
  metadata {
    name      = "operator-oauth"
    namespace = "tailscale"
  }

  data = {
    "client_id" = tailscale_oauth_client.tailscale-operator.id
    "client_secret" = tailscale_oauth_client.tailscale-operator.key
  }
}
