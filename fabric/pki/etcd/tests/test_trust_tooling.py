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


class TrustRolloutTests(unittest.TestCase):
    def test_check_validates_public_payload_without_live_access(self) -> None:
        result = run(str(ROLLOUT), "--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(
            result.stdout,
            r"^validated Fabric etcd client-trust rollout payload [0-9a-f]{64}\n$",
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
        self.assertIn('[[ -z $trust_dropins ]]', body)
        self.assertIn(
            '[[ $etcd_dropins == "$etcd_firewall_dropin_path" ]]', body
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
            "global lock ownership changed; refuse to delete it",
            "global lock could not be released",
            "trust.previously-active",
            "systemctl restart fabric-etcd-client-trust.service",
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
                self.assertIn(contract, source)
        self.assertNotIn("systemd-run", source)
        self.assertNotIn("remote_work=/run/", source)
        disarm = source.index("disarm_ready_program=")
        self.assertLess(source.rindex("verify_live_trust"), disarm)
        self.assertLess(source.rindex("assert_etcd_health"), disarm)
        self.assertLess(source.rindex("assert_api_health"), disarm)

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
