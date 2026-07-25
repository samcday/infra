# Manage Dead Man's Switch, save the callback URL into a k8s secret (for use by Prometheus).
# See the dms AlertmanagerConfig for usage of the secret.

resource "dmsnitch_snitch" "hub" {
  name = "hub"

  interval = "hourly"
  type     = "basic"
}

# Fabric is the root of trust for the child control planes, so it needs an
# independent heartbeat. Reusing the hub snitch would let either cluster hide
# the other's total failure.
resource "dmsnitch_snitch" "fabric" {
  name  = "fabric"
  notes = "Fabric root-cluster Watchdog heartbeat, managed by hub OpenTofu"
  tags  = ["fabric", "kubernetes", "root-cluster"]

  interval = "hourly"
  type     = "basic"
}

resource "kubernetes_secret" "dms-url" {
  metadata {
    name      = "dms-url"
    namespace = "monitoring"
  }

  data = {
    "url" = dmsnitch_snitch.hub.url
  }
}

# This is a bounded handoff location, not a runtime dependency from Fabric to
# hub. After creation an operator copies only this callback into a SOPS Secret
# in fabric/cluster/monitoring; Fabric Alertmanager then calls DMS directly.
resource "kubernetes_secret" "fabric-dms-url" {
  metadata {
    name      = "fabric-dms-url"
    namespace = "monitoring"
  }

  data = {
    "url" = dmsnitch_snitch.fabric.url
  }
}
