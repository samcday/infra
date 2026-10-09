"""One HTTP request, executed inside the isolated mock container via stdin."""

import json
import sys
import urllib.error
import urllib.request

options = json.load(sys.stdin)
request = urllib.request.Request(
    options["url"],
    data=json.dumps(options["body"]).encode() if options["body"] is not None else None,
    headers={"Content-Type": "application/json", **options["headers"]},
)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    try:
        response = opener.open(request, timeout=8)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        data = response.read()
        content_type = response.headers.get("Content-Type", "")
        value = (
            data.decode() if "text/event-stream" in content_type
            else json.loads(data) if "application/json" in content_type and data
            else data.decode()
        )
        print(json.dumps([response.status, value]))
except (OSError, urllib.error.URLError):
    # Only a connection failure is retryable for startup checks.
    print(json.dumps([0, "connection failed"]))
