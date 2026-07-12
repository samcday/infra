# Hub control-plane stability

## Storage invariants

- Never place etcd data on Ceph, RBD, NFS, or another service whose recovery
  depends on the hub Kubernetes API.
- A new partition, LUKS mapping, loop device, or filesystem on the existing
  control-plane SATA SSD does not isolate etcd from device latency.
- Keep general workloads off control-plane nodes with the standard
  `node-role.kubernetes.io/control-plane:NoSchedule` taint.
- Apply etcd host changes to one member at a time. Preserve quorum and wait for
  the changed member to catch up before touching the next member.

## Before changing an etcd member

1. Confirm all three members are healthy, at the same revision, and have no
   alarms.
2. Record the current leader and start with a follower.
3. Take and verify a fresh snapshot.
4. Confirm the other two members can maintain quorum if the selected member is
   unavailable.

## Rolling the host service

The desired unit is in `hub/butane/etcd.yaml`. Existing Fedora CoreOS machines
do not replay Ignition automatically, so the same unit change must be installed
on the running host before restarting that member.

For each follower, then the former/current leader:

1. Install the reviewed unit and run `systemctl daemon-reload`.
2. Restart only that member.
3. Wait for its mTLS `/metrics` endpoint on port 2379 and etcd endpoint health.
4. Verify it has caught up to the cluster revision and has no alarms.
5. Check WAL fsync, backend commit, proposal failure, and leader-change metrics.
6. Proceed only if the API SLO and remaining quorum stayed healthy.

The Podman block-I/O weight enforced by BFQ is containment, not physical
isolation. Roll it out as an experiment and stop if Kubernetes or Ceph latency
materially regresses.

## Stability proof

Do not call the hub stable until one continuous seven-day observation window
shows all of the following:

- all three etcd scrape targets continuously present and healthy;
- no etcd member-down, no-leader, proposal-failure, high-fsync, or high-commit
  alert;
- no unexpected etcd leader election;
- no kube-vip restart and no stale kube-vip leader lease;
- no hub API error-budget burn alert;
- the Dead Man's Snitch heartbeat remains healthy; and
- a controlled critical test alert reaches Sam through PagerDuty and is
  observed on both the configured phone path and email path.

Any maintenance-induced alert must be annotated and excluded explicitly; an
absence of retained metrics is not evidence of stability.
