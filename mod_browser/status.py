"""Server state, read through the ServerPort and cached for a few seconds.

States, as the page names them:
  online    RCON answered `list`.
  starting  The container is running but RCON does not answer yet: a boot,
            or a restart in progress.
  down      The container is not running: stopped, or between a crash and
            Docker restarting it.

The snapshot is refreshed on demand when older than `max_age`, so a stopped
container shows as down on the first request after `max_age` (10 s by
default), well inside the one minute the page promises. There is no
background thread to die silently.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .ports import ServerPort, ServerUnavailable

# "There are 2 of a max of 10 players online: Alex, Steve"
_LIST = re.compile(r"There are (\d+) of a max(?: of)? (\d+) players online:?\s*(.*)", re.S)
# NeoForge 1.21.1 `neoforge tps`: one line per dimension, then
# "Overall: 20.000 TPS (0.759 ms/tick)"
_TPS = re.compile(r"^(.+?): ([\d.]+) TPS \(([\d.]+) ms/tick\)\s*$", re.M)


def parse_list(reply: str) -> dict[str, Any] | None:
    match = _LIST.search(reply)
    if not match:
        return None
    names = [n.strip() for n in match.group(3).split(",") if n.strip()]
    return {"online": int(match.group(1)), "max": int(match.group(2)), "names": sorted(names, key=str.lower)}


def parse_tps(reply: str) -> dict[str, Any] | None:
    rows = {m.group(1).strip(): (float(m.group(2)), float(m.group(3))) for m in _TPS.finditer(reply)}
    if not rows:
        return None
    overall = rows.get("Overall") or rows.get("Overworld") or next(iter(rows.values()))
    return {"tps": round(min(overall[0], 20.0), 2), "msPerTick": round(overall[1], 2)}


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat(timespec="seconds") if moment else None


class StatusMonitor:
    def __init__(self, server: ServerPort, address: str, max_age: float = 10.0,
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.server, self.address, self.max_age = server, address, max_age
        self._clock, self._now = clock, now
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] | None = None
        self._taken = 0.0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._snapshot is None or self._clock() - self._taken >= self.max_age:
                self._snapshot = self._read()
                self._taken = self._clock()
            return self._snapshot

    def _read(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "state": "down",
            "players": None,
            "tps": None,
            "lastRestart": None,
            "address": self.address,
            "checkedAt": _iso(self._now()),
        }
        if not self.server.container_running():
            return result
        result["state"] = "starting"
        result["lastRestart"] = _iso(self.server.last_start())
        try:
            players = parse_list(self.server.rcon("list"))
        except ServerUnavailable:
            return result
        if players is None:
            return result
        result["state"] = "online"
        result["players"] = players
        try:
            result["tps"] = parse_tps(self.server.rcon("neoforge tps"))
        except ServerUnavailable:
            pass
        return result
