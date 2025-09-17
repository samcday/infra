terraform {
  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "4.38.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "3.6.2"
    }
  }
}

provider "cloudflare" {}
provider "random" {}

resource "random_password" "tunnel_secret" {
  length = 32
}

resource "cloudflare_tunnel" "tunnel" {
  name       = "simonet"
  secret     = base64encode(random_password.tunnel_secret.result)
  account_id = "444c14b123bd021dcdf0400fbd847d63"
}

output "tunnel_token" {
  value     = cloudflare_tunnel.tunnel.tunnel_token
  sensitive = true
}

output "tunnel_secret" {
  value     = random_password.tunnel_secret.result
  sensitive = true
}

output "tunnel_cname" {
  value = cloudflare_tunnel.tunnel.cname
}
