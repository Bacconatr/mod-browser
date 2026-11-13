"""The applier: queued requests become a pack change, a backup and a restart, or a rollback.

Behind an off switch (`MOD_BROWSER_APPLIER=1`) and with no real host adapter
yet, so in production nothing here runs. What it is built to do, for each
batch (everything `queued` when it starts):

  1. stage      the candidate on a branch beside the live pack (one commit per request)
  2. test boot  a throwaway container on a COPY of the world must reach Done with no new
                mod-sourced ERRORs. If it does not, the live server, world and pack were never
                touched: requests become `test-failed` with the reason.
  3. countdown  if players are online, RCON `say` warnings first
  4. backup     a world backup, taken right before the live restart. No backup, no restart.
  5. promote    merge the candidate into the live pack (one merge commit)
  6. restart    the live server pulls the pack on start, then must reach Done
  7. verify     if it does not: revert the merge, restart again, requests become `rolled-back`
                with the reason. If even that boot fails, the applier halts and says so.

WHEN: as soon as the server is empty; else at the nightly slot (about 05:00 local)
after the countdown; "restart now" skips the wait but never steps 2 or 4.

One thread calls `tick()` every few seconds. A tick that starts a batch runs it to
the end (minutes), so the status page reads the phase from `status()`, which is a
snapshot under a lock and never blocks on the batch.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .packrepo import StageError, Staged
from .ports import BootResult, ServerUnavailable
from .queue import Queue, Request
from .status import parse_list

log = logging.getLogger("mod_browser.applier")

SLOT = (5, 0)
SLOT_WINDOW = timedelta(hours=1)
NIGHTLY_COUNTDOWN = (300, 60, 10)
NOW_COUNTDOWN = (30,)
HALT_KEY = "applier.halted"


def _clock_text(moment: datetime) -> str:
    return moment.strftime("%H:%M")


class Applier:
    def __init__(self, queue: Queue, repo: Any, host: Any, server: Any, *, enabled: bool = False,
                 tz: str = "America/New_York", now: Callable[[], datetime] | None = None,
                 sleep: Callable[[float], None] = time.sleep, test_timeout: float = 900.0,
                 boot_timeout: float = 600.0, grace: float = 30.0):
        self.queue, self.repo, self.host, self.server = queue, repo, host, server
        self.enabled, self._tz = enabled, ZoneInfo(tz)
        self._now = now or (lambda: datetime.now(self._tz))
        self._sleep = sleep
        self.test_timeout, self.boot_timeout, self.grace = test_timeout, boot_timeout, grace
        self._lock = threading.Lock()
        self._restart_now = False
        # Kept in the queue's database: a halt means the live server may be in a state nobody checked,
        # so a restart of this container (a deploy, an OOM kill) must not quietly resume applying.
        self._halted: str | None = queue.kv_get(HALT_KEY)
        self._status: dict[str, Any] = {"phase": "idle", "detail": "", "since": None, "nextAt": None}
        self._set("idle", "")

    # ---- what the page and admin see ----
    def _set(self, phase: str, detail: str, next_at: datetime | None = None) -> None:
        with self._lock:
            self._status = {"phase": phase, "detail": detail, "since": self._now().isoformat(timespec="seconds"),
                            "nextAt": next_at.isoformat(timespec="seconds") if next_at else None}

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {**self._status, "halted": self._halted, "enabled": self.enabled}

    def request_restart_now(self) -> dict[str, Any]:
        """Apply what is queued without waiting for an empty server or the nightly slot.
        It still test-boots and backs up first, and warns players for 30 seconds."""
        with self._lock:
            self._restart_now = True
        return self.status()

    def resume(self) -> dict[str, Any]:
        """The owner has looked at the server after a halt: clear it (loopback admin API only)."""
        with self._lock:
            self._halted = None
        self.queue.kv_set(HALT_KEY, "")
        self._set("idle", "")
        return self.status()

    # ---- the loop ----
    def run_forever(self, stop: threading.Event, every: float = 15.0) -> None:
        while not stop.wait(every):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the loop must outlive one bad tick
                log.exception("applier tick failed")

    def next_slot(self) -> datetime:
        now = self._now()
        slot = now.replace(hour=SLOT[0], minute=SLOT[1], second=0, microsecond=0)
        return slot if now < slot else slot + timedelta(days=1)

    def _players(self) -> int | None:
        """How many are online; None when the server cannot say (down or booting)."""
        try:
            parsed = parse_list(self.server.rcon("list"))
        except (ServerUnavailable, ValueError):
            return None
        return parsed["online"] if parsed else None

    def _due(self, queued: list[Request]) -> tuple[bool, str]:
        """(apply now?, which countdown applies)"""
        if self._restart_now:
            return True, "now"
        players = self._players()
        if players == 0:
            return True, "empty"
        if players is None:
            if not self.server.container_running():
                return True, "empty"  # nobody can be online in a stopped container
            # Up but not saying who is on (booting, or RCON down): someone may be playing and no countdown
            # could reach them, so wait for an answer, even in the nightly window. Restart now still works.
            return False, ""
        now = self._now()
        slot = now.replace(hour=SLOT[0], minute=SLOT[1], second=0, microsecond=0)
        if slot <= now < slot + SLOT_WINDOW:
            return True, "nightly"
        return False, ""

    def tick(self) -> None:
        if not self.enabled or self._halted:
            return
        # A restart of this process can leave requests mid-batch. Before `promote` the live pack was not
        # changed, so they are safe to fail and ask again. After it, the live pack holds the batch and
        # nobody knows whether the server booted with it: that is the owner's to look at, so halt.
        stuck = self.queue.in_flight()
        promoted = {r.batch for r in stuck if r.batch is not None and self.repo.promoted(r.batch)}
        for request in stuck:
            if request.batch in promoted:
                continue
            self.queue.advance([request.id], ("testing", "applying"), "test-failed",
                               reason="The site restarted while this was being applied. Ask again; the live server was not changed by it.")
        if promoted:
            batches = ", ".join(str(b) for b in sorted(promoted))
            ids = [r.id for r in stuck if r.batch in promoted and r.state == "applying"]
            # The pack holds them, and the server loads the pack on every start: they are applied as far as the
            # pack goes. Recording that keeps a resumed applier from finding them stuck again.
            if ids:
                self.queue.advance(ids, ("applying",), "applied",
                                   reason="In the pack; the site restarted during the server's restart, so the owner was asked to check it.")
            return self._halt(None, f"The site restarted after batch {batches} was merged into the live pack, so whether the "
                                    "server came up with it is unknown. Check the server, roll the pack back by hand if needed, then resume.")
        queued = self.queue.queued()
        if not queued:
            with self._lock:
                self._restart_now = False
            if self._status["phase"] in ("queued", "countdown"):
                self._set("idle", "")
            return
        due, mode = self._due(queued)
        if not due:
            slot = self.next_slot()
            self._set("queued", f"Queued. Applies when the server is empty, or at {_clock_text(slot)} after a countdown.", slot)
            return
        self._run_batch(queued, mode)

    # ---- one batch ----
    def _run_batch(self, queued: list[Request], mode: str) -> None:
        # An upstream update is applied on its own: it rewrites the base pack, and a friend's request
        # computed against the old pack must not ride along. The others wait for the next batch.
        upstream = [r for r in queued if r.kind == "upstream"]
        queued = upstream[:1] or queued
        ids = [r.id for r in queued]
        batch = self.queue.next_batch_id()
        self.queue.advance(ids, ("queued",), "testing", batch=batch)
        with self._lock:
            self._restart_now = False
        staged: Staged | None = None

        def fail_before_live(reason: str) -> None:
            if staged is not None:
                self.repo.discard(staged)
            self.queue.advance(ids, ("testing", "applying"), "test-failed", reason=reason)
            self._set("failed", f"Batch {batch} did not apply, and the live server was not touched: {reason}")
            log.warning("batch %s failed before the live server was touched: %s", batch, reason)

        self._set("test-booting", f"Test-booting {len(ids)} new mod request(s) on a copy of the world.")
        try:
            staged = self.repo.stage(batch, queued)
        except StageError as exc:
            return fail_before_live(f"it could not be written into the pack: {exc}")
        result = self._bounded(lambda: self.host.test_boot(staged.path, self.test_timeout), self.test_timeout + self.grace,
                               "the test boot did not finish")
        if not result.ok:
            return fail_before_live(f"the test boot failed: {result.reason}")

        players = self._players()
        if players:
            self._countdown(NOW_COUNTDOWN if mode == "now" else NIGHTLY_COUNTDOWN)

        self.queue.advance(ids, ("testing",), "applying")
        self._set("backing-up", "Taking a world backup before the restart.")
        try:
            backup = self.host.backup_world()
        except Exception as exc:  # noqa: BLE001 - no backup, no restart
            return fail_before_live(f"the world backup failed, so nothing was restarted: {exc}")
        log.info("batch %s: backup %s", batch, backup)

        try:
            merge = self.repo.promote(staged)
        except StageError as exc:
            return fail_before_live(f"the pack could not be updated: {exc}")
        self.repo.discard(staged)
        staged = None
        self._set("restarting", "Restarting the server. It downloads the new mods on the way up.")
        outcome = self._restart_and_wait()
        if outcome.ok:
            self.queue.advance(ids, ("applying",), "applied")
            self._set("online", f"Applied {len(ids)} request(s). The server is online.")
            return
        reason = f"the server did not come up with the new mods: {outcome.reason}"
        self._set("rolling-back", "The server did not come up. Putting the old pack back.")
        try:
            self.repo.revert(merge)
        except StageError as exc:
            return self._halt(ids, f"{reason}. The pack could not be reverted ({exc}); the owner must fix it.")
        again = self._restart_and_wait()
        self.queue.advance(ids, ("applying",), "rolled-back", reason=reason)
        if not again.ok:
            return self._halt(None, f"{reason}. It also failed to boot after the rollback ({again.reason}); the owner must look.")
        self._set("failed", f"Failed and rolled back: {reason}")
        log.warning("batch %s rolled back: %s", batch, reason)

    def _restart_and_wait(self) -> BootResult:
        try:
            self.host.restart_live()
        except Exception as exc:  # noqa: BLE001
            return BootResult(False, f"the restart itself failed: {exc}")
        return self._bounded(lambda: self.host.wait_live_done(self.boot_timeout), self.boot_timeout + self.grace,
                             "the server never reached Done")

    def _halt(self, ids: list[int] | None, reason: str) -> None:
        if ids:
            self.queue.advance(ids, ("applying",), "rolled-back", reason=reason)
        with self._lock:
            self._halted = reason
        self.queue.kv_set(HALT_KEY, reason)
        self._set("halted", reason)
        log.error("applier halted: %s", reason)

    def _countdown(self, steps: tuple[int, ...]) -> None:
        """RCON `say` at each step, sleeping between them, ending with the last warning's delay."""
        for i, remaining in enumerate(steps):
            when = f"{remaining // 60} minutes" if remaining >= 120 else "1 minute" if remaining == 60 else f"{remaining} seconds"
            self._set("countdown", f"Restarting in {when} to add mods.", self._now() + timedelta(seconds=remaining))
            try:
                self.server.rcon(f"say The server restarts in {when} to add new mods.")
            except (ServerUnavailable, ValueError):
                return  # nobody to warn
            nxt = steps[i + 1] if i + 1 < len(steps) else 0
            self._sleep(remaining - nxt)

    @staticmethod
    def _bounded(fn: Callable[[], BootResult], limit: float, why: str) -> BootResult:
        """Run a host call that may hang; give up on it after `limit` seconds."""
        box: dict[str, Any] = {}

        def run() -> None:
            try:
                box["result"] = fn()
            except Exception as exc:  # noqa: BLE001
                box["result"] = BootResult(False, f"{type(exc).__name__}: {exc}")

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(limit)
        return box.get("result") or BootResult(False, f"{why} within {int(limit)} s")
