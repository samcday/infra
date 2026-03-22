resource "b2_application_key" "rokkitpokkit" {
  key_name = "rokkitpokkit-cloud"
  capabilities = [
    "listAllBucketNames",
    "listBuckets",
    "readBuckets",
    "writeBuckets",
    "listFiles",
    "readFiles",
    "writeFiles",
    "deleteFiles",
  ]
}

resource "kubernetes_secret" "rokkitpokkit-tofu-generated" {
  metadata {
    name      = "rokkitpokkit-tofu-generated"
    namespace = "cloud-cluster"
  }

  data = {
    "TF_VAR_b2_application_key_id" = b2_application_key.rokkitpokkit.application_key_id
    "TF_VAR_b2_application_key"    = b2_application_key.rokkitpokkit.application_key
  }
}
