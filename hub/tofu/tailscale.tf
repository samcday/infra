resource "tailscale_acl" "policy" {
  acl = <<EOF
  {
    "grants": [
      {"src": ["*"], "dst": ["*"], "ip": ["*"]},
    ],

    "tagOwners": {
      "tag:k8s-operator": [],
      "tag:k8s": ["tag:k8s-operator"],
    }
  }

  EOF
}

resource "tailscale_oauth_client" "tailscale-operator" {
  depends_on = [
    tailscale_acl.policy
  ]

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
