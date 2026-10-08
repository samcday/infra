import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest import mock
import urllib.error
import urllib.request


SPEC = importlib.util.spec_from_file_location(
    "cleanup", Path(__file__).resolve().parents[1] / "cleanup.py"
)
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)

ACCOUNT = "a001"
BUCKET = "b001"
PRIVATE_NAME = "private/never-print-this-name"
KEY_ID = "never-print-key-id"
KEY = "never-print-application-key"
TOKEN = "never-print-authorization-token"


def authorization():
    return {
        "accountId": ACCOUNT,
        "apiUrl": "https://api001.backblazeb2.com",
        "authorizationToken": TOKEN,
        "allowed": {
            "bucketId": BUCKET, "bucketName": cleanup.BUCKET_NAME, "namePrefix": None,
            "capabilities": ["listBuckets", "listFiles", "readFiles", "writeFiles", "deleteFiles"],
        },
    }


def file_record(file_id, action="upload", size=10, name=PRIVATE_NAME):
    return {
        "accountId": ACCOUNT, "bucketId": BUCKET,
        "fileId": file_id, "fileName": name, "action": action, "contentLength": size,
    }


def version_page(files=(), next_name=None, next_id=None):
    return {"files": list(files), "nextFileName": next_name, "nextFileId": next_id}


class FakeClock:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeB2:
    """In-memory API model with inclusive two-field version cursors."""

    def __init__(self, versions=(), unfinished=(), page_size=1):
        self.auth = authorization()
        self.buckets = [{
            "accountId": ACCOUNT, "bucketId": BUCKET, "bucketName": cleanup.BUCKET_NAME,
        }]
        self.versions = copy.deepcopy(list(versions))
        self.unfinished = copy.deepcopy(list(unfinished))
        self.parts = {item["fileId"]: ["part1", "part2"] for item in self.unfinished}
        self.page_size = page_size
        self.calls = []
        self.auth_calls = 0
        self.mutation_errors = {}
        self.scans = {}
        self.verification_versions = None
        self.verification_unfinished = None

    def authorize(self, key_id, key):
        self.auth_calls += 1
        return copy.deepcopy(self.auth)

    def call(self, operation, payload):
        self.calls.append((operation, copy.deepcopy(payload)))
        if operation == "b2_list_buckets":
            assert payload == {"accountId": ACCOUNT, "bucketName": cleanup.BUCKET_NAME}
            return {"buckets": copy.deepcopy(self.buckets)}
        if operation in ("b2_list_file_versions", "b2_list_unfinished_large_files"):
            assert payload["bucketId"] == BUCKET
            assert "delimiter" not in payload and "prefix" not in payload
            unfinished = operation == "b2_list_unfinished_large_files"
            # API contract limits, deliberately independent of script constants.
            assert 1 <= payload["maxFileCount"] <= (100 if unfinished else 10000)
            if "startFileId" not in payload:
                self.scans[operation] = self.scans.get(operation, 0) + 1
            files = self.unfinished if unfinished else self.versions
            final = self.verification_unfinished if unfinished else self.verification_versions
            if self.scans[operation] == 3 and final is not None:
                files = final
            start = 0
            if "startFileId" in payload:
                start = next(i for i, item in enumerate(files)
                             if item["fileId"] == payload["startFileId"])
                if not unfinished:
                    assert files[start]["fileName"] == payload["startFileName"]
            page = files[start:start + self.page_size]
            following = files[start + self.page_size:]
            next_id = following[0]["fileId"] if following else None
            if unfinished:
                return {"files": copy.deepcopy(page), "nextFileId": next_id}
            return version_page(copy.deepcopy(page),
                                following[0]["fileName"] if following else None, next_id)
        assert operation in ("b2_delete_file_version", "b2_cancel_large_file")
        file_id = payload["fileId"]
        if file_id in self.mutation_errors:
            raise self.mutation_errors[file_id]
        if operation == "b2_delete_file_version":
            assert set(payload) == {"fileId", "fileName"}  # Never bypassGovernance.
            record = next(item for item in self.versions if item["fileId"] == file_id)
            assert record["fileName"] == payload["fileName"]
            self.versions.remove(record)
        else:
            assert set(payload) == {"fileId"}
            self.versions = [item for item in self.versions if item["fileId"] != file_id]
            self.unfinished = [item for item in self.unfinished if item["fileId"] != file_id]
            self.parts.pop(file_id, None)
        return {"fileId": file_id, "fileName": PRIVATE_NAME}

    def mutations(self):
        return [(operation, payload) for operation, payload in self.calls
                if operation in ("b2_delete_file_version", "b2_cancel_large_file")]


