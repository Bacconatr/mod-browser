"""The maintenance runner: the one thing that stops, backs up, test-boots, promotes and starts the server.

It runs on guest 120 itself, as root, started by PVE over `pct exec 120` (a
timer that ships disabled, and the owner's `recover`). The Internet-facing app
never gets Docker access: it only records that the owner asked for an apply,
and the runner reaches it on the loopback admin API through `docker exec`
(`adminctl.py`), so request states still change in one place (`applier.py`).

One batch, in order. Each phase is written to the journal BEFORE its side
effect, so an interrupted run knows the worst it may have done:

   1 claim       the app hands over what is queued (an upstream update alone)
   2 stage       the candidate pack on a branch in a worktree beside the live pack
   3 download    packwiz-installer fills a COPY of mods/ for the candidate (live server still up)
   4 recheck     players, right before maintenance. when-empty: anyone online, or no answer,
                 defers the batch untouched. now: a warning in game (60 s, 10 s)
   5 block       new connections to the game port are refused (iptables, DOCKER-USER);
                 when-empty checks once more behind the block and defers if anyone got in
   6 stop        graceful; the container must have exited 0 having saved every dimension
   7 backup      a cold copy of the world and the server files, verified against the disk
   8 test boot   the candidate against a disposable copy restored FROM that backup; fits in
                 memory only because the live server is stopped. Must reach Done and stay up
   9 clean up    the test container and its copy must be confirmed gone, or the run halts
                 with the live server stopped (it would not fit beside a leftover)
  10 promote     merge the candidate into the live pack, swap in the downloaded mods
  11 start       the live server, then fresh-boot evidence: Done in the log of THIS start
  12 open        joins allowed again; the app records `applied`

On a failure before promote, the live pack and world were never changed: start
the old server (fresh Done required) and record `test-failed`. On a failure
after promote: stop, revert the pack to the matched commit (its tree checked),
put the failed world aside, restore world and server files from the verified
backup, start the old server, and only then record `rolled-back`. Anything that
cannot be verified halts: the journal keeps "needs recovery" and the app shows
it, until the owner runs `recover`, which verifies the actual pack, world and
boot before it records anything.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .packrepo import GitPackRepo, StageError, Staged
from .ports import BootResult, HostError, MaintenanceHost

COUNTDOWN = (60, 10)
#: The phases before the server is stopped: an interruption there changed nothing live (at most a join block).
BEFORE_STOP = ("claimed", "staging", "staged", "warning", "blocking-joins", "joins-blocked")


class AppError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status, self.message = status, message


class AppClient:
    """The app's loopback admin API. `transport(method, path, body) -> (status, body)`: in production
    `docker exec` + adminctl (see `exec_transport`), in tests plain HTTP to the admin listener."""

    def __init__(self, transport: Callable[[str, str, dict[str, Any] | None], tuple[int, Any]]):
        self._transport = transport

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        status, answer = self._transport(method, "/admin-api/v1" + path, body)
        if status != 200:
            message = answer.get("error") if isinstance(answer, dict) else str(answer)
            raise AppError(status, str(message)[:300])
        return answer.get("apply", answer) if isinstance(answer, dict) else {}

    def pending(self) -> dict[str, Any]:
        return self._call("GET", "/maintenance")

    def claim(self) -> dict[str, Any]:
        return self._call("POST", "/maintenance/claim", {})

    def release(self, batch: int, reason: str) -> None:
        self._call("POST", "/maintenance/release", {"batch": batch, "reason": reason})

    def phase(self, batch: int | None, phase: str, detail: str, state: str | None = None) -> None:
        body: dict[str, Any] = {"batch": batch, "phase": phase, "detail": detail}
        if state:
            body["state"] = state
        self._call("POST", "/maintenance/phase", body)

    def finish(self, batch: int, outcome: str, reason: str) -> None:
        self._call("POST", "/maintenance/finish", {"batch": batch, "outcome": outcome, "reason": reason})

    def halt(self, reason: str) -> None:
        self._call("POST", "/maintenance/halt", {"reason": reason})

    def resume(self) -> None:
        self._call("POST", "/applier/resume", {})


def exec_transport(container: str, run: Callable[..., Any] = subprocess.run):
    """Reach the app's loopback listener from the guest: `docker exec -i <app> python -m mod_browser.adminctl`."""
    def transport(method: str, path: str, body: dict[str, Any] | None) -> tuple[int, Any]:
        done = run(["docker", "exec", "-i", container, "python", "-m", "mod_browser.adminctl", method, path],
                   input=json.dumps(body or {}), capture_output=True, text=True, timeout=60)
        if done.returncode != 0:
            return 503, {"error": f"adminctl exited {done.returncode}: {done.stderr.strip()[:200]}"}
        try:
            answer = json.loads(done.stdout)
        except ValueError:
            return 503, {"error": "adminctl printed no JSON"}
        return int(answer.get("status", 503)), answer.get("body")
    return transport


