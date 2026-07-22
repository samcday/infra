#!/usr/bin/env python3
from __future__ import annotations

import copy
import pathlib
import subprocess
import unittest

import yaml


CHART = pathlib.Path(__file__).resolve().parents[1]

ENDPOINTS = [
    "https://10.66.0.10:2379",
    "https://10.66.0.11:2379",
    "https://10.66.0.12:2379",
]
PREFIX = "/kubernetes/child/"
SECRET_NAME = "child-apiserver-etcd-client"
SERVER_CA_REVISION = "sha256:" + ("a5" * 32)
CLIENT_CERTIFICATE_REVISION = "sha256:" + ("3c" * 32)

BASE_VALUES = {
    "clusterName": "child",
    "externalIP": "192.0.2.10",
    "serviceCIDRs": ["10.96.0.0/16"],
    "clusterCIDRs": ["10.244.0.0/16"],
    "clusterDNS": ["10.96.0.10"],
    "serviceIP": "10.96.0.1",
    "etcd": {
        "clientSecret": {
            "create": False,
            "name": SECRET_NAME,
        },
        "endpoints": ENDPOINTS,
        "externalSecretRevisionsRequired": True,
        "prefix": PREFIX,
    },
}


def helm_template(values: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "helm",
            "template",
            "child",
            str(CHART),
            "--namespace",
            "child-system",
            "--values",
            "-",
        ],
        input=yaml.safe_dump(values, sort_keys=True),
        text=True,
        capture_output=True,
        check=False,
    )


def objects(rendered: str) -> list[dict]:
    return [
        document
        for document in yaml.safe_load_all(rendered)
        if isinstance(document, dict)
    ]


def object_named(rendered: list[dict], kind: str, name: str) -> dict:
    matches = [
        document
        for document in rendered
        if document.get("kind") == kind
        and document.get("metadata", {}).get("name") == name
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one {kind}/{name}, found {len(matches)}"
        )
    return matches[0]


