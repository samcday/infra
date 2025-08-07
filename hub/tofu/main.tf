terraform {
  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "4.52.0"
    }
    github = {
      source  = "integrations/github"
      version = "6.6.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "2.36.0"
    }
    random = {
      source = "hashicorp/random"
      version = "3.7.1"
    }
  }
}

provider "cloudflare" {}
provider "github" {}
provider "kubernetes" {}
provider "random" {}
