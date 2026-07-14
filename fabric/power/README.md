# Fabric root power meter commissioning

The `SM-732-A` is only a candidate. Its model string does not establish a
manufacturer, Australian electrical admission, Matter transport, or energy
telemetry. Do not put the root cluster behind it until a completed copy of
[`commissioning.example.yaml`](commissioning.example.yaml) is reviewed and its
single top-level `status` is `accepted`, except for one explicitly attended
load/thermal test after it reaches `assembly_qualified`.

The fixed accounting boundary is:

```text
house mains
  -> admitted SM-732-A
  -> one labelled extension board
  -> OpenWrt One + five-port switch + fabric-az1-cp1/cp2/cp3
```

Workers, monitors, laptop chargers, the Matter controller/recorder, and its
Wi-Fi AP or Thread border router stay outside that board. The external recorder
is authoritative for cumulative energy; hub Prometheus/Grafana may later
consume sensor-only telemetry and must never receive a credential that can
operate the relay.

## Remote-hands sequence

1. Keep the extension board and all fabric equipment disconnected. Inspect the
   plug for damage, heating, Australian rating/RCM and responsible-supplier
   evidence. Record the wall outlet, plug, board/lead, and all five PSU ratings,
   then calculate aggregate load and headroom against the lowest-rated part of
   the chain. If sharing a photograph, physically cover the Matter QR code and
   numeric setup code first; neither belongs in Git, chat, screenshots, or the
   commissioning record.
2. Put a harmless, well-understood load on the plug and factory-reset it.
   Commission it to one deliberately owned controller that remains powered
   outside current and future clusters. Discover whether it uses Wi-Fi or
   Thread. Record its VID/PID, firmware, certificate/product reference or
   digest, exposed clusters, and entities; do not copy certificate material or
   infer identity from the shell model string.
3. Require instantaneous `W` plus cumulative `Wh` or `kWh`. The cumulative
   entity may be device-native or a reviewed, persistent, gap-aware integrator
   outside the measured domain. A W-only integrator must record its input,
   reset behavior, gap detection and missing-sample policy; silently treating
   missing telemetry as zero fails admission. Matter 1.3 added energy-management
   capabilities, but a Matter label alone does not prove a particular device
   implements them. See the [Connectivity Standards Alliance Matter 1.3
   announcement](https://csa-iot.org/newsroom/matter-1-3-specification-released/).
4. Remove old fabrics, schedules, local automations, automatic power cycling,
   voice control, and vendor-cloud actuation. Disable unattended firmware
   updates while preserving local overload protection. Reject the plug if
   remote cloud actuation cannot be removed.
5. With the harmless load, prove the relay remains ON through controller and
   AP/Thread-border-router loss. Perform three attended relay cycles and three
   attended upstream-mains cycles. Upstream restoration must return the relay
   to ON, and the cumulative counter must preserve or explicitly account for
   every interval. Prove the recorder remains available while the measured
   board is entirely off.
6. Before the run, declare load watts, duration, expected Wh, and tolerance.
   Record start/end cumulative readings and RFC3339 timestamps with explicit
   UTC offsets. Compare the reported delta with a known load or independent
   reference meter. Ten percent is only a coarse sanity gate; it is not a
   billing-grade accuracy claim. Measure or explicitly account for the plug's
   own standby consumption.
7. After `assembly_qualified`, assemble and label the exact five-load board
   under attendance. Record actual peak load, headroom, fit, smell, and
   post-load heating before changing status to `accepted`. The later
   root-cluster power test has two distinct cases: downstream relay loss and
   upstream loss of power to the meter itself.

`status` is the sole admission decision. It stays `pending` while any required
pre-assembly evidence is unknown, may become `assembly_qualified` only for the
attended full-domain load/thermal test, becomes `rejected` with a reason after
any failed gate, and becomes `accepted` only after the complete record and
evidence are reviewed. Never leave the domain running unattended at
`assembly_qualified`.
There is no operational “kill-switch acceptance”: automated or routine relay
actuation remains prohibited. The relay is used only for explicit, attended
power-loss tests after the clean-shutdown and restore prerequisites in the main
fabric plan.

Australian electrical admission must be based on the physical product and its
responsible supplier, not Matter certification. The [EESS RCM
guidance](https://www.eess.gov.au/rcm/regulatory-compliance-mark-rcm-general/)
is the starting point. Controller networking must also satisfy the controller's
local requirements; for Home Assistant, see its [Matter integration network
requirements](https://www.home-assistant.io/integrations/matter).

Copy the example to `commissioning.yaml` only when real evidence is available.
The record is intended to be non-secret and reviewable. Never add setup codes,
QR payloads, controller credentials, Wi-Fi credentials, cloud tokens, or a
relay-capable API credential.
