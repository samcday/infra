data "cloudflare_api_token_permission_groups" "all" {}

data "cloudflare_zone" "samcday" {
  name = "samcday.com"
}

data "cloudflare_zone" "fastboop" {
  name = "fastboop.win"
}

resource "cloudflare_record" "samcday_apex" {
  zone_id         = data.cloudflare_zone.samcday.id
  name            = "samcday.com"
  type            = "CNAME"
  content         = "matrix.samcday.com"
  ttl             = 1
  proxied         = true
  allow_overwrite = true
}

resource "random_password" "tunnel_secret" {
  length = 32
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
      "com.cloudflare.api.account.zone.${data.cloudflare_zone.samcday.id}"  = "*"
      "com.cloudflare.api.account.zone.${data.cloudflare_zone.fastboop.id}" = "*"
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

# sub clusters write DNS records (external-dns) and manage cloudflared tunnels
resource "cloudflare_api_token" "sub-cluster" {
  name = "sub-cluster"

  policy {
    permission_groups = [
      data.cloudflare_api_token_permission_groups.all.account["Cloudflare Tunnel Write"],
      data.cloudflare_api_token_permission_groups.all.zone["DNS Write"],
    ]
    resources = {
      "com.cloudflare.api.account.*"                                        = "*"
      "com.cloudflare.api.account.zone.${data.cloudflare_zone.samcday.id}"  = "*"
      "com.cloudflare.api.account.zone.${data.cloudflare_zone.fastboop.id}" = "*"
    }
  }
}

resource "kubernetes_secret" "sub-cluster-cloudflare-token" {
  for_each = toset(["cloud-cluster", "fastboop", "simonet"])

  metadata {
    name      = "cloudflare"
    namespace = each.key
  }

  data = {
    "token" = cloudflare_api_token.sub-cluster.value
  }
}
