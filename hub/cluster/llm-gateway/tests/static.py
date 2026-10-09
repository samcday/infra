"""Security regression checks against the real Kustomize render, not fixtures."""

import json
from pathlib import Path
import sys
import unittest

import yaml

GATEWAY = Path(__file__).resolve().parent.parent
BIFROST_IMAGE = (
    "docker.io/maximhq/bifrost:v2.2.6@sha256:"
    "d0b4708ace1aa175766f94afa715fd9bba3a1b56c1dc2afff15a21edf3ae4e2a"
)
CLI_IMAGE = (
    "docker.io/eceasy/cli-proxy-api:v8.0.22@sha256:"
    "fb287227792ae14eff38063479568049c90cb692b1f299e39c6f54650584428f"
)
PUBLIC_ENDPOINTS = {
    ("GET", "/v1/models"),
    ("POST", "/v1/chat/completions"),
    ("POST", "/v1/responses"),
    ("POST", "/v1/responses/compact"),
    ("POST", "/anthropic/v1/messages"),
    ("POST", "/anthropic/v1/messages/count_tokens"),
}


def load_render(path):
    return [obj for obj in yaml.safe_load_all(Path(path).read_text()) if obj]


def resource(objects, kind, name):
    matches = [
        obj for obj in objects
        if obj["kind"] == kind and obj["metadata"]["name"] == name
    ]
    if len(matches) != 1:
        raise AssertionError(f"expected one {kind}/{name}, found {len(matches)}")
    return matches[0]


def config_data(objects, key):
    matches = [
        obj["data"][key] for obj in objects
        if obj["kind"] == "ConfigMap" and key in obj.get("data", {})
    ]
    if len(matches) != 1:
        raise AssertionError(f"expected one ConfigMap containing {key}")
    return matches[0]


