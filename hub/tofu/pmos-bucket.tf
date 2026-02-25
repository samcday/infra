resource "b2_bucket" "pmos" {
  bucket_name = "samcday-pmos"
  bucket_type = "allPublic"
}

resource "b2_application_key" "pmos" {
  key_name     = "pmos-cloud"
  bucket_id    = b2_bucket.pmos.bucket_id
  capabilities = ["listAllBucketNames", "listBuckets", "listFiles", "readFiles", "writeFiles", "deleteFiles"]
}

resource "kubernetes_secret" "pmos_storage" {
  metadata {
    name      = "pmos-storage"
    namespace = "cloud-cluster"
  }

  data = {
    ACCESS_KEY_ID     = b2_application_key.pmos.application_key_id
    SECRET_ACCESS_KEY = b2_application_key.pmos.application_key
    ENDPOINT          = "s3.eu-central-003.backblazeb2.com"
    REGION            = "eu-central-003"
    BUCKET            = b2_bucket.pmos.bucket_name
  }
}
