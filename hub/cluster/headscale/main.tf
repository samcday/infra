terraform {
  required_providers {
    headscale = {
      source  = "awlsring/headscale"
      version = "0.4.2"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "2.38.0"
    }
  }
}

provider "headscale" {
  endpoint = "http://headscale-generic.headscale.svc.cluster.hub.internal"
}
provider "kubernetes" {}

resource "headscale_user" "cloud" {
  name = "cloud"
}

resource "headscale_user" "hub" {
  name = "hub"
}

resource "headscale_user" "sam" {
  name = "sam"
}

resource "headscale_user" "simonet" {
  name = "simonet"
}

resource "headscale_pre_auth_key" "cloud" {
  user           = headscale_user.cloud.id
  time_to_expire = "520w"
  reusable       = true
}

resource "kubernetes_secret" "preauth-keys" {
  for_each = {
    "cloud-cluster" = headscale_pre_auth_key.cloud.key
  }

  metadata {
    name      = "headscale-preauth-key"
    namespace = each.key
  }

  data = {
    key = each.value
  }
}
