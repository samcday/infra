# LiteLLM comparison pilot — additive, not a migration

`https://litellm.samcday.com/v1` is a **proposed** second endpoint. This component
uses the official LiteLLM Helm chart (1.105.1), its digest-pinned non-root image,
and a dedicated PostgreSQL 17 database on a 4Gi Ceph PVC. It does not change
`llm.samcday.com`, Bifrost policy/keys, existing clients, or MiMo PR #59. It does
not resolve the existing gateway's policy/restart gate in #60.

The only existing file changed is the Flux fan-out, adding this independent
component. The pilot depends on the existing `llm-gateway` subscription bridge;
it does not run another CLIProxyAPI or copy any OAuth refresh state.

## What standard LiteLLM replaces

- Native `/key/generate`, `/key/info`, `/key/update`, `/key/delete` and private UI:
  named virtual keys, model allowlists, expiry, RPM/concurrency and usage budgets.
- PostgreSQL-backed key policy and usage: no custom key/bootstrap controller.
- Official Helm deployment, config checksum rollouts and migration hook.
- OpenAI Responses/Chat and native Anthropic Messages through the existing bridge.

Models are `gpt-6-astra` and `claude-haiku-5-5` (no Bifrost prefixes). Anthropic
clients use `https://litellm.samcday.com` as their SDK base, producing
`/v1/messages`. There are no paid-provider fallbacks or additional subscription
grants. Both frontends share the same upstream subscription limits; their key
budgets are independent, not a combined subscription allowance.

## Authority and secrets

| State | Authority |
|---|---|
| Images, resources, model/provider config, pricing, routes | Git → Flux → Helm |
| Master key, stable encryption salt, DB credentials, bridge client bearer | SOPS → Kubernetes Secret |
| Recipient keys, scope, expiry, usage, revocation | LiteLLM API → PostgreSQL |
| Rotating subscription OAuth state | Existing CLIProxyAPI/auth PVC only |

`credentials.sops.yaml` contains fresh pilot-only master/salt/database credentials
and the existing bridge's **client bearer**, encrypted for the repository's SOPS
recipients. No credential values go in a ConfigMap or Helm values. The static
bridge bearer is duplicated into this namespace; rotate both gateway and pilot
Secret copies together in a future coordinated change. Never rotate the salt
without a supported data migration. Preserve DB and encryption material together.

`STORE_MODEL_IN_DB=False` avoids competing Git/API model inventories. To add a
provider, update the model list and SOPS Secret in a PR; Helm rolls the pod for
config changes. For a secret-only change, explicitly arrange an approved rollout
(env-var Secret references do not hot-reload). Creating/editing/revoking a friend
key requires **neither a ConfigMap edit nor a Helm reconciliation**.

## Private administration and bot delegation

Only four exact inference routes/methods are public: GET `/v1/models`, POST
`/v1/responses`, `/v1/chat/completions`, `/v1/messages`. Matches require the
Cloudflare tunnel's original HTTPS visitor scheme. Root, UI, docs, health,
key/model-management, files, MCP and arbitrary pass-through routes are not
published. This is intentionally a smaller surface than the current gateway:
Responses compaction and Anthropic count-tokens are not included in this pilot.

After an approved rollout, use private, loopback-only access:

```sh
./scripts/ik --context=hub -n litellm port-forward --address 127.0.0.1 service/litellm 14000:4000
```

The external bot identity is `system:serviceaccount:litellm:litellm-operator`.
Kubernetes/Sam can issue its short-lived credential separately; this PR does not
mint or deliver one. Its namespace Role permits diagnostics/port-forwarding,
read-only HelmRelease status and `get` of exactly `litellm-credentials`. It cannot
edit workloads, ConfigMaps, Secrets or HelmReleases, nor read other namespaces.
Application and migration pods use a separate tokenless ServiceAccount.

