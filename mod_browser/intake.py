"""Turns asks into queue entries, and owner decisions into state changes.

Friends ask with a typed Minecraft name, checked against the whitelist (which
is never published as a list). Every ask is resolved first, so a request that
could never load is refused at the door, with the reason, instead of waiting
for the owner. Approval re-resolves, because the pack may have changed since
the ask: if it still fits the request is queued, otherwise it stays approved
with a note saying what is wrong.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from .catalog import camel
from .limits import RateLimiter
from .queue import OPEN_STATES, Queue, Request
from .resolver import Verdict

NAME = re.compile(r"^[A-Za-z0-9_]{3,16}$")
MAX_OPEN_PER_NAME = 5
MAX_OPEN_TOTAL = 200
REMOVED = "Removed from the queue by the owner."
OWNER = "Owner"


class Refused(Exception):
    """The ask or the change is refused; `status` is the HTTP status to answer with."""

    def __init__(self, status: int, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status, self.message, self.details = status, message, details or {}


def snapshot_of(verdict: Verdict) -> dict[str, Any]:
    return {"added": camel(verdict.added), "side": verdict.side}


class Intake:
    def __init__(self, queue: Queue, resolve: Callable[[str, str, str | None], Verdict], whitelist: Any,
                 limiter: RateLimiter | None = None):
        self.queue, self._resolve, self.whitelist = queue, resolve, whitelist
        self.limiter = limiter or RateLimiter(30, 60.0)
        # Wrong names are counted too: the name check must not become a way to probe the whitelist.
        self.guesses = RateLimiter(20, 60.0)

    # ---- friends ----
    def submit(self, name: str, source: str, project_id: str, version_id: str | None) -> Request:
        if not isinstance(name, str) or not NAME.match(name):
            raise Refused(400, "That is not a Minecraft name.")
        if self.guesses.full():
            raise Refused(429, "Too many wrong names; try again in a minute.")
        canonical = self.whitelist.canonical(name)
        if canonical is None:
            self.guesses.record()
            raise Refused(403, "That name is not on the whitelist. Ask the owner to add you first.")
        if not self.limiter.allow():
            raise Refused(429, "Too many requests right now; try again in a minute.")
        if self.queue.open_for(canonical) >= MAX_OPEN_PER_NAME:
            raise Refused(429, f"You already have {MAX_OPEN_PER_NAME} open requests; wait for the owner to decide on some.")
        if self.queue.open_count() >= MAX_OPEN_TOTAL:
            raise Refused(429, "The queue is full; try again later.")
        snapshot = self._checked(source, project_id, version_id)
        return self.queue.create(canonical, False, snapshot)

    def _checked(self, source: str, project_id: str, version_id: str | None) -> dict[str, Any]:
        """The resolver's verdict as a snapshot, or a refusal that carries the reasons."""
        verdict = self._resolve(source, project_id, version_id)
        if verdict.status == "already-installed":
            raise Refused(409, "That mod is already in the pack.")
        if verdict.status != "ok":
            problems = [p.message for p in verdict.problems]
            raise Refused(422, problems[0] if problems else "That mod cannot be added.", {"problems": problems})
        existing = self.queue.open_for_project(verdict.added[0].source, verdict.added[0].project_id)
        if existing is not None:
            raise Refused(409, f"That mod is already requested by {existing.requester} ({existing.state}).")
        return snapshot_of(verdict)

    # ---- the owner ----
    def add_directly(self, source: str, project_id: str, version_id: str | None) -> Request:
        snapshot = self._checked(source, project_id, version_id)
        created = self.queue.create(OWNER, True, snapshot)
        return self.approve(created.id)

    def approve(self, request_id: int) -> Request:
        self.queue.approve(request_id)
        return self._try_queue(request_id)

    def requeue(self, request_id: int) -> Request:
        """Try again to queue an approved request that was held back."""
        current = self.queue.get(request_id)
        if current.state != "approved":
            self.queue.queue(request_id)  # raises InvalidTransition
        return self._try_queue(request_id)

    def _try_queue(self, request_id: int) -> Request:
        current = self.queue.get(request_id)
        try:
            verdict = self._resolve(current.source, current.project_id, current.version_id)
        except Exception as exc:  # noqa: BLE001 - an outage must not lose the approval
            return self.queue.hold(request_id, f"Could not check it against the pack just now ({type(exc).__name__}); try queueing again.")
        if verdict.status != "ok":
            reasons = " ".join(p.message for p in verdict.problems) or "It no longer fits the pack."
            if verdict.status == "already-installed":
                reasons = "It is already in the pack."
            return self.queue.hold(request_id, reasons)
        self.queue.override(request_id, snapshot_of(verdict))  # refresh the dependency set
        return self.queue.queue(request_id)

    def deny(self, request_id: int, reason: str) -> Request:
        reason = (reason or "").strip()[:300]
        if not reason:
            raise Refused(400, "A denial needs a reason the friend can read.")
        return self.queue.deny(request_id, reason)

    def remove(self, request_id: int) -> Request:
        if self.queue.get(request_id).state not in ("approved", "queued"):
            raise Refused(409, "Only an approved or queued request can be removed from the queue.")
        return self.queue.deny(request_id, REMOVED)

    def override(self, request_id: int, version_id: str) -> Request:
        current = self.queue.get(request_id)
        if current.state not in OPEN_STATES:
            raise Refused(409, f"A {current.state} request cannot change version.")
        verdict = self._resolve(current.source, current.project_id, version_id)
        if verdict.status != "ok":
            problems = [p.message for p in verdict.problems]
            raise Refused(422, problems[0] if problems else "That version cannot be added.", {"problems": problems})
        changed = self.queue.override(request_id, snapshot_of(verdict))
        return self._try_queue(request_id) if current.state in ("approved", "queued") else changed
