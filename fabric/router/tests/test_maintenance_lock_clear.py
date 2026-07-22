import pathlib
import subprocess
import unittest


REPO = pathlib.Path(__file__).parents[3]
HELPER = REPO / "scripts" / "clear-stale-fabric-maintenance-lock"


class MaintenanceLockClearTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = HELPER.read_text(encoding="utf-8")

    def test_shell_is_syntactically_valid(self) -> None:
        subprocess.run(["bash", "-n", str(HELPER)], check=True)

    def test_confirmation_binds_source_and_complete_lock_identity(self) -> None:
        for required in (
            "CLEAR-STALE-FABRIC-MAINTENANCE-LOCK",
            '"$confirmation_prefix" "$head_commit"',
            '"$lock_uid" "$lock_resource_version" "$lock_state_sha256"',
            "git fetch --quiet origin main",
            'HEAD must exactly match pushed origin/main',
            'helper source must exactly match the pushed revision',
            '[[ $(git branch --show-current) == main ]]',
            '[[ -z $(git status --porcelain) ]]',
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.source)

    def test_only_old_nonlocal_locks_can_reach_the_attended_gate(self) -> None:
        for required in (
            "readonly minimum_age_seconds=1800",
            "lock creation time is in the future",
            "minimum stale age",
            'holder_host == "$(hostname)" && -d /proc/$holder_pid',
            "recorded local holder PID",
            "lock holder does not contain a valid PID and operation identity",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.source)

    def test_lock_is_reread_and_deleted_with_both_preconditions(self) -> None:
        confirmation = self.source.index('[[ $confirmation == "$expected" ]]')
        second_revision = self.source.index("assert_pushed_revision", confirmation)
        second_read = self.source.index("load_and_validate_lock", second_revision)
        deletion = self.source.index("delete_response=", second_read)
        absence = self.source.index("--ignore-not-found --output=name", deletion)
        self.assertLess(confirmation, second_revision)
        self.assertLess(second_revision, second_read)
        self.assertLess(second_read, deletion)
        self.assertLess(deletion, absence)
        for required in (
            "preconditions: {uid: $uid, resourceVersion: $rv}",
            'lock_uid == "$confirmed_uid"',
            'lock_resource_version == "$confirmed_resource_version"',
            'lock_state_sha256 == "$confirmed_state_sha256"',
            '"$repo_root/scripts/ik" --context=fabric --request-timeout=10s',
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.source)

    def test_help_states_remote_holder_limit_and_operator_attestation(self) -> None:
        self.assertIn("A remote holder cannot be proved dead automatically", self.source)
        self.assertIn("operator's attestation", self.source)
        self.assertIn("It never", self.source)
        self.assertIn("adopts or removes a replacement lock", self.source)


if __name__ == "__main__":
    unittest.main()
