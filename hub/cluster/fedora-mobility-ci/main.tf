terraform {
  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "4.52.4"
    }
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "1.52.0"
    }
    random = {
      source = "hashicorp/random"
      version = "3.7.2"
    }
  }
}

provider "cloudflare" {}
provider "hcloud" {}
provider "random" {}

resource "hcloud_ssh_key" "samcday" {
  name       = "samcday"
  public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFwawprQXEkGl38Q7T0PNseL0vpoyr4TbATMkEaZJTWQ"
}

resource "hcloud_placement_group" "fedora_mobility_ci" {
  name = "fedora-mobility-ci"
  type = "spread"
}

resource "hcloud_firewall" "fedora_mobility_ci" {
  name = "fedora-mobility-ci-firewall"
  rule {
    direction = "in"
    protocol  = "udp"
    port      = "41641"
    source_ips = [
      "0.0.0.0/0",
      "::/0"
    ]
  }
}

resource "hcloud_network" "fedora_mobility_ci" {
  name     = "fedora-mobility-ci-network"
  ip_range = "172.32.0.0/16"
}

resource "hcloud_network_subnet" "subnet" {
  network_id   = hcloud_network.fedora_mobility_ci.id
  type         = "cloud"
  network_zone = "eu-central"
  ip_range     = "172.33.0.0/16"
}

resource "random_password" "tunnel_secret" {
  length           = 32
}

resource "cloudflare_tunnel" "tunnel" {
  name       = "fedora-mobility-ci"
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
