# Shared LLM gateway

The proposed public inference endpoint is `https://llm.samcday.com`. This
component centralizes CLIProxyAPI and places Bifrost's per-person virtual keys
and governance in front of it.

```text
personal machines / friends / a capped Hermes client
  → Cloudflare HTTPS + existing hub Tunnel
  → ingress-nginx/public Gateway, exact inference routes only
  → Bifrost, mandatory virtual key
  → private CLIProxyAPI
  → configured upstream providers
```

This is an inference deployment, not a completed Discord enrollment service.
No provider login or user virtual key is seeded by these manifests. Do not
advertise it as ready for friends until the commissioning checks below pass.

## Boundaries and ownership

- The existing Cloudflare Tunnel and external-dns configuration supply the
  public front door; no new tunnel or Cloudflare AI Gateway is required.
- `route.yaml` is an explicit method/path allowlist, not a `/` or `/v1/*`
  proxy. Neither Bifrost's dashboard and `/api` management routes nor
  CLIProxyAPI's management, OAuth callback, discovery, or UI routes are public.
- The route requires Cloudflare's `X-Forwarded-Proto: https`. Clients must
  always start with HTTPS; a server rejection cannot undo a key already sent
  over plaintext HTTP. HSTS is supplementary, not an API-client security
  boundary.
- Public authentication is a Bifrost virtual key, never the internal
  CLIProxyAPI key, Bifrost admin password, or provider OAuth token.
- CLIProxyAPI accepts only the generated internal key. Its Service has an
  ingress NetworkPolicy permitting only Bifrost pods in this namespace.
- Neither workload has a Kubernetes service-account token. Both run non-root
  with a read-only root filesystem and explicitly writable state.
- Bifrost has one replica with `Recreate` updates. OSS replicas do not share
  live quota counters; do not scale this Deployment to get availability.
- The auth PVC contains current, rotating provider credentials. The Bifrost
  PVC contains configuration, issued keys, and usage state. Treat both as
  sensitive; persistence is **not** a backup plan.
- Request-content logging is disabled. CLIProxyAPI commercial mode also
  disables error-request payload capture, which `request-log: false` alone
  does not disable. Cloudflare still terminates TLS and processes traffic.
- Client-controlled extra-header forwarding is limited to `anthropic-beta`
  and `anthropic-version` using `header_filter_config`. An empty allowlist
  means allow-all in this Bifrost version. `allowed_headers` is a separate
  CORS/WebSocket setting, not an outbound forwarding policy. Integrations
  also have fixed protocol-specific headers; add session-header forwarding
  only when the relevant client/provider workflow has been tested.

The small init script adds the internal key to CLIProxyAPI's config in a
memory-backed volume. CLIProxyAPI v8 does not expand arbitrary environment
references in YAML. All non-secret settings remain reviewable in
`cliproxyapi-config.yaml`; the shared key must use a URL-safe alphabet.

## Configuration and state

`bifrost-config.json` defines two protocol-preserving custom providers:

| Provider | Model identifier |
| --- | --- |
| `cliproxy-openai` | `cliproxy-openai/<upstream-model-id>` |
| `cliproxy-anthropic` | `cliproxy-anthropic/<upstream-model-id>` |

The base URL points at the same-namespace `cliproxyapi` Service **without**
`/v1`; Bifrost appends the appropriate upstream endpoint. Do not silently
replace Anthropic transport with chat-completion translation.

Both fixed CLIProxyAPI providers explicitly enable
`network_config.allow_private_network`: Bifrost otherwise rejects the
Service's private ClusterIP. This exception is provider-scoped; it is not a
global bypass or permission for clients to choose arbitrary upstream URLs.

A fresh Bifrost database also needs its initial pricing-catalog download.
Outbound DNS and HTTPS are startup dependencies, not just inference
dependencies: without existing pricing data, a failed catalog fetch prevents
startup. The isolated container tests must supply a mock pricing catalog
rather than relying on Internet access or real provider accounts.

