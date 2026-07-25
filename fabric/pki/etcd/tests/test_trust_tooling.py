#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import pathlib
import re
import subprocess
import unittest


REPO = pathlib.Path(__file__).resolve().parents[4]
ROLLOUT = REPO / "scripts" / "rollout-fabric-etcd-client-trust"
ADMIN = REPO / "fabric" / "pki" / "etcd" / "manage-etcdetcetc-admin"
ENABLE_AUTH = REPO / "fabric" / "pki" / "etcd" / "enable-auth"
ETCD_PROFILE = REPO / "fabric" / "butane" / "etcd.yaml"
FIREWALL_PROFILE = REPO / "fabric" / "butane" / "firewall.yaml"
ETCD_RUNBOOK = REPO / "fabric" / "pki" / "etcd" / "README.md"
GLOBAL_ATTENDED_HELPERS = (
    ADMIN,
    ROLLOUT,
    REPO / "scripts" / "rollout-fabric-root-firewall",
    REPO / "scripts" / "rollout-fabric-router-etcd-policy",
    REPO / "scripts" / "qualify-fabric-etcd-pod-sources",
    REPO / "scripts" / "qualify-fabric-etcd-post-open",
)


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=REPO,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


class EnableAuthHelperTests(unittest.TestCase):
    def test_single_endpoint_observer_probe_does_not_accumulate_endpoints(self) -> None:
        source = ENABLE_AUTH.read_text()
        self.assertIn("etcdctl_one_as()", source)
        self.assertIn(
            "etcdctl_one_as fabric-observer https://10.66.0.10:2379 \\\n"
            "  --write-out=json endpoint status",
            source,
        )
        self.assertNotIn(
            "etcdctl_as fabric-observer \\\n"
            "  --endpoints=https://10.66.0.10:2379",
            source,
        )


