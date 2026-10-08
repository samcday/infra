#!/usr/bin/env python3
"""One-bucket B2 Native API v2 inventory/purge; no third-party dependencies."""

import base64
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request


BUCKET_NAME = "samcday-fastboopmos"
AUTH_URL = "https://api.backblazeb2.com/b2api/v2/b2_authorize_account"
PAGE_SIZE = 1000
UNFINISHED_PAGE_SIZE = 100
HTTP_TIMEOUT = 30
DEADLINE_SECONDS = 840  # Leave time to report before the Job's 900s hard limit.
MAX_ATTEMPTS = 4
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
OPERATIONS = {
    "b2_list_buckets", "b2_list_file_versions", "b2_list_unfinished_large_files",
    "b2_delete_file_version", "b2_cancel_large_file",
}
# Never echo arbitrary server text, including an arbitrary "code" value.
ERROR_CODES = {
    "access_denied", "unauthorized", "bad_auth_token", "expired_auth_token",
    "bad_request", "file_not_present", "file_lock_conflict", "retention_violation",
    "too_many_requests", "service_unavailable", "internal_error",
    "cap_exceeded", "transaction_cap_exceeded", "storage_cap_exceeded",
}


class CleanupError(Exception):
    def __init__(self, operation, code):
        self.operation = operation
        self.code = code
        super().__init__(code)


def require(condition, operation, code):
    if not condition:
        raise CleanupError(operation, code)


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{1,64}", value)


def nonempty(value):
    return isinstance(value, str) and bool(value)


def api_origin(value):
    # Native API hosts only, not download/upload hosts. The exact grammar also
    # rejects userinfo, non-443 ports, paths, queries, fragments and whitespace.
    require(
        isinstance(value, str) and re.fullmatch(
            r"https://api(?:[0-9]+)?\.backblazeb2\.com(?::443)?/?", value
        ),
        "authorize", "unsafe_api_url",
    )
    return value.rstrip("/")


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}, sort_keys=True), flush=True)


class Deadline:
    def __init__(self, seconds=DEADLINE_SECONDS, clock=time.monotonic, sleep=time.sleep):
        self.clock = clock
        self.sleep = sleep
        self.end = clock() + seconds

    def remaining(self):
        remaining = self.end - self.clock()
        require(remaining > 0, "deadline", "deadline_exceeded")
        return remaining

    def pause(self, seconds):
        require(seconds < self.remaining(), "deadline", "deadline_exceeded")
        self.sleep(seconds)
        self.remaining()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CleanupError("http", "redirect_rejected")


class B2HTTP:
    def __init__(self, deadline, opener=None):
        self.deadline = deadline
        self.opener = opener if opener is not None else urllib.request.build_opener(
            urllib.request.ProxyHandler({}),  # Do not forward keys to env proxies.
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            NoRedirect(),
        )
        self.origin = None
        self.token = None

    def read_body(self, response):
        chunks = []
        size = 0
        while True:
            self.deadline.remaining()
            chunk = response.read1(65536)
            self.deadline.remaining()
            if not chunk:
                return b"".join(chunks)
            size += len(chunk)
            require(size <= MAX_RESPONSE_BYTES, "http", "response_too_large")
            chunks.append(chunk)

    def request(self, operation, url, authorization, payload=None):
        for attempt in range(1, MAX_ATTEMPTS + 1):
            timeout = min(HTTP_TIMEOUT, self.deadline.remaining())
            request = urllib.request.Request(
                url,
                data=None if payload is None else json.dumps(payload).encode("utf-8"),
                headers={"Authorization": authorization, "Content-Type": "application/json"},
                method="GET" if payload is None else "POST",
            )
            try:
                with self.opener.open(request, timeout=timeout) as response:
                    body = self.read_body(response)
                result = json.loads(body)
                require(isinstance(result, dict), operation, "invalid_response")
                return result
            except urllib.error.HTTPError as error:
                # The exception string, headers and raw body may contain secrets
                # or filenames. Do not print them, even when parsing fails.
                with error:
                    body = self.read_body(error)
                code = "unrecognized_server_error"
                try:
                    result = json.loads(body)
                    candidate = result.get("code") if isinstance(result, dict) else None
                    if isinstance(candidate, str) and candidate in ERROR_CODES:
                        code = candidate
                except (ValueError, UnicodeError):
                    pass
                if 300 <= error.code < 400:
                    raise CleanupError(operation, "redirect_rejected") from None
                if error.code == 429 or 500 <= error.code < 600:
                    if attempt == MAX_ATTEMPTS:
                        raise CleanupError(operation, "retry_limit") from None
                    # Honor numeric Retry-After, but never exceed 30s or deadline.
                    delay = 2 ** (attempt - 1)
                    retry_after = error.headers.get("Retry-After", "")
                    if re.fullmatch(r"[0-9]{1,9}", retry_after):
                        delay = max(delay, min(30, int(retry_after)))
                    emit("retry", operation=operation, code=code, attempt=attempt)
                    self.deadline.pause(delay)
                    continue
                raise CleanupError(operation, code) from None
            except (urllib.error.URLError, OSError):
                # A transport failure can leave a mutation's outcome unknown.
                # Do not retry it or claim it succeeded.
                raise CleanupError(operation, "transport_error") from None
            except (ValueError, UnicodeError):
                raise CleanupError(operation, "invalid_json") from None

    def authorize(self, key_id, key):
        basic = base64.b64encode(f"{key_id}:{key}".encode("utf-8")).decode("ascii")
        result = self.request("b2_authorize_account", AUTH_URL, "Basic " + basic)
        self.origin = api_origin(result.get("apiUrl"))
        require(nonempty(result.get("authorizationToken")), "authorize", "missing_token")
        self.token = result["authorizationToken"]
        return result

    def call(self, operation, payload):
        require(operation in OPERATIONS, "http", "unsupported_operation")
        require(self.origin is not None and self.token is not None, "http", "not_authorized")
        return self.request(
            operation, self.origin + "/b2api/v2/" + operation, self.token, payload
        )


