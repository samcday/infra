import hashlib
import pathlib
import re
import subprocess
import unittest


REPO = pathlib.Path(__file__).parents[3]
HELPER = REPO / "scripts" / "rollout-fabric-router-monitoring-policy"
TRANSITION = REPO / "fabric" / "router" / "monitoring-policy-transition.uci"
DESIRED = REPO / "fabric" / "router" / "files" / "etc" / "uci-defaults" / "30-isolation"


class MonitoringPolicyRolloutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.helper = HELPER.read_text(encoding="utf-8")
        cls.transition = TRANSITION.read_text(encoding="utf-8")
        cls.desired = DESIRED.read_text(encoding="utf-8")

    def test_shell_and_embedded_remote_programs_are_syntactically_valid(self) -> None:
        subprocess.run(["bash", "-n", str(HELPER)], check=True)
        heredocs = re.findall(
            r"<<'(?P<tag>REMOTE_[A-Z_]+)'\n(?P<body>.*?)\n(?P=tag)",
            self.helper,
            flags=re.DOTALL,
        )
        self.assertEqual([tag for tag, _ in heredocs], [
            "REMOTE_VALIDATE", "REMOTE_APPLY", "REMOTE_FINALIZE", "REMOTE_ROLLBACK_NOW"
        ])
        for tag, body in heredocs:
            with self.subTest(tag=tag):
                subprocess.run(["sh", "-n"], input=body, text=True, check=True)

    def test_payload_hash_and_commands_are_exact(self) -> None:
        digest = hashlib.sha256(TRANSITION.read_bytes()).hexdigest()
        self.assertIn(f"readonly payload_sha256={digest}", self.helper)
        lines = self.transition.splitlines()
        self.assertEqual(len(lines), 17)
        self.assertEqual(
            lines[0],
            "del_list firewall.fabric_flat_reject_services_etcd_internal."
            "dest_port='2381'",
        )
        self.assertEqual(
            lines[-1], "reorder firewall.fabric_flat_services_root_metrics='9'"
        )
        for line in lines[1:-1]:
            with self.subTest(line=line):
                self.assertEqual(self.desired.count(line), 1)

    def test_aperture_is_exact_and_peer_port_stays_rejected(self) -> None:
        for source in ("10.66.1.10/32", "10.66.1.11/32"):
            self.assertEqual(self.transition.count(f"src_ip='{source}'"), 1)
        for root in ("10.66.0.10/32", "10.66.0.11/32", "10.66.0.12/32"):
            self.assertEqual(self.transition.count(f"dest_ip='{root}'"), 1)
        ports = re.findall(
            r"fabric_flat_services_root_metrics\.dest_port='([0-9]+)'",
            self.transition,
        )
        self.assertEqual(ports, ["2112", "2381", "9100"])
        self.assertNotIn("dest_port='2379'", self.transition)
        self.assertNotIn("dest_port='2380'", self.transition)
        self.assertIn(
            "assert_uci firewall.fabric_flat_reject_services_etcd_internal."
            "dest_port 2380",
            self.helper,
        )
        self.assertIn("Reject-other-services-to-roots", self.helper)
        self.assertIn("Reject-other-roots-to-services", self.helper)

    def test_attended_authorization_and_both_locks_are_mandatory(self) -> None:
        for required in (
            "ROLLOUT-FABRIC-ROUTER-MONITORING-POLICY",
            '[[ $(operator_git branch --show-current) == main ]]',
            '[[ -z $(operator_git status --porcelain) ]]',
            'operator_git fetch --quiet origin main',
            'head == "$(operator_git rev-parse origin/main)"',
            'expected_confirmation=$confirmation_prefix:$head:$payload_sha256',
            'flock --exclusive --nonblock "$local_lock_fd"',
            '"$repo_root/scripts/ik" --context=fabric --request-timeout=10s',
            'create configmap',
            'preconditions: {uid: $uid, resourceVersion: $rv}',
            'mkdir "$operation_lock"',
            "router-local operation lock is already held",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.helper)
        self.assertNotIn('"$repo_root/scripts/ik" --context=hub', self.helper)

    def test_exact_preflight_watchdog_rollback_and_post_proof_are_ordered(self) -> None:
        pre = self.helper.index('validate_remote_phase pre "$evidence_dir/router-before-apply.txt"')
        stage = self.helper.index("stage_and_apply", pre)
        post = self.helper.index('validate_remote_phase post "$evidence_dir/router-post-apply.txt"', stage)
        finalize = self.helper.index("finalize_remote", post)
        final = self.helper.index('validate_remote_phase post "$evidence_dir/router-final.txt"', finalize)
        self.assertLess(pre, stage)
        self.assertLess(stage, post)
        self.assertLess(post, finalize)
        self.assertLess(finalize, final)
        for required in (
            '"$init" enable',
            '"$init" start',
            '"$init" running',
            'cp "$work/candidate/firewall" /etc/config/.firewall.monitoring-candidate.next',
            "request_remote_rollback",
            'exec "$work/rollback" now',
            'cp "$work/firewall.before" /etc/config/.firewall.monitoring-rollback.next',
            "FABRIC_ROUTER_MONITORING_POLICY_ROLLOUT=pass",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.helper)

    def test_pre_and_post_contracts_are_distinct(self) -> None:
        self.assertIn(
            "assert_uci firewall.fabric_flat_reject_services_etcd_internal."
            "dest_port '2380 2381'",
            self.helper,
        )
        self.assertIn(
            "assert_absent firewall.fabric_flat_services_root_metrics", self.helper
        )
        self.assertIn("expected_rules=26", self.helper)
        self.assertIn("expected_chain=16", self.helper)
        self.assertIn("expected_rules=27", self.helper)
        self.assertIn("expected_chain=17", self.helper)
        self.assertIn(
            "assert_uci firewall.fabric_flat_services_root_metrics.dest_port "
            "'2112 2381 9100'",
            self.helper,
        )


if __name__ == "__main__":
    unittest.main()
