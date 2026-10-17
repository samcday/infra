# Focused gateway validation

From the repository root:

```sh
sh hub/cluster/llm-gateway/tests/run
sh hub/cluster/llm-gateway/tests/run --containers
```

`run` requires `kubectl`, Python 3 and PyYAML. It runs exactly
`kubectl kustomize hub/cluster/llm-gateway`; no cluster access is involved.
Static checks consume the generated ConfigMaps and compare them with their
source files. They check MiMo's native `api-keys.claude` provider shape and the
dedicated SOPS-encrypted `mimo-credentials` Secret's required
`MIMO_CODE_API_KEY` reference on the init container only. No decryption is
performed. There are no copied production-config fixtures.

The default suite also executes `prepare-cliproxyapi.sh` with synthetic YAML
and fake keys under a temporary directory. Optional input/output path
arguments permit this without changing the production defaults
(`/config/config.yaml`, `/runtime/config.yaml`). Tests cover literal
substitution of every allowed ASCII graph character (including shell
metacharacters), both keys' validation, missing/duplicate/unquoted/unresolved
placeholders, unchanged unrelated settings, no secret output, no writes on
invalid secrets, and mode `0600`, including replacement of an existing file.
Literal `__MIMO_CODE_API_KEY__` and `@@...@@` markers, and entire `@...@`
values, are also rejected when they arrive as the environment credential
rather than remaining in the input.
The shared key stays URL-safe; the MiMo key accepts all nonempty ASCII graph
characters except double quote/backslash, not just URL-safe or base64 tokens.

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
with a generated fake MiMo key containing punctuation and an empty temporary
auth directory: no OAuth, real provider keys, cluster credentials or real
inference. Temporary state, containers and the network are
cleaned in `finally` (and the render file by the shell trap). HTTP/startup
waits and Docker commands are bounded.

Checks cover missing/invalid keys in the image's shell, exact rendered-config
comparison after MiMo substitution, and registration of `mimo-v2.6-pro`,
`mimo-v2.6-pro[1m]` and `mimo-v2.6-flash` by the actual pinned CLI image.
No MiMo inference is requested: model discovery is local, with internet
egress blocked. Existing checks cover actual image auth, disabled management,
container-local loopback probe commands, non-root/read-only root operation;
Bifrost inference/admin credential separation, OpenAI chat/Responses and
Anthropic content/usage/SSE, request and post-hoc token limits, and persistence
across a graceful restart.

## What this does not prove

- Static HTTPRoute checks prove only the manifest allowlist, Cloudflare
  visitor-scheme match and disabled stream timeouts. They do **not** prove live
  Cloudflare TLS, trustworthy header handling, Gateway implementation support
  or routing.
- Docker isolation is not a live test of Kubernetes NetworkPolicy, storage
  attachment, SOPS/Secret reconciliation or pod security admission. The local
  Docker SELinux label check is disabled to permit temporary bind mounts;
  non-root, read-only root, dropped capabilities and no-new-privileges remain.
- The mock proves protocol forwarding and usage transport, not provider OAuth,
  subscription entitlements, model availability or real generation quality.
  MiMo registration does not validate its real subscription key or upstream
  inference, nor establish that the encrypted entry came from a real runtime
  credential rather than a template. See the main README for dedicated-Secret
  onboarding and the controlled restart required after a Secret-only key change.
- No dollar-budget correctness claim is made: deterministic fictional models
  do not have real pricing. Token limits are checked with reported mock usage.
- Cleanup cannot run after SIGKILL or Docker daemon failure. Containers and
  networks have a unique `llm-gateway-test-` prefix for manual identification.
