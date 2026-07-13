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

resource "headscale_user" "conduit" {
  name = "conduit"
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

resource "headscale_pre_auth_key" "cloud_cluster_apiserver_stable" {
  user           = headscale_user.cloud.id
  time_to_expire = "520w"
  reusable       = true
  ephemeral      = false
  acl_tags       = ["tag:cloud-cluster-apiserver"]
}

resource "headscale_pre_auth_key" "cloud-cluster-nodes" {
  user           = headscale_user.cloud.id
  time_to_expire = "520w"
  reusable       = true
  ephemeral      = false
  acl_tags       = ["tag:cloud-cluster-node"]
}

resource "headscale_pre_auth_key" "cloud-cluster" {
  user           = headscale_user.cloud.id
  time_to_expire = "520w"
  reusable       = true
  ephemeral      = true
}

resource "headscale_pre_auth_key" "cloud-cluster-wasmcloud-nats" {
  user           = headscale_user.cloud.id
  time_to_expire = "520w"
  reusable       = true
  ephemeral      = false
  acl_tags       = ["tag:cloud-cluster-wasmcloud-nats"]
}

resource "headscale_pre_auth_key" "conduit_client_stable" {
  user           = headscale_user.conduit.id
  time_to_expire = "520w"
  reusable       = true
  ephemeral      = false
}

resource "headscale_pre_auth_key" "edge_au_east_apiserver" {
  user           = headscale_user.edge_au_east.id
  time_to_expire = "520w"
  reusable       = true
  ephemeral      = false
  acl_tags       = ["tag:edge-au-east-apiserver"]
}

resource "headscale_pre_auth_key" "edge_au_east_nodes" {
  user           = headscale_user.edge_au_east.id
  time_to_expire = "520w"
  reusable       = true
  ephemeral      = true
  acl_tags       = ["tag:edge-au-east-node"]
}

resource "headscale_pre_auth_key" "labgrid_coordinator" {
  user           = headscale_user.hub.id
  time_to_expire = "520w"
  reusable       = true
  ephemeral      = false
  acl_tags       = ["tag:labgrid-coordinator"]
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
    authkey = headscale_pre_auth_key.cloud_cluster_apiserver_stable.key
  }
}

resource "kubernetes_secret" "cloud-cluster-wasmcloud-nats-preauth" {
  metadata {
    name      = "wasmcloud-nats-ts-auth"
    namespace = "cloud-cluster"
  }

  data = {
    authkey = headscale_pre_auth_key.cloud-cluster-wasmcloud-nats.key
  }
}

resource "kubernetes_secret" "conduit_client_preauth" {
  metadata {
    name      = "conduit-ts-auth"
    namespace = "conduit"
  }

  data = {
    authkey = headscale_pre_auth_key.conduit_client_stable.key
  }
}

resource "kubernetes_secret" "edge_au_east_apiserver_preauth" {
  metadata {
    name      = "edge-au-east-apiserver-ts-auth"
    namespace = "cloud-cluster"
  }

  data = {
    authkey = headscale_pre_auth_key.edge_au_east_apiserver.key
  }
}

resource "kubernetes_secret" "edge_au_east_node_preauth" {
  metadata {
    name      = "edge-au-east-node-ts-auth"
    namespace = "cloud-cluster"
  }

  data = {
    key = headscale_pre_auth_key.edge_au_east_nodes.key
  }
}

resource "kubernetes_secret" "labgrid_coordinator_preauth" {
  metadata {
    name      = "labgrid-ts-auth"
    namespace = "labgrid"
  }

  data = {
    authkey = headscale_pre_auth_key.labgrid_coordinator.key
  }
}