class TrustRolloutTests(unittest.TestCase):
    def test_check_validates_public_payload_without_live_access(self) -> None:
        result = run(str(ROLLOUT), "--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        output = re.fullmatch(
            r"validated Fabric etcd client-trust rollout payload "
            r"(?P<payload>[0-9a-f]{64}) at revision "
            r"(?P<revision>[0-9a-f]{40})\n",
            result.stdout,
        )
        self.assertIsNotNone(output)
        head = run("git", "rev-parse", "HEAD")
        self.assertEqual(head.returncode, 0, head.stderr)
        self.assertEqual(output.group("revision"), head.stdout.strip())

    def test_confirmation_binds_node_revision_and_payload(self) -> None:
        source = ROLLOUT.read_text()
        self.assertIn(
            "--confirm ROLLOUT-FABRIC-ETCD-CLIENT-TRUST:"
            "<full-node>:<main-commit>:<payload-sha256>",
            source,
        )
        self.assertIn(
            'expected="ROLLOUT-FABRIC-ETCD-CLIENT-TRUST:'
            '$full_node:$head_commit:$payload_hash"',
            source,
        )
        head_commit = source.index(
            "head_commit=$(git -C \"$repo_root\" rev-parse HEAD)"
        )
        self.assertLess(
            head_commit,
            source.index(
                'echo "validated Fabric etcd client-trust rollout payload',
                head_commit,
            ),
        )

    def test_check_rejects_live_selectors(self) -> None:
        result = run(str(ROLLOUT), "--check", "--node", "cp1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--check does not accept", result.stderr)

    def test_live_all_rejects_rollout_selectors_and_other_modes(self) -> None:
        for arguments in (
            ("--verify-live-all", "--node", "cp1"),
            ("--verify-live-all", "--confirm", "WRONG"),
            ("--verify-live-all", "--check"),
        ):
            with self.subTest(arguments=arguments):
                result = run(str(ROLLOUT), *arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("--verify-live-all", result.stderr)

    def test_live_all_is_bounded_read_only_and_reuses_exact_proofs(self) -> None:
        source = ROLLOUT.read_text()
        start = source.index(
            "if $verify_live_all; then\n  readonly verification_nodes=(cp1 cp2 cp3)"
        )
        end = source.index("\nfi\n", start) + len("\nfi\n")
        live_all = source[start:end]

        self.assertIn("readonly verification_nodes=(cp1 cp2 cp3)", live_all)
        self.assertEqual(
            live_all.count(
                'for verification_node in "${verification_nodes[@]}"; do'
            ),
            3,
        )
        self.assertEqual(live_all.count("verify_remote_etcdctl_version"), 2)
        self.assertEqual(live_all.count("assert_etcd_health"), 2)
        self.assertEqual(live_all.count("verify_live_trust"), 1)
        self.assertEqual(live_all.count("assert_api_health"), 1)
        self.assertEqual(live_all.count("assert_k3s_fresh_heartbeat"), 1)
        self.assertLess(
            live_all.index("assert_etcd_health"),
            live_all.index("verify_live_trust"),
        )
        self.assertLess(
            live_all.index("verify_live_trust"),
            live_all.rindex("assert_etcd_health"),
        )
        self.assertIn(
            "FABRIC_ETCD_CLIENT_TRUST_LIVE_ALL=PASS "
            "revision=%s payload_sha256=%s\\n",
            live_all,
        )
        for mutating_contract in (
            "--confirm",
            "create configmap",
            "delete configmap",
            "systemctl restart",
            "systemctl daemon-reload",
            "systemctl reset-failed",
            "systemctl start",
            "systemctl stop",
            "systemctl enable",
            "systemctl disable",
            "/usr/bin/install",
        ):
            with self.subTest(mutating_contract=mutating_contract):
                self.assertNotIn(mutating_contract, live_all)

        self.assertEqual(source.count("verify_live_trust() {"), 1)
        self.assertEqual(source.count("assert_etcd_health() {"), 1)
        self.assertLess(source.index("worktree must be clean"), start)
        self.assertLess(source.index("fetch --quiet origin main"), start)
        self.assertLess(
            source.index("HEAD must exactly match freshly fetched origin/main"),
            start,
        )
        self.assertLess(end, source.index("fabric-maintenance-lock", end))
        self.assertIn("\nverify_live_trust\n", source[end:])
        self.assertIn("\nassert_etcd_health\n", source[end:])

    def test_live_all_proofs_cover_payload_runtime_argv_and_consensus(self) -> None:
        source = ROLLOUT.read_text()
        required = (
            'actual_hash=$("${ssh[@]}" sudo --non-interactive '
            '/usr/bin/sha256sum "$target"',
            '"0:0:$expected_mode:1:regular file"',
            "--property=Result --value",
            "--property=FragmentPath --value",
            "--property=DropInPaths --value",
            "runtime client trust bundle does not contain exactly two certificates",
            "exact physical-plus-delegated concatenation",
            "0:0:444:1:regular file",
            '"/proc/$etcd_pid/cmdline"',
            "--trusted-ca-file=/run/fabric-etcd-client-trust/client-ca-bundle.pem",
            "--peer-trusted-ca-file=/etc/etcd/pki/ca.pem",
            'all(.Status.version == "3.6.13")',
            "([.[].Status.header.member_id] | unique | length) == 3",
            "([.[].Status.leader] | unique | length) == 1",
            "all((.Status.isLearner // false) == false)",
            "member list --write-out=json",
            'name: "fabric-az1-cp1"',
            'name: "fabric-az1-cp2"',
            'name: "fabric-az1-cp3"',
            'peer_url: "https://fabric-az1-cp1.fabric.internal:2380"',
            'peer_url: "https://fabric-az1-cp2.fabric.internal:2380"',
            'peer_url: "https://fabric-az1-cp3.fabric.internal:2380"',
            'client_url: "https://fabric-az1-cp1.fabric.internal:2379"',
            'client_url: "https://fabric-az1-cp2.fabric.internal:2379"',
            'client_url: "https://fabric-az1-cp3.fabric.internal:2379"',
            '(.isLearner // false) == false',
            "select(.Endpoint == $identity.endpoint)",
            ".Status.header.member_id",
            "etcd_member_contract=",
            '[[ $contract == "$etcd_member_contract" ]]',
            "etcd cluster or member identity changed during client-trust verification",
            "((.alarms // []) | length) == 0",
        )
        for contract in required:
            with self.subTest(contract=contract):
                self.assertIn(contract, source)

    def test_member_contract_binds_each_root_endpoint_to_reviewed_topology(self) -> None:
        source = ROLLOUT.read_text()
        function = re.search(
            r"(?ms)^assert_etcd_health\(\) \{(?P<body>.*?)^\}\n\n"
            r"verify_remote_etcdctl_version\(\)",
            source,
        )
        self.assertIsNotNone(function)
        body = function.group("body")

        self.assertLess(
            body.index("--write-out=json endpoint status"),
            body.index("member list --write-out=json"),
        )
        self.assertLess(
            body.index("member list --write-out=json"),
            body.index("alarm list"),
        )
        self.assertIn("($listing.members | length) == 3", body)
        self.assertIn(
            "([$listing.members[].ID | tostring] | unique | length) == 3",
            body,
        )
        self.assertIn(
            "($listing.header.cluster_id | tostring) ==", body
        )
        self.assertIn(".peerURLs == [$identity.peer_url]", body)
        self.assertIn(".clientURLs == [$identity.client_url]", body)
        self.assertIn("status_member_id:", body)
        self.assertLess(
            body.index("die 'etcd membership differs"),
            body.index("if [[ -z $etcd_member_contract ]]"),
        )

    def test_member_contract_filter_rejects_topology_drift(self) -> None:
        source = ROLLOUT.read_text()
        membership_filter = re.search(
            r'''(?ms)  jq -e --argjson status "\$status_json" '\n'''
            r'''(?P<filter>.*?)\n  ' <<<"\$member_json" >/dev/null \|\|''',
            source,
        )
        self.assertIsNotNone(membership_filter)
        jq_filter = membership_filter.group("filter")

        reviewed = (
            (
                "https://10.66.0.10:2379",
                "fabric-az1-cp1",
                101,
                "https://fabric-az1-cp1.fabric.internal:2380",
                "https://fabric-az1-cp1.fabric.internal:2379",
            ),
            (
                "https://10.66.0.11:2379",
                "fabric-az1-cp2",
                102,
                "https://fabric-az1-cp2.fabric.internal:2380",
                "https://fabric-az1-cp2.fabric.internal:2379",
            ),
            (
                "https://10.66.0.12:2379",
                "fabric-az1-cp3",
                103,
                "https://fabric-az1-cp3.fabric.internal:2380",
                "https://fabric-az1-cp3.fabric.internal:2379",
            ),
        )
        status = [
            {
                "Endpoint": endpoint,
                "Status": {
                    "header": {
                        "cluster_id": 1000,
                        "member_id": member_id,
                        "revision": 42,
                    },
                    "leader": 101,
                    "version": "3.6.13",
                    "raftAppliedIndex": 20,
                    "raftIndex": 20,
                    "isLearner": False,
                },
            }
            for endpoint, _, member_id, _, _ in reviewed
        ]
        listing = {
            "header": {"cluster_id": 1000, "member_id": 101},
            "members": [
                {
                    "ID": member_id,
                    "name": name,
                    "peerURLs": [peer_url],
                    "clientURLs": [client_url],
                    "isLearner": False,
                }
                for _, name, member_id, peer_url, client_url in reviewed
            ],
        }

        def evaluate(candidate_listing: object, candidate_status: object) -> int:
            result = subprocess.run(
                [
                    "jq",
                    "-e",
                    "--argjson",
                    "status",
                    json.dumps(candidate_status),
                    jq_filter,
                ],
                input=json.dumps(candidate_listing),
                text=True,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            return result.returncode

        self.assertEqual(evaluate(listing, status), 0)
        omitted_false = copy.deepcopy(listing)
        for member in omitted_false["members"]:
            member.pop("isLearner")
        self.assertEqual(evaluate(omitted_false, status), 0)

        bad_listings = []
        missing_member = copy.deepcopy(listing)
        missing_member["members"].pop()
        bad_listings.append(missing_member)
        wrong_name = copy.deepcopy(listing)
        wrong_name["members"][0]["name"] = "fabric-az1-cp4"
        bad_listings.append(wrong_name)
        learner = copy.deepcopy(listing)
        learner["members"][1]["isLearner"] = True
        bad_listings.append(learner)
        wrong_peer = copy.deepcopy(listing)
        wrong_peer["members"][2]["peerURLs"] = ["https://10.66.0.12:2380"]
        bad_listings.append(wrong_peer)
        wrong_client = copy.deepcopy(listing)
        wrong_client["members"][0]["clientURLs"] = ["https://10.66.0.10:2379"]
        bad_listings.append(wrong_client)
        wrong_cluster = copy.deepcopy(listing)
        wrong_cluster["header"]["cluster_id"] = 2000
        bad_listings.append(wrong_cluster)
        for candidate in bad_listings:
            with self.subTest(candidate=candidate):
                self.assertNotEqual(evaluate(candidate, status), 0)

        mismatched_endpoint_id = copy.deepcopy(status)
        mismatched_endpoint_id[0]["Status"]["header"]["member_id"] = 102
        self.assertNotEqual(evaluate(listing, mismatched_endpoint_id), 0)

        normalizer = re.search(
            r'''(?ms)  contract=\$\(jq -cnS \\\n'''
            r'''    --argjson status "\$status_json" '''
            r'''--argjson listing "\$member_json" '\n'''
            r'''(?P<filter>.*?)\n  '\)''',
            source,
        )
        self.assertIsNotNone(normalizer)

        def normalize(candidate_listing: object, candidate_status: object) -> str:
            result = subprocess.run(
                [
                    "jq",
                    "-cnS",
                    "--argjson",
                    "status",
                    json.dumps(candidate_status),
                    "--argjson",
                    "listing",
                    json.dumps(candidate_listing),
                    normalizer.group("filter"),
                ],
                text=True,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

        normalized = normalize(listing, status)
        self.assertEqual(normalize(omitted_false, status), normalized)
        reordered_listing = copy.deepcopy(listing)
        reordered_listing["members"].reverse()
        self.assertEqual(normalize(reordered_listing, list(reversed(status))), normalized)
        self.assertNotEqual(normalize(wrong_peer, status), normalized)
        self.assertNotEqual(normalize(listing, mismatched_endpoint_id), normalized)

    def test_live_argv_rejects_duplicate_or_overriding_security_flags(self) -> None:
        source = ROLLOUT.read_text()
        helper = re.search(
            r"(?ms)^assert_exact_etcd_flag\(\) \{.*?^\}\n",
            source,
        )
        self.assertIsNotNone(helper)
        harness = "die() { return 1; }\n" + helper.group(0) + (
            '\nassert_exact_etcd_flag "$1" "$2" "$3"\n'
        )
        reviewed_flags = (
            ("--client-cert-auth=", "--client-cert-auth=true"),
            ("--peer-client-cert-auth=", "--peer-client-cert-auth=true"),
            ("--cert-file=", "--cert-file=/etc/etcd/pki/peer.pem"),
            ("--key-file=", "--key-file=/etc/etcd/pki/peer-key.pem"),
            (
                "--trusted-ca-file=",
                "--trusted-ca-file=/run/fabric-etcd-client-trust/client-ca-bundle.pem",
            ),
            ("--peer-cert-file=", "--peer-cert-file=/etc/etcd/pki/peer.pem"),
            ("--peer-key-file=", "--peer-key-file=/etc/etcd/pki/peer-key.pem"),
            (
                "--peer-trusted-ca-file=",
                "--peer-trusted-ca-file=/etc/etcd/pki/ca.pem",
            ),
        )

        def evaluate(argv: str, prefix: str, expected: str) -> int:
            result = subprocess.run(
                ["bash", "-c", harness, "argv-fixture", argv, prefix, expected],
                text=True,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            return result.returncode

        for prefix, expected in reviewed_flags:
            with self.subTest(prefix=prefix):
                self.assertEqual(evaluate(expected, prefix, expected), 0)
                self.assertNotEqual(evaluate(expected + "\n" + expected, prefix, expected), 0)
                self.assertNotEqual(evaluate(prefix + "OVERRIDE", prefix, expected), 0)
                self.assertNotEqual(
                    evaluate(expected + "\n" + prefix + "OVERRIDE", prefix, expected),
                    0,
                )
                invocation = (
                    'assert_exact_etcd_flag "$etcd_argv" \\\n'
                    f"    '{prefix}'"
                )
                self.assertIn(invocation, source)

    def test_live_proof_requires_the_exact_committed_etcd_firewall_dropin(self) -> None:
        source = ROLLOUT.read_text()
        firewall = FIREWALL_PROFILE.read_text()
        live_proof = re.search(
            r"(?ms)^verify_live_trust\(\) \{(?P<body>.*?)^\}\n\nif \$verify_live_all;",
            source,
        )
        self.assertIsNotNone(live_proof)
        body = live_proof.group("body")

        self.assertIn(
            "readonly etcd_firewall_dropin_path="
            "/etc/systemd/system/etcd.service.d/10-fabric-guard.conf",
            source,
        )
        self.assertIn(
            'extract_dropin etcd.service 10-fabric-guard.conf '
            '"$work/etcd-firewall-dropin.conf"',
            source,
        )
        self.assertIn(
            "$'[Unit]\\nRequires=fabric-guard.service\\n"
            "After=fabric-guard.service\\n'",
            source,
        )
        self.assertIn(
            'verify_installed_file "$etcd_firewall_dropin_path" \\',
            body,
        )
        self.assertIn('"$work/etcd-firewall-dropin.conf" 644', body)
        self.assertIn(
            '[[ $trust_dropins == "$systemd_service_dropin_path" ]]', body
        )
        self.assertIn(
            '[[ $etcd_dropins == "$etcd_firewall_dropin_path '
            '$systemd_service_dropin_path" ]]',
            body,
        )
        self.assertNotIn('"etcd.service:$etcd_unit_path"', body)

        self.assertEqual(firewall.count("    - name: etcd.service\n"), 1)
        self.assertIn("        - name: 10-fabric-guard.conf", firewall)
        self.assertIn("            Requires=fabric-guard.service", firewall)
        self.assertIn("            After=fabric-guard.service", firewall)

    def test_rollout_contains_every_safety_gate(self) -> None:
        source = ROLLOUT.read_text()
        required = (
            "HEAD must exactly match freshly fetched origin/main",
            "fabric-maintenance-lock",
            "remote_work=/var/lib/fabric-etcd-client-trust-rollback",
            "rollback_service_path=/etc/systemd/system/$rollback_unit.service",
            "rollback_timer_path=/etc/systemd/system/$rollback_unit.timer",
            "OnCalendar=$deadline_calendar",
            "Persistent=true",
            '"$rollback_unit.service" "$rollback_unit.timer"',
            'systemctl start \\',
            "ROLLBACK_DISARM_READY=",
            "ROLLBACK_DISARMED=",
            "state_lock=$work/state.lock",
            'write_state rollback-required',
            'write_state rolling-back',
            'write_state rolled-back',
            'write_state accepted',
            'flock --unlock "$state_fd"',
            'systemctl start --no-block "${service##*/}"',
            "ROLLBACK_FORCED=",
            "ROLLBACK_CLEANED=",
            "preconditions: {uid: $uid, resourceVersion: $rv}",
            '--unix-socket "$proxy_socket" --request DELETE',
            "global lock could not be released",
            "trust.previously-active",
            "k3s.previously-active",
            "lease_anchor=$(lease_tuple)",
            "lease_advanced=false",
            "prove_lease_not_regressed",
            "verify_k3s_dependency",
            "systemctl restart --no-block fabric-etcd-client-trust.service",
            "systemctl start --no-block k3s.service",
            "/usr/bin/timeout --signal=TERM --kill-after=2s 5s",
            "/usr/local/bin/k3s kubectl --request-timeout=3s",
            "endpoint health",
            "endpoint status",
            "alarm list",
            "readyz?verbose",
            "heartbeat Lease",
            "DropInPaths",
            "--trusted-ca-file=/run/fabric-etcd-client-trust/client-ca-bundle.pem",
            "--peer-trusted-ca-file=/etc/etcd/pki/ca.pem",
        )
        for contract in required:
            with self.subTest(contract=contract):
                self.assertTrue(contract in source, contract)
        self.assertNotIn("systemd-run", source)
        self.assertNotIn("remote_work=/run/", source)
        disarm = source.index("disarm_ready_program=")
        self.assertLess(source.rindex("verify_live_trust"), disarm)
        self.assertLess(source.rindex("assert_etcd_health"), disarm)
        self.assertLess(source.rindex("assert_api_health"), disarm)

    def test_success_lock_release_is_exact_object_preconditioned(self) -> None:
        source = ROLLOUT.read_text()
        self.assertIn('lock_uid=$(jq -er \'.metadata.uid\'', source)
        self.assertIn(
            'lock_rv=$(jq -er \'.metadata.resourceVersion\'', source
        )
        cleanup = source[
            source.index("stop_lock_proxy() {") : source.index("trap cleanup EXIT")
        ]
        self.assertIn(
            "preconditions: {uid: $uid, resourceVersion: $rv}", cleanup
        )
        self.assertIn(
            '--unix-socket="$proxy_socket"',
            cleanup,
        )
        self.assertIn('--request DELETE \\', cleanup)
        self.assertIn('--data-binary "$delete_options"', cleanup)
        self.assertIn("--max-time 10", cleanup)
        self.assertIn("--accept-paths=", cleanup)
        self.assertIn("--reject-methods=", cleanup)
        self.assertIn('kill -KILL "$lock_proxy_pid"', cleanup)
        self.assertNotIn("delete configmap", cleanup)
        self.assertNotIn("delete --raw", cleanup)

    def test_etcd_restart_and_rollback_restore_k3s_before_acknowledgement(
        self,
    ) -> None:
        source = ROLLOUT.read_text()
        rollback_match = re.search(
            r"(?ms)^rollback_program='(?P<body>.*?)'\n",
            source,
        )
        apply_match = re.search(
            r"(?ms)^apply_candidate_program='(?P<body>.*?)'\n",
            source,
        )
        self.assertIsNotNone(rollback_match)
        self.assertIsNotNone(apply_match)
        rollback = rollback_match.group("body")
        apply = apply_match.group("body")

        rollback_order = (
            "restore /etc/systemd/system/etcd.service etcd.service",
            "systemctl daemon-reload",
            "systemctl restart --no-block etcd.service",
            "systemctl start --no-block k3s.service",
            "lease_anchor=$(lease_tuple)",
            "lease_advanced=false",
            "write_state rolled-back",
        )
        rollback_positions = [rollback.index(item) for item in rollback_order]
        self.assertEqual(rollback_positions, sorted(rollback_positions))
        daemon_reload = rollback.index("systemctl daemon-reload")
        etcd_restart = rollback.index("systemctl restart --no-block etcd.service")
        for trust_transition in (
            "systemctl restart --no-block fabric-etcd-client-trust.service",
            "systemctl stop --no-block fabric-etcd-client-trust.service",
        ):
            with self.subTest(trust_transition=trust_transition):
                transition = rollback.index(trust_transition)
                self.assertLess(daemon_reload, transition)
                self.assertLess(transition, etcd_restart)
        self.assertIn("$k3s_ready == True", rollback)
        self.assertIn(
            '$lease_uid_after == "$lease_uid_before"', rollback
        )
        self.assertIn('$lease_rv_after != "$lease_rv_before"', rollback)
        self.assertIn(
            "((10#$lease_after_ns > 10#$lease_before_ns))", rollback
        )
        self.assertLess(
            rollback.index("$k3s_healthy"),
            rollback.index("lease_anchor=$(lease_tuple)"),
        )

        apply_order = (
            "systemctl restart --no-block fabric-etcd-client-trust.service",
            "systemctl restart --no-block etcd.service",
            "systemctl start --no-block k3s.service",
            'bounded_k3s get --raw="/readyz?verbose"',
            "ROLLOUT_APPLIED=",
        )
        apply_positions = [apply.index(item) for item in apply_order]
        self.assertEqual(apply_positions, sorted(apply_positions))

    def test_state_locked_k3s_calls_have_double_timeouts(self) -> None:
        source = ROLLOUT.read_text()
        for variable in (
            "rollback_program",
            "apply_candidate_program",
            "disarm_ready_program",
            "disarm_program",
        ):
            with self.subTest(variable=variable):
                match = re.search(
                    rf"(?ms)^\s*{variable}='(?P<body>.*?)'\n", source
                )
                self.assertIsNotNone(match)
                body = match.group("body")
                bounded = re.search(
                    r"(?ms)^bounded_k3s\(\) \{(?P<body>.*?)^\}\n", body
                )
                self.assertIsNotNone(bounded)
                self.assertIn(
                    "/usr/bin/timeout --signal=TERM --kill-after=2s 5s",
                    bounded.group("body"),
                )
                self.assertIn(
                    "/usr/local/bin/k3s kubectl --request-timeout=3s",
                    bounded.group("body"),
                )
                without_wrapper = body.replace(bounded.group(0), "")
                self.assertNotIn("/usr/local/bin/k3s kubectl", without_wrapper)
                self.assertIn("bounded_k3s ", without_wrapper)

    def test_rollback_timing_budgets_are_strictly_nested(self) -> None:
        source = ROLLOUT.read_text()
        rollback = re.search(
            r"(?ms)^rollback_program='(?P<body>.*?)'\n", source
        ).group("body")
        force = re.search(
            r"(?ms)^force_rollback_program='(?P<body>.*?)'\n", source
        ).group("body")

        internal = int(
            re.search(r"rollback_deadline=\$\(\(now \+ ([0-9]+)\)\)", rollback).group(1)
        )
        service = int(re.search(r"TimeoutStartSec=([0-9]+)s", source).group(1))
        force_poll = int(
            re.search(
                r"for \(\(attempt = 0; attempt < ([0-9]+); attempt\+\+\)\); do",
                force,
            ).group(1)
        )
        armed = int(
            re.search(r"deadline_epoch=\$\(\(remote_now \+ ([0-9]+)\)\)", source).group(1)
        )

        self.assertLess(internal, service)
        self.assertLess(service, force_poll)
        self.assertEqual(force_poll, armed)
        self.assertIn("sleep 1", force)

    def test_disarm_lease_proof_rejects_replacement_and_regression(self) -> None:
        source = ROLLOUT.read_text()
        programs = {}
        for variable in ("disarm_ready_program", "disarm_program"):
            match = re.search(
                rf"(?ms)^\s*{variable}='(?P<body>.*?)'\n", source
            )
            self.assertIsNotNone(match)
            body = match.group("body")
            rfc3339 = re.search(
                r"(?ms)^rfc3339_ns\(\) \{.*?^\}\n", body
            )
            proof = re.search(
                r"(?ms)^prove_lease_not_regressed\(\) \{.*?^\}\n", body
            )
            self.assertIsNotNone(rfc3339)
            self.assertIsNotNone(proof)
            programs[variable] = rfc3339.group(0) + proof.group(0)

        self.assertEqual(
            programs["disarm_ready_program"], programs["disarm_program"]
        )
        harness = r'''set -euo pipefail
proof_uid=11111111-1111-1111-1111-111111111111
proof_rv=10
proof_renew=2026-07-22T00:00:00Z
node=fabric-az1-cp1
lease_tuple=$1
node_ready=$2
bounded_k3s() {
  if [[ " $* " == *" get lease "* ]]; then
    printf '%s\n' "$lease_tuple"
  else
    printf '%s\n' "$node_ready"
  fi
}
''' + programs["disarm_program"] + "\nprove_lease_not_regressed\n"

        uid = "11111111-1111-1111-1111-111111111111"
        other_uid = "22222222-2222-2222-2222-222222222222"
        cases = (
            ("exact proof", f"{uid}\t10\t2026-07-22T00:00:00Z", "True", True),
            ("advanced", f"{uid}\t11\t2026-07-22T00:00:01Z", "True", True),
            ("advanced same rv", f"{uid}\t10\t2026-07-22T00:00:01Z", "True", False),
            ("same time new rv", f"{uid}\t11\t2026-07-22T00:00:00Z", "True", False),
            ("older", f"{uid}\t9\t2026-07-21T23:59:59Z", "True", False),
            ("replacement", f"{other_uid}\t11\t2026-07-22T00:00:01Z", "True", False),
            ("not ready", f"{uid}\t11\t2026-07-22T00:00:01Z", "False", False),
        )
        for name, lease_tuple, ready, accepted in cases:
            with self.subTest(name=name):
                result = subprocess.run(
                    ["bash", "-c", harness, "lease-proof", lease_tuple, ready],
                    text=True,
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                self.assertEqual(result.returncode == 0, accepted, result.stderr)

    def test_embedded_rollback_and_disarm_programs_are_valid_bash(self) -> None:
        source = ROLLOUT.read_text()
        for variable in (
            "force_rollback_program",
            "rollback_program",
            "disarm_ready_program",
            "disarm_program",
            "rollback_cleanup_program",
        ):
            match = re.search(rf"(?ms)^\s*{variable}='(?P<body>.*?)'\n", source)
            self.assertIsNotNone(match, variable)
            result = subprocess.run(
                ["bash", "-n"],
                input=match.group("body"),
                text=True,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, f"{variable}: {result.stderr}")

    def test_profile_preserves_distinct_client_and_peer_trust(self) -> None:
        profile = ETCD_PROFILE.read_text()
        self.assertEqual(
            profile.count(
                "--trusted-ca-file=/run/fabric-etcd-client-trust/client-ca-bundle.pem"
            ),
            1,
        )
        self.assertEqual(
            profile.count("--peer-trusted-ca-file=/etc/etcd/pki/ca.pem"), 1
        )
        self.assertNotIn(
            "--peer-trusted-ca-file=/run/fabric-etcd-client-trust/", profile
        )


class AdminHelperTests(unittest.TestCase):
    def test_all_etcd_foundation_operations_share_one_global_lock(self) -> None:
        for helper in GLOBAL_ATTENDED_HELPERS:
            source = helper.read_text()
            with self.subTest(helper=helper.name):
                self.assertIn("fabric-maintenance-lock", source)
                self.assertNotIn("fabric-etcd-maintenance-lock", source)
                self.assertNotIn("fabric-network-maintenance-lock", source)

    def test_check_validates_offline_inputs_without_etcdctl(self) -> None:
        result = run(str(ADMIN), "--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "validated offline fabric-etcdetcetc administration inputs and "
            "etcdctl 3.6.13 pin\n",
        )

    def test_confirmation_is_operation_specific(self) -> None:
        for operation in ("--provision", "--revoke"):
            with self.subTest(operation=operation):
                result = run(str(ADMIN), operation)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("--confirm is required", result.stderr)

        result = run(str(ADMIN), "--plan-provision", "--confirm", "WRONG")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("planning modes do not accept", result.stderr)

        result = run(str(ADMIN), "--verify-remote", "--confirm", "WRONG")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--verify-remote does not accept", result.stderr)

    def test_remote_verifier_is_delegated_read_only_and_cleanup_bound(self) -> None:
        source = ADMIN.read_text()
        start = source.index("if [[ $mode == verify-remote ]]; then\n  readonly online_ca=")
        end = source.index("\nfi\n\netcdctl_output=", start) + len("\nfi\n")
        block = source[start:end]

        for contract in (
            "fabric_secure_tmpdir fabric-etcdetcetc-admin-verify",
            '(.data | keys) == ["ca.crt", "tls.crt", "tls.key"]',
            "secret_resource_version=",
            "secret_data_hash=",
            "fabric-etcd-server-ca --output=json",
            'yq -er \'.data["ca.crt"]\'',
            "live physical server CA differs byte-for-byte",
            "env -i LC_ALL=C",
            "admin_validity_seconds",
            "admin_leaf_duration_seconds + admin_leaf_issuance_skew_seconds",
            "etcdctl_binary_sha256",
            'sha256sum -- "$local_etcdctl"',
            "-F /dev/null",
            "-N",
            "-T",
            "ClearAllForwardings=no",
            "ExitOnForwardFailure=yes",
            "ControlMaster=no",
            "ForwardAgent=no",
            "StrictHostKeyChecking=yes",
            "ProxyCommand=$proxy_command",
            "UserKnownHostsFile=$known_hosts",
            "-L 127.0.0.1:42379:127.0.0.1:2379",
            "-L 127.0.0.1:42380:10.66.0.11:2379",
            "-L 127.0.0.1:42381:10.66.0.12:2379",
            'ssh "${ssh_arguments[@]}" sam@10.66.0.10 &',
            "tunnel_pid=$!",
            "assert_tunnel_alive",
            'kill -TERM "$tunnel_pid"',
            'kill -KILL "$tunnel_pid"',
            'wait "$tunnel_pid"',
            "trap '' HUP INT TERM",
            '"--cacert=$online_work/server-ca.pem"',
            '"--cert=$online_work/admin.pem"',
            '"--key=$online_work/admin-key.pem"',
            "delegated_query status endpoint status",
            "delegated_query alarms alarm list",
            "delegated_query auth auth status",
            "delegated_query root-user user get root",
            "delegated_query fabric-root-user user get fabric-root",
            "delegated_query fabric-root-role role get fabric-root",
            "delegated_query admin-user user get fabric-etcdetcetc",
            "delegated_query members member list",
            "secret_after_resource_version",
            "delegated administrator Secret rotated during the forwarded proof",
            "FABRIC_ETCDETCETC_ADMIN_REMOTE=PASS revision=%s",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, block)

        for forbidden in (
            "sops --decrypt",
            "root-key.pem",
            "create configmap",
            "delete configmap",
            "user add",
            "user delete",
            "grant-role",
            "fabric-ssh cp1",
            "/run/fabric-etcdetcetc-admin",
            "admin.pem sam@",
            "admin-key.pem sam@",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, block)

    def test_admin_contract_is_exact_and_retains_fabric_root(self) -> None:
        source = ADMIN.read_text()
        required = (
            "fabric_secure_tmpdir fabric-etcdetcetc-admin",
            "etcdctl $required_version is required",
            'admin_user=fabric-etcdetcetc',
            'user add "$admin_user" --no-password',
            'user grant-role "$admin_user" root',
            'user delete "$admin_user"',
            "fabric-maintenance-lock",
            "prestate_hash=",
            "alarm_before=$(alarm_contract)",
            '[[ $contract == \'{"alarms":[]}\' ]]',
            "--argjson alarms \"$alarm_before\"",
            "alarm_after=$(alarm_contract)",
            '[[ $alarm_after == "$alarm_before" ]]',
            "HEAD must exactly match freshly fetched origin/main",
            '.roles == ["root"]',
            '.roles == ["fabric-root"]',
            'key: "/bootstrap/", range_end: "/bootstrap0"',
            'key: "/fabric-root/", range_end: "/fabric-root0"',
            "fabric-root user/role changed",
            "ambiguous $admin_user provision committed",
            "rollback_admin_roles != '[]'",
            "deletion of partially provisioned $admin_user could not be proven",
            "every bound authorization and membership field while holding it",
            "alarm_locked=$(alarm_contract)",
            '[[ $fabric_root_locked == "$fabric_root_before" ]]',
            '[[ $admin_exists_locked == "$admin_exists" ]]',
        )
        for contract in required:
            with self.subTest(contract=contract):
                self.assertIn(contract, source)
        self.assertLess(
            source.index("provision_started=true"),
            source.index('user add "$admin_user" --no-password'),
        )

    def test_admin_lock_release_is_exact_object_preconditioned_and_bounded(self) -> None:
        source = ADMIN.read_text()
        mutation = source[source.index(
            "work=$(fabric_secure_tmpdir fabric-etcdetcetc-admin 16777216)"
        ):]
        required = (
            'ik=("$repo_root/scripts/ik" --context=fabric --request-timeout=15s)',
            '--from-literal="holder=$lock_holder" --output=json',
            "lock_uid=$(jq -er '.metadata.uid'",
            "lock_resource_version=$(jq -er '.metadata.resourceVersion'",
            '.metadata.uid == $uid',
            '.metadata.resourceVersion == $resource_version',
            'preconditions: {uid: $uid, resourceVersion: $resource_version}',
            '"${ik[@]}" proxy --unix-socket="$proxy_socket"',
            '--accept-paths="^/api/v1/namespaces/kube-system/configmaps/$lock_name\\$"',
            "--reject-methods='^(GET|POST|PUT|PATCH|HEAD|OPTIONS|CONNECT|TRACE)$'",
            '--unix-socket "$proxy_socket" --request DELETE',
            '--data-binary "$delete_options"',
            '--max-time 10',
            'kill -KILL "$lock_proxy_pid"',
            '--ignore-not-found --output=name',
            "a global Fabric maintenance lock exists after delegated-admin "
            "conditional release",
        )
        for contract in required:
            with self.subTest(contract=contract):
                self.assertIn(contract, mutation)
        self.assertNotIn('delete configmap "$lock_name"', mutation)
        self.assertNotIn('delete --raw', mutation)
        self.assertLess(
            mutation.index("lock_uid=$(jq -er '.metadata.uid'"),
            mutation.index('user add "$admin_user" --no-password'),
        )
        release = mutation[mutation.index("current_lock=") :]
        self.assertLess(
            release.index('.metadata.resourceVersion == $resource_version'),
            release.index("delete_lock_preconditioned ||"),
        )

    def test_admin_lock_accepts_an_opaque_resource_version(self) -> None:
        source = ADMIN.read_text()
        match = re.search(
            r"jq -e --arg holder \"\$lock_holder\" '(?P<filter>.*?)'"
            r" <<<\"\$lock_object\"",
            source,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match)
        lock = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "fabric-maintenance-lock",
                "namespace": "kube-system",
                "uid": "217c60e5-e4b3-4a8b-a453-7a0b4db60cb3",
                "resourceVersion": "opaque.rv/alpha",
            },
            "data": {"holder": "test-holder"},
        }
        accepted = subprocess.run(
            ["jq", "-e", "--arg", "holder", "test-holder", match.group("filter")],
            input=json.dumps(lock),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        lock["metadata"]["resourceVersion"] = "opaque rv"
        rejected = subprocess.run(
            ["jq", "-e", "--arg", "holder", "test-holder", match.group("filter")],
            input=json.dumps(lock),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertNotEqual(rejected.returncode, 0)

    def test_runbook_keeps_gates_independent(self) -> None:
        runbook = ETCD_RUNBOOK.read_text()
        self.assertIn("one voter at a time", runbook)
        self.assertIn("--plan-provision", runbook)
        self.assertIn("PROVISION:fabric-etcdetcetc-admin:<main-commit>:<prestate-sha256>", runbook)
        self.assertIn("--plan-revoke", runbook)
        self.assertIn("REVOKE:fabric-etcdetcetc-admin:<main-commit>:<prestate-sha256>", runbook)
        self.assertIn("does not remove the delegated CA from trust", runbook)


if __name__ == "__main__":
    unittest.main(verbosity=2)
