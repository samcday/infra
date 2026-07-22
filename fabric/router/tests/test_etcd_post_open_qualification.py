#!/usr/bin/env python3

from __future__ import annotations

import copy
import json
import pathlib
import re
import subprocess
import unittest


REPO_ROOT = pathlib.Path(__file__).parents[3]
HELPER = REPO_ROOT / "scripts" / "qualify-fabric-etcd-post-open"
ACTIVATION = REPO_ROOT / "fabric" / "cluster" / "etcdetcetc" / "README.md"
ROUTER_RUNBOOK = REPO_ROOT / "fabric" / "router" / "README.md"
DATA_FILES = REPO_ROOT / "fabric" / "router" / "data-files.txt"


class EtcdPostOpenQualificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = HELPER.read_text(encoding="utf-8")
        cls.activation = ACTIVATION.read_text(encoding="utf-8")
        cls.router_runbook = ROUTER_RUNBOOK.read_text(encoding="utf-8")
        cls.data_files = DATA_FILES.read_text(encoding="utf-8")

    def test_shell_and_every_embedded_program_are_syntactically_valid(self) -> None:
        subprocess.run(["bash", "-n", str(HELPER)], check=True)
        subprocess.run(["shellcheck", "-x", str(HELPER)], check=True)

        sh_programs = re.findall(
            r"<<'(?P<tag>REMOTE_ROUTER_POST|PROBE_SCRIPT)'(?: \|\| true)?\n"
            r"(?P<body>.*?)\n(?P=tag)",
            self.script,
            flags=re.DOTALL,
        )
        self.assertEqual({tag for tag, _body in sh_programs}, {
            "REMOTE_ROUTER_POST",
            "PROBE_SCRIPT",
        })
        for tag, body in sh_programs:
            with self.subTest(tag=tag):
                subprocess.run(["sh", "-n"], input=body, text=True, check=True)

        bash_program = re.search(
            r"<<'REMOTE_ROOT_CONTINUITY'\n(?P<body>.*?)\nREMOTE_ROOT_CONTINUITY",
            self.script,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(bash_program)
        subprocess.run(
            ["bash", "-n"],
            input=bash_program.group("body"),
            text=True,
            check=True,
        )

    def test_revision_attendance_and_both_network_locks_are_mandatory(self) -> None:
        for required in (
            "QUALIFY-FABRIC-ETCD-POST-OPEN",
            "[[ $(operator_git branch --show-current) == main ]]",
            "[[ -z $(operator_git status --porcelain) ]]",
            "operator_git fetch --quiet origin main",
            '[[ $head == "$(operator_git rev-parse origin/main)" ]]',
            'expected_confirmation="$confirmation_prefix:$head"',
            '[[ $confirm == "$expected_confirmation" ]]',
            "readonly fabric_network_lock=/run/lock/fabric-network-operation.lock",
            'flock --exclusive --nonblock "$network_lock_fd"',
            "readonly fabric_network_cluster_lock=fabric-maintenance-lock",
            "acquire_cluster_network_lock",
            '--from-literal="holder=$cluster_lock_holder"',
            "Global Fabric maintenance lock remains held by %s for inspection",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)

    def test_router_ssh_and_final_policy_are_exact(self) -> None:
        for required in (
            'ip netns exec "$observer_namespace" ssh-keyscan -4 -T 5 -t ed25519',
            '[[ $scanned_fingerprint == "$serial_fingerprint" ]]',
            "-o StrictHostKeyChecking=yes",
            "-o HostKeyAlgorithms=ssh-ed25519",
            "-o GlobalKnownHostsFile=/dev/null",
            "-o KnownHostsCommand=none",
            "-o ForwardAgent=no",
            "-o ClearAllForwardings=yes",
            "-o ProxyCommand=none",
            "-o ProxyJump=none",
            "-o IdentityAgent=none",
            'assert_target_section fabric_flat_services_etcd_client',
            "Allow-platform-to-root-etcd-client 2379 ACCEPT",
            'assert_target_section fabric_flat_reject_services_etcd_internal',
            "Reject-services-to-root-etcd-internal 2380 REJECT",
            'assert_target_section fabric_flat_services_root_metrics',
            "Allow-platform-to-root-metrics '2112 2381 9100' ACCEPT",
            "assert_uci_absent firewall.fabric_flat_reject_services_etcd",
            "assert_uci firewall.fabric_router_services_http.dest_ip '10.66.1.1/32'",
            "assert_uci firewall.fabric_router_services_http.dest_port 80",
            "live service-side router HTTP rule is absent",
            '[ "$actual_input" = "$expected_input" ]',
            "live input_lan terminal verdict is not exact",
            '[ "$actual_rules" = "$expected_rules" ]',
            '[ "$actual_forward" = "$expected_forward" ]',
            "live forward_lan terminal verdict is not exact",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)

    def test_root_guards_accept_exact_service_nodes_on_client_and_metrics_ports(self) -> None:
        for required in (
            "elements = { 10.66.1.10, 10.66.1.11 }",
            'ip saddr @service_nodes_v4 tcp dport 2379 counter accept comment "trusted platform etcd clients"',
            'tcp dport 2379 counter drop comment "deny all other etcd clients including stale flows"',
            'ip saddr @service_nodes_v4 tcp dport { 2112, 2381, 9100 } counter accept comment "trusted platform monitoring"',
            "! grep -Eq '@service_nodes_v4 tcp dport.*2380'",
            "fabric_guard semantics changed during acceptance",
            "new_packets - old_packets >= 2",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)

    def test_probe_toolchain_is_immutable_and_runtime_verified(self) -> None:
        image = (
            "docker.io/library/busybox:1.37.0@sha256:"
            "9532d8c39891ca2ecde4d30d7710e01fb739c87a8b9299685c63704296b16028"
        )
        archive_hash = (
            "b4928654aed84d90952620c7144555e4186d795e1e7414e65fe0cf6265fd0465"
        )
        for required in (
            image,
            "sha256:7a3ebe5bfd1a4a19797d20b0c0bb39d44393e9a03fd852c0865b0f540d868df0",
            "sha256:db287cb6be81219cd18c1d82b70908f5d33eb028568b456f78eedff2ff2930e4",
            archive_hash,
            "etcdctl version: 3.6.13",
            "API version: 3.6",
            "sha256sum -c -",
            "http://10.66.1.1/static/etcd-v3.6.13-linux-amd64.tar.gz",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)
        self.assertIn(archive_hash, self.data_files)

    def test_network_policy_and_pods_are_narrow_and_restricted(self) -> None:
        for required in (
            'podSelector: {matchLabels:',
            '"fabric.samcday.com/revision": $revision',
            '{ipBlock: {cidr: "10.66.1.1/32"}}',
            '{port: 80, protocol: "TCP"}',
            '{ipBlock: {cidr: "10.66.0.10/32"}}',
            '{ipBlock: {cidr: "10.66.0.11/32"}}',
            '{ipBlock: {cidr: "10.66.0.12/32"}}',
            '{port: 2379, protocol: "TCP"}',
            '{port: 2380, protocol: "TCP"}',
            '{port: 2381, protocol: "TCP"}',
            "hostNetwork: false",
            "automountServiceAccountToken: false",
            "runAsNonRoot: true",
            "runAsUser: 65534",
            'seccompProfile: {type: "RuntimeDefault"}',
            "allowPrivilegeEscalation: false",
            'capabilities: {drop: ["ALL"]}',
            "readOnlyRootFilesystem: true",
            'secretName: $secret',
            '([.spec.volumes[] | select(has("configMap"))] | length) == 0',
            'emptyDir: {medium: "Memory", sizeLimit: "128Mi"}',
            '.spec.containers[0].command == ["/bin/sh", "-ec"]',
            '.spec.containers[0].args == [$script, "probe", $node, $address, $inside, $outside, $value]',
            '.spec.containers[0].resources == {',
            '.spec.containers[0].securityContext == {',
            '.spec.containers[0].volumeMounts == [',
            '.spec.volumes == [',
            '(.spec.hostPID // false) == false',
            '(.spec.hostIPC // false) == false',
            '(.spec.shareProcessNamespace // false) == false',
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)
        self.assertNotIn("kind: \"Namespace\"", self.script)
        self.assertNotIn("create namespace", self.script)

    def test_each_service_probe_covers_ports_mtls_and_prefix_rbac(self) -> None:
        for required in (
            "for address in 10.66.0.10 10.66.0.11 10.66.0.12",
            'nc -n -z -w 3 "$address" 2379',
            'nc -n -z -w 3 "$address" 2381',
            'nc -n -z -w 3 "$address" 2380',
            "metrics=reachable peer=blocked",
            "no_client_cert=rejected tenant_client=accepted",
            '--cacert=/credential/ca.crt --cert=/credential/tls.crt --key=/credential/tls.key',
            'tenant_ctl lease grant 30',
            'tenant_ctl put "$inside_key" "$value" --lease="$lease_id"',
            'tenant_ctl get "$inside_key" --print-value-only',
            'tenant_ctl del "$inside_key"',
            'tenant_ctl get "$outside_key"',
            'tenant_ctl put "$outside_key" "$value" --lease="$lease_id"',
            "grep -Fq 'etcdserver: permission denied' /work/outside-read.stderr",
            "grep -Fq 'etcdserver: permission denied' /work/outside-write.stderr",
            'tenant_ctl lease revoke "$lease_id"',
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)
        self.assertNotIn("endpoint health", self.script)

    def test_ready_owner_and_secret_contract_is_checked_without_persisting_data(self) -> None:
        for required in (
            'status.lastAppliedRevision == $revision',
            '.status.observedGeneration == $generation',
            '.status.connected == true and .status.authEnabled == true',
            '(.status.clusterId | test("^[0-9a-f]{16}$"))',
            '.status.version == "3.6.13"',
            '.status.conditions[0].reason == "Connected"',
            '.status.conditions[0].reason == "Provisioned"',
            '.status.externalAccessState == "Provisioned"',
            "Fabric must contain exactly the one qualified physical EtcdCluster",
            "Fabric must contain exactly the one permanent smoke EtcdTenant",
            'etcdUser: $user, etcdRole: $role, prefix: $prefix',
            'credentialMode: "TLS"',
            'controller: true, kind: "EtcdTenant", name: "fabric-smoke", uid: $uid',
            '(.data | keys | sort) == ["ca.crt", "tls.crt", "tls.key", "username"]',
            'client_certificate_revision=sha256:$(',
            'secret_server_ca_revision=sha256:$(',
            'clientCertificateRevision: $client_certificate_revision',
            'externalSecretRevisionsRequired: true',
            "smoke Secret physical server CA differs from the mirrored ConfigMap CA",
            "smoke TLS certificate is not signed by the committed delegated CA",
            "unset secret_json username",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)
        self.assertNotRegex(self.script, r'printf[^\n]*\$secret_json')
        self.assertNotRegex(self.script, r'>[^\n]*secret[^\n]*\.json')

        client_hash = re.search(
            r'client_certificate_revision=sha256:\$\(\n(?P<body>.*?)\n  \)',
            self.script,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(client_hash)
        self.assertIn('.data["tls.crt"]', client_hash.group("body"))
        self.assertNotIn('tls.key', client_hash.group("body"))

    def test_generated_handoff_rejects_missing_or_tampered_rollout_revisions(self) -> None:
        contract = re.search(
            r'(?P<filter>\(\.data\["values\.yaml"\] \| fromjson\) == \{\n'
            r'      etcd: \{.*?\n'
            r'      \}\n'
            r'    \})',
            self.script,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(contract)

        client_revision = "sha256:" + "1" * 64
        server_revision = "sha256:" + "2" * 64
        prefix = "/etcdetcetc-smoke:fabric-smoke/"
        values = {
            "etcd": {
                "clientCertificateRevision": client_revision,
                "clientSecret": {"create": False, "name": "fabric-smoke-etcd"},
                "endpoints": [
                    "https://fabric-az1-cp1.fabric.internal:2379",
                    "https://fabric-az1-cp2.fabric.internal:2379",
                    "https://fabric-az1-cp3.fabric.internal:2379",
                ],
                "externalSecretRevisionsRequired": True,
                "prefix": prefix,
                "serverCATrustRevision": server_revision,
            }
        }

        def accepted(candidate: dict[str, object]) -> bool:
            payload = {"data": {"values.yaml": json.dumps(candidate)}}
            result = subprocess.run(
                [
                    "jq",
                    "-e",
                    "--arg",
                    "client_certificate_revision",
                    client_revision,
                    "--arg",
                    "server_ca_revision",
                    server_revision,
                    "--arg",
                    "prefix",
                    prefix,
                    contract.group("filter"),
                ],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                check=False,
            )
            return result.returncode == 0

        self.assertTrue(accepted(values))
        mutations = {}
        for name in (
            "clientCertificateRevision",
            "externalSecretRevisionsRequired",
            "serverCATrustRevision",
        ):
            candidate = copy.deepcopy(values)
            del candidate["etcd"][name]
            mutations[f"missing {name}"] = candidate

        candidate = copy.deepcopy(values)
        candidate["etcd"]["clientCertificateRevision"] = "sha256:" + "3" * 64
        mutations["tampered client certificate revision"] = candidate
        candidate = copy.deepcopy(values)
        candidate["etcd"]["externalSecretRevisionsRequired"] = False
        mutations["disabled external revision requirement"] = candidate
        candidate = copy.deepcopy(values)
        candidate["etcd"]["unexpectedSafetyBypass"] = True
        mutations["unexpected handoff key"] = candidate

        for label, candidate in mutations.items():
            with self.subTest(label=label):
                self.assertFalse(accepted(candidate))

    def test_signer_admission_is_typechecked_and_exercised_with_dry_runs(self) -> None:
        for required in (
            "validate_admission_contract",
            ".status.observedGeneration == .metadata.generation",
            "(.status.typeChecking.expressionWarnings // []) == []",
            '.spec.failurePolicy == "Fail"',
            '.spec.validationActions == ["Deny"]',
            "--as=system:serviceaccount:flux-system:kustomize-controller",
            "replace --dry-run=server --output=name -f -",
            "create --dry-run=server --output=name -f -",
            'fabric-etcd-client-v1-negative',
            "grep -Fq 'fabric-etcd-client-v1-certificates'",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)
        self.assertEqual(self.script.count("validate_admission_contract\n"), 3)

    def test_jq_pipe_precedence_is_parenthesized(self) -> None:
        self.assertIn(
            '(.metadata.uid | test("^[0-9a-f-]{36}$"))',
            self.script,
        )
        self.assertNotIn(
            '.metadata.deletionTimestamp == null and\n    .metadata.uid | test(',
            self.script,
        )

    def test_router_semantics_counters_and_observer_cleanup_are_proven(self) -> None:
        for required in (
            "normalize_router_fw4",
            "del(.handle)",
            '.counter |= del(.packets, .bytes)',
            'cmp -s -- "$router_before_semantic" "$router_after_semantic"',
            '"require_increase": ["etcd_client_allow", "etcd_internal_reject", "root_metrics_allow"]',
            "allow_delta -ge 6",
            "reject_delta -ge 6",
            "metrics_delta -ge 6",
            "table inet $router_probe_table",
            "type filter hook forward priority -10",
            'router_ssh /usr/sbin/nft delete table inet "$router_probe_table"',
            "assert_router_probe_table_absent",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)

        observer = re.search(
            r"read -r -d '' payload <<EOF \|\| true\n(?P<body>.*?)\nEOF",
            self.script,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(observer)
        body = observer.group("body").lower()
        for verdict in (" accept", " drop", " reject", " queue", " dnat", " snat"):
            with self.subTest(verdict=verdict):
                self.assertNotIn(verdict, body)

    def test_cleanup_is_uid_aware_and_preserves_failed_operation_lock(self) -> None:
        for required in (
            'current_uid=${state#present:}',
            '[[ -n $expected_uid && $current_uid == "$expected_uid" ]]',
            'remove_owned_object pod "$name" "${pod_uids[$index]}"',
            'remove_owned_object networkpolicy "$network_policy_name" "$network_policy_uid"',
            'cmp -s -- "$default_deny_before" "$default_deny_after"',
            "permanent smoke default-deny changed during post-open acceptance",
            "trap '' HUP INT TERM",
            "trap 'exit 129' HUP",
            "trap 'exit 130' INT",
            "trap 'exit 143' TERM",
            "ephemeralCleanup: \"uid-verified\"",
            "namespaceCreated: false",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)

    def test_runbooks_describe_attended_check_run_and_no_copy_boundary(self) -> None:
        combined = self.activation + self.router_runbook
        for required in (
            "scripts/qualify-fabric-etcd-post-open",
            "--serial-ed25519-fingerprint",
            "QUALIFY-FABRIC-ETCD-POST-OPEN",
            "No temporary Namespace",
            "no credential copy",
            "PermissionDenied",
            "short-TTL",
        ):
            with self.subTest(required=required):
                self.assertIn(required, combined)


if __name__ == "__main__":
    unittest.main()
