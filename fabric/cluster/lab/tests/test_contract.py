import pathlib
import subprocess
import unittest

import yaml


REPO = pathlib.Path(__file__).resolve().parents[4]


def documents(path):
    return [item for item in yaml.safe_load_all(path.read_text()) if item]


def render(relative):
    result = subprocess.run(
        ["kubectl", "kustomize", str(REPO / relative)],
        check=True,
        capture_output=True,
        text=True,
    )
    return [item for item in yaml.safe_load_all(result.stdout) if item]


def object_named(items, kind, name, namespace=None):
    for item in items:
        metadata = item.get("metadata", {})
        if (
            item.get("kind") == kind
            and metadata.get("name") == name
            and (namespace is None or metadata.get("namespace") == namespace)
        ):
            return item
    raise AssertionError(f"missing {kind}/{name}")


class LabFoundationContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.foundation = render("fabric/cluster/lab/foundation")

    def test_runtime_owned_handoffs_use_merge_ownership(self):
        state = object_named(
            self.foundation, "Secret", "lab-apiserver-ts-state", "lab"
        )
        endpoint = object_named(
            self.foundation, "ConfigMap", "lab-control-plane-endpoint", "lab"
        )
        annotation = "kustomize.toolkit.fluxcd.io/ssa"
        self.assertEqual(state["metadata"]["annotations"][annotation], "Merge")
        self.assertEqual(endpoint["metadata"]["annotations"][annotation], "Merge")
        self.assertNotIn("data", state)
        self.assertNotIn("data", endpoint)
        self.assertEqual(
            endpoint["metadata"]["labels"]["reconcile.fluxcd.io/watch"],
            "Enabled",
        )

    def test_tenant_and_tailnet_identity_are_exact(self):
        tenant = object_named(self.foundation, "EtcdTenant", "apiserver", "lab")
        self.assertEqual(tenant["spec"]["secretName"], "apiserver-etcd-client")
        self.assertEqual(
            tenant["spec"]["clusterRef"],
            {"name": "fabric-etcd", "namespace": "etcdetcetc"},
        )
        proxy = object_named(
            self.foundation, "Deployment", "lab-apiserver-tailnet", "lab"
        )
        pod = proxy["spec"]["template"]["spec"]
        container = pod["containers"][0]
        env = {entry["name"]: entry.get("value") for entry in container["env"]}
        self.assertEqual(proxy["spec"]["strategy"]["type"], "Recreate")
        self.assertTrue(container["securityContext"]["runAsNonRoot"])
        self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])
        self.assertEqual(env["TS_HOSTNAME"], "lab-apiserver")
        self.assertEqual(env["TS_KUBE_SECRET"], "lab-apiserver-ts-state")
        self.assertEqual(env["TS_SOCKET"], "/var/run/tailscale/tailscaled.sock")
        self.assertNotIn("--advertise-tags", env["TS_EXTRA_ARGS"])
        headscale = (REPO / "hub/cluster/headscale/main.tf").read_text()
        key = headscale.split(
            'resource "headscale_pre_auth_key" "lab_apiserver_stable" {'
        )[1].split("\n}", 1)[0]
        self.assertIn('acl_tags       = ["tag:lab-apiserver"]', key)

    def test_publisher_is_atomic_and_change_driven(self):
        publisher = object_named(
            self.foundation,
            "Deployment",
            "lab-apiserver-endpoint-publisher",
            "lab",
        )
        script = publisher["spec"]["template"]["spec"]["containers"][0]["args"][0]
        self.assertIn("lab-apiserver.tailnet.hub.samcday.com", script)
        self.assertIn("externalHostname", script)
        self.assertIn("externalIP", script)
        self.assertIn("$2 >= 64 && $2 <= 127", script)
        compare = script.index('if [ "$current" != "$expected" ]; then')
        apply = script.index("kubectl apply --server-side")
        self.assertLess(compare, apply)

    def test_foundation_workloads_target_service_nodes_by_hostname(self):
        expected = [
            {
                "key": "kubernetes.io/hostname",
                "operator": "In",
                "values": ["fabric-az1-svc1", "fabric-az1-svc2"],
            }
        ]
        for name in (
            "lab-apiserver-tailnet",
            "lab-apiserver-endpoint-publisher",
        ):
            with self.subTest(deployment=name):
                deployment = object_named(
                    self.foundation, "Deployment", name, "lab"
                )
                term = deployment["spec"]["template"]["spec"]["affinity"][
                    "nodeAffinity"
                ]["requiredDuringSchedulingIgnoredDuringExecution"][
                    "nodeSelectorTerms"
                ][0]
                self.assertEqual(term["matchExpressions"], expected)
                self.assertNotIn("matchFields", term)

    def test_public_proxy_egress_is_tcp_443_only(self):
        policy = object_named(
            self.foundation,
            "NetworkPolicy",
            "allow-tailnet-proxy-public-egress",
            "lab",
        )
        self.assertEqual(
            policy["spec"]["egress"][0]["ports"],
            [{"port": 443, "protocol": "TCP"}],
        )


