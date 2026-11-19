"""Server state, read through the ServerPort and cached for a few seconds.

States, as the page names them:
  online    RCON answered `list`.
  starting  The container is running but RCON does not answer yet: a boot,
            or a restart in progress.
  down      The container is not running: stopped, or between a crash and
            Docker restarting it.

Any boot is tracked, not only one the applier drives (a crash, Docker's
restart, a host reboot): while `starting`, the snapshot carries `boot` with
when the server process started and when it is usually ready, learned from
the boots this app has watched finish (BootTimes). While `down` it carries
`downSince`, the first time this app saw it down. A boot past its usual
length says `overdue` instead of an estimate that keeps moving.

The snapshot is refreshed on demand when older than `max_age`, so a stopped
container shows as down on the first request after `max_age` (10 s by
default), well inside the one minute the page promises. There is no
background thread to die silently.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
    # No "Overall" line: report the slowest dimension, never a flattering one.
    overall = rows.get("Overall") or min(rows.values())
    return {"tps": round(min(overall[0], 20.0), 2), "msPerTick": round(overall[1], 2)}


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat(timespec="seconds") if moment else None


#: Until a boot has been watched to the end: about what the pack takes on the live server.
DEFAULT_BOOT_SECONDS = 240.0
#: A watched boot shorter or longer than this is not a boot (or not one worth learning).
_BOOT_BOUNDS = (20.0, 3600.0)
#: A boot is learned only when it was last seen starting this recently before it was seen
#: online. Snapshots are taken only when someone asks, so a long gap would measure when a
#: viewer came back, not how long the boot took.
_LEARN_GAP = timedelta(seconds=90)


class BootTimes:
    """How long a boot usually takes, kept in a small JSON file so a restart of this app keeps it."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self._typical = DEFAULT_BOOT_SECONDS
        self._dirty = False
        if path is not None:
            try:
                value = json.loads(path.read_text()).get("typicalSeconds")
                if isinstance(value, (int, float)) and _BOOT_BOUNDS[0] <= value <= _BOOT_BOUNDS[1]:
                    self._typical = float(value)
            except (OSError, ValueError, AttributeError):
                pass

    @property
    def typical(self) -> float:
        return self._typical

    def learn(self, seconds: float) -> None:
        """Move the estimate halfway toward a boot that was watched to the end. Saved by flush()."""
        if _BOOT_BOUNDS[0] <= seconds <= _BOOT_BOUNDS[1]:
            self._typical = round(0.5 * self._typical + 0.5 * seconds, 1)
            self._dirty = True

    def flush(self) -> None:
        """Write a learned estimate. An estimate that is not saved is relearned; never worth failing a page for."""
        if not self._dirty or self.path is None:
            return
        self._dirty = False
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".boot-")
            with os.fdopen(fd, "w") as f:
                json.dump({"typicalSeconds": self._typical}, f)
            os.replace(tmp, self.path)
            tmp = None
        except OSError:
            pass
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


class StatusMonitor:
    def __init__(self, server: ServerPort, address: str, max_age: float = 10.0,
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 boots: BootTimes | None = None):
        self.server, self.address, self.max_age = server, address, max_age
        self._clock, self._now = clock, now
        self.boots = boots or BootTimes()
        self._booting: datetime | None = None      # the start time of a boot seen in progress
        self._seen_starting: datetime | None = None  # when it was last seen starting
        self._down_since: datetime | None = None
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] | None = None
        self._taken = 0.0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._snapshot is None or self._clock() - self._taken >= self.max_age:
                self._snapshot = self._read()
                self._taken = self._clock()
            snapshot = self._snapshot
        self.boots.flush()  # outside the lock: a slow disk must not hold up every status request
        return snapshot

    def _read(self) -> dict[str, Any]:
        now = self._now()
        result: dict[str, Any] = {
            "state": "down",
            "players": None,
            "tps": None,
            "lastRestart": None,
            "address": self.address,
            "checkedAt": _iso(now),
        }
        if not self.server.container_running():
            self._down_since = self._down_since or now
            self._booting = self._seen_starting = None
            result["downSince"] = _iso(self._down_since)
            return result
        self._down_since = None
        result["state"] = "starting"
        started = self.server.last_start()
        result["lastRestart"] = _iso(started)
        try:
            players = parse_list(self.server.rcon("list"))
        except ServerUnavailable:
            players = None
        if players is None:
            return self._starting(result, started, now)
        if (self._booting is not None and started == self._booting and self._seen_starting is not None
                and now - self._seen_starting <= _LEARN_GAP):
            self.boots.learn((now - started).total_seconds())
        self._booting = self._seen_starting = None
        result["state"] = "online"
        result["players"] = players
        try:
            result["tps"] = parse_tps(self.server.rcon("neoforge tps"))
        except ServerUnavailable:
            pass
        return result

    def _starting(self, result: dict[str, Any], started: datetime | None, now: datetime) -> dict[str, Any]:
        """A boot in progress: when it began, and when it is usually ready or that it is overdue.

        A start older than any boot is the previous run's (the new JVM has not rolled
        latest.log yet), so it is no start time at all: no since, no estimate.
        """
        typical = self.boots.typical
        if started is None or (now - started).total_seconds() > _BOOT_BOUNDS[1]:
            self._booting = self._seen_starting = None
            result["boot"] = {"since": None, "typicalSeconds": round(typical), "readyBy": None, "overdue": False}
            return result
        self._booting, self._seen_starting = started, now
        ready = started + timedelta(seconds=typical)
        overdue = now > ready
        result["boot"] = {"since": _iso(started), "typicalSeconds": round(typical),
                          "readyBy": None if overdue else _iso(ready), "overdue": overdue}
        return result
