#!/usr/bin/env python3
from __future__ import annotations

import json
import pathlib
import subprocess
import unittest


REPO = pathlib.Path(__file__).resolve().parents[4]
POLICY = (
    REPO
    / "fabric"
    / "cluster"
    / "etcdetcetc-policy"
    / "signer-admission.yaml"
)
POLICY_NAME = "fabric-etcd-client-v1-clusterissuer"
ISSUER_NAME = "fabric-etcd-client-v1"
SECRET_NAME = "fabric-etcd-client-v1-ca"


class SignerAdmissionContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        rendered = subprocess.run(
            ["yq", "-o=json", "-I=0", ".", str(POLICY)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        documents = [
            json.loads(line)
            for line in rendered.splitlines()
            if line and line != "---"
        ]
        cls.policy = next(
            document
            for document in documents
            if document.get("kind") == "ValidatingAdmissionPolicy"
            and document.get("metadata", {}).get("name") == POLICY_NAME
        )

    def test_reserved_secret_rules_have_no_resource_name_blind_spot(self) -> None:
        self.assertEqual(
            self.policy["spec"]["matchConstraints"]["resourceRules"],
            [
                {
                    "apiGroups": ["cert-manager.io"],
                    "apiVersions": ["v1"],
                    "operations": ["CREATE", "UPDATE", "DELETE"],
                    "resources": ["clusterissuers"],
                },
                {
                    "apiGroups": ["cert-manager.io"],
                    "apiVersions": ["v1"],
                    "operations": ["CREATE", "UPDATE"],
                    "resources": ["issuers"],
                },
            ],
        )

    def test_namespaced_and_cluster_scoped_aliases_are_negative_cases(self) -> None:
        condition = " ".join(
            self.policy["spec"]["matchConditions"][0]["expression"].split()
        )
        validation = " ".join(
            self.policy["spec"]["validations"][0]["expression"].split()
        )

        self.assertEqual(
            condition,
            "(request.resource.resource == 'clusterissuers' && "
            "((request.operation == 'DELETE' ? oldObject : object).metadata.name "
            "== 'fabric-etcd-client-v1' || (request.operation != 'DELETE' && "
            "has(object.spec.ca) && object.spec.ca.secretName == "
            "'fabric-etcd-client-v1-ca'))) || (request.resource.resource == "
            "'issuers' && request.namespace == 'cert-manager' && "
            "has(object.spec.ca) && object.spec.ca.secretName == "
            "'fabric-etcd-client-v1-ca')",
        )
        self.assertEqual(
            validation,
            "request.resource.resource == 'clusterissuers' && "
            "variables.target.metadata.name == 'fabric-etcd-client-v1' && "
            "has(variables.target.spec.ca) && "
            "variables.target.spec.ca.secretName == 'fabric-etcd-client-v1-ca' "
            "&& (request.userInfo.username == "
            "'system:serviceaccount:flux-system:kustomize-controller' || "
            "(request.operation == 'UPDATE' && request.userInfo.username == "
            "'system:serviceaccount:cert-manager:cert-manager' && "
            "oldObject != null && object.spec == oldObject.spec))",
        )

        negative_cases = (
            ("issuers", "CREATE", "cert-manager", "alias", SECRET_NAME),
            ("issuers", "UPDATE", "cert-manager", "alias", SECRET_NAME),
            ("clusterissuers", "CREATE", "", "alias", SECRET_NAME),
            ("clusterissuers", "UPDATE", "", "alias", SECRET_NAME),
        )
        for case in negative_cases:
            with self.subTest(case=case):
                self.assertTrue(self._matches(*case))
                self.assertFalse(self._validation_allows(*case))

    def test_alias_cleanup_and_unrelated_namespaced_secrets_remain_usable(self) -> None:
        self.assertFalse(
            self._matches("clusterissuers", "DELETE", "", "alias", SECRET_NAME)
        )
        self.assertFalse(
            self._matches("issuers", "CREATE", "child", "ca", SECRET_NAME)
        )
        self.assertFalse(
            self._matches(
                "issuers", "CREATE", "cert-manager", "unrelated", "other-ca"
            )
        )

    @staticmethod
    def _matches(
        resource: str,
        operation: str,
        namespace: str,
        name: str,
        secret_name: str,
    ) -> bool:
        if resource == "clusterissuers":
            return name == ISSUER_NAME or (
                operation != "DELETE" and secret_name == SECRET_NAME
            )
        return (
            resource == "issuers"
            and operation in {"CREATE", "UPDATE"}
            and namespace == "cert-manager"
            and secret_name == SECRET_NAME
        )

    @staticmethod
    def _validation_allows(
        resource: str,
        operation: str,
        namespace: str,
        name: str,
        secret_name: str,
    ) -> bool:
        del operation, namespace
        return (
            resource == "clusterissuers"
            and name == ISSUER_NAME
            and secret_name == SECRET_NAME
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
