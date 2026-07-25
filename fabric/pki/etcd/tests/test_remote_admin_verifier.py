#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest


REPO = pathlib.Path(__file__).resolve().parents[4]
ADMIN = REPO / "fabric" / "pki" / "etcd" / "manage-etcdetcetc-admin"


class RemoteAdminVerifierFixtureTests(unittest.TestCase):
    def test_exact_fixture_passes_and_admin_role_drift_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture_root = pathlib.Path(temporary) / "repo"
            admin_dir = fixture_root / "fabric" / "pki" / "etcd"
            online_ca_dir = fixture_root / "fabric" / "pki" / "etcdetcetc"
            router_dir = fixture_root / "fabric" / "router"
            access_dir = fixture_root / "fabric" / "access"
            config_dir = fixture_root / "fabric" / "cluster" / "etcdetcetc"
            lib_dir = fixture_root / "scripts" / "lib"
            fake_bin = pathlib.Path(temporary) / "bin"
            for directory in (
                admin_dir,
                online_ca_dir,
                router_dir,
                access_dir,
                config_dir,
                lib_dir,
                fake_bin,
            ):
                directory.mkdir(parents=True, exist_ok=True)

            shutil.copy2(ADMIN, admin_dir / ADMIN.name)
            shutil.copy2(
                REPO / "scripts" / "lib" / "fabric-secure-tempdir.sh",
                lib_dir / "fabric-secure-tempdir.sh",
            )
            shutil.copy2(
                REPO / "fabric" / "router" / "data-files.txt",
                router_dir / "data-files.txt",
            )
            shutil.copy2(
                REPO / "fabric" / "access" / "known_hosts",
                access_dir / "known_hosts",
            )
            shutil.copy2(
                REPO / "fabric" / "cluster" / "etcdetcetc" / "server-ca.yaml",
                config_dir / "server-ca.yaml",
            )
            shutil.copy2(
                REPO / "scripts" / "fabric-ssh-proxy",
                fixture_root / "scripts" / "fabric-ssh-proxy",
            )

            ca_key = pathlib.Path(temporary) / "ca-key.pem"
            leaf_key = pathlib.Path(temporary) / "leaf-key.pem"
            leaf_csr = pathlib.Path(temporary) / "leaf.csr"
            leaf_cert = pathlib.Path(temporary) / "leaf.pem"
            extension = pathlib.Path(temporary) / "leaf.ext"
            extension.write_text(
                "basicConstraints=critical,CA:FALSE\n"
                "keyUsage=critical,digitalSignature\n"
                "extendedKeyUsage=critical,clientAuth\n"
            )
            commands = (
                (
                    "openssl", "req", "-x509", "-newkey", "ec", "-nodes",
                    "-pkeyopt", "ec_paramgen_curve:P-256",
                    "-keyout", str(ca_key),
                    "-out", str(online_ca_dir / "client-ca.pem"),
                    "-days", "2", "-subj", "/CN=fabric-etcdetcetc-client-ca",
                ),
                (
                    "openssl", "req", "-newkey", "ec", "-nodes",
                    "-pkeyopt", "ec_paramgen_curve:P-256",
                    "-keyout", str(leaf_key), "-out", str(leaf_csr),
                    "-subj", "/CN=fabric-etcdetcetc",
                ),
                (
                    "openssl", "x509", "-req", "-in", str(leaf_csr),
                    "-CA", str(online_ca_dir / "client-ca.pem"),
                    "-CAkey", str(ca_key), "-CAcreateserial",
                    "-out", str(leaf_cert), "-days", "1",
                    "-extfile", str(extension),
                ),
            )
            for command in commands:
                result = subprocess.run(
                    command,
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

            secret_data = {
                "ca.crt": base64.b64encode(
                    (online_ca_dir / "client-ca.pem").read_bytes()
                ).decode(),
                "tls.crt": base64.b64encode(leaf_cert.read_bytes()).decode(),
                "tls.key": base64.b64encode(leaf_key.read_bytes()).decode(),
            }
            secret = {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": "fabric-etcdetcetc-admin",
                    "namespace": "etcdetcetc",
                    "uid": "11111111-2222-3333-4444-555555555555",
                    "resourceVersion": "12345",
                    "labels": {
                        "app.kubernetes.io/name": "etcdetcetc",
                        "app.kubernetes.io/component": "admin-credential",
                    },
                },
                "type": "kubernetes.io/tls",
                "data": secret_data,
            }
            secret_fixture = pathlib.Path(temporary) / "secret.json"
            secret_fixture.write_text(json.dumps(secret))

            server_ca_pem = subprocess.run(
                (
                    "yq", "-er", '.data["ca.crt"]',
                    str(config_dir / "server-ca.yaml"),
                ),
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.rstrip("\n") + "\n"
            server_ca = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": "fabric-etcd-server-ca",
                    "namespace": "etcdetcetc",
                    "uid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "resourceVersion": "67890",
                },
                "data": {"ca.crt": server_ca_pem},
            }
            server_ca_fixture = pathlib.Path(temporary) / "server-ca.json"
            server_ca_fixture.write_text(json.dumps(server_ca))

            endpoints = [
                "https://127.0.0.1:42379",
                "https://127.0.0.1:42380",
                "https://127.0.0.1:42381",
            ]
            status = [
                {
                    "Endpoint": endpoint,
                    "Status": {
                        "header": {
                            "cluster_id": 42,
                            "member_id": member_id,
                            "revision": 100,
                        },
                        "leader": 101,
                        "version": "3.6.13",
                        "isLearner": False,
                    },
                }
                for endpoint, member_id in zip(endpoints, (101, 102, 103))
            ]
            records: dict[str, object] = {
                "health": [
                    {"endpoint": endpoint, "health": True} for endpoint in endpoints
                ],
                "status": status,
                "alarms": {"alarms": []},
                "auth": {"enabled": True},
                "root-user": {"roles": ["root"]},
                "fabric-root-user": {"roles": ["fabric-root"]},
                "fabric-root-role": {
                    "perm": [
                        {
                            "key": base64.b64encode(b"/bootstrap").decode(),
                            "range_end": base64.b64encode(b"/bootstraq").decode(),
                        },
                        {
                            "permType": 2,
                            "key": base64.b64encode(b"/fabric-root/").decode(),
                            "range_end": base64.b64encode(b"/fabric-root0").decode(),
                        },
                        {
                            "permType": 2,
                            "key": base64.b64encode(b"/bootstrap/").decode(),
                            "range_end": base64.b64encode(b"/bootstrap0").decode(),
                        },
                    ]
                },
                "admin-user": {"roles": ["root"]},
                "members": {
                    "header": {"cluster_id": 42},
                    "members": [
                        {
                            "ID": member_id,
                            "name": f"fabric-az1-cp{index}",
                            "peerURLs": [
                                f"https://fabric-az1-cp{index}.fabric.internal:2380"
                            ],
                            "clientURLs": [
                                f"https://fabric-az1-cp{index}.fabric.internal:2379"
                            ],
                            "isLearner": False,
                        }
                        for index, member_id in enumerate((101, 102, 103), start=1)
                    ],
                },
            }
            query_dir = pathlib.Path(temporary) / "queries"
            query_dir.mkdir()

            def write_queries(query_records: dict[str, object]) -> None:
                for label, value in query_records.items():
                    (query_dir / f"{label}.json").write_text(
                        json.dumps(value, separators=(",", ":")) + "\n"
                    )

            write_queries(records)
            fake_git = fake_bin / "git"
            fake_git.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "[[ ${1:-} == -C ]] && shift 2\n"
                "case \"$*\" in\n"
                "  'rev-parse --show-toplevel') printf '%s\\n' \"$FAKE_REPO_ROOT\" ;;\n"
                "  'branch --show-current') printf '%s\\n' main ;;\n"
                "  'status --porcelain') : ;;\n"
                "  'fetch --quiet origin main') : ;;\n"
                "  'rev-parse HEAD'|'rev-parse refs/remotes/origin/main') "
                "printf '%s\\n' \"$FAKE_COMMIT\" ;;\n"
                "  *) printf 'unexpected fake git call: %s\\n' \"$*\" >&2; exit 1 ;;\n"
                "esac\n"
            )
            fake_git.chmod(0o755)
            fake_ik = fixture_root / "scripts" / "ik"
            fake_ik.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "case \"$*\" in\n"
                "  *'get secret'*) exec /usr/bin/cat \"$FAKE_SECRET_JSON\" ;;\n"
                "  *'get configmap'*) exec /usr/bin/cat \"$FAKE_SERVER_CA_JSON\" ;;\n"
                "  *) printf 'unexpected fake ik call: %s\\n' \"$*\" >&2; exit 1 ;;\n"
                "esac\n"
            )
            fake_ik.chmod(0o755)
            fake_identity = pathlib.Path(temporary) / "identity"
            fake_identity.write_text("fixture identity\n")
            fake_identity.chmod(0o600)
            fake_ssh = fake_bin / "ssh"
            fake_ssh.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "trap 'exit 0' TERM INT\n"
                "while :; do /usr/bin/sleep 1; done\n"
            )
            fake_ssh.chmod(0o755)
            fake_etcdctl = fake_bin / "etcdctl"
            fake_etcdctl.write_text(
                "#!/usr/bin/bash\n"
                "set -euo pipefail\n"
                "[[ -z ${ETCDCTL_USER+x} && -z ${ETCDCTL_PASSWORD+x} && "
                "-z ${ETCDCTL_ENDPOINTS+x} && -z ${ETCDCTL_CERT+x} && "
                "-z ${ETCDCTL_KEY+x} && -z ${ETCDCTL_CACERT+x} ]]\n"
                "query_dir=$(/usr/bin/dirname \"$0\")/../queries\n"
                "case \"$*\" in\n"
                "  'version') printf 'etcdctl version: 3.6.13\\nAPI version: 3.6\\n' ;;\n"
                "  *'endpoint health') exec /usr/bin/cat \"$query_dir/health.json\" ;;\n"
                "  *'endpoint status') exec /usr/bin/cat \"$query_dir/status.json\" ;;\n"
                "  *'alarm list') exec /usr/bin/cat \"$query_dir/alarms.json\" ;;\n"
                "  *'auth status') exec /usr/bin/cat \"$query_dir/auth.json\" ;;\n"
                "  *'user get root') exec /usr/bin/cat \"$query_dir/root-user.json\" ;;\n"
                "  *'user get fabric-root') exec /usr/bin/cat \"$query_dir/fabric-root-user.json\" ;;\n"
                "  *'role get fabric-root') exec /usr/bin/cat \"$query_dir/fabric-root-role.json\" ;;\n"
                "  *'user get fabric-etcdetcetc') exec /usr/bin/cat \"$query_dir/admin-user.json\" ;;\n"
                "  *'member list') exec /usr/bin/cat \"$query_dir/members.json\" ;;\n"
                "  *) printf 'unexpected fake etcdctl call: %s\\n' \"$*\" >&2; exit 1 ;;\n"
                "esac\n"
            )
            fake_etcdctl.chmod(0o755)
            fake_sha256sum = fake_bin / "sha256sum"
            fake_sha256sum.write_text(
                "#!/usr/bin/bash\n"
                "set -euo pipefail\n"
                "if (( $# > 0 )) && [[ ${!#} == */bin/etcdctl ]]; then\n"
                "  printf '%s  %s\\n' "
                "83040f35846861c2b121a599d7066cf847436dd63b623a91b203ee7db209c6df "
                "\"${!#}\"\n"
                "  exit 0\n"
                "fi\n"
                "exec /usr/bin/sha256sum \"$@\"\n"
            )
            fake_sha256sum.chmod(0o755)

            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{fake_bin}:{environment['PATH']}",
                    "FABRIC_SSH_IDENTITY": str(fake_identity),
                    "FAKE_COMMIT": "a" * 40,
                    "FAKE_REPO_ROOT": str(fixture_root),
                    "FAKE_SECRET_JSON": str(secret_fixture),
                    "FAKE_SERVER_CA_JSON": str(server_ca_fixture),
                    "ETCDCTL_USER": "ambient-user:ambient-password",
                    "ETCDCTL_PASSWORD": "ambient-password",
                    "ETCDCTL_ENDPOINTS": "http://127.0.0.1:1",
                    "ETCDCTL_CERT": "/ambient/cert",
                    "ETCDCTL_KEY": "/ambient/key",
                    "ETCDCTL_CACERT": "/ambient/ca",
                }
            )

            def invoke() -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    (str(admin_dir / ADMIN.name), "--verify-remote"),
                    cwd=fixture_root,
                    env=environment,
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

            result = invoke()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout,
                f"FABRIC_ETCDETCETC_ADMIN_REMOTE=PASS revision={'a' * 40}\n",
            )

            records["admin-user"] = {"roles": ["unexpected"]}
            write_queries(records)
            result = invoke()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "delegated administrator does not have exactly the built-in root role",
                result.stderr,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
