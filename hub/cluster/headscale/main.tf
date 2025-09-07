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

resource "headscale_pre_auth_key" "cloud-cluster-apiserver" {
  user           = headscale_user.cloud.id
  time_to_expire = "520w"
  reusable       = true
  ephemeral      = true
  acl_tags       = ["tag:cloud-cluster-apiserver"]
}

resource "headscale_pre_auth_key" "cloud-cluster-nodes" {
  user           = headscale_user.cloud.id
  time_to_expire = "520w"
  reusable       = true
  ephemeral      = true
  acl_tags       = ["tag:cloud-cluster-node"]
}

resource "headscale_pre_auth_key" "cloud-cluster" {
  user           = headscale_user.cloud.id
  time_to_expire = "520w"
  reusable       = true
  ephemeral      = true
}

resource "headscale_pre_auth_key" "subnet-router" {
  user           = headscale_user.hub.id
  time_to_expire = "520w"
  reusable       = true
  ephemeral      = true
}


resource "kubernetes_secret" "subnet-router-preauth" {
  metadata {
    name      = "subnet-router-ts-auth"
    namespace = "headscale"
  }

  data = {
    key = headscale_pre_auth_key.subnet-router.key
  }
}

resource "kubernetes_secret" "cloud-cluster-node-preauth" {
  metadata {
    name      = "node-ts-auth"
    namespace = "cloud-cluster"
  }

  data = {
    key = headscale_pre_auth_key.cloud-cluster-nodes.key
  }
}

resource "kubernetes_secret" "cloud-cluster-apiserver-preauth" {
  metadata {
    name      = "apiserver-ts-auth"
    namespace = "cloud-cluster"
  }

  data = {
    key = headscale_pre_auth_key.cloud-cluster-apiserver.key
  }
}