class ExternalEtcdHandoffTests(unittest.TestCase):
    def test_legacy_external_secret_without_revision_contract_still_renders(self) -> None:
        values = copy.deepcopy(BASE_VALUES)
        values["etcd"].pop("externalSecretRevisionsRequired")
        values["etcd"]["serverCATrustRevision"] = SERVER_CA_REVISION

        result = helm_template(values)

        self.assertEqual(result.returncode, 0, result.stderr)
        deployment = object_named(objects(result.stdout), "Deployment", "apiserver")
        annotations = deployment["spec"]["template"]["metadata"]["annotations"]
        self.assertEqual(
            annotations["etcd.samcday.com/server-ca-trust-revision"],
            SERVER_CA_REVISION,
        )
        self.assertNotIn(
            "etcd.samcday.com/client-certificate-revision",
            annotations,
        )

    def test_external_secret_requires_server_ca_trust_revision(self) -> None:
        values = copy.deepcopy(BASE_VALUES)
        values["etcd"]["clientCertificateRevision"] = (
            CLIENT_CERTIFICATE_REVISION
        )

        result = helm_template(values)

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(
            "missing etcd.serverCATrustRevision for externally managed "
            "etcd client Secret",
            result.stderr,
        )

    def test_external_secret_requires_client_certificate_revision(self) -> None:
        values = copy.deepcopy(BASE_VALUES)
        values["etcd"]["serverCATrustRevision"] = SERVER_CA_REVISION

        result = helm_template(values)

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(
            "missing etcd.clientCertificateRevision for externally managed "
            "etcd client Secret",
            result.stderr,
        )

    def test_server_ca_trust_revision_must_be_a_canonical_sha256(self) -> None:
        values = copy.deepcopy(BASE_VALUES)
        values["etcd"]["clientCertificateRevision"] = (
            CLIENT_CERTIFICATE_REVISION
        )
        values["etcd"]["serverCATrustRevision"] = "sha256:NOT-A-DIGEST"

        result = helm_template(values)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(
            "etcd.serverCATrustRevision must be sha256:<64 lowercase hex characters>",
            result.stderr,
        )

    def test_client_certificate_revision_must_be_a_canonical_sha256(self) -> None:
        values = copy.deepcopy(BASE_VALUES)
        values["etcd"]["serverCATrustRevision"] = SERVER_CA_REVISION
        values["etcd"]["clientCertificateRevision"] = "sha256:NOT-A-DIGEST"

        result = helm_template(values)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(
            "etcd.clientCertificateRevision must be sha256:<64 lowercase hex "
            "characters>",
            result.stderr,
        )

    def test_external_handoff_renders_exact_apiserver_contract(self) -> None:
        values = copy.deepcopy(BASE_VALUES)
        values["etcd"]["serverCATrustRevision"] = SERVER_CA_REVISION
        values["etcd"]["clientCertificateRevision"] = (
            CLIENT_CERTIFICATE_REVISION
        )

        result = helm_template(values)
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = objects(result.stdout)
        deployment = object_named(rendered, "Deployment", "apiserver")

        pod_template = deployment["spec"]["template"]
        self.assertEqual(
            pod_template["metadata"]["annotations"],
            {
                "etcd.samcday.com/client-certificate-revision": (
                    CLIENT_CERTIFICATE_REVISION
                ),
                "etcd.samcday.com/server-ca-trust-revision": (
                    SERVER_CA_REVISION
                )
            },
        )

        apiserver = next(
            container
            for container in pod_template["spec"]["containers"]
            if container["name"] == "apiserver"
        )
        self.assertIn(f"--etcd-prefix={PREFIX}", apiserver["command"])
        self.assertIn(
            f"--etcd-servers={','.join(ENDPOINTS)}",
            apiserver["command"],
        )

        etcd_volume = next(
            volume
            for volume in pod_template["spec"]["volumes"]
            if volume["name"] == "apiserver-etcd-client"
        )
        self.assertEqual(etcd_volume["secret"], {"secretName": SECRET_NAME})
        self.assertFalse(
            any(
                document.get("kind") == "Certificate"
                and document.get("metadata", {}).get("name") == SECRET_NAME
                for document in rendered
            )
        )

    def test_chart_managed_secret_does_not_require_server_ca_revision(self) -> None:
        values = copy.deepcopy(BASE_VALUES)
        values["etcd"]["clientSecret"]["create"] = True
        values["etcd"]["externalSecretRevisionsRequired"] = False
        values["etcd"]["certIssuer"] = {
            "kind": "ClusterIssuer",
            "name": "physical-etcd-client",
        }

        result = helm_template(values)
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = objects(result.stdout)
        deployment = object_named(rendered, "Deployment", "apiserver")

        self.assertNotIn(
            "etcd.samcday.com/server-ca-trust-revision",
            deployment["spec"]["template"]["metadata"].get(
                "annotations", {}
            ),
        )
        self.assertNotIn(
            "etcd.samcday.com/client-certificate-revision",
            deployment["spec"]["template"]["metadata"].get(
                "annotations", {}
            ),
        )
        certificate = object_named(rendered, "Certificate", SECRET_NAME)
        self.assertEqual(certificate["spec"]["secretName"], SECRET_NAME)
        self.assertEqual(
            certificate["spec"]["issuerRef"],
            {
                "kind": "ClusterIssuer",
                "name": "physical-etcd-client",
            },
        )

    def test_revision_contract_rejects_chart_managed_secret(self) -> None:
        values = copy.deepcopy(BASE_VALUES)
        values["etcd"]["clientSecret"]["create"] = True
        values["etcd"]["certIssuer"] = {
            "kind": "ClusterIssuer",
            "name": "physical-etcd-client",
        }

        result = helm_template(values)

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(
            "etcd.externalSecretRevisionsRequired is valid only when "
            "etcd.clientSecret.create=false",
            result.stderr,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
