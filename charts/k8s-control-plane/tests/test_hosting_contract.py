#!/usr/bin/env python3
from __future__ import annotations

import copy
import unittest

import yaml

from test_external_etcd_handoff import (
    BASE_VALUES,
    CLIENT_CERTIFICATE_REVISION,
    SERVER_CA_REVISION,
    helm_template,
    object_named,
    objects,
)


DIGESTS = {
    "apiserver": "sha256:" + ("1a" * 32),
    "controller-manager": "sha256:" + ("2b" * 32),
    "kube-scheduler": "sha256:" + ("3c" * 32),
}
STABLE_LABELS = {
    "app.kubernetes.io/name": "k8s-control-plane",
    "app.kubernetes.io/instance": "child",
}


def legacy_values() -> dict:
    values = copy.deepcopy(BASE_VALUES)
    values["etcd"]["externalSecretRevisionsRequired"] = False
    return values


def hardened_values() -> dict:
    values = copy.deepcopy(BASE_VALUES)
    values["externalIP"] = None
    values["externalHostname"] = "lab.example.test"
    values["adminKubeconfig"] = {
        "schedule": "@daily",
        "concurrencyPolicy": "Forbid",
    }
    values["etcd"]["serverCATrustRevision"] = SERVER_CA_REVISION
    values["etcd"]["clientCertificateRevision"] = CLIENT_CERTIFICATE_REVISION
    values["parentWorkloads"] = {
        "enabled": True,
        "placement": {
            "nodeSelector": {"node-role.samcday.com/fabric": ""},
            "tolerations": [
                {
                    "key": "node-role.samcday.com/fabric",
                    "operator": "Exists",
                    "effect": "NoSchedule",
                }
            ],
            "affinity": {
                "nodeAffinity": {
                    "preferredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "weight": 1,
                            "preference": {
                                "matchExpressions": [
                                    {
                                        "key": "topology.kubernetes.io/zone",
                                        "operator": "Exists",
                                    }
                                ]
                            },
                        }
                    ]
                }
            },
        },
        "podSecurityContext": {
            "runAsNonRoot": True,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containerSecurityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "deployment": {
            "priorityClassName": "system-cluster-critical",
            "requiredPodAntiAffinityTopologyKey": "kubernetes.io/hostname",
            "minReadySeconds": 10,
            "terminationGracePeriodSeconds": 30,
            "strategy": {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1},
            },
            "pdb": {"enabled": True, "spec": {"minAvailable": 1}},
        },
    }
    values["utilities"] = {
        "image": "registry.k8s.io/kubectl:v1.35.3",
        "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}},
        "writableTmp": True,
    }
    values["bootstrap"] = {"kubeadmAPIVersion": "kubeadm.k8s.io/v1beta4"}
    values["konnectivity"] = {
        "server": {
            "clientIdentity": "dedicated",
            "extraArgs": ["--v=2"],
            "resources": {"requests": {"cpu": "10m"}},
        }
    }
    values["monitoring"] = {"podMonitors": {"enabled": False}}
    values["apiServer"] = {
        "image": {"digest": DIGESTS["apiserver"]},
        "resources": {"requests": {"cpu": "100m"}},
        "securityContext": {"runAsUser": 65534},
    }
    values["controllerManager"] = {
        "image": {"digest": DIGESTS["controller-manager"]},
        "resources": {"requests": {"cpu": "50m"}},
        "securityContext": {"runAsUser": 65534},
    }
    values["scheduler"] = {
        "image": {"digest": DIGESTS["kube-scheduler"]},
        "resources": {"requests": {"cpu": "50m"}},
        "securityContext": {"runAsUser": 65534},
        "vpa": {"enabled": False},
    }
    return values


def workload_pods(rendered: list[dict]) -> dict[tuple[str, str], tuple[dict, dict]]:
    result = {}
    for document in rendered:
        kind = document.get("kind")
        name = document.get("metadata", {}).get("name")
        if kind == "Deployment":
            template = document["spec"]["template"]
        elif kind == "Job":
            template = document["spec"]["template"]
        elif kind == "CronJob":
            template = document["spec"]["jobTemplate"]["spec"]["template"]
        else:
            continue
        result[(kind, name)] = (document, template)
    return result