class OfflineTest(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket", "socket.create_connection"):
            patch = mock.patch(target, side_effect=AssertionError("network forbidden"))
            patch.start()
            self.addCleanup(patch.stop)
        self.output = io.StringIO()
        capture = contextlib.redirect_stdout(self.output)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)
        self.clock = FakeClock()
        self.deadline = cleanup.Deadline(clock=self.clock, sleep=self.clock.sleep)

    def job(self, client=None, mode="purge", **kwargs):
        client = FakeB2() if client is None else client
        ids = {"expected_account_id": ACCOUNT, "expected_bucket_id": BUCKET} if mode == "purge" else {}
        ids.update(kwargs)
        return cleanup.Cleanup(client, self.deadline, mode, cleanup.BUCKET_NAME, **ids)

    def assert_error(self, code, callable_, *args):
        with self.assertRaises(cleanup.CleanupError) as raised:
            callable_(*args)
        self.assertEqual(raised.exception.code, code)

    def assert_private_logs(self):
        for private in (PRIVATE_NAME, KEY_ID, KEY, TOKEN, "raw-private-message"):
            self.assertNotIn(private, self.output.getvalue())


class ScopeTests(OfflineTest):
    def test_wrong_secret_bucket_before_authorization(self):
        for name in ("fastboop", "rokkitpokkit", "samcday-fastboop", "", None):
            with self.subTest(name=name):
                client = FakeB2()
                self.assert_error("wrong_bucket_name", cleanup.Cleanup,
                                  client, self.deadline, "inventory", name)
                self.assertEqual(client.auth_calls, 0)
                self.assertEqual(client.calls, [])

    def test_purge_requires_both_explicit_ids(self):
        for account, bucket in (("", ""), (ACCOUNT, ""), ("", BUCKET),
                                ("not-an-id", BUCKET)):
            with self.subTest(account=account, bucket=bucket):
                client = FakeB2()
                with self.assertRaises(cleanup.CleanupError):
                    self.job(client, expected_account_id=account, expected_bucket_id=bucket)
                self.assertEqual(client.calls, [])

    def test_wrong_pinned_account_or_bucket(self):
        for field, code in (("expected_account_id", "wrong_account_id"),
                            ("expected_bucket_id", "wrong_bucket_id")):
            with self.subTest(field=field):
                client = FakeB2()
                self.assert_error(code, self.job(client, **{field: "c001"}).run, KEY_ID, KEY)
                self.assertEqual(client.mutations(), [])

    def test_unrestricted_prefix_multibucket_and_wrong_name(self):
        cases = (
            ({"bucketId": None, "bucketName": None}, "unrestricted_credentials"),
            ({"bucketId": ""}, "unrestricted_credentials"),
            ({"bucketId": [BUCKET, "c001"]}, "unrestricted_credentials"),
            ({"buckets": [{"bucketId": BUCKET}, {"bucketId": "c001"}]},
             "ambiguous_bucket_scope"),
            ({"namePrefix": PRIVATE_NAME}, "prefix_restricted"),
            ({"bucketName": "fastboop"}, "wrong_bucket_name"),
            ({"bucketName": "rokkitpokkit"}, "wrong_bucket_name"),
        )
        for changes, code in cases:
            for mode in ("inventory", "purge"):
                with self.subTest(changes=changes, mode=mode):
                    client = FakeB2()
                    client.auth["allowed"].update(changes)
                    self.assert_error(code, self.job(client, mode).run, KEY_ID, KEY)
                    self.assertEqual(client.calls, [])
        self.assert_private_logs()

    def test_empty_prefix_and_inventory_listing_only_capabilities(self):
        client = FakeB2()
        client.auth["allowed"]["namePrefix"] = ""
        client.auth["allowed"]["capabilities"] = ["listBuckets", "listFiles"]
        self.job(client, "inventory").run(KEY_ID, KEY)
        self.assertEqual(client.mutations(), [])

    def test_missing_prefix_scope_fails_closed(self):
        client = FakeB2()
        del client.auth["allowed"]["namePrefix"]
        self.assert_error("missing_prefix_scope", self.job(client).run, KEY_ID, KEY)
        self.assertEqual(client.calls, [])

    def test_missing_capabilities_stop_before_mutation(self):
        for missing in ("listBuckets", "listFiles", "writeFiles", "deleteFiles"):
            with self.subTest(missing=missing):
                client = FakeB2()
                client.auth["allowed"]["capabilities"].remove(missing)
                self.assert_error("missing_capabilities", self.job(client).run, KEY_ID, KEY)
                self.assertEqual(client.calls, [])

    def test_list_buckets_disagreement(self):
        cases = (
            [], [FakeB2().buckets[0], FakeB2().buckets[0]],
            [{"accountId": "c001", "bucketId": BUCKET, "bucketName": cleanup.BUCKET_NAME}],
            [{"accountId": ACCOUNT, "bucketId": "c001", "bucketName": cleanup.BUCKET_NAME}],
            [{"accountId": ACCOUNT, "bucketId": BUCKET, "bucketName": "rokkitpokkit"}],
        )
        for buckets in cases:
            with self.subTest(buckets=buckets):
                client = FakeB2()
                client.buckets = buckets
                self.assert_error("bucket_list_disagreement", self.job(client).run, KEY_ID, KEY)
                self.assertEqual(client.mutations(), [])

    def test_foreign_file_scope_before_page_mutations(self):
        for field in ("accountId", "bucketId"):
            with self.subTest(field=field):
                client = FakeB2([file_record("one"), file_record("two")], page_size=100)
                client.versions[1][field] = "c001"
                self.assert_error("file_scope_disagreement", self.job(client).run, KEY_ID, KEY)
                self.assertEqual(client.mutations(), [])


