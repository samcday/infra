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
   same-name member back as a learner after a validated snapshot. The helper
   writes its snapshot-bound intent before changing membership; rerun the same
   command and confirmation after an interruption. It resumes only from the
   exact old-voter, two-survivor, or receipt-bound learner topology. Both the
   documented pre-auth bootstrap state and the later authenticated state are
   supported: the observed mode is bound into the replacement and promotion
   intents, and an enabled cluster additionally requires the offline TLS-CN
   root user to retain exactly the built-in root role.
4. `retire plan` and `retire apply` delete only the stopped Node/password
   receipt UIDs.
5. `prepare` renders the selected node's Ignition on tmpfs. For a service node
   it reads the current agent token through one healthy pinned root.
6. `arm` publishes one immutable in-cluster session, then `reboot` binds its
   local receipt to the exact live Secret and armed placeholder, persists
   PXE-first, selects the same MAC-bound entry as one-shot UEFI `BootNext`, and
   restarts the host. The one-shot selection keeps firmware from preferring a
   previously successful local entry during this armed transition. Its
   confirmation includes the session UID.
   Exact confirmation strings are printed or derivable from the preceding
   receipt; there is no broad or name-only arm or reboot. If `arm` is
   interrupted, rerun the same command: it adopts only the exact lock/handoff
   placeholder and immutable session bytes, then completes the missing publish,
   patch, or receipt step.
   Bootie first serves a non-consuming live profile that copies the exact
   MAC-bound static connection into the installed system. Its live boot also
   carries the recognized `rd.net.timeout.carrier=60` network argument, so the
   stock FCOS installer writes first-boot-only `rd.neednet=1`. The later
   destination-Ignition request consumes the one-use token. Thus PXE may use
   its temporary DHCP address, while the first local boot must use the node's
   inventoried address before Ignition fetches router assets; no
   temporary-address router aperture is part of day two.
7. `status` reports `install-response-issued`; after the destination-Ignition
   request consumes the one-use token, `status` reports that token absent and
   `close` conditionally deletes the exact session Secret. Rerunning it after a
   lost delete reply safely records the already-absent Secret as closed. Do not
   infer install completion from a silent console, lost video, or network
   silence: token consumption is the positive installer-side gate.
8. A successfully installed service node powers off instead of warm-rebooting.
   Remove AC power from only that node for at least 30 seconds, then restore it;
   this is the explicit TPM first-boot gate. A failed install remains on its
   live console for diagnosis. This cold cycle is the one physical exception
   for a full service-node reprovision, not a normal package/config upgrade.
9. On a control-plane node, `etcd plan-promote` and `etcd promote` restore the
   third voter. Promotion is also intent-backed and rerunnable if the API reply
   or local process is lost. Then `finalize` attests and uncordons the same Node
   UID and destroys the secret-bearing tmpfs handoff. If its Node patch already
   succeeded, rerunning `finalize` recognizes the exact final contract and
   finishes local cleanup without patching again.
10. `lock release`, then `qualify --revision <main-commit>` before selecting the
    next node. Release is rerunnable if its exact conditional delete succeeded
    but the reply was lost; it re-proves the final Node before cleaning the
    local receipt.

For a planned service-node chassis replacement, drain and retire the old exact
Node under its existing lock, then admit one candidate MAC through
`bootie/discovery.tsv`. That override serves only a non-installing live FCOS;
capture its read-only inventory, commit the reviewed chassis/TPM/MAC and disk
identity as the canonical node, and remove the override. Run
`lock advance-replacement-discovery` after Fabric root and Bootie have applied
that commit. It requires the target Node and Bootie session to be absent and
all four peers Ready, but deliberately does not require the aggregate
foundation path to be Ready: one CoreDNS replica is expected to remain Pending
while a service node is absent. The transaction preserves the lock and original
Node UID evidence, then ordinary `prepare` and `arm` create the replacement
placeholder and one-use install.

If a TPM-bound service node from an older Bootie revision warm-rebooted into a
failed first Ignition boot, keep it off and recover through the same operator
surface. Retain its existing maintenance lock and closed session receipt. Use
`retire plan/apply --failed-firstboot` to remove only the exact consumed
placeholder: it must have no Lease, node-password Secret, machine ID,
InternalIP, or non-synthetic status, and the confirmation binds the admitted
disk plus an explicit `POWERED-OFF` attestation. Then run
`lock advance-failed-firstboot` with the old lock and closed session receipts.
It requires the other four nodes to be Ready and the Fabric root, foundation,
platform, and Bootie Flux paths to have applied current pushed `main`; its exact
confirmation preserves the global lock UID while producing a new revision-bound
tmpfs receipt. Continue with ordinary `prepare` and `arm` using that receipt.
The current service-node install will power off at the cold gate before its
first TPM-bound local boot. Do not clear and reacquire the lock: ordinary
acquisition intentionally requires the target to exist in the healthy five-node
preflight set.

If PXE served the live installer but the destination-Ignition token was never
consumed, the disk was not successfully replaced and the ordinary session
cannot be closed. First prove the target is physically powered off; lost video,
ping failure, or an idle Bootie log is not proof. Run `abort-failed-install`
without `--confirm` to qualify the exact never-joined service placeholder and
print its UID-bound `POWERED-OFF` confirmation, then rerun it with that exact
value. It atomically revokes the outstanding token, conditionally deletes only
the receipt-bound immutable Secret, and records an aborted closed receipt. Use
that receipt with `retire plan/apply --failed-firstboot`, then
`lock advance-failed-firstboot`, `prepare`, and `arm` as above. This recovery is
restricted to service placeholders with no Lease, node-password Secret,
machine ID, InternalIP, or real kubelet status; it cannot abort a joined node or
a control-plane session.

All mutating phases require the same UID-bound lock receipt and an exact
confirmation. An interruption deliberately leaves a visible receipt and a
fail-closed state for inspection. The old observer-host Bootie station remains
available only for full-cluster/offline recovery.
