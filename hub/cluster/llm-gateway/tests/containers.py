"""Local pinned-image checks; derived production config and FAKE credentials only."""

import base64
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import time

import yaml

from static import BIFROST_IMAGE, CLI_IMAGE, config_data, load_render, resource

TEST_DIR = Path(__file__).resolve().parent
DOCKER = None
HTTP_CONTAINER = None


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def request(base, path, body=None, headers=None):
    result = DOCKER.command(
        "exec", "-i", HTTP_CONTAINER, "python3", "-B", "/test/http_client.py",
        stdin=json.dumps({"url": base + path, "body": body, "headers": headers or {}}),
    )
    status, data = json.loads(result.stdout)
    if status == 0:
        raise OSError(f"{path}: connection failed")
    return status, data


def expect(base, path, status, body=None, headers=None):
    actual, data = request(base, path, body, headers)
    # Never dump admin response bodies (they can contain generated credentials).
    require(actual == status, f"{path}: expected HTTP {status}, got {actual}")
    return data


def wait_ready(base, path, headers=None):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            if request(base, path, headers=headers)[0] == 200:
                return
        except OSError:
            pass
        time.sleep(0.25)
    raise AssertionError(f"{path}: not ready within 45 seconds")


class Docker:
    def __init__(self, env):
        self.env = {**os.environ, **env}
        self.prefix = "llm-gateway-test-" + secrets.token_hex(5)
        self.containers = []
        self.network_created = False

    def command(self, *args, check=True, stdin=None):
        result = subprocess.run(
            ["docker", *args], env=self.env, capture_output=True, text=True,
            input=stdin, timeout=65,
        )
        if check and result.returncode:
            # Arguments only include env *names*, never env credential values.
            raise RuntimeError(f"docker {' '.join(args)} failed: {result.stderr.strip()}")
        return result

    def network(self):
        self.command("network", "create", "--internal", self.prefix)
        self.network_created = True
        require(self.command("network", "inspect", "--format", "{{.Internal}}", self.prefix).stdout.strip() == "true", "network is not internal")

    def run(self, name, image, args=(), mounts=(), alias=None, env=(), network=None):
        name = self.prefix + "-" + name
        # Register before starting, so signals/failed starts still clean up.
        self.containers.append(name)
        command = [
            "run", "-d", "--pull=never", "--name", name,
            "--network", network or self.prefix,
            "--user", "1000:1000", "--read-only",
            "--cap-drop=ALL", "--security-opt=no-new-privileges",
            # Local SELinux hosts otherwise deny temporary bind mounts. This
            # suite does not claim to test Kubernetes/SELinux admission policy.
            "--security-opt=label=disable",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=32m",
        ]
        if alias:
            command += ["--network-alias", alias]
        for source, target, readonly in mounts:
            command += ["-v", f"{source}:{target}" + (":ro" if readonly else "")]
        for key in env:
            command += ["-e", key]
        # Bypass image wrappers just as the production Deployments do.
        command += ["--entrypoint", args[0], image, *args[1:]]
        self.command(*command)
        ports = self.command("inspect", "--format", "{{json .HostConfig.PortBindings}}", name).stdout.strip()
        require(ports in ("{}", "null"), "test container has published ports")
        return name

    def cleanup(self):
        failures = []
        for name in reversed(self.containers):
            try:
                result = self.command("rm", "-f", name, check=False)
            except (OSError, subprocess.TimeoutExpired):
                failures.append(name)
                continue
            if result.returncode and "No such container" not in result.stderr:
                failures.append(name)
        if self.network_created:
            try:
                if self.command("network", "rm", self.prefix, check=False).returncode:
                    failures.append(self.prefix)
            except (OSError, subprocess.TimeoutExpired):
                failures.append(self.prefix)
        require(not failures, "Docker cleanup failed for: " + ", ".join(failures))


def writable_dir(root, name):
    path = root / name
    path.mkdir()
    path.chmod(0o777)  # Only ephemeral fake test state, writable by image UID 1000.
    return path


