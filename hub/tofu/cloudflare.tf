data "cloudflare_api_token_permission_groups" "all" {}

data "cloudflare_zone" "samcday" {
  name = "samcday.com"
}

resource "cloudflare_api_token" "hub-cluster" {
  name = "hub-cluster"

  policy {
    permission_groups = [
      data.cloudflare_api_token_permission_groups.all.account["Cloudflare Tunnel Write"],
      data.cloudflare_api_token_permission_groups.all.zone["DNS Write"],
    ]
    resources = {
      "com.cloudflare.api.account.*" = "*"
      "com.cloudflare.api.account.zone.${data.cloudflare_zone.samcday.id}" = "*"
    }
  }
}

resource "random_password" "tunnel_secret" {
  length           = 32
}

resource "cloudflare_tunnel" "tunnel" {
  name       = "hub-cluster"
  secret     = base64encode(random_password.tunnel_secret.result)
  account_id = "444c14b123bd021dcdf0400fbd847d63"
}

resource "kubernetes_secret" "cloudflared-tunnel-token" {
  metadata {
    name      = "cloudflared-tunnel-token"
    namespace = "ingress-nginx"
  }

  data = {
    "token" = cloudflare_tunnel.tunnel.tunnel_token
    "cname" = cloudflare_tunnel.tunnel.cname
  }
}
