terraform {
  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "1.52.0"
    }
  }
}

provider "hcloud" {}

resource "hcloud_ssh_key" "samcday" {
  name       = "samcday"
  public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFwawprQXEkGl38Q7T0PNseL0vpoyr4TbATMkEaZJTWQ"
}

resource "hcloud_placement_group" "placement-group" {
  name = "placement-group"
  type = "spread"
}

resource "hcloud_firewall" "firewall" {
  name = "firewall"

  rule {
    direction = "in"
    protocol  = "tcp"
    port      = "80"
    source_ips = [
      "0.0.0.0/0",
      "::/0"
    ]
  }

  rule {
    direction = "in"
    protocol  = "tcp"
    port      = "443"
    source_ips = [
      "0.0.0.0/0",
      "::/0"
    ]
  }

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