def cli_checks(docker, objects, root):
    source = root / "cli-config"
    source.mkdir()
    (source / "config.yaml").write_text(config_data(objects, "config.yaml"))
    (source / "prepare-config.sh").write_text(config_data(objects, "prepare-config.sh"))
    runtime = writable_dir(root, "cli-runtime")
    auth = writable_dir(root, "cli-auth")
    mounts = [(source, "/config", True), (runtime, "/runtime", False)]
    for index, invalid in enumerate(["", "space key", "colon:key", 'quote"key', "line\nkey"]):
        docker.env["CLIPROXY_API_KEY"] = invalid
        name = docker.run(
            "invalid-" + str(index), CLI_IMAGE, ["/bin/sh", "/config/prepare-config.sh"],
            mounts=mounts, env=["CLIPROXY_API_KEY"], network="none",
        )
        code = docker.command("wait", name).stdout.strip()
        require(code == "1", "unsafe key was not rejected by the validation script")
        logs = docker.command("logs", name)
        require("must be a nonempty URL-safe token" in logs.stderr, "key validation did not execute")
        require(not (runtime / "config.yaml").exists(), "invalid key wrote runtime config")
    docker.env["CLIPROXY_API_KEY"] = FAKE_PROXY_KEY
    name = docker.run(
        "prepare", CLI_IMAGE, ["/bin/sh", "/config/prepare-config.sh"],
        mounts=mounts, env=["CLIPROXY_API_KEY"], network="none",
    )
    require(docker.command("wait", name).stdout.strip() == "0", "valid fake key failed preparation")
    generated = yaml.safe_load((runtime / "config.yaml").read_text())
    require(generated.pop("access") == {"api-keys": [FAKE_PROXY_KEY]}, "incorrect injected access key")
    require(generated == yaml.safe_load((source / "config.yaml").read_text()), "preparation changed base config")
    require((runtime / "config.yaml").stat().st_mode & 0o077 == 0, "runtime key file not private")
    pod = resource(objects, "Deployment", "cliproxyapi")["spec"]["template"]["spec"]
    container = pod["containers"][0]
    name = docker.run(
        "cli", container["image"], container["command"] + container["args"],
        mounts=[(runtime, "/runtime", False), (auth, "/auths", False)], alias="cli",
    )
    base = "http://cli:8317"
    valid = {"Authorization": "Bearer " + FAKE_PROXY_KEY}
    wait_ready(base, "/v1/models", valid)
    expect(base, "/v1/models", 401)
    expect(base, "/v1/models", 401, headers={"Authorization": "Bearer fake-invalid"})
    expect(base, "/v1/models", 200, headers=valid)
    expect(base, "/v0/management/config", 404, headers=valid)
    expect(base, "/management.html", 404, headers=valid)
    expect(base, "/", 200)
    for probe in ("readinessProbe", "livenessProbe"):
        docker.command("exec", name, *container[probe]["exec"]["command"])
    require(docker.command("exec", name, "id", "-u").stdout.strip() == "1000", "CLI ran as root")
    mounts = docker.command("exec", name, "cat", "/proc/mounts").stdout.splitlines()
    root_options = next(line.split()[3] for line in mounts if line.split()[1] == "/")
    require("ro" in root_options.split(","), "CLI root mount is not read-only")
    require(docker.command("exec", name, "touch", "/root-write-test", check=False).returncode != 0, "CLI root filesystem writable")
    print("PASS CLI image: key validation/auth, disabled management, loopback probes, UID1000 and read-only root")


