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
from unittest import mock


COLLECTOR_PATH = pathlib.Path(__file__).parents[1] / "collector.py"
ACCESS_PATH = pathlib.Path(__file__).parents[1] / "access.yaml"
PROVISION_PATH = pathlib.Path(__file__).parents[1] / "provision-kubernetes-access"
ROTATION_PATH = pathlib.Path(__file__).parents[2] / "pki" / "etcdetcetc" / "ROTATION.md"
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


def certificate_item(
    *,
    tenant_uid: str | None = None,
    issuer_name: str = "fabric-etcd-client-v1",
) -> dict[str, object]:
    admin = tenant_uid is None
    name = (
        "fabric-etcdetcetc-admin" if admin else f"etcdtenant-{tenant_uid}"
    )
    common_name = "fabric-etcdetcetc" if admin else f"etcdtenant:{tenant_uid}"
    secret_name = name if admin else f"{name}-tls"
    metadata: dict[str, object] = {
        "name": name,
        "namespace": "etcdetcetc",
        "uid": "99999999-9999-4999-8999-999999999999",
        "generation": 3,
    }
    if not admin:
        metadata["labels"] = {
            "etcdetcetc.samcday.com/tenant-uid": tenant_uid,
            "etcdetcetc.samcday.com/tenant-name": "api",
            "etcdetcetc.samcday.com/tenant-namespace": "child-api",
        }
        metadata["ownerReferences"] = [
            {
                "apiVersion": "etcdetcetc.samcday.com/v1alpha1",
                "kind": "EtcdCluster",
                "name": "fabric-etcd",
                "uid": "88888888-8888-4888-8888-888888888888",
                "controller": True,
            }
        ]
    return {
        "apiVersion": "cert-manager.io/v1",
        "kind": "Certificate",
        "metadata": metadata,
        "spec": {
            "commonName": common_name,
            "duration": "24h0m0s",
            "issuerRef": {
                "group": "cert-manager.io",
                "kind": "ClusterIssuer",
                "name": issuer_name,
            },
            "privateKey": {
                "algorithm": "ECDSA",
                "rotationPolicy": "Always",
            },
            "renewBefore": "8h0m0s",
            "secretName": secret_name,
            "usages": ["digital signature", "client auth"],
        },
        "status": {
            "conditions": [
                {
                    "observedGeneration": 3,
                    "status": "True",
                    "type": "Ready",
                }
            ],
            "notAfter": "2026-07-14T10:00:00Z",
            "renewalTime": "2026-07-14T02:00:00Z",
            "revision": 7,
        },
    }


