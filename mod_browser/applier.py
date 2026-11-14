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
from datetime import datetime, timedelta, timezone
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


#: The phases a restart walks through, in order, and how long each is assumed to take until a finished
#: batch has been measured (then `learn` moves them toward what really happened). The warning is the
#: runner's in-game countdown (60 s); the rest are first guesses for guest 120, deliberately on the slow side.
RESTART_PHASES = ("warning", "stopping", "backing-up", "test-booting", "promoting", "starting")
DEFAULT_SECONDS = {"warning": 60, "stopping": 60, "backing-up": 120, "test-booting": 300, "promoting": 30,
                   "starting": 300}
#: Before a "restart now" is picked up: the runner's 2-minute timer plus staging the downloads.
PICKUP_SECONDS = 180
DURATIONS_KEY = "apply.durations"
TIMINGS_KEY = "apply.timings"


def _parse(stamp: Any) -> datetime | None:
    if not isinstance(stamp, str):
        return None
    try:
        dt = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def back_online_by(phase: str, since: Any, now: Any, durations: dict[str, float]) -> str | None:
    """When the server should be up again, if `phase` is part of a restart; else None.

    The current phase counts down from when it began, and a phase that overruns its estimate still
    leaves at least a minute, so the page never shows a time already gone."""
    if phase not in RESTART_PHASES:
        return None
    start, at = _parse(since), _parse(now)
    if start is None or at is None:
        return None
    current = max(durations.get(phase, DEFAULT_SECONDS[phase]) - (at - start).total_seconds(), 60.0)
    later = sum(durations.get(p, DEFAULT_SECONDS[p]) for p in RESTART_PHASES[RESTART_PHASES.index(phase) + 1:])
    return (at + timedelta(seconds=current + later)).isoformat(timespec="seconds")


def learn(durations: dict[str, float], starts: dict[str, Any], finished_at: Any) -> dict[str, float]:
    """Move each measured phase's estimate halfway toward what it took this time."""
    stamps = sorted(((p, _parse(t)) for p, t in starts.items() if p in RESTART_PHASES and _parse(t)),
                    key=lambda pt: pt[1])
    end = _parse(finished_at)
    out = {p: float(durations.get(p, DEFAULT_SECONDS[p])) for p in RESTART_PHASES}
    if end is None:
        return out
    for i, (phase, began) in enumerate(stamps):
        until = stamps[i + 1][1] if i + 1 < len(stamps) else end
        took = (until - began).total_seconds()
        if 0 <= took <= 6 * 3600:
            out[phase] = round((out[phase] + took) / 2, 1)
    return out


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
        eta = None
        if not halted:
            durations = self._durations()
            if phase.get("phase") == "requested" and requested and requested.get("mode") == "now":
                start = _parse(requested.get("at"))
                eta = back_online_by("warning", requested.get("at"), self._clock(), durations)
                if eta and start is not None:
                    eta = (_parse(eta) + timedelta(seconds=PICKUP_SECONDS)).isoformat(timespec="seconds")
            else:
                eta = back_online_by(str(phase.get("phase")), phase.get("since"), self._clock(), durations)
        return {**phase, "requested": requested["mode"] if requested else None, "halted": halted,
                "enabled": self.enabled, "backOnlineBy": eta}

    def _durations(self) -> dict[str, float]:
        saved = self._kv_json(DURATIONS_KEY) or {}
        return {p: float(saved[p]) if isinstance(saved.get(p), (int, float)) else float(DEFAULT_SECONDS[p])
                for p in RESTART_PHASES}

    def _set_phase(self, phase: str, detail: str, batch: int | None = None) -> None:
        now = self._clock()
        self.queue.kv_set(PHASE_KEY, json.dumps({"phase": phase, "detail": detail[:500], "since": now, "batch": batch}))
        if batch is not None and phase in RESTART_PHASES:
            timings = self._kv_json(TIMINGS_KEY) or {}
            if timings.get("batch") != batch:
                timings = {"batch": batch, "starts": {}}
            timings["starts"].setdefault(phase, now)
            self.queue.kv_set(TIMINGS_KEY, json.dumps(timings))

    def _learn_from(self, batch: int) -> None:
        """A batch that applied cleanly teaches the estimates; failures take odd paths and teach nothing."""
        timings = self._kv_json(TIMINGS_KEY) or {}
        if timings.get("batch") == batch and isinstance(timings.get("starts"), dict):
            self.queue.kv_set(DURATIONS_KEY, json.dumps(learn(self._durations(), timings["starts"], self._clock())))

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
        if outcome == "applied":
            self._learn_from(batch)
        # Friends read these on the status page: no batch numbers (the admin page has them).
        detail = {"applied": "The mod update was applied.",
                  "test-failed": f"The mod update did not apply, so the server is still on the old mods: {reason}",
                  "rolled-back": f"The mod update was undone; the server is back on the old mods and world: {reason}"}[outcome]
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