def bifrost_checks(docker, objects, root):
    mock_url = "http://mock-provider:8317"
    config = json.loads(config_data(objects, "config.json"))
    # Cold-start Bifrost requires catalog data. Use supported air-gapped file
    # URLs and synthetic prices rather than contacting its public catalog.
    catalog = root / "catalog"
    catalog.mkdir()
    (catalog / "pricing.json").write_text(json.dumps({
        model: {
            "provider": provider, "base_model": model, "mode": "chat",
            "input_cost_per_token": 0.000001, "output_cost_per_token": 0.000002,
        }
        for model, provider in [
            ("mock-openai", "cliproxy-openai"), ("mock-anthropic", "cliproxy-anthropic"),
        ]
    }))
    (catalog / "parameters.json").write_text("{}")
    config.setdefault("framework", {})["pricing"] = {
        "pricing_url": "file:///test-data/pricing.json",
        "model_parameters_url": "file:///test-data/parameters.json",
        "pricing_sync_interval": 86400,
        "mcp_library_sync_interval": 0,
        "live_models_sync_interval": 0,
    }
    # All inference traffic goes to the deterministic provider.
    for provider in config["providers"].values():
        provider["network_config"]["base_url"] = "http://mock-provider:8317"
    config_file = root / "bifrost-config.json"
    config_file.write_text(json.dumps(config))
    data_dir = writable_dir(root, "bifrost-data")
    pod = resource(objects, "Deployment", "bifrost")["spec"]["template"]["spec"]
    container = pod["containers"][0]
    bifrost = docker.run(
        "bifrost", container["image"], container["command"] + container["args"],
        mounts=[
            (data_dir, "/app/data", False), (config_file, "/app/data/config.json", True),
            (catalog, "/test-data", True),
        ],
        alias="bifrost", env=["CLIPROXY_API_KEY", "BIFROST_ADMIN_PASSWORD", "BIFROST_ENCRYPTION_KEY"],
    )
    base = "http://bifrost:8080"
    admin = {"Authorization": "Basic " + base64.b64encode(
        ("sam:" + docker.env["BIFROST_ADMIN_PASSWORD"]).encode()
    ).decode()}
    endpoint = "/api/governance/virtual-keys"
    wait_ready(base, endpoint, admin)
    expect(base, endpoint, 401)
    expect(base, endpoint, 401, headers={"Authorization": "Bearer " + FAKE_PROXY_KEY})

    def create_key(name, **extras):
        body = {"name": name, "provider_configs": [
            {"provider": "cliproxy-openai", "allowed_models": ["mock-openai"], "key_ids": ["*"], "weight": 1},
            {"provider": "cliproxy-anthropic", "allowed_models": ["mock-anthropic"], "key_ids": ["*"], "weight": 1},
        ], **extras}
        key = expect(base, endpoint, 200, body, admin)["virtual_key"]
        require(isinstance(key["value"], str) and key["value"].startswith("sk-bf-"), "unexpected VK response contract")
        return key

    vk = create_key("permitted")
    bearer = {"Authorization": "Bearer " + vk["value"]}
    anthropic_headers = {"x-api-key": vk["value"], "anthropic-version": "2023-06-01"}
    chat = {"model": "cliproxy-openai/mock-openai", "messages": [{"role": "user", "content": "fake test input"}]}
    claude = {"model": "cliproxy-anthropic/mock-anthropic", "messages": chat["messages"], "max_tokens": 16}
    responses = {"model": chat["model"], "input": "fake test input"}
    before = len(expect(mock_url, "/__requests", 200))
    for path, body in [
        ("/v1/chat/completions", chat), ("/v1/responses", responses),
        ("/v1/responses/compact", responses), ("/anthropic/v1/messages", claude),
        ("/anthropic/v1/messages/count_tokens", claude),
    ]:
        for headers in [{}, {"Authorization": "Bearer fake-invalid"}, admin, {"Authorization": "Bearer " + FAKE_PROXY_KEY}]:
            expect(base, path, 401, body, headers)
    expect(base, "/v1/models", 401)
    expect(base, "/v1/models", 401, headers={"Authorization": "Bearer fake-invalid"})
    require(len(expect(mock_url, "/__requests", 200)) == before, "rejected inference reached provider")
    expect(base, endpoint, 401, headers=bearer)
    print("PASS Bifrost: missing/invalid inference keys rejected; admin, proxy key and VK auth separated")

    result = expect(base, "/v1/chat/completions", 200, chat, {
        **bearer, "x-bf-eh-x-unapproved": "must-not-forward",
    })
    require(result["choices"][0]["message"]["content"] == "mock answer", "chat content lost")
    require(result["usage"]["total_tokens"] == 10, "chat usage lost")
    result = expect(base, "/v1/responses", 200, responses, bearer)
    require(result["output"][0]["content"][0]["text"] == "mock answer", "Responses content lost")
    require(result["usage"]["total_tokens"] == 10, "Responses usage lost")
    result = expect(base, "/anthropic/v1/messages", 200, claude, anthropic_headers)
    require(result["content"][0]["text"] == "mock answer", "Anthropic content lost")
    require(result["usage"]["input_tokens"] == 7 and result["usage"]["output_tokens"] == 3, "Anthropic usage lost")
    for path, body, headers, final_event in [
        ("/v1/chat/completions", {**chat, "stream_options": {"include_usage": True}}, bearer, "[DONE]"),
        ("/v1/responses", responses, bearer, "response.completed"),
        ("/anthropic/v1/messages", claude, anthropic_headers, "message_stop"),
    ]:
        stream = expect(base, path, 200, {**body, "stream": True}, headers)
        require(isinstance(stream, str) and "mock answer" in stream, f"{path}: stream content lost")
        require(final_event in stream and "usage" in stream, f"{path}: terminal event or stream usage missing")
        events = [
            json.loads(line[6:]) for line in stream.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        if path == "/v1/chat/completions":
            require(any((event.get("usage") or {}).get("total_tokens") == 10 for event in events), "chat stream usage lost")
        elif path == "/v1/responses":
            completed = next(event for event in events if event.get("type") == "response.completed")
            require(completed["response"]["usage"]["total_tokens"] == 10, "Responses stream usage lost")
        else:
            start = next(event for event in events if event.get("type") == "message_start")
            delta = next(event for event in events if event.get("type") == "message_delta")
            require(start["message"]["usage"]["input_tokens"] == 7 and delta["usage"]["output_tokens"] == 3, "Anthropic stream usage lost")
    recorded = expect(mock_url, "/__requests", 200)
    require(all(entry["authorized"] for entry in recorded), "provider credential was not forwarded")
    require(all(entry["unapproved_header"] is None for entry in recorded), "unapproved x-bf-eh header reached provider")
    require({entry["path"] for entry in recorded} == {"/v1/chat/completions", "/v1/responses", "/v1/messages"}, "unexpected provider paths")
    require({entry["body"]["model"] for entry in recorded} == {"mock-openai", "mock-anthropic"}, "provider model prefixes not removed")
    print("PASS Bifrost: permitted VK forwards OpenAI/Anthropic content, usage and streaming")

    limits = [
        ("requests", {"request_max_limit": 1, "request_reset_duration": "1h"}),
        # Token accounting is post-hoc: first response consumes 10, next blocked.
        ("tokens", {"token_max_limit": 5, "token_reset_duration": "1h"}),
    ]
    limited_keys = []
    for name, limit in limits:
        key = create_key(name, rate_limit=limit)
        limited_keys.append(key)
        headers = {"Authorization": "Bearer " + key["value"]}
        expect(base, "/v1/chat/completions", 200, chat, headers)
        before = len(expect(mock_url, "/__requests", 200))
        expect(base, "/v1/chat/completions", 429, chat, headers)
        require(len(expect(mock_url, "/__requests", 200)) == before, "rate-limited request reached provider")
    print("PASS Bifrost: per-VK request and post-hoc token limits enforced")

    docker.command("stop", "--time", "30", bifrost)
    exit_code = docker.command("inspect", "--format", "{{.State.ExitCode}}", bifrost).stdout.strip()
    require(exit_code == "0", "Bifrost did not stop gracefully")
    require(list(data_dir.glob("*.db")), "no persistent SQLite database created")
    docker.command("start", bifrost)
    wait_ready(base, endpoint, admin)
    persisted = expect(base, endpoint + "/" + vk["id"], 200, headers=admin)["virtual_key"]
    require(persisted["value"] == vk["value"], "VK did not survive restart")
    expect(base, "/v1/chat/completions", 200, chat, bearer)
    for key in limited_keys:
        expect(base, "/v1/chat/completions", 429, chat, {"Authorization": "Bearer " + key["value"]})
    print("PASS Bifrost: VK and limit state survive graceful restart of same persistent data")


def interrupted(signum, _frame):
    raise KeyboardInterrupt(f"signal {signum}")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    objects = load_render(sys.argv[1])
    FAKE_PROXY_KEY = "test-only-" + secrets.token_urlsafe(24)
    docker = Docker({
        "CLIPROXY_API_KEY": FAKE_PROXY_KEY,
        "BIFROST_ADMIN_PASSWORD": "test-only-" + secrets.token_urlsafe(24),
        "BIFROST_ENCRYPTION_KEY": secrets.token_hex(16),
    })
    DOCKER = docker
    with tempfile.TemporaryDirectory(prefix="llm-gateway-test-") as directory:
        root = Path(directory)
        root.chmod(0o755)
        try:
            # Fail without pulling: this suite never needs registry/network access.
            for image in [BIFROST_IMAGE, CLI_IMAGE, os.environ.get("LLM_GATEWAY_TEST_PYTHON_IMAGE", "python:3.13-alpine")]:
                docker.command("image", "inspect", image)
            docker.network()
            HTTP_CONTAINER = docker.run(
                "mock", os.environ.get("LLM_GATEWAY_TEST_PYTHON_IMAGE", "python:3.13-alpine"),
                ["python3", "-B", "/test/mock_provider.py"],
                mounts=[(TEST_DIR, "/test", True)], alias="mock-provider", env=["CLIPROXY_API_KEY"],
            )
            wait_ready("http://mock-provider:8317", "/__requests")
            cli_checks(docker, objects, root)
            bifrost_checks(docker, objects, root)
        finally:
            docker.cleanup()
