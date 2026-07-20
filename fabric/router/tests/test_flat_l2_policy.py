#!/usr/bin/env python3

from __future__ import annotations

import ipaddress
import pathlib
import re
import unittest
from collections.abc import Iterable


ROUTER_ROOT = pathlib.Path(__file__).parents[1]
REPO_ROOT = pathlib.Path(__file__).parents[3]
VERIFIER = REPO_ROOT / "scripts/verify-fabric-router"
SYSTEM_DEFAULT = ROUTER_ROOT / "files/etc/uci-defaults/10-system"
FIREWALL_DEFAULT = ROUTER_ROOT / "files/etc/uci-defaults/30-isolation"
TRANSITION_CONFIG = ROUTER_ROOT / "files/etc/config/fabric"
SYSCTL_CONFIG = ROUTER_ROOT / "files/etc/sysctl.d/90-fabric-flat-l2.conf"
STORAGE_DEFAULT = ROUTER_ROOT / "files/etc/uci-defaults/storage"
COMMON_DNS_DEFAULT = REPO_ROOT / "common/router/files/etc/uci-defaults/dns"


def parse_firewall_rules() -> list[tuple[str, dict[str, list[str]]]]:
    section_order: list[str] = []
    sections: dict[str, dict[str, list[str]]] = {}
    command = re.compile(
        r"^(set|add_list) firewall\.([a-zA-Z0-9_]+)(?:\.([a-zA-Z0-9_]+))?='([^']*)'$"
    )
    for raw_line in FIREWALL_DEFAULT.read_text(encoding="utf-8").splitlines():
        match = command.fullmatch(raw_line.strip())
        if match is None:
            continue
        operation, section, option, value = match.groups()
        if option is None:
            if operation == "set" and value == "rule":
                if section in sections:
                    raise AssertionError(f"duplicate firewall rule section: {section}")
                sections[section] = {}
                section_order.append(section)
            continue
        if section not in sections:
            continue
        if operation == "set":
            sections[section][option] = [value]
        else:
            sections[section].setdefault(option, []).append(value)
    return [(name, sections[name]) for name in section_order]


def tokens(values: Iterable[str]) -> list[str]:
    return [token for value in values for token in value.split()]


def ip_matches(address: str, configured: list[str] | None) -> bool:
    if configured is None:
        return True
    candidate = ipaddress.ip_address(address)
    return any(candidate in ipaddress.ip_network(network) for network in configured)


class FirewallModel:
    def __init__(self) -> None:
        self.rules = parse_firewall_rules()

    def decide(
        self,
        *,
        src_zone: str,
        dest_zone: str | None,
        src_ip: str,
        dest_ip: str,
        proto: str,
        dest_port: int,
    ) -> tuple[str, str]:
        for name, options in self.rules:
            if tokens(options.get("src", [])) != [src_zone]:
                continue
            configured_dest = tokens(options.get("dest", []))
            if dest_zone is None:
                if configured_dest:
                    continue
            elif configured_dest != [dest_zone]:
                continue
            if not ip_matches(src_ip, options.get("src_ip")):
                continue
            if not ip_matches(dest_ip, options.get("dest_ip")):
                continue
            configured_protocols = tokens(options.get("proto", ["all"]))
            if "all" not in configured_protocols and proto not in configured_protocols:
                continue
            configured_ports = tokens(options.get("dest_port", []))
            if configured_ports and str(dest_port) not in configured_ports:
                continue
            return tokens(options["target"])[0], name
        # Both LAN and WAN zone forwarding/input policies are REJECT.
        return "REJECT", "zone-default"


