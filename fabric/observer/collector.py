#!/usr/bin/env python3
"""Bounded, loopback-only observer for the pre-worker fabric root cluster."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import http.client
import http.server
import ipaddress
import json
import os
import pathlib
import re
import signal
import ssl
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any


VERSION = "2"
API_ENDPOINTS = (
    ("vip", "api-vip", "", "10.66.0.254", 6443),
    ("node", "fabric-az1-cp1", "fabric-az1-cp1", "10.66.0.10", 6443),
    ("node", "fabric-az1-cp2", "fabric-az1-cp2", "10.66.0.11", 6443),
    ("node", "fabric-az1-cp3", "fabric-az1-cp3", "10.66.0.12", 6443),
)
ROOT_NODES = tuple(item[2] for item in API_ENDPOINTS if item[0] == "node")
LEASE_PATH = (
    "/apis/coordination.k8s.io/v1/namespaces/kube-system/"
    "leases/plndr-cp-lock"
)
ETCD_CERTIFICATE_NAMESPACE = "etcdetcetc"
ETCD_CERTIFICATES_PATH = (
    "/apis/cert-manager.io/v1/namespaces/etcdetcetc/certificates?limit=250"
)
READY_PATH = "/readyz"
MAX_HTTP_BODY = 65536
MAX_CERTIFICATE_LIST_BODY = 2 * 1024 * 1024
MAX_ETCD_CERTIFICATES = 250
RFC3339_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?P<fraction>\.\d+)?Z$"
)
TENANT_UID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
# This is a reviewed phase allowlist, not a naming-pattern allowlist. R1 adds
# exactly the reviewed next issuer; R5 removes current after the old-reference
# inventory is empty. Arbitrary version-shaped names must stay rejected.
ETCD_ISSUER_ALLOWLIST = frozenset({"fabric-etcd-client-v1"})


class CollectorError(RuntimeError):
    """A bounded collector operation failed validation."""


@dataclass(frozen=True)
class Config:
    interval_seconds: int
    request_timeout_seconds: int
    listen_address: str
    listen_port: int
    kube_ca_file: pathlib.Path
    kube_cert_file: pathlib.Path
    kube_key_file: pathlib.Path


def _exact_keys(value: Any, expected: set[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CollectorError(f"{where} must be an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise CollectorError(
            f"{where} has unexpected keys (missing={missing}, extra={extra})"
        )
    return value


def _bounded_int(value: Any, minimum: int, maximum: int, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CollectorError(f"{where} must be an integer")
    if not minimum <= value <= maximum:
        raise CollectorError(f"{where} must be between {minimum} and {maximum}")
    return value


def _path(value: Any, where: str) -> pathlib.Path:
    if not isinstance(value, str) or not value.startswith("/"):
        raise CollectorError(f"{where} must be an absolute path")
    if "\x00" in value:
        raise CollectorError(f"{where} contains a NUL byte")
    return pathlib.Path(value)


def load_config(path: pathlib.Path) -> Config:
    raw = read_regular_file(path, secret=False, maximum=65536)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectorError("configuration is not valid UTF-8 JSON") from exc

    top = _exact_keys(
        payload,
        {
            "interval_seconds",
            "request_timeout_seconds",
            "listen_address",
            "listen_port",
            "kubernetes",
        },
        "configuration",
    )
    kubernetes = _exact_keys(
        top["kubernetes"], {"ca_file", "cert_file", "key_file"}, "kubernetes"
    )

    listen_address = top["listen_address"]
    if not isinstance(listen_address, str):
        raise CollectorError("listen_address must be a string")
    try:
        address = ipaddress.ip_address(listen_address)
    except ValueError as exc:
        raise CollectorError("listen_address must be an IP literal") from exc
    if address.version != 4 or not address.is_loopback:
        raise CollectorError("listen_address must be an IPv4 loopback address")

    return Config(
        interval_seconds=_bounded_int(
            top["interval_seconds"], 10, 300, "interval_seconds"
        ),
        request_timeout_seconds=_bounded_int(
            top["request_timeout_seconds"], 1, 10, "request_timeout_seconds"
        ),
        listen_address=listen_address,
        listen_port=_bounded_int(top["listen_port"], 1024, 65535, "listen_port"),
        kube_ca_file=_path(kubernetes["ca_file"], "kubernetes.ca_file"),
        kube_cert_file=_path(
            kubernetes["cert_file"], "kubernetes.cert_file"
        ),
        kube_key_file=_path(kubernetes["key_file"], "kubernetes.key_file"),
    )


def read_regular_file(
    path: pathlib.Path, *, secret: bool, maximum: int
) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise CollectorError(f"required file is unavailable: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise CollectorError(f"required path is not a regular file: {path}")
        if metadata.st_size > maximum:
            raise CollectorError(f"required file is unexpectedly large: {path}")
        if metadata.st_mode & 0o022:
            raise CollectorError(f"required file is group/other writable: {path}")
        if secret and metadata.st_mode & 0o007:
            raise CollectorError(f"secret file is accessible to other users: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(maximum + 1)
    except OSError as exc:
        raise CollectorError(f"required file cannot be read: {path}") from exc
    finally:
        os.close(descriptor)
    if len(data) > maximum:
        raise CollectorError(f"required file is unexpectedly large: {path}")
    return data


def _prom_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _metric(name: str, value: int | float, **labels: str) -> str:
    rendered = ""
    if labels:
        pairs = [f'{key}="{_prom_label(labels[key])}"' for key in sorted(labels)]
        rendered = "{" + ",".join(pairs) + "}"
    if isinstance(value, float):
        number = f"{value:.6f}"
    else:
        number = str(value)
    return f"{name}{rendered} {number}"


def parse_rfc3339(value: Any, where: str = "Lease renewTime") -> float:
    if not isinstance(value, str):
        raise CollectorError(f"{where} is not a string")
    match = RFC3339_RE.fullmatch(value)
    if match is None:
        raise CollectorError(f"{where} is not strict UTC RFC3339")
    fraction = match.group("fraction") or ""
    if fraction:
        fraction = "." + fraction[1:7].ljust(6, "0")
    try:
        parsed = dt.datetime.fromisoformat(match.group("date") + fraction + "+00:00")
    except ValueError as exc:
        raise CollectorError(f"{where} is invalid") from exc
    return parsed.timestamp()


def parse_lease(payload: bytes, now: float) -> dict[str, Any]:
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectorError("Lease response is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict) or document.get("kind") != "Lease":
        raise CollectorError("Lease response has the wrong kind")
    metadata = document.get("metadata")
    spec = document.get("spec")
    if not isinstance(metadata, dict) or not isinstance(spec, dict):
        raise CollectorError("Lease response lacks metadata or spec")
    if metadata.get("name") != "plndr-cp-lock" or metadata.get("namespace") != "kube-system":
        raise CollectorError("Lease response has the wrong identity")
    if not isinstance(metadata.get("resourceVersion"), str) or not metadata.get(
        "resourceVersion"
    ):
        raise CollectorError("Lease response lacks a resourceVersion")
    holder = spec.get("holderIdentity")
    if holder not in ROOT_NODES:
        raise CollectorError("Lease holder is not one of the fixed root nodes")
    duration = spec.get("leaseDurationSeconds")
    if isinstance(duration, bool) or not isinstance(duration, int) or duration != 30:
        raise CollectorError("Lease duration is not the declared 30 seconds")
    transitions = spec.get("leaseTransitions", 0)
    if (
        isinstance(transitions, bool)
        or not isinstance(transitions, int)
        or not 0 <= transitions <= 2**53
    ):
        raise CollectorError("Lease transition count is invalid")
    renew_timestamp = parse_rfc3339(spec.get("renewTime"))
    age = now - renew_timestamp
    future_skew = max(0.0, -age)
    age = max(0.0, age)
    valid = future_skew <= 5.0 and age <= float(duration)
    return {
        "holder": holder,
        "duration": duration,
        "transitions": transitions,
        "renew_timestamp": renew_timestamp,
        "renew_age": age,
        "future_skew": future_skew,
        "valid": valid,
    }


def _certificate_leaf_contract(
    item: dict[str, Any], metadata: dict[str, Any]
) -> dict[str, str]:
    name = metadata.get("name")
    namespace = metadata.get("namespace")
    uid = metadata.get("uid")
    generation = metadata.get("generation")
    if (
        not isinstance(name, str)
        or not name
        or namespace != ETCD_CERTIFICATE_NAMESPACE
        or not isinstance(uid, str)
        or not uid
        or metadata.get("deletionTimestamp") is not None
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
    ):
        raise CollectorError("etcd client Certificate metadata is incomplete")

    spec = item.get("spec")
    if not isinstance(spec, dict):
        raise CollectorError(f"Certificate {name} has no spec")
    issuer = spec.get("issuerRef")
    private_key = spec.get("privateKey")
    usages = spec.get("usages")
    if (
        not isinstance(issuer, dict)
        or issuer.get("group") != "cert-manager.io"
        or issuer.get("kind") != "ClusterIssuer"
        or not isinstance(issuer.get("name"), str)
        or issuer["name"] not in ETCD_ISSUER_ALLOWLIST
        or not isinstance(private_key, dict)
        or private_key.get("algorithm") != "ECDSA"
        or private_key.get("rotationPolicy") != "Always"
        or private_key.get("size", 256) != 256
        or spec.get("duration") not in ("24h", "24h0m0s")
        or spec.get("renewBefore") not in ("8h", "8h0m0s")
        or not isinstance(usages, list)
        or len(usages) != 2
        or not all(isinstance(usage, str) for usage in usages)
        or set(usages) != {"digital signature", "client auth"}
        or spec.get("isCA", False) is not False
    ):
        raise CollectorError(f"Certificate {name} violates the fixed leaf contract")

    if name == "fabric-etcdetcetc-admin":
        if (
            spec.get("commonName") != "fabric-etcdetcetc"
            or spec.get("secretName") != "fabric-etcdetcetc-admin"
        ):
            raise CollectorError("the etcdetcetc admin Certificate identity is invalid")
        return {
            "certificate": name,
            "credential": "admin",
            "issuer": issuer["name"],
            "tenant_name": "",
            "tenant_namespace": "",
            "tenant_uid": "",
        }

    labels = metadata.get("labels")
    owners = metadata.get("ownerReferences")
    if not isinstance(labels, dict) or not isinstance(owners, list) or len(owners) != 1:
        raise CollectorError(f"tenant Certificate {name} lacks exact provenance")
    tenant_uid = labels.get("etcdetcetc.samcday.com/tenant-uid")
    tenant_name = labels.get("etcdetcetc.samcday.com/tenant-name")
    tenant_namespace = labels.get("etcdetcetc.samcday.com/tenant-namespace")
    owner = owners[0]
    expected_name = f"etcdtenant-{tenant_uid}"
    if (
        not isinstance(tenant_uid, str)
        or TENANT_UID_RE.fullmatch(tenant_uid) is None
        or not isinstance(tenant_name, str)
        or not tenant_name
        or not isinstance(tenant_namespace, str)
        or not tenant_namespace
        or name != expected_name
        or spec.get("commonName") != f"etcdtenant:{tenant_uid}"
        or spec.get("secretName") != f"{expected_name}-tls"
        or not isinstance(owner, dict)
        or owner.get("apiVersion") != "etcdetcetc.samcday.com/v1alpha1"
        or owner.get("kind") != "EtcdCluster"
        or owner.get("name") != "fabric-etcd"
        or owner.get("controller") is not True
        or not isinstance(owner.get("uid"), str)
        or not owner["uid"]
    ):
        raise CollectorError(f"tenant Certificate {name} identity is invalid")
    return {
        "certificate": name,
        "credential": "tenant",
        "issuer": issuer["name"],
        "tenant_name": tenant_name,
        "tenant_namespace": tenant_namespace,
        "tenant_uid": tenant_uid,
    }


def _certificate_status(
    item: dict[str, Any], metadata: dict[str, Any]
) -> dict[str, Any]:
    generation = metadata["generation"]
    status = item.get("status")
    if not isinstance(status, dict):
        return {"ready": False, "issuing": False, "status_valid": False}
    conditions = status.get("conditions")
    revision = status.get("revision")
    if (
        not isinstance(conditions, list)
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
    ):
        return {"ready": False, "issuing": False, "status_valid": False}

    current_ready_conditions = [
        condition
        for condition in conditions
        if isinstance(condition, dict)
        and condition.get("type") == "Ready"
        and condition.get("observedGeneration") == generation
        and condition.get("status") in ("True", "False", "Unknown")
    ]
    issuing = any(
        isinstance(condition, dict)
        and condition.get("type") == "Issuing"
        and condition.get("status") == "True"
        and condition.get("observedGeneration") == generation
        for condition in conditions
    )
    if len(current_ready_conditions) != 1:
        return {"ready": False, "issuing": issuing, "status_valid": False}

    try:
        not_after = parse_rfc3339(
            status.get("notAfter"), "Certificate status.notAfter"
        )
        renewal_time = parse_rfc3339(
            status.get("renewalTime"), "Certificate status.renewalTime"
        )
    except CollectorError:
        return {"ready": False, "issuing": issuing, "status_valid": False}
    if abs((not_after - renewal_time) - 8 * 60 * 60) > 1:
        return {"ready": False, "issuing": issuing, "status_valid": False}
    return {
        "ready": current_ready_conditions[0]["status"] == "True",
        "issuing": issuing,
        "status_valid": True,
        "not_after": not_after,
        "renewal_time": renewal_time,
        "revision": revision,
    }


def parse_etcd_certificate_list(payload: bytes) -> list[dict[str, Any]]:
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectorError("Certificate list is not valid UTF-8 JSON") from exc
    if (
        not isinstance(document, dict)
        or document.get("apiVersion") != "cert-manager.io/v1"
        or document.get("kind") != "CertificateList"
    ):
        raise CollectorError("Certificate list has the wrong identity")
    list_metadata = document.get("metadata")
    items = document.get("items")
    if (
        not isinstance(list_metadata, dict)
        or not isinstance(list_metadata.get("resourceVersion"), str)
        or not list_metadata["resourceVersion"]
        or list_metadata.get("continue", "") != ""
        or not isinstance(items, list)
        or len(items) > MAX_ETCD_CERTIFICATES
    ):
        raise CollectorError("Certificate list is incomplete or exceeds its bound")

    certificates: list[dict[str, Any]] = []
    identities: set[str] = set()
    admin_count = 0
    for item in items:
        if (
            not isinstance(item, dict)
            or item.get("apiVersion") != "cert-manager.io/v1"
            or item.get("kind") != "Certificate"
            or not isinstance(item.get("metadata"), dict)
        ):
            raise CollectorError("Certificate list contains a malformed item")
        metadata = item["metadata"]
        labels = _certificate_leaf_contract(item, metadata)
        if labels["certificate"] in identities:
            raise CollectorError("Certificate list contains a duplicate identity")
        identities.add(labels["certificate"])
        admin_count += int(labels["credential"] == "admin")
        certificates.append(labels | _certificate_status(item, metadata))
    if admin_count != 1:
        raise CollectorError("Certificate list does not contain exactly one admin leaf")
    return sorted(
        certificates,
        key=lambda certificate: (
            certificate["credential"],
            certificate["certificate"],
        ),
    )


def certificate_not_after(path: pathlib.Path) -> float:
    read_regular_file(path, secret=False, maximum=65536)
    try:
        result = subprocess.run(
            ["/usr/bin/openssl", "x509", "-in", str(path), "-noout", "-enddate"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=2,
            env={"HOME": "/nonexistent", "PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CollectorError("Kubernetes client certificate expiry check failed") from exc
    prefix = b"notAfter="
    line = result.stdout.strip()
    if result.returncode != 0 or not line.startswith(prefix) or b"\n" in line:
        raise CollectorError("Kubernetes client certificate has an invalid expiry")
    try:
        parsed = dt.datetime.strptime(
            line[len(prefix) :].decode("ascii"), "%b %d %H:%M:%S %Y GMT"
        ).replace(tzinfo=dt.timezone.utc)
    except (UnicodeDecodeError, ValueError) as exc:
        raise CollectorError("Kubernetes client certificate expiry is not parseable") from exc
    return parsed.timestamp()


def _api_get(
    host: str,
    port: int,
    path: str,
    context: ssl.SSLContext,
    timeout: int,
    maximum_body: int = MAX_HTTP_BODY,
) -> tuple[int, str, bytes]:
    connection = http.client.HTTPSConnection(host, port, timeout=timeout, context=context)
    try:
        connection.request(
            "GET",
            path,
            headers={
                "Accept": "application/json, text/plain;q=0.9",
                "Connection": "close",
                "User-Agent": f"fabric-observer/{VERSION}",
            },
        )
        response = connection.getresponse()
        body = response.read(maximum_body + 1)
        if len(body) > maximum_body:
            raise CollectorError("Kubernetes API response exceeded the size bound")
        return response.status, response.getheader("Content-Type", ""), body
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        raise CollectorError("bounded Kubernetes API request failed") from exc
    finally:
        connection.close()


def collect_etcd_certificate_metrics(
    context: ssl.SSLContext | None,
    api_vip_ready: bool,
    request_timeout_seconds: int,
) -> list[str]:
    lines = [
        "# HELP fabric_observer_etcd_client_certificate_query_success One when the bounded cert-manager Certificate list request succeeded.",
        "# TYPE fabric_observer_etcd_client_certificate_query_success gauge",
        "# HELP fabric_observer_etcd_client_certificate_inventory_valid One when the list contains exactly the structurally valid admin leaf and zero or more UID-scoped tenant leaves.",
        "# TYPE fabric_observer_etcd_client_certificate_inventory_valid gauge",
    ]
    response_body: bytes | None = None
    if context is not None and api_vip_ready:
        try:
            status, content_type, body = _api_get(
                "10.66.0.254",
                6443,
                ETCD_CERTIFICATES_PATH,
                context,
                request_timeout_seconds,
                MAX_CERTIFICATE_LIST_BODY,
            )
            if status != 200 or "json" not in content_type.lower():
                raise CollectorError(
                    "Certificate list request returned an unexpected response"
                )
            response_body = body
        except CollectorError:
            response_body = None
    lines.append(
        _metric(
            "fabric_observer_etcd_client_certificate_query_success",
            int(response_body is not None),
        )
    )

    certificates: list[dict[str, Any]] | None = None
    if response_body is not None:
        try:
            certificates = parse_etcd_certificate_list(response_body)
        except CollectorError:
            certificates = None
    lines.append(
        _metric(
            "fabric_observer_etcd_client_certificate_inventory_valid",
            int(certificates is not None),
        )
    )
    if certificates is None:
        return lines

    lines.extend(
        [
            "# HELP fabric_observer_etcd_client_certificate_ready One when cert-manager reports Ready=True for the current Certificate generation.",
            "# TYPE fabric_observer_etcd_client_certificate_ready gauge",
            "# HELP fabric_observer_etcd_client_certificate_issuing One when cert-manager reports Issuing=True for the current Certificate generation.",
            "# TYPE fabric_observer_etcd_client_certificate_issuing gauge",
            "# HELP fabric_observer_etcd_client_certificate_status_valid One when current-generation status has a valid revision and exact renewal/expiry relationship.",
            "# TYPE fabric_observer_etcd_client_certificate_status_valid gauge",
            "# HELP fabric_observer_etcd_client_certificate_revision cert-manager's issued revision for the current leaf.",
            "# TYPE fabric_observer_etcd_client_certificate_revision gauge",
            "# HELP fabric_observer_etcd_client_certificate_not_after_timestamp_seconds Expiry time reported for the current admin or tenant client leaf.",
            "# TYPE fabric_observer_etcd_client_certificate_not_after_timestamp_seconds gauge",
            "# HELP fabric_observer_etcd_client_certificate_renewal_timestamp_seconds Scheduled renewal time reported for the current admin or tenant client leaf.",
            "# TYPE fabric_observer_etcd_client_certificate_renewal_timestamp_seconds gauge",
        ]
    )
    for certificate in certificates:
        labels = {
            key: certificate[key]
            for key in (
                "certificate",
                "credential",
                "issuer",
                "tenant_name",
                "tenant_namespace",
                "tenant_uid",
            )
        }
        lines.extend(
            [
                _metric(
                    "fabric_observer_etcd_client_certificate_ready",
                    int(certificate["ready"]),
                    **labels,
                ),
                _metric(
                    "fabric_observer_etcd_client_certificate_issuing",
                    int(certificate["issuing"]),
                    **labels,
                ),
                _metric(
                    "fabric_observer_etcd_client_certificate_status_valid",
                    int(certificate["status_valid"]),
                    **labels,
                ),
            ]
        )
        if certificate["status_valid"]:
            lines.extend(
                [
                    _metric(
                        "fabric_observer_etcd_client_certificate_revision",
                        certificate["revision"],
                        **labels,
                    ),
                    _metric(
                        "fabric_observer_etcd_client_certificate_not_after_timestamp_seconds",
                        certificate["not_after"],
                        **labels,
                    ),
                    _metric(
                        "fabric_observer_etcd_client_certificate_renewal_timestamp_seconds",
                        certificate["renewal_time"],
                        **labels,
                    ),
                ]
            )
    return lines


def collect_kubernetes(config: Config, now: float) -> list[str]:
    lines = [
        "# HELP fabric_observer_kube_api_ready Kubernetes readyz succeeded through a fixed fabric endpoint.",
        "# TYPE fabric_observer_kube_api_ready gauge",
    ]
    certificate_expiry: float | None = None
    try:
        ca_data = read_regular_file(config.kube_ca_file, secret=False, maximum=65536)
        read_regular_file(config.kube_cert_file, secret=False, maximum=65536)
        read_regular_file(config.kube_key_file, secret=True, maximum=65536)
        context = ssl.create_default_context(cadata=ca_data.decode("ascii"))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = True
        context.load_cert_chain(config.kube_cert_file, config.kube_key_file)
        certificate_expiry = certificate_not_after(config.kube_cert_file)
        if certificate_expiry <= now:
            raise CollectorError("Kubernetes client certificate is expired")
    except (UnicodeDecodeError, OSError, ssl.SSLError, CollectorError):
        context = None
        certificate_expiry = None

    lines.extend(
        [
            "# HELP fabric_observer_kube_client_certificate_valid One when the observer client certificate can be loaded and its expiry parsed.",
            "# TYPE fabric_observer_kube_client_certificate_valid gauge",
            _metric(
                "fabric_observer_kube_client_certificate_valid",
                int(certificate_expiry is not None),
            ),
        ]
    )
    if certificate_expiry is not None:
        lines.extend(
            [
                "# HELP fabric_observer_kube_client_certificate_not_after_timestamp_seconds Expiry time of the short-lived Kubernetes observer client certificate.",
                "# TYPE fabric_observer_kube_client_certificate_not_after_timestamp_seconds gauge",
                _metric(
                    "fabric_observer_kube_client_certificate_not_after_timestamp_seconds",
                    certificate_expiry,
                ),
            ]
        )

    results: dict[str, bool] = {}

    def ready(endpoint: tuple[str, str, str, str, int]) -> tuple[str, bool]:
        _kind, endpoint_name, _node, host, port = endpoint
        if context is None:
            return endpoint_name, False
        try:
            status, _content_type, body = _api_get(
                host,
                port,
                READY_PATH,
                context,
                config.request_timeout_seconds,
            )
            return endpoint_name, status == 200 and body.strip() == b"ok"
        except CollectorError:
            return endpoint_name, False

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(ready, endpoint) for endpoint in API_ENDPOINTS]
        for future in futures:
            endpoint_name, success = future.result()
            results[endpoint_name] = success

    for kind, endpoint_name, node, _host, _port in API_ENDPOINTS:
        lines.append(
            _metric(
                "fabric_observer_kube_api_ready",
                int(results.get(endpoint_name, False)),
                endpoint=endpoint_name,
                endpoint_type=kind,
                node=node,
            )
        )

    lines.extend(
        [
            "# HELP fabric_observer_kube_vip_lease_query_success The fixed kube-vip Lease was fetched and strictly parsed.",
            "# TYPE fabric_observer_kube_vip_lease_query_success gauge",
            "# HELP fabric_observer_kube_vip_lease_holder One for the current fixed root Lease holder, zero for the other roots.",
            "# TYPE fabric_observer_kube_vip_lease_holder gauge",
        ]
    )
    lease: dict[str, Any] | None = None
    if context is not None and results.get("api-vip", False):
        try:
            status, content_type, body = _api_get(
                "10.66.0.254",
                6443,
                LEASE_PATH,
                context,
                config.request_timeout_seconds,
            )
            if status != 200 or "json" not in content_type.lower():
                raise CollectorError("Lease request returned an unexpected response")
            lease = parse_lease(body, now)
        except CollectorError:
            lease = None

    lines.append(
        _metric("fabric_observer_kube_vip_lease_query_success", int(lease is not None))
    )
    for node in ROOT_NODES:
        lines.append(
            _metric(
                "fabric_observer_kube_vip_lease_holder",
                int(lease is not None and lease["holder"] == node),
                node=node,
            )
        )

    lines.extend(
        [
            "# HELP fabric_observer_kube_vip_lease_valid One only when the fixed Lease holder, duration, renew time, and clock relationship are valid.",
            "# TYPE fabric_observer_kube_vip_lease_valid gauge",
            _metric(
                "fabric_observer_kube_vip_lease_valid",
                int(lease is not None and lease["valid"]),
            ),
        ]
    )
    if lease is not None:
        lines.extend(
            [
                "# HELP fabric_observer_kube_vip_lease_renew_age_seconds Observer wall-clock age of the kube-vip Lease renewal.",
                "# TYPE fabric_observer_kube_vip_lease_renew_age_seconds gauge",
                _metric(
                    "fabric_observer_kube_vip_lease_renew_age_seconds",
                    lease["renew_age"],
                ),
                "# HELP fabric_observer_kube_vip_lease_future_skew_seconds Amount by which Lease renewal is ahead of observer time.",
                "# TYPE fabric_observer_kube_vip_lease_future_skew_seconds gauge",
                _metric(
                    "fabric_observer_kube_vip_lease_future_skew_seconds",
                    lease["future_skew"],
                ),
                "# HELP fabric_observer_kube_vip_lease_duration_seconds Declared kube-vip Lease duration.",
                "# TYPE fabric_observer_kube_vip_lease_duration_seconds gauge",
                _metric(
                    "fabric_observer_kube_vip_lease_duration_seconds",
                    lease["duration"],
                ),
                "# HELP fabric_observer_kube_vip_lease_transitions_total Kubernetes Lease transition counter.",
                "# TYPE fabric_observer_kube_vip_lease_transitions_total counter",
                _metric(
                    "fabric_observer_kube_vip_lease_transitions_total",
                    lease["transitions"],
                ),
                "# HELP fabric_observer_kube_vip_lease_renew_timestamp_seconds Kubernetes Lease renewal timestamp.",
                "# TYPE fabric_observer_kube_vip_lease_renew_timestamp_seconds gauge",
                _metric(
                    "fabric_observer_kube_vip_lease_renew_timestamp_seconds",
                    lease["renew_timestamp"],
                ),
            ]
        )
    lines.extend(
        collect_etcd_certificate_metrics(
            context,
            results.get("api-vip", False),
            config.request_timeout_seconds,
        )
    )
    return lines


def collect_metrics(config: Config) -> bytes:
    started = time.monotonic()
    now = time.time()
    lines = [
        "# HELP fabric_observer_collection_available One when the collector produced a complete payload.",
        "# TYPE fabric_observer_collection_available gauge",
        _metric("fabric_observer_collection_available", 1),
        "# HELP fabric_observer_collection_timestamp_seconds Unix time at the start of the most recently completed collection cycle.",
        "# TYPE fabric_observer_collection_timestamp_seconds gauge",
        _metric("fabric_observer_collection_timestamp_seconds", now),
        "# HELP fabric_observer_build_info Fixed fabric observer collector build information.",
        "# TYPE fabric_observer_build_info gauge",
        _metric("fabric_observer_build_info", 1, version=VERSION),
    ]
    lines.extend(collect_kubernetes(config, now))
    lines.extend(
        [
            "# HELP fabric_observer_collection_duration_seconds Time required for one bounded collection cycle.",
            "# TYPE fabric_observer_collection_duration_seconds gauge",
            _metric(
                "fabric_observer_collection_duration_seconds",
                time.monotonic() - started,
            ),
        ]
    )
    return ("\n".join(lines) + "\n").encode("ascii")


class MetricsState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._payload = (
            b"# HELP fabric_observer_collection_available One after the first collection cycle.\n"
            b"# TYPE fabric_observer_collection_available gauge\n"
            b"fabric_observer_collection_available 0\n"
        )

    def set(self, payload: bytes) -> None:
        with self._lock:
            self._payload = payload

    def get(self) -> bytes:
        with self._lock:
            return self._payload


def make_handler(state: MetricsState):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "fabric-observer"
        sys_version = ""

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            target = urllib.parse.urlsplit(self.path)
            if target.path != "/metrics" or target.query or target.fragment:
                self.send_error(404)
                return
            payload = state.get()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    return Handler


def serve(config: Config) -> None:
    read_regular_file(config.kube_ca_file, secret=False, maximum=65536)
    read_regular_file(config.kube_cert_file, secret=False, maximum=65536)
    read_regular_file(config.kube_key_file, secret=True, maximum=65536)
    state = MetricsState()
    stopped = threading.Event()

    def collect_loop() -> None:
        while not stopped.is_set():
            try:
                state.set(collect_metrics(config))
            except Exception:  # The fail-closed payload is preferable to process death.
                state.set(
                    b"# HELP fabric_observer_collection_available One when the collector produced a complete payload.\n"
                    b"# TYPE fabric_observer_collection_available gauge\n"
                    b"fabric_observer_collection_available 0\n"
                )
            stopped.wait(config.interval_seconds)

    worker = threading.Thread(target=collect_loop, name="collector", daemon=True)
    worker.start()
    server = http.server.ThreadingHTTPServer(
        (config.listen_address, config.listen_port), make_handler(state)
    )
    server.daemon_threads = True

    def stop(_signum: int, _frame: Any) -> None:
        stopped.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        stopped.set()
        worker.join(timeout=config.request_timeout_seconds + 2)
        server.server_close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("serve", "once"), help="bounded action"
    )
    parser.add_argument("--config", required=True, type=pathlib.Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        config = load_config(arguments.config)
        if arguments.command == "serve":
            serve(config)
        elif arguments.command == "once":
            sys.stdout.buffer.write(collect_metrics(config))
    except CollectorError as exc:
        print(f"fabric-observer: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
