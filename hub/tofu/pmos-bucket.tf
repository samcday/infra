resource "b2_bucket" "pmos" {
  bucket_name = "samcday-pmos"
  bucket_type = "allPublic"
}

resource "b2_application_key" "pmos" {
  key_name     = "pmos-cloud"
  bucket_id    = b2_bucket.pmos.bucket_id
  capabilities = ["listAllBucketNames", "listBuckets", "listFiles", "readFiles", "writeFiles", "deleteFiles"]
}
