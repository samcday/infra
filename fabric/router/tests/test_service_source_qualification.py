#!/usr/bin/env python3

from __future__ import annotations

import pathlib
import unittest


REPO_ROOT = pathlib.Path(__file__).parents[3]
RETIRED_HELPER = REPO_ROOT / "scripts" / "qualify-fabric-service-sources"
POD_HELPER = REPO_ROOT / "scripts" / "qualify-fabric-etcd-pod-sources"


class ServiceSourceQualificationRetirementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.retired = RETIRED_HELPER.read_text(encoding="utf-8")
        cls.replacement = POD_HELPER.read_text(encoding="utf-8")

    def test_historical_preinstall_simulator_cannot_report_success(self) -> None:
        self.assertIn("This pre-service-node source simulator is retired.", self.retired)
        self.assertIn("exit 1", self.retired)
        self.assertNotIn("FABRIC_SERVICE_SOURCE_QUALIFICATION=pass", self.retired)
        self.assertNotIn("--run)", self.retired)

    def test_retirement_routes_to_real_pod_replacement(self) -> None:
        self.assertIn(
            "scripts/qualify-fabric-etcd-pod-sources --check", self.retired
        )
        self.assertIn(
            "the live router is still in its deny phase", self.retired
        )
        self.assertIn("Reject-services-to-root-etcd", self.replacement)


if __name__ == "__main__":
    unittest.main()
