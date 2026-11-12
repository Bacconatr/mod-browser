"""Container healthcheck: exit 0 when the public listener answers /api/health."""

import os
import sys
import urllib.request

port = os.environ.get("MOD_BROWSER_LISTEN_PORT", "8080")
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as response:
        sys.exit(0 if response.status == 200 else 1)
except OSError:
    sys.exit(1)
