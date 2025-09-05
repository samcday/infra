resource "tailscale_acl" "policy" {
  overwrite_existing_content = true
  acl = <<EOF
  {
    "autoApprovers": {
      "routes": {
        "10.0.2.0/24": [
          "tag:hub-node"
        ],
        "10.0.1.0/24": [
          "tag:hub-node"
        ],
        "172.30.0.0/16": [
          "tag:hub-node"
        ],
        "172.31.0.0/16": [
          "tag:hub-node"
        ],
      },
    },
    "grants": [
      {"src": ["*"], "dst": ["*"], "ip": ["*"]},
    ],

    "nodeAttrs": [
      {
        "target": ["tag:k8s"],
        "attr":   ["funnel"],
      },
    ],

    "tagOwners": {
      "tag:hub-node": [],
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

resource "tailscale_oauth_client" "node" {
  depends_on = [
    tailscale_acl.policy
  ]

  description = "hub ts-operator"
  scopes      = ["auth_keys"]
  tags        = ["tag:hub-node"]
}

resource "kubernetes_secret" "node-oauth" {
  metadata {
    name      = "node-oauth"
    namespace = "kube-system"
  }

  data = {
    "client_id" = tailscale_oauth_client.node.id
    "client_secret" = tailscale_oauth_client.node.key
  }
}

resource "tailscale_dns_split_nameservers" "cluster-dns" {
  domain = "cluster.hub.internal"
  nameservers = ["172.31.0.10"]
}

resource "tailscale_dns_split_nameservers" "lan-dns" {
  domain = "hub.internal"
  nameservers = ["10.0.1.1"]
}
