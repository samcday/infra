terraform {
  required_providers {
    b2 = {
      source  = "Backblaze/b2"
      version = "0.10.0"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "4.52.4"
    }
    dmsnitch = {
      source  = "plukevdh/dmsnitch"
      version = "0.1.5"
    }
    github = {
      source  = "integrations/github"
      version = "6.6.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "2.38.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "3.7.2"
    }
    tailscale = {
      source  = "tailscale/tailscale"
      version = "0.29.2"
    }
  }
}

provider "b2" {}
provider "cloudflare" {}
provider "github" {}
provider "kubernetes" {}
provider "random" {}
provider "tailscale" {}