class Journal:
    """The run's state on the guest's disk (root-only), written atomically before every side effect."""

    def __init__(self, path: Path, clock: Callable[[], str]):
        self.path, self._clock = path, clock

    def load(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return None

    def save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as fh:
            json.dump(state, fh, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def enter(self, state: dict[str, Any], phase: str, **fields: Any) -> None:
        state.update(fields, phase=phase)
        state.setdefault("history", []).append([phase, self._clock()])
        self.save(state)

    def close(self, state: dict[str, Any]) -> None:
        """The run ended verified: keep its record in the log, and clear the journal."""
        with open(self.path.with_name("runs.jsonl"), "a") as fh:
            fh.write(json.dumps(state) + "\n")
        self.path.unlink(missing_ok=True)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _Request:
    """What `GitPackRepo.stage` reads, from the app's JSON."""

    def __init__(self, raw: dict[str, Any]):
        self.id, self.requester, self.added = raw["id"], raw["requester"], raw["added"]
        self.kind, self.mod_name = raw.get("kind", "mod"), raw["mod"]["name"]


class Runner:
    def __init__(self, app: AppClient, repo: GitPackRepo, host: MaintenanceHost, journal: Journal, *,
                 work_dir: Path, backup_dir: Path, test_timeout: float = 1200.0, boot_timeout: float = 1200.0,
                 sleep: Callable[[float], None] = time.sleep, keep_backups: int = 3):
        self.app, self.repo, self.host, self.journal = app, repo, host, journal
        self.work_dir, self.backup_dir = work_dir, backup_dir
        self.test_timeout, self.boot_timeout, self._sleep, self.keep_backups = test_timeout, boot_timeout, sleep, keep_backups

    # ---- telling the app (best effort for phases: the journal is the record) ----
    def _say(self, j: dict[str, Any] | None, phase: str, detail: str, state: str | None = None) -> None:
        try:
            self.app.phase(j["batch"] if j else None, phase, detail, state)
        except AppError:
            if state:
                raise
            # A status line the page missed is not worth stopping a maintenance for.

    def _halt(self, j: dict[str, Any] | None, reason: str) -> str:
        j = j if j is not None else {}
        j["halted"] = reason
        self.journal.enter(j, j.get("phase") or "halted")
        try:
            self.app.halt(reason)
        except AppError:
            pass
        return "halted"

    # ---- one tick: start a batch if the owner asked, or report what stops us ----
    def tick(self) -> str:
        j = self.journal.load()
        if j and j.get("halted"):
            self._tell_halt(j["halted"])
            return "halted"
        if self.host.test_leftover() and not self.host.test_cleanup():
            return self._halt(j, "needs recovery: a test-boot container or its world copy is left over and could not be "
                                 "removed, so nothing else runs (it would not fit beside the live server).")
        if j and j.get("outcome"):
            return self._report_outcome(j)
        if j:
            if j.get("phase") in BEFORE_STOP and not j.get("stopStarted"):
                return self._abandon_before_stop(j, "The runner was interrupted before the server was touched; it tries again.")
            return self._halt(j, f"needs recovery: a maintenance run of batch {j.get('batch')} was interrupted at "
                                 f"'{j.get('phase')}'. Run the recovery on PVE; it verifies the pack, world and boot.")
        try:
            pending = self.app.pending()
        except AppError as exc:
            return "off" if exc.status in (404, 409) else "app-unreachable"
        if pending.get("halted"):
            return "idle"
        if pending.get("inFlight"):
            # The app holds requests mid-batch but this journal has none: a lost journal. Never guess.
            return self._halt(None, "needs recovery: the app has requests in flight but the runner has no record of them.")
        if not pending.get("requested") or not pending.get("queued"):
            return "idle"
        if pending["requested"] == "when-empty":
            players, running = self.host.players(), self.host.running()
            if players or (players is None and running):
                self._say(None, "waiting", "Waiting for an empty server." if players else
                          "Waiting: the server could not say who is online.")
                return "waiting"
        try:
            claim = self.app.claim()
        except AppError:
            return "idle"
        return self._run(claim)

    def _tell_halt(self, reason: str) -> None:
        try:
            self.app.halt(reason)
        except AppError:
            pass

    def _report_outcome(self, j: dict[str, Any]) -> str:
        """A verified outcome the app has not heard yet (it was unreachable): say it before anything else."""
        try:
            self.app.finish(j["batch"], j["outcome"], j.get("reason", ""))
        except AppError as exc:
            if exc.status != 409:
                return "app-unreachable"
        self.journal.close(j)
        return j["outcome"]

    # ---- the batch ----
    def _stage_dir(self, batch: int) -> Path:
        return self.work_dir / f"stage-{batch}"

    @staticmethod
    def _checked(claim: Any) -> tuple[int, str, list[_Request]]:
        """The claim comes from the Internet-facing app, and `batch` names directories this root process
        writes and deletes: nothing in it is used until it has the exact shape expected."""
        def whole(v: Any) -> bool:
            return isinstance(v, int) and not isinstance(v, bool) and 1 <= v < 10**9
        if not isinstance(claim, dict) or not whole(claim.get("batch")) or claim.get("mode") not in ("when-empty", "now"):
            raise ValueError("malformed claim")
        raw = claim.get("requests")
        if not isinstance(raw, list) or not raw or len(raw) > 200:
            raise ValueError("malformed claim: requests")
        requests = []
        for r in raw:
            if (not isinstance(r, dict) or not whole(r.get("id")) or not isinstance(r.get("requester"), str)
                    or not isinstance(r.get("added"), list) or not all(isinstance(a, dict) for a in r["added"])
                    or not isinstance(r.get("mod"), dict) or not isinstance(r["mod"].get("name"), str)):
                raise ValueError("malformed claim: a request")
            requests.append(_Request(r))
        return claim["batch"], claim["mode"], requests

    def _run(self, claim: dict[str, Any]) -> str:
        try:
            batch, mode, requests = self._checked(claim)
        except ValueError as exc:
            return self._halt(None, f"needs recovery: the app handed over a {exc}; nothing was done.")
        j: dict[str, Any] = {"batch": batch, "mode": mode, "ids": [r.id for r in requests], "started": _utc()}
        self.journal.enter(j, "claimed", before=self.repo.head(), beforeTree=self.repo.tree())
        self.journal.enter(j, "staging")
        try:
            staged = self.repo.stage(batch, requests)
        except StageError as exc:
            return self._fail_before_stop(j, None, f"it could not be written into the pack: {exc}")
        j["candidate"] = str(staged.path)
        self._say(j, "staging", f"Downloading the mods for {len(requests)} request(s); the server is still up.")
        try:
            self.host.preflight(self._stage_dir(batch))
            self.host.download(staged.path, self._stage_dir(batch))
        except HostError as exc:
            return self._fail_before_stop(j, staged, f"its mods could not be downloaded: {exc}")
        self.journal.enter(j, "staged")

        # ---- right before maintenance: who is on? ----
        players, running = self.host.players(), self.host.running()
        if mode == "when-empty":
            if players is None and running:
                return self._defer(j, staged, "The server could not say who is online, so nothing was stopped.")
            if players:
                return self._defer(j, staged, f"{players} player(s) joined; waiting for an empty server.")
        elif running and players != 0:
            self.journal.enter(j, "warning")
            if not self._countdown(j):
                j["warningFailed"] = True  # the owner asked for now; the stop below still saves the world first
        self.journal.enter(j, "blocking-joins")
        try:
            self.host.block_joins()
        except HostError as exc:
            return self._defer(j, staged, f"New joins could not be blocked ({exc}), so nothing was stopped.")
        self.journal.enter(j, "joins-blocked")
        if mode == "when-empty" and running:
            late = self.host.players()
            if late is None or late:
                return self._defer(j, staged, "Someone joined at the last moment; waiting for an empty server."
                                   if late else "The server stopped answering at the last moment, so nothing was stopped.")

        # ---- the server stops here ----
        self.journal.enter(j, "stopping", stopStarted=True)
        self._say(j, "stopping", "Stopping the server to add mods. It saves the world first.")
        try:
            self.host.stop_live()
        except HostError as exc:
            return self._halt(j, f"needs recovery: the server did not stop cleanly ({exc}); nothing else was done.")
        self.journal.enter(j, "backing-up")
        self._say(j, "backing-up", "Backing up the world.")
        try:
            manifest = self.host.backup(self.backup_dir / f"batch-{batch}")
        except HostError as exc:
            return self._start_old(j, staged, "test-failed", f"the world backup failed ({exc}), so nothing was changed")
        self.journal.enter(j, "backed-up", manifest=manifest)
        self.journal.enter(j, "test-booting")
        self._say(j, "test-booting", "Test-booting the new mods on a copy of the world.")
        result = self.host.test_boot(staged.path, self._stage_dir(batch), manifest, self.test_timeout)
        self.journal.enter(j, "test-cleaning", testResult=[result.ok, result.reason])
        if not self.host.test_cleanup():
            return self._halt(j, "needs recovery: the test boot's container or world copy could not be confirmed gone, so "
                                 "the live server was left stopped (it would not fit beside it).")
        if not result.ok:
            return self._start_old(j, staged, "test-failed", f"the test boot failed: {result.reason}")

        # ---- promote: the live pack changes here ----
        try:
            self._say(j, "promoting", "The test boot passed. Putting the new pack in place.", state="applying")
        except AppError as exc:
            return self._start_old(j, staged, "test-failed", f"the app did not accept the batch as applying ({exc})")
        self.journal.enter(j, "promoting", promoteStarted=True)
        try:
            j["merge"] = self.repo.promote(staged)
            self.journal.save(j)
            self.host.promote_mods(self._stage_dir(batch))
        except (StageError, HostError) as exc:
            return self._roll_back(j, staged, f"the new pack could not be put in place: {exc}")
        self.journal.enter(j, "promoted")
        return self._start_new(j, staged)

    def _countdown(self, j: dict[str, Any]) -> bool:
        said = True
        for i, remaining in enumerate(COUNTDOWN):
            when = "1 minute" if remaining == 60 else f"{remaining} seconds"
            self._say(j, "warning", f"Restarting in {when} to add mods.")
            said = self.host.say(f"The server restarts in {when} to add new mods.") and said
            nxt = COUNTDOWN[i + 1] if i + 1 < len(COUNTDOWN) else 0
            self._sleep(remaining - nxt)
        return said

    def _start_new(self, j: dict[str, Any], staged: Staged | None) -> str:
        self.journal.enter(j, "starting")
        self._say(j, "starting", "Starting the server with the new mods.")
        try:
            token = self.host.start_live()
        except HostError as exc:
            return self._roll_back(j, staged, f"the server would not start with the new mods: {exc}")
        self.journal.enter(j, "waiting-done", startToken=token)
        boot = self.host.wait_fresh_done(token, self.boot_timeout)
        if not boot.ok:
            return self._roll_back(j, staged, f"the server did not come up with the new mods: {boot.reason}")
        self.journal.enter(j, "live-verified")
        return self._open_and_finish(j, staged, "applied", "")

    def _open_and_finish(self, j: dict[str, Any], staged: Staged | None, outcome: str, reason: str) -> str:
        try:
            self.host.allow_joins()
        except HostError as exc:
            return self._halt(j, f"needs recovery: the server is up but joins could not be unblocked ({exc}).")
        if j.get("warningFailed") and outcome == "applied":
            reason = "Applied; the in-game warning could not be sent."
        self._discard(j, staged)
        self.journal.enter(j, "finished", outcome=outcome, reason=reason)
        if outcome == "applied":
            self.host.prune_backups(self.backup_dir, self.keep_backups)
        return self._report_outcome(j)

    def _start_old(self, j: dict[str, Any], staged: Staged | None, outcome: str, reason: str) -> str:
        """Nothing was promoted: the live pack and world are as they were. Start the old server, verified."""
        self.journal.enter(j, "starting-old")
        self._say(j, "starting", "Starting the server again on the old pack.")
        if self.repo.tree() != j["beforeTree"]:
            return self._halt(j, "needs recovery: the live pack is not the one the run started from, so the server was left stopped.")
        try:
            token = self.host.start_live()
        except HostError as exc:
            return self._halt(j, f"needs recovery: {reason}; then the old server would not start ({exc}).")
        self.journal.enter(j, "waiting-old", startToken=token)
        boot = self.host.wait_fresh_done(token, self.boot_timeout)
        if not boot.ok:
            return self._halt(j, f"needs recovery: {reason}; then the old server did not boot either ({boot.reason}).")
        self.journal.enter(j, "old-verified")
        return self._open_and_finish(j, staged, outcome, reason)

    def _roll_back(self, j: dict[str, Any], staged: Staged | None, reason: str) -> str:
        """After promote: the matched pack and the world backup both go back before the old server starts."""
        self.journal.enter(j, "rolling-back", rollbackReason=reason)
        self._say(j, "rolling-back", "The new mods did not come up. Putting the old pack and world back.")
        try:
            self.host.stop_live()
        except HostError as exc:
            return self._halt(j, f"needs recovery: {reason}; then the server did not stop for the rollback ({exc}).")
        if self.repo.tree() != j["beforeTree"]:
            merge = j.get("merge") or (self.repo.head() if self.repo.promoted(j["batch"]) else None)
            try:
                if merge is None:
                    raise StageError("the live pack changed but holds no merge of this batch")
                self.repo.revert(merge)
            except StageError as exc:
                return self._halt(j, f"needs recovery: {reason}; the pack could not be reverted ({exc}).")
        if self.repo.tree() != j["beforeTree"]:
            return self._halt(j, f"needs recovery: {reason}; after the revert the pack still differs from the one the run started from.")
        if not j.get("manifest"):
            return self._halt(j, f"needs recovery: {reason}; there is no verified backup to restore.")
        self.journal.enter(j, "restoring")
        try:
            self.host.restore(j["manifest"], self.work_dir / f"failed-{j['batch']}")
        except HostError as exc:
            return self._halt(j, f"needs recovery: {reason}; restoring the world backup failed ({exc}).")
        self.journal.enter(j, "restored")
        try:
            token = self.host.start_live()
        except HostError as exc:
            return self._halt(j, f"needs recovery: {reason}; after the restore the old server would not start ({exc}).")
        self.journal.enter(j, "waiting-old", startToken=token)
        boot = self.host.wait_fresh_done(token, self.boot_timeout)
        if not boot.ok:
            return self._halt(j, f"needs recovery: {reason}; after the restore the old server did not boot ({boot.reason}).")
        self.journal.enter(j, "old-verified")
        return self._open_and_finish(j, staged, "rolled-back", reason)

    # ---- ends that never touched the server ----
    def _discard(self, j: dict[str, Any], staged: Staged | None) -> None:
        if staged is None and j.get("candidate"):
            staged = Staged(j["batch"], f"candidate-{j['batch']}", Path(j["candidate"]))
        if staged is not None:
            self.repo.discard(staged)
        self.host.discard_stage(self._stage_dir(j["batch"]))

    def _fail_before_stop(self, j: dict[str, Any], staged: Staged | None, reason: str) -> str:
        self._discard(j, staged)
        self.journal.enter(j, "finished", outcome="test-failed", reason=reason)
        return self._report_outcome(j)

    def _defer(self, j: dict[str, Any], staged: Staged | None, reason: str) -> str:
        if j.get("phase") in ("blocking-joins", "joins-blocked"):
            try:
                self.host.allow_joins()
            except HostError as exc:
                return self._halt(j, f"needs recovery: joins could not be unblocked ({exc}).")
        self._discard(j, staged)
        try:
            self.app.release(j["batch"], reason)
        except AppError as exc:
            if exc.status != 409:
                self.journal.enter(j, "deferred-unreported", deferReason=reason)
                return "app-unreachable"
        self.journal.enter(j, "deferred")
        self.journal.close(j)
        return "deferred"

    def _abandon_before_stop(self, j: dict[str, Any], reason: str) -> str:
        return self._defer(j, None, reason) if j.get("phase") != "deferred-unreported" else self._defer(j, None, j["deferReason"])

    # ---- the owner's recovery: verify, then record ----
    def recover(self) -> str:
        j = self.journal.load()
        if self.host.test_leftover() and not self.host.test_cleanup():
            return self._halt(j, "needs recovery: the test-boot container or its world copy still cannot be removed.")
        if j is None or not j.get("batch"):
            if j is not None:
                self.journal.close(j)
            self._resume()
            return "clear"
        j.pop("halted", None)
        self.journal.enter(j, j.get("phase", "claimed"), recovering=_utc())
        if j.get("outcome"):
            result = self._report_outcome(j)
        elif not j.get("stopStarted"):
            result = self._defer(j, None, "Recovered: the run was interrupted before the server was touched.")
        elif j.get("phase") == "live-verified" or (j.get("startToken") and j.get("promoteStarted") and j.get("phase") == "waiting-done"):
            result = self._recover_new(j)
        elif j.get("promoteStarted"):
            result = self._roll_back(j, None, "the run was interrupted after the new pack was put in place; "
                                              "rolled back to the backup by the owner's recovery")
        else:
            try:
                self.host.stop_live()
            except HostError as exc:
                return self._halt(j, f"needs recovery: the server did not stop for the recovery ({exc}).")
            result = self._start_old(j, None, "test-failed", "the run was interrupted before the new pack was put in "
                                                             "place; the server was restarted on the old pack by the owner's recovery")
        if result not in ("halted", "app-unreachable"):
            self._resume()
        return result

    def _recover_new(self, j: dict[str, Any]) -> str:
        """The new server may already be up. Accept it only on evidence: the pack is the merge, and this
        very start (same token) logged Done. Anything less rolls back."""
        merged = j.get("merge") and self.repo.head() == j["merge"]
        boot = self.host.wait_fresh_done(j["startToken"], 60) if merged else BootResult(False, "the pack is not the merge")
        if merged and boot.ok:
            self.journal.enter(j, "live-verified")
            return self._open_and_finish(j, None, "applied", "")
        return self._roll_back(j, None, f"the recovery could not verify the new server ({boot.reason}); rolled back")

    def _resume(self) -> None:
        try:
            self.app.resume()
        except AppError:
            pass


# ---- the command line, on guest 120 (PVE: `pct exec 120 -- mod-maintenance <command>`) ----
EXIT = {"halted": 3, "app-unreachable": 4, "busy": 5}


def main(argv: list[str] | None = None) -> int:
    from .dockerhost import DockerHost, IptablesGate, NoGate

    parser = argparse.ArgumentParser(prog="mod-maintenance", description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=("tick", "recover", "status"))
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/mod-maintenance"))
    parser.add_argument("--work-dir", type=Path, default=Path("/opt/aeronautics/maintenance"))
    parser.add_argument("--backup-dir", type=Path, default=Path("/mnt/aeronautics-backups/maintenance"))
    parser.add_argument("--data-dir", type=Path, default=Path("/opt/aeronautics/data"))
    parser.add_argument("--pack-dir", type=Path, default=None, help="default: <data-dir>/pack")
    parser.add_argument("--live", default="aeronautics")
    parser.add_argument("--sidecar", default="aeronautics-backup", help="'' for none")
    parser.add_argument("--app", default="aeronautics-mod-browser")
    parser.add_argument("--game-port", type=int, default=25565, help="the container's port, as DOCKER-USER sees it")
    parser.add_argument("--join-gate", choices=("iptables", "none"), default="iptables")
    parser.add_argument("--test-memory", default="9g")
    parser.add_argument("--test-timeout", type=float, default=1200.0)
    parser.add_argument("--boot-timeout", type=float, default=1200.0)
    parser.add_argument("--keep-backups", type=int, default=3)
    args = parser.parse_args(argv)
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    journal = Journal(args.state_dir / "journal.json", _utc)
    if args.command == "status":
        print(json.dumps(journal.load() or {"phase": "none"}, indent=1))
        return 0
    lock = open(args.state_dir / "lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("mod-maintenance: another run holds the lock", file=sys.stderr)
        return EXIT["busy"]
    gate = IptablesGate(args.game_port) if args.join_gate == "iptables" else NoGate()
    host = DockerHost(data_dir=args.data_dir, work_dir=args.work_dir, live=args.live, sidecar=args.sidecar or None,
                      gate=gate, test_memory=args.test_memory)
    runner = Runner(AppClient(exec_transport(args.app)), GitPackRepo(args.pack_dir or args.data_dir / "pack", args.work_dir / "worktrees"),
                    host, journal, work_dir=args.work_dir, backup_dir=args.backup_dir, test_timeout=args.test_timeout,
                    boot_timeout=args.boot_timeout, keep_backups=args.keep_backups)
    result = runner.tick() if args.command == "tick" else runner.recover()
    state = journal.load()
    print(f"mod-maintenance {args.command}: {result}" + (f" ({state['halted']})" if state and state.get("halted") else ""))
    return EXIT.get(result, 0)


if __name__ == "__main__":
    sys.exit(main())
