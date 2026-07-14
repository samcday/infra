#!/usr/bin/env python3
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from typing import Any


REPO_ROOT = pathlib.Path(__file__).parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "compare-fabric-nft-counters"
LOADER = importlib.machinery.SourceFileLoader(
    "compare_fabric_nft_counters", str(SCRIPT_PATH)
)
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None
counter_compare = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = counter_compare
LOADER.exec_module(counter_compare)


def selector(
    comment: str = "fabric selected rule", expected_matches: int = 1
) -> dict[str, Any]:
    return {
        "family": "inet",
        "table": "fabric_filter",
        "chain": "forward",
        "comment": comment,
        "expected_matches": expected_matches,
    }


def spec(
    *,
    rules: dict[str, dict[str, Any]] | None = None,
    increase: list[str] | None = None,
    unchanged: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "version": 1,
        "rules": rules if rules is not None else {"selected": selector()},
        "require_increase": increase if increase is not None else ["selected"],
        "require_unchanged": unchanged if unchanged is not None else [],
    }


def rule(
    *,
    comment: str = "fabric selected rule",
    handle: int = 41,
    packets: Any = 10,
    bytes_: Any = 1000,
    expressions: list[Any] | None = None,
    verdict: str = "accept",
) -> dict[str, Any]:
    return {
        "family": "inet",
        "table": "fabric_filter",
        "chain": "forward",
        "handle": handle,
        "comment": comment,
        "expr": expressions
        if expressions is not None
        else [
            {"match": {"op": "==", "left": {"meta": {"key": "iifname"}}, "right": "br-fabric"}},
            {"counter": {"packets": packets, "bytes": bytes_}},
            {verdict: None},
        ],
    }


def snapshot(*rules: dict[str, Any]) -> dict[str, Any]:
    return {
        "nftables": [
            {"metainfo": {"json_schema_version": 1}},
            *({"rule": item} for item in rules),
        ]
    }


class SuccessfulComparisonTests(unittest.TestCase):
    def test_increase_and_unchanged_are_sorted_and_derived(self) -> None:
        rules = {
            "zeta": selector("must increase"),
            "alpha": selector("must stay fixed"),
        }
        policy = spec(rules=rules, increase=["zeta"], unchanged=["alpha"])
        before = snapshot(
            rule(comment="must increase", packets=2, bytes_=200),
            rule(comment="must stay fixed", handle=42, packets=5, bytes_=500),
        )
        after = snapshot(
            rule(comment="must stay fixed", handle=42, packets=5, bytes_=500),
            rule(comment="must increase", packets=3, bytes_=350),
        )

        results = counter_compare.compare_snapshots(before, after, policy)

        self.assertEqual([item["logical_name"] for item in results], ["alpha", "zeta"])
        self.assertEqual(results[0]["result"], "pass")
        self.assertEqual(results[0]["delta"], {"packets": 0, "bytes": 0})
        self.assertEqual(results[1]["result"], "pass")
        self.assertEqual(results[1]["delta"], {"packets": 1, "bytes": 150})
        self.assertEqual(len(results[1]["matches"][0]["semantic_sha256"]), 64)
        self.assertEqual(
            set(results[1]),
            {
                "logical_name",
                "before",
                "after",
                "delta",
                "matches",
                "policy",
                "result",
            },
        )
        self.assertEqual(len(results[1]["matches"]), 1)
        self.assertEqual(results[1]["policy"], "require_increase")

    def test_policy_misses_are_results_not_malformed_input(self) -> None:
        no_packet_delta = counter_compare.compare_snapshots(
            snapshot(rule()), snapshot(rule(packets=10, bytes_=1001)), spec()
        )
        self.assertEqual(no_packet_delta[0]["result"], "fail")

        unchanged_policy = spec(increase=[], unchanged=["selected"])
        changed = counter_compare.compare_snapshots(
            snapshot(rule()), snapshot(rule(packets=11, bytes_=1001)), unchanged_policy
        )
        self.assertEqual(changed[0]["result"], "fail")


