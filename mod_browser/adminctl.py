"""One call to the loopback admin API, from inside the container: how the maintenance runner reaches the app.

    docker exec -i aeronautics-mod-browser python -m mod_browser.adminctl POST /admin-api/v1/maintenance/claim < body.json

Prints one line of JSON, `{"status": <http status>, "body": <the answer>}`, and
exits 0 whenever the API answered (the caller reads the status). Only
`/admin-api/v1/` paths; the body is read from stdin (at most 8 KiB).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

MAX_BODY = 8192


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("GET", "POST") or not argv[1].startswith("/admin-api/v1/"):
        print("usage: adminctl GET|POST /admin-api/v1/<path>", file=sys.stderr)
        return 2
    method, path = argv
    data = sys.stdin.buffer.read(MAX_BODY + 1) if method == "POST" else None
    if data is not None and len(data) > MAX_BODY:
        print("body too large", file=sys.stderr)
        return 2
    port = os.environ.get("MOD_BROWSER_ADMIN_LISTEN_PORT", "8081")
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data or (b"{}" if method == "POST" else None),
                                     method=method, headers={"Content-Type": "application/json"} if method == "POST" else {})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    except OSError as exc:
        print(f"the admin API did not answer: {exc}", file=sys.stderr)
        return 1
    try:
        body = json.loads(raw)
    except ValueError:
        body = raw.decode(errors="replace")[:500]
    print(json.dumps({"status": status, "body": body}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
