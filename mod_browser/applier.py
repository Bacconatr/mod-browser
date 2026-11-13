"""The app's half of applying queued requests: it records what the owner asked for, and nothing more.

The work (stage the candidate pack, stop the server, back the world up,
test-boot a copy, promote, start, roll back) is done by the maintenance runner
(`maintenance.py`), which PVE runs on guest 120 over `pct exec`. This
Internet-facing app has no Docker socket and never gets one, so it cannot stop
or start anything. It only:

  - records "apply requested" (when the server is empty, or now with a short
    in-game warning) from the owner, behind the /admin password;
  - hands the runner a batch on the loopback admin API (`claim`), takes it
    back when the runner defers it (`release`), and records how it ended
    (`finish`), so request states change in one place;
  - shows the phase the runner reports on the status page.

Owner-triggered only: there is no nightly slot. Everything is behind
`MOD_BROWSER_APPLIER=1`, off by default; while off, every route here is a 409.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from .queue import InvalidTransition, Queue, Request

REQUEST_KEY = "apply.requested"
PHASE_KEY = "apply.phase"
HALT_KEY = "applier.halted"
MODES = ("when-empty", "now")
OUTCOMES = ("applied", "test-failed", "rolled-back")
#: What the status page may say. The runner's detail is shown as text, never as HTML.
PHASES = ("idle", "requested", "waiting", "staging", "warning", "stopping", "backing-up", "test-booting",
          "promoting", "starting", "online", "failed", "rolling-back", "needs-recovery", "halted")


class DeskRefused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ApplyDesk:
    def __init__(self, queue: Queue, *, enabled: bool = False, requeue: Callable[[int], Request] | None = None,
                 clock: Callable[[], str] = _now):
        self.queue, self.enabled, self._clock = queue, enabled, clock
        # Re-resolves a queued request that predates file hashes (queued by an older build) before it is staged.
        self._requeue = requeue
        self._lock = threading.Lock()

    # ---- what the page and the owner see ----
    def _kv_json(self, key: str) -> dict[str, Any] | None:
        raw = self.queue.kv_get(key)
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def status(self) -> dict[str, Any]:
        requested = self._kv_json(REQUEST_KEY)
        phase = self._kv_json(PHASE_KEY) or {"phase": "idle", "detail": "", "since": None}
        halted = self.queue.kv_get(HALT_KEY) or None
        if phase.get("phase") == "idle" and requested:
            when = "when the server is empty" if requested["mode"] == "when-empty" else "now, after a short warning in game"
            phase = {"phase": "requested", "detail": f"The owner asked to apply what is queued {when}.",
                     "since": requested.get("at")}
        if halted:
            phase = {**phase, "phase": "halted", "detail": halted}
        return {**phase, "requested": requested["mode"] if requested else None, "halted": halted, "enabled": self.enabled}

    def _set_phase(self, phase: str, detail: str, batch: int | None = None) -> None:
        self.queue.kv_set(PHASE_KEY, json.dumps({"phase": phase, "detail": detail[:500], "since": self._clock(), "batch": batch}))

    def _on(self) -> None:
        if not self.enabled:
            raise DeskRefused(409, "The applier is switched off.")

    # ---- the owner (behind the password, or loopback) ----
    def request(self, mode: str) -> dict[str, Any]:
        self._on()
        if mode not in MODES:
            raise DeskRefused(400, f"mode is one of {', '.join(MODES)}")
        if self.queue.kv_get(HALT_KEY):
            raise DeskRefused(409, "The applier is halted; the owner must run the recovery on PVE first.")
        if not self.queue.queued():
            raise DeskRefused(409, "Nothing is queued.")
        with self._lock:
            current = self._kv_json(REQUEST_KEY)
            # "now" upgrades a waiting "when-empty"; asking again never downgrades it.
            if current is None or mode == "now":
                self.queue.kv_set(REQUEST_KEY, json.dumps({"mode": mode, "at": self._clock()}))
        return self.status()

    def cancel(self) -> dict[str, Any]:
        """Withdraw the request. A batch the runner already holds is not stopped by this (it may have stopped the server)."""
        self._on()
        self.queue.kv_set(REQUEST_KEY, "")
        if (self._kv_json(PHASE_KEY) or {}).get("phase") in ("requested", "waiting"):
            self._set_phase("idle", "")
        return self.status()

    # ---- the runner (loopback only) ----
    def pending(self) -> dict[str, Any]:
        self._on()
        return {"requested": (self._kv_json(REQUEST_KEY) or {}).get("mode"), "halted": self.queue.kv_get(HALT_KEY) or None,
                "queued": len(self.queue.queued()), "inFlight": [r.admin() for r in self.queue.in_flight()]}

    def claim(self) -> dict[str, Any]:
        """Hand the runner one batch: everything queued, or an upstream update on its own. Moves it to `testing`."""
        self._on()
        with self._lock:
            if self.queue.kv_get(HALT_KEY):
                raise DeskRefused(409, "halted")
            requested = self._kv_json(REQUEST_KEY)
            if not requested:
                raise DeskRefused(409, "Nobody asked for an apply.")
            if self.queue.in_flight():
                raise DeskRefused(409, "A batch is already in flight.")
            queued = [self._fresh(r) for r in self.queue.queued()]
            queued = [r for r in queued if r.state == "queued"]
            if not queued:
                self.queue.kv_set(REQUEST_KEY, "")
                raise DeskRefused(409, "Nothing is queued.")
            # An upstream update rewrites the base pack: a friend's request computed against the old pack
            # must not ride along with it. The others wait for the next batch.
            upstream = [r for r in queued if r.kind == "upstream"]
            batch_of = upstream[:1] or queued
            batch = self.queue.next_batch_id()
            ids = [r.id for r in batch_of]
            moved = self.queue.advance(ids, ("queued",), "testing", batch=batch)
        self._set_phase("staging", f"Preparing {len(ids)} request(s): writing the pack and downloading the mods.", batch)
        return {"batch": batch, "mode": requested["mode"], "requests": [r.admin() for r in moved]}

    def _fresh(self, request: Request) -> Request:
        """A request queued before snapshots carried file hashes cannot be written into the pack.
        Re-resolve it (as approval does); if it no longer fits it is held `approved` with the reason."""
        if request.kind != "mod" or all(a.get("fileHash") for a in request.added if a.get("op", "add") != "remove"):
            return request
        if self._requeue is None:
            return request
        try:
            self.queue.hold(request.id, "Being checked again against the pack before it is applied.")
            return self._requeue(request.id)
        except Exception:  # noqa: BLE001 - leave it out of this batch rather than fail the batch
            return self.queue.get(request.id)

    def _batch(self, batch: int, states: tuple[str, ...]) -> list[int]:
        ids = [r.id for r in self.queue.in_flight() if r.batch == batch and r.state in states]
        if not ids:
            raise DeskRefused(409, f"Batch {batch} has nothing in {' or '.join(states)}.")
        return ids

    def release(self, batch: int, reason: str) -> dict[str, Any]:
        """The runner deferred the batch before touching the server (players joined, a download failed to
        start...): the requests go back to `queued` and the owner's request stands."""
        self._on()
        self.queue.advance(self._batch(batch, ("testing",)), ("testing",), "queued")
        self._set_phase("waiting", reason, batch)
        return self.status()

    def phase(self, batch: int | None, phase: str, detail: str, state: str | None = None) -> dict[str, Any]:
        self._on()
        if phase not in PHASES:
            raise DeskRefused(400, f"unknown phase {phase!r}")
        if state is not None:
            if state != "applying" or batch is None:
                raise DeskRefused(400, "the only state a phase moves to is applying")
            self.queue.advance(self._batch(batch, ("testing",)), ("testing",), "applying")
        self._set_phase(phase, detail, batch)
        return self.status()

    def finish(self, batch: int, outcome: str, reason: str) -> dict[str, Any]:
        """How the batch ended, as the runner verified it. Clears the owner's request."""
        self._on()
        if outcome not in OUTCOMES:
            raise DeskRefused(400, f"outcome is one of {', '.join(OUTCOMES)}")
        allowed = ("applying",) if outcome != "test-failed" else ("testing", "applying")
        try:
            self.queue.advance(self._batch(batch, allowed), allowed, outcome, reason=(reason or None) and reason[:500])
        except InvalidTransition as exc:
            raise DeskRefused(409, str(exc)) from exc
        # The owner's ask covers what was queued: an upstream update goes alone, so friends' requests left
        # behind it still stand. A failure clears the ask, so a broken batch is never retried by itself.
        if outcome != "applied" or not self.queue.queued():
            self.queue.kv_set(REQUEST_KEY, "")
        detail = {"applied": f"Batch {batch} applied. The server is online.",
                  "test-failed": f"Batch {batch} did not apply; the server is back on the old pack: {reason}",
                  "rolled-back": f"Batch {batch} was rolled back; the server is back on the old pack and world: {reason}"}[outcome]
        self._set_phase("online" if outcome == "applied" else "failed", detail, batch)
        return self.status()

    def halt(self, reason: str) -> dict[str, Any]:
        """The runner stopped and wants the owner. Nothing more is claimed until it reports `resume`."""
        self._on()
        reason = (reason or "halted")[:500]
        self.queue.kv_set(HALT_KEY, reason)
        self._set_phase("halted", reason)
        return self.status()

    def resume(self) -> dict[str, Any]:
        """The runner's recovery verified the server (loopback only: never cleared from the Internet)."""
        self._on()
        self.queue.kv_set(HALT_KEY, "")
        self._set_phase("idle", "")
        return self.status()
