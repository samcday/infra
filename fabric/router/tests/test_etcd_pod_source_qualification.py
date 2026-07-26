#!/usr/bin/env python3

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import tempfile
import unittest


REPO_ROOT = pathlib.Path(__file__).parents[3]
HELPER = REPO_ROOT / "scripts" / "qualify-fabric-etcd-pod-sources"
ROOT_FIREWALL = REPO_ROOT / "fabric" / "butane" / "firewall.yaml"
ROOT_ROLLOUT = REPO_ROOT / "scripts" / "rollout-fabric-root-firewall"


class EtcdPodSourceQualificationPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = HELPER.read_text(encoding="utf-8")
        cls.root_firewall = ROOT_FIREWALL.read_text(encoding="utf-8")
        cls.root_rollout = ROOT_ROLLOUT.read_text(encoding="utf-8")

    def test_probe_identity_addresses_and_image_are_fixed(self) -> None:
        for required in (
            "readonly namespace=fabric-etcd-source-probe",
            "readonly router_probe_table=fabric_etcd_pod_source_probe",
            "readonly target_port=2379",
            "readonly -a service_nodes=(fabric-az1-svc1 fabric-az1-svc2)",
            "readonly -a service_addresses=(10.66.1.10 10.66.1.11)",
            "readonly -a target_addresses=(10.66.0.10 10.66.0.11 10.66.0.12)",
            "svc1_cp1_sources_v4 svc1_cp2_sources_v4 svc1_cp3_sources_v4",
            "svc2_cp1_sources_v4 svc2_cp2_sources_v4 svc2_cp3_sources_v4",
            "docker.io/library/busybox:1.37.0@sha256:9532d8c39891ca2ecde4d30d7710e01fb739c87a8b9299685c63704296b16028",
            "QUALIFY-FABRIC-ETCD-POD-SOURCES",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)

    def test_run_is_guarded_by_pushed_main_lock_and_confirmation(self) -> None:
        for required in (
            "[[ $(operator_git branch --show-current) == main ]]",
            "[[ -z $(operator_git status --porcelain) ]]",
            "operator_git fetch --quiet origin main",
            '[[ $head == "$(operator_git rev-parse origin/main)" ]]',
            '[[ $confirm == "$expected_confirmation" ]]',
            "readonly fabric_network_lock=/run/lock/fabric-network-operation.lock",
            'flock --exclusive --nonblock "$network_lock_fd"',
            "readonly fabric_network_cluster_lock=fabric-maintenance-lock",
            'acquire_cluster_network_lock',
            '--from-literal="holder=$cluster_lock_holder"',
            'Global Fabric maintenance lock remains held by %s for inspection',
            "observer_verify || die 'fabric observer isolation changed before qualification'",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)

    def test_router_parent_lock_mode_is_hidden_and_run_only(self) -> None:
        usage = self.script[: self.script.index("die() {")]
        for hidden in (
            "--internal-parent-network-lock-fd",
            "--internal-parent-cluster-lock-holder",
        ):
            with self.subTest(hidden=hidden):
                self.assertNotIn(hidden, usage)
                self.assertEqual(self.script.count(f"    {hidden})"), 1)
        for required in (
            "internal parent lock arguments must be specified together",
            "internal parent locks are valid only with --run",
            "parent_lock_mode=true",
            'holder_prefix="$(hostname):$PPID:router-etcd-policy:$head:"',
            "internal cluster-lock holder does not name the direct router parent and revision",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)

    def test_parent_host_lock_proof_is_exact_and_inherited(self) -> None:
        function = re.search(
            r"(?ms)^validate_parent_network_lock\(\) \{\n(?P<body>.*?)^\}$",
            self.script,
        )
        self.assertIsNotNone(function)
        body = function.group("body")
        self.assertIn("^[1-9][0-9]{0,5}$", body)
        for required in (
            '[[ -f /proc/self/fd/$parent_network_lock_fd ]]',
            'readlink -e -- "/proc/self/fd/$parent_network_lock_fd"',
            '[[ $resolved == "$fabric_network_lock" ]]',
            "stat -Lc '%u:%g:%a:%h'",
            "stat -Lc '%d:%i'",
            '[[ $fd_identity == "$path_identity" ]]',
            'exec {probe_fd}>>"$fabric_network_lock"',
            'if flock --exclusive --nonblock "$probe_fd"; then',
            "Fabric host lock was not already held by the parent",
            'flock --exclusive --nonblock "$parent_network_lock_fd"',
        ):
            with self.subTest(required=required):
                self.assertIn(required, body)

    def test_inherited_flock_open_description_survives_exec(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock = pathlib.Path(temporary) / "lock"
            lock.touch(mode=0o600)
            harness = r'''
set -euo pipefail
lock=$1
exec {parent_fd}>>"$lock"
flock --exclusive --nonblock "$parent_fd"
bash -c '
  set -euo pipefail
  inherited_fd=$1
  lock=$2
  [[ $(readlink -e -- "/proc/self/fd/$inherited_fd") == "$lock" ]]
  flock --exclusive --nonblock "$inherited_fd"
  exec {fresh_fd}>>"$lock"
  ! flock --exclusive --nonblock "$fresh_fd"
' _ "$parent_fd" "$lock"
'''
            subprocess.run(["bash", "-c", harness, "_", str(lock)], check=True)

    def test_parent_configmap_identity_is_rechecked_after_cleanup(self) -> None:
        for required in (
            '.data == {holder: $holder}',
            ".metadata.uid == $uid",
            ".metadata.resourceVersion == $resource_version",
            "parent-owned Fabric locks changed during source qualification",
            'scope: $lock_scope',
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)
        cleanup_start = self.script.index("cleanup() {")
        cleanup_end = self.script.index("\n}\ntrap cleanup EXIT", cleanup_start)
        cleanup = self.script[cleanup_start:cleanup_end]
        self.assertLess(
            cleanup.index('--raw="/api/v1/namespaces/$namespace"'),
            cleanup.index("if $parent_lock_mode && ! parent_locks_unchanged"),
        )
        self.assertLess(
            cleanup.index("if $parent_lock_mode && ! parent_locks_unchanged"),
            cleanup.index("FABRIC_ETCD_POD_SOURCE_QUALIFICATION=pass"),
        )
        self.assertEqual(self.script.count("cluster_lock_held=true"), 1)

    def test_live_phase_must_still_be_the_exact_old_reject(self) -> None:
        for required in (
            "validate_live_reject_phase",
            "firewall.fabric_flat_reject_services_etcd.name Reject-services-to-root-etcd",
            "firewall.fabric_flat_reject_services_etcd.dest_port '2379 2380 2381'",
            "firewall.fabric_flat_reject_services_etcd.target REJECT",
            '"comment": "!fw4: Reject-services-to-root-etcd"',
            '"require_increase": ["current_etcd_reject"]',
            '[[ $reject_delta =~ ^[0-9]+$ && $reject_delta -ge 6 ]]',
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)
        self.assertNotIn("Allow-platform-to-root-etcd-client", self.script)

    def test_fw4_semantics_are_identical_before_and_after_probes(self) -> None:
        for required in (
            "normalize_router_fw4",
            "del(.handle)",
            '.counter |= del(.packets, .bytes)',
            'validate_live_reject_phase "$router_after"',
            'normalize_router_fw4 "$router_before" "$router_before_semantic"',
            'normalize_router_fw4 "$router_after" "$router_after_semantic"',
            'cmp -s -- "$router_before_semantic" "$router_after_semantic"',
            "live fw4 semantics changed during source qualification",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)

    def test_read_only_check_removes_its_tmpfs_evidence(self) -> None:
        for required in (
            "preflight_cleanup()",
            "trap preflight_cleanup EXIT",
            'if [[ $mode == check && $status -eq 0 ]]',
            'rm -rf -- "$evidence_dir"',
            "Preflight did not complete; protected evidence remains",
            "trap - EXIT INT TERM",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)

    def test_observer_records_sources_without_a_packet_verdict(self) -> None:
        table = re.search(
            r"read -r -d '' payload <<EOF \|\| true\n(?P<body>.*?)\nEOF",
            self.script,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(table)
        body = table.group("body")
        for required in (
            "type filter hook forward priority -10",
            "flags dynamic",
            'comment "$router_table_comment"',
            "ip saddr ${service_addresses[0]} ip daddr ${target_addresses[0]}",
            "ip saddr ${service_addresses[0]} ip daddr ${target_addresses[1]}",
            "ip saddr ${service_addresses[0]} ip daddr ${target_addresses[2]}",
            "ip saddr ${service_addresses[1]} ip daddr ${target_addresses[0]}",
            "ip saddr ${service_addresses[1]} ip daddr ${target_addresses[1]}",
            "ip saddr ${service_addresses[1]} ip daddr ${target_addresses[2]}",
            "no verdict",
        ):
            with self.subTest(required=required):
                self.assertIn(required, body)
        for forbidden in (
            " accept",
            " drop",
            " reject",
            " queue",
            " dnat",
            " snat",
            " masquerade",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, body.lower())

    def test_probes_are_restricted_ordinary_pods_one_per_service_node(self) -> None:
        for required in (
            "hostNetwork: false",
            'nodeName: $node',
            "automountServiceAccountToken: false",
            'seccompProfile: {type: "RuntimeDefault"}',
            "runAsUser: 65534",
            "runAsGroup: 65534",
            "allowPrivilegeEscalation: false",
            'capabilities: {drop: ["ALL"]}',
            "readOnlyRootFilesystem: true",
            'operator: "Equal", value: "true"',
            'for index in "${!service_nodes[@]}"; do',
            'for target in $targets; do',
            'nc -w 4 \\$target $target_port',
            'ETCD_PROBE_REJECTED node=$node expected_host_ip=$address target=\\$target:$target_port',
            '$podIp != $hostIp',
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)

    def test_runtime_contract_accepts_only_absent_or_false_host_network(self) -> None:
        predicate = (
            '((.spec | has("hostNetwork") | not) or '
            '(.spec.hostNetwork == false))'
        )
        self.assertIn(predicate, self.script)
        cases = (
            ({"spec": {}}, True),
            ({"spec": {"hostNetwork": False}}, True),
            ({"spec": {"hostNetwork": True}}, False),
            ({"spec": {"hostNetwork": None}}, False),
        )
        for document, accepted in cases:
            with self.subTest(document=document):
                result = subprocess.run(
                    ["jq", "-e", predicate],
                    input=json.dumps(document),
                    text=True,
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                self.assertEqual(result.returncode == 0, accepted, result.stderr)

    def test_final_pod_json_is_protected_before_runtime_validation(self) -> None:
        function = re.search(
            r"(?ms)^wait_probe_pod\(\) \{\n(?P<body>.*?)^\}$",
            self.script,
        )
        self.assertIsNotNone(function)
        body = function.group("body")
        write = 'printf \'%s\\n\' "$pod" >"$evidence_dir/$name.pod.json"'
        chmod = 'chmod 0600 -- "$evidence_dir/$name.pod.json"'
        validation = "  jq -e \\\n"
        self.assertIn(write, body)
        self.assertIn(chmod, body)
        self.assertLess(body.index(write), body.index(chmod))
        self.assertLess(body.index(chmod), body.index(validation))

    def test_only_exact_observed_node_addresses_can_pass(self) -> None:
        for required in (
            '] == [$expected]',
            "was not observed only as $expected; stop without broadening policy",
            '"fabric-az1-svc1": "10.66.1.10"',
            '"fabric-az1-svc2": "10.66.1.11"',
            'policyInput: "exact-node-addresses-only"',
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)
        for forbidden in (
            "10.42.0.0/16",
            "10.42.0.0/24",
            "podCIDR",
            "pod_cidr",
            "ipBlock",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.script)

    def test_exit_trap_removes_and_verifies_every_ephemeral_artifact(self) -> None:
        for required in (
            "trap cleanup EXIT",
            '--raw="/api/v1/namespaces/$namespace"',
            "preconditions: {uid: $uid, resourceVersion: $resource_version}",
            'current_uid != "$namespace_uid"',
            '--arg comment "$router_table_comment"',
            ".name == $table and .comment == $comment",
            'router_ssh /usr/sbin/nft delete table inet "$router_probe_table"',
            'grep -Fxq "table inet $router_probe_table"',
            "ephemeralCleanup: \"verified\"",
            "FABRIC_ETCD_POD_SOURCE_QUALIFICATION=pass",
            "trap '' HUP INT TERM",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)

    def test_all_six_service_to_root_paths_are_required(self) -> None:
        for required in (
            'for service_index in "${!service_nodes[@]}"; do',
            'for target_index in "${!target_addresses[@]}"; do',
            'validate_observed_source "$service_index" "$target_index"',
            "qualifiedPathCount: 6",
            '"10.66.0.10", "10.66.0.11", "10.66.0.12"',
            "expected at least six",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)

    def test_root_return_routes_cover_both_service_nodes(self) -> None:
        for required in (
            "service_probes=(10.66.1.10 10.66.1.11)",
            'for service_probe in "${service_probes[@]}"; do',
            '[[ $destination == "$service_probe"',
            '[[ $interface != "$expected_interface" ]]',
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.root_rollout)

    def test_root_static_contract_separates_etcd_client_and_metrics(self) -> None:
        service_set = "elements = { 10.66.1.10, 10.66.1.11 }"
        service_allow = (
            "ip saddr @service_nodes_v4 tcp dport 2379 counter accept "
            'comment "trusted platform etcd clients"'
        )
        catchall = (
            'tcp dport 2379 counter drop comment "deny all other etcd clients '
            'including stale flows"'
        )
        metrics_allow = (
            "ip saddr @service_nodes_v4 tcp dport { 2112, 2381, 9100 } "
            'counter accept comment "trusted platform monitoring"'
        )
        operator_ssh_allow = (
            "ip saddr @service_nodes_v4 tcp dport 22 "
            'counter accept comment "tailnet-routed operator SSH"'
        )
        self.assertEqual(self.root_firewall.count(service_set), 1)
        self.assertEqual(self.root_firewall.count(service_allow), 1)
        self.assertEqual(self.root_firewall.count(catchall), 1)
        self.assertEqual(self.root_firewall.count(metrics_allow), 1)
        self.assertEqual(self.root_firewall.count(operator_ssh_allow), 1)
        self.assertLess(
            self.root_firewall.index(service_allow), self.root_firewall.index(catchall)
        )
        self.assertNotRegex(self.root_firewall, r"10\.66\.1\.0/24|10\.42\.\d")
        self.assertNotRegex(
            self.root_firewall,
            r"@service_nodes_v4 tcp dport[^\n]*2380",
        )
        self.assertNotRegex(metrics_allow, r"\b(?:2379|2380)\b")
        etcd_accepts = [
            line.strip()
            for line in self.root_firewall.splitlines()
            if "tcp dport 2379 counter accept comment" in line
        ]
        self.assertEqual(
            etcd_accepts,
            [
                'ip saddr @root_nodes_v4 tcp dport 2379 counter accept comment "root etcd clients"',
                'ip saddr @etcd_maintenance_v4 tcp dport 2379 counter accept comment "attended etcd maintenance"',
                'ip saddr @service_nodes_v4 tcp dport 2379 counter accept comment "trusted platform etcd clients"',
            ],
        )
        for required in (
            'service_set=\'elements = { 10.66.1.10, 10.66.1.11 }\'',
            "service_etcd_allow='ip saddr @service_nodes_v4 tcp dport 2379",
            "service_operator_ssh_allow='ip saddr @service_nodes_v4 tcp dport 22",
            "etcd_catchall='tcp dport 2379 counter drop",
            "service_etcd_allow_line -lt $etcd_catchall_line",
            "expected_etcd_accepts='ip saddr @root_nodes_v4 tcp dport 2379",
            '[[ $actual_etcd_accepts == "$expected_etcd_accepts" ]]',
            "committed root policy broadens service access to a subnet, PodCIDR, or etcd peer port",
            "lock_name=fabric-maintenance-lock",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.root_rollout)

    def test_root_rollout_proves_exact_live_input_before_disarm(self) -> None:
        for required in (
            "assert_live_etcd_input_policy()",
            "service_elements == '10.66.1.10,10.66.1.11'",
            "expected_live_etcd_rules='ip saddr @root_nodes_v4 tcp dport 2379",
            '[[ $live_etcd_rules == "$expected_live_etcd_rules" ]]',
            'grep -nFx "$service_etcd_allow"',
            '$allow_line -lt $catchall_line',
            '[[ $live_rules == "$expected_live_rules" ]]',
            '[[ $live_service_accepts == "$expected_live_service_accepts" ]]',
            "assert_firewall_loader_provenance()",
            "FragmentPath",
            "DropInPaths",
            "ExecReload=/usr/sbin/nft --check --file /etc/nftables/fabric-guard.nft",
            "These are the final pre-disarm gates",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.root_rollout)
        self.assertGreaterEqual(
            self.root_rollout.count("assert_live_etcd_input_policy"), 3
        )
        disarm = self.root_rollout.index("disarm_ready_program=")
        self.assertLess(self.root_rollout.rindex("installed_firewall_hash="), disarm)
        self.assertLess(self.root_rollout.rindex("installed_routing_hash="), disarm)
        self.assertLess(
            self.root_rollout.rindex("assert_firewall_loader_provenance"), disarm
        )
        self.assertLess(self.root_rollout.rindex("assert_live_etcd_input_policy"), disarm)

    def test_root_rollback_is_persistent_and_acknowledged(self) -> None:
        for required in (
            "rollback_work=/var/lib/fabric-root-network-policy-rollback",
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
            "firewall.previous.sha256",
            "routing-live.previous.sha256",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.root_rollout)
        self.assertNotIn("systemd-run", self.root_rollout)

    def test_root_embedded_rollback_and_disarm_programs_are_valid_bash(self) -> None:
        for variable in (
            "force_rollback_program",
            "capture_live_program",
            "rollback_program",
            "apply_candidate_program",
            "verify_live_program",
            "disarm_ready_program",
            "disarm_program",
            "rollback_cleanup_program",
        ):
            match = re.search(
                rf"(?ms)^\s*{variable}='(?P<body>.*?)'\n", self.root_rollout
            )
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


if __name__ == "__main__":
    unittest.main()