def empty_summary():
    return {
        "versions": 0, "upload_versions": 0, "hide_markers": 0,
        "start_versions": 0, "version_bytes": 0, "unfinished_uploads": 0,
    }


class Cleanup:
    def __init__(self, client, deadline, mode, bucket_name,
                 expected_account_id="", expected_bucket_id=""):
        self.client = client
        self.deadline = deadline
        self.mode = mode
        self.account_id = None
        self.bucket_id = None
        self.initial = empty_summary()
        self.verification = empty_summary()
        self.progress = {
            "delete_requests": 0, "deleted_versions": 0,
            "cancel_requests": 0, "cancelled_uploads": 0,
        }
        require(bucket_name == BUCKET_NAME, "configuration", "wrong_bucket_name")
        require(mode in ("inventory", "purge"), "configuration", "invalid_mode")
        require(
            (not expected_account_id and not expected_bucket_id)
            or (identifier(expected_account_id) and identifier(expected_bucket_id)),
            "configuration", "invalid_expected_ids",
        )
        if mode == "purge":
            require(
                identifier(expected_account_id) and identifier(expected_bucket_id),
                "configuration", "expected_ids_required",
            )
        self.expected_account_id = expected_account_id
        self.expected_bucket_id = expected_bucket_id

    def report(self, event, **extra):
        emit(
            event, mode=self.mode, account_id=self.account_id, bucket_id=self.bucket_id,
            inventory=self.initial, verification=self.verification,
            progress=self.progress, **extra,
        )

    def check_scope(self, auth):
        allowed = auth.get("allowed")
        require(isinstance(allowed, dict), "scope", "missing_scope")
        require("buckets" not in allowed, "scope", "ambiguous_bucket_scope")
        require(identifier(allowed.get("bucketId")), "scope", "unrestricted_credentials")
        require(allowed.get("bucketName") == BUCKET_NAME, "scope", "wrong_bucket_name")
        require("namePrefix" in allowed, "scope", "missing_prefix_scope")
        require(allowed["namePrefix"] in (None, ""), "scope", "prefix_restricted")
        capabilities = allowed.get("capabilities")
        required = {"listBuckets", "listFiles"}
        if self.mode == "purge":
            required |= {"deleteFiles", "writeFiles"}
        require(
            isinstance(capabilities, list)
            and all(isinstance(value, str) for value in capabilities)
            and required.issubset(capabilities),
            "scope", "missing_capabilities",
        )
        account_id, bucket_id = auth.get("accountId"), allowed["bucketId"]
        require(identifier(account_id), "scope", "invalid_account_id")
        if self.expected_account_id:
            require(account_id == self.expected_account_id, "scope", "wrong_account_id")
            require(bucket_id == self.expected_bucket_id, "scope", "wrong_bucket_id")
        result = self.client.call("b2_list_buckets", {
            "accountId": account_id, "bucketName": BUCKET_NAME,
        })
        buckets = result.get("buckets")
        require(
            isinstance(buckets, list) and len(buckets) == 1
            and isinstance(buckets[0], dict),
            "scope", "bucket_list_disagreement",
        )
        bucket = buckets[0]
        require(
            bucket.get("bucketName") == BUCKET_NAME
            and bucket.get("bucketId") == bucket_id
            and bucket.get("accountId") == account_id,
            "scope", "bucket_list_disagreement",
        )
        self.account_id, self.bucket_id = account_id, bucket_id
        self.report("scope_validated")

    def validate_file(self, item, unfinished):
        require(isinstance(item, dict), "listing", "invalid_file")
        require(
            item.get("accountId") == self.account_id
            and item.get("bucketId") == self.bucket_id,
            "listing", "file_scope_disagreement",
        )
        require(
            nonempty(item.get("fileId")) and nonempty(item.get("fileName")),
            "listing", "invalid_file_identity",
        )
        if unfinished:
            require(item.get("action", "start") == "start", "listing", "invalid_action")
        else:
            require(item.get("action") in ("upload", "hide", "start"),
                    "listing", "invalid_action")
            size = item.get("contentLength")
            require(type(size) is int and size >= 0, "listing", "invalid_content_length")

    def pages(self, unfinished=False):
        operation = "b2_list_unfinished_large_files" if unfinished else "b2_list_file_versions"
        page_size = UNFINISHED_PAGE_SIZE if unfinished else PAGE_SIZE
        cursor_fields = (("nextFileId", "startFileId"),) if unfinished else (
            ("nextFileName", "startFileName"), ("nextFileId", "startFileId"),
        )
        cursor = None
        seen = set()
        while True:
            payload = {"bucketId": self.bucket_id, "maxFileCount": page_size}
            if cursor is not None:
                payload.update({field[1]: value for field, value in zip(cursor_fields, cursor)})
            result = self.client.call(operation, payload)
            files = result.get("files")
            require(isinstance(files, list) and len(files) <= page_size,
                    operation, "invalid_page")
            # Validate the whole page before issuing any mutation from it.
            for item in files:
                self.deadline.remaining()
                self.validate_file(item, unfinished)
            require(all(field[0] in result for field in cursor_fields),
                    operation, "missing_cursor")
            next_cursor = tuple(result[field[0]] for field in cursor_fields)
            done = all(value is None for value in next_cursor)
            if not done:
                require(all(nonempty(value) for value in next_cursor),
                        operation, "incomplete_cursor")
                require(files and next_cursor not in seen, operation, "pagination_stalled")
                seen.add(next_cursor)
            yield files
            if done:
                return
            cursor = next_cursor

    def inventory(self, summary):
        actions = {"upload": "upload_versions", "hide": "hide_markers", "start": "start_versions"}
        for page in self.pages():
            for item in page:
                summary["versions"] += 1
                summary[actions[item["action"]]] += 1
                summary["version_bytes"] += item["contentLength"]
        for page in self.pages(unfinished=True):
            summary["unfinished_uploads"] += len(page)

    def mutate(self, item, cancel=False):
        self.deadline.remaining()
        operation = "b2_cancel_large_file" if cancel else "b2_delete_file_version"
        payload = {"fileId": item["fileId"]}
        if not cancel:
            payload["fileName"] = item["fileName"]
        self.progress["cancel_requests" if cancel else "delete_requests"] += 1
        result = self.client.call(operation, payload)
        require(result.get("fileId") == item["fileId"], operation, "mutation_unconfirmed")
        self.progress["cancelled_uploads" if cancel else "deleted_versions"] += 1

    def run(self, key_id, key):
        self.check_scope(self.client.authorize(key_id, key))
        self.inventory(self.initial)
        self.report("inventory_complete")
        if self.mode == "inventory":
            return
        # Cancellation removes all parts as part of the Native API operation.
        # Start records found here are gone before the separate unfinished scan.
        for page in self.pages():
            for item in page:
                self.mutate(item, cancel=item["action"] == "start")
            self.report("purge_progress")
        for page in self.pages(unfinished=True):
            for item in page:
                self.mutate(item, cancel=True)
            self.report("purge_progress")
        self.inventory(self.verification)
        require(
            self.verification["versions"] == 0
            and self.verification["unfinished_uploads"] == 0,
            "verification", "bucket_not_empty",
        )
        self.report("purge_verified_empty")


def main(environ=None):
    environ = os.environ if environ is None else environ
    cleanup = None
    try:
        deadline = Deadline()
        cleanup = Cleanup(
            B2HTTP(deadline), deadline,
            environ.get("B2_CLEANUP_MODE", "inventory"),
            environ.get("B2_BUCKET_NAME"),
            environ.get("B2_EXPECTED_ACCOUNT_ID", ""),
            environ.get("B2_EXPECTED_BUCKET_ID", ""),
        )
        key_id, key = environ.get("B2_APPLICATION_KEY_ID"), environ.get("B2_APPLICATION_KEY")
        require(nonempty(key_id) and nonempty(key), "configuration", "missing_credentials")
        cleanup.run(key_id, key)
        return 0
    except Exception as error:
        # No traceback: request objects / exception messages may contain secrets.
        fields = {
            "operation": error.operation if isinstance(error, CleanupError) else "internal",
            "code": error.code if isinstance(error, CleanupError) else "internal_error",
            "complete": False,
        }
        if cleanup is None:
            emit("stopped", **fields)
        else:
            cleanup.report("stopped", **fields)
        return 1


if __name__ == "__main__":
    sys.exit(main())