class SecurityContract(unittest.TestCase):
    def test_namespace_and_workload_security(self):
        # The namespace is created by the root namespaces HelmRelease.
        for obj in OBJECTS:
            if obj["kind"] != "Namespace":
                self.assertEqual(obj["metadata"].get("namespace"), "llm-gateway")
        for name, image in [("bifrost", BIFROST_IMAGE), ("cliproxyapi", CLI_IMAGE)]:
            spec = resource(OBJECTS, "Deployment", name)["spec"]
            self.assertEqual(spec["replicas"], 1)
            self.assertEqual(spec["strategy"]["type"], "Recreate")
            pod = spec["template"]["spec"]
            self.assertIs(pod["automountServiceAccountToken"], False)
            self.assertIs(pod["securityContext"]["runAsNonRoot"], True)
            self.assertEqual(pod["securityContext"]["runAsUser"], 1000)
            self.assertEqual(pod["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")
            for container in pod["containers"] + pod.get("initContainers", []):
                self.assertEqual(container["image"], image)
                security = container["securityContext"]
                self.assertIs(security["allowPrivilegeEscalation"], False)
                self.assertIs(security["readOnlyRootFilesystem"], True)
                self.assertEqual(security["capabilities"]["drop"], ["ALL"])
            persistent = {
                volume["name"]: volume["persistentVolumeClaim"]["claimName"]
                for volume in pod["volumes"] if "persistentVolumeClaim" in volume
            }
            mounts = pod["containers"][0]["volumeMounts"]
            data_path = "/app/data" if name == "bifrost" else "/auths"
            mount = next(m for m in mounts if m["mountPath"] == data_path)
            resource(OBJECTS, "PersistentVolumeClaim", persistent[mount["name"]])

    def test_required_credentials_and_command(self):
        bifrost = resource(OBJECTS, "Deployment", "bifrost")["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(bifrost["command"], ["/app/main"])
        args = bifrost["args"]
        self.assertEqual(len(args) % 2, 0)
        flags = dict(zip(args[::2], args[1::2]))
        self.assertEqual(len(flags), len(args) // 2)
        for flag, value in {"-app-dir": "/app/data", "-host": "0.0.0.0", "-port": "8080"}.items():
            self.assertEqual(flags[flag], value)
        pods = {
            name: resource(OBJECTS, "Deployment", name)["spec"]["template"]["spec"]
            for name in ("bifrost", "cliproxyapi")
        }
        for container, names in [
            (bifrost, ["CLIPROXY_API_KEY", "BIFROST_ADMIN_PASSWORD", "BIFROST_ENCRYPTION_KEY"]),
            (pods["cliproxyapi"]["initContainers"][0], ["CLIPROXY_API_KEY"]),
        ]:
            env = {entry["name"]: entry for entry in container["env"]}
            for name in names:
                ref = env[name]["valueFrom"]["secretKeyRef"]
                self.assertEqual(ref["name"], "gateway-credentials")
                self.assertEqual(ref["key"], name)
                self.assertIs(ref.get("optional", False), False)
        cli = pods["cliproxyapi"]["containers"][0]
        self.assertEqual(cli["command"], ["/CLIProxyAPI/CLIProxyAPI"])
        self.assertEqual(cli["args"], ["-config", "/runtime/config.yaml"])
        for probe in ("readinessProbe", "livenessProbe"):
            self.assertEqual(cli[probe], {
                "exec": {"command": [
                    "/usr/bin/timeout", "2", "/bin/bash", "-ec",
                    "exec 3<>/dev/tcp/127.0.0.1/8317",
                ]},
                "timeoutSeconds": 3,
            })
        prepare = pods["cliproxyapi"]["initContainers"][0]
        self.assertEqual(prepare["command"], ["/bin/sh", "/config/prepare-config.sh"])
        config_mount = next(m for m in bifrost["volumeMounts"] if m["mountPath"] == "/app/data/config.json")
        self.assertEqual(config_mount["subPath"], "config.json")
        self.assertIs(config_mount["readOnly"], True)
        for pod in pods.values():
            for volume in pod["volumes"]:
                if "configMap" in volume:
                    resource(OBJECTS, "ConfigMap", volume["configMap"]["name"])

    def test_real_config_sources(self):
        self.assertEqual(
            json.loads(config_data(OBJECTS, "config.json")),
            json.loads((GATEWAY / "bifrost-config.json").read_text()),
        )
        self.assertEqual(
            yaml.safe_load(config_data(OBJECTS, "config.yaml")),
            yaml.safe_load((GATEWAY / "cliproxyapi-config.yaml").read_text()),
        )
        self.assertEqual(
            config_data(OBJECTS, "prepare-config.sh"),
            (GATEWAY / "prepare-cliproxyapi.sh").read_text(),
        )
        config = json.loads(config_data(OBJECTS, "config.json"))
        client = config["client"]
        self.assertIs(client["enforce_auth_on_inference"], True)
        self.assertIs(client["disable_content_logging"], True)
        self.assertIs(client["enable_logging"], False)
        self.assertIs(config["logs_store"]["enabled"], False)
        self.assertEqual(client["whitelisted_routes"], [])
        self.assertEqual(client["header_filter_config"]["allowlist"], [
            "anthropic-beta", "anthropic-version",
        ])
        self.assertEqual(config["source_of_truth"], "config.json")
        self.assertEqual(config["config_store"], {
            "enabled": True, "type": "sqlite", "config": {"path": "/app/data/config.db"},
        })
        self.assertEqual(config["encryption_key"], "env.BIFROST_ENCRYPTION_KEY")
        auth = config.get("governance", {}).get("auth_config", config.get("auth_config", {}))
        self.assertIs(auth["is_enabled"], True)
        self.assertEqual(auth["admin_username"], "sam")
        self.assertEqual(auth["admin_password"], "env.BIFROST_ADMIN_PASSWORD")
        # Omitted DB-owned grants survive config.json-authoritative restarts;
        # present empty sections would explicitly prune API-created state.
        for section in ("virtual_keys", "budgets", "rate_limits"):
            self.assertNotIn(section, config["governance"])
        self.assertEqual(set(config["providers"]), {"cliproxy-openai", "cliproxy-anthropic"})
        for name, base in [("cliproxy-openai", "openai"), ("cliproxy-anthropic", "anthropic")]:
            provider = config["providers"][name]
            self.assertEqual(provider["custom_provider_config"]["base_provider_type"], base)
            self.assertEqual(provider["network_config"]["base_url"], "http://cliproxyapi:8317")
            self.assertIs(provider["network_config"]["allow_private_network"], True)
            allowed = provider["custom_provider_config"]["allowed_requests"]
            self.assertIs(allowed["responses"], True)
            self.assertIs(allowed["responses_stream"], True)
            for key in provider["keys"]:
                self.assertEqual(key["value"], "env.CLIPROXY_API_KEY")
                self.assertEqual(key["models"], ["*"])
        cli = yaml.safe_load(config_data(OBJECTS, "config.yaml"))
        self.assertEqual(cli["management"]["secret-key"], "")
        self.assertIs(cli["management"]["allow-remote"], False)
        self.assertIs(cli["management"]["disable-control-panel"], True)
        self.assertIs(cli["server"]["commercial-mode"], True)
        for field in ("debug", "logging-to-file", "request-log"):
            self.assertIs(cli["observability"]["logs"][field], False)
        self.assertIs(cli["observability"]["pprof"]["enable"], False)
        self.assertFalse(cli.get("access", {}).get("api-keys"))

    def test_public_route_is_an_exact_allowlist(self):
        routes = [obj for obj in OBJECTS if obj["kind"] == "HTTPRoute"]
        self.assertEqual(len(routes), 1)
        route = routes[0]["spec"]
        self.assertEqual(route["hostnames"], ["llm.samcday.com"])
        self.assertEqual(route["parentRefs"], [{
            "name": "public", "namespace": "ingress-nginx", "sectionName": "http",
        }])
        actual = []
        for rule in route["rules"]:
            self.assertEqual(rule["timeouts"], {"request": "0s", "backendRequest": "0s"})
            for backend in rule["backendRefs"]:
                self.assertEqual(backend["name"], "bifrost")
                self.assertEqual(backend.get("namespace", "llm-gateway"), "llm-gateway")
                self.assertEqual(backend["port"], 8080)
            for match in rule["matches"]:
                self.assertEqual(match["path"]["type"], "Exact")
                headers = match["headers"]
                self.assertEqual(len(headers), 1)
                self.assertEqual(headers[0]["name"].lower(), "cf-visitor")
                self.assertEqual(headers[0]["type"], "RegularExpression")
                self.assertEqual(headers[0]["value"], r'^\{\s*"scheme"\s*:\s*"https"\s*\}$')
                self.assertRegex('{"scheme":"https"}', headers[0]["value"])
                self.assertRegex('{ "scheme" : "https" }', headers[0]["value"])
                self.assertNotRegex('{"scheme":"http"}', headers[0]["value"])
                actual.append((match["method"], match["path"]["value"]))
        self.assertEqual(set(actual), PUBLIC_ENDPOINTS)
        self.assertEqual(len(actual), len(PUBLIC_ENDPOINTS))

    def test_cli_is_private_and_only_bifrost_can_enter(self):
        for name in ("bifrost", "cliproxyapi"):
            service = resource(OBJECTS, "Service", name)["spec"]
            self.assertEqual(service.get("type", "ClusterIP"), "ClusterIP")
            self.assertFalse(service.get("externalIPs"))
            labels = resource(OBJECTS, "Deployment", name)["spec"]["template"]["metadata"]["labels"]
            self.assertEqual(labels["app.kubernetes.io/name"], name)
            self.assertEqual(service["selector"], {"app.kubernetes.io/name": name})
        self.assertFalse(any(obj["kind"] == "Ingress" for obj in OBJECTS))
        policies = [obj for obj in OBJECTS if obj["kind"] == "NetworkPolicy"]
        # A second selecting policy could union in wider ingress; prohibit it.
        self.assertEqual(len(policies), 1)
        policy = policies[0]["spec"]
        self.assertEqual(policy["podSelector"], {
            "matchLabels": {"app.kubernetes.io/name": "cliproxyapi"},
        })
        self.assertEqual(policy["policyTypes"], ["Ingress"])
        self.assertEqual(policy["ingress"], [{
            "from": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "bifrost"}}}],
            "ports": [{"protocol": "TCP", "port": 8317}],
        }])


if __name__ == "__main__":
    OBJECTS = load_render(sys.argv.pop(1))
    unittest.main(verbosity=2)
