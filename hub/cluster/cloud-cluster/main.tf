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

resource "hcloud_network" "network" {
  name     = "network"
  ip_range = "172.28.0.0/15"
}

resource "hcloud_network_subnet" "subnet" {
  network_id   = hcloud_network.network.id
  type         = "cloud"
  network_zone = "eu-central"
  ip_range     = "172.29.0.0/16"
}

resource "hcloud_network_subnet" "subnet_us_east" {
  network_id   = hcloud_network.network.id
  type         = "cloud"
  network_zone = "us-east"
  ip_range     = "172.28.0.0/17"
}

resource "hcloud_network_subnet" "subnet_us_west" {
  network_id   = hcloud_network.network.id
  type         = "cloud"
  network_zone = "us-west"
  ip_range     = "172.28.128.0/17"
}