Git owns the providers and security settings. Bifrost's split source-of-truth
mode is deliberately **not** used: it can retain dashboard edits when the
file's hash has not changed. `source_of_truth: "config.json"` makes the file
authoritative on startup. The `virtual_keys`, `budgets`, and `rate_limits`
governance sections are **omitted**, not empty arrays, so API-created grants
remain database-managed rather than being pruned on restart. Preserve that
distinction and run the restart test when upgrading Bifrost.

Keep user keys out of the ConfigMap, Git, chat transcripts, and Discord
messages. Use narrow provider/model allowlists on each virtual key, rather
than automatically granting all future models.

Required Secret `gateway-credentials`:

| Entry | Used by |
| --- | --- |
| `CLIPROXY_API_KEY` | CLIProxyAPI config initializer and Bifrost upstream auth |
| `BIFROST_ADMIN_PASSWORD` | Bifrost private administration, username `sam` |
| `BIFROST_ENCRYPTION_KEY` | Bifrost's sensitive database-field encryption |

`credentials.sops.yaml` includes fresh random values encrypted to the root
repo's hub and SSH-recovery recipients. Generation used an in-memory pipe
into SOPS; no plaintext credential file was written. This Secret contains no
provider login or personal virtual key.

Keep these values encrypted. Missing Secret keys must prevent startup; never
substitute example passwords or enable keyless operation. Keep the encryption
key with any Bifrost backup and do not rotate it without the upstream migration
procedure. Secret environment changes require a controlled workload restart.

## Budget semantics

Each person gets a distinct virtual key. The owner chooses their allowed
models, request/token limits, reset intervals, and any dollar budget.
There are deliberately no default grants or financial presets here.

For subscription-backed traffic, gateway dollar accounting is a local
valuation, not a measurement of the provider's remaining subscription
allowance. Custom model prices must be verified before relying on a dollar
budget. Token/request limits are useful independently.

Everyone still consumes the upstream account's shared limits. Calls made
directly from another machine bypass gateway accounting. Counters are
periodically persisted and requests may be in flight when a limit is reached;
neither crash recovery nor concurrent requests provide an exact hard billing
ceiling. Start with conservative limits and test rejection behavior.

## Clients

First obtain a personal virtual key privately from the owner. Use the exact
model IDs allowed by that key; do not paste secrets into repository configs.

- **OpenAI-compatible clients:** base URL `https://llm.samcday.com/v1`,
  `Authorization: Bearer <personal-virtual-key>`.
- **Codex:** use a named custom provider with that base URL, `wire_api =
  "responses"` and `supports_websockets = false`. Do not expect a ChatGPT
  login to automatically route through this endpoint.
- **Anthropic-compatible clients / Claude Code:** base URL
  `https://llm.samcday.com/anthropic`, personal key in the client's supported
  bearer or `x-api-key` setting. The client appends `/v1/messages`.
- **Hermes:** later provision an inference-only bot key with its own limits.
  Do not migrate its working local Codex setup until streaming and tools have
  been tested through this gateway.

Only the exact paths in `route.yaml` are supported publicly. File uploads,
batch APIs, arbitrary response retrieval, WebSockets, admin APIs, and browser
CORS access are not implicitly exposed.

## Discord / Hermes integration contract

