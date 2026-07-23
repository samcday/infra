# Fabric node day two

Every Fabric node keeps its exact IPv4 PXE entry first in persistent UEFI
order. The singleton in-cluster Bootie answers all five admitted MACs, but its
steady-state GRUB response is only `exit`, so an ordinary boot continues to the
local FCOS disk. Reinstallation requires the global maintenance lock, a fresh
placeholder Node UID, and one immutable `bootie/bootie-session` Secret. Bootie
consumes both the install response and the Ignition token once.

The normal operator surface is `scripts/fabric-node-day2`. Run one node at a
time, in the order `svc1`, `svc2`, `cp1`, `cp2`, `cp3` for the first rollout.
The NanoKVM is useful observation for cp1, but no routine step depends on a
laptop, observer-host PXE station, removable media, or physical hands.

For one node, the flow is:

1. `preflight`, then `lock acquire` using the printed main revision.
2. `drain`, then `quiesce`, using the lock-bound Node UID.
3. On a control-plane node only, `etcd plan-replace` and `etcd replace` add the
   same-name member back as a learner after a validated snapshot.
4. `retire plan` and `retire apply` delete only the stopped Node/password
   receipt UIDs.
5. `prepare` renders the selected node's Ignition on tmpfs. For a service node
   it reads the current agent token through one healthy pinned root.
6. `arm` publishes one immutable in-cluster session, then `reboot` persists
   PXE-first and restarts the host. Exact confirmation strings are printed or
   derivable from the preceding receipt; there is no broad or name-only arm.
7. `status` reports `install-response-issued`; after the one-use Ignition fetch,
   `close` conditionally deletes the exact session Secret.
8. On a control-plane node, `etcd plan-promote` and `etcd promote` restore the
   third voter. Then `finalize` attests and uncordons the same Node UID and destroys
   the secret-bearing tmpfs handoff.
9. `lock release`, then `qualify --revision <main-commit>` before selecting the
   next node.

All mutating phases require the same UID-bound lock receipt and an exact
confirmation. An interruption deliberately leaves a visible receipt and a
fail-closed state for inspection. The old observer-host Bootie station remains
available only for full-cluster/offline recovery.
