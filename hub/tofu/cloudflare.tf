data "cloudflare_api_token_permission_groups" "all" {}

data "cloudflare_zone" "samcday" {
  name = "samcday.com"
}

resource "random_password" "tunnel_secret" {
  length           = 32
}

resource "cloudflare_tunnel" "tunnel" {
  name       = "hub-cluster"
  secret     = base64encode(random_password.tunnel_secret.result)
  account_id = "444c14b123bd021dcdf0400fbd847d63"
}

resource "kubernetes_secret" "ingress-nginx-cloudflared-tunnel-token" {
  metadata {
    name      = "cloudflared-tunnel-token"
    namespace = "ingress-nginx"
  }

  data = {
    "token" = cloudflare_tunnel.tunnel.tunnel_token
    "cname" = cloudflare_tunnel.tunnel.cname
  }
}

resource "cloudflare_api_token" "external-dns" {
  name = "hub-cluster-external-dns"

  policy {
    permission_groups = [
      data.cloudflare_api_token_permission_groups.all.zone["DNS Write"],
    ]
    resources = {
      "com.cloudflare.api.account.zone.${data.cloudflare_zone.samcday.id}" = "*"
    }
  }
}

resource "kubernetes_secret" "external-dns-token" {
  metadata {
    name      = "cloudflare"
    namespace = "external-dns"
  }

  data = {
    "token" = cloudflare_api_token.external-dns.value
  }
}

# cert-manager needs the DNS token for TXT verification challenges
resource "kubernetes_secret" "cert-manager-token" {
  metadata {
    name      = "cloudflare"
    namespace = "cert-manager"
  }

  data = {
    "token" = cloudflare_api_token.external-dns.value
  }
}

# cloud-cluster has its own external-dns instance that can reuse the main token
# that hub's external-dns uses
resource "kubernetes_secret" "cloud-cluster-external-dns" {
  metadata {
    name      = "cloudflare"
    namespace = "cloud-cluster"
  }

  data = {
    "token" = cloudflare_api_token.external-dns.value
  }
}