Coordination thread:
[Design Hermes Discord voice agent integration](delta://thread/ksQQS1akDwLkRO2HYop0Zt4jj5LOAGtbPJIBxBDTetr0VIFMfrpsKQV3XS2z).

Target guild: `1558037422108057672`. Existing bot application:
`1558036971769565274`. The bot remains in its existing VM; this deployment
does not move it or widen its normal owner-only agent admission policy.

The VM currently runs agent tools and the Discord gateway under the same Unix
identity. A privileged token handed to a plugin is therefore also available
to agent tools. Native slash-command checks alone are not a sufficient
boundary for financial authority.

The agreed safe, optional first interface is credential-free:

| Command | Intended behavior |
| --- | --- |
| `/gateway access` | Ephemeral link to independently authenticated enrollment |
| `/gateway status` | Ephemeral link to the caller's authenticated account page |
| `/gateway admin` | Owner-only link to independently authenticated administration |

These pages and commands are **not implemented by this component**. Do not
configure placeholder links or tell users that joining the guild grants
access. The Hermes thread owns the optional plugin and currently needs its
project mount restored before it can implement it.

Before enabling this interface:

1. Obtain approval for the browser-based flow and choose its real HTTPS URLs.
2. Independently authenticate the human, for example through Discord OAuth.
   Verify guild membership and immutable Discord user ID server-side.
   User IDs or role claims supplied by Hermes are untrusted hints.
3. Require owner approval for enrollment and increases in limits. Source the
   owner's identity from an explicit policy, never a display name or an LLM.
4. Show keys only in the authenticated web UI, not to Hermes, model context,
   public messages, or a link bearer. Bind any enrollment intent to verified
   identity, expiry, and an idempotency key.
5. Keep the bot unable to approve grants, raise limits, read other users'
   keys, or impersonate the owner. Do not give it a cluster or Bifrost admin
   credential.

Entirely in-Discord approval needs a stronger, separate trusted interaction
receiver. Changing this bot's HTTP interaction endpoint would divert its
existing slash commands and must not be done silently.

## Local verification

From the repository root:

```sh
sh hub/cluster/llm-gateway/tests/run
sh hub/cluster/llm-gateway/tests/run --containers
```

The first command renders with `kubectl kustomize` and checks the security
invariants without contacting a cluster. Container checks additionally need
Docker, the pinned gateway images, and a local Python-capable fixture image.
The runner does not pull images; prepare them deliberately. Its default
fixture image is `python:3.13-alpine`; an existing compatible image can be
selected with `LLM_GATEWAY_TEST_PYTHON_IMAGE`.

Container tests use fake credentials, synthetic pricing/model-parameter
catalogs, a deterministic mock provider, and an internal Docker network with
no published ports. They exercise the pinned application binaries, not real
provider accounts. They do not prove Cilium policy enforcement, Cloudflare
TLS/streaming, live PVC ownership, or subscription credential validity.

## Commissioning

Repository changes go through review and Flux. This component's addition is
not authorization to apply resources, exec into live pods, import credentials,
or otherwise mutate the live cluster directly.

1. Review the hostname, pinned images, Secret handling, route allowlist, and
   persistence. Render with `kubectl kustomize hub/cluster/llm-gateway`.
2. Review the included SOPS-encrypted Secret and confirm the hub can decrypt
   it before enabling the Flux Kustomization. Retrieve the admin password
   privately with SOPS when needed; never paste it into a transcript.
3. After approved landing, check the Flux revision and Deployment readiness
   on context `hub`, namespace `llm-gateway`.
4. Access Bifrost administration only through an owner-controlled local
   forwarding path, with its separate admin password. It must not be
   available at the public inference hostname.
5. Agree and perform the provider-auth cutover separately. Stop old instances
   that share a rotating refresh credential before migrating its latest state
   or doing a fresh hub login. Do not copy a stale login snapshot to several
   running instances. CLIProxyAPI management is disabled; any temporary
   onboarding mechanism needs separate approval and must remain private.
6. Create one tightly capped owner test key. Verify models, a streamed
   response and a tool call through the **public HTTPS endpoint**, plus
   rejection of missing/wrong/revoked keys and exhausted limits. Confirm
   public admin, OAuth and unsupported paths return no backend access.
7. Test a delayed first token exceeding Cloudflare's documented 125-second
   proxy-read window and a long-running stream. Gateway timeouts set to zero
   do not disable Cloudflare's own limits. Do not assume a heartbeat survives
   every protocol adapter or protects non-streaming requests.
8. Test a graceful restart without losing keys/limits, then establish an
   encrypted backup/restore procedure for both PVCs and the encryption key.
9. Only then issue friend/bot keys. Discord automation remains a separate
   reviewed rollout.

Cloudflare Tunnel supplies connectivity, TLS, and edge controls; it does not
subsidize the providers' token bills or accelerate model generation. Leave
response caching off for private, stateful coding-agent traffic. Adding
Cloudflare AI Gateway would introduce another processing/accounting layer;
there is no demonstrated need for it in this initial setup.