def certificate_list_payload(*items: dict[str, object]) -> bytes:
    return json.dumps(
        {
            "apiVersion": "cert-manager.io/v1",
            "kind": "CertificateList",
            "metadata": {"resourceVersion": "12345"},
            "items": list(items),
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


class EtcdClientCertificateTests(unittest.TestCase):
    TENANT_UID = "11111111-1111-4111-8111-111111111111"

    def test_admin_and_tenant_inventory_exposes_current_renewal_state(self) -> None:
        parsed = collector.parse_etcd_certificate_list(
            certificate_list_payload(
                certificate_item(), certificate_item(tenant_uid=self.TENANT_UID)
            )
        )
        self.assertEqual(len(parsed), 2)
        admin = next(item for item in parsed if item["credential"] == "admin")
        tenant = next(item for item in parsed if item["credential"] == "tenant")
        self.assertTrue(admin["ready"])
        self.assertTrue(admin["status_valid"])
        self.assertEqual(admin["revision"], 7)
        self.assertEqual(tenant["tenant_uid"], self.TENANT_UID)
        self.assertEqual(tenant["tenant_namespace"], "child-api")
        self.assertEqual(
            tenant["not_after"] - tenant["renewal_time"], 8 * 60 * 60
        )

    def test_stale_observed_generation_fails_status_closed(self) -> None:
        admin = certificate_item()
        admin["status"]["conditions"][0]["observedGeneration"] = 2  # type: ignore[index]
        [parsed] = collector.parse_etcd_certificate_list(
            certificate_list_payload(admin)
        )
        self.assertFalse(parsed["ready"])
        self.assertFalse(parsed["status_valid"])
        self.assertNotIn("not_after", parsed)

    def test_bounded_api_inventory_renders_actionable_metrics(self) -> None:
        payload = certificate_list_payload(
            certificate_item(), certificate_item(tenant_uid=self.TENANT_UID)
        )
        with mock.patch.object(
            collector,
            "_api_get",
            return_value=(200, "application/json", payload),
        ) as api_get:
            output = "\n".join(
                collector.collect_etcd_certificate_metrics(object(), True, 3)
            )
        api_get.assert_called_once_with(
            "10.66.0.254",
            6443,
            collector.ETCD_CERTIFICATES_PATH,
            mock.ANY,
            3,
            collector.MAX_CERTIFICATE_LIST_BODY,
        )
        self.assertIn(
            "fabric_observer_etcd_client_certificate_query_success 1", output
        )
        self.assertIn(
            "fabric_observer_etcd_client_certificate_inventory_valid 1", output
        )
        self.assertIn('credential="admin"', output)
        self.assertIn(f'tenant_uid="{self.TENANT_UID}"', output)
        self.assertIn(
            "fabric_observer_etcd_client_certificate_renewal_timestamp_seconds",
            output,
        )

    def test_inventory_rejects_missing_admin_and_unexpected_leaf_contract(self) -> None:
        with self.assertRaises(collector.CollectorError):
            collector.parse_etcd_certificate_list(
                certificate_list_payload(certificate_item(tenant_uid=self.TENANT_UID))
            )
        admin = certificate_item()
        admin["spec"]["renewBefore"] = "1h"  # type: ignore[index]
        with self.assertRaises(collector.CollectorError):
            collector.parse_etcd_certificate_list(certificate_list_payload(admin))

    def test_unreviewed_version_shaped_issuer_is_rejected_by_default(self) -> None:
        self.assertEqual(
            collector.ETCD_ISSUER_ALLOWLIST,
            frozenset({"fabric-etcd-client-v1"}),
        )
        with self.assertRaises(collector.CollectorError):
            collector.parse_etcd_certificate_list(
                certificate_list_payload(
                    certificate_item(issuer_name="fabric-etcd-client-v2")
                )
            )

    def test_terminating_admin_or_tenant_certificate_invalidates_inventory(self) -> None:
        for item in (
            certificate_item(),
            certificate_item(tenant_uid=self.TENANT_UID),
        ):
            item["metadata"]["deletionTimestamp"] = "2026-07-14T01:00:00Z"  # type: ignore[index]
            with self.subTest(certificate=item["metadata"]["name"]):  # type: ignore[index]
                with self.assertRaises(collector.CollectorError):
                    collector.parse_etcd_certificate_list(
                        certificate_list_payload(certificate_item(), item)
                    )

    def test_inventory_rejects_pagination_and_duplicates(self) -> None:
        document = json.loads(certificate_list_payload(certificate_item()))
        document["metadata"]["continue"] = "opaque"
        with self.assertRaises(collector.CollectorError):
            collector.parse_etcd_certificate_list(json.dumps(document).encode())
        admin = certificate_item()
        with self.assertRaises(collector.CollectorError):
            collector.parse_etcd_certificate_list(
                certificate_list_payload(admin, admin)
            )


class CertificateObserverAccessTests(unittest.TestCase):
    def test_certificate_rbac_is_namespace_bound_list_only(self) -> None:
        documents = ACCESS_PATH.read_text(encoding="utf-8").split("\n---\n")
        certificate_role = next(
            document
            for document in documents
            if "name: fabric-observer-etcd-client-certificates" in document
        )
        self.assertIn("kind: ClusterRole", certificate_role)
        self.assertIn("      - certificates", certificate_role)
        self.assertIn("      - list", certificate_role)
        for forbidden in ("secrets", "certificaterequests", "      - get", "      - watch"):
            self.assertNotIn(forbidden, certificate_role)

        provisioner = PROVISION_PATH.read_text(encoding="utf-8")
        self.assertIn('namespace: "etcdetcetc"', provisioner)
        self.assertIn(
            'name: "fabric-observer-etcd-client-certificates"', provisioner
        )
        self.assertIn(
            'delete rolebinding "$role_binding" --namespace etcdetcetc',
            provisioner,
        )
        self.assertIn("deny_can_i 'read etcdetcetc Secrets'", provisioner)

    def test_rotation_phases_pin_the_exact_issuer_allowlist_lifecycle(self) -> None:
        rotation = ROTATION_PATH.read_text(encoding="utf-8")
        self.assertIn("R1 changes it to exact current plus", rotation)
        self.assertIn("R5 removes current only", rotation)
        self.assertIn("R6 removes that retiring", rotation)
        collector_source = COLLECTOR_PATH.read_text(encoding="utf-8")
        self.assertNotIn("VERSIONED_ETCD_ISSUER_RE", collector_source)


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
        self.assertIn(
            "fabric_observer_etcd_client_certificate_query_success 0", output
        )
        self.assertIn(
            "fabric_observer_etcd_client_certificate_inventory_valid 0", output
        )
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