class CompiledMatchTests(unittest.TestCase):
    def two_match_spec(self) -> dict[str, Any]:
        return spec(
            rules={"dns": selector("fabric dns", expected_matches=2)},
            increase=["dns"],
            unchanged=[],
        )

    def test_every_match_increases_and_aggregates_are_derived(self) -> None:
        before = snapshot(
            rule(comment="fabric dns", handle=52, packets=20, bytes_=2000),
            rule(comment="fabric dns", handle=51, packets=10, bytes_=1000),
        )
        after = snapshot(
            rule(comment="fabric dns", handle=51, packets=12, bytes_=1300),
            rule(comment="fabric dns", handle=52, packets=21, bytes_=2100),
        )

        result = counter_compare.compare_snapshots(
            before, after, self.two_match_spec()
        )[0]

        self.assertEqual(result["result"], "pass")
        self.assertEqual(result["before"], {"packets": 30, "bytes": 3000})
        self.assertEqual(result["after"], {"packets": 33, "bytes": 3400})
        self.assertEqual(result["delta"], {"packets": 3, "bytes": 400})
        self.assertEqual(
            [match["identity"]["handle"] for match in result["matches"]],
            [51, 52],
        )
        self.assertEqual(
            [match["result"] for match in result["matches"]], ["pass", "pass"]
        )

    def test_aggregate_increase_cannot_hide_unchanged_match(self) -> None:
        before = snapshot(
            rule(comment="fabric dns", handle=51, packets=10, bytes_=1000),
            rule(comment="fabric dns", handle=52, packets=20, bytes_=2000),
        )
        after = snapshot(
            rule(comment="fabric dns", handle=51, packets=11, bytes_=1100),
            rule(comment="fabric dns", handle=52, packets=20, bytes_=2000),
        )

        result = counter_compare.compare_snapshots(
            before, after, self.two_match_spec()
        )[0]

        self.assertEqual(result["delta"], {"packets": 1, "bytes": 100})
        self.assertEqual(result["result"], "fail")
        self.assertEqual(
            [match["result"] for match in result["matches"]], ["pass", "fail"]
        )

    def test_wrong_compiled_match_count_is_malformed(self) -> None:
        with self.assertRaises(counter_compare.ComparisonError):
            counter_compare.compare_snapshots(
                snapshot(rule(comment="fabric dns")),
                snapshot(
                    rule(comment="fabric dns", handle=51),
                    rule(comment="fabric dns", handle=52),
                ),
                self.two_match_spec(),
            )

    def test_one_compiled_match_identity_or_semantics_change_is_malformed(self) -> None:
        before = snapshot(
            rule(comment="fabric dns", handle=51),
            rule(comment="fabric dns", handle=52),
        )
        after_handle_change = snapshot(
            rule(comment="fabric dns", handle=51, packets=11, bytes_=1001),
            rule(comment="fabric dns", handle=53, packets=11, bytes_=1001),
        )
        after_semantic_change = snapshot(
            rule(comment="fabric dns", handle=51, packets=11, bytes_=1001),
            rule(
                comment="fabric dns",
                handle=52,
                packets=11,
                bytes_=1001,
                verdict="drop",
            ),
        )
        for after in (after_handle_change, after_semantic_change):
            with self.subTest(after=after):
                with self.assertRaises(counter_compare.ComparisonError):
                    counter_compare.compare_snapshots(
                        before, after, self.two_match_spec()
                    )


