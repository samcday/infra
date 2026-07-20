#!/usr/bin/env python3

from __future__ import annotations

import pathlib
import re
import unittest


REPO_ROOT = pathlib.Path(__file__).parents[3]
HELPER = REPO_ROOT / "scripts" / "qualify-fabric-service-sources"
CREDENTIAL_HELPER = REPO_ROOT / "scripts" / "fabric-credential"


class ServiceSourceQualificationPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = HELPER.read_text(encoding="utf-8")
        cls.credential_script = CREDENTIAL_HELPER.read_text(encoding="utf-8")

    def test_operator_credential_rotates_before_qualification_floor(self) -> None:
        qualification_floor = re.search(r"checkend (\d+)", self.script)
        rotation_floor = re.search(
            r"minimum_remaining_seconds=(\d+)", self.credential_script
        )
        self.assertIsNotNone(qualification_floor)
        self.assertIsNotNone(rotation_floor)
        self.assertGreaterEqual(
            int(rotation_floor.group(1)), int(qualification_floor.group(1)) + 60
        )
        self.assertIn(
            '-checkend "$minimum_remaining_seconds"', self.credential_script
        )

    def test_probe_identity_and_namespace_are_fixed(self) -> None:
        for required in (
            "readonly namespace=fabric-policy-probe",
            "readonly interface=enp9s0u1u2",
            "readonly interface_serial=00154000F",
            "readonly interface_mac=98:fd:b4:9a:7a:d7",
            "readonly fabric_network_lock=/run/lock/fabric-network-operation.lock",
            "QUALIFY-FABRIC-SERVICE-SOURCES:$interface:$interface_serial:$interface_mac",
            'observer_verify || die \'fabric observer isolation changed before qualification\'',
            "if ((flags & 1)); then",
            "validate_networkmanager_boundary",
            "probe interface must be persistently unmanaged by NetworkManager",
            "Electrical carrier will be required only after the NIC enters its isolated namespace.",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)

        self.assertNotIn("ip link set dev wlp95s0", self.script)
        self.assertNotIn("ip link set dev eno1", self.script)

    def test_sources_routes_and_api_identity_are_exact(self) -> None:
        for required in (
            "readonly -a service_addresses=(10.66.1.10 10.66.1.11)",
            "readonly unauthorized_address=10.66.1.100",
            'ip -n "$namespace" -4 route replace "$root_subnet" via "$service_gateway"',
            "curl --disable",
            "--noproxy '*' --proto '=https' --tlsv1.2",
            '--cacert "$server_ca" --cert "$client_cert" --key "$client_key"',
            '--resolve "api.fabric.internal:6443:$api_address"',
            "'https://api.fabric.internal:6443/readyz?verbose'",
            "[[ $(tail -n 1 \"$output\") == 'readyz check passed' ]]",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)

        self.assertIn("probe_api_rejected", self.script)
        self.assertIn("unexpectedly reached the fabric API", self.script)

    def test_etcd_denial_is_one_syn_per_member_and_port(self) -> None:
        for required in (
            "readonly -a root_addresses=(10.66.0.10 10.66.0.11 10.66.0.12)",
            "readonly -a etcd_ports=(2379 2380 2381)",
            "--tcp --send-ip --flags syn --count 1",
            "Raw packets sent: 1 ",
            "unexpectedly received SYN-ACK from root etcd",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)

    def test_named_router_and_host_guard_counters_are_required(self) -> None:
        for required in (
            "!fw4: Allow-services-to-fabric-API",
            "!fw4: Reject-services-to-root-etcd",
            "!fw4: Reject-other-services-to-roots",
            "service agents to API and root kubelets",
            '[[ $holder_after == "$holder_before" ]]',
            '"$repo_root/scripts/compare-fabric-nft-counters"',
            "require_counter_packet_delta 9 etcd_reject",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)

    def test_cleanup_restores_only_the_dedicated_interface(self) -> None:
        for required in (
            "trap cleanup EXIT",
            'ip netns exec "$namespace" ip link set dev "$interface" netns 1',
            'ip netns delete "$namespace"',
            '[[ ${root_default_after:-} == "$root_default_before" ]]',
            'observer_verify >"$evidence_dir/restoration-observer.txt"',
            "restore_interface_state",
            'initial_interface_sysctls[$key]=$value',
            "write_interface_baseline",
            "trap '' INT TERM",
            "timeout --kill-after=2s",
            "This is a narrow pre-install gate, not the complete attended source-role",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)


if __name__ == "__main__":
    unittest.main()
