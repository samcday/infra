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