class StrictSpecTests(unittest.TestCase):
    def assert_bad_spec(self, policy: Any) -> None:
        with self.assertRaises(counter_compare.ComparisonError):
            counter_compare.validate_spec(policy)

    def test_unknown_missing_and_bad_version_fields_are_rejected(self) -> None:
        unknown = spec()
        unknown["extra"] = True
        missing = spec()
        del missing["rules"]
        boolean_version = spec()
        boolean_version["version"] = True
        selector_extra = spec()
        selector_extra["rules"]["selected"]["handle"] = "41"
        for policy in (unknown, missing, boolean_version, selector_extra):
            with self.subTest(policy=policy):
                self.assert_bad_spec(policy)

    def test_duplicate_overlap_unknown_and_unassigned_policies_are_rejected(self) -> None:
        duplicate = spec(increase=["selected", "selected"])
        overlap = spec(increase=["selected"], unchanged=["selected"])
        unknown = spec(increase=["other"])
        unassigned = spec(increase=[], unchanged=[])
        for policy in (duplicate, overlap, unknown, unassigned):
            with self.subTest(policy=policy):
                self.assert_bad_spec(policy)

    def test_reused_selectors_are_rejected(self) -> None:
        self.assert_bad_spec(
            spec(
                rules={"one": selector(), "two": selector()},
                increase=["one"],
                unchanged=["two"],
            )
        )

    def test_expected_matches_is_mandatory_and_bounded(self) -> None:
        missing = selector()
        del missing["expected_matches"]
        bad_selectors = [missing]
        for value in (True, 0, 17, "2"):
            candidate = selector()
            candidate["expected_matches"] = value
            bad_selectors.append(candidate)
        for bad_selector in bad_selectors:
            with self.subTest(selector=bad_selector):
                self.assert_bad_spec(
                    spec(rules={"selected": bad_selector}, increase=["selected"])
                )

    def test_unsafe_logical_names_are_rejected(self) -> None:
        for name in ("", "Upper", "has space", "../escape"):
            with self.subTest(name=name):
                self.assert_bad_spec(
                    spec(rules={name: selector()}, increase=[name], unchanged=[])
                )

    def test_duplicate_json_object_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "duplicate.json"
            path.write_text('{"version":1,"version":1}', encoding="utf-8")
            with self.assertRaises(counter_compare.ComparisonError):
                counter_compare.load_json(path)

    def test_lone_unicode_surrogates_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for name, contents in (
                ("key", '{"\\ud800":1}'),
                ("value", '{"value":"\\ud800"}'),
            ):
                with self.subTest(name=name):
                    path = root / f"{name}.json"
                    path.write_text(contents, encoding="utf-8")
                    with self.assertRaises(counter_compare.ComparisonError):
                        counter_compare.load_json(path)

    def test_floating_point_numbers_are_rejected_during_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "float.json"
            for contents in ('{"value":1.5}', '{"value":1e400}'):
                with self.subTest(contents=contents):
                    path.write_text(contents, encoding="utf-8")
                    with self.assertRaises(counter_compare.ComparisonError):
                        counter_compare.load_json(path)


class SnapshotValidationTests(unittest.TestCase):
    def assert_bad_snapshots(self, before: Any, after: Any | None = None) -> None:
        with self.assertRaises(counter_compare.ComparisonError):
            counter_compare.compare_snapshots(
                before, after if after is not None else snapshot(rule(packets=11, bytes_=1001)), spec()
            )

    def test_missing_and_duplicate_matches_are_rejected(self) -> None:
        self.assert_bad_snapshots(snapshot())
        self.assert_bad_snapshots(snapshot(rule(), rule()))

    def test_missing_duplicate_and_malformed_counters_are_rejected(self) -> None:
        cases = [
            rule(expressions=[{"accept": None}]),
            rule(
                expressions=[
                    {"counter": {"packets": 1, "bytes": 2}},
                    {"counter": {"packets": 3, "bytes": 4}},
                ]
            ),
            rule(expressions=[{"counter": {"packets": 1}}]),
            rule(expressions=[{"counter": {"packets": 1, "bytes": 2, "extra": 0}}]),
        ]
        for malformed_rule in cases:
            with self.subTest(expressions=malformed_rule["expr"]):
                self.assert_bad_snapshots(snapshot(malformed_rule))

    def test_negative_and_boolean_counter_values_are_rejected(self) -> None:
        over_uint64 = 1 << 64
        for packets, bytes_ in (
            (-1, 2),
            (1, -2),
            (True, 2),
            (1, False),
            (over_uint64, 2),
            (1, over_uint64),
        ):
            with self.subTest(packets=packets, bytes=bytes_):
                self.assert_bad_snapshots(snapshot(rule(packets=packets, bytes_=bytes_)))

    def test_invalid_or_oversized_handles_are_rejected(self) -> None:
        for handle in (-1, True, 1 << 64):
            with self.subTest(handle=handle):
                self.assert_bad_snapshots(snapshot(rule(handle=handle)))

    def test_unknown_snapshot_top_level_fields_are_rejected(self) -> None:
        malformed = snapshot(rule())
        malformed["unexpected"] = []
        self.assert_bad_snapshots(malformed)

    def test_decreases_are_rejected(self) -> None:
        self.assert_bad_snapshots(
            snapshot(rule(packets=10, bytes_=1000)),
            snapshot(rule(packets=9, bytes_=1001)),
        )
        self.assert_bad_snapshots(
            snapshot(rule(packets=10, bytes_=1000)),
            snapshot(rule(packets=11, bytes_=999)),
        )

    def test_handle_identity_change_is_rejected(self) -> None:
        self.assert_bad_snapshots(
            snapshot(rule()), snapshot(rule(handle=42, packets=11, bytes_=1001))
        )

    def test_non_counter_semantic_change_is_rejected(self) -> None:
        self.assert_bad_snapshots(
            snapshot(rule()), snapshot(rule(packets=11, bytes_=1001, verdict="drop"))
        )


