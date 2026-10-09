# Focused gateway validation

From the repository root:

```sh
sh hub/cluster/llm-gateway/tests/run
sh hub/cluster/llm-gateway/tests/run --containers
```

`run` requires `kubectl`, Python 3 and PyYAML. It runs exactly
`kubectl kustomize hub/cluster/llm-gateway`; no cluster access is involved.
Static checks consume the generated ConfigMaps and compare them with their
source files. There are no copied production-config fixtures.

`--containers` additionally requires Docker and the two pinned deployment
images already present locally. The mock runs in `python:3.13-alpine`, also
already present locally; override with `LLM_GATEWAY_TEST_PYTHON_IMAGE` to use
another local image containing `python3`. Images are never automatically
pulled. For example, when a local development image has Python:

```sh
LLM_GATEWAY_TEST_PYTHON_IMAGE=rust:1-trixie \
  sh hub/cluster/llm-gateway/tests/run --containers
```

The suite generates **fake, per-run credentials**, creates its VKs through
Bifrost's private authenticated admin API, and overrides provider base URLs
and `framework.pricing` in a temporary copy of the rendered Bifrost config.
Cold-start Bifrost requires a pricing catalog: supported air-gapped `file://`
URLs point at generated synthetic prices and empty capability metadata, with
external MCP catalog and background live-model refresh disabled. An internal Docker
network prevents internet egress. **No ports are published**, even on loopback;
the HTTP client runs inside the mock container with `docker exec`.
CLIProxyAPI uses the real rendered config and preparation script,
with an empty temporary auth directory: no OAuth, real provider keys, cluster
credentials or real inference. Temporary state, containers and the network are
cleaned in `finally` (and the render file by the shell trap). HTTP/startup
waits and Docker commands are bounded.

Checks cover CLI key preparation, actual image auth, disabled management,
non-root/read-only root operation; Bifrost inference/admin credential
separation, OpenAI chat/Responses and Anthropic content/usage/SSE, request and
post-hoc token limits, and persistence across a graceful restart.

## What this does not prove

- Static HTTPRoute checks prove only the manifest allowlist, forwarded-proto
  match and disabled stream timeouts. They do **not** prove live Cloudflare TLS,
  trustworthy header handling, Gateway implementation support or routing.
- Docker isolation is not a live test of Kubernetes NetworkPolicy, storage
  attachment, SOPS/Secret reconciliation or pod security admission. The local
  Docker SELinux label check is disabled to permit temporary bind mounts;
  non-root, read-only root, dropped capabilities and no-new-privileges remain.
- The mock proves protocol forwarding and usage transport, not provider OAuth,
  subscription entitlements, model availability or real generation quality.
- No dollar-budget correctness claim is made: deterministic fictional models
  do not have real pricing. Token limits are checked with reported mock usage.
- Cleanup cannot run after SIGKILL or Docker daemon failure. Containers and
  networks have a unique `llm-gateway-test-` prefix for manual identification.
