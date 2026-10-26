terraform {
  required_providers {
    headscale = {
      source  = "awlsring/headscale"
      version = "0.5.1"
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

resource "headscale_user" "binarylane_demo" {
  name = "binarylane-demo"
}

resource "headscale_user" "edge" {
  name = "edge"
}

resource "headscale_user" "edge_au_east" {
  name = "edge-au-east"
}

resource "headscale_user" "hub" {
  name = "hub"
}

resource "headscale_user" "sam" {
  name = "sam"
}

resource "headscale_pre_auth_key" "hub_apiserver_stable" {
  user           = headscale_user.hub.id
  time_to_expire = "520w"
  reusable       = true
  ephemeral      = false
  acl_tags       = ["tag:hub-apiserver"]
}

resource "headscale_pre_auth_key" "services_subnet_router" {
  user           = headscale_user.hub.id
  time_to_expire = "520w"
  reusable       = true
  ephemeral      = false
  acl_tags       = ["tag:hub-services-subnet-router"]
}

resource "kubernetes_secret" "services_subnet_router_preauth" {
  metadata {
    name      = "services-subnet-router-ts-auth"
    namespace = "headscale"
  }

  data = {
    authkey = headscale_pre_auth_key.services_subnet_router.key
  }
}

resource "kubernetes_secret" "hub_apiserver_preauth" {
  metadata {
    name      = "hub-apiserver-ts-auth"
    namespace = "headscale"
  }

  data = {
    authkey = headscale_pre_auth_key.hub_apiserver_stable.key
  }
}
