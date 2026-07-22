#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import hashlib
import pathlib
import re
import ssl
import unittest


REPO = pathlib.Path(__file__).resolve().parents[3]
CA = REPO / "fabric" / "pki" / "etcdetcetc" / "client-ca.pem"
ALERTS = REPO / "fabric" / "observer" / "alerts.yml"


class DelegatedClientCAAlertTests(unittest.TestCase):
    def test_recording_rule_is_bound_to_the_committed_public_ca(self) -> None:
        certificate = ssl._ssl._test_decode_cert(str(CA))  # type: ignore[attr-defined]
        not_after = dt.datetime.strptime(
            certificate["notAfter"], "%b %d %H:%M:%S %Y %Z"
        ).replace(tzinfo=dt.timezone.utc)
        not_after_epoch = int(not_after.timestamp())

        der = ssl.PEM_cert_to_DER_cert(CA.read_text(encoding="ascii"))
        fingerprint = hashlib.sha256(der).hexdigest()
        alerts = ALERTS.read_text(encoding="utf-8")

        self.assertIn(f"expr: vector({not_after_epoch})", alerts)
        self.assertIn(f"fingerprint_sha256: {fingerprint}", alerts)
        self.assertIn("generation: v1", alerts)
        self.assertIn("lifecycle: active", alerts)

    def test_alert_windows_are_exact_and_non_overlapping(self) -> None:
        alerts = ALERTS.read_text(encoding="utf-8")
        compact = " ".join(alerts.split())
        for alert in (
            "FabricEtcdDelegatedClientCAExpiresWithin90Days",
            "FabricEtcdDelegatedClientCAExpiresWithin30Days",
            "FabricEtcdDelegatedClientCAExpiresWithin7Days",
        ):
            self.assertEqual(alerts.count(f"alert: {alert}"), 1)

        self.assertRegex(
            compact,
            re.compile(
                r"ExpiresWithin90Days.*?< 7776000.*?>= 2592000.*?for: 1h"
            ),
        )
        self.assertRegex(
            compact,
            re.compile(
                r"ExpiresWithin30Days.*?< 2592000.*?>= 604800.*?for: 15m"
            ),
        )
        self.assertRegex(
            compact,
            re.compile(r"ExpiresWithin7Days.*?< 604800.*?for: 5m"),
        )


if __name__ == "__main__":
    unittest.main()
