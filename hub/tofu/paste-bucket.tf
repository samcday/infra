resource "b2_bucket" "paste" {
  bucket_name = "samcday-paste-cloud"
  bucket_type = "allPrivate"

  lifecycle_rules {
    file_name_prefix              = ""
    days_from_uploading_to_hiding = 30
    days_from_hiding_to_deleting  = 1
  }
}

resource "b2_application_key" "paste" {
  key_name     = "paste-cloud"
  bucket_id    = b2_bucket.paste.bucket_id
  capabilities = ["listAllBucketNames", "listBuckets", "listFiles", "readFiles", "writeFiles", "deleteFiles"]
}

resource "kubernetes_secret" "paste_storage" {
  metadata {
    name      = "paste-storage"
    namespace = "cloud-cluster"
  }

  data = {
    ACCESS_KEY_ID     = b2_application_key.paste.application_key_id
    SECRET_ACCESS_KEY = b2_application_key.paste.application_key
    ENDPOINT          = "s3.eu-central-003.backblazeb2.com"
    REGION            = "eu-central-003"
    BUCKET            = b2_bucket.paste.bucket_name
  }
}
