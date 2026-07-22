#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import unittest


REPO = pathlib.Path(__file__).resolve().parents[4]
RUNBOOK = REPO / "fabric" / "pki" / "etcdetcetc" / "ROTATION.md"
CRD = REPO / "apps" / "etcdetcetc" / "src" / "crd.rs"
CLUSTER = REPO / "apps" / "etcdetcetc" / "src" / "cluster.rs"
TENANT = REPO / "apps" / "etcdetcetc" / "src" / "tenant.rs"
ADMIN = (
    REPO
    / "fabric"
    / "cluster"
    / "etcdetcetc"
    / "admin-certificate.yaml"
)
BUTANE = REPO / "fabric" / "butane" / "etcd.yaml"
ROLLOUT = REPO / "scripts" / "rollout-fabric-etcd-client-trust"
QUALIFIER = REPO / "scripts" / "qualify-fabric-etcd-post-open"


class RotationDesignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runbook = RUNBOOK.read_text(encoding="utf-8")
        cls.runbook_words = " ".join(cls.runbook.split())
        cls.crd = CRD.read_text(encoding="utf-8")
        cls.cluster = CLUSTER.read_text(encoding="utf-8")
        cls.tenant = TENANT.read_text(encoding="utf-8")

    def test_runbook_is_explicitly_non_executable_until_tooling_exists(self) -> None:
        self.assertIn("Status: design contract only", self.runbook)
        self.assertIn("Nothing in this document authorizes a live mutation", self.runbook)
        for blocker in (
            "fabric/pki/etcdetcetc/generate-client-ca-rotation",
            "fabric/butane/etcd.yaml",
            "scripts/rollout-fabric-etcd-client-trust",
            "fabric/pki/etcdetcetc/verify-foundation",
            "scripts/qualify-fabric-etcd-post-open",
            "replicaCount",
            "Automated warning",
        ):
            with self.subTest(blocker=blocker):
                self.assertIn(blocker, self.runbook)

    def test_routine_phase_order_is_fail_closed(self) -> None:
        phases = [
            "### R0: generate an unused next generation",
            "### R1: trust next without permitting it to sign",
            "### R2: publish the next signer, still unused",
            "### R3: reissue the existing admin credential",
            "### R4: reissue every tenant credential",
            "### R5: stop old signing",
            "### R6: remove old trust",
        ]
        offsets = [self.runbook.index(phase) for phase in phases]
        self.assertEqual(offsets, sorted(offsets))
        self.assertIn("Do not collapse adjacent phases", self.runbook_words)
        self.assertLess(
            self.runbook.index("physical + current + next", offsets[1]),
            self.runbook.index(".spec.issuerRef.name", offsets[3]),
        )
        self.assertLess(
            self.runbook.index(
                "prove the old ClusterIssuer\nand CA Secret are both Gone", offsets[5]
            ),
            self.runbook.index("T_stop + 24h + 5m", offsets[5]),
        )

    def test_rotation_never_changes_immutable_admin_secret_reference(self) -> None:
        immutable = (
            'self.spec.authSecretRef == oldSelf.spec.authSecretRef'
        )
        self.assertIn(immutable, self.crd)
        self.assertIn("Never change\n`EtcdCluster.spec.authSecretRef`", self.runbook)
        self.assertIn("There is no second admin Secret", self.runbook)

        tenant_tls_offset = self.crd.index("pub tenant_tls")
        rules_offset = self.crd.index("pub struct EtcdClusterSpec")
        self.assertGreater(tenant_tls_offset, rules_offset)
        self.assertNotIn("self.spec.tenantTls == oldSelf.spec.tenantTls", self.crd)

    def test_current_admin_and_tenant_leaf_contract_supports_reissuance(self) -> None:
        admin = ADMIN.read_text(encoding="utf-8")
        for required in (
            "duration: 24h",
            "renewBefore: 8h",
            "rotationPolicy: Always",
            "kind: ClusterIssuer",
            "- client auth",
        ):
            with self.subTest(required=required):
                self.assertIn(required, admin)

        for required in (
            '"duration": "24h"',
            '"renewBefore": "8h"',
            '"rotationPolicy": "Always"',
            '"kind": issuer_ref.kind.as_str()',
            '"usages": ["digital signature", "client auth"]',
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.tenant)

    def test_controller_polls_both_admin_and_tenant_secret_rotation(self) -> None:
        self.assertIn("compute_config_hash(&cluster.spec, &secret", self.cluster)
        self.assertIn("for (k, v) in entries", self.cluster)
        self.assertIn("Action::requeue(Duration::from_secs(15))", self.cluster)
        self.assertIn(
            "poll picks up cert-manager renewals and referenced admin-Secret or",
            self.tenant,
        )
        self.assertIn(
            "physical-server-CA ConfigMap rotation",
            self.tenant,
        )
        self.assertIn("Duration::from_secs(5 * 60)", self.tenant)
        self.assertIn("Tenant `Ready=True` alone is insufficient", self.runbook)

    def test_proof_requires_cryptographic_and_inventory_evidence(self) -> None:
        for required in (
            "CertificateRequest",
            "observedGeneration == metadata.generation",
            "fails verification\n   against current",
            "AKI matches next's SKI",
            "staging and stable Secret",
            "one-to-one inventory",
            "PermissionDenied",
            "physical server CA, never the client Issuer CA",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.runbook)

    def test_emergency_contains_before_cleanup(self) -> None:
        emergency = self.runbook.index("## Emergency compromised-CA containment")
        fence = self.runbook.index("Fence `svc1` and `svc2`", emergency)
        physical_only = self.runbook.index("exact `physical only`", emergency)
        revoke = self.runbook.index("may the\n   `fabric-etcdetcetc` user be revoked", emergency)
        self.assertLess(fence, physical_only)
        self.assertLess(physical_only, revoke)
        self.assertIn("revocation is cleanup, not the\n   containment boundary", self.runbook)
        self.assertIn("Do not suspend the config child before", self.runbook)
        self.assertIn("Never re-add the compromised fingerprint", self.runbook)

    def test_current_single_ca_constraints_are_named_as_implementation_work(self) -> None:
        butane = BUTANE.read_text(encoding="utf-8")
        rollout = ROLLOUT.read_text(encoding="utf-8")
        qualifier = QUALIFIER.read_text(encoding="utf-8")
        self.assertIn(
            "rendered client trust bundle does not contain exactly two certificates",
            butane,
        )
        self.assertIn(
            "runtime client trust bundle does not contain exactly two certificates",
            rollout,
        )
        self.assertIn(
            'fabric/pki/etcdetcetc/client-ca.pem',
            qualifier,
        )
        self.assertIn("support three-, two-, and one-certificate", self.runbook)


if __name__ == "__main__":
    unittest.main(verbosity=2)
