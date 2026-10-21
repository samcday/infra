"""Services-only routing contract against the actual Flux Kustomize render."""

import json
from pathlib import Path
import re
import shlex
import subprocess
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[4]
HEADSCALE = ROOT / "hub/cluster/headscale"
ROUTER = "services-subnet-router"
PREFIX = "10.0.2.0/24"
TAG = "tag:hub-services-subnet-router"


class ServicesSubnetRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rendered = subprocess.check_output(
            ["kubectl", "kustomize", str(HEADSCALE)], text=True,
        )
        cls.objects = [obj for obj in yaml.safe_load_all(rendered) if obj]

    def resource(self, kind, name):
        matches = [
            obj for obj in self.objects
            if obj["kind"] == kind and obj["metadata"]["name"] == name
        ]
        self.assertEqual(len(matches), 1, f"expected one {kind}/{name}")
        return matches[0]

    def policy(self):
        configs = [
            obj["data"]["acls.json"] for obj in self.objects
            if obj["kind"] == "ConfigMap" and "acls.json" in obj.get("data", {})
        ]
        self.assertEqual(len(configs), 1)
        # Repository policy uses whole-line HuJSON comments. The real Headscale
        # parser is also exercised by the optional container validation.
        return json.loads(re.sub(r"(?m)^\s*//.*$", "", configs[0]))

    def test_only_services_are_autoapproved_for_the_dedicated_router_tag(self):
        policy = self.policy()
        self.assertEqual(policy.get("autoApprovers"), {
            "routes": {PREFIX: [TAG]},
        })
        self.assertEqual(policy["tagOwners"].get(TAG), [])
        # Existing personal access remains; tagged infrastructure gains no new
        # network grants merely because service routing is restored.
        self.assertEqual(policy["acls"][0], {
            "action": "accept", "src": ["sam@"], "dst": ["*:*"],
        })
        for rule in policy["acls"][1:]:
            self.assertNotIn(PREFIX + ":*", rule["dst"])
            self.assertNotIn("*:*", rule["dst"])

    def terraform_resource(self, kind, name):
        # Match top-level blocks only; tofu validate checks real HCL syntax and
        # reference types, while this contract checks the intended bindings.
        match = re.search(
            rf'^resource "{re.escape(kind)}" "{re.escape(name)}" \{{\n(.*?)^\}}',
            (HEADSCALE / "main.tf").read_text(), re.MULTILINE | re.DOTALL,
        )
        if match is None:
            self.fail(f"missing {kind}.{name}")
        return match.group(1)

    def test_enrollment_is_durable_tagged_and_wired_to_the_router(self):
        key = self.terraform_resource("headscale_pre_auth_key", "services_subnet_router")
        for field, value in {
            "user": r"headscale_user\.hub\.id", "reusable": "true",
            "ephemeral": "false", "acl_tags": r'\["' + TAG + r'"\]',
        }.items():
            self.assertRegex(key, rf"(?m)^\s*{field}\s*=\s*{value}\s*$")
        secret = self.terraform_resource("kubernetes_secret", "services_subnet_router_preauth")
        self.assertRegex(secret, r'name\s*=\s*"services-subnet-router-ts-auth"')
        self.assertRegex(secret, r'namespace\s*=\s*"headscale"')
        self.assertRegex(secret, r"authkey\s*=\s*headscale_pre_auth_key\.services_subnet_router\.key")
        rules = self.resource("ClusterRole", "headscale-tofu")["rules"]
        self.assertTrue(any(
            "services-subnet-router-ts-auth" in rule.get("resourceNames", [])
            and set(rule["verbs"]) >= {"get", "patch", "update"}
            for rule in rules
        ))
        pod = self.resource("StatefulSet", ROUTER)["spec"]["template"]["spec"]
        env = {entry["name"]: entry for entry in pod["containers"][0]["env"]}
        self.assertEqual(env["TS_AUTHKEY"]["valueFrom"]["secretKeyRef"], {
            "name": "services-subnet-router-ts-auth", "key": "authkey",
        })
        self.assertEqual(env["TS_AUTH_ONCE"]["value"], "true")
        for name in ("TS_HOSTNAME", "TS_KUBE_SECRET"):
            self.assertEqual(env[name]["valueFrom"]["fieldRef"]["fieldPath"], "metadata.name")
        for ordinal in range(2):
            state = self.resource("Secret", f"{ROUTER}-{ordinal}")
            self.assertFalse(state.get("data"))
            self.assertFalse(state.get("stringData"))
        role = self.resource("Role", ROUTER)
        self.assertEqual(role["rules"], [{
            "apiGroups": [""], "resources": ["secrets"],
            "resourceNames": [f"{ROUTER}-0", f"{ROUTER}-1"],
            "verbs": ["get", "update", "patch"],
        }])
        binding = self.resource("RoleBinding", ROUTER)
        self.assertEqual(binding["subjects"], [{
            "kind": "ServiceAccount", "name": ROUTER, "namespace": "headscale",
        }])
        self.assertEqual(pod["serviceAccountName"], ROUTER)

    def test_routers_are_isolated_and_ready_before_serving(self):
        workload = self.resource("StatefulSet", ROUTER)["spec"]
        self.assertEqual(workload["replicas"], 2)
        pod = workload["template"]["spec"]
        self.assertEqual(pod.get("nodeSelector"), {"node-role.kubernetes.io/worker": "true"})
        separation = pod["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
        self.assertEqual(separation, [{
            "labelSelector": {"matchLabels": {"app.kubernetes.io/name": ROUTER}},
            "topologyKey": "kubernetes.io/hostname",
        }])
        self.assertFalse(pod.get("hostPID", False))
        self.assertFalse(any("hostPath" in volume for volume in pod.get("volumes", [])))
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        container = pod["containers"][0]
        security = container["securityContext"]
        self.assertFalse(security["allowPrivilegeEscalation"])
        self.assertFalse(security.get("privileged", False))
        self.assertEqual(security["capabilities"], {"drop": ["ALL"]})
        self.assertTrue(security["readOnlyRootFilesystem"])
        self.assertRegex(container["image"], r"^ghcr.io/tailscale/tailscale:v[0-9.]+@sha256:[a-f0-9]{64}$")
        env = {entry["name"]: entry.get("value") for entry in container["env"]}
        self.assertEqual(env["TS_ENABLE_HEALTH_CHECK"], "true")
        for probe in ("startupProbe", "readinessProbe"):
            self.assertEqual(container[probe]["httpGet"], {"path": "/healthz", "port": "health"})
        self.assertNotIn("livenessProbe", container)  # Don't restart on coordinator outages.
        budget = self.resource("PodDisruptionBudget", ROUTER)["spec"]
        self.assertEqual(budget["maxUnavailable"], 1)
        self.assertEqual(budget["selector"], workload["selector"])
        service = self.resource("Service", workload["serviceName"])["spec"]
        self.assertEqual(service["clusterIP"], "None")
        self.assertEqual(service["selector"], workload["selector"]["matchLabels"])

    def test_router_advertises_only_services_and_never_imports_routes(self):
        pod = self.resource("StatefulSet", ROUTER)["spec"]["template"]["spec"]
        tailscale = next(c for c in pod["containers"] if c["name"] == "tailscale")
        env = {entry["name"]: entry.get("value") for entry in tailscale["env"]}
        self.assertEqual(env["TS_ROUTES"], PREFIX)
        args = shlex.split(env["TS_EXTRA_ARGS"])
        self.assertIn("--accept-routes=false", args)
        self.assertFalse(any(arg.startswith("--advertise-routes") for arg in args))
        self.assertFalse(any(arg.startswith("--advertise-exit-node") for arg in args))
        self.assertEqual(env["TS_ACCEPT_DNS"], "false")
        self.assertEqual(env["TS_USERSPACE"], "true")
        self.assertFalse(pod.get("hostNetwork", False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
