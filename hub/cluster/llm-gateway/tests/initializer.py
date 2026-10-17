"""Execute the real config initializer locally, with synthetic input and fake keys."""

import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

import yaml

GATEWAY = Path(__file__).resolve().parent.parent
PLACEHOLDER = "__MIMO_CODE_API_KEY__"
# A small parser fixture, not a duplicate of the production config.
TEMPLATE = 'unrelated:\n  enabled: false\nprovider-key: "' + PLACEHOLDER + '"\n'
FAKE_PROXY_KEY = "test-only-proxy_123-ABC"
FAKE_MIMO_KEY = "test-only-" + "".join(
    chr(value) for value in range(33, 127) if chr(value) not in '\\"'
)


class InitializerContract(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="llm-gateway-init-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "input config.yaml"
        self.output = self.root / "output config.yaml"
        self.source.write_text(TEMPLATE)

    def prepare(self, proxy=FAKE_PROXY_KEY, mimo=FAKE_MIMO_KEY):
        env = {key: value for key, value in os.environ.items()
               if key not in ("CLIPROXY_API_KEY", "MIMO_CODE_API_KEY")}
        for name, value in [("CLIPROXY_API_KEY", proxy), ("MIMO_CODE_API_KEY", mimo)]:
            if value is not None:
                env[name] = value
        return subprocess.run(
            ["/bin/sh", str(GATEWAY / "prepare-cliproxyapi.sh"),
             str(self.source), str(self.output)],
            env=env, capture_output=True, text=True, timeout=5,
        )

    def test_missing_or_invalid_mimo_fails_before_writing(self):
        invalid_keys = [None, "", 'test-only-"injection', "test-only-\\injection",
                        "test-only-é", "test-only-🔑", "test-only-\u0085"]
        invalid_keys += ["test-only-" + chr(value) for value in [*range(1, 33), 127]]
        for index, invalid in enumerate(invalid_keys):
            for existing in (False, True):
                with self.subTest(case=index, existing=existing):
                    self.output.unlink(missing_ok=True)
                    if existing:
                        self.output.write_text("untouched\n")
                        self.output.chmod(0o644)
                    result = self.prepare(mimo=invalid)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("MIMO_CODE_API_KEY", result.stderr)
                    if invalid:
                        self.assertNotIn(invalid, result.stderr)
                    if existing:
                        self.assertEqual(self.output.read_text(), "untouched\n")
                        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o644)
                    else:
                        self.assertFalse(self.output.exists())

    def test_requires_exactly_one_double_quoted_placeholder(self):
        templates = [
            TEMPLATE.replace(PLACEHOLDER, "missing"),
            TEMPLATE + '# duplicate ' + PLACEHOLDER + '\n',
            TEMPLATE.replace(PLACEHOLDER, PLACEHOLDER * 2),
            TEMPLATE.replace('"' + PLACEHOLDER + '"', PLACEHOLDER),
            TEMPLATE.replace('"' + PLACEHOLDER + '"', "'" + PLACEHOLDER + "'"),
            TEMPLATE.replace(PLACEHOLDER, "prefix" + PLACEHOLDER),
            TEMPLATE.replace(PLACEHOLDER, PLACEHOLDER + "suffix"),
        ]
        for index, template in enumerate(templates):
            with self.subTest(case=index):
                self.source.write_text(template)
                result = self.prepare()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("placeholder", result.stderr)
                self.assertNotIn(FAKE_MIMO_KEY, result.stderr)
                self.assertFalse(self.output.exists())

    def test_rejects_unresolved_placeholder_from_key(self):
        for name in ("mimo", "proxy"):
            with self.subTest(key=name):
                result = self.prepare(**{name: "test-only-" + PLACEHOLDER})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("placeholder", result.stderr)
                self.assertNotIn("test-only-", result.stderr)
                self.assertFalse(self.output.exists())

    def test_environment_template_markers_fail_before_writing(self):
        for key in [PLACEHOLDER, "@MIMO_CODE_API_KEY@", "@mimo-api-key@",
                    "@@MIMO_CODE_API_KEY@@", "@@OTHER_TOKEN@@",
                    "test-only-@@MIMO_CODE_API_KEY@@-suffix"]:
            for existing in (False, True):
                with self.subTest(marker=key, existing=existing):
                    self.output.unlink(missing_ok=True)
                    if existing:
                        self.output.write_text("untouched\n")
                    result = self.prepare(mimo=key)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("placeholder", result.stderr)
                    self.assertNotIn(key, result.stderr)
                    if existing:
                        self.assertEqual(self.output.read_text(), "untouched\n")
                    else:
                        self.assertFalse(self.output.exists())

    def test_existing_output_is_restricted_before_replacement(self):
        self.output.write_text("old config\n")
        self.output.chmod(0o644)
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertEqual(yaml.safe_load(self.output.read_text())["provider-key"], FAKE_MIMO_KEY)

    def test_missing_or_invalid_proxy_retains_url_safe_restriction(self):
        for invalid in [None, "", "test-only-space key", "test-only-:key", "test-only-+key",
                        'test-only-"key', "test-only-\\key", "test-only-\nkey", "test-only-é"]:
            with self.subTest(key=invalid):
                result = self.prepare(proxy=invalid)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("CLIPROXY_API_KEY", result.stderr)
                if invalid:
                    self.assertNotIn(invalid, result.stderr)
                self.assertFalse(self.output.exists())

    def test_shell_syntax_is_only_token_text(self):
        marker = self.root / "must-not-exist"
        key = "test-only-$(touch${IFS}" + str(marker) + ")-`id`-$HOME-%s"
        result = self.prepare(mimo=key)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout + result.stderr, "")
        self.assertFalse(marker.exists())
        self.assertEqual(yaml.safe_load(self.output.read_text())["provider-key"], key)

    def test_preserves_template_with_or_without_final_newline(self):
        for ending in ("", "\n", "\n\n"):
            with self.subTest(ending=repr(ending)):
                template = TEMPLATE.rstrip("\n") + ending
                self.source.write_text(template)
                result = self.prepare()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.output.read_text(), template.replace(PLACEHOLDER, FAKE_MIMO_KEY)
                                 + '\naccess:\n  api-keys:\n    - "' + FAKE_PROXY_KEY + '"\n')

    def test_missing_input_does_not_write_config(self):
        self.source.unlink()
        result = self.prepare()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(FAKE_MIMO_KEY, result.stdout + result.stderr)
        self.assertFalse(self.output.exists())

    def test_substitutes_all_safe_graph_characters_literally(self):
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout + result.stderr, "")
        rendered = self.output.read_text()
        self.assertEqual(rendered, TEMPLATE.replace(PLACEHOLDER, FAKE_MIMO_KEY)
                         + '\naccess:\n  api-keys:\n    - "' + FAKE_PROXY_KEY + '"\n')
        self.assertEqual(yaml.safe_load(rendered), {
            "unrelated": {"enabled": False}, "provider-key": FAKE_MIMO_KEY,
            "access": {"api-keys": [FAKE_PROXY_KEY]},
        })
        self.assertEqual(self.source.read_text(), TEMPLATE)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
