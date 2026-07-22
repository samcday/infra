#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import os
import pathlib
import re
import subprocess
import tempfile
import unittest


REPO_ROOT = pathlib.Path(__file__).parents[3]


class SystemdRollbackStateProtocolTests(unittest.TestCase):
    """Regression contract for the two root-host systemd rollback fences."""

    HELPERS = {
        "root-firewall": {
            "path": REPO_ROOT / "scripts" / "rollout-fabric-root-firewall",
            "owner": "fabric-root-network-policy-rollout",
            "work": "/var/lib/fabric-root-network-policy-rollback",
            "unit": "fabric-root-network-policy-rollback",
            "sweep_marker": "assert_live_etcd_input_policy",
            "restore_markers": (
                "restore_scope ipv4",
                'restore_file "$firewall_path" "$firewall_previous"',
                "systemctl restart fabric-guard.service",
            ),
        },
        "client-trust": {
            "path": REPO_ROOT
            / "scripts"
            / "rollout-fabric-etcd-client-trust",
            "owner": "fabric-etcd-client-trust-rollout",
            "work": "/var/lib/fabric-etcd-client-trust-rollback",
            "unit": "fabric-etcd-client-trust-rollback",
            "sweep_marker": "verify_live_trust",
            "restore_markers": (
                'mv --force -- "$next" "$target"',
                "systemctl daemon-reload",
                "systemctl restart --no-block etcd.service",
            ),
        },
    }

    PROGRAMS = (
        "apply_candidate_program",
        "rollback_program",
        "force_rollback_program",
        "disarm_ready_program",
        "disarm_program",
        "rollback_cleanup_program",
    )

    @classmethod
    def setUpClass(cls) -> None:
        cls.helpers = {
            name: {
                **contract,
                "text": contract["path"].read_text(encoding="utf-8"),
            }
            for name, contract in cls.HELPERS.items()
        }

    def program(self, helper: str, name: str) -> str:
        text = self.helpers[helper]["text"]
        match = re.search(
            rf"(?ms)^[ \t]*{re.escape(name)}='(?P<body>.*?)^[ \t]*'$",
            text,
        )
        self.assertIsNotNone(match, f"{helper}: missing {name}")
        return match.group("body")

    def function(self, body: str, name: str) -> str:
        match = re.search(
            rf"(?ms)^{re.escape(name)}\(\) \{{\n(?P<body>.*?)^\}}$",
            body,
        )
        self.assertIsNotNone(match, f"missing {name} function")
        return match.group("body")

    def assert_ordered(self, body: str, *markers: str) -> None:
        missing = [marker for marker in markers if marker not in body]
        self.assertEqual(missing, [], f"missing ordered markers: {missing}")
        positions = [body.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions))

    def assert_state_lock(self, body: str) -> None:
        self.assertIn("state_lock=$work/state.lock", body)
        self.assertRegex(
            body,
            r'exec \{state_fd\}>>"\$(?:state_lock|work/state\.lock)"',
        )
        self.assertIn('flock --exclusive "$state_fd"', body)

    def assert_no_queued_job_check(self, body: str) -> None:
        self.assertIn("--property=Job", body)
        self.assertRegex(
            body,
            r"(?:-z \$\(systemctl show --property=Job|"
            r"\[\[ -z \$[A-Za-z_][A-Za-z0-9_]* \]\])",
        )

    def assert_exact_inactive_checks(
        self, body: str, minimum: int = 2
    ) -> None:
        self.assertGreaterEqual(body.count("--property=ActiveState"), minimum)
        self.assertGreaterEqual(body.count("== inactive"), minimum)

    def run_boot_fixture(
        self,
        helper: str,
        state: str,
        *,
        client_trust_failure: str | None = None,
    ) -> tuple[str, str, int, bool]:
        contract = self.helpers[helper]
        rollback = self.program(helper, "rollback_program")
        token = "fixture-token"
        if client_trust_failure is not None:
            self.assertEqual(helper, "client-trust")
            self.assertIn(
                client_trust_failure,
                {"api-timeout", "api-not-ready", "lease-unchanged"},
            )
            long_deadline = "rollback_deadline=$((now + 240))"
            short_deadline = "rollback_deadline=$((now + 2))"
            self.assertIn(long_deadline, rollback)
            rollback = rollback.replace(long_deadline, short_deadline, 1)
            if client_trust_failure == "api-timeout":
                long_timeout = "--kill-after=2s 5s"
                short_timeout = "--kill-after=0.05s 0.05s"
                self.assertIn(long_timeout, rollback)
                rollback = rollback.replace(long_timeout, short_timeout, 1)

        # /tmp is tmpfs without SELinux labels on the FCOS test host. The
        # rollback contract intentionally records %C, so use labeled /var/tmp.
        with tempfile.TemporaryDirectory(dir="/var/tmp") as temporary:
            root = pathlib.Path(temporary)
            work = root / "work"
            fake_bin = root / "bin"
            targets = root / "targets"
            work.mkdir()
            fake_bin.mkdir()
            targets.mkdir()
            log = root / "systemctl.log"

            fake_systemctl = fake_bin / "systemctl"
            fake_systemctl.write_text(
                '#!/usr/bin/env bash\n'
                'set -euo pipefail\n'
                'printf "%s\\n" "$*" >>"$SYSTEMCTL_LOG"\n'
                'case "$*" in\n'
                '  "show --no-pager --property=Requires --property=After '
                '--property=BindsTo --property=PartOf --property=Requisite '
                '--property=StopPropagatedFrom etcd.service") '
                'printf "Requires=fabric-etcd-client-trust.service\\n'
                'After=fabric-etcd-client-trust.service\\nBindsTo=\\n'
                'PartOf=\\nRequisite=\\nStopPropagatedFrom=\\n" ;;\n'
                '  "show --property=Result --value "*) '
                'printf "success\\n" ;;\n'
                '  "show --property=ActiveState --value "*) '
                'printf "inactive\\n" ;;\n'
                'esac\n',
                encoding="utf-8",
            )
            fake_systemctl.chmod(0o700)
            fake_sync = fake_bin / "sync"
            fake_sync.write_text(
                "#!/usr/bin/env bash\nset -euo pipefail\n",
                encoding="utf-8",
            )
            fake_sync.chmod(0o700)
            fake_cp = fake_bin / "cp"
            fake_cp.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                'source=${@: -2:1}\n'
                'target=${@: -1}\n'
                'exec /usr/bin/cp --preserve=mode,ownership,timestamps '
                '-- "$source" "$target"\n',
                encoding="utf-8",
            )
            fake_cp.chmod(0o700)
            fake_stat = fake_bin / "stat"
            fake_stat.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                'if [[ ${1-} == *%C* ]]; then\n'
                '  /usr/bin/stat --format="%u:%g:%a:%h:%F" "${@: -1}" | '
                '/usr/bin/sed "s/$/:fixture_context/"\n'
                "else\n"
                '  exec /usr/bin/stat "$@"\n'
                "fi\n",
                encoding="utf-8",
            )
            fake_stat.chmod(0o700)
            fake_matchpathcon = fake_bin / "matchpathcon"
            fake_matchpathcon.write_text(
                "#!/usr/bin/env bash\nset -euo pipefail\n",
                encoding="utf-8",
            )
            fake_matchpathcon.chmod(0o700)

            for name, value in (
                ("owner", contract["owner"]),
                ("expected-token", token),
                ("armed", token),
                ("deadline", "0"),
                ("state", f"{state}:{token}"),
            ):
                (work / name).write_text(value + "\n", encoding="utf-8")
            (work / "state.lock").touch()

            replacements = {contract["work"]: str(work)}
            target_paths: list[pathlib.Path] = []
            if helper == "root-firewall":
                firewall = targets / "fabric-guard.nft"
                routing = targets / "root-routing.conf"
                target_paths.extend((firewall, routing))
                replacements.update(
                    {
                        "/etc/nftables/fabric-guard.nft": str(firewall),
                        "/etc/sysctl.d/90-fabric-root-routing.conf": str(
                            routing
                        ),
                    }
                )
                fake_sysctl = fake_bin / "sysctl"
                fake_sysctl.write_text(
                    '#!/usr/bin/env bash\n'
                    'set -euo pipefail\n'
                    'if [[ ${1-} == -n ]]; then printf "1\\n"; fi\n',
                    encoding="utf-8",
                )
                fake_sysctl.chmod(0o700)
                replacements["/usr/sbin/sysctl"] = str(fake_sysctl)

                firewall_previous = work / "firewall.previous"
                firewall_previous.write_text(
                    "fixture firewall\n", encoding="utf-8"
                )
                firewall_metadata = subprocess.run(
                    [
                        "stat",
                        "--format=%u:%g:%a:%h:%F",
                        str(firewall_previous),
                    ],
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout.rstrip("\n") + ":fixture_context\n"
                (work / "firewall.previous.metadata").write_text(
                    firewall_metadata, encoding="utf-8"
                )
                firewall_digest = hashlib.sha256(
                    firewall_previous.read_bytes()
                ).hexdigest()
                firewall_metadata_digest = hashlib.sha256(
                    (work / "firewall.previous.metadata").read_bytes()
                ).hexdigest()
                (work / "firewall.previous.sha256").write_text(
                    f"{firewall_digest}  firewall.previous\n"
                    f"{firewall_metadata_digest}  "
                    "firewall.previous.metadata\n",
                    encoding="utf-8",
                )
                live_previous = work / "routing-live.previous"
                live_previous.write_text(
                    "net/ipv4/ip_forward = 1\n"
                    "net/ipv4/conf/default/accept_redirects = 1\n"
                    "net/ipv4/conf/default/secure_redirects = 1\n"
                    "net/ipv4/conf/default/send_redirects = 1\n"
                    "net/ipv4/conf/default/accept_source_route = 1\n"
                    "net/ipv6/conf/default/accept_redirects = 1\n"
                    "net/ipv6/conf/default/accept_source_route = 1\n",
                    encoding="utf-8",
                )
                live_digest = hashlib.sha256(
                    live_previous.read_bytes()
                ).hexdigest()
                (work / "routing-live.previous.sha256").write_text(
                    f"{live_digest}  routing-live.previous\n",
                    encoding="utf-8",
                )
                firewall_live_previous = work / "firewall-live.previous"
                firewall_live_previous.write_text(
                    "fixture live firewall\n", encoding="utf-8"
                )
                firewall_live_digest = hashlib.sha256(
                    firewall_live_previous.read_bytes()
                ).hexdigest()
                (work / "firewall-live.previous.sha256").write_text(
                    f"{firewall_live_digest}  firewall-live.previous\n",
                    encoding="utf-8",
                )
                fake_nft = fake_bin / "nft"
                fake_nft.write_text(
                    "#!/usr/bin/env bash\n"
                    "set -euo pipefail\n"
                    'printf "fixture live firewall\\n"\n',
                    encoding="utf-8",
                )
                fake_nft.chmod(0o700)
                replacements["/usr/sbin/nft"] = str(fake_nft)
                (work / "routing.previous-absent").touch()
            else:
                trust_targets = {
                    "/etc/etcd/pki/etcdetcetc-client-ca.pem": "delegated-ca",
                    "/usr/local/sbin/render-fabric-etcd-client-trust": "renderer",
                    "/etc/systemd/system/fabric-etcd-client-trust.service":
                        "client-trust.service",
                    "/etc/systemd/system/etcd.service": "etcd.service",
                }
                for production_path, backup_name in trust_targets.items():
                    target = targets / backup_name
                    replacements[production_path] = str(target)
                    target_paths.append(target)
                    previous = work / f"{backup_name}.previous"
                    previous.write_text(
                        f"previous {backup_name}\n", encoding="utf-8"
                    )
                    metadata = subprocess.run(
                        [
                            "stat",
                            "--format=%u:%g:%a:%h:%F",
                            str(previous),
                        ],
                        check=True,
                        text=True,
                        capture_output=True,
                    ).stdout.rstrip("\n") + ":fixture_context\n"
                    metadata_path = work / f"{backup_name}.previous.metadata"
                    metadata_path.write_text(metadata, encoding="utf-8")
                    digest = hashlib.sha256(previous.read_bytes()).hexdigest()
                    metadata_digest = hashlib.sha256(
                        metadata_path.read_bytes()
                    ).hexdigest()
                    (work / f"{backup_name}.previous.sha256").write_text(
                        f"{digest}  {backup_name}.previous\n"
                        f"{metadata_digest}  "
                        f"{backup_name}.previous.metadata\n",
                        encoding="utf-8",
                    )
                (work / "trust.previously-enabled").touch()
                (work / "trust.previously-active").touch()
                (work / "k3s.previously-enabled").touch()
                (work / "k3s.previously-active").touch()
                (work / "k3s-node").write_text(
                    "fabric-az1-cp1\n", encoding="utf-8"
                )
                dependency_contract = work / "etcd-dependency-contract.previous"
                dependency_contract.write_text(
                    "Requires=fabric-etcd-client-trust.service\n"
                    "After=fabric-etcd-client-trust.service\n"
                    "BindsTo=\nPartOf=\nRequisite=\nStopPropagatedFrom=\n",
                    encoding="utf-8",
                )
                dependency_digest = hashlib.sha256(
                    dependency_contract.read_bytes()
                ).hexdigest()
                (work / "etcd-dependency-contract.previous.sha256").write_text(
                    f"{dependency_digest}  etcd-dependency-contract.previous\n",
                    encoding="utf-8",
                )
                fake_etcdctl = fake_bin / "etcdctl"
                fake_etcdctl.write_text(
                    "#!/usr/bin/env bash\nset -euo pipefail\n",
                    encoding="utf-8",
                )
                fake_etcdctl.chmod(0o700)
                replacements["/usr/local/bin/etcdctl"] = str(fake_etcdctl)
                fake_k3s = fake_bin / "k3s"
                fake_k3s.write_text(
                    "#!/usr/bin/env bash\n"
                    "set -euo pipefail\n"
                    'case "$*" in\n'
                    '  *"--raw=/readyz?verbose"*)\n'
                    '    case ${K3S_FIXTURE_MODE-} in\n'
                    '      api-timeout) sleep 5; exit 1 ;;\n'
                    '      api-not-ready) printf "not ready\\n"; exit 1 ;;\n'
                    '      *) printf "readyz check passed\\n" ;;\n'
                    '    esac ;;\n'
                    '  *"get node fabric-az1-cp1"*) printf "True" ;;\n'
                    '  *"-n kube-node-lease get lease fabric-az1-cp1"*) '
                    'count=0; '
                    '[[ ! -f $K3S_LEASE_COUNTER ]] || '
                    'count=$(<"$K3S_LEASE_COUNTER"); '
                    'count=$((count + 1)); '
                    'printf "%s\\n" "$count" >"$K3S_LEASE_COUNTER"; '
                    'if [[ ${K3S_FIXTURE_MODE-} == lease-unchanged ]] || '
                    '   ((count == 1)); then '
                    'printf "11111111-1111-1111-1111-111111111111\\t10\\t'
                    '2026-01-01T00:00:00Z"; else '
                    'printf "11111111-1111-1111-1111-111111111111\\t11\\t'
                    '2026-01-01T00:00:01Z"; fi ;;\n'
                    "  *) exit 1 ;;\n"
                    "esac\n",
                    encoding="utf-8",
                )
                fake_k3s.chmod(0o700)
                replacements["/usr/local/bin/k3s"] = str(fake_k3s)

            replacements["/usr/bin/matchpathcon"] = str(fake_matchpathcon)

            for target in target_paths:
                target.write_text("must survive accepted boot\n", encoding="utf-8")

            for production_path, fixture_path in sorted(
                replacements.items(), key=lambda item: len(item[0]), reverse=True
            ):
                rollback = rollback.replace(production_path, fixture_path)

            result = subprocess.run(
                ["bash"],
                input=rollback,
                text=True,
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "SYSTEMCTL_LOG": str(log),
                    "K3S_LEASE_COUNTER": str(root / "k3s-lease-counter"),
                    "K3S_FIXTURE_MODE": client_trust_failure or "healthy",
                },
                capture_output=True,
                check=False,
            )
            if client_trust_failure is None:
                self.assertEqual(
                    result.returncode,
                    0,
                    f"{helper} {state} fixture failed: {result.stderr}",
                )
            resulting_state = (work / "state").read_text(
                encoding="utf-8"
            ).strip()
            systemctl_log = (
                log.read_text(encoding="utf-8") if log.exists() else ""
            )
            if state == "accepted":
                for target in target_paths:
                    self.assertEqual(
                        target.read_text(encoding="utf-8"),
                        "must survive accepted boot\n",
                    )
            return (
                resulting_state,
                systemctl_log,
                result.returncode,
                (work / "rolled-back").exists(),
            )

    def test_outer_helpers_and_all_embedded_protocol_programs_parse_as_bash(
        self,
    ) -> None:
        for helper, contract in self.helpers.items():
            with self.subTest(helper=helper, program="outer"):
                subprocess.run(
                    ["bash", "-n", str(contract["path"])],
                    check=True,
                )
            for name in self.PROGRAMS:
                body = self.program(helper, name)
                with self.subTest(helper=helper, program=name):
                    subprocess.run(
                        ["bash", "-n"],
                        input=body,
                        text=True,
                        check=True,
                    )

    def test_state_machine_is_persistent_token_bound_and_has_no_disarm_marker(
        self,
    ) -> None:
        for helper, contract in self.helpers.items():
            text = contract["text"]
            with self.subTest(helper=helper):
                self.assertIn(f'work={contract["work"]}', text)
                for path in ("state", "state.lock", "state.next"):
                    self.assertIn(f"$work/{path}", text)
                for state in (
                    "armed",
                    "accepted",
                    "rollback-required",
                    "rolling-back",
                    "rolled-back",
                ):
                    self.assertIn(f'{state}:$token', text)
                self.assertNotIn("$work/disarmed", text)

    def test_negative_systemd_assertions_do_not_use_bare_bang_under_errexit(
        self,
    ) -> None:
        for helper in self.helpers:
            for name in (
                "force_rollback_program",
                "disarm_ready_program",
                "disarm_program",
                "rollback_cleanup_program",
            ):
                body = self.program(helper, name)
                with self.subTest(helper=helper, program=name):
                    self.assertNotRegex(body, r"(?m)^! systemctl\b")

    def test_every_state_writer_is_atomic_and_durable(self) -> None:
        for helper in self.helpers:
            for name in (
                "rollback_program",
                "force_rollback_program",
                "disarm_program",
            ):
                body = self.program(helper, name)
                writer = self.function(body, "write_state")
                with self.subTest(helper=helper, program=name):
                    self.assertIn("state_path=$work/state", body)
                    self.assert_ordered(
                        writer,
                        '"$work/state.next"',
                        'chmod 0600 "$work/state.next"',
                    )
                    rename_match = re.search(
                        r'mv "\$work/state\.next" '
                        r'"\$(?:state_path|work/state)"',
                        writer,
                    )
                    self.assertIsNotNone(rename_match)
                    rename = rename_match.start()
                    self.assertIn("sync", writer[rename:])

    def test_initial_armed_state_is_durable_before_systemd_can_run_rollback(
        self,
    ) -> None:
        for helper, contract in self.helpers.items():
            text = contract["text"]
            arm = text.index("printf 'armed:%s\\n' \"$rollout_token\"")
            state_lock = text.rindex("state.lock", 0, arm)
            state_next = text.index("state.next", arm)
            if helper == "root-firewall":
                state_move = text.index(
                    'state.next" "$rollback_work/state"', state_next
                )
            else:
                state_move = text.index(
                    'state.next" "$remote_work/state"', state_next
                )
            durable = text.index("sync --file-system", state_move)
            enabled = text.index("/usr/bin/systemctl enable", durable)
            both_triggers = text.index(
                '"$rollback_unit.service" "$rollback_unit.timer"', enabled
            )
            start = text.index("/usr/bin/systemctl start", both_triggers)
            timer_started = text.index(
                '"$rollback_unit.timer"', start
            )
            with self.subTest(helper=helper):
                self.assertEqual(
                    [
                        state_lock,
                        arm,
                        state_next,
                        state_move,
                        durable,
                        enabled,
                        both_triggers,
                        start,
                        timer_started,
                    ],
                    sorted(
                        [
                            state_lock,
                            arm,
                            state_next,
                            state_move,
                            durable,
                            enabled,
                            both_triggers,
                            start,
                            timer_started,
                        ]
                    ),
                )

    def test_boot_resumes_nonterminal_rollback_and_accepted_boot_is_a_noop(
        self,
    ) -> None:
        for helper, contract in self.helpers.items():
            text = contract["text"]
            rollback = self.program(helper, "rollback_program")
            with self.subTest(helper=helper):
                self.assertIn(
                    f"service={contract['unit']}.service", rollback
                )
                self.assertIn(
                    '"accepted:$token"|"rolled-back:$token")', rollback
                )
                accepted = rollback.index('"accepted:$token"')
                accepted_disable = rollback.index(
                    'systemctl disable "$timer" "$service"', accepted
                )
                accepted_exit = rollback.index("exit 0", accepted_disable)
                restore = rollback.index("write_state rolling-back")
                self.assertLess(accepted, accepted_disable)
                self.assertLess(accepted_disable, accepted_exit)
                self.assertLess(accepted_exit, restore)

                self.assertIn(
                    '"armed:$token"|"rollback-required:$token"|'
                    '"rolling-back:$token"',
                    rollback,
                )
                marker = rollback.index(
                    'mv "$work/rolled-back.next" "$work/rolled-back"'
                )
                terminal = rollback.index("write_state rolled-back", marker)
                disable = rollback.index(
                    'systemctl disable "$timer" "$service"', terminal
                )
                self.assertLess(marker, terminal)
                self.assertLess(terminal, disable)

                service = f"{contract['unit']}.service"
                self.assertIn("Restart=on-failure", text)
                self.assertIn("WantedBy=multi-user.target", text)
                self.assertIn(service, text)

    def test_reboot_fixture_resumes_rolling_back_and_noops_accepted(self) -> None:
        for helper, contract in self.helpers.items():
            service = f"{contract['unit']}.service"
            timer = f"{contract['unit']}.timer"
            with self.subTest(helper=helper, state="rolling-back"):
                (
                    state,
                    systemctl_log,
                    returncode,
                    rolled_back,
                ) = self.run_boot_fixture(helper, "rolling-back")
                self.assertEqual(returncode, 0)
                self.assertEqual(state, "rolled-back:fixture-token")
                self.assertTrue(rolled_back)
                self.assertIn(f"disable {timer} {service}", systemctl_log)
            with self.subTest(helper=helper, state="accepted"):
                (
                    state,
                    systemctl_log,
                    returncode,
                    rolled_back,
                ) = self.run_boot_fixture(helper, "accepted")
                self.assertEqual(returncode, 0)
                self.assertEqual(state, "accepted:fixture-token")
                self.assertFalse(rolled_back)
                self.assertIn(f"disable {timer} {service}", systemctl_log)
                self.assertNotIn("restart", systemctl_log)
                self.assertNotIn("reload", systemctl_log)

    def test_client_trust_rollback_does_not_finish_without_k3s_liveness(
        self,
    ) -> None:
        for failure in ("api-timeout", "api-not-ready", "lease-unchanged"):
            with self.subTest(failure=failure):
                state, _, returncode, rolled_back = self.run_boot_fixture(
                    "client-trust",
                    "rolling-back",
                    client_trust_failure=failure,
                )
                self.assertNotEqual(returncode, 0)
                self.assertEqual(state, "rolling-back:fixture-token")
                self.assertFalse(rolled_back)

    def test_rollback_holds_the_state_lock_through_restore_and_terminal_state(
        self,
    ) -> None:
        for helper, contract in self.helpers.items():
            rollback = self.program(helper, "rollback_program")
            with self.subTest(helper=helper):
                self.assert_state_lock(rollback)
                lock = rollback.index('flock --exclusive "$state_fd"')
                rolling = rollback.index('write_state rolling-back')
                restored = max(
                    rollback.index(marker)
                    for marker in contract["restore_markers"]
                )
                terminal = rollback.index('write_state rolled-back')
                self.assertLess(lock, rolling)
                self.assertLess(rolling, restored)
                self.assertLess(restored, terminal)
                self.assertNotIn('flock --unlock "$state_fd"', rollback)
                self.assertNotIn('exec {state_fd}>&-', rollback)

    def test_post_arm_candidate_writer_is_one_deadline_bound_lock_section(
        self,
    ) -> None:
        last_mutation = {
            "root-firewall": "systemctl reload fabric-guard.service",
            "client-trust": "systemctl start --no-block k3s.service",
        }
        for helper, contract in self.helpers.items():
            apply = self.program(helper, "apply_candidate_program")
            with self.subTest(helper=helper):
                self.assert_state_lock(apply)
                self.assert_ordered(
                    apply,
                    'flock --exclusive "$state_fd"',
                    '"armed:$token"',
                    '[[ ! -e $work/force-rollback',
                    'deadline=$(<"$work/deadline")',
                    "now=$(date +%s)",
                    "$now -lt $deadline",
                    "install_atomic()",
                    last_mutation[helper],
                )
                final_state = apply.rindex(
                    '[[ $(<"$state_path") == "armed:$token" ]]'
                )
                acknowledgement = apply.index("ROLLOUT_APPLIED")
                self.assertLess(apply.index(last_mutation[helper]), final_state)
                self.assertLess(final_state, acknowledgement)
                self.assertNotIn('flock --unlock "$state_fd"', apply)
                self.assertNotIn('exec {state_fd}>&-', apply)

                text = contract["text"]
                invocation = text.index("apply_candidate_result=$(")
                read_only_end = text.index(
                    "disarm_ready_program=", invocation
                )
                post_apply = text[invocation:read_only_end]
                self.assertNotRegex(
                    post_apply,
                    r"systemctl (?:daemon-reload|enable|reload|restart)\b",
                )
                self.assertNotIn("sysctl -q -w", post_apply)
                self.assertNotIn("install_atomic ", post_apply)

    def test_client_trust_rollback_restores_dependency_graph_and_k3s(self) -> None:
        rollback = self.program("client-trust", "rollback_program")
        apply = self.program("client-trust", "apply_candidate_program")

        self.assert_ordered(
            rollback,
            "restore /etc/systemd/system/etcd.service etcd.service",
            "systemctl daemon-reload",
            "systemctl stop --no-block fabric-etcd-client-trust.service",
            "systemctl restart --no-block etcd.service",
            "systemctl start --no-block k3s.service",
            "lease_advanced=true",
            'write_state rolled-back',
        )
        self.assertNotIn(
            "systemctl stop --no-block fabric-etcd-client-trust.service",
            rollback[: rollback.index("systemctl daemon-reload")],
        )
        for marker in (
            "k3s-node",
            "k3s.previously-enabled",
            "k3s.previously-active",
        ):
            self.assertIn(marker, rollback)
        self.assertIn("$k3s_ready == True", rollback)
        self.assertIn('$lease_rv_after != "$lease_rv_before"', rollback)
        self.assertIn(
            "((10#$lease_after_ns > 10#$lease_before_ns))", rollback
        )

        self.assert_ordered(
            apply,
            "systemctl restart --no-block etcd.service",
            "systemctl start --no-block k3s.service",
            'bounded_k3s get --raw="/readyz?verbose"',
            "ROLLOUT_APPLIED=",
        )

    def test_payload_promotions_and_restores_are_atomic_and_durable(
        self,
    ) -> None:
        for helper in self.helpers:
            apply = self.program(helper, "apply_candidate_program")
            install = self.function(apply, "install_atomic")
            rollback = self.program(helper, "rollback_program")
            restore_name = "restore_file" if helper == "root-firewall" else "restore"
            restore = self.function(rollback, restore_name)
            with self.subTest(helper=helper, phase="apply"):
                self.assertIn(".fabric-rollout.next", install)
                self.assert_ordered(
                    install,
                    'install --owner=root --group=root --mode="$mode"',
                    'cmp --silent "$source" "$next"',
                    'sync --file-system "$next"',
                    'mv --force -- "$next" "$target"',
                    'sync --file-system "$directory"',
                    'cmp --silent "$source" "$target"',
                )
            with self.subTest(helper=helper, phase="rollback"):
                self.assertIn(".fabric-rollback.next", restore)
                file_sync = restore.index('sync --file-system "$next"')
                move = restore.index(
                    'mv --force -- "$next" "$target"', file_sync
                )
                directory_sync = restore.index(
                    'sync --file-system "$directory"', move
                )
                compare = restore.index("cmp --silent", directory_sync)
                self.assertEqual(
                    [file_sync, move, directory_sync, compare],
                    sorted([file_sync, move, directory_sync, compare]),
                )

    def test_candidate_selinux_restore_uses_the_supported_short_option(
        self,
    ) -> None:
        for helper in self.helpers:
            apply = self.program(helper, "apply_candidate_program")
            install = self.function(apply, "install_atomic")
            with self.subTest(helper=helper):
                self.assertIn('/usr/bin/restorecon -F "$target"', install)
                self.assertNotIn("restorecon --force", install)

    def test_system_service_dropin_is_exactly_reviewed_everywhere(self) -> None:
        path = "/usr/lib/systemd/system/service.d/10-timeout-abort.conf"
        digest = (
            "ae6b234f92bc22f1201a7572b59b454c"
            "9809f33c80d13f361b9674e1801acc37"
        )
        for helper, contract in self.helpers.items():
            text = contract["text"]
            disarm_ready = self.program(helper, "disarm_ready_program")
            disarm = self.program(helper, "disarm_program")
            with self.subTest(helper=helper):
                self.assertIn(path, text)
                self.assertIn(digest, text)
                self.assertIn("reviewed system service drop-in is an unsafe symlink", text)
                self.assertIn("system service drop-in metadata is unsafe", text)
                self.assertIn("system service drop-in differs", text)
                self.assertIn(
                    f'== {path} ]]',
                    disarm_ready,
                )
                self.assertIn(
                    '[[ -z $(systemctl show --property=DropInPaths '
                    '--value "${timer##*/}") ]]',
                    disarm_ready,
                )
                for program in (disarm_ready, disarm):
                    self.assertIn(f"! -L {path}", program)
                    self.assertIn(
                        f"system_dropin_hash=$(sha256sum {path})",
                        program,
                    )
                    self.assertIn(digest, program)
                    self.assertIn(
                        f"/usr/bin/matchpathcon -V {path}",
                        program,
                    )

    def test_terminal_states_follow_live_proof_target_sync_and_deadline(
        self,
    ) -> None:
        live_marker = {
            "root-firewall": 'cmp --silent "$firewall_live_previous"',
            "client-trust": "/usr/local/bin/etcdctl",
        }
        disarm_live_marker = {
            "root-firewall": 'cmp --silent "$work/firewall-live.accepted"',
            "client-trust": "/usr/local/bin/etcdctl",
        }
        for helper in self.helpers:
            rollback = self.program(helper, "rollback_program")
            terminal = rollback.index("write_state rolled-back")
            marker = rollback.index(
                'mv "$work/rolled-back.next" "$work/rolled-back"'
            )
            target_sync = rollback.rindex(
                "sync --file-system /etc", 0, marker
            )
            with self.subTest(helper=helper, phase="rollback"):
                self.assertLess(rollback.index(live_marker[helper]), target_sync)
                if helper == "client-trust":
                    self.assertLess(
                        rollback.rindex(
                            "sync --file-system /usr/local", 0, marker
                        ),
                        marker,
                    )
                marker_sync = rollback.index(
                    'sync --file-system "$work"', marker
                )
                self.assertLess(target_sync, marker)
                self.assertLess(marker, marker_sync)
                self.assertLess(marker_sync, terminal)

            disarm = self.program(helper, "disarm_program")
            accepted = disarm.index("write_state accepted")
            target_sync = disarm.rindex(
                "sync --file-system /etc", 0, accepted
            )
            deadline = disarm.rindex(
                'deadline=$(<"$work/deadline")', 0, accepted
            )
            fresh = disarm.rindex("$now -lt $deadline", 0, accepted)
            with self.subTest(helper=helper, phase="accept"):
                self.assertLess(
                    disarm.index(disarm_live_marker[helper]), target_sync
                )
                if helper == "client-trust":
                    self.assertLess(
                        disarm.rindex(
                            "sync --file-system /usr/local", 0, accepted
                        ),
                        deadline,
                    )
                self.assertLess(target_sync, deadline)
                self.assertLess(deadline, fresh)
                self.assertLess(fresh, accepted)

    def test_disarm_accepts_only_exact_armed_state_while_holding_the_same_lock(
        self,
    ) -> None:
        for helper, contract in self.helpers.items():
            disarm = self.program(helper, "disarm_program")
            with self.subTest(helper=helper):
                self.assert_state_lock(disarm)
                lock = disarm.index('flock --exclusive "$state_fd"')
                owner = disarm.index(contract["owner"])
                token = disarm.index('"$work/expected-token"')
                armed = disarm.index('"armed:$token"')
                no_force = disarm.index('[[ ! -e $work/force-rollback')
                accepted = disarm.index('write_state accepted')
                unlock = disarm.index('flock --unlock "$state_fd"')
                close_fd = disarm.index('exec {state_fd}>&-')
                stopped = disarm.index('systemctl stop "${timer##*/}"')
                disabled = disarm.index(
                    'systemctl disable "${timer##*/}" "${service##*/}"'
                )
                trigger_sync = disarm.index(
                    "sync --file-system /etc/systemd/system", disabled
                )
                self.assertEqual(
                    [
                        owner,
                        token,
                        lock,
                        armed,
                        no_force,
                        accepted,
                        disabled,
                        trigger_sync,
                        unlock,
                        close_fd,
                        stopped,
                    ],
                    sorted(
                        [
                            owner,
                            token,
                            lock,
                            armed,
                            no_force,
                            accepted,
                            disabled,
                            trigger_sync,
                            unlock,
                            close_fd,
                            stopped,
                        ]
                    ),
                )
                post_disable = disarm[stopped:]
                self.assert_exact_inactive_checks(post_disable)
                self.assert_no_queued_job_check(post_disable)
                acknowledgement = post_disable.index("ROLLBACK_DISARMED")
                self.assertLess(
                    post_disable.index("--property=Job"), acknowledgement
                )

    def test_disarm_readiness_is_also_a_locked_exact_armed_snapshot(self) -> None:
        for helper in self.helpers:
            ready = self.program(helper, "disarm_ready_program")
            with self.subTest(helper=helper):
                self.assert_state_lock(ready)
                self.assert_ordered(
                    ready,
                    'flock --exclusive "$state_fd"',
                    '"armed:$token"',
                    '[[ ! -e $work/force-rollback',
                    "--property=ActiveState",
                    "--property=Job",
                    "ROLLBACK_DISARM_READY",
                )
                self.assert_exact_inactive_checks(ready, minimum=1)
                self.assert_no_queued_job_check(ready)

    def test_force_claims_rollback_under_lock_then_releases_it_before_start(
        self,
    ) -> None:
        for helper in self.helpers:
            force = self.program(helper, "force_rollback_program")
            with self.subTest(helper=helper):
                self.assert_state_lock(force)
                lock = force.index('flock --exclusive "$state_fd"')
                armed = force.index('"armed:$token"')
                required = force.index('write_state rollback-required')
                unlock = force.index('flock --unlock "$state_fd"')
                start_match = re.search(
                    r'systemctl start(?: --no-block)? "\$\{service##\*/\}"',
                    force,
                )
                self.assertIsNotNone(start_match)
                start = start_match.start()
                rolled_back = force.index('"rolled-back:$token"', start)
                acknowledgement = force.index("ROLLBACK_FORCED", rolled_back)
                self.assertEqual(
                    [
                        lock,
                        armed,
                        required,
                        unlock,
                        start,
                        rolled_back,
                        acknowledgement,
                    ],
                    sorted(
                        [
                            lock,
                            armed,
                            required,
                            unlock,
                            start,
                            rolled_back,
                            acknowledgement,
                        ]
                    ),
                )
                tail = force[rolled_back:acknowledgement]
                self.assert_exact_inactive_checks(tail, minimum=1)
                self.assert_no_queued_job_check(tail)

    def test_cleanup_proves_units_quiescent_before_deleting_any_artifact(
        self,
    ) -> None:
        for helper in self.helpers:
            cleanup = self.program(helper, "rollback_cleanup_program")
            with self.subTest(helper=helper):
                self.assert_state_lock(cleanup)
                accepted = cleanup.index('"accepted:$token"')
                first_remove = cleanup.index("rm ")
                pre_remove = cleanup[accepted:first_remove]
                self.assert_exact_inactive_checks(pre_remove)
                self.assert_no_queued_job_check(pre_remove)
                unit_sync = cleanup.index(
                    "sync --file-system /etc/systemd/system", first_remove
                )
                reload = cleanup.index("systemctl daemon-reload", first_remove)
                work_remove = cleanup.index(
                    'rm --recursive --force -- "$work"', reload
                )
                work_sync = cleanup.index(
                    "sync --file-system /var/lib", work_remove
                )
                acknowledgement = cleanup.index(
                    "ROLLBACK_CLEANED", work_sync
                )
                self.assertLess(first_remove, unit_sync)
                self.assertLess(unit_sync, reload)
                self.assertLess(work_remove, work_sync)
                self.assertLess(work_sync, acknowledgement)
                post_sweep = cleanup[reload:work_remove]
                self.assertGreaterEqual(
                    post_sweep.count("--property=LoadState"), 2
                )
                self.assertGreaterEqual(post_sweep.count("== not-found"), 2)
                self.assert_exact_inactive_checks(post_sweep)
                self.assert_no_queued_job_check(post_sweep)
                self.assertGreaterEqual(
                    post_sweep.count("systemctl is-enabled"), 2
                )
                durable_sweep = cleanup[work_sync:acknowledgement]
                self.assertGreaterEqual(
                    durable_sweep.count("--property=LoadState"), 2
                )
                self.assert_exact_inactive_checks(durable_sweep)
                self.assert_no_queued_job_check(durable_sweep)
                self.assertNotIn('flock --unlock "$state_fd"', cleanup)

    def test_live_all_and_locked_preflight_use_the_full_absence_contract(
        self,
    ) -> None:
        for helper, contract in self.helpers.items():
            text = contract["text"]
            unit = contract["unit"]
            absence = self.function(text, "assert_rollback_residue_absent")
            with self.subTest(helper=helper):
                self.assertIn(contract["work"], absence)
                self.assertGreaterEqual(absence.count(f"{unit}.service"), 2)
                self.assertGreaterEqual(absence.count(f"{unit}.timer"), 2)
                self.assertIn(".service.d", absence)
                self.assertIn(".timer.d", absence)
                self.assertIn("! -e", absence)
                self.assertIn("! -L", absence)
                self.assertIn("--property=LoadState", absence)
                self.assertIn("--property=ActiveState", absence)
                self.assertIn("--property=UnitFileState", absence)
                self.assertIn("--property=Job", absence)
                self.assertIn("not-found", absence)
                self.assertIn("inactive", absence)
                self.assertIn("disabled", absence)
                self.assertIn("systemctl is-enabled", absence)
                self.assertGreaterEqual(
                    text.count("assert_rollback_residue_absent"), 4
                )

                definition = text.index("assert_rollback_residue_absent()")
                live_all = text.index("if $verify_live_all; then", definition)
                first_live_proof = text.index(
                    "assert_rollback_residue_absent", live_all
                )
                second_live_proof = text.index(
                    "assert_rollback_residue_absent", first_live_proof + 1
                )
                self.assertIn(
                    contract["sweep_marker"],
                    text[first_live_proof:second_live_proof],
                )

                lock_acquisition = text.index(
                    'create configmap "$lock_name"', second_live_proof
                )
                locked_preflight = text.index(
                    "assert_rollback_residue_absent", lock_acquisition
                )
                self.assertGreater(locked_preflight, lock_acquisition)


if __name__ == "__main__":
    unittest.main()