class LabControlPlaneContract(unittest.TestCase):
    def test_values_are_pinned_and_hardened(self):
        manifest = documents(
            REPO / "fabric/cluster/lab/control-plane/control-plane-values.yaml"
        )[0]
        values = yaml.safe_load(manifest["data"]["values.yaml"])
        self.assertEqual(values["version"], "1.36.3")
        self.assertEqual(values["clusterCIDRs"], ["172.26.0.0/16"])
        self.assertEqual(values["serviceCIDRs"], ["172.25.0.0/16"])
        self.assertEqual(values["serviceIP"], "172.25.0.1")
        self.assertEqual(values["adminKubeconfig"]["schedule"], "@daily")
        self.assertEqual(
            values["adminKubeconfig"]["concurrencyPolicy"], "Forbid"
        )
        self.assertEqual(values["apiServer"]["service"]["spec"]["clusterIP"], "172.21.0.25")
        self.assertTrue(values["parentWorkloads"]["enabled"])
        self.assertEqual(
            values["parentWorkloads"]["deployment"]["pdb"]["spec"],
            {"minAvailable": 1},
        )
        self.assertFalse(values["monitoring"]["podMonitors"]["enabled"])
        self.assertEqual(values["konnectivity"]["server"]["clientIdentity"], "dedicated")
        for component in ("apiServer", "controllerManager", "scheduler"):
            self.assertRegex(
                values[component]["image"]["digest"], r"^sha256:[0-9a-f]{64}$"
            )

    def test_helm_values_handoffs_are_ordered(self):
        release = documents(
            REPO / "fabric/cluster/lab/control-plane/release.yaml"
        )[0]
        refs = [entry["name"] for entry in release["spec"]["valuesFrom"]]
        self.assertEqual(
            refs,
            ["control-plane-values", "apiserver-etcd", "lab-control-plane-endpoint"],
        )
        self.assertEqual(release["spec"]["driftDetection"]["mode"], "enabled")
        self.assertNotIn("force", release["spec"]["upgrade"])

    def test_flux_activation_order_is_safe(self):
        children = documents(REPO / "fabric/cluster/flux-system/children.yaml")
        foundation = object_named(
            children, "Kustomization", "fabric-lab-foundation", "flux-system"
        )
        control_plane = object_named(
            children, "Kustomization", "fabric-lab-control-plane", "flux-system"
        )
        self.assertIn(
            (
                foundation["spec"]["suspend"],
                control_plane["spec"]["suspend"],
            ),
            ((True, True), (False, True), (False, False)),
        )
        self.assertNotIn("wait", foundation["spec"])
        self.assertEqual(
            [item["name"] for item in control_plane["spec"]["dependsOn"]],
            ["fabric-lab-foundation"],
        )


if __name__ == "__main__":
    unittest.main()
