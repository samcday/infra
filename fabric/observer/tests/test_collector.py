#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import http.client
import http.server
import importlib.util
import json
import pathlib
import sys
import tempfile
import threading
import unittest


COLLECTOR_PATH = pathlib.Path(__file__).parents[1] / "collector.py"
SPEC = importlib.util.spec_from_file_location("fabric_observer_collector", COLLECTOR_PATH)
assert SPEC is not None and SPEC.loader is not None
collector = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = collector
SPEC.loader.exec_module(collector)


def lease_payload(
    *,
    holder: str = "fabric-az1-cp2",
    renew_time: str = "2026-07-13T10:00:00.123456789Z",
    duration: int = 30,
) -> bytes:
    return json.dumps(
        {
            "apiVersion": "coordination.k8s.io/v1",
            "kind": "Lease",
            "metadata": {
                "name": "plndr-cp-lock",
                "namespace": "kube-system",
                "resourceVersion": "42",
            },
            "spec": {
                "holderIdentity": holder,
                "leaseDurationSeconds": duration,
                "leaseTransitions": 7,
                "renewTime": renew_time,
            },
        }
    ).encode()


class LeaseTests(unittest.TestCase):
    def test_valid_lease_is_strict_and_nanoseconds_are_bounded(self) -> None:
        now = dt.datetime(2026, 7, 13, 10, 0, 12, tzinfo=dt.timezone.utc).timestamp()
        parsed = collector.parse_lease(lease_payload(), now)
        self.assertEqual(parsed["holder"], "fabric-az1-cp2")
        self.assertEqual(parsed["transitions"], 7)
        self.assertTrue(parsed["valid"])
        self.assertAlmostEqual(parsed["renew_age"], 11.876544, places=5)

    def test_stale_lease_fails_validity_without_losing_holder_evidence(self) -> None:
        now = dt.datetime(2026, 7, 13, 10, 1, tzinfo=dt.timezone.utc).timestamp()
        parsed = collector.parse_lease(lease_payload(), now)
        self.assertFalse(parsed["valid"])
        self.assertEqual(parsed["holder"], "fabric-az1-cp2")

    def test_unknown_holder_and_duration_fail_closed(self) -> None:
        now = dt.datetime(2026, 7, 13, 10, 0, 12, tzinfo=dt.timezone.utc).timestamp()
        with self.assertRaises(collector.CollectorError):
            collector.parse_lease(lease_payload(holder='bad"holder'), now)
        with self.assertRaises(collector.CollectorError):
            collector.parse_lease(lease_payload(duration=31), now)


class ConfigurationTests(unittest.TestCase):
    def write_config(self, root: pathlib.Path, listen: str = "127.0.0.1") -> pathlib.Path:
        path = root / "config.json"
        path.write_text(
            json.dumps(
                {
                    "interval_seconds": 15,
                    "request_timeout_seconds": 3,
                    "listen_address": listen,
                    "listen_port": 19101,
                    "kubernetes": {
                        "ca_file": str(root / "ca.crt"),
                        "cert_file": str(root / "client.pem"),
                        "key_file": str(root / "client-key.pem"),
                    },
                }
            )
        )
        path.chmod(0o600)
        return path

    def test_non_loopback_listener_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(pathlib.Path(directory), "10.66.0.2")
            with self.assertRaises(collector.CollectorError):
                collector.load_config(path)

    def test_missing_credentials_emit_fixed_zero_series(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            config = collector.load_config(self.write_config(root))
            output = "\n".join(collector.collect_kubernetes(config, 1_700_000_000))
        self.assertEqual(output.count("fabric_observer_kube_api_ready{"), 4)
        self.assertIn("fabric_observer_kube_vip_lease_query_success 0", output)
        self.assertIn("fabric_observer_kube_vip_lease_valid 0", output)
        self.assertIn("fabric_observer_kube_client_certificate_valid 0", output)
        self.assertEqual(
            output.count("fabric_observer_kube_vip_lease_holder{"), 3
        )


class HTTPHandlerTests(unittest.TestCase):
    def test_metrics_body_and_content_length_are_exact(self) -> None:
        payload = b"one_metric 1\nsecond_metric 2\n"
        state = collector.MetricsState()
        state.set(payload)
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), collector.make_handler(state)
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=2
            )
            connection.request("GET", "/metrics")
            response = connection.getresponse()
            body = response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Length"), str(len(payload)))
            self.assertEqual(body, payload)
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
