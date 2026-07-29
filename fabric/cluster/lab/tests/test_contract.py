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

    def test_child_api_policy_admits_bootstrap_jobs(self):
        policy = object_named(
            self.foundation,
            "NetworkPolicy",
            "allow-control-plane-lab-api-egress",
            "lab",
        )
        values = policy["spec"]["podSelector"]["matchExpressions"][0]["values"]
        self.assertIn("bootstrap", values)
        self.assertIn("admin-kubeconfig-generator", values)

        ingress = object_named(
            self.foundation,
            "NetworkPolicy",
            "allow-apiserver-proxy-ingress",
            "lab",
        )
        values = ingress["spec"]["ingress"][0]["from"][0]["podSelector"][
            "matchExpressions"
        ][0]["values"]
        self.assertIn("bootstrap", values)
        self.assertIn("admin-kubeconfig-generator", values)


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
        term = values["parentWorkloads"]["placement"]["affinity"][
            "nodeAffinity"
        ]["requiredDuringSchedulingIgnoredDuringExecution"][
            "nodeSelectorTerms"
        ][0]
        self.assertEqual(
            term["matchExpressions"],
            [
                {
                    "key": "kubernetes.io/hostname",
                    "operator": "In",
                    "values": ["fabric-az1-svc1", "fabric-az1-svc2"],
                }
            ],
        )
        self.assertNotIn("matchFields", term)
        self.assertEqual(
            values["parentWorkloads"]["deployment"]["pdb"]["spec"],
            {"minAvailable": 1},
        )
        self.assertFalse(values["monitoring"]["podMonitors"]["enabled"])
        self.assertEqual(values["konnectivity"]["server"]["clientIdentity"], "dedicated")
        self.assertEqual(values["konnectivity"]["agent"]["replicas"], 1)
        users = {user["name"]: user for user in values["users"]}
        self.assertEqual(set(users), {"lab-flux", "lab-bootie"})
        self.assertEqual(
            users["lab-flux"],
            {
                "name": "lab-flux",
                "clusterRbac": [
                    {
                        "rules": [
                            {
                                "apiGroups": ["*"],
                                "resources": ["*"],
                                "verbs": ["*"],
                            },
                            {"nonResourceURLs": ["*"], "verbs": ["*"]},
                        ]
                    }
                ],
                "exportKubeconfigs": [
                    {"name": "lab-flux-kubeconfig", "namespace": "flux-system"}
                ],
            },
        )
        self.assertEqual(
            users["lab-bootie"],
            {
                "name": "lab-bootie",
                "clusterRbac": [
                    {
                        "rules": [
                            {
                                "apiGroups": [""],
                                "resourceNames": ["lab-worker-1"],
                                "resources": ["nodes"],
                                "verbs": ["get", "patch"],
                            }
                        ]
                    }
                ],
                "exportKubeconfigs": [
                    {"name": "lab-bootie-kubeconfig", "namespace": "lab"}
                ],
            },
        )
        self.assertEqual(
            values["utilities"]["resources"]["limits"]["memory"], "256Mi"
        )
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


class LabComputeContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bootie = render("fabric/cluster/lab/bootie")
        cls.child = render("fabric/cluster/lab/child")
        cls.inventory = render("lab/cluster/inventory")
        cls.labgrid = render("lab/cluster/labgrid")

    def test_placeholder_and_bootie_fail_closed(self):
        node = object_named(self.inventory, "Node", "lab-worker-1")
        self.assertTrue(node["spec"]["unschedulable"])
        self.assertEqual(node["metadata"]["labels"]["samcday.com/discovery"], "true")
        self.assertNotIn("samcday.com/mac", node["metadata"]["labels"])
        self.assertNotIn("samcday.com/boot-device", node["metadata"]["annotations"])

        policy = object_named(
            self.bootie, "ConfigMap", "lab-bootie-install-policy", "lab"
        )
        self.assertIn("/dev/disk/by-id/NOT-DECLARED", policy["data"]["install-policy"])
        deployment = object_named(self.bootie, "Deployment", "lab-bootie", "lab")
        pod = deployment["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        container = next(item for item in pod["containers"] if item["name"] == "bootie")
        env = {item["name"]: item.get("value") for item in container["env"]}
        self.assertEqual(env["BOOTIE_ALLOW_NODE_CREATE"], "false")
        self.assertEqual(env["BOOTIE_REQUIRE_BOOTSTRAP_STATE"], "true")
        self.assertEqual(env["BOOTIE_REQUIRE_INSTALL_POLICY"], "true")
        self.assertEqual(env["KUBECONFIG"], "/kubeconfig/value")
        self.assertEqual(env["BOOTIE_PUBLIC_ORIGIN"], "http://lab-bootie.tailnet.hub.samcday.com")

    def test_compute_secrets_are_runtime_compiled(self):
        deployment = object_named(self.bootie, "Deployment", "lab-bootie", "lab")
        init = deployment["spec"]["template"]["spec"]["initContainers"][0]
        script = init["args"][0]
        self.assertIn("/secrets/bootstrap/token", script)
        self.assertIn("/secrets/node/authkey", script)
        self.assertIn("lab-apiserver.tailnet.hub.samcday.com:6443", script)
        self.assertIn("sha256:$ca_hash", script)
        for path in (REPO / "lab/butane").glob("*.yaml"):
            self.assertNotIn("hskey-auth-", path.read_text())

    def test_child_platform_is_minimal_and_pinned(self):
        cilium = object_named(self.child, "HelmRelease", "lab-cilium", "lab")
        coredns = object_named(self.child, "HelmRelease", "lab-coredns", "lab")
        openebs = object_named(self.child, "HelmRelease", "lab-openebs", "lab")
        self.assertEqual(cilium["spec"]["chart"]["spec"]["version"], "1.19.4")
        self.assertEqual(
            cilium["spec"]["values"]["k8sServiceHost"],
            "lab-apiserver.tailnet.hub.samcday.com",
        )
        self.assertEqual(coredns["spec"]["chart"]["spec"]["version"], "1.39.2")
        self.assertEqual(coredns["spec"]["values"]["replicaCount"], 1)
        self.assertEqual(openebs["spec"]["chart"]["spec"]["version"], "4.5.1")
        values = openebs["spec"]["values"]
        self.assertFalse(values["engines"]["replicated"]["mayastor"]["enabled"])
        self.assertEqual(values["localpv-provisioner"]["hostpathClass"]["name"], "lab-local")
        self.assertTrue(
            values["localpv-provisioner"]["hostpathClass"]["isDefaultClass"]
        )

    def test_coordinator_starts_fresh_and_is_tailnet_published(self):
        pvc = object_named(self.labgrid, "PersistentVolumeClaim", "labgrid-coordinator")
        self.assertEqual(pvc["spec"]["storageClassName"], "lab-local")
        deployment = object_named(self.labgrid, "Deployment", "labgrid-coordinator")
        self.assertEqual(deployment["spec"]["strategy"]["type"], "Recreate")
        self.assertRegex(
            deployment["spec"]["template"]["spec"]["containers"][0]["image"],
            r"@sha256:[0-9a-f]{64}$",
        )
        proxy = object_named(
            self.labgrid, "Deployment", "labgrid-coordinator-tailnet"
        )
        tailscale = proxy["spec"]["template"]["spec"]["containers"][0]
        env = {item["name"]: item.get("value") for item in tailscale["env"]}
        self.assertEqual(env["TS_HOSTNAME"], "labgrid-coordinator")
        self.assertIn("https://headscale.tail22d0a0.ts.net", env["TS_EXTRA_ARGS"])
        self.assertNotIn("--advertise-tags", env["TS_EXTRA_ARGS"])


class LabHeadscaleContract(unittest.TestCase):
    def test_old_coordinator_identity_is_not_rotated_in_place(self):
        terraform = (REPO / "hub/cluster/headscale/main.tf").read_text()
        old = terraform.split(
            'resource "headscale_pre_auth_key" "labgrid_coordinator" {'
        )[1].split("\n}", 1)[0]
        self.assertIn("user           = headscale_user.hub.id", old)
        self.assertIn('time_to_expire = "520w"', old)
        self.assertIn("reusable       = true", old)
        new = terraform.split(
            'resource "headscale_pre_auth_key" "labgrid_coordinator_lab" {'
        )[1].split("\n}", 1)[0]
        self.assertIn("user           = headscale_user.lab.id", new)
        self.assertIn("reusable       = false", new)

    def test_lab_router_and_node_acls_are_narrow(self):
        text = (REPO / "hub/cluster/headscale/acls.json").read_text()
        self.assertIn('"src": ["tag:lab-router"]', text)
        self.assertIn('"dst": ["tag:lab-bootie:80"]', text)
        self.assertIn('"src": ["tag:lab-node"]', text)
        self.assertIn('"tag:lab-apiserver:6443"', text)
        self.assertIn('"tag:lab-apiserver:8091"', text)

    def test_tofu_secret_access_is_exactly_extended_for_lab_handoffs(self):
        manifests = documents(REPO / "hub/cluster/headscale/tofu.yaml")
        role = object_named(manifests, "ClusterRole", "headscale-tofu")
        names = set(role["rules"][0]["resourceNames"])
        self.assertTrue(
            {
                "lab-bootie-ts-auth",
                "lab-node-ts-auth",
                "labgrid-lab-ts-auth",
            }
            <= names
        )


if __name__ == "__main__":
    unittest.main()
