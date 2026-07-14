#!/usr/bin/env python3

from __future__ import annotations

import pathlib
import re
import unittest


REPO_ROOT = pathlib.Path(__file__).parents[3]
VERIFIER = REPO_ROOT / "scripts" / "verify-fabric-router"


class RouterVerifierWirelessWanPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = VERIFIER.read_text(encoding="utf-8")

    def test_upstream_secrets_are_never_queried(self) -> None:
        for forbidden in (
            "wireless.fabric_upstream.ssid",
            "wireless.fabric_upstream.key",
            "uci -q show wireless",
            'iw dev "$wan_l3_device"',
            "ifstatus wan",
            "LISTENERS_BEGIN",
            "assert_uci wireless.fabric_operator.ssid",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.script)

        # UBus may expose the full lease, so every invocation must immediately
        # select one non-sensitive field instead of writing raw status output.
        wan_status_uses = re.findall(
            r"ubus call network\.interface\.wan status \|\n"
            r"\s+jsonfilter -e '([^']+)'",
            self.script,
        )
        self.assertEqual(wan_status_uses, ["@.up", "@.l3_device"])

    def test_wan_is_wireless_only_and_physical_port_is_unassigned(self) -> None:
        for required in (
            "assert_uci network.wan.proto dhcp",
            "assert_uci_absent network.wan.device",
            "assert_uci network.wan6.disabled 1",
            "assert_uci_absent network.wan6.device",
            '[ -e "/sys/class/net/$wan_l3_device/phy80211" ]',
            "ip -o -4 route show default dev \"$wan_l3_device\"",
            "[ ! -e /sys/class/net/eth0/master ]",
            '"$(cat /sys/class/net/eth0/carrier)" = 0',
            "ip -o -4 address show dev eth0",
            "ip -o -6 address show dev eth0",
            "ip -o -4 route show table all dev eth0",
            "ip -o -6 route show table all dev eth0",
            "NETWORK_TOPOLOGY=br-lan:eth1-plus-one-operator-ap,"
            "wan:2g-managed-sta,eth0:unassigned",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)

    def test_two_radio_policy_uses_sae_and_required_mfp(self) -> None:
        for required in (
            'assert_uci "wireless.$operator_radio.band" 5g',
            'assert_uci "wireless.$upstream_radio.band" 2g',
            'assert_uci "wireless.$upstream_radio.channel" auto',
            'assert_uci "wireless.$upstream_radio.htmode" HE20',
            "assert_uci wireless.fabric_upstream.network wan",
            "assert_uci wireless.fabric_upstream.mode sta",
            "assert_uci wireless.fabric_upstream.encryption sae",
            "assert_uci wireless.fabric_upstream.ieee80211w 2",
            "assert_uci wireless.fabric_upstream.sae_pwe 2",
            "assert_uci wireless.fabric_upstream.disabled 0",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.script)

        self.assertIn("wpad-basic-mbedtls", self.script)
        self.assertIn("command -v wpa_supplicant", self.script)


if __name__ == "__main__":
    unittest.main()
