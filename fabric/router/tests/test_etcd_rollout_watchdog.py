#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import os
import pathlib
import re
import subprocess
import tempfile
import textwrap
import unittest


REPO_ROOT = pathlib.Path(__file__).parents[3]
HELPER = REPO_ROOT / "scripts" / "rollout-fabric-router-etcd-policy"
EARLY_GUARD = REPO_ROOT / "fabric" / "router" / "etcd-client-fence.nft"


class RouterRolloutWatchdogTests(unittest.TestCase):
    """Safety contract for the normal (open-policy) rollout watchdog."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.helper = HELPER.read_text(encoding="utf-8")
        cls.guard = EARLY_GUARD.read_text(encoding="utf-8")

    def heredoc(self, tag: str) -> str:
        match = re.search(
            rf"<<'{re.escape(tag)}'(?:\s*\|\|\s*true)?\n"
            rf"(?P<body>.*?)\n{re.escape(tag)}(?:\n|$)",
            self.helper,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match, f"missing {tag} heredoc")
        return match.group("body")

    def normal_rollout(self) -> str:
        start = self.helper.index(
            'payload_b64=$(base64 -w0 -- "$transition_file")'
        )
        return self.helper[start:]

    def terminal_link(self, body: str) -> tuple[str, str, str]:
        match = re.search(
            r'(?P<command>ln(?:\s+--)?\s+"\$work/(?P<source>[A-Za-z0-9._-]+)"'
            r'\s+"\$work/(?P<target>[A-Za-z0-9._-]*terminal[A-Za-z0-9._-]*)")',
            body,
        )
        self.assertIsNotNone(
            match,
            "terminal decision must use one-filesystem hard-link creation",
        )
        return (
            match.group("command"),
            match.group("source"),
            match.group("target"),
        )

    def named_hard_link(
        self, body: str, target: str
    ) -> tuple[str, str, str]:
        match = re.search(
            rf'(?P<command>ln(?:\s+--)?\s+"\$work/(?P<source>[A-Za-z0-9._-]+)"'
            rf'\s+"\$work/(?P<target>{re.escape(target)})")',
            body,
        )
        self.assertIsNotNone(
            match,
            f"{target} decision must use one-filesystem hard-link creation",
        )
        return (
            match.group("command"),
            match.group("source"),
            match.group("target"),
        )

    def shell_function(self, body: str, name: str) -> str:
        match = re.search(
            rf"(?ms)^{re.escape(name)}\(\) \{{\n(?P<body>.*?)^\}}$",
            body,
        )
        self.assertIsNotNone(match, f"missing {name} shell function")
        return match.group("body")

    def assert_ordered(self, body: str, *markers: str) -> None:
        missing = [marker for marker in markers if marker not in body]
        self.assertEqual(missing, [], f"missing ordered markers: {missing}")
        positions = [body.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions))

    def write_executable(self, path: pathlib.Path, body: str) -> None:
        path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
        path.chmod(0o700)

    def test_watchdog_heredocs_are_valid_posix_shell(self) -> None:
        for tag in (
            "REMOTE_ROLLBACK",
            "REMOTE_INIT",
            "REMOTE_STAGE",
            "REMOTE_ARM",
            "REMOTE_APPLY",
            "REMOTE_ACCEPT",
            "REMOTE_FORCE_ROLLBACK",
            "REMOTE_ROLLBACK_CLEANUP",
        ):
            body = self.heredoc(tag)
            with self.subTest(tag=tag):
                subprocess.run(
                    ["sh", "-n"],
                    input=body,
                    text=True,
                    check=True,
                )

    def test_normal_staging_binds_the_exact_committed_early_guard(self) -> None:
        normal = self.normal_rollout()
        stage = self.heredoc("REMOTE_STAGE")
        guard_digest = hashlib.sha256(EARLY_GUARD.read_bytes()).hexdigest()

        self.assertIn(f"readonly fence_guard_sha256={guard_digest}", self.helper)
        self.assertIn('guard_b64=$(base64 -w0 -- "$fence_guard_file")', normal)
        self.assertIn('"$guard_b64" "$fence_guard_sha256"', normal)
        for required in (
            "guard_sha",
            "guard_b64",
            'printf \'%s\' "$guard_b64" | base64 -d >"$work/early-guard.nft"',
            '"$work/early-guard.nft" | awk',
            "early-guard.nft.sha256",
        ):
            with self.subTest(required=required):
                self.assertIn(required, stage)
        self.assertIn(
            'rollback early-drop payload changed in transport', stage
        )

    def test_arm_is_boot_bound_and_the_watchdog_respawns(self) -> None:
        arm = self.heredoc("REMOTE_ARM")
        init = self.heredoc("REMOTE_INIT")

        self.assert_ordered(
            arm,
            "/proc/sys/kernel/random/boot_id",
            '"$work/armed-boot-id.next"',
            'mv "$work/armed-boot-id.next" "$work/armed-boot-id"',
            'mv "$work/armed.next" "$work/armed"',
            '"$init" enable',
            '"$init" start',
        )
        self.assertIn("procd_set_param respawn 3600 5 0", init)
        self.assertLess(
            init.index("procd_set_param command"),
            init.index("procd_set_param respawn 3600 5 0"),
        )

    def test_apply_started_is_durable_before_any_live_uci_mutation(self) -> None:
        apply = self.heredoc("REMOTE_APPLY")
        self.assert_ordered(
            apply,
            '"$work/apply-started.next"',
            'mv "$work/apply-started.next" "$work/apply-started"',
            "sync",
            'uci -q batch <"$work/transition.uci"',
            "uci -q commit firewall",
        )
        durable = apply.index("sync")
        for mutation in (
            'uci -q batch <"$work/transition.uci"',
            "uci -q commit firewall",
            "/etc/init.d/firewall reload",
        ):
            with self.subTest(mutation=mutation):
                self.assertLess(durable, apply.index(mutation))

    def test_apply_loads_a_persistent_guard_before_opening_uci(self) -> None:
        apply = self.heredoc("REMOTE_APPLY")
        install = apply.index('mv "$guard.apply-next" "$guard"')
        durable = apply.index("sync", install)
        closed_reload = apply.index("/etc/init.d/firewall reload", durable)
        first_request = apply.index(
            'comment "fabric etcd fence client requests"', closed_reload
        )
        first_reply = apply.index(
            'comment "fabric etcd fence server replies"', first_request
        )
        batch = apply.index('uci -q batch <"$work/transition.uci"')
        commit = apply.index("uci -q commit firewall", batch)
        guarded_reload = apply.index("/etc/init.d/firewall reload", commit)
        finished = apply.index('"$work/apply-finished.next"', guarded_reload)
        self.assertEqual(
            [
                install,
                durable,
                closed_reload,
                first_request,
                first_reply,
                batch,
                commit,
                guarded_reload,
                finished,
            ],
            sorted(
                [
                    install,
                    durable,
                    closed_reload,
                    first_request,
                    first_reply,
                    batch,
                    commit,
                    guarded_reload,
                    finished,
                ]
            ),
        )

        apply_end = self.helper.index("\nREMOTE_APPLY", self.helper.index("<<'REMOTE_APPLY'"))
        accept_start = self.helper.index("<<'REMOTE_ACCEPT'", apply_end)
        qualification = self.helper[apply_end:accept_start]
        self.assertEqual(
            qualification.count("validate_remote_phase guarded-post"), 2
        )
        self.assertNotRegex(qualification, r"validate_remote_phase post(?:\s|$)")

    def test_fence_and_accept_race_the_same_atomic_hard_link(self) -> None:
        rollback = self.heredoc("REMOTE_ROLLBACK")
        disarm = self.heredoc("REMOTE_ACCEPT")
        fence_command, fence_source, terminal = self.terminal_link(rollback)
        accept_command, accept_source, accept_terminal = self.terminal_link(disarm)

        self.assertEqual(accept_terminal, terminal)
        self.assertNotEqual(accept_source, fence_source)
        self.assertNotIn("-f", fence_command)
        self.assertNotIn("-f", accept_command)
        self.assertNotRegex(
            rollback + disarm,
            rf'mv[^\n]*"\$work/{re.escape(terminal)}"',
        )

        # Execute the exact two link commands extracted from the production
        # heredocs.  A shared hard-link target makes the decision an atomic,
        # one-filesystem compare-and-set: exactly one actor can create it.
        with tempfile.TemporaryDirectory() as temporary:
            work = pathlib.Path(temporary)
            for attempt in range(24):
                target = work / terminal
                target.unlink(missing_ok=True)
                fence = work / fence_source
                accept = work / accept_source
                fence.write_text(f"FENCED:{attempt}\n", encoding="utf-8")
                accept.write_text(f"ACCEPTED:{attempt}\n", encoding="utf-8")
                environment = dict(os.environ, work=str(work))
                processes = [
                    subprocess.Popen(
                        ["sh", "-c", fence_command],
                        env=environment,
                        stderr=subprocess.DEVNULL,
                    ),
                    subprocess.Popen(
                        ["sh", "-c", accept_command],
                        env=environment,
                        stderr=subprocess.DEVNULL,
                    ),
                ]
                returncodes = [process.wait(timeout=5) for process in processes]
                self.assertEqual(sorted(returncodes), [0, 1])
                target_inode = target.stat().st_ino
                winning_sources = [
                    source
                    for source in (fence, accept)
                    if source.stat().st_ino == target_inode
                ]
                self.assertEqual(len(winning_sources), 1)

    def test_acceptance_outcome_is_a_second_atomic_one_winner_race(self) -> None:
        rollback = self.heredoc("REMOTE_ROLLBACK")
        accept = self.heredoc("REMOTE_ACCEPT")
        force = self.heredoc("REMOTE_FORCE_ROLLBACK")
        accept_command, accept_source, target = self.named_hard_link(
            accept, "accepted"
        )
        fence_command, fence_source, fence_target = self.named_hard_link(
            rollback, "accepted"
        )
        force_command, force_source, force_target = self.named_hard_link(
            force, "accepted"
        )
        self.assertEqual((fence_target, force_target), (target, target))
        self.assertEqual(fence_source, force_source)
        self.assertNotEqual(accept_source, fence_source)

        for forbidden in (
            "delete rule inet fw4 forward handle",
            'rm -f -- "$guard"',
            "/etc/init.d/firewall reload",
            'ln "$work/accepted-state" "$work/accept-complete"',
        ):
            with self.subTest(accept_actor_mutation=forbidden):
                self.assertNotIn(forbidden, accept)

        apply_acceptance = self.shell_function(rollback, "apply_acceptance")
        self.assert_ordered(
            apply_acceptance,
            'rm -f -- "$guard"',
            "/etc/init.d/firewall reload",
            "acceptance_open_base",
            'ln "$work/accepted-state" "$work/accept-complete"',
            "accept_complete_exact",
        )
        terminal_claim = accept.index(
            'ln "$work/accept-state" "$work/terminal"'
        )
        self.assert_ordered(
            accept[:terminal_claim],
            'current_boot_id=$(cat "$boot_id_path")',
            '[ "$current_boot_id" = "$armed_boot_id" ]',
            'now=$(date +%s)',
            '[ "$now" -lt "$accept_deadline" ]',
        )

        with tempfile.TemporaryDirectory() as temporary:
            work = pathlib.Path(temporary)
            accept_state = work / accept_source
            fence_state = work / fence_source
            accept_state.write_text("accepted\n", encoding="utf-8")
            fence_state.write_text("fenced\n", encoding="utf-8")
            environment = dict(os.environ, work=str(work))
            for _attempt in range(24):
                outcome = work / target
                outcome.unlink(missing_ok=True)
                processes = [
                    subprocess.Popen(
                        ["sh", "-c", accept_command],
                        env=environment,
                        stderr=subprocess.DEVNULL,
                    ),
                    subprocess.Popen(
                        ["sh", "-c", fence_command],
                        env=environment,
                        stderr=subprocess.DEVNULL,
                    ),
                ]
                returncodes = [process.wait(timeout=5) for process in processes]
                self.assertEqual(sorted(returncodes), [0, 1])
                outcome_inode = outcome.stat().st_ino
                self.assertEqual(
                    sum(
                        source.stat().st_ino == outcome_inode
                        for source in (accept_state, fence_state)
                    ),
                    1,
                )

        self.assertEqual(
            force_command,
            fence_command,
            "cleanup and watchdog must race the identical fenced outcome",
        )

    def test_watchdog_is_the_only_acceptance_completion_writer(self) -> None:
        rollback = self.heredoc("REMOTE_ROLLBACK")
        accept = self.heredoc("REMOTE_ACCEPT")
        force = self.heredoc("REMOTE_FORCE_ROLLBACK")
        completion_link = (
            'ln "$work/accepted-state" "$work/accept-complete"'
        )

        self.assertEqual(rollback.count(completion_link), 1)
        self.assertNotIn(completion_link, accept)
        self.assertNotIn(completion_link, force)
        self.assertNotIn("exit 0", rollback)
        self.assertGreaterEqual(
            rollback.count("accepted_exact && reconcile_acceptance_forever"),
            1,
        )

    def test_accept_actor_waits_read_only_for_watchdog_completion(self) -> None:
        accept = self.heredoc("REMOTE_ACCEPT")
        accept_command, _, _ = self.named_hard_link(accept, "accepted")
        claim = accept.index(accept_command)
        stop = accept.index('"$init" stop', claim)
        wait = accept[claim:stop]

        for forbidden in (
            'rm -f -- "$guard"',
            'ln "$work/accepted-state" "$work/accept-complete"',
            "/etc/init.d/firewall reload",
            "nft -f -",
            "uci -q batch",
            "uci -q commit",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, wait)

        exact = self.shell_function(wait, "accepted_open_exact")
        for required in (
            '"$work/accept-complete"',
            '"$work/accepted-state"',
            'cmp -s "$firewall" "$work/candidate/firewall"',
            '[ ! -e "$guard" ] && [ ! -L "$guard" ]',
            "nft -a list table inet fw4",
            "nft -a list chain inet fw4 forward_lan",
            '"$expected_forward"',
        ):
            with self.subTest(required=required):
                self.assertIn(required, exact)
        self.assertIn("Allow-platform-to-root-etcd-client", accept[:claim])
        self.assertIn("Reject-services-to-root-etcd-internal", accept[:claim])

        wait_call = accept.index("if accepted_open_exact; then", claim)
        post_stop_proof = accept.index("\naccepted_open_exact\n", stop)
        disable = accept.index('"$init" disable', post_stop_proof)
        post_disable_proof = accept.index("\naccepted_open_exact\n", disable)
        success = accept.index("accept_success=true", post_disable_proof)
        acknowledgement = accept.index("ROLLBACK_ACCEPTED", post_disable_proof)
        self.assertEqual(
            [
                claim,
                wait_call,
                stop,
                post_stop_proof,
                disable,
                post_disable_proof,
                success,
                acknowledgement,
            ],
            sorted(
                [
                    claim,
                    wait_call,
                    stop,
                    post_stop_proof,
                    disable,
                    post_disable_proof,
                    success,
                    acknowledgement,
                ]
            ),
        )

    def test_accept_failure_handler_restarts_watchdog_until_final_proof(self) -> None:
        accept = self.heredoc("REMOTE_ACCEPT")
        handler_name = "restart_watchdog_on_failure"
        handler = self.shell_function(accept, handler_name)
        accept_command, _, _ = self.named_hard_link(accept, "accepted")

        self.assert_ordered(
            accept,
            "accept_success=false",
            f"trap {handler_name} EXIT",
            accept_command,
            '"$init" stop',
            '"$init" disable',
            "accept_success=true",
            "ROLLBACK_ACCEPTED",
        )
        enable = handler.index('"$init" enable')
        initial_running = handler.index('"$init" running', enable)
        start = handler.index('"$init" start', initial_running)
        final_running = handler.index('"$init" running', start + 1)
        self.assertEqual(
            [enable, initial_running, start, final_running],
            sorted([enable, initial_running, start, final_running]),
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            init = root / "init"
            runner = root / "runner"
            trace = root / "trace"
            running = root / "running"
            self.write_executable(
                init,
                """
                #!/bin/sh
                printf '%s\n' "$1" >>"$TRACE"
                case $1 in
                  enable) exit 0 ;;
                  running) [ -f "$RUNNING" ] ;;
                  start) : >"$RUNNING" ;;
                  *) exit 90 ;;
                esac
                """,
            )
            self.write_executable(
                runner,
                f"""
                #!/bin/sh
                set -eu
                init=$1
                accept_success=$2
                {handler_name}() {{
                {handler}
                }}
                trap {handler_name} EXIT
                exit 47
                """,
            )
            environment = dict(
                os.environ,
                TRACE=str(trace),
                RUNNING=str(running),
            )
            failed = subprocess.run(
                [runner, init, "false"],
                env=environment,
                check=False,
            )
            self.assertEqual(failed.returncode, 47)
            self.assertEqual(
                trace.read_text(encoding="utf-8").splitlines(),
                ["enable", "running", "start", "running"],
            )

            trace.unlink()
            running.unlink()
            completed = subprocess.run(
                [runner, init, "true"],
                env=environment,
                check=False,
            )
            self.assertEqual(completed.returncode, 47)
            self.assertFalse(trace.exists())
            self.assertFalse(running.exists())

    def test_force_accept_ack_requires_completion_and_fresh_open_proof(self) -> None:
        force = self.heredoc("REMOTE_FORCE_ROLLBACK")
        exact = self.shell_function(force, "accepted_open_exact")
        for required in (
            '"$(terminal_state)" = accept',
            '"$(acceptance_state)" = accept',
            "accept_complete_exact",
            'cmp -s "$firewall" "$work/candidate/firewall"',
            '[ ! -e "$guard" ] && [ ! -L "$guard" ]',
            "nft -a list table inet fw4",
            "nft -a list chain inet fw4 forward_lan",
        ):
            with self.subTest(required=required):
                self.assertIn(required, exact)

        accepted_acks = [
            match.start()
            for match in re.finditer("ROLLBACK_ACCEPTED", force)
        ]
        self.assertEqual(len(accepted_acks), 2)
        proof_then_ack = re.compile(
            r"accepted_open_exact; then\n"
            r"\s+sync\n"
            r"\s+if accepted_open_exact; then\n"
            r"\s+printf 'ROLLBACK_ACCEPTED=%s\\n' \"\$token\""
        )
        proof_blocks = list(proof_then_ack.finditer(force))
        self.assertEqual(len(proof_blocks), len(accepted_acks))
        self.assertEqual(
            [
                force.index("ROLLBACK_ACCEPTED", block.start())
                for block in proof_blocks
            ],
            accepted_acks,
        )

    def test_force_deadline_or_reboot_claims_fence_before_exposure(self) -> None:
        rollback = self.heredoc("REMOTE_ROLLBACK")
        fence_command, _, _ = self.terminal_link(rollback)
        apply_fence = self.shell_function(rollback, "apply_fence")

        for required in (
            '"$work/force-rollback"',
            '"$work/deadline"',
            '"$work/armed-boot-id"',
            "/proc/sys/kernel/random/boot_id",
        ):
            with self.subTest(required=required):
                self.assertIn(required, rollback)
        boot_read = rollback.index("/proc/sys/kernel/random/boot_id")
        wait = rollback.index("sleep 2")
        claim = rollback.index(fence_command)
        guard_install = apply_fence.index("install_guard_file")
        restore = apply_fence.index(
            'mv /etc/config/.firewall.fabric-etcd-rollback.next "$firewall"'
        )
        reload_firewall = apply_fence.rindex("/etc/init.d/firewall reload")
        self.assertLess(boot_read, wait)
        self.assertLess(claim, rollback.index("while :"))
        self.assertLess(guard_install, restore)
        self.assertLess(guard_install, reload_firewall)

    def test_reboot_mismatch_executes_fence_and_ack_before_repair_sleep(self) -> None:
        rollback = self.heredoc("REMOTE_ROLLBACK")
        token = f"{'a' * 40}:{'b' * 64}"
        armed_boot_id = "11111111-1111-1111-1111-111111111111"
        current_boot_id = "22222222-2222-2222-2222-222222222222"

        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            work = root / "work"
            fake_bin = root / "bin"
            config = root / "config"
            nft_root = root / "nftables.d"
            init = root / "rollback-init"
            boot_id = root / "boot-id"
            trace = root / "trace"
            immediate = root / "immediate.nft"
            lan_fixture = root / "forward-lan.nft"
            open_lan_fixture = root / "forward-lan-open.nft"
            forward_fixture = root / "forward.nft"
            work.mkdir()
            fake_bin.mkdir()
            config.mkdir()
            nft_root.mkdir(mode=0o755)

            firewall = config / "firewall"
            before = work / "firewall.before"
            candidate = work / "candidate" / "firewall"
            candidate.parent.mkdir()
            firewall.write_text("open-policy\n", encoding="utf-8")
            before.write_text("closed-policy\n", encoding="utf-8")
            candidate.write_text("open-policy\n", encoding="utf-8")
            (work / "early-guard.nft").write_text(
                self.guard, encoding="utf-8"
            )
            (work / "owner").write_text(
                "fabric-router-etcd-policy-rollout\n", encoding="utf-8"
            )
            (work / "expected-token").write_text(token + "\n", encoding="utf-8")
            (work / "armed").write_text(token + "\n", encoding="utf-8")
            (work / "deadline").write_text("4102444800\n", encoding="utf-8")
            (work / "armed-boot-id").write_text(
                armed_boot_id + "\n", encoding="utf-8"
            )
            (work / "fence-state").write_text(
                f"fence:{token}\n", encoding="utf-8"
            )
            (work / "accept-state").write_text(
                f"accept:{token}\n", encoding="utf-8"
            )
            (work / "accepted-state").write_text(
                token + "\n", encoding="utf-8"
            )
            (work / "rollback").write_text(rollback, encoding="utf-8")
            (work / "init").write_text("watchdog-init\n", encoding="utf-8")
            init.write_text("watchdog-init\n", encoding="utf-8")
            boot_id.write_text(current_boot_id + "\n", encoding="utf-8")
            for artifact in (
                "firewall.before",
                "early-guard.nft",
                "rollback",
                "init",
            ):
                digest = hashlib.sha256((work / artifact).read_bytes()).hexdigest()
                (work / f"{artifact}.sha256").write_text(
                    f"{digest}  {artifact}\n", encoding="utf-8"
                )
            candidate_digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
            (work / "firewall.candidate.sha256").write_text(
                f"{candidate_digest}  candidate/firewall\n",
                encoding="utf-8",
            )

            closed_order = (
                "Block-fabric-to-private-WAN",
                "Allow-roots-to-public-WAN",
                "Allow-services-to-public-Web",
                "Reject-other-fabric-to-WAN",
                "Reject-services-to-root-etcd",
                "Allow-services-to-fabric-API",
                "Allow-services-to-root-VXLAN",
                "Allow-roots-to-services-VXLAN",
                "Allow-services-to-root-kubelets",
                "Allow-roots-to-service-kubelets",
                "Allow-observer-to-services-SSH",
                "Allow-services-to-Bootie-rootfs",
                "Reject-other-services-to-roots",
                "Reject-other-roots-to-services",
            )
            lan_lines = ["chain forward_lan {"]
            lan_lines.extend(
                f'  counter comment "!fw4: {name}" # handle {index}'
                for index, name in enumerate(closed_order, start=1)
            )
            lan_lines.extend(("  jump reject_to_lan # handle 15", "}"))
            lan_fixture.write_text("\n".join(lan_lines) + "\n", encoding="utf-8")
            open_order = (
                "Block-fabric-to-private-WAN",
                "Allow-roots-to-public-WAN",
                "Allow-services-to-public-Web",
                "Reject-other-fabric-to-WAN",
                "Allow-platform-to-root-etcd-client",
                "Reject-services-to-root-etcd-internal",
                "Allow-services-to-fabric-API",
                "Allow-services-to-root-VXLAN",
                "Allow-roots-to-services-VXLAN",
                "Allow-services-to-root-kubelets",
                "Allow-roots-to-service-kubelets",
                "Allow-observer-to-services-SSH",
                "Allow-services-to-Bootie-rootfs",
                "Reject-other-services-to-roots",
                "Reject-other-roots-to-services",
            )
            open_lan_lines = ["chain forward_lan {"]
            open_lan_lines.extend(
                f'  counter comment "!fw4: {name}" # handle {index}'
                for index, name in enumerate(open_order, start=1)
            )
            open_lan_lines.extend(("  jump reject_to_lan # handle 16", "}"))
            open_lan_fixture.write_text(
                "\n".join(open_lan_lines) + "\n", encoding="utf-8"
            )
            forward_fixture.write_text(
                textwrap.dedent(
                    """
                    chain forward {
                      ip saddr { 10.66.1.10, 10.66.1.11 } ip daddr { 10.66.0.10, 10.66.0.11, 10.66.0.12 } tcp dport 2379 counter packets 0 bytes 0 drop comment "fabric etcd fence client requests" # handle 1
                      ip saddr { 10.66.0.10, 10.66.0.11, 10.66.0.12 } ip daddr { 10.66.1.10, 10.66.1.11 } tcp sport 2379 counter packets 0 bytes 0 drop comment "fabric etcd fence server replies" # handle 2
                      ct state established,related counter packets 0 bytes 0 accept comment "!fw4: Accept established flows" # handle 3
                    }
                    """
                ).lstrip(),
                encoding="utf-8",
            )

            self.write_executable(
                fake_bin / "uci",
                """
                #!/bin/sh
                if [ "$ACCEPTANCE_MODE" = success ]; then
                  case "$*" in
                    *" changes firewall"*) exit 0 ;;
                    *" firewall.fabric_flat_services_etcd_client.name")
                      printf '%s\n' Allow-platform-to-root-etcd-client ;;
                    *" firewall.fabric_flat_services_etcd_client.dest_port")
                      printf '%s\n' 2379 ;;
                    *" firewall.fabric_flat_services_etcd_client.target")
                      printf '%s\n' ACCEPT ;;
                    *" firewall.fabric_flat_reject_services_etcd_internal.name")
                      printf '%s\n' Reject-services-to-root-etcd-internal ;;
                    *" firewall.fabric_flat_reject_services_etcd_internal.dest_port")
                      printf '%s\n' '2380 2381' ;;
                    *" firewall.fabric_flat_reject_services_etcd_internal.target")
                      printf '%s\n' REJECT ;;
                    *" firewall.fabric_flat_reject_services_etcd") exit 1 ;;
                    *) exit 0 ;;
                  esac
                fi
                case "$*" in
                  *" changes firewall"*) exit 0 ;;
                  *"fabric_flat_reject_services_etcd.name"*)
                    printf '%s\n' Reject-services-to-root-etcd ;;
                  *"fabric_flat_reject_services_etcd.dest_port"*)
                    printf '%s\n' '2379 2380 2381' ;;
                  *"fabric_flat_reject_services_etcd.target"*)
                    printf '%s\n' REJECT ;;
                  *"fabric_flat_services_etcd_client"*|\
                  *"fabric_flat_reject_services_etcd_internal"*) exit 1 ;;
                  *) exit 0 ;;
                esac
                """,
            )
            self.write_executable(fake_bin / "fw4", "#!/bin/sh\nexit 0\n")
            self.write_executable(fake_bin / "chown", "#!/bin/sh\nexit 0\n")
            self.write_executable(
                fake_bin / "stat",
                "#!/bin/sh\nprintf '%s\\n' '0:0:755:directory'\n",
            )
            self.write_executable(
                fake_bin / "logger",
                "#!/bin/sh\nprintf 'logger:%s\\n' \"$*\" >>\"$TRACE\"\n",
            )
            self.write_executable(
                fake_bin / "firewall_reload",
                """
                #!/bin/sh
                if [ "$ACCEPTANCE_MODE" = success ]; then
                  [ ! -e "$GUARD" ] || exit 80
                  printf 'accept-reload\n' >>"$TRACE"
                else
                  cmp -s "$GUARD_SOURCE" "$GUARD" || exit 81
                  printf 'reload\n' >>"$TRACE"
                fi
                """,
            )
            self.write_executable(
                fake_bin / "sleep",
                """
                #!/bin/sh
                printf 'sleep:%s\n' "$1" >>"$TRACE"
                if [ "$1" = 2 ] && [ -f "$WORK/fenced" ]; then
                  exit 73
                fi
                exit 74
                """,
            )
            self.write_executable(
                fake_bin / "nft",
                """
                #!/bin/sh
                case "$*" in
                  "list chain inet fw4 forward") exit 0 ;;
                  "-f -") cat >"$IMMEDIATE" ;;
                  "list tables") printf '%s\n' 'table inet fw4' ;;
                  "-a list table inet fw4")
                    if [ "$ACCEPTANCE_MODE" = success ]; then
                      printf '%s\n' 'table inet fw4 { }'
                    else
                      printf '%s\n' 'comment "fabric etcd fence incomplete"'
                    fi
                    ;;
                  "-a list chain inet fw4 forward_lan")
                    if [ "$ACCEPTANCE_MODE" = success ]; then
                      [ ! -e "$GUARD" ] || exit 82
                      cmp -s "$CANDIDATE" "$FIREWALL" || exit 83
                      cat "$OPEN_LAN_FIXTURE"
                    else
                      cmp -s "$GUARD_SOURCE" "$GUARD" || exit 82
                      cmp -s "$BEFORE" "$FIREWALL" || exit 83
                      cat "$LAN_FIXTURE"
                    fi
                    ;;
                  "-a list chain inet fw4 forward")
                    cmp -s "$GUARD_SOURCE" "$GUARD" || exit 84
                    cmp -s "$BEFORE" "$FIREWALL" || exit 85
                    cat "$FORWARD_FIXTURE"
                    ;;
                  *) printf 'unexpected nft argv: %s\n' "$*" >&2; exit 86 ;;
                esac
                """,
            )

            executable = rollback
            executable = executable.replace(
                "work=/etc/fabric-router-etcd-policy-rollback",
                f"work={work}",
            )
            executable = executable.replace(
                "init=/etc/init.d/fabric-router-etcd-policy-rollback",
                f"init={init}",
            )
            executable = executable.replace(
                "boot_id_path=/proc/sys/kernel/random/boot_id",
                f"boot_id_path={boot_id}",
            )
            executable = executable.replace(
                "/etc/init.d/firewall reload", "firewall_reload"
            )
            executable = executable.replace("/etc/config", str(config))
            executable = executable.replace(
                "/usr/share/nftables.d", str(nft_root)
            )

            environment = dict(
                os.environ,
                PATH=f"{fake_bin}:{os.environ['PATH']}",
                WORK=str(work),
                ACCEPTANCE_MODE="closed",
                TRACE=str(trace),
                IMMEDIATE=str(immediate),
                GUARD=str(nft_root / "chain-pre" / "forward" / "10-fabric-etcd-fence.nft"),
                GUARD_SOURCE=str(work / "early-guard.nft"),
                BEFORE=str(before),
                CANDIDATE=str(candidate),
                FIREWALL=str(firewall),
                LAN_FIXTURE=str(lan_fixture),
                OPEN_LAN_FIXTURE=str(open_lan_fixture),
                FORWARD_FIXTURE=str(forward_fixture),
            )

            def execute_watchdog() -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    ["sh"],
                    input=executable,
                    text=True,
                    capture_output=True,
                    env=environment,
                    timeout=10,
                )

            def reset_live_state() -> None:
                for marker in (
                    "terminal",
                    "accepted",
                    "fenced",
                    "force-rollback",
                    "accept-boot-id",
                    "accept-deadline",
                    "accept-complete",
                ):
                    (work / marker).unlink(missing_ok=True)
                guard.unlink(missing_ok=True)
                firewall.write_text("open-policy\n", encoding="utf-8")
                trace.unlink(missing_ok=True)
                immediate.unlink(missing_ok=True)

            guard = (
                nft_root
                / "chain-pre"
                / "forward"
                / "10-fabric-etcd-fence.nft"
            )
            result = execute_watchdog()
            self.assertEqual(
                result.returncode,
                73,
                f"stdout={result.stdout!r}\nstderr={result.stderr!r}",
            )
            self.assertEqual((work / "terminal").stat().st_ino, (work / "fence-state").stat().st_ino)
            self.assertEqual((work / "fenced").read_text(encoding="utf-8"), token + "\n")
            self.assertEqual(firewall.read_text(encoding="utf-8"), "closed-policy\n")
            self.assertEqual(guard.read_text(encoding="utf-8"), self.guard)
            trace_lines = trace.read_text(encoding="utf-8").splitlines()
            self.assertNotIn("sleep:1", trace_lines)
            self.assertGreaterEqual(trace_lines.count("reload"), 2)
            self.assertEqual(trace_lines[-1], "sleep:2")
            immediate_lines = immediate.read_text(encoding="utf-8").splitlines()
            self.assertIn("server replies", immediate_lines[0])
            self.assertIn("client requests", immediate_lines[1])

            # A reboot after terminal ACCEPT but before the outcome marker is
            # not an accepted rollout.  The persisted guard makes boot
            # fail-closed while the watchdog wins the second outcome race and
            # repairs the old UCI policy.
            reset_live_state()
            os.link(work / "accept-state", work / "terminal")
            (work / "accept-boot-id").write_text(
                armed_boot_id + "\n", encoding="utf-8"
            )
            (work / "accept-deadline").write_text(
                "4102444800\n", encoding="utf-8"
            )
            result = execute_watchdog()
            self.assertEqual(
                result.returncode,
                73,
                f"stdout={result.stdout!r}\nstderr={result.stderr!r}",
            )
            self.assertEqual(
                (work / "terminal").stat().st_ino,
                (work / "accept-state").stat().st_ino,
            )
            self.assertEqual(
                (work / "accepted").stat().st_ino,
                (work / "fence-state").stat().st_ino,
            )
            self.assertEqual(
                (work / "fenced").read_text(encoding="utf-8"), token + "\n"
            )
            self.assertEqual(firewall.read_text(encoding="utf-8"), "closed-policy\n")
            self.assertEqual(guard.read_text(encoding="utf-8"), self.guard)

            # Even an ACCEPT outcome is not blindly trusted after a reboot.
            # If its final no-guard/open-state proof is not exact, the
            # watchdog must hold persistent early drops rather than exit.  The
            # candidate UCI file may remain installed because the persisted
            # guard, not the old UCI policy, is the acceptance fail-closed
            # mechanism.
            reset_live_state()
            os.link(work / "accept-state", work / "terminal")
            os.link(work / "accepted-state", work / "accepted")
            (work / "accept-boot-id").write_text(
                armed_boot_id + "\n", encoding="utf-8"
            )
            (work / "accept-deadline").write_text(
                "4102444800\n", encoding="utf-8"
            )
            result = execute_watchdog()
            self.assertEqual(
                result.returncode,
                74,
                f"stdout={result.stdout!r}\nstderr={result.stderr!r}",
            )
            self.assertEqual(
                (work / "accepted").stat().st_ino,
                (work / "accepted-state").stat().st_ino,
            )
            self.assertFalse((work / "fenced").exists())
            self.assertEqual(firewall.read_text(encoding="utf-8"), "open-policy\n")
            self.assertEqual(guard.read_text(encoding="utf-8"), self.guard)
            self.assertFalse((work / "accept-complete").exists())
            self.assertEqual(
                trace.read_text(encoding="utf-8").splitlines()[-1], "sleep:2"
            )

            # With exact candidate UCI and the persisted boot guard, the same
            # accepted authorization converges to exact open state, creates a
            # hard-linked completion proof, and stays supervised instead of
            # treating the marker as permission to exit.
            reset_live_state()
            os.link(work / "accept-state", work / "terminal")
            os.link(work / "accepted-state", work / "accepted")
            guard.parent.mkdir(parents=True, exist_ok=True)
            guard.write_text(self.guard, encoding="utf-8")
            environment["ACCEPTANCE_MODE"] = "success"
            result = execute_watchdog()
            self.assertEqual(
                result.returncode,
                74,
                f"stdout={result.stdout!r}\nstderr={result.stderr!r}",
            )
            self.assertEqual(
                (work / "accept-complete").stat().st_ino,
                (work / "accepted-state").stat().st_ino,
            )
            self.assertFalse(guard.exists())
            self.assertEqual(firewall.read_text(encoding="utf-8"), "open-policy\n")
            success_trace = trace.read_text(encoding="utf-8").splitlines()
            self.assertIn("accept-reload", success_trace)
            self.assertEqual(success_trace[-1], "sleep:2")

    def test_fenced_ack_follows_closed_uci_and_exact_first_two_live_drops(self) -> None:
        rollback = self.heredoc("REMOTE_ROLLBACK")
        guard_live = self.shell_function(rollback, "guard_live")
        is_fenced = self.shell_function(rollback, "is_fenced")
        acknowledgement = self.shell_function(rollback, "ack_fenced")
        first_drop = 'comment "fabric etcd fence client requests"'
        second_drop = 'comment "fabric etcd fence server replies"'
        closed_uci = "Reject-services-to-root-etcd"
        first_rule = "first=$("
        second_rule = "second=$("

        for required in (
            first_drop,
            second_drop,
            closed_uci,
            first_rule,
            second_rule,
            "while :",
        ):
            with self.subTest(required=required):
                self.assertIn(required, rollback)
        self.assert_ordered(is_fenced, "assert_closed_lan", "guard_live")
        self.assert_ordered(
            guard_live, first_drop, second_drop, first_rule, second_rule
        )
        self.assert_ordered(
            acknowledgement,
            '"$work/fenced.next"',
            'mv "$work/fenced.next" "$work/fenced"',
            "sync",
        )
        self.assertNotIn(
            "/etc/init.d/fabric-router-etcd-policy-rollback disable",
            rollback,
        )
        persistent_loop = rollback.rindex("while :")
        proof = rollback.index("if is_fenced || apply_fence", persistent_loop)
        ack = rollback.index("ack_fenced", proof)
        self.assertLess(proof, ack)

    def test_disarm_rechecks_exact_open_state_before_accepting(self) -> None:
        disarm = self.heredoc("REMOTE_ACCEPT")
        accept_command, _, _ = self.terminal_link(disarm)
        claim = disarm.index(accept_command)

        for required in (
            'cmp -s "$firewall" "$work/candidate/firewall"',
            "Allow-platform-to-root-etcd-client",
            "Reject-services-to-root-etcd-internal",
            "nft -a list chain inet fw4 forward_lan",
            "expected_forward=",
            "actual_chain_count=",
            "terminal_rule=",
        ):
            with self.subTest(required=required):
                self.assertIn(required, disarm)
                self.assertLess(disarm.index(required), claim)
        self.assertLess(claim, disarm.index('"$init" stop'))
        self.assertLess(claim, disarm.index('"$init" disable'))
        self.assertIn("ROLLBACK_ACCEPTED", disarm[claim:])

    def test_failure_cleanup_waits_for_an_exact_fenced_ack(self) -> None:
        force = self.heredoc("REMOTE_FORCE_ROLLBACK")
        self.assert_ordered(
            force,
            '"$work/force-rollback"',
            '"$init" enable',
            '"$init" start',
            "FENCED",
        )
        for required in (
            '"$work/terminal"',
            '"$work/fenced"',
            '"$init" enabled',
            '"$init" running',
        ):
            with self.subTest(required=required):
                self.assertIn(required, force)
        self.assertRegex(force, r"while[^\n]*;\s*do|for [^\n]*;\s*do")

    def test_local_state_cannot_force_fence_after_acceptance(self) -> None:
        normal = self.normal_rollout()
        arm = normal.index("rollback_armed=true")
        disarm_call = normal.index("<<'REMOTE_ACCEPT'", arm)
        disarm_end = normal.index("\nREMOTE_ACCEPT", disarm_call)
        accepted = normal.index("rollback_accepted=true", disarm_end)
        cleanup_call = normal.index("<<'REMOTE_ROLLBACK_CLEANUP'", accepted)
        cleanup_end = normal.index("\nREMOTE_ROLLBACK_CLEANUP", cleanup_call)
        armed_false = normal.index("rollback_armed=false", cleanup_end)
        self.assertEqual(
            [arm, disarm_call, disarm_end, accepted, cleanup_call, cleanup_end, armed_false],
            sorted(
                [
                    arm,
                    disarm_call,
                    disarm_end,
                    accepted,
                    cleanup_call,
                    cleanup_end,
                    armed_false,
                ]
            ),
        )

        cleanup = self.helper[
            self.helper.index("cleanup() {") : self.helper.index(
                "\n}\ntrap cleanup EXIT", self.helper.index("cleanup() {")
            )
        ]
        self.assertIn("rollback_accepted=false", self.helper)
        self.assert_ordered(
            cleanup,
            "if $rollback_accepted; then",
            "elif $rollback_armed; then",
        )
        force = self.heredoc("REMOTE_FORCE_ROLLBACK")
        self.assertIn("ACCEPTED", force)
        self.assertLess(force.index('"$work/terminal"'), force.index("FENCED"))


if __name__ == "__main__":
    unittest.main()
