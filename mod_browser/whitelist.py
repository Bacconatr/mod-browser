"""The server's whitelist, read from its `whitelist.json` (mounted read-only).

Only ever asked "is this name on it?", never "who is on it?": the site has no
route that returns the list. Names compare case-insensitively, the way the
game's whitelist does, and the answer is the spelling the whitelist uses.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path


class FileWhitelist:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._sig: tuple[int, int] | None = None
        self._names: dict[str, str] = {}

    def canonical(self, name: str) -> str | None:
        """The whitelist's own spelling of `name`, or None when it is not on the list.

        A file that cannot be read answers None (fail closed) rather than letting anyone in.
        """
        try:
            stat = self.path.stat()
            sig = (stat.st_size, stat.st_mtime_ns)
            with self._lock:
                if sig != self._sig:
                    entries = json.loads(self.path.read_text())
                    self._names = {e["name"].casefold(): e["name"] for e in entries
                                   if isinstance(e, dict) and isinstance(e.get("name"), str)}
                    self._sig = sig
                return self._names.get(name.casefold())
        except (OSError, ValueError, TypeError):
            return None
