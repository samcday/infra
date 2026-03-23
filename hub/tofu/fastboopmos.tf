resource "b2_bucket" "fastboopmos" {
  bucket_name = "samcday-fastboopmos"
  bucket_type = "allPublic"
}

resource "b2_application_key" "fastboopmos" {
  key_name  = "fastboopmos-cloud"
  bucket_id = b2_bucket.fastboopmos.bucket_id
  capabilities = [
    "listBuckets",
    "listFiles",
    "readFiles",
    "writeFiles",
    "deleteFiles",
  ]
}

resource "kubernetes_secret" "fastboopmos-tofu-vars" {
  metadata {
    name      = "fastboopmos-tofu-vars"
    namespace = "cloud-cluster"
  }

  data = {
    "b2_application_key_id" = b2_application_key.fastboopmos.application_key_id
    "b2_application_key"    = b2_application_key.fastboopmos.application_key
    "b2_bucket_name"        = b2_bucket.fastboopmos.bucket_name
  }
}