class LifecycleTests(OfflineTest):
    def test_listing_endpoints_have_separate_page_limits(self):
        client = FakeB2()
        self.job(client, mode="inventory").run(KEY_ID, KEY)
        limits = {op: data["maxFileCount"] for op, data in client.calls
                  if op in ("b2_list_file_versions", "b2_list_unfinished_large_files")}
        self.assertEqual(limits["b2_list_file_versions"], 1000)
        self.assertEqual(limits["b2_list_unfinished_large_files"], 100)
        self.assertEqual(client.mutations(), [])

    def test_main_missing_credentials_or_invalid_mode_is_sanitized(self):
        for env in (
            {"B2_BUCKET_NAME": cleanup.BUCKET_NAME},
            {"B2_BUCKET_NAME": cleanup.BUCKET_NAME, "B2_CLEANUP_MODE": PRIVATE_NAME},
        ):
            with self.subTest(env=env):
                client = FakeB2()
                with mock.patch.object(cleanup, "B2HTTP", return_value=client):
                    self.assertEqual(cleanup.main(env), 1)
                self.assertEqual(client.auth_calls, 0)
                self.assertEqual(client.calls, [])
        self.assert_private_logs()

    def test_readonly_all_same_name_versions_and_unfinished_pages(self):
        versions = [file_record("v1", size=12), file_record("v2", "hide", 0),
                    file_record("v3", "start", 0), file_record("v4", size=7)]
        unfinished = [file_record("v3", "start", 0), file_record("u2", "start", 0)]
        client = FakeB2(versions, unfinished)
        job = self.job(client, "inventory")
        job.run(KEY_ID, KEY)
        self.assertEqual(job.initial, {
            "versions": 4, "upload_versions": 2, "hide_markers": 1,
            "start_versions": 1, "version_bytes": 19, "unfinished_uploads": 2,
        })
        self.assertEqual(client.mutations(), [])
        version_requests = [payload for op, payload in client.calls if op == "b2_list_file_versions"]
        self.assertEqual([p.get("startFileId") for p in version_requests], [None, "v2", "v3", "v4"])
        self.assertTrue(all(p["startFileName"] == PRIVATE_NAME for p in version_requests[1:]))
        unfinished_requests = [p for op, p in client.calls if op == "b2_list_unfinished_large_files"]
        self.assertEqual([p.get("startFileId") for p in unfinished_requests], [None, "u2"])
        self.assert_private_logs()

    def test_purge_hide_start_and_unfinished_parts_then_verify_both_empty(self):
        versions = [file_record("v1"), file_record("v2", "hide", 0),
                    file_record("v3", "start", 0), file_record("v4")]
        unfinished = [file_record("v3", "start", 0), file_record("u2", "start", 0),
                      file_record("u3", "start", 0)]
        client = FakeB2(versions, unfinished)
        job = self.job(client)
        job.run(KEY_ID, KEY)
        self.assertEqual(job.progress, {
            "delete_requests": 3, "deleted_versions": 3,
            "cancel_requests": 3, "cancelled_uploads": 3,
        })
        self.assertEqual(client.versions, [])
        self.assertEqual(client.unfinished, [])
        self.assertEqual(client.parts, {})
        self.assertEqual(job.verification, cleanup.empty_summary())
        self.assertEqual(client.scans["b2_list_file_versions"], 3)
        self.assertEqual(client.scans["b2_list_unfinished_large_files"], 3)
        self.assertIn('"event": "purge_verified_empty"', self.output.getvalue())
        self.assert_private_logs()

    def test_retention_stops_with_partial_progress_and_no_completion(self):
        client = FakeB2([file_record("one"), file_record("locked", "hide", 0),
                         file_record("later")], page_size=100)
        client.mutation_errors["locked"] = cleanup.CleanupError(
            "b2_delete_file_version", "access_denied"
        )
        env = {
            "B2_CLEANUP_MODE": "purge", "B2_BUCKET_NAME": cleanup.BUCKET_NAME,
            "B2_EXPECTED_ACCOUNT_ID": ACCOUNT, "B2_EXPECTED_BUCKET_ID": BUCKET,
            "B2_APPLICATION_KEY_ID": KEY_ID, "B2_APPLICATION_KEY": KEY,
        }
        with mock.patch.object(cleanup, "B2HTTP", return_value=client):
            self.assertEqual(cleanup.main(env), 1)
        stopped = json.loads(self.output.getvalue().splitlines()[-1])
        self.assertEqual(stopped["event"], "stopped")
        self.assertFalse(stopped["complete"])
        self.assertEqual(stopped["code"], "access_denied")
        self.assertEqual(stopped["progress"]["deleted_versions"], 1)
        self.assertEqual(stopped["progress"]["delete_requests"], 2)
        self.assertEqual([item["fileId"] for item in client.versions], ["locked", "later"])
        self.assertNotIn("purge_verified_empty", self.output.getvalue())
        self.assert_private_logs()

    def test_final_nonempty_versions_or_unfinished_never_claims_completion(self):
        for field in ("verification_versions", "verification_unfinished"):
            with self.subTest(field=field):
                self.output.seek(0)
                self.output.truncate(0)
                client = FakeB2()
                setattr(client, field, [file_record("leftover", "start", 0)])
                job = self.job(client)
                self.assert_error("bucket_not_empty", job.run, KEY_ID, KEY)
                self.assertNotIn("purge_verified_empty", self.output.getvalue())
                self.assertEqual(client.scans["b2_list_file_versions"], 3)
                self.assertEqual(client.scans["b2_list_unfinished_large_files"], 3)

    def test_bad_cursor_missing_pair_or_repeated_pair_fails_closed(self):
        for result, code in (
            ({"files": [], "nextFileId": None}, "missing_cursor"),
            (version_page([file_record("one")], PRIVATE_NAME, None), "incomplete_cursor"),
            (version_page([file_record("one")], PRIVATE_NAME, "one"), "pagination_stalled"),
        ):
            with self.subTest(code=code):
                client = FakeB2()
                original = client.call

                def call(operation, payload):
                    if operation == "b2_list_file_versions":
                        return result
                    return original(operation, payload)

                client.call = call
                self.assert_error(code, self.job(client).run, KEY_ID, KEY)
                self.assertEqual(client.mutations(), [])

    def test_deadline_before_mutation(self):
        client = FakeB2()
        job = self.job(client)
        job.account_id, job.bucket_id = ACCOUNT, BUCKET
        self.clock.now = cleanup.DEADLINE_SECONDS
        self.assert_error("deadline_exceeded", job.mutate, file_record("one"))
        self.assertEqual(client.mutations(), [])