def embedded_bootstrap(rendered: list[dict]) -> list[dict]:
    job = object_named(rendered, "Job", "bootstrap-1")
    command = job["spec"]["template"]["spec"]["containers"][0]["command"][-1]
    start = command.index("apiVersion: v1")
    end = command.index("\nHERE\n", start)
    return [doc for doc in yaml.safe_load_all(command[start:end]) if isinstance(doc, dict)]


class HostingContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        legacy = helm_template(legacy_values())
        if legacy.returncode:
            raise AssertionError(legacy.stderr)
        cls.legacy = objects(legacy.stdout)

        hardened = helm_template(hardened_values())
        if hardened.returncode:
            raise AssertionError(hardened.stderr)
        cls.hardened = objects(hardened.stdout)

    def test_legacy_defaults_remain_opt_in(self) -> None:
        self.assertEqual(
            {doc["metadata"]["name"] for doc in self.legacy if doc.get("kind") == "PodMonitor"},
            {"apiserver", "controller-manager", "scheduler"},
        )
        self.assertFalse(any(doc.get("kind") == "PodDisruptionBudget" for doc in self.legacy))
        self.assertFalse(
            any(
                doc.get("kind") == "Certificate"
                and doc.get("metadata", {}).get("name") == "konnectivity-server"
                for doc in self.legacy
            )
        )
        binding = object_named(self.legacy, "ClusterRoleBinding", "system:konnectivity-server")
        self.assertEqual(binding["roleRef"]["name"], "system:auth-delegator")
        bootstrap = embedded_bootstrap(self.legacy)
        config = yaml.safe_load(object_named(bootstrap, "ConfigMap", "kubeadm-config")["data"]["ClusterConfiguration"])
        self.assertEqual(config["apiVersion"], "kubeadm.k8s.io/v1beta3")
        apiserver = object_named(self.legacy, "Deployment", "apiserver")
        self.assertNotIn("app.kubernetes.io/name", apiserver.get("metadata", {}).get("labels", {}))
        self.assertNotIn("automountServiceAccountToken", apiserver["spec"]["template"]["spec"])

    def test_hardened_contract_covers_every_parent_pod(self) -> None:
        workloads = workload_pods(self.hardened)
        expected_components = {
            ("Deployment", "apiserver"): "apiserver",
            ("Deployment", "controller-manager"): "controller-manager",
            ("Deployment", "kube-scheduler"): "scheduler",
            ("Job", "bootstrap-1"): "bootstrap",
            ("Job", "ca-hash-1"): "ca-hash",
            ("Job", "advertise-address-1"): "advertise-address",
            ("Job", "admin-kubeconfig-generator-1"): "admin-kubeconfig-generator",
            ("CronJob", "admin-kubeconfig-generator"): "admin-kubeconfig-generator",
        }
        self.assertEqual(set(workloads), set(expected_components))

        tokenless = {
            ("Deployment", "apiserver"),
            ("Deployment", "controller-manager"),
            ("Deployment", "kube-scheduler"),
            ("Job", "bootstrap-1"),
        }
        for key, component in expected_components.items():
            with self.subTest(workload=key):
                document, template = workloads[key]
                labels = {**STABLE_LABELS, "app.kubernetes.io/component": component}
                self.assertEqual(labels.items() <= document["metadata"]["labels"].items(), True)
                self.assertEqual(labels.items() <= template["metadata"]["labels"].items(), True)
                if key[0] == "Job":
                    self.assertEqual(document["spec"]["ttlSecondsAfterFinished"], 60)
                    self.assertEqual(
                        document["metadata"]["annotations"][
                            "helm.toolkit.fluxcd.io/driftDetection"
                        ],
                        "disabled",
                    )
                pod = template["spec"]
                self.assertEqual(pod["nodeSelector"], {"node-role.samcday.com/fabric": ""})
                self.assertEqual(pod["securityContext"]["runAsNonRoot"], True)
                self.assertEqual(pod["securityContext"]["seccompProfile"], {"type": "RuntimeDefault"})
                self.assertEqual(pod.get("automountServiceAccountToken", True), key not in tokenless)
                volume_names = {volume["name"] for volume in pod.get("volumes", [])}
                for container in pod["containers"]:
                    security = container["securityContext"]
                    self.assertFalse(security["allowPrivilegeEscalation"])
                    self.assertTrue(security["readOnlyRootFilesystem"])
                    self.assertEqual(security["capabilities"], {"drop": ["ALL"]})
                    for mount in container.get("volumeMounts", []):
                        self.assertIn(mount["name"], volume_names)

                if key[0] == "Deployment":
                    self.assertEqual(document["spec"]["minReadySeconds"], 10)
                    self.assertEqual(
                        document["spec"]["strategy"],
                        {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1}},
                    )
                    self.assertEqual(pod["priorityClassName"], "system-cluster-critical")
                    self.assertEqual(pod["terminationGracePeriodSeconds"], 30)
                    anti = pod["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
                    self.assertEqual(anti[-1]["topologyKey"], "kubernetes.io/hostname")
                    self.assertEqual(anti[-1]["labelSelector"]["matchLabels"], labels)

    def test_hardened_images_resources_tmp_and_pdbs(self) -> None:
        deployment_containers = {
            name: object_named(self.hardened, "Deployment", name)["spec"]["template"]["spec"]["containers"]
            for name in ("apiserver", "controller-manager", "kube-scheduler")
        }
        for deployment_name, container_name in (
            ("apiserver", "apiserver"),
            ("controller-manager", "controller-manager"),
            ("kube-scheduler", "kube-scheduler"),
        ):
            container = next(c for c in deployment_containers[deployment_name] if c["name"] == container_name)
            self.assertTrue(container["image"].endswith("@" + DIGESTS[deployment_name]))
            self.assertEqual(container["securityContext"]["runAsUser"], 65534)
            self.assertIn("cpu", container["resources"]["requests"])

        proxy = next(c for c in deployment_containers["apiserver"] if c["name"] == "konnectivity-server")
        self.assertIn("--v=2", proxy["args"])
        self.assertEqual(proxy["resources"], {"requests": {"cpu": "10m"}})

        for key, (_, template) in workload_pods(self.hardened).items():
            if key[0] not in {"Job", "CronJob"}:
                continue
            pod = template["spec"]
            container = pod["containers"][0]
            self.assertEqual(container["image"], "registry.k8s.io/kubectl:v1.35.3")
            self.assertEqual(container["resources"]["requests"]["cpu"], "10m")
            self.assertIn({"name": "HOME", "value": "/tmp"}, container.get("env", []))
            self.assertIn({"name": "tmp", "mountPath": "/tmp"}, container["volumeMounts"])
            self.assertIn({"name": "tmp", "emptyDir": {}}, pod["volumes"])

        pdbs = [doc for doc in self.hardened if doc.get("kind") == "PodDisruptionBudget"]
        self.assertEqual({doc["metadata"]["name"] for doc in pdbs}, {"apiserver", "controller-manager", "kube-scheduler"})
        for pdb in pdbs:
            self.assertEqual(pdb["spec"]["minAvailable"], 1)
            self.assertEqual(pdb["spec"]["selector"]["matchLabels"], pdb["metadata"]["labels"])
        self.assertFalse(any(doc.get("kind") == "PodMonitor" for doc in self.hardened))

        exporter = object_named(self.hardened, "CronJob", "admin-kubeconfig-generator")
        self.assertEqual(exporter["spec"]["schedule"], "@daily")
        self.assertEqual(exporter["spec"]["concurrencyPolicy"], "Forbid")

    def test_dedicated_konnectivity_identity_is_child_scoped(self) -> None:
        certificate = object_named(self.hardened, "Certificate", "konnectivity-server")
        self.assertEqual(certificate["spec"]["commonName"], "system:konnectivity-server")
        self.assertEqual(certificate["spec"]["issuerRef"], {"name": "ca", "kind": "Issuer"})
        self.assertFalse(
            any(
                doc.get("kind") == "ClusterRoleBinding"
                and doc.get("metadata", {}).get("name") == "system:konnectivity-server"
                for doc in self.hardened
            )
        )
        kubeconfig = yaml.safe_load(object_named(self.hardened, "ConfigMap", "konnectivity-server-kubeconfig")["data"]["kubeconfig"])
        user = kubeconfig["users"][0]["user"]
        self.assertEqual(user["client-certificate"], "/konnectivity-client/tls.crt")
        self.assertEqual(user["client-key"], "/konnectivity-client/tls.key")

        bootstrap = embedded_bootstrap(self.hardened)
        binding = object_named(bootstrap, "ClusterRoleBinding", "system:konnectivity-server")
        self.assertEqual(binding["roleRef"]["name"], "system:auth-delegator")
        config = yaml.safe_load(object_named(bootstrap, "ConfigMap", "kubeadm-config")["data"]["ClusterConfiguration"])
        self.assertEqual(config["apiVersion"], "kubeadm.k8s.io/v1beta4")

    def test_scheduler_specific_overrides_remain_authoritative(self) -> None:
        values = hardened_values()
        values["scheduler"]["deployment"] = {
            "labels": {"example.test/custom": "true"},
            "spec": {
                "minReadySeconds": 25,
                "strategy": {"type": "Recreate"},
                "template": {
                    "spec": {
                        "priorityClassName": "custom-priority",
                        "nodeSelector": {"node-role.samcday.com/fabric": "custom"},
                        "containers": [
                            {
                                "name": "kube-scheduler",
                                "command": ["--v=4"],
                                "securityContext": {"runAsUser": 1234},
                            }
                        ],
                    }
                },
            },
        }
        result = helm_template(values)
        self.assertEqual(result.returncode, 0, result.stderr)
        deployment = object_named(objects(result.stdout), "Deployment", "kube-scheduler")
        self.assertEqual(deployment["spec"]["strategy"], {"type": "Recreate"})
        self.assertEqual(deployment["spec"]["minReadySeconds"], 25)
        pod = deployment["spec"]["template"]["spec"]
        self.assertEqual(pod["priorityClassName"], "custom-priority")
        self.assertEqual(pod["nodeSelector"], {"node-role.samcday.com/fabric": "custom"})
        scheduler = pod["containers"][0]
        self.assertIn("--v=4", scheduler["command"])
        self.assertEqual(scheduler["securityContext"]["runAsUser"], 1234)

    def test_disabling_scheduler_omits_all_scheduler_workloads(self) -> None:
        values = legacy_values()
        values["scheduler"] = {"enabled": False}
        result = helm_template(values)
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = objects(result.stdout)
        self.assertFalse(
            any(
                doc.get("metadata", {}).get("name") in {
                    "kube-scheduler",
                    "scheduler",
                }
                and doc.get("kind") in {
                    "Deployment",
                    "PodMonitor",
                    "VerticalPodAutoscaler",
                }
                for doc in rendered
            )
        )

    def test_invalid_contract_values_are_rejected(self) -> None:
        cases = []

        invalid_digest = legacy_values()
        invalid_digest["apiServer"] = {"image": {"digest": "sha256:NOT-A-DIGEST"}}
        cases.append((invalid_digest, "apiServer.image.digest must be sha256:"))

        invalid_kubeadm = legacy_values()
        invalid_kubeadm["bootstrap"] = {"kubeadmAPIVersion": "kubeadm.k8s.io/v1beta2"}
        cases.append((invalid_kubeadm, "bootstrap.kubeadmAPIVersion must be"))

        invalid_identity = legacy_values()
        invalid_identity["konnectivity"] = {"server": {"clientIdentity": "admin"}}
        cases.append((invalid_identity, "konnectivity.server.clientIdentity must be"))

        invalid_pdb = legacy_values()
        invalid_pdb["parentWorkloads"] = {
            "enabled": True,
            "deployment": {"pdb": {"enabled": True, "spec": {}}},
        }
        cases.append((invalid_pdb, "pdb.spec must set exactly one"))

        read_only = legacy_values()
        read_only["parentWorkloads"] = {
            "enabled": True,
            "containerSecurityContext": {"readOnlyRootFilesystem": True},
        }
        cases.append((read_only, "utilities.writableTmp=true is required"))

        too_many_cidrs = legacy_values()
        too_many_cidrs["serviceCIDRs"] = ["10.0.0.0/16", "fd00::/108", "10.1.0.0/16"]
        cases.append((too_many_cidrs, "too many serviceCIDRs"))

        for values, message in cases:
            with self.subTest(message=message):
                result = helm_template(values)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(message, result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