class FlatL2NetworkShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.system = SYSTEM_DEFAULT.read_text(encoding="utf-8")
        cls.firewall = FIREWALL_DEFAULT.read_text(encoding="utf-8")

    def test_migration_flag_is_explicit_and_removable(self) -> None:
        self.assertEqual(
            TRANSITION_CONFIG.read_text(encoding="utf-8").splitlines(),
            [
                "config migration 'flat_l2'",
                "\toption enabled '1'",
                "\toption profile 'unmanaged-switch-flat-l2'",
                "\toption physical_device 'eth1'",
                "\toption root_cidr '10.66.0.0/24'",
                "\toption service_cidr '10.66.1.0/24'",
                "\toption removal_gate 'managed-vlan-trunk-ready'",
            ],
        )
        for required in (
            "uci -q get fabric.flat_l2.enabled",
            "unmanaged-switch-flat-l2",
            "managed-vlan-trunk-ready",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.firewall)

    def test_preserved_network_is_rebuilt_as_one_exact_lan(self) -> None:
        for required in (
            'network_section=$(network_sections | sed -n \'1p\')',
            'uci -q delete "network.$network_section"',
            "set network.loopback='interface'",
            "set network.globals='globals'",
            "set network.globals.packet_steering='1'",
            "set network.lan='interface'",
            "set network.lan.device='br-lan'",
            "set network.lan.proto='static'",
            "add_list network.lan.ipaddr='10.66.0.1/24'",
            "add_list network.lan.ipaddr='10.66.1.1/24'",
            "set network.wan='interface'",
            "set network.wan.proto='dhcp'",
            "set network.wan6='interface'",
            "set network.wan6.proto='dhcpv6'",
            "set network.wan6.disabled='1'",
            "set network.br_lan='device'",
            "set network.br_lan.name='br-lan'",
            "add_list network.br_lan.ports='eth1'",
            "expected_network_sections='loopback\nglobals\nlan\nwan\nwan6\nbr_lan'",
            "assert_network_option_keys br_lan 'name\nports\ntype'",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.system)
        self.assertNotRegex(self.system, r"set network\.[^.]+\.device='eth0'")
        self.assertNotIn("set network.services=", self.system)
        self.assertNotIn("ula_prefix", self.system)

    def test_lan_addressing_services_and_ipv6_are_fail_closed(self) -> None:
        for required in (
            "set dhcp.lan.ignore='1'",
            "set dhcp.lan.dhcpv4='disabled'",
            "set dhcp.lan.dhcpv6='disabled'",
            "set dhcp.lan.ra='disabled'",
            "set dhcp.lan.ndp='disabled'",
            "delete dhcp.lan.ra_slaac",
            "delete dhcp.lan.ra_flags",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.firewall)

        sysctls = {}
        for line in SYSCTL_CONFIG.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                key, value = line.split("=", 1)
                sysctls[key] = value
        for key in (
            "net.ipv4.ip_forward",
            "net.ipv4.conf.all.send_redirects",
            "net.ipv4.conf.default.send_redirects",
            "net.ipv4.conf.all.accept_redirects",
            "net.ipv4.conf.default.accept_redirects",
            "net.ipv6.conf.all.disable_ipv6",
            "net.ipv6.conf.default.disable_ipv6",
        ):
            with self.subTest(key=key):
                self.assertEqual(sysctls.get(key), "0" if "redirect" in key else "1")
        self.assertEqual(sysctls.get("net.ipv6.conf.all.accept_source_route"), "-1")
        self.assertEqual(sysctls.get("net.ipv6.conf.default.accept_source_route"), "-1")
        self.assertIn("flat_l2_sysctl=/etc/sysctl.d/90-fabric-flat-l2.conf", self.system)
        self.assertIn("preserved /etc/sysctl.conf overrides", self.system)
        self.assertIn('sysctl -e -p "$flat_l2_sysctl"', self.system)

    def test_combined_overlay_has_exact_static_upstream_dns(self) -> None:
        common_dns = COMMON_DNS_DEFAULT.read_text(encoding="utf-8")
        verifier = VERIFIER.read_text(encoding="utf-8")
        for required in (
            'set network.wan.peerdns="0"',
            'add_list network.wan.dns="1.1.1.1"',
            'add_list network.wan.dns="1.0.0.1"',
            'set network.wan6.peerdns="0"',
            'add_list network.wan6.dns="2606:4700:4700::1111"',
            'add_list network.wan6.dns="2606:4700:4700::1001"',
        ):
            with self.subTest(required=required):
                self.assertIn(required, common_dns)
        for required in (
            "assert_uci network.wan.dns '1.1.1.1 1.0.0.1'",
            "assert_network_option_keys wan 'dns peerdns proto'",
            "assert_uci network.wan6.dns '2606:4700:4700::1111 2606:4700:4700::1001'",
            "assert_network_option_keys wan6 'disabled dns peerdns proto'",
        ):
            with self.subTest(required=required):
                self.assertIn(required, verifier)

    def test_checksum_pinned_assets_are_reachable_on_both_router_addresses(self) -> None:
        self.assertIn("add_list uhttpd.main.listen_http='10.66.0.1:80'", self.system)
        self.assertIn("add_list uhttpd.main.listen_http='10.66.1.1:80'", self.system)
        storage = STORAGE_DEFAULT.read_text(encoding="utf-8")
        self.assertIn("set fstab.mount_storage.options='ro,nodev,nosuid,noexec,noatime'", storage)

        records = [
            paragraph.splitlines()
            for paragraph in (ROUTER_ROOT / "data-files.txt")
            .read_text(encoding="utf-8")
            .strip()
            .split("\n\n")
        ]
        self.assertGreater(len(records), 0)
        self.assertEqual(len({record[0] for record in records}), len(records))
        for record in records:
            with self.subTest(record=record[0]):
                self.assertEqual(len(record), 3)
                self.assertRegex(record[2], r"^[0-9a-f]{64}$")

    def test_commissioning_verifier_tracks_the_transitional_profile(self) -> None:
        verifier = VERIFIER.read_text(encoding="utf-8")
        for required in (
            "assert_uci fabric.flat_l2.profile unmanaged-switch-flat-l2",
            "expected_network_sections='loopback\nglobals\nlan\nwan\nwan6\nbr_lan'",
            "assert_network_option_keys loopback 'device ipaddr netmask proto'",
            "assert_network_option_keys globals 'dhcp_default_duid packet_steering'",
            "DHCP_DEFAULT_DUID=duid-uuid",
            "assert_network_option_keys br_lan 'name ports type'",
            "assert_uci network.lan.ipaddr '10.66.0.1/24 10.66.1.1/24'",
            "expected_lan_addresses='10.66.0.1/24\n10.66.1.1/24'",
            "flat_l2_sysctl=/etc/sysctl.d/90-fabric-flat-l2.conf",
            "net.ipv6.conf.all.accept_source_route=-1",
            "SYSCTL_POLICY=image-owned-no-preserved-overrides-live-exact",
            "assert_section_count rule 25",
            "assert_live_fw4_chain forward_lan 15",
            "assert_live_fw4_chain input_lan 11",
            "Allow-observer-to-services-SSH",
            "Allow-services-to-Bootie-rootfs",
            "Reject-services-to-root-etcd",
            "api_endpoints='10.66.0.254/32 10.66.0.10/32 10.66.0.11/32 10.66.0.12/32'",
            "assert_uci uhttpd.main.listen_http '10.66.0.1:80 10.66.1.1:80'",
            "http://10.66.1.1/static/SHASUMS.txt",
        ):
            with self.subTest(required=required):
                self.assertIn(required, verifier)
        self.assertNotIn("assert_uci network.lan.ipaddr 10.66.0.1\n", verifier)


