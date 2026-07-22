#!/usr/bin/env python3

from __future__ import annotations

import pathlib
import subprocess
import unittest


REPO = pathlib.Path(__file__).resolve().parents[3]
VERIFIER = REPO / "scripts" / "verify-fabric-etcd-pre-open"
ROOT_POLICY = REPO / "scripts" / "rollout-fabric-root-firewall"
ROUTER_POLICY = REPO / "scripts" / "rollout-fabric-router-etcd-policy"


class EtcdPreOpenVerifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = VERIFIER.read_text(encoding="utf-8")
        cls.root_policy = ROOT_POLICY.read_text(encoding="utf-8")
        cls.router_policy = ROUTER_POLICY.read_text(encoding="utf-8")

    def test_script_is_valid_and_explicitly_read_only(self) -> None:
        subprocess.run(["bash", "-n", str(VERIFIER)], check=True)
        for forbidden in (
            " create configmap ",
            " delete configmap ",
            " patch ",
            " apply ",
            " replace ",
            " rollout restart ",
            " user add ",
            " user delete ",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.source)

    def test_pre_open_phase_is_exact_revision_and_fail_closed(self) -> None:
        for required in (
            'expected_revision="main@sha1:$head"',
            ".status.artifact.revision == $revision",
            ".status.lastAppliedRevision == $revision",
            "fabric-root",
            "fabric-foundation",
            "fabric-platform",
            "fabric-etcdetcetc-policy",
            "fabric-etcdetcetc-config",
            "verify_suspended_child fabric-etcdetcetc-controller",
            "verify_suspended_child fabric-etcdetcetc-runtime",
            "Fabric controller HelmRelease already exists",
            "runtime CRD already exists before controller activation",
            "deployments,replicasets,statefulsets,daemonsets,jobs,cronjobs,replicationcontrollers,pods",
            "--ignore-not-found --output=name",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.source)

    def test_live_admission_specs_equal_the_defaulted_committed_contract(self) -> None:
        for required in (
            "kubectl kustomize",
            '"$repo_root/fabric/cluster/etcdetcetc-policy"',
            "server_defaults",
            '.matchPolicy = (.matchPolicy // "Equivalent")',
            '.namespaceSelector = (.namespaceSelector // {})',
            '.objectSelector = (.objectSelector // {})',
            'map(.scope = (.scope // "*"))',
            "live_contract=$(jq -cnS",
            '[[ $live_contract == "$expected_contract" ]]',
            "live Fabric signer admission specs differ",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.source)

    def test_every_cross_cluster_and_security_prerequisite_is_live(self) -> None:
        for required in (
            "etcdetcetc\tetcdetcetc",
            "cloud-cluster\tetcdetcetc",
            "cloud-cluster\tetcdetcetc-cloud-etcd",
            "verify_issuer selfsigned selfsigned",
            'verify_issuer "$client_issuer" ca',
            "verify_admission_status",
            "verify_admin_certificate",
            'rollout-fabric-etcd-client-trust" --verify-live-all',
            'rollout-fabric-root-firewall" --verify-live-all',
            'manage-etcdetcetc-admin" --verify-remote',
            'FABRIC_ETCDETCETC_ADMIN_REMOTE=PASS revision=$head',
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.source)

    def test_image_pin_is_registry_and_source_tree_bound(self) -> None:
        for required in (
            "skopeo inspect --retry-times 3",
            '"docker://$repository:$tag"',
            '"docker://$repository@$digest"',
            '.Digest == $digest and .Architecture == "amd64" and .Os == "linux"',
            "org.opencontainers.image.revision",
            "merge-base --is-ancestor",
            'rev-parse "$image_revision:apps/etcdetcetc"',
            'rev-parse "$head:apps/etcdetcetc"',
            'tag != "$bootstrap_image_tag"',
            'digest != "$bootstrap_image_digest"',
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.source)

    def test_admin_secret_key_is_never_requested_or_written(self) -> None:
        self.assertIn("ca.crt\\ntls.crt\\ntls.key", self.source)
        self.assertIn("{.data.tls\\.crt}", self.source)
        self.assertIn("{.data.ca\\.crt}", self.source)
        self.assertNotIn("{.data.tls\\.key}", self.source)
        self.assertNotIn("tls.key}'", self.source)

    def test_router_owned_lock_is_rechecked_at_both_boundaries(self) -> None:
        self.assertEqual(self.source.count("verify_lock_contract\n"), 2)
        self.assertIn('.data == {holder: $holder}', self.source)
        self.assertIn("--expected-lock-holder", self.source)

    def test_root_policy_has_a_non_mutating_all_root_live_verifier(self) -> None:
        subprocess.run(["bash", "-n", str(ROOT_POLICY)], check=True)
        start = self.root_policy.index("if $verify_live_all; then\n", 1000)
        end = self.root_policy.index("\nfi\n\nassert_firewall_loader_provenance", start)
        branch = self.root_policy[start:end]
        for required in (
            "cp1:fabric-az1-cp1:10.66.0.10",
            "cp2:fabric-az1-cp2:10.66.0.11",
            "cp3:fabric-az1-cp3:10.66.0.12",
            "assert_firewall_loader_provenance",
            "assert_live_etcd_input_policy",
            "validate_service_route",
            "committed_firewall_hash",
            "committed_routing_hash",
            "fabric-guard.service",
            "live routed-host sysctls differ from the exact policy",
            "stale root-policy rollout state",
            "FABRIC_ROOT_NETWORK_POLICY_LIVE=pass",
            "exit 0",
        ):
            with self.subTest(required=required):
                self.assertIn(required, branch)
        for forbidden in (
            "create configmap",
            "delete configmap",
            "systemctl reload",
            "systemctl restart",
            "/usr/bin/install",
            "/usr/bin/tee",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, branch)
        for required in (
            "guard_unit_hash",
            "etcd_guard_dropin_hash",
            "etcd.service.d/10-fabric-guard.conf",
            "does not load exactly the reviewed fabric-guard drop-in",
        ):
            with self.subTest(persistence=required):
                self.assertIn(required, self.root_policy)

    def test_both_independent_root_checks_require_hooked_drop_chains_and_sets(self) -> None:
        for source in (self.root_policy, self.router_policy):
            for required in (
                'type: "filter", hook: "input", prio: 10, policy: "drop"',
                'type: "filter", hook: "forward", prio: 10, policy: "drop"',
                'name: "root_nodes_v4"',
                'elem: ["10.66.0.10", "10.66.0.11", "10.66.0.12"]',
                'name: "service_nodes_v4"',
                'elem: ["10.66.1.10", "10.66.1.11"]',
                'name: "observer_v4"',
                'elem: ["10.66.0.2"]',
                'name: "etcd_maintenance_v4"',
                'flags: ["timeout"], timeout: 900',
                '"gc-interval": 60, elem: []',
                'ct state { established, related } counter accept comment "forwarded replies"',
                'counter drop comment "deny all new forwarding"',
            ):
                with self.subTest(script=source[:30], required=required):
                    self.assertIn(required, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