This is **application-administrator** access: the named Secret also contains the
pilot DB and bridge credentials. It is not a field-limited or friend-safe
sandbox. Kubernetes `resourceNames` restricts objects, not individual data keys
or patch fields. A HelmRelease `patch` grant would not be annotation-only, so
none is granted. Friend keys receive only LiteLLM's `llm_api` key type, never this
ServiceAccount or the master key.

For a future operational ConfigMap delegation, choose one configuration writer
first. A Flux/Helm-owned ConfigMap must not simultaneously be edited out of band.
A watched `valuesFrom` dependency can trigger Helm updates without giving a bot
HelmRelease patch rights; that is not needed for native virtual-key CRUD here.

## Example friend allocation (no key is pre-created)

Send this JSON to the **private** `/key/generate` API using the master credential
read into process memory from the named Secret:

```json
{
  "key_alias": "friend-astra-trial",
  "key_type": "llm_api",
  "models": ["gpt-6-astra"],
  "duration": "7d",
  "max_budget": 1.0,
  "budget_duration": null,
  "rpm_limit": 60,
  "max_parallel_requests": 1
}
```

Save the returned key once to a recipient-specific `0600` file in a `0700`
directory. Do not put it in Git, terminal output, arguments, tickets or chat.
Read `/key/info` back privately and confirm the effective scope, expiry and
budget before delivery. Share only that recipient key via an approved secret
channel. `/key/update` adjusts policy; `/key/delete` revokes, followed by exact
readback and an authentication rejection check. Preserve existing allocations;
this example does not authorize another live key or migrate the Bifrost trial.

### Budget units and limits — important

LiteLLM's ordinary budget is a **cost** budget, not a native lifetime-token field.
This pilot sets input/output/cache token rates to `0.000001` usage credits per
token, so `max_budget: 1` represents one million reported tokens rather than
provider dollars. The UI/API may label these values with `$`; here they are
**normalized credits**, not money spent, subscription balance, or Extra Usage.
No reset duration means no periodic refill; native seven-day expiry ends access.
Reasoning effort remains client-selected and uses the same usage budget.

This is a **post-usage guardrail**, not an atomic prepaid ceiling. In-flight
requests can overshoot; missing upstream usage, asynchronous DB updates or an
unclean crash can impair accounting. The subscription bridge may also remove
output-token ceilings. Use low concurrency and do not promise an exact hard cap.
The pilot has one replica/process with `Recreate` and no Redis. Do not increase
workers/replicas without re-evaluating shared rate/budget state. PostgreSQL/PVC
persistence is not HA or backup; establish a backup/restore before relying on it.

## Verification and review/rollout boundary

From this directory, with Python + PyYAML, kubectl, Helm and Docker:

```sh
python3 tests/static.py
# Pull the exact IMAGE and PG_IMAGE references in tests/static.py first.
docker pull python:3.13-alpine
python3 tests/containers.py
```

Optionally set `LITELLM_CHART_DIR` to a locally unpacked official 1.105.1 chart.
`LITELLM_TEST_REPORT` writes a secret-free JSON result. The runtime suite uses
actual pinned LiteLLM/Postgres images, a Docker `--internal` network, no published
ports, fake credentials and the existing protocol mock. It does not use live
subscriptions, decrypt production secrets, create Kubernetes resources or
simulate success responses from LiteLLM itself. Containers, network and temporary
DB volume are removed at the end. This verifies application behavior, not the
live ingress/OAuth chain or a Kubernetes Helm installation.

The Flux fan-out is enabled: merging this PR is a rollout decision, because
normal Flux reconciliation will deploy it without a separate manual apply.
Opening this review PR does not authorize that merge or rollout.

After an explicitly approved merge/rollout: confirm Helm migration/readiness,
PVC binding, DNS/route acceptance; privately commission one explicitly authorized
pilot key; test real Codex and Claude, streaming, scope/auth negatives and public
admin 404s; then exercise approved restart/usage persistence. Keep all existing
clients and the Bifrost friend trial on the current gateway. No migration,
retirement, merge, reconciliation or production restart is authorized by opening
this PR.
