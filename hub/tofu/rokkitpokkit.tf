resource "b2_bucket" "rokkitpokkit" {
  bucket_name = "samcday-rokkitpokkit"
  bucket_type = "allPublic"
}

resource "b2_application_key" "rokkitpokkit" {
  key_name = "rokkitpokkit-cloud"
  bucket_id = b2_bucket.rokkitpokkit.bucket_id
  capabilities = [
    "listBuckets",
    "listFiles",
    "readFiles",
    "writeFiles",
    "deleteFiles",
  ]
}

resource "kubernetes_secret" "rokkitpokkit-tofu-vars" {
  metadata {
    name      = "rokkitpokkit-tofu-vars"
    namespace = "cloud-cluster"
  }

  data = {
    "b2_application_key_id" = b2_application_key.rokkitpokkit.application_key_id
    "b2_application_key"    = b2_application_key.rokkitpokkit.application_key
    "b2_bucket_name"        = b2_bucket.rokkitpokkit.bucket_name
  }
}

resource "cloudflare_api_token" "rokkitpokkit" {
  name = "rokkitpokkit"

  policy {
    permission_groups = [
      data.cloudflare_api_token_permission_groups.all.account["Workers Scripts Write"],
      data.cloudflare_api_token_permission_groups.all.zone["DNS Write"],
    ]
    resources = {
      "com.cloudflare.api.account.*"                                       = "*"
      "com.cloudflare.api.account.zone.${data.cloudflare_zone.samcday.id}" = "*"
    }
  }
}

resource "kubernetes_secret" "rokkitpokkit-tofu-cloudflare-vars" {
  metadata {
    name      = "rokkitpokkit-tofu-cloudflare-vars"
    namespace = "cloud-cluster"
  }

  data = {
    "cloudflare_api_token"  = cloudflare_api_token.rokkitpokkit.value
    "cloudflare_account_id" = local.cloudflare_account_id
    "cloudflare_zone_id"    = data.cloudflare_zone.samcday.id
  }
}