class Response(io.BytesIO):
    pass


class FakeOpener:
    def __init__(self, *actions):
        self.actions = list(actions)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        action = self.actions.pop(0)
        if isinstance(action, Exception):
            raise action
        if isinstance(action, dict):
            return Response(json.dumps(action).encode())
        return action


def http_error(status, code="access_denied", retry_after=None):
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    body = json.dumps({"code": code, "message": "raw-private-message " + PRIVATE_NAME})
    return urllib.error.HTTPError(cleanup.AUTH_URL, status, "raw-private-message",
                                  headers, Response(body.encode()))


class HTTPTests(OfflineTest):
    def test_auth_get_v2_and_post_exact_scoped_endpoint(self):
        opener = FakeOpener(authorization(), {"buckets": []})
        client = cleanup.B2HTTP(self.deadline, opener)
        client.authorize(KEY_ID, KEY)
        client.call("b2_list_buckets", {"accountId": ACCOUNT, "bucketName": cleanup.BUCKET_NAME})
        auth_request, timeout = opener.requests[0]
        self.assertEqual(auth_request.full_url, cleanup.AUTH_URL)
        self.assertEqual(auth_request.get_method(), "GET")
        self.assertTrue(auth_request.get_header("Authorization").startswith("Basic "))
        self.assertLessEqual(timeout, cleanup.HTTP_TIMEOUT)
        request, _ = opener.requests[1]
        self.assertEqual(request.full_url, "https://api001.backblazeb2.com/b2api/v2/b2_list_buckets")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), TOKEN)
        self.assert_private_logs()

    def test_reject_unvalidated_api_origins_before_token_use(self):
        for url in (
            "http://api001.backblazeb2.com", "https://api001.backblazeb2.com.evil.test",
            "https://evil.test", "https://api001.backblazeb2.com:444",
            "https://user@api001.backblazeb2.com", "https://api001.backblazeb2.com/path",
            "https://api001.backblazeb2.com?x=1", "https://api001.backblazeb2.com#fragment",
            "https://api001.backblazeb2.com\n", "https://f001.backblazeb2.com",
        ):
            with self.subTest(url=url):
                auth = authorization()
                auth["apiUrl"] = url
                opener = FakeOpener(auth)
                client = cleanup.B2HTTP(self.deadline, opener)
                self.assert_error("unsafe_api_url", client.authorize, KEY_ID, KEY)
                self.assertIsNone(client.token)
                self.assertEqual(len(opener.requests), 1)
        for url in ("https://api.backblazeb2.com", "https://api001.backblazeb2.com:443/"):
            self.assertTrue(cleanup.api_origin(url))

    def test_redirect_handler_never_forwards_authorization(self):
        handler = cleanup.NoRedirect()
        request = urllib.request.Request(cleanup.AUTH_URL, headers={"Authorization": TOKEN})
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                method = getattr(handler, f"http_error_{status}")
                self.assert_error(
                    "redirect_rejected", method,
                    request, Response(), status, "redirect", {"location": "https://evil.test"},
                )
        opener = FakeOpener(http_error(302))
        self.assert_error("redirect_rejected", cleanup.B2HTTP(self.deadline, opener).authorize,
                          KEY_ID, KEY)
        self.assertEqual(len(opener.requests), 1)
        self.assert_private_logs()

    def test_transient_retries_then_success_and_numeric_retry_after_bound(self):
        opener = FakeOpener(http_error(429, "too_many_requests", "99999"),
                            http_error(503, "service_unavailable"), authorization())
        client = cleanup.B2HTTP(self.deadline, opener)
        client.authorize(KEY_ID, KEY)
        self.assertEqual(len(opener.requests), 3)
        self.assertEqual(self.clock.sleeps, [30, 2])
        self.assert_private_logs()

    def test_transient_retry_limit(self):
        for status in (429, 500, 503):
            with self.subTest(status=status):
                opener = FakeOpener(*(http_error(status) for _ in range(cleanup.MAX_ATTEMPTS)))
                client = cleanup.B2HTTP(self.deadline, opener)
                self.assert_error("retry_limit", client.authorize, KEY_ID, KEY)
                self.assertEqual(len(opener.requests), cleanup.MAX_ATTEMPTS)
        self.assert_private_logs()

    def test_deadline_bounds_timeout_and_prevents_retry(self):
        deadline = cleanup.Deadline(seconds=1, clock=self.clock, sleep=self.clock.sleep)
        opener = FakeOpener(http_error(503))
        client = cleanup.B2HTTP(deadline, opener)
        self.assert_error("deadline_exceeded", client.authorize, KEY_ID, KEY)
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(opener.requests[0][1], 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_deadline_during_response_read(self):
        clock = self.clock

        class SlowResponse(Response):
            def read1(self, size):
                clock.now += 2
                return super().read1(size)

        deadline = cleanup.Deadline(seconds=1, clock=clock, sleep=clock.sleep)
        client = cleanup.B2HTTP(deadline, FakeOpener(SlowResponse(b"{}")))
        self.assert_error("deadline_exceeded", client.authorize, KEY_ID, KEY)

    def test_permission_retention_and_untrusted_error_codes_are_not_retried_or_logged(self):
        for status, code, expected in (
            (401, "bad_auth_token", "bad_auth_token"),
            (403, "access_denied", "access_denied"),
            (400, "file_lock_conflict", "file_lock_conflict"),
            (403, PRIVATE_NAME, "unrecognized_server_error"),
            (403, TOKEN, "unrecognized_server_error"),
        ):
            with self.subTest(status=status, code=code):
                opener = FakeOpener(http_error(status, code))
                self.assert_error(expected, cleanup.B2HTTP(self.deadline, opener).authorize,
                                  KEY_ID, KEY)
                self.assertEqual(len(opener.requests), 1)
        self.assert_private_logs()

    def test_transport_failure_no_retry_and_invalid_or_oversize_response_stop(self):
        for action, expected in (
            (urllib.error.URLError("raw-private-message"), "transport_error"),
            (Response(b"raw-private-message"), "invalid_json"),
            (Response(b"[]"), "invalid_response"),
            (Response(b"x" * 40), "response_too_large"),
        ):
            with self.subTest(expected=expected):
                opener = FakeOpener(action)
                with mock.patch.object(cleanup, "MAX_RESPONSE_BYTES", 32):
                    self.assert_error(expected, cleanup.B2HTTP(self.deadline, opener).authorize,
                                      KEY_ID, KEY)
                self.assertEqual(len(opener.requests), 1)
        self.assert_private_logs()

    def test_real_opener_disables_proxies_and_installs_redirect_guard(self):
        client = cleanup.B2HTTP(self.deadline)
        self.assertFalse(any(isinstance(h, urllib.request.ProxyHandler)
                             for h in client.opener.handlers))
        self.assertTrue(any(isinstance(h, cleanup.NoRedirect) for h in client.opener.handlers))


if __name__ == "__main__":
    unittest.main()
