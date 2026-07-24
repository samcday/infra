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
   Bootie first serves a non-consuming live profile that copies the exact
   MAC-bound static connection into the installed system. Its live boot also
   carries the recognized `rd.net.timeout.carrier=60` network argument, so the
   stock FCOS installer writes first-boot-only `rd.neednet=1`. The later
   destination-Ignition request consumes the one-use token. Thus PXE may use
   its temporary DHCP address, while the first local boot must use the node's
   inventoried address before Ignition fetches router assets; no
   temporary-address router aperture is part of day two.
7. `status` reports `install-response-issued`; after the one-use Ignition fetch,
   `close` conditionally deletes the exact session Secret.
8. A successfully installed service node powers off instead of warm-rebooting.
   Remove AC power from only that node for at least 30 seconds, then restore it;
   this is the explicit TPM first-boot gate. A failed install remains on its
   live console for diagnosis. This cold cycle is the one physical exception
   for a full service-node reprovision, not a normal package/config upgrade.
9. On a control-plane node, `etcd plan-promote` and `etcd promote` restore the
   third voter. Then `finalize` attests and uncordons the same Node UID and destroys
   the secret-bearing tmpfs handoff.
10. `lock release`, then `qualify --revision <main-commit>` before selecting the
    next node.

If a TPM-bound service node from an older Bootie revision warm-rebooted into a
failed first Ignition boot, keep it off and recover through the same operator
surface. After inspecting and conditionally clearing any stale maintenance lock,
run a fresh `preflight` and `lock acquire`, then use `retire plan/apply` with
`--failed-firstboot`. That mode accepts only the exact consumed placeholder with
no Lease, node-password Secret, machine ID, InternalIP, or non-synthetic status;
its confirmation binds the admitted disk and explicitly attests `POWERED-OFF`.
Continue with ordinary `prepare` and `arm`. The current service-node install
will power off at the cold gate before its first TPM-bound local boot.

All mutating phases require the same UID-bound lock receipt and an exact
confirmation. An interruption deliberately leaves a visible receipt and a
fail-closed state for inspection. The old observer-host Bootie station remains
available only for full-cluster/offline recovery.