class FlatL2FirewallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.model = FirewallModel()
        cls.rules = dict(cls.model.rules)
        cls.firewall = FIREWALL_DEFAULT.read_text(encoding="utf-8")

    def assert_flow(
        self,
        expected_target: str,
        expected_rule: str,
        **flow: object,
    ) -> None:
        self.assertEqual(
            self.model.decide(**flow),  # type: ignore[arg-type]
            (expected_target, expected_rule),
        )

    def routed_flow(
        self,
        src: str,
        dest: str,
        proto: str,
        port: int,
        dest_zone: str = "lan",
    ) -> dict[str, object]:
        return {
            "src_zone": "lan",
            "dest_zone": dest_zone,
            "src_ip": src,
            "dest_ip": dest,
            "proto": proto,
            "dest_port": port,
        }

    def router_flow(self, src: str, dest: str, proto: str, port: int) -> dict[str, object]:
        return {
            "src_zone": "lan",
            "dest_zone": None,
            "src_ip": src,
            "dest_ip": dest,
            "proto": proto,
            "dest_port": port,
        }

    def test_only_exact_k3s_cross_prefix_flows_are_accepted(self) -> None:
        for service in ("10.66.1.10", "10.66.1.11"):
            for endpoint in (
                "10.66.0.254",
                "10.66.0.10",
                "10.66.0.11",
                "10.66.0.12",
            ):
                self.assert_flow(
                    "ACCEPT",
                    "fabric_flat_services_api",
                    **self.routed_flow(service, endpoint, "tcp", 6443),
                )
            for root in ("10.66.0.10", "10.66.0.11", "10.66.0.12"):
                self.assert_flow(
                    "ACCEPT",
                    "fabric_flat_services_vxlan",
                    **self.routed_flow(service, root, "udp", 8472),
                )
                self.assert_flow(
                    "ACCEPT",
                    "fabric_flat_services_kubelet",
                    **self.routed_flow(service, root, "tcp", 10250),
                )
                self.assert_flow(
                    "ACCEPT",
                    "fabric_flat_roots_vxlan",
                    **self.routed_flow(root, service, "udp", 8472),
                )
                self.assert_flow(
                    "ACCEPT",
                    "fabric_flat_roots_kubelet",
                    **self.routed_flow(root, service, "tcp", 10250),
                )

    def test_observer_has_only_attended_ssh_to_exact_service_nodes(self) -> None:
        for service in ("10.66.1.10", "10.66.1.11"):
            self.assert_flow(
                "ACCEPT",
                "fabric_flat_observer_services_ssh",
                **self.routed_flow("10.66.0.2", service, "tcp", 22),
            )
            self.assert_flow(
                "REJECT",
                "fabric_flat_reject_roots_services",
                **self.routed_flow("10.66.0.2", service, "tcp", 10250),
            )
        self.assert_flow(
            "REJECT",
            "fabric_flat_reject_roots_services",
            **self.routed_flow("10.66.0.2", "10.66.1.12", "tcp", 22),
        )

    def test_only_service_nodes_can_fetch_bootie_rootfs_after_readdressing(self) -> None:
        for service in ("10.66.1.10", "10.66.1.11"):
            self.assert_flow(
                "ACCEPT",
                "fabric_flat_services_bootie_rootfs",
                **self.routed_flow(service, "10.66.0.2", "tcp", 80),
            )
            for destination, protocol, port in (
                ("10.66.0.2", "tcp", 443),
                ("10.66.0.2", "udp", 80),
                ("10.66.0.3", "tcp", 80),
            ):
                self.assert_flow(
                    "REJECT",
                    "fabric_flat_reject_services_roots",
                    **self.routed_flow(service, destination, protocol, port),
                )
        self.assert_flow(
            "REJECT",
            "fabric_flat_reject_services_roots",
            **self.routed_flow("10.66.1.12", "10.66.0.2", "tcp", 80),
        )

    def test_etcd_and_every_other_cross_prefix_flow_are_rejected(self) -> None:
        for port in (2379, 2380, 2381):
            self.assert_flow(
                "REJECT",
                "fabric_flat_reject_services_etcd",
                **self.routed_flow("10.66.1.10", "10.66.0.10", "tcp", port),
            )
        for flow, rule in (
            (self.routed_flow("10.66.1.10", "10.66.0.10", "tcp", 22),
             "fabric_flat_reject_services_roots"),
            (self.routed_flow("10.66.1.10", "10.66.0.11", "tcp", 10257),
             "fabric_flat_reject_services_roots"),
            (self.routed_flow("10.66.0.10", "10.66.1.10", "tcp", 6443),
             "fabric_flat_reject_roots_services"),
            (self.routed_flow("10.66.1.12", "10.66.0.10", "udp", 8472),
             "fabric_flat_reject_services_roots"),
            (self.routed_flow("10.66.1.12", "10.66.0.10", "tcp", 6443),
             "fabric_flat_reject_services_roots"),
            (self.routed_flow("10.66.1.10", "10.66.0.13", "tcp", 6443),
             "fabric_flat_reject_services_roots"),
        ):
            with self.subTest(flow=flow):
                self.assert_flow("REJECT", rule, **flow)

    def test_services_have_only_pinned_public_web_egress(self) -> None:
        for service in ("10.66.1.10", "10.66.1.11"):
            for port in (80, 443):
                self.assert_flow(
                    "ACCEPT",
                    "fabric_services_egress",
                    **self.routed_flow(service, "1.1.1.1", "tcp", port, "wan"),
                )
            for proto, port in (("udp", 53), ("tcp", 22), ("icmp", 0)):
                self.assert_flow(
                    "REJECT",
                    "fabric_reject_other_fabric_to_wan",
                    **self.routed_flow(service, "1.1.1.1", proto, port, "wan"),
                )
            self.assert_flow(
                "REJECT",
                "fabric_block_private",
                **self.routed_flow(service, "192.168.1.1", "tcp", 443, "wan"),
            )
        self.assert_flow(
            "REJECT",
            "fabric_reject_other_fabric_to_wan",
            **self.routed_flow("10.66.1.12", "1.1.1.1", "tcp", 443, "wan"),
        )

    def test_router_services_are_address_and_source_pinned(self) -> None:
        for service in ("10.66.1.10", "10.66.1.11"):
            for proto in ("tcp", "udp"):
                self.assert_flow(
                    "ACCEPT",
                    "fabric_router_services_dns",
                    **self.router_flow(service, "10.66.1.1", proto, 53),
                )
            self.assert_flow(
                "ACCEPT",
                "fabric_router_services_ntp",
                **self.router_flow(service, "10.66.1.1", "udp", 123),
            )
            self.assert_flow(
                "ACCEPT",
                "fabric_router_services_http",
                **self.router_flow(service, "10.66.1.1", "tcp", 80),
            )
            self.assert_flow(
                "REJECT",
                "fabric_reject_other_fabric_router_input",
                **self.router_flow(service, "10.66.0.1", "tcp", 80),
            )
            self.assert_flow(
                "REJECT",
                "fabric_reject_other_fabric_router_input",
                **self.router_flow(service, "10.66.1.1", "tcp", 22),
            )

    def test_no_broad_forwarding_or_redirect_section_survives(self) -> None:
        for forbidden in ("forwarding", "redirect", "nat", "include"):
            self.assertNotRegex(
                self.firewall,
                rf"(?m)^set firewall\.[^=]+='{forbidden}'$",
            )
        self.assertIn("set firewall.@defaults[0].forward='REJECT'", self.firewall)
        self.assertIn("set firewall.${lan_zone}.forward='REJECT'", self.firewall)
        self.assertIn("set firewall.${wan_zone}.forward='REJECT'", self.firewall)

        forwarding_accepts = {
            name: options
            for name, options in self.model.rules
            if options.get("target") == ["ACCEPT"] and "dest" in options
        }
        self.assertEqual(
            set(forwarding_accepts),
            {
                "fabric_root_egress",
                "fabric_services_egress",
                "fabric_flat_services_api",
                "fabric_flat_services_vxlan",
                "fabric_flat_roots_vxlan",
                "fabric_flat_services_kubelet",
                "fabric_flat_roots_kubelet",
                "fabric_flat_observer_services_ssh",
                "fabric_flat_services_bootie_rootfs",
            },
        )
        for name, options in forwarding_accepts.items():
            with self.subTest(name=name):
                self.assertEqual(options.get("family"), ["ipv4"])
                self.assertTrue(options.get("src_ip"))
                if options["dest"] == ["lan"]:
                    self.assertTrue(options.get("dest_ip"))
                    self.assertNotIn("all", tokens(options.get("proto", [])))
                    self.assertTrue(options.get("dest_port"))


if __name__ == "__main__":
    unittest.main()