class CLITests(unittest.TestCase):
    def run_cli(
        self, before: Any, after: Any, policy: Any
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            paths = {}
            for name, value in (("before", before), ("after", after), ("spec", policy)):
                paths[name] = root / f"{name}.json"
                paths[name].write_text(json.dumps(value), encoding="utf-8")
            return subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "--before",
                    str(paths["before"]),
                    "--after",
                    str(paths["after"]),
                    "--spec",
                    str(paths["spec"]),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

    def test_cli_emits_deterministic_json_and_status(self) -> None:
        passed = self.run_cli(
            snapshot(rule()), snapshot(rule(packets=11, bytes_=1001)), spec()
        )
        self.assertEqual(passed.returncode, 0, passed.stderr)
        parsed = json.loads(passed.stdout)
        self.assertEqual(parsed["version"], 1)
        self.assertEqual(parsed["result"], "pass")
        self.assertEqual(parsed["results"][0]["result"], "pass")
        self.assertEqual(parsed["results"][0]["policy"], "require_increase")
        self.assertNotIn("expr", passed.stdout)
        self.assertNotIn("br-fabric", passed.stdout)

        repeated = self.run_cli(
            snapshot(rule()), snapshot(rule(packets=11, bytes_=1001)), spec()
        )
        self.assertEqual(repeated.stdout, passed.stdout)

        failed = self.run_cli(snapshot(rule()), snapshot(rule()), spec())
        self.assertEqual(failed.returncode, 1, failed.stderr)
        failed_output = json.loads(failed.stdout)
        self.assertEqual(failed_output["result"], "fail")
        self.assertEqual(failed_output["results"][0]["result"], "fail")

    def test_invalid_input_has_no_json_output(self) -> None:
        failed = self.run_cli(snapshot(), snapshot(rule()), spec())
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(failed.stdout, "")
        self.assertTrue(failed.stderr.startswith("ERROR: "))

    def test_invalid_unicode_is_exit_two_without_traceback(self) -> None:
        policy = spec()
        policy["rules"]["selected"]["comment"] = "\ud800"
        failed = self.run_cli(snapshot(rule()), snapshot(rule()), policy)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(failed.stdout, "")
        self.assertTrue(failed.stderr.startswith("ERROR: "))
        self.assertNotIn("Traceback", failed.stderr)

    def test_overflowing_float_is_exit_two_without_traceback(self) -> None:
        policy = spec()
        policy["rules"]["selected"]["comment"] = 1e400
        failed = self.run_cli(snapshot(rule()), snapshot(rule()), policy)
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(failed.stdout, "")
        self.assertTrue(failed.stderr.startswith("ERROR: "))
        self.assertNotIn("Traceback", failed.stderr)


if __name__ == "__main__":
    unittest.main()
