#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import os
import pathlib
import re
import subprocess
import tempfile
import time
import unittest

import yaml


REPO_ROOT = pathlib.Path(__file__).parents[3]
HELPER = REPO_ROOT / "scripts" / "rollout-fabric-router-etcd-policy"
WIRED_OBSERVER = REPO_ROOT / "fabric" / "observer" / "verify-wired-netns"
TRANSITION = REPO_ROOT / "fabric" / "router" / "etcd-client-transition.uci"
FENCE_GUARD = REPO_ROOT / "fabric" / "router" / "etcd-client-fence.nft"
DESIRED = (
    REPO_ROOT
    / "fabric"
    / "router"
    / "files"
    / "etc"
    / "uci-defaults"
    / "30-isolation"
)
CHILDREN = REPO_ROOT / "fabric" / "cluster" / "flux-system" / "children.yaml"
ROOT_KUSTOMIZATION = REPO_ROOT / "fabric" / "cluster" / "flux-system" / "root.yaml"
ACTIVATION = REPO_ROOT / "fabric" / "cluster" / "etcdetcetc" / "README.md"
ROUTER_README = REPO_ROOT / "fabric" / "router" / "README.md"
BOOTSTRAP_KUSTOMIZATION = (
    REPO_ROOT / "fabric" / "cluster" / "etcdetcetc" / "kustomization.yaml"
)
RUNTIME_KUSTOMIZATION = (
    REPO_ROOT
    / "fabric"
    / "cluster"
    / "etcdetcetc"
    / "runtime"
    / "kustomization.yaml"
)
CONTROLLER_RELEASE = (
    REPO_ROOT
    / "fabric"
    / "cluster"
    / "etcdetcetc-controller"
    / "release.yaml"
)
PLATFORM_NAMESPACES = REPO_ROOT / "fabric" / "cluster" / "platform" / "namespaces.yaml"
PLATFORM_NETWORK_POLICIES = (
    REPO_ROOT / "fabric" / "cluster" / "platform" / "network-policies.yaml"
)
BOOTSTRAP_IMAGE_TAG = "2026072008"
BOOTSTRAP_IMAGE_DIGEST = (
    "sha256:863603a393e2671b1492b9a212df1ff87a37925bc6bac67c207133b9ce108977"
)


class EtcdPolicyRolloutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.helper = HELPER.read_text(encoding="utf-8")
        cls.transition = TRANSITION.read_text(encoding="utf-8")
        cls.fence_guard = FENCE_GUARD.read_text(encoding="utf-8")
        cls.desired = DESIRED.read_text(encoding="utf-8")
        cls.children = CHILDREN.read_text(encoding="utf-8")
        cls.root_kustomization = yaml.safe_load(
            ROOT_KUSTOMIZATION.read_text(encoding="utf-8")
        )
        cls.activation = ACTIVATION.read_text(encoding="utf-8")
        cls.router_readme = ROUTER_README.read_text(encoding="utf-8")
        cls.bootstrap_kustomization = BOOTSTRAP_KUSTOMIZATION.read_text(
            encoding="utf-8"
        )
        cls.runtime_kustomization = RUNTIME_KUSTOMIZATION.read_text(
            encoding="utf-8"
        )
        cls.controller_release = CONTROLLER_RELEASE.read_text(encoding="utf-8")
        cls.platform_namespaces = list(
            yaml.safe_load_all(PLATFORM_NAMESPACES.read_text(encoding="utf-8"))
        )
        cls.platform_network_policies = list(
            yaml.safe_load_all(
                PLATFORM_NETWORK_POLICIES.read_text(encoding="utf-8")
            )
        )

    def test_scripts_are_syntactically_valid(self) -> None:
        subprocess.run(["bash", "-n", str(HELPER)], check=True)
        subprocess.run(["bash", "-n", str(WIRED_OBSERVER)], check=True)
        heredocs = re.findall(
            r"<<'(?P<tag>REMOTE_[A-Z_]+)'(?: \|\| true)?\n(?P<body>.*?)\n(?P=tag)",
            self.helper,
            flags=re.DOTALL,
        )
        self.assertGreaterEqual(len(heredocs), 8)
        for tag, body in heredocs:
            with self.subTest(tag=tag):
                subprocess.run(
                    ["sh", "-n"],
                    input=body,
                    text=True,
                    check=True,
                )

    def test_transition_payload_is_fixed_and_matches_current_etcd_policy(self) -> None:
        digest = hashlib.sha256(TRANSITION.read_bytes()).hexdigest()
        self.assertIn(f"readonly transition_sha256={digest}", self.helper)
        self.assertEqual(len(self.transition.splitlines()), 27)
        self.assertEqual(
            self.transition.splitlines()[0],
            "delete firewall.fabric_flat_reject_services_etcd",
        )
        for line in self.transition.splitlines()[1:]:
            with self.subTest(line=line):
                self.assertEqual(self.desired.count(line), 1)
        self.assertNotIn("dest_port='2381'", self.transition)
        self.assertIn(
            "set firewall.fabric_flat_services_root_metrics='rule'", self.desired
        )
        self.assertNotIn(
            "firewall.fabric_flat_reject_services_etcd.", self.desired
        )
        self.assertLess(
            self.desired.index("set firewall.fabric_flat_services_etcd_client='rule'"),
            self.desired.index(
                "set firewall.fabric_flat_reject_services_etcd_internal='rule'"
            ),
        )
        self.assertLess(
            self.desired.index(
                "set firewall.fabric_flat_reject_services_etcd_internal='rule'"
            ),
            self.desired.index(
                "set firewall.fabric_flat_services_root_metrics='rule'"
            ),
        )
        self.assertLess(
            self.desired.index(
                "set firewall.fabric_flat_services_root_metrics='rule'"
            ),
            self.desired.index("set firewall.fabric_flat_services_api='rule'"),
        )

    def test_fence_contract_binds_exact_uci_and_early_drop_payloads(self) -> None:
        embedded = re.search(
            r"<<'FENCE_TRANSITION'\n(?P<body>.*?)\nFENCE_TRANSITION",
            self.helper,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(embedded)
        fence_transition = (embedded.group("body") + "\n").encode()
        transition_digest = hashlib.sha256(fence_transition).hexdigest()
        guard_digest = hashlib.sha256(FENCE_GUARD.read_bytes()).hexdigest()
        contract_digest = hashlib.sha256(
            f"uci:{transition_digest}\nguard:{guard_digest}\n".encode()
        ).hexdigest()
        self.assertIn(
            f"readonly fence_transition_sha256={transition_digest}", self.helper
        )
        self.assertIn(f"readonly fence_guard_sha256={guard_digest}", self.helper)
        self.assertIn(
            f"readonly fence_contract_sha256={contract_digest}", self.helper
        )
        self.assertIn(
            'expected_confirmation="$fence_confirmation_prefix:$head:'
            '$fence_contract_sha256"',
            self.helper,
        )
        self.assertEqual(
            self.fence_guard.splitlines(),
            [
                "ip saddr { 10.66.1.10, 10.66.1.11 } ip daddr { "
                "10.66.0.10, 10.66.0.11, 10.66.0.12 } tcp dport 2379 "
                'counter drop comment "fabric etcd fence client requests"',
                "ip saddr { 10.66.0.10, 10.66.0.11, 10.66.0.12 } "
                "ip daddr { 10.66.1.10, 10.66.1.11 } tcp sport 2379 "
                'counter drop comment "fabric etcd fence server replies"',
            ],
        )

    def test_fence_check_executes_exit_trap_without_fallthrough(self) -> None:
        start = self.helper.index("run_fence() {")
        end = self.helper.index("\n}\n\ncase $mode in", start) + 2
        run_fence = self.helper[start:end]
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            fake_repo = root / "repo"
            fake_scripts = fake_repo / "scripts"
            fake_scripts.mkdir(parents=True)
            root_check = fake_scripts / "rollout-fabric-root-firewall"
            root_check.write_text("#!/bin/sh\nprintf 'ROOT_CHECK=pass\\n'\n")
            os.chmod(root_check, 0o700)
            evidence = root / "evidence"
            evidence.mkdir()
            trace = root / "trace"
            harness = f"""
set -euo pipefail
repo_root={fake_repo}
evidence_dir={evidence}
trace={trace}
mode=fence-check
head={'1' * 40}
fence_transition_sha256={'2' * 64}
fence_guard_sha256={'3' * 64}
fence_contract_sha256={'4' * 64}
fence_remote_work=/never/work
fence_remote_init=/never/init
expected_confirmation=FENCE-CONFIRMATION
fence_check=false
root_nodes=(cp1 cp2 cp3)
die() {{ printf 'DIE:%s\\n' "$*" >&2; exit 1; }}
note() {{ printf '%s\\n' "$*"; }}
observer_verify() {{ printf 'observer\\n' >>"$trace"; }}
prepare_router_ssh() {{ printf 'ssh-preflight\\n' >>"$trace"; }}
probe_fabric_api() {{ printf 'api:%s\\n' "$1" >>"$trace"; return 0; }}
inspect_post_open_probe() {{ post_open_probe_present=false; }}
operator_kubectl() {{
  printf 'kubectl:%s\\n' "$*" >>"$trace"
  case " $* " in *' get --raw=/readyz '*) printf 'ok\\n' ;; esac
}}
validate_remote_phase() {{ printf 'phase:%s\\n' "$1" >>"$trace"; : >"$2"; }}
capture_exact_root_guards() {{ printf 'roots:%s\\n' "$1" >>"$trace"; }}
acquire_network_lock() {{ printf 'MUTATION:host-lock\\n' >>"$trace"; return 91; }}
acquire_cluster_network_lock() {{
  printf 'MUTATION:cluster-lock\\n' >>"$trace"
  return 92
}}
router_ssh() {{ printf 'MUTATION:router\\n' >>"$trace"; return 93; }}
{run_fence}
case $mode in
  fence-check | fence-run)
    run_fence
    exit 0
    ;;
esac
printf 'MUTATION:fallthrough\\n' >>"$trace"
exit 94
"""
            result = subprocess.run(
                ["bash"], input=harness, text=True, capture_output=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Read-only fence preflight passed", result.stdout)
            self.assertNotIn("unbound variable", result.stderr)
            self.assertFalse(evidence.exists())
            self.assertNotIn("MUTATION:", trace.read_text())

    def test_api_down_fence_reaches_router_without_global_lock(self) -> None:
        start = self.helper.index("run_fence() {")
        end = self.helper.index("\n}\n\ncase $mode in", start) + 2
        run_fence = self.helper[start:end]
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            fake_repo = root / "repo"
            fake_scripts = fake_repo / "scripts"
            fake_scripts.mkdir(parents=True)
            root_check = fake_scripts / "rollout-fabric-root-firewall"
            root_check.write_text("#!/bin/sh\nprintf 'ROOT_CHECK=pass\\n'\n")
            os.chmod(root_check, 0o700)
            evidence = root / "evidence"
            evidence.mkdir()
            transition = root / "fence.uci"
            transition.write_text("delete firewall.open\n")
            guard = root / "fence.nft"
            guard.write_text(self.fence_guard)
            trace = root / "trace"
            harness = f"""
set -euo pipefail
repo_root={fake_repo}
evidence_dir={evidence}
trace={trace}
mode=fence-run
head={'1' * 40}
fence_transition_sha256={'2' * 64}
fence_guard_sha256={'3' * 64}
fence_contract_sha256={'4' * 64}
fence_transition_file={transition}
fence_guard_file={guard}
fence_remote_work=/etc/fabric-router-etcd-policy-fence
fence_remote_init=/etc/init.d/fabric-router-etcd-policy-fence
fence_remote_guard=/usr/share/nftables.d/chain-pre/forward/fence.nft
fence_remote_operation_lock=/tmp/fabric-router-etcd-policy-fence.operation
expected_confirmation=FENCE-CONFIRMATION
fence_check=false
post_open_probe_present=false
root_nodes=(cp1 cp2 cp3)
die() {{ printf 'DIE:%s\\n' "$*" >&2; exit 1; }}
note() {{ printf '%s\\n' "$*" >>"$trace"; }}
observer_verify() {{ printf 'OBSERVER\\n' >>"$trace"; }}
prepare_router_ssh() {{ printf 'SSH-PINNED\\n' >>"$trace"; }}
probe_fabric_api() {{ printf 'API-DOWN:%s\\n' "$1" >>"$trace"; return 1; }}
inspect_post_open_probe() {{ printf 'TABLES:%s\\n' "$1" >>"$trace"; }}
validate_remote_phase() {{ printf 'PHASE:%s\\n' "$1" >>"$trace"; : >"$2"; }}
capture_exact_root_guards() {{ printf 'ROOTS:%s\\n' "$1" >>"$trace"; }}
acquire_network_lock() {{ printf 'HOST-LOCK\\n' >>"$trace"; }}
try_acquire_fence_cluster_lock() {{
  printf 'CLUSTER-LOCK-ATTEMPT\\n' >>"$trace"
  return 1
}}
release_cluster_network_lock() {{ printf 'CLUSTER-LOCK-RELEASE\\n' >>"$trace"; }}
router_ssh() {{ printf 'ROUTER-MUTATION\\n' >>"$trace"; return 97; }}
{run_fence}
run_fence
"""
            result = subprocess.run(
                ["bash"], input=harness, text=True, capture_output=True
            )
            trace_text = trace.read_text()
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("unbound variable", result.stderr)
            self.assertIn("API-DOWN:preflight", trace_text)
            self.assertIn("API-DOWN:before-lock", trace_text)
            self.assertIn("HOST-LOCK", trace_text)
            self.assertIn("ROUTER-MUTATION", trace_text)
            self.assertNotIn("CLUSTER-LOCK-ATTEMPT", trace_text)
            normal_path = self.helper.index(
                'cluster_lock_holder="$(hostname):$$:router-etcd-policy:'
            )
            self.assertIn(
                "acquire_cluster_network_lock", self.helper[normal_path:]
            )

    def test_existing_global_lock_holder_is_preserved_but_does_not_block_fence(self) -> None:
        run_start = self.helper.index("run_fence() {")
        run_end = self.helper.index("\n}\n\ncase $mode in", run_start) + 2
        run_fence = self.helper[run_start:run_end]
        lock_function = re.search(
            r"(?ms)^try_acquire_fence_cluster_lock\(\) \{\n.*?^\}$",
            self.helper,
        )
        self.assertIsNotNone(lock_function)
        for holder in (
            "other-host:123:root-firewall:deadbeef",
            "old-host:456:router-etcd-fence:stale-revision:stale-contract",
        ):
            with self.subTest(holder=holder), tempfile.TemporaryDirectory() as temporary:
                root = pathlib.Path(temporary)
                fake_repo = root / "repo"
                fake_scripts = fake_repo / "scripts"
                fake_scripts.mkdir(parents=True)
                root_check = fake_scripts / "rollout-fabric-root-firewall"
                root_check.write_text("#!/bin/sh\nprintf 'ROOT_CHECK=pass\\n'\n")
                os.chmod(root_check, 0o700)
                evidence = root / "evidence"
                evidence.mkdir()
                transition = root / "fence.uci"
                transition.write_text("delete firewall.open\n")
                guard = root / "fence.nft"
                guard.write_text(self.fence_guard)
                trace = root / "trace"
                harness = f"""
set -euo pipefail
repo_root={fake_repo}
evidence_dir={evidence}
trace={trace}
mode=fence-run
head={'1' * 40}
fence_transition_sha256={'2' * 64}
fence_guard_sha256={'3' * 64}
fence_contract_sha256={'4' * 64}
fence_transition_file={transition}
fence_guard_file={guard}
fence_remote_work=/etc/fabric-router-etcd-policy-fence
fence_remote_init=/etc/init.d/fabric-router-etcd-policy-fence
fence_remote_guard=/usr/share/nftables.d/chain-pre/forward/fence.nft
fence_remote_operation_lock=/tmp/fabric-router-etcd-policy-fence.operation
fabric_network_cluster_lock=fabric-maintenance-lock
expected_confirmation=FENCE-CONFIRMATION
fence_check=false
post_open_probe_present=false
root_nodes=(cp1 cp2 cp3)
die() {{ printf 'DIE:%s\\n' "$*" >&2; exit 1; }}
note() {{ printf '%s\\n' "$*" >>"$trace"; }}
observer_verify() {{ printf 'OBSERVER\\n' >>"$trace"; }}
prepare_router_ssh() {{ printf 'SSH-PINNED\\n' >>"$trace"; }}
probe_fabric_api() {{ printf 'API-UP:%s\\n' "$1" >>"$trace"; return 0; }}
inspect_post_open_probe() {{ printf 'TABLES:%s\\n' "$1" >>"$trace"; }}
validate_remote_phase() {{ printf 'PHASE:%s\\n' "$1" >>"$trace"; : >"$2"; }}
capture_exact_root_guards() {{ printf 'ROOTS:%s\\n' "$1" >>"$trace"; }}
acquire_network_lock() {{ printf 'HOST-LOCK\\n' >>"$trace"; }}
release_cluster_network_lock() {{ printf 'CLUSTER-LOCK-RELEASE\\n' >>"$trace"; }}
operator_kubectl() {{
  printf 'KUBECTL:%s\\n' "$*" >>"$trace"
  case " $* " in
    *' create configmap '*) return 1 ;;
    *' get configmap '*) printf '%s' "$EXISTING_HOLDER" ;;
    *) return 88 ;;
  esac
}}
router_ssh() {{ printf 'ROUTER-MUTATION\\n' >>"$trace"; return 97; }}
{lock_function.group(0)}
{run_fence}
run_fence
"""
                environment = dict(os.environ, EXISTING_HOLDER=holder)
                result = subprocess.run(
                    ["bash"],
                    input=harness,
                    text=True,
                    capture_output=True,
                    env=environment,
                )
                trace_text = trace.read_text()
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("unbound variable", result.stderr)
                self.assertIn("HOST-LOCK", trace_text)
                self.assertIn("ROUTER-MUTATION", trace_text)
                self.assertIn("preserving its existing holder", trace_text)
                lock_state = (evidence / "fence-cluster-lock-state.txt").read_text()
                self.assertIn("FENCE_CLUSTER_LOCK=not-acquired", lock_state)
                self.assertIn(f"EXISTING_HOLDER={holder}", lock_state)
                self.assertNotIn("another attended router fence", result.stderr)

    def test_router_local_fence_operation_lock_refuses_concurrent_stage(self) -> None:
        stage = re.search(
            r"<<'REMOTE_FENCE_STAGE'\n(?P<body>.*?)\nREMOTE_FENCE_STAGE",
            self.helper,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(stage)
        body = stage.group("body")
        prefix = body[: body.index('\n[ -f "$firewall"')]
        self.assertLess(
            body.index('mkdir "$operation_lock"'),
            body.index('if [ -e "$staging" ]'),
        )
        self.assertIn('rm -- "$operation_lock/owner"', body)
        self.assertIn('rmdir -- "$operation_lock"', body)
        self.assertNotIn('rm -rf -- "$operation_lock"', body)

        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            os.chmod(root, 0o1777)
            operation_lock = root / "operation"
            prefix = prefix.replace(
                "/tmp/fabric-router-etcd-policy-fence.operation", "__LOCK__"
            )
            prefix = prefix.replace("/tmp", "__TMP_ROOT__")
            prefix = prefix.replace("__LOCK__", str(operation_lock))
            prefix = prefix.replace("__TMP_ROOT__", str(root))
            uid = os.getuid()
            gid = os.getgid()
            prefix = prefix.replace(
                "'0:0:1777:directory'", f"'{uid}:{gid}:1777:directory'"
            )
            prefix = prefix.replace(
                "'0:0:700:directory'", f"'{uid}:{gid}:700:directory'"
            )
            prefix = prefix.replace(
                "'0:0:600:1:regular file'",
                f"'{uid}:{gid}:600:1:regular file'",
            )
            ready = root / "ready"
            release = root / "release"
            hold_script = (
                prefix
                + '\nprintf ready >"$READY_FILE"\n'
                + 'while [ ! -e "$RELEASE_FILE" ]; do sleep 0.01; done\n'
            )
            args = [
                "/etc/fabric-router-etcd-policy-fence",
                "/etc/init.d/fabric-router-etcd-policy-fence",
                "/usr/share/nftables.d/chain-pre/forward/10-fabric-etcd-fence.nft",
                "1" * 64,
                "2" * 64,
                f"{'3' * 40}:{'4' * 64}",
                "cGF5bG9hZA==",
                "Z3VhcmQ=",
                "ZW5mb3JjZXI=",
                "aW5pdA==",
                "5" * 64,
                "6" * 64,
                str(operation_lock),
                "7" * 64,
            ]
            environment = dict(
                os.environ, READY_FILE=str(ready), RELEASE_FILE=str(release)
            )
            first = subprocess.Popen(
                ["sh", "-c", hold_script, "--", *args],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            try:
                for _ in range(200):
                    if ready.exists():
                        break
                    if first.poll() is not None:
                        break
                    time.sleep(0.01)
                self.assertTrue(
                    ready.exists(), f"first stage exited early: {first.poll()}"
                )
                second = subprocess.run(
                    ["sh", "-c", prefix, "--", *args],
                    text=True,
                    capture_output=True,
                    env=environment,
                )
                self.assertNotEqual(second.returncode, 0)
                self.assertIn(
                    "another or stale router-local fence operation exists",
                    second.stderr,
                )
                self.assertTrue(operation_lock.is_dir())
            finally:
                release.touch()
                first_stdout, first_stderr = first.communicate(timeout=5)
            self.assertEqual(first.returncode, 0, first_stderr + first_stdout)
            self.assertFalse(operation_lock.exists())

    def test_inactive_partial_staging_is_exact_or_preserved(self) -> None:
        stage = re.search(
            r"<<'REMOTE_FENCE_STAGE'\n(?P<body>.*?)\nREMOTE_FENCE_STAGE",
            self.helper,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(stage)
        body = stage.group("body")
        start = body.index('if [ -e "$staging" ] || [ -L "$staging" ]; then')
        end = body.index('\n\nmkdir "$work"', start)
        predicate = body[start:end]
        uid = os.getuid()
        gid = os.getgid()
        predicate = predicate.replace(
            "'0:0:700:directory'", f"'{uid}:{gid}:700:directory'"
        )
        predicate = predicate.replace(
            "'0:0:600:1:regular file'",
            f"'{uid}:{gid}:600:1:regular file'",
        )
        predicate = predicate.replace(
            "'0:0:700:1:regular file'",
            f"'{uid}:{gid}:700:1:regular file'",
        )
        token = f"{'1' * 40}:{'2' * 64}"
        operation_id = "3" * 64
        for unknown in (False, True):
            with self.subTest(unknown=unknown), tempfile.TemporaryDirectory() as temporary:
                staging = pathlib.Path(temporary) / "fence.staging"
                staging.mkdir(mode=0o700)
                markers = {
                    "owner": "fabric-router-etcd-policy-fence\n",
                    "expected-token": f"{token}\n",
                    "operation-id": f"{operation_id}\n",
                }
                for name, content in markers.items():
                    marker = staging / name
                    marker.write_text(content)
                    os.chmod(marker, 0o600)
                if unknown:
                    mystery = staging / "unreviewed-residue"
                    mystery.write_text("preserve me\n")
                    os.chmod(mystery, 0o600)
                harness = f"""
set -eu
staging=$1
token=$2
fail() {{ printf 'FAIL:%s\\n' "$*" >&2; exit 1; }}
{predicate}
"""
                result = subprocess.run(
                    ["sh", "-s", "--", str(staging), token],
                    input=harness,
                    text=True,
                    capture_output=True,
                )
                if unknown:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("unknown or unsafe entry", result.stderr)
                    self.assertTrue(staging.is_dir())
                    self.assertTrue((staging / "unreviewed-residue").is_file())
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(staging.exists())

    def test_completed_staging_is_hash_validated_then_atomically_promoted(self) -> None:
        init = re.search(
            r"<<'REMOTE_FENCE_INIT' \|\| true\n(?P<body>.*?)\nREMOTE_FENCE_INIT",
            self.helper,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(init)
        init_body = init.group("body")
        start = init_body.index("promote_staging() {")
        end = init_body.index("\n\nstart_service()", start)
        promote = init_body[start:end]

        for tampered in (False, True):
            with self.subTest(tampered=tampered), tempfile.TemporaryDirectory() as temporary:
                root = pathlib.Path(temporary)
                work = root / "fence"
                staging = root / "fence.staging"
                installed_init = root / "fence.init"
                staging.mkdir()
                token = f"{'1' * 40}:{'2' * 64}"
                files = {
                    "owner": b"fabric-router-etcd-policy-fence\n",
                    "operation-id": ("3" * 64 + "\n").encode(),
                    "expected-token": (token + "\n").encode(),
                    "armed": (token + "\n").encode(),
                    "firewall.fenced": b"fenced-firewall\n",
                    "early-guard.nft": b"drop exact path\n",
                    "enforce": b"#!/bin/sh\nexit 0\n",
                    "init": b"#!/bin/sh\nexit 0\n",
                }
                for name, content in files.items():
                    (staging / name).write_bytes(content)
                installed_init.write_bytes(files["init"])
                for payload, manifest in (
                    ("firewall.fenced", "firewall.fenced.sha256"),
                    ("early-guard.nft", "early-guard.nft.sha256"),
                    ("enforce", "enforce.sha256"),
                    ("init", "init.sha256"),
                ):
                    digest = hashlib.sha256((staging / payload).read_bytes()).hexdigest()
                    (staging / manifest).write_text(f"{digest}  {payload}\n")
                if tampered:
                    (staging / "init").write_text("#!/bin/sh\nexit 99\n")
                candidate = promote.replace(
                    "/etc/fabric-router-etcd-policy-fence", "__WORK__"
                )
                candidate = candidate.replace(
                    "/etc/init.d/fabric-router-etcd-policy-fence", "__INIT__"
                )
                candidate = candidate.replace("__WORK__", str(work))
                candidate = candidate.replace("__INIT__", str(installed_init))
                result = subprocess.run(
                    ["sh"],
                    input=candidate + "\npromote_staging\n",
                    text=True,
                    capture_output=True,
                )
                if tampered:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(work.exists())
                    self.assertTrue(staging.is_dir())
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(work.is_dir())
                    self.assertFalse(staging.exists())

    def test_force_fence_host_anchors_init_before_any_execution(self) -> None:
        force = re.search(
            r"<<'REMOTE_FORCE_FENCE'\n(?P<body>.*?)\nREMOTE_FORCE_FENCE",
            self.helper,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(force)
        body = force.group("body")
        branch_start = body.index('if [ ! -e "$work" ] && [ ! -L "$work" ]; then')
        branch_end = body.index('\n[ -d "$work" ]', branch_start)
        branch = body[branch_start:branch_end]
        first_enable = branch.index('"$init" enable')
        for anchor in (
            'sha256sum "$staging/transition.uci"',
            'sha256sum "$staging/early-guard.nft"',
            'sha256sum "$staging/enforce"',
            'sha256sum "$staging/init"',
            "'0:0:700:1:regular file'",
            'cmp -s "$staging/init" "$init"',
        ):
            with self.subTest(anchor=anchor):
                self.assertLess(branch.index(anchor), first_enable)
        promoted_anchor = body.index('sha256sum "$work/init"')
        self.assertLess(promoted_anchor, body.rindex('"$init" enable'))

        reviewed_init = (
            b"#!/bin/sh\n"
            b"printf '%s\\n' \"$1\" >>\"$EXECUTION_TRACE\"\n"
            b"exit 0\n"
        )
        for tampered in (False, True):
            with self.subTest(tampered=tampered), tempfile.TemporaryDirectory() as temporary:
                root = pathlib.Path(temporary)
                work = root / "fence"
                staging = root / "fence.staging"
                staging.mkdir(mode=0o700)
                installed_init = root / "fence.init"
                execution_trace = root / "executed"
                token = f"{'1' * 40}:{'2' * 64}"
                operation_id = "3" * 64
                payloads = {
                    "transition.uci": b"delete exact-open-rule\n",
                    "early-guard.nft": b"drop exact path\n",
                    "enforce": b"#!/bin/sh\nexit 0\n",
                    "init": reviewed_init,
                    "firewall.fenced": b"fenced firewall\n",
                }
                expected = {
                    name: hashlib.sha256(content).hexdigest()
                    for name, content in payloads.items()
                }
                if tampered:
                    payloads["init"] = (
                        b"#!/bin/sh\n"
                        b"printf compromised >>\"$EXECUTION_TRACE\"\n"
                        b"exit 0\n"
                    )
                markers = {
                    "owner": b"fabric-router-etcd-policy-fence\n",
                    "expected-token": (token + "\n").encode(),
                    "operation-id": (operation_id + "\n").encode(),
                    "armed": (token + "\n").encode(),
                }
                for name, content in {**payloads, **markers}.items():
                    (staging / name).write_bytes(content)
                installed_init.write_bytes(payloads["init"])
                os.chmod(installed_init, 0o700)
                for payload, manifest in (
                    ("firewall.fenced", "firewall.fenced.sha256"),
                    ("early-guard.nft", "early-guard.nft.sha256"),
                    ("enforce", "enforce.sha256"),
                    ("init", "init.sha256"),
                ):
                    digest = hashlib.sha256((staging / payload).read_bytes()).hexdigest()
                    (staging / manifest).write_text(f"{digest}  {payload}\n")
                uid = os.getuid()
                gid = os.getgid()
                candidate = branch.replace(
                    "'0:0:700:directory'", f"'{uid}:{gid}:700:directory'"
                )
                candidate = candidate.replace(
                    "'0:0:700:1:regular file'",
                    f"'{uid}:{gid}:700:1:regular file'",
                )
                harness = f"""
set -eu
work=$1
init=$2
token=$3
payload_sha=$4
guard_sha=$5
enforcer_sha=$6
init_sha=$7
{candidate}
"""
                result = subprocess.run(
                    [
                        "sh",
                        "-s",
                        "--",
                        str(work),
                        str(installed_init),
                        token,
                        expected["transition.uci"],
                        expected["early-guard.nft"],
                        expected["enforce"],
                        expected["init"],
                    ],
                    input=harness,
                    text=True,
                    capture_output=True,
                    env=dict(os.environ, EXECUTION_TRACE=str(execution_trace)),
                )
                if tampered:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(execution_trace.exists())
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        execution_trace.read_text().splitlines(),
                        ["enable", "running", "running"],
                    )

    def test_exact_failed_post_open_probe_is_tolerated_then_removed(self) -> None:
        probe = re.search(
            r"<<'REMOTE_VALIDATE_POST_OPEN_PROBE'\n(?P<body>.*?)\n"
            r"REMOTE_VALIDATE_POST_OPEN_PROBE",
            self.helper,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(probe)
        roots = "10.66.0.10, 10.66.0.11, 10.66.0.12"
        rules = []
        handle = 3
        for source, node in (("10.66.1.10", "svc1"), ("10.66.1.11", "svc2")):
            for port, role in ((2379, "client"), (2380, "peer"), (2381, "metrics")):
                rules.append(
                    f"        ip saddr {source} ip daddr {{ {roots} }} "
                    f"tcp dport {port} counter packets {handle} bytes {handle * 10} "
                    f'comment "observe {node} etcd {role}; no verdict" # handle {handle}'
                )
                handle += 1
        exact = "\n".join(
            [
                "table inet fabric_etcd_post_open_probe { # handle 1",
                "    chain observe_forward { # handle 2",
                "        type filter hook forward priority -10; policy accept;",
                *rules,
                "    }",
                "}",
            ]
        )
        fixtures = {
            "exact": (exact, True),
            "verdict": (exact.replace(" counter packets 3", " accept counter packets 3", 1), False),
            "wrong_endpoint": (exact.replace("10.66.0.12", "10.66.0.99", 1), False),
            "extra_object": (exact.replace("\n}", "\n    counter probe_extra {}\n}"), False),
        }
        for name, (fixture, accepted) in fixtures.items():
            harness = f"""
nft() {{ printf '%s\\n' "$NFT_FIXTURE"; }}
{probe.group('body')}
"""
            result = subprocess.run(
                ["sh", "-s", "--", "fabric_etcd_post_open_probe"],
                input=harness,
                text=True,
                capture_output=True,
                env=dict(os.environ, NFT_FIXTURE=fixture),
            )
            with self.subTest(name=name, stderr=result.stderr):
                self.assertEqual(result.returncode == 0, accepted)
                if accepted:
                    self.assertIn(
                        "POST_OPEN_PROBE_RESIDUE=exact-no-verdict", result.stdout
                    )

        validate_start = self.helper.index("validate_remote_phase() {")
        validate_end = self.helper.index("\n}\n\nvalidate_committed_fence_guard", validate_start)
        validate = self.helper[validate_start:validate_end]
        self.assertIn("fence-*:open-live) allow_probe=true", validate)
        self.assertNotIn("post_open_probe_present", validate)
        self.assertIn(
            "table inet fabric_etcd_post_open_probe\ntable inet fw4') "
            "assert_exact_post_open_probe",
            validate,
        )
        applied = self.helper.index(
            "$fence_applied || die 'router-local enforcer did not acknowledge"
        )
        removal = self.helper.index("remove_exact_post_open_probe", applied)
        fenced_validation = self.helper.index("validate_remote_phase fenced", removal)
        self.assertLess(applied, removal)
        self.assertLess(removal, fenced_validation)

    def test_persistent_guard_rejects_live_rule_and_order_tampering(self) -> None:
        enforcer = re.search(
            r"<<'REMOTE_FENCE_ENFORCER' \|\| true\n(?P<body>.*?)\n"
            r"REMOTE_FENCE_ENFORCER",
            self.helper,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(enforcer)
        body = enforcer.group("body")
        function_start = body.index("guard_rule_exact() {")
        function_end = body.index("\nis_fenced() {", function_start)
        predicate = body[function_start:function_end]
        base = """chain forward { # handle 1
        type filter hook forward priority filter; policy drop;
        ip saddr { 10.66.1.11, 10.66.1.10 } ip daddr { 10.66.0.12, 10.66.0.10, 10.66.0.11 } tcp dport 2379 counter packets 7 bytes 800 drop comment "fabric etcd fence client requests" # handle 2
        ip saddr { 10.66.0.12, 10.66.0.11, 10.66.0.10 } ip daddr { 10.66.1.11, 10.66.1.10 } tcp sport 2379 counter packets 9 bytes 900 drop comment "fabric etcd fence server replies" # handle 3
        ct state vmap { established : accept, related : accept } comment "!fw4: Handle forwarded flows" # handle 4
        jump handle_reject comment "!fw4: reject" # handle 5
}"""
        fixtures = {
            "exact": (base, False, True),
            "extra_cidr": (
                base.replace(
                    "{ 10.66.1.11, 10.66.1.10 } ip daddr",
                    "{ 0.0.0.0/0, 10.66.1.11, 10.66.1.10 } ip daddr",
                    1,
                ),
                False,
                False,
            ),
            "comment_preserving_accept": (
                base.replace(
                    "counter packets 7 bytes 800 drop comment",
                    "counter packets 7 bytes 800 accept comment",
                    1,
                ),
                False,
                False,
            ),
            "earlier_accept": (
                base.replace(
                    "type filter hook forward priority filter; policy drop;",
                    "type filter hook forward priority filter; policy drop;\n"
                    '        ip saddr 0.0.0.0/0 accept comment "evil" '
                    "# handle 99",
                ),
                False,
                False,
            ),
            "extra_include": (base, True, False),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            guard = root / "guard.nft"
            guard.write_text(self.fence_guard)
            stock = root / "10-custom-filter-chains.nft"
            stock.write_text("# stock comment-only template\n")
            predicate = predicate.replace(
                "/etc/nftables.d/10-custom-filter-chains.nft", str(stock)
            )
            for name, (fixture, extra_include, accepted) in fixtures.items():
                harness = f"""
set -u
export LC_ALL=C
guard={guard}
guard_source={guard}
stock={stock}
find() {{
  case $1 in
    /usr/share/nftables.d)
      printf '%s\\n' "$guard"
      if [ "$EXTRA_INCLUDE" = true ]; then
        printf '%s\\n' /usr/share/nftables.d/evil.nft
      fi
      ;;
    /etc/nftables.d) printf '%s\\n' "$stock" ;;
    *) command find "$@" ;;
  esac
}}
nft() {{ printf '%s\\n' "$NFT_FIXTURE"; }}
{predicate}
guard_live
"""
                environment = dict(os.environ)
                environment.update(
                    NFT_FIXTURE=fixture,
                    EXTRA_INCLUDE="true" if extra_include else "false",
                )
                result = subprocess.run(
                    ["sh"],
                    input=harness,
                    text=True,
                    capture_output=True,
                    env=environment,
                )
                with self.subTest(name=name, stderr=result.stderr):
                    self.assertEqual(result.returncode == 0, accepted)

    def test_fence_stays_persistent_and_normal_open_rejects_residue(self) -> None:
        for required in (
            "procd_set_param respawn 3600 5 0",
            "safe_contract && { is_fenced || apply_fence; }",
            "automatic nftables include membership is not exactly the fence guard",
            "fence drops are not the first two executable forward rules",
            "fence drops do not precede every forward accept",
            "REMOTE_FENCE_PERSISTENCE",
            'note "Persistent router fence remains enabled at $fence_remote_init"',
            "assert_fence_guard_absent",
            "assert_fence_artifacts_absent",
            "persistent fence staging path is present outside the fenced phase",
            "router-local fence operation lock is present outside the fenced phase",
            "router-local fence operation lock survived staging",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.helper)
        self.assertNotIn("REMOTE_FENCE_DISARM", self.helper)
        self.assertNotIn("REMOTE_FENCE_CLEANUP", self.helper)
        for required in (
            "Persistent post-open etcd fence",
            "first two executable 'forward' rules",
            "There is intentionally no in-place unfence mode",
            "closed-policy router reprovision",
        ):
            with self.subTest(router_runbook=required):
                self.assertIn(required, self.router_readme)
        normalized_activation = " ".join(self.activation.split())
        for required in (
            "fix-forward, re-suspend, and fence only",
            "Suspending a Flux Kustomization or HelmRelease does not stop",
            "never roll the shared CRDs/chart or controller back",
            "unsuspend through a failing gate",
        ):
            with self.subTest(activation_runbook=required):
                self.assertIn(required, normalized_activation)

    def test_live_run_is_revision_payload_and_network_locked(self) -> None:
        for required in (
            "ROLLOUT-FABRIC-ROUTER-ETCD-POLICY",
            '[[ $(operator_git branch --show-current) == main ]]',
            '[[ -z $(operator_git status --porcelain) ]]',
            "operator_git fetch --quiet origin main",
            '[[ $head == "$(operator_git rev-parse origin/main)" ]]',
            'expected_confirmation="$confirmation_prefix:$head:$transition_sha256"',
            '[[ $confirm == "$expected_confirmation" ]]',
            "readonly fabric_network_lock=/run/lock/fabric-network-operation.lock",
            'flock --exclusive --nonblock "$network_lock_fd"',
            "readonly fabric_network_cluster_lock=fabric-maintenance-lock",
            "acquire_cluster_network_lock",
            "release_cluster_network_lock",
            '--from-literal="holder=$cluster_lock_holder"',
            "Global Fabric maintenance lock remains held by %s for inspection",
            "observer_verify || die 'fabric observer isolation changed before router transition'",
            "observer_verify || die 'fabric observer isolation changed after fw4 reload'",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.helper)

    def test_live_run_requalifies_pod_sources_while_holding_both_locks(self) -> None:
        normal_call = self.helper.rindex("\nqualify_pod_sources_under_parent_locks\n")
        check_exit = self.helper.index(
            "note 'Read-only preflight passed: the pre-open contract, root guards, and old router reject phase are exact.'"
        )
        acquire_host = self.helper.rindex("acquire_network_lock", 0, normal_call)
        acquire_cluster = self.helper.rindex(
            "acquire_cluster_network_lock", 0, normal_call
        )
        remote_staging = self.helper.index(
            "read -r -d '' rollback_program", normal_call
        )
        self.assertLess(check_exit, acquire_host)
        self.assertLess(acquire_host, acquire_cluster)
        self.assertLess(acquire_cluster, normal_call)
        self.assertLess(normal_call, remote_staging)

        function = re.search(
            r"(?ms)^qualify_pod_sources_under_parent_locks\(\) \{\n(?P<body>.*?)^\}$",
            self.helper,
        )
        self.assertIsNotNone(function)
        body = function.group("body")
        for required in (
            '"$repo_root/scripts/qualify-fabric-etcd-pod-sources" \\\n    --run',
            '--confirm "$exact_confirmation"',
            '--internal-parent-network-lock-fd "$network_lock_fd"',
            '--internal-parent-cluster-lock-holder "$cluster_lock_holder"',
            "fresh parent-locked Pod-source qualification failed",
            "FABRIC_ETCD_POD_SOURCE_QUALIFICATION=pass",
            '.parentLock.scope == "router-parent"',
            '.parentLock.holder == $holder',
            ".metadata.uid == $uid",
            ".metadata.resourceVersion == $resource_version",
            'flock --exclusive --nonblock "$network_lock_fd"',
        ):
            with self.subTest(required=required):
                self.assertIn(required, body)
        self.assertNotIn("sudo ", body)
        self.assertNotIn("timeout ", body)

    def test_global_lock_is_identity_bound_and_conditionally_deleted(self) -> None:
        for required in (
            '--from-literal="holder=$cluster_lock_holder" --output=json',
            "cluster_lock_uid=$(jq -er '.metadata.uid'",
            "cluster_lock_resource_version=$(jq -er '.metadata.resourceVersion'",
            "assert_cluster_network_lock_exact",
            ".metadata.uid == $uid",
            ".metadata.resourceVersion == $resource_version",
            "preconditions: {uid: $uid, resourceVersion: $resource_version}",
            '--raw="/api/v1/namespaces/kube-system/configmaps/$fabric_network_cluster_lock"',
            "--ignore-not-found --output=name",
            'parent_uid == "$cluster_lock_uid"',
            'parent_resource_version == "$cluster_lock_resource_version"',
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.helper)
        start = self.helper.index("release_cluster_network_lock() {")
        end = self.helper.index(
            "\n}\n\nqualify_pod_sources_under_parent_locks", start
        )
        release = self.helper[start:end]
        self.assertNotIn(" delete configmap ", release)
        self.assertIn("operator_kubectl delete", release)

    def test_final_activation_fence_precedes_packet_open(self) -> None:
        stage = self.helper.index("REMOTE_STAGE\n\nvalidate_remote_phase pre")
        full_final = self.helper.index(
            "verify_pre_open_contract locked-final", stage
        )
        before_arm = self.helper.index("verify_activation_fence before-arm", full_final)
        arm = self.helper.index("rollback_armed=true", before_arm)
        before_apply = self.helper.index("verify_activation_fence before-apply", arm)
        apply = self.helper.index("REMOTE_APPLY", before_apply)
        self.assertEqual(
            [stage, full_final, before_arm, arm, before_apply, apply],
            sorted([stage, full_final, before_arm, arm, before_apply, apply]),
        )
        for required in (
            "operator_git fetch --quiet origin main",
            'operator_git rev-parse HEAD) == "$head"',
            'operator_git rev-parse origin/main) == "$head"',
            "fabric-etcdetcetc-controller fabric-etcdetcetc-runtime",
            "operator_hub_kubectl",
            "etcdetcetc etcdetcetc-cloud-etcd --output=json",
            "deployments,replicasets,statefulsets,daemonsets,jobs,cronjobs,replicationcontrollers,pods",
            "--ignore-not-found --output=name",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.helper)

    def test_normal_path_rechecks_complete_pre_open_and_root_contracts(self) -> None:
        normal_start = self.helper.index(
            'cluster_lock_holder="$(hostname):$$:router-etcd-policy:'
        )
        root_check = self.helper.index("\ncheck_committed_root_firewall\n", normal_start)
        preflight_verify = self.helper.index(
            "\nverify_pre_open_contract preflight\n", normal_start
        )
        preflight_guards = self.helper.index(
            "\ncapture_exact_root_guards preflight\n", normal_start
        )
        acquire_host = self.helper.index("\nacquire_network_lock\n", preflight_guards)
        acquire_cluster = self.helper.index(
            "\nacquire_cluster_network_lock\n", acquire_host
        )
        locked_before = self.helper.index(
            "\nverify_pre_open_contract locked-before-source", acquire_cluster
        )
        source_proof = self.helper.index(
            "\nqualify_pod_sources_under_parent_locks\n", locked_before
        )
        locked_after = self.helper.index(
            "\nverify_pre_open_contract locked-after-source", source_proof
        )
        locked_guards = self.helper.index(
            "\ncapture_exact_root_guards locked\n", locked_after
        )
        remote_staging = self.helper.index(
            "read -r -d '' rollback_program", locked_guards
        )
        self.assertEqual(
            [
                root_check,
                preflight_verify,
                preflight_guards,
                acquire_host,
                acquire_cluster,
                locked_before,
                source_proof,
                locked_after,
                locked_guards,
                remote_staging,
            ],
            sorted(
                [
                    root_check,
                    preflight_verify,
                    preflight_guards,
                    acquire_host,
                    acquire_cluster,
                    locked_before,
                    source_proof,
                    locked_after,
                    locked_guards,
                    remote_staging,
                ]
            ),
        )
        for required in (
            '"$repo_root/scripts/verify-fabric-etcd-pre-open" "$@"',
            'expected="FABRIC_ETCDETCETC_PRE_OPEN=pass revision=$head"',
            '--expected-lock-holder "$cluster_lock_holder"',
            '"$repo_root/scripts/rollout-fabric-root-firewall" --check',
            'root-$((index + 1))-guard.preflight.normalized',
            'root-$((index + 1))-guard.locked.normalized',
            "fabric_guard semantics changed after preflight",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.helper)

        operator = re.search(
            r"(?ms)^operator_pre_open\(\) \{\n(?P<body>.*?)^\}$", self.helper
        )
        self.assertIsNotNone(operator)
        self.assertIn(
            'sudo --non-interactive --user "$operator_user" --',
            operator.group("body"),
        )
        self.assertIn('FABRIC_SSH_IDENTITY="$identity"', operator.group("body"))
        self.assertIn(
            'PATH="$etcdctl_dir:/usr/local/sbin:/usr/local/bin:/usr/sbin:'
            '/usr/bin:/sbin:/bin"',
            operator.group("body"),
        )

    def test_etcdctl_is_explicit_ephemeral_and_checksum_pinned(self) -> None:
        for required in (
            "readonly etcdctl_binary_sha256="
            "83040f35846861c2b121a599d7066cf847436dd63b623a91b203ee7db209c6df",
            "readonly etcdctl_version=3.6.13",
            "[[ -n $etcdctl ]] || die '--etcdctl is required'",
            "${etcdctl##*/} == etcdctl",
            '"$operator_uid:$operator_gid:755:regular file:1"',
            '"$operator_uid:$operator_gid:700:directory"',
            'stat --file-system --format=%T -- "$etcdctl_dir"',
            'sha256sum -- "$etcdctl"',
            'env -i LC_ALL=C "$etcdctl" version',
            "grep -Fxq 'API version: 3.6'",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.helper)

        validate = self.helper.index("\nvalidate_etcdctl\n")
        preflight = self.helper.index("\nverify_pre_open_contract preflight\n")
        self.assertLess(validate, preflight)

    def test_wired_observer_is_an_explicit_exact_alternative(self) -> None:
        verifier = WIRED_OBSERVER.read_text(encoding="utf-8")
        for required in (
            "readonly namespace=fabric-observer",
            "readonly observer_address=10.66.0.2/24",
            "length == 2",
            '[[ ! -e /sys/class/net/$interface ]]',
            'ethtool -P "$interface"',
            '"$services_subnet via $services_gateway dev $interface proto static '
            'src 10.66.0.2"',
            "net.ipv4.conf.all.accept_redirects",
            "net.ipv6.conf.all.disable_ipv6",
            'nft -nn list ruleset',
            "FABRIC_WIRED_OBSERVER=pass",
        ):
            with self.subTest(required=required):
                self.assertIn(required, verifier)
        observer = re.search(
            r"(?ms)^observer_verify\(\) \{\n(?P<body>.*?)^\}$", self.helper
        )
        self.assertIsNotNone(observer)
        self.assertIn('"$repo_root/fabric/observer/verify-wired-netns"', observer.group("body"))
        self.assertIn('--interface "$wired_observer_interface"', observer.group("body"))
        self.assertIn(
            '--permanent-mac "$wired_observer_permanent_mac"',
            observer.group("body"),
        )

    def test_check_path_cannot_launch_mutating_source_qualification(self) -> None:
        check_block = re.search(
            r"(?ms)^if \[\[ \$mode == check \]\]; then\n(?P<body>.*?)^fi$",
            self.helper[self.helper.index("observer_verify || die"):],
        )
        self.assertIsNotNone(check_block)
        self.assertIn("exit 0", check_block.group("body"))
        self.assertNotIn(
            "qualify_pod_sources_under_parent_locks", check_block.group("body")
        )
        invocation = self.helper.rindex("\nqualify_pod_sources_under_parent_locks\n")
        self.assertGreater(invocation, self.helper.index(check_block.group(0)))

    def test_operator_commands_remain_inside_sudo_argument_vector(self) -> None:
        for function_name in ("operator_git", "operator_kubectl"):
            function = re.search(
                rf"(?ms)^{function_name}\(\) \{{\n(?P<body>.*?)^\}}$",
                self.helper,
            )
            self.assertIsNotNone(function)
            body = function.group("body")
            with self.subTest(function=function_name):
                self.assertIn(
                    'sudo --non-interactive --user "$operator_user" -- \\\n'
                    "    env",
                    body,
                )
                self.assertNotRegex(body, r'--\n[ \t]+env')

    def test_ssh_is_serial_pinned_and_confined_to_observer_namespace(self) -> None:
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
            "-o IdentitiesOnly=yes",
            "-o PasswordAuthentication=no",
            'ip netns exec "$observer_namespace" ssh "${router_ssh_options[@]}"',
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.helper)

    def test_pre_and_post_states_are_exact_and_phase_locked(self) -> None:
        for required in (
            "assert_target_section fabric_flat_reject_services_etcd",
            "Reject-services-to-root-etcd '2379 2380' REJECT",
            "assert_uci_absent firewall.fabric_flat_services_etcd_client",
            "assert_uci_absent firewall.fabric_flat_reject_services_etcd_internal",
            "assert_target_section fabric_flat_services_etcd_client",
            "Allow-platform-to-root-etcd-client 2379 ACCEPT",
            "assert_target_section fabric_flat_reject_services_etcd_internal",
            "Reject-services-to-root-etcd-internal 2380 REJECT",
            "Allow-platform-to-root-metrics '2112 2381 9100' ACCEPT",
            "assert_root_bootie_section",
            "assert_live_rule Allow-roots-to-Bootie 'jump accept_to_lan' no",
            "assert_uci_absent firewall.fabric_flat_reject_services_etcd",
            '[ "$actual_rules" = "$expected_rules" ]',
            '[ "$actual_forward" = "$expected_forward" ]',
            "fail 'live forward_lan terminal verdict is not exact'",
            "new target rule saw traffic before acceptance",
            "TARGET_COUNTER name=%s packets=%s bytes=%s",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.helper)

        self.assertEqual(
            self.helper.count(
                "Allow-roots-to-service-kubelets\n"
                "Allow-roots-to-Bootie\n"
                "Allow-observer-to-services-SSH"
            ),
            5,
        )
        self.assertEqual(
            self.helper.count(
                "fabric_flat_roots_kubelet\n"
                "fabric_flat_roots_bootie\n"
                "fabric_flat_observer_services_ssh"
            ),
            2,
        )
        for required in (
            "expected_rule_count=27",
            "expected_chain_count=17",
            "expected_rule_count=28",
            "expected_chain_count=18",
        ):
            self.assertIn(required, self.helper)

        self.assertIn(
            'ip saddr @service_nodes_v4 tcp dport { 2112, 2381, 9100 } '
            'counter accept comment "trusted platform monitoring"',
            self.helper,
        )

    def test_candidate_and_apply_are_byte_exact_and_syntax_checked(self) -> None:
        for required in (
            'mkdir "$work/candidate-save"',
            '-t "$work/candidate-save" batch',
            "candidate validation leaked a live UCI delta",
            "sha256sum candidate/firewall >firewall.candidate.sha256",
            "live firewall config metadata is unsafe",
            'cmp -s "$firewall" "$work/firewall.before"',
            'uci -q batch <"$work/transition.uci"',
            "uci -q commit firewall",
            'cmp -s "$firewall" "$work/candidate/firewall"',
            "fw4 check >/dev/null",
            "/etc/init.d/firewall reload",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.helper)

    def test_router_local_rollback_survives_ssh_loss_and_reboot(self) -> None:
        for required in (
            "readonly remote_work=/etc/fabric-router-etcd-policy-rollback",
            "readonly remote_init=/etc/init.d/fabric-router-etcd-policy-rollback",
            "readonly rollback_seconds=300",
            "#!/bin/sh /etc/rc.common",
            "USE_PROCD=1",
            "START=15",
            "procd_set_param command /bin/sh /etc/fabric-router-etcd-policy-rollback/rollback",
            "procd_set_param respawn 3600 5 0",
            "trap 'exit 129' HUP",
            '"$init" enable',
            '"$init" running',
            "boot_id_path=/proc/sys/kernel/random/boot_id",
            'ln "$work/fence-state" "$work/terminal"',
            "install_guard_file()",
            "insert_immediate_guard()",
            "assert_closed_lan()",
            "guard_live()",
            "is_fenced()",
            "ack_fenced()",
            'comment "fabric etcd fence client requests"',
            'comment "fabric etcd fence server replies"',
            "persistent rollback fence application failed; retrying",
            'router_ssh /bin/sh -s -- "$remote_work"',
            'mv "$work/force-rollback.next" "$work/force-rollback"',
            "ROLLBACK_FENCED=",
            "persistent rollback fence was not acknowledged over SSH",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.helper)
        accept = self.helper.index("REMOTE_ACCEPT")
        self.assertNotIn("observer_verify", self.helper[accept:])

    def test_success_disarms_and_removes_only_owned_artifacts(self) -> None:
        for required in (
            '[ "$(cat "$work/owner")" = fabric-router-etcd-policy-rollout ]',
            'ln "$work/accept-state" "$work/terminal"',
            '[ "$(cat "$work/terminal")" = "accept:$token" ]',
            'ln "$work/accepted-state" "$work/accepted"',
            'ln "$work/accepted-state" "$work/accept-complete"',
            "accepted_open_exact()",
            '"$init" stop',
            "ROLLBACK_ACCEPTED=",
            "rollback_accepted=true",
            "REMOTE_ROLLBACK_CLEANUP",
            'if find /etc/rc.d -maxdepth 1 -type l -name \'*fabric-router-etcd-policy-rollback\'',
            'rm -f -- "$init"',
            'rm -rf -- "$work"',
            "ROLLBACK_CLEANED=pass",
            "rollback_armed=false",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.helper)

    def test_platform_namespaces_and_controller_egress_are_exact(self) -> None:
        restricted_labels = {
            "pod-security.kubernetes.io/audit": "restricted",
            "pod-security.kubernetes.io/audit-version": "v1.36",
            "pod-security.kubernetes.io/enforce": "restricted",
            "pod-security.kubernetes.io/enforce-version": "v1.36",
            "pod-security.kubernetes.io/warn": "restricted",
            "pod-security.kubernetes.io/warn-version": "v1.36",
        }
        self.assertEqual(
            self.platform_namespaces,
            [
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {
                        "name": name,
                        "labels": restricted_labels,
                    },
                }
                for name in ("cert-manager", "etcdetcetc", "etcdetcetc-smoke")
            ],
        )

        policies = self.platform_network_policies
        self.assertTrue(
            all(
                policy["apiVersion"] == "networking.k8s.io/v1"
                and policy["kind"] == "NetworkPolicy"
                for policy in policies
            )
        )
        self.assertEqual(
            sorted(
                (
                    policy["metadata"]["namespace"],
                    policy["metadata"]["name"],
                )
                for policy in policies
            ),
            sorted(
                (
                    ("cert-manager", "allow-dns-and-kubernetes-api"),
                    ("cert-manager", "allow-kubernetes-api-to-webhook"),
                    ("cert-manager", "default-deny"),
                    ("etcdetcetc", "allow-controller-egress"),
                    ("etcdetcetc", "default-deny"),
                    ("etcdetcetc-smoke", "default-deny"),
                )
            ),
        )
        default_denies = [
            policy
            for policy in policies
            if policy["metadata"]["name"] == "default-deny"
        ]
        self.assertEqual(
            [
                (policy["metadata"]["namespace"], policy["spec"])
                for policy in default_denies
            ],
            [
                (
                    namespace,
                    {"podSelector": {}, "policyTypes": ["Egress", "Ingress"]},
                )
                for namespace in ("cert-manager", "etcdetcetc", "etcdetcetc-smoke")
            ],
        )

        webhook = next(
            policy
            for policy in policies
            if policy["metadata"]
            == {
                "name": "allow-kubernetes-api-to-webhook",
                "namespace": "cert-manager",
            }
        )
        self.assertEqual(
            webhook["spec"],
            {
                "podSelector": {
                    "matchLabels": {
                        "app.kubernetes.io/component": "webhook",
                        "app.kubernetes.io/instance": "cert-manager",
                        "app.kubernetes.io/name": "webhook",
                    }
                },
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [
                            {"ipBlock": {"cidr": "10.66.0.10/32"}},
                            {"ipBlock": {"cidr": "10.66.0.11/32"}},
                            {"ipBlock": {"cidr": "10.66.0.12/32"}},
                            {"ipBlock": {"cidr": "10.66.0.254/32"}},
                            {"ipBlock": {"cidr": "172.22.10.0/32"}},
                            {"ipBlock": {"cidr": "172.22.0.0/32"}},
                            {"ipBlock": {"cidr": "172.22.1.0/32"}},
                        ],
                        "ports": [{"port": 10250, "protocol": "TCP"}],
                    }
                ],
            },
        )

        controller = next(
            policy
            for policy in policies
            if policy["metadata"]
            == {"name": "allow-controller-egress", "namespace": "etcdetcetc"}
        )
        self.assertEqual(
            controller["spec"],
            {
                "podSelector": {
                    "matchLabels": {
                        "app.kubernetes.io/component": "controller",
                        "app.kubernetes.io/name": "etcdetcetc",
                    }
                },
                "policyTypes": ["Egress"],
                "egress": [
                    {
                        "to": [
                            {
                                "namespaceSelector": {
                                    "matchLabels": {
                                        "kubernetes.io/metadata.name": "kube-system"
                                    }
                                },
                                "podSelector": {
                                    "matchLabels": {"k8s-app": "kube-dns"}
                                },
                            }
                        ],
                        "ports": [
                            {"port": 53, "protocol": "UDP"},
                            {"port": 53, "protocol": "TCP"},
                        ],
                    },
                    {
                        "to": [{"ipBlock": {"cidr": "172.21.0.10/32"}}],
                        "ports": [
                            {"port": 53, "protocol": "UDP"},
                            {"port": 53, "protocol": "TCP"},
                        ],
                    },
                    {
                        "to": [{"ipBlock": {"cidr": "172.21.0.1/32"}}],
                        "ports": [{"port": 443, "protocol": "TCP"}],
                    },
                    {
                        "to": [
                            {"ipBlock": {"cidr": "10.66.0.10/32"}},
                            {"ipBlock": {"cidr": "10.66.0.11/32"}},
                            {"ipBlock": {"cidr": "10.66.0.12/32"}},
                            {"ipBlock": {"cidr": "10.66.0.254/32"}},
                        ],
                        "ports": [{"port": 6443, "protocol": "TCP"}],
                    },
                    {
                        "to": [
                            {"ipBlock": {"cidr": "10.66.0.10/32"}},
                            {"ipBlock": {"cidr": "10.66.0.11/32"}},
                            {"ipBlock": {"cidr": "10.66.0.12/32"}},
                        ],
                        "ports": [{"port": 2379, "protocol": "TCP"}],
                    },
                ],
            },
        )

    def test_flux_activation_phase_is_monotonic_and_runtime_stays_gated(self) -> None:
        policy_document = next(
            document
            for document in self.children.split("\n---\n")
            if re.search(
                r"(?m)^metadata:\n  name: fabric-etcdetcetc-policy$", document
            )
        )
        config_document = next(
            document
            for document in self.children.split("\n---\n")
            if re.search(
                r"(?m)^metadata:\n  name: fabric-etcdetcetc-config$", document
            )
        )
        controller_document = next(
            document
            for document in self.children.split("\n---\n")
            if re.search(
                r"(?m)^metadata:\n  name: fabric-etcdetcetc-controller$", document
            )
        )
        runtime_document = next(
            document
            for document in self.children.split("\n---\n")
            if re.search(
                r"(?m)^metadata:\n  name: fabric-etcdetcetc-runtime$", document
            )
        )
        release = next(
            document
            for document in yaml.safe_load_all(self.controller_release)
            if document.get("metadata", {}).get("name") == "etcdetcetc"
        )

        def suspended(document: str) -> bool:
            return re.search(r"(?m)^  suspend: true$", document) is not None

        activation_state = (
            suspended(config_document),
            suspended(controller_document),
            suspended(runtime_document),
            release["spec"].get("suspend", False),
        )
        phases = {
            (True, True, True, True): "staged",
            (False, True, True, True): "config-active",
            (False, False, True, False): "controller-active",
            (False, False, False, False): "runtime-active",
        }
        self.assertIn(activation_state, phases)
        activation_phase = phases[activation_state]

        self.assertIn("- name: fabric-platform", policy_document)
        self.assertNotIn("suspend: true", policy_document)
        self.assertIn("- name: fabric-etcdetcetc-policy", config_document)
        self.assertNotIn("- name: fabric-etcdetcetc-controller", config_document)
        self.assertIn("- name: fabric-etcdetcetc-config", controller_document)
        self.assertIn("- name: fabric-etcdetcetc-config", runtime_document)
        self.assertIn("- name: fabric-etcdetcetc-controller", runtime_document)
        self.assertNotIn("cluster.yaml", self.bootstrap_kustomization)
        self.assertNotIn("smoke-tenant.yaml", self.bootstrap_kustomization)
        self.assertIn("cluster.yaml", self.runtime_kustomization)
        self.assertIn("smoke-tenant.yaml", self.runtime_kustomization)
        self.assertEqual(
            release["spec"]["values"]["affinity"],
            {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {
                                "matchFields": [
                                    {
                                        "key": "metadata.name",
                                        "operator": "In",
                                        "values": [
                                            "fabric-az1-svc1",
                                            "fabric-az1-svc2",
                                        ],
                                    }
                                ]
                            }
                        ]
                    }
                }
            },
        )

        if activation_phase in ("controller-active", "runtime-active"):
            image = release["spec"]["values"]["image"]
            self.assertRegex(image["tag"], r"^[0-9]{10}(\.[0-9]+)?$")
            self.assertRegex(image["digest"], r"^sha256:[0-9a-f]{64}$")
            self.assertNotEqual(image["tag"], BOOTSTRAP_IMAGE_TAG)
            self.assertNotEqual(image["digest"], BOOTSTRAP_IMAGE_DIGEST)

        ordered_markers = (
            "qualify-fabric-etcd-pod-sources",
            "Remove only `fabric-etcdetcetc-config.spec.suspend`",
            "Roll the public delegated client trust",
            "provision the exact passwordless",
            "rollout-fabric-root-firewall",
            "rollout-fabric-router-etcd-policy",
            "Remove both controller suspension gates",
            "fabric-etcdetcetc-runtime.spec.suspend",
            "qualify-fabric-etcd-post-open",
        )
        positions = [self.activation.index(marker) for marker in ordered_markers]
        self.assertEqual(positions, sorted(positions))

    def test_foundation_rollback_accounts_for_nonpruning_root(self) -> None:
        self.assertIs(self.root_kustomization["spec"]["prune"], False)
        children = {
            document["metadata"]["name"]: document
            for document in yaml.safe_load_all(self.children)
        }
        for name in (
            "fabric-platform",
            "fabric-etcdetcetc-policy",
            "fabric-etcdetcetc-config",
            "fabric-etcdetcetc-controller",
            "fabric-etcdetcetc-runtime",
        ):
            with self.subTest(name=name):
                self.assertIs(children[name]["spec"]["prune"], True)

        for required in (
            "A plain revert of the foundation commit is not a rollback.",
            "`etcdetcetc-policy/kustomization.yaml` to `resources: []`",
            "Do not suspend the\n   `cert-manager/cert-manager` HelmRelease",
            "generated ReplicaSets and Pods",
            "change `network-policies.yaml` to retain exactly",
            "`etcdetcetc/allow-controller-egress`",
            "Secret `cert-manager/cert-manager-webhook-ca`",
            "`kube-system/cert-manager-controller`",
            "`kube-system/cert-manager-cainjector-leader-election`",
            "unchanged for longer than their lease\n   durations",
            "explicit mutation\n   authorization",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.activation)

    def test_offline_verifier_accepts_only_the_four_activation_phases(self) -> None:
        verifier = (
            REPO_ROOT
            / "fabric"
            / "pki"
            / "etcdetcetc"
            / "verify-foundation"
        ).read_text(encoding="utf-8")
        expected_cases = (
            "true:true:true:true) activation_phase=staged ;;",
            "false:true:true:true) activation_phase=config-active ;;",
            "false:false:true:false) activation_phase=controller-active ;;",
            "false:false:false:false) activation_phase=runtime-active ;;",
        )
        for expected in expected_cases:
            with self.subTest(expected=expected):
                self.assertIn(expected, verifier)
        self.assertIn("read_suspend_state()", verifier)
        self.assertIn(
            "[[ $state == true || $state == false ]]",
            verifier,
        )
        self.assertNotRegex(
            verifier,
            r"(?m)^(config|controller|runtime|release)_suspended=\$\(yq -er",
        )
        self.assertIn(
            "inconsistent Fabric etcdetcetc activation gates", verifier
        )
        self.assertIn(
            "active Fabric controller must use a canonical non-bootstrap "
            "tag-and-digest image pin",
            verifier,
        )


if __name__ == "__main__":
    unittest.main()
