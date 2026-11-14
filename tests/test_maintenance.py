"""The maintenance runner and the app's apply desk, together, at the HTTP API.

The app runs for real (both listeners); the runner reaches it over HTTP on the
loopback listener, as `docker exec` + adminctl does on guest 120. The guest is
`FakeHost`, a small model of the live server: running or stopped, its world
and mods, a join block, a test container that can be left over. The pack is a
real git repository written by the real `GitPackRepo`. A `Crash` thrown from a
host step is the runner's process dying there (PVE killed, guest rebooted); a
new `Runner` on the same journal is the next run.
"""

from __future__ import annotations

import json
import subprocess
import tomllib

from mod_browser.applier import ApplyDesk
from mod_browser.maintenance import AppClient, Journal, Runner
from mod_browser.packrepo import GitPackRepo, metafile_text
from mod_browser.ports import BootResult, HostError
from tests.test_requests import CONNECTED, PONDER, RequestsCase


class Crash(BaseException):
    """The runner's process dies inside a host step."""


class FakeHost:
    """Guest 120: the live server and its files, as far as the runner can change them."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.player_counts: list[int | None] = [0]   # one per players() call; the last repeats
        self.is_running = True
        self.blocked = False
        self.world, self.mods = "world-v1", "mods-v1"
        self.staged_mods: list[str] = []
        self.test_result = BootResult(True, "Done in 40 s")
        self.test_left = False
        self.cleanup_ok = True
        self.boots: list[bool] = [True]               # one per start; the last repeats
        self.start_count = 0
        self.token: str | None = None
        self.token_ok: dict[str, bool] = {}
        self.aside: list[str] = []
        self.said: list[str] = []
        self.say_ok = True
        self.fail: dict[str, str] = {}                # step -> HostError message
        self.crash_at: str | None = None
        self.pruned = 0

    def _step(self, name: str) -> None:
        self.calls.append(name)
        if self.crash_at == name:
            self.crash_at = None
            raise Crash(name)
        if name in self.fail:
            raise HostError(self.fail[name])

    # reads
    def players(self):
        self.calls.append("players")
        if not self.is_running:
            return None
        return self.player_counts.pop(0) if len(self.player_counts) > 1 else self.player_counts[0]

    def running(self):
        return self.is_running

    def say(self, text):
        self.said.append(text)
        return self.say_ok

    def test_leftover(self):
        return self.test_left

    # steps
    def preflight(self, stage_dir):
        self._step("preflight")

    def download(self, pack_dir, stage_dir):
        self._step("download")
        self.staged_mods = sorted(p.name for p in (pack_dir / "mods").glob("*.pw.toml"))

    def block_joins(self):
        self._step("block")
        self.blocked = True

    def allow_joins(self):
        self._step("allow")
        self.blocked = False

    def stop_live(self, clean=True):
        self._step("stop")
        unclean = self.is_running and self.token is not None and not self.token_ok[self.token]
        self.is_running = False
        if unclean and clean:
            raise HostError("the server exited 255 (OOM killed: False), not a clean stop")  # stopped mid-boot

    def backup(self, dest):
        self._step("backup")
        return {"dir": str(dest), "world": {"file": self.world, "sha256": "x"}, "files": {"file": self.mods, "sha256": "y", "members": ["mods"]}}

    def test_boot(self, pack_dir, stage_dir, manifest, timeout):
        self.test_left = True
        self._step("test_boot")
        assert not self.is_running, "the test boot only fits with the live server stopped"
        return self.test_result

    def test_cleanup(self):
        self.calls.append("cleanup")
        if self.cleanup_ok:
            self.test_left = False
        return self.cleanup_ok

    def promote_mods(self, stage_dir):
        self._step("promote_mods")
        self.mods = "mods-v2"

    def restore(self, manifest, aside):
        self._step("restore")
        self.aside.append(self.world)
        self.world, self.mods = manifest["world"]["file"], manifest["files"]["file"]

    def start_live(self):
        self._step("start")
        assert not self.test_left, "never start live beside a test container"
        self.start_count += 1
        self.is_running = True
        self.token = f"start-{self.start_count}"
        ok = self.boots.pop(0) if len(self.boots) > 1 else self.boots[0]
        self.token_ok[self.token] = ok
        if self.mods == "mods-v2":
            self.world = "world-v1+new-blocks"   # a boot with the new mods writes into the world
        return self.token

    def wait_fresh_done(self, token, timeout):
        self._step("wait_done")
        if token != self.token:
            return BootResult(False, "the container restarted during the boot")
        return BootResult(self.token_ok[token], "Done" if self.token_ok[token] else "crashed: missing registry entry")

    def discard_stage(self, stage_dir):
        self.calls.append("discard_stage")

    def prune_backups(self, backup_dir, keep):
        self.pruned += 1


class MaintenanceCase(RequestsCase):
    def setUp(self) -> None:
        self.start()
        self.desk = ApplyDesk(self.queue, enabled=True, requeue=self.app.requests.intake.requeue)
        self.app.applier = self.desk
        self.host = FakeHost()
        self.sleeps: list[float] = []
        self.repo = GitPackRepo(self.dir / "pack", self.dir / "work" / "worktrees")
        self.journal_path = self.dir / "state" / "journal.json"
        self.runner = self.new_runner()

    def new_runner(self) -> Runner:
        def transport(method, path, body):
            code, answer, _ = self.call(method, self.local + path, body if method == "POST" else None)
            return code, answer
        return Runner(AppClient(transport), self.repo, self.host, Journal(self.journal_path, lambda: "2026-11-13T20:00:00+00:00"),
                      work_dir=self.dir / "work", backup_dir=self.dir / "backups", sleep=self.sleeps.append)

    def queued(self, *ids) -> list[int]:
        made = []
        for who, mod in zip(("Test_Player", "Fake_Friend"), ids):
            rid = self.ask(name=who, ids=mod)[1]["request"]["id"]
            self.assertEqual(self.act(rid, "approve")[1]["request"]["state"], "queued")
            made.append(rid)
        return made

    def owner_asks(self, mode="when-empty"):
        return self.call("POST", self.public + "/admin/api/apply", {"mode": mode}, self.owner())

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.dir / "pack"), *args], capture_output=True, text=True, check=True).stdout

    def tree(self) -> str:
        return self.git("rev-parse", "HEAD^{tree}").strip()

    def apply_status(self) -> dict:
        return self.call("GET", self.public + "/api/status")[1]["apply"]

    def journal(self) -> dict | None:
        return json.loads(self.journal_path.read_text()) if self.journal_path.exists() else None


class ApplyTest(MaintenanceCase):
    def test_on_an_empty_server_it_stages_stops_backs_up_test_boots_promotes_and_starts_in_that_order(self):
        ids = self.queued(CONNECTED, PONDER)
        before = self.git("rev-parse", "HEAD").strip()
        self.assertEqual(self.owner_asks()[0], 200)
        self.assertEqual(self.runner.tick(), "applied")
        self.assertEqual([self.state_of(i) for i in ids], ["applied", "applied"])
        steps = [c for c in self.host.calls if c != "players"]
        self.assertEqual(steps, ["preflight", "download", "block", "stop", "backup", "test_boot", "cleanup",
                                 "promote_mods", "start", "wait_done", "allow", "discard_stage"])
        self.assertIn("create-connected.pw.toml", self.host.staged_mods)
        # players were checked before staging, right before maintenance, and behind the join block
        self.assertEqual(self.host.calls.count("players"), 3)
        self.assertFalse(self.host.blocked)
        # one commit per request under one merge, each naming who asked; the mod list shows them
        self.assertNotEqual(self.git("rev-parse", "HEAD").strip(), before)
        log = self.git("log", "--format=%b", "--no-merges", "-n", "2")
        self.assertIn("Requested-by: Test_Player", log)
        self.assertIn("Requested-by: Fake_Friend", log)
        _, mods, _ = self.call("GET", self.public + "/api/mods?q=create%20connected")
        self.assertEqual(mods["mods"][0]["addedBy"], "Test_Player")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertNotIn("candidate-", self.git("branch"))
        # the run is over: the journal is cleared, its record kept, the ask is cleared
        self.assertIsNone(self.journal())
        self.assertEqual(json.loads((self.dir / "state" / "runs.jsonl").read_text().splitlines()[-1])["outcome"], "applied")
        self.assertEqual((self.apply_status()["phase"], self.apply_status()["requested"]), ("online", None))
        self.assertEqual(self.host.pruned, 1)

    def test_nothing_happens_until_the_owner_asks(self):
        (rid,) = self.queued(CONNECTED)
        self.assertEqual(self.runner.tick(), "idle")
        self.assertEqual((self.state_of(rid), self.host.calls), ("queued", []))

    def test_the_status_page_shows_the_ask_and_the_outcome(self):
        self.queued(CONNECTED)
        self.assertEqual(self.apply_status()["phase"], "idle")
        self.owner_asks()
        self.assertEqual((self.apply_status()["phase"], self.apply_status()["requested"]), ("requested", "when-empty"))
        self.host.player_counts = [2]
        self.runner.tick()
        self.assertEqual(self.apply_status()["phase"], "waiting")
        self.host.player_counts = [0]
        self.runner.tick()
        self.assertIn("applied", self.apply_status()["detail"])

    def test_the_app_never_runs_anything_itself_and_the_off_switch_answers_409(self):
        self.queued(CONNECTED)
        self.desk.enabled = False
        self.assertEqual(self.owner_asks()[0], 409)
        self.assertEqual(self.call("POST", self.local + "/admin-api/v1/maintenance/claim", {})[0], 409)
        self.assertEqual(self.runner.tick(), "off")
        self.assertNotIn("apply", self.call("GET", self.public + "/api/status")[1])
        self.assertEqual(self.host.calls, [])

    def test_asking_needs_the_password_and_something_queued_and_resume_is_loopback_only(self):
        self.assertEqual(self.call("POST", self.public + "/admin/api/apply", {"mode": "now"})[0], 401)
        self.assertEqual(self.owner_asks()[0], 409)                                 # nothing queued
        self.queued(CONNECTED)
        self.assertEqual(self.owner_asks("sometime")[0], 400)
        self.assertEqual(self.call("POST", self.public + "/admin/api/restart-now", {}, self.owner())[1]["apply"]["requested"], "now")
        for path in ("/admin/api/maintenance/claim", "/admin-api/v1/applier/resume", "/admin/api/applier/resume"):
            self.assertIn(self.call("POST", self.public + path, {}, self.owner())[0], (404, 405))

    def test_a_request_queued_by_an_older_build_without_a_file_hash_is_resolved_again_at_claim(self):
        (rid,) = self.queued(CONNECTED)
        stale = self.queue.get(rid)
        added = [{k: v for k, v in a.items() if k not in ("fileHash", "downloadUrl")} for a in stale.added]
        self.queue._db.execute("UPDATE requests SET added = ? WHERE id = ?", (json.dumps(added), rid))
        self.owner_asks()
        self.assertEqual(self.runner.tick(), "applied")
        self.assertEqual(self.state_of(rid), "applied")


class WhoIsOnTest(MaintenanceCase):
    def test_with_players_online_it_waits_and_claims_nothing(self):
        (rid,) = self.queued(CONNECTED)
        self.owner_asks()
        self.host.player_counts = [1]
        self.assertEqual(self.runner.tick(), "waiting")
        self.assertEqual(self.state_of(rid), "queued")
        self.assertEqual([c for c in self.host.calls if c != "players"], [])

    def test_a_player_who_joins_during_the_download_defers_the_batch_untouched(self):
        (rid,) = self.queued(CONNECTED)
        self.owner_asks()
        self.host.player_counts = [0, 1]        # empty at the start, one on at the recheck
        self.assertEqual(self.runner.tick(), "deferred")
        self.assertEqual(self.state_of(rid), "queued")
        self.assertNotIn("stop", self.host.calls)
        self.assertNotIn("block", self.host.calls)
        self.assertEqual(self.apply_status()["requested"], "when-empty")       # the ask stands
        self.assertNotIn("candidate-", self.git("branch"))
        self.host.player_counts = [0]
        self.assertEqual(self.runner.tick(), "applied")

    def test_someone_who_gets_in_before_the_block_defers_it_and_joins_are_opened_again(self):
        (rid,) = self.queued(CONNECTED)
        self.owner_asks()
        self.host.player_counts = [0, 0, 1]     # in between the recheck and the block
        self.assertEqual(self.runner.tick(), "deferred")
        self.assertEqual(self.state_of(rid), "queued")
        self.assertEqual([c for c in self.host.calls if c in ("block", "allow", "stop")], ["block", "allow"])
        self.assertFalse(self.host.blocked)

    def test_an_unknown_count_never_stops_an_automatic_run(self):
        (rid,) = self.queued(CONNECTED)
        self.owner_asks()
        self.host.player_counts = [None]
        self.assertEqual(self.runner.tick(), "waiting")
        self.host.player_counts = [0, None]
        self.assertEqual(self.runner.tick(), "deferred")
        self.assertNotIn("stop", self.host.calls)
        self.assertEqual(self.state_of(rid), "queued")

    def test_a_stopped_server_needs_no_count(self):
        self.queued(CONNECTED)
        self.owner_asks()
        self.host.is_running = False
        self.assertEqual(self.runner.tick(), "applied")

    def test_now_warns_in_game_then_goes_ahead(self):
        self.queued(CONNECTED)
        self.owner_asks("now")
        self.host.player_counts = [3]
        self.assertEqual(self.runner.tick(), "applied")
        self.assertEqual(self.host.said, ["The server restarts in 1 minute to add new mods.",
                                          "The server restarts in 10 seconds to add new mods."])
        self.assertEqual(self.sleeps, [50, 10])

    def test_now_with_a_failed_warning_still_goes_ahead_and_says_so(self):
        (rid,) = self.queued(CONNECTED)
        self.owner_asks("now")
        self.host.player_counts, self.host.say_ok = [None], False
        self.assertEqual(self.runner.tick(), "applied")
        self.assertIn("warning could not be sent", self.queue.get(rid).reason)


class FailureTest(MaintenanceCase):
    def test_a_failed_test_boot_starts_the_old_server_and_never_touches_the_pack_or_world(self):
        (rid,) = self.queued(CONNECTED)
        tree = self.tree()
        self.owner_asks()
        self.host.test_result = BootResult(False, "ERROR [create_connected] missing registry entry")
        self.assertEqual(self.runner.tick(), "test-failed")
        self.assertIn("missing registry entry", self.queue.get(rid).reason)
        self.assertEqual(self.tree(), tree)
        self.assertNotIn("promote_mods", self.host.calls)
        self.assertNotIn("restore", self.host.calls)
        self.assertEqual(self.host.calls[-4:], ["start", "wait_done", "allow", "discard_stage"])
        self.assertEqual((self.host.world, self.host.mods, self.host.is_running), ("world-v1", "mods-v1", True))
        self.assertEqual(self.apply_status()["requested"], None)       # a failure is not retried by itself

    def test_a_timed_out_test_boot_is_killed_and_cleaned_up_before_the_live_server_starts(self):
        self.queued(CONNECTED)
        self.owner_asks()
        self.host.test_result = BootResult(False, "no Done within 1200 s; the test server was killed")
        self.assertEqual(self.runner.tick(), "test-failed")
        self.assertLess(self.host.calls.index("cleanup"), self.host.calls.index("start"))

    def test_uncertain_cleanup_halts_with_the_live_server_stopped_and_nothing_more_runs(self):
        (rid,) = self.queued(CONNECTED)
        self.owner_asks()
        self.host.test_result = BootResult(False, "no Done; killed")
        self.host.cleanup_ok = False
        self.assertEqual(self.runner.tick(), "halted")
        self.assertNotIn("start", self.host.calls)
        self.assertFalse(self.host.is_running)
        self.assertEqual(self.state_of(rid), "testing")                 # not marked anything unverified
        self.assertEqual(self.apply_status()["phase"], "halted")
        self.assertEqual(self.owner_asks()[0], 409)
        self.host.calls.clear()
        self.assertEqual(self.new_runner().tick(), "halted")
        self.assertEqual(self.host.calls, [])

    def test_a_failed_live_boot_restores_the_matched_pack_and_world_and_keeps_the_failed_world_aside(self):
        (rid,) = self.queued(CONNECTED)
        tree = self.tree()
        self.owner_asks()
        self.host.boots = [False, True]
        self.assertEqual(self.runner.tick(), "rolled-back")
        self.assertEqual(self.tree(), tree)                             # the matched pack
        self.assertEqual((self.host.world, self.host.mods), ("world-v1", "mods-v1"))
        self.assertEqual(self.host.aside, ["world-v1+new-blocks"])
        order = [c for c in self.host.calls if c in ("stop", "restore", "start")]
        self.assertEqual(order, ["stop", "start", "stop", "restore", "start"])
        self.assertIn("did not come up", self.queue.get(rid).reason)
        self.assertFalse(self.host.blocked)

    def test_if_the_old_server_does_not_boot_after_the_restore_it_halts_and_marks_nothing(self):
        (rid,) = self.queued(CONNECTED)
        self.owner_asks()
        self.host.boots = [False, False]
        self.assertEqual(self.runner.tick(), "halted")
        self.assertEqual(self.state_of(rid), "applying")
        self.assertTrue(self.host.blocked)                              # nobody joins a server nobody verified
        self.assertIn("did not boot", self.apply_status()["detail"])

    def test_no_backup_no_change(self):
        (rid,) = self.queued(CONNECTED)
        tree = self.tree()
        self.owner_asks()
        self.host.fail["backup"] = "tar: No space left on device"
        self.assertEqual(self.runner.tick(), "test-failed")
        self.assertNotIn("test_boot", self.host.calls)
        self.assertEqual(self.tree(), tree)
        self.assertIn("No space left", self.queue.get(rid).reason)
        self.assertTrue(self.host.is_running)

    def test_an_unclean_stop_halts_before_anything_else(self):
        self.queued(CONNECTED)
        self.owner_asks()
        self.host.fail["stop"] = "the server exited 137 (OOM killed: True), not a clean stop"
        self.assertEqual(self.runner.tick(), "halted")
        self.assertEqual(self.host.calls[-1], "stop")

    def test_a_failed_download_fails_the_batch_with_the_server_untouched(self):
        (rid,) = self.queued(CONNECTED)
        self.owner_asks()
        self.host.fail["download"] = "packwiz-installer exited 1: hash mismatch"
        self.assertEqual(self.runner.tick(), "test-failed")
        self.assertNotIn("stop", self.host.calls)
        self.assertIn("hash mismatch", self.queue.get(rid).reason)

    def test_a_request_that_cannot_be_written_fails_before_the_download(self):
        (rid,) = self.queued(CONNECTED)
        added = [{**a, "replaces": "1.0"} for a in self.queue.get(rid).added]
        self.queue._db.execute("UPDATE requests SET added = ? WHERE id = ?", (json.dumps(added), rid))
        self.owner_asks()
        self.assertEqual(self.runner.tick(), "test-failed")
        self.assertNotIn("download", self.host.calls)

    def test_an_outcome_the_app_did_not_hear_is_reported_first_next_time(self):
        (rid,) = self.queued(CONNECTED)
        self.owner_asks()
        broken = self.new_runner()
        real = broken.app._transport
        broken.app._transport = lambda m, p, b: (503, {"error": "down"}) if p.endswith("/finish") else real(m, p, b)
        self.assertEqual(broken.tick(), "app-unreachable")
        self.assertEqual(self.state_of(rid), "applying")
        self.assertEqual(self.journal()["outcome"], "applied")
        self.host.calls.clear()
        self.assertEqual(self.new_runner().tick(), "applied")
        self.assertEqual(self.state_of(rid), "applied")
        self.assertEqual(self.host.calls, [])


class InterruptedTest(MaintenanceCase):
    """The runner's process dies mid-run; a later run must not guess."""

    def crash(self, at: str) -> int:
        (rid,) = self.queued(CONNECTED)
        self.owner_asks()
        self.host.crash_at = at
        with self.assertRaises(Crash):
            self.runner.tick()
        return rid

    def test_before_the_stop_the_next_run_releases_the_batch_and_tries_again(self):
        rid = self.crash("block")
        self.assertEqual(self.new_runner().tick(), "deferred")
        self.assertEqual(self.state_of(rid), "queued")
        self.assertFalse(self.host.blocked)
        self.assertEqual(self.new_runner().tick(), "applied")

    def test_after_the_stop_it_needs_recovery_until_the_owner_runs_it(self):
        rid = self.crash("backup")
        self.assertEqual(self.new_runner().tick(), "halted")
        self.assertIn("needs recovery", self.apply_status()["detail"])
        self.assertEqual(self.state_of(rid), "testing")
        self.assertEqual(self.new_runner().recover(), "test-failed")
        self.assertTrue(self.host.is_running)
        self.assertEqual(self.host.world, "world-v1")
        self.assertEqual(self.apply_status()["halted"], None)

    def test_a_test_container_left_by_a_dead_run_is_removed_before_anything_else(self):
        rid = self.crash("test_boot")
        self.assertTrue(self.host.test_left)
        self.assertEqual(self.new_runner().tick(), "halted")       # cleaned up, but still needs recovery
        self.assertFalse(self.host.test_left)
        self.assertEqual(self.new_runner().recover(), "test-failed")
        self.assertEqual(self.state_of(rid), "test-failed")

    def test_after_promote_with_the_new_server_verified_up_recovery_records_applied(self):
        rid = self.crash("wait_done")                              # it started; the runner died watching it
        self.assertEqual(self.new_runner().tick(), "halted")
        self.assertEqual(self.new_runner().recover(), "applied")
        self.assertEqual(self.state_of(rid), "applied")
        self.assertNotIn("restore", self.host.calls)

    def test_after_promote_without_evidence_of_a_fresh_boot_recovery_halts(self):
        (rid,) = self.queued(CONNECTED)
        tree = self.tree()
        self.owner_asks()
        self.host.boots = [False, True]
        self.host.crash_at = "wait_done"
        with self.assertRaises(Crash):
            self.runner.tick()
        self.assertEqual(self.new_runner().recover(), "halted")
        self.assertNotIn("restore", self.host.calls)
        self.assertNotEqual(self.state_of(rid), "rolled-back")

    def test_reboot_may_open_joins_and_recovery_preserves_player_progress(self):
        rid = self.crash("wait_done")
        self.host.blocked = False  # guest reboot lost the firewall rule
        self.host.start_live()
        self.host.world = "player-progress"
        self.assertEqual(self.new_runner().recover(), "halted")
        self.assertEqual(self.host.world, "player-progress")
        self.assertNotIn("restore", self.host.calls)
        self.assertNotEqual(self.state_of(rid), "rolled-back")

    def test_recovery_never_rolls_back_after_joins_may_have_opened(self):
        self.crash("allow")
        self.host.start_live()
        self.assertEqual(self.new_runner().recover(), "halted")
        self.assertNotIn("restore", self.host.calls)

    def test_filesystem_error_during_backup_restarts_old_server(self):
        (rid,) = self.queued(CONNECTED)
        self.owner_asks()
        def fail(dest):
            raise OSError("disk full")
        self.host.backup = fail
        self.assertEqual(self.runner.tick(), "test-failed")
        self.assertTrue(self.host.is_running)
        self.assertEqual(self.state_of(rid), "test-failed")

    def test_recovery_accepts_an_uncleanly_stopped_container(self):
        self.crash("backup")
        calls = []
        def stop(clean=True):
            calls.append(clean)
            if clean:
                raise HostError("unclean exit")
        self.host.stop_live = stop
        self.assertEqual(self.new_runner().recover(), "test-failed")
        self.assertEqual(calls, [False])

    def test_restore_filesystem_error_halts_without_claiming_success(self):
        (rid,) = self.queued(CONNECTED)
        self.owner_asks()
        self.host.boots = [False, True]
        def fail(manifest, aside):
            raise OSError("disk full")
        self.host.restore = fail
        self.assertEqual(self.runner.tick(), "halted")
        self.assertNotEqual(self.state_of(rid), "rolled-back")
        self.assertFalse(self.host.is_running)

    def test_a_crash_in_the_middle_of_a_restore_requires_inspection(self):
        (rid,) = self.queued(CONNECTED)
        self.owner_asks()
        self.host.boots = [False, True]
        self.host.crash_at = "restore"
        with self.assertRaises(Crash):
            self.runner.tick()
        self.assertEqual(self.new_runner().recover(), "halted")
        self.assertEqual(self.host.calls.count("restore"), 1)

    def test_a_malformed_claim_from_the_app_is_never_used_for_paths(self):
        self.queued(CONNECTED)
        self.owner_asks()
        runner = self.new_runner()
        real = runner.app._transport

        def evil(method, path, body):
            code, answer = real(method, path, body)
            if path.endswith("/claim") and code == 200:
                answer["apply"]["batch"] = "../../../etc"
            return code, answer
        runner.app._transport = evil
        self.assertEqual(runner.tick(), "halted")
        self.assertEqual([c for c in self.host.calls if c != "players"], [])
        self.assertFalse((self.dir / "etc").exists())

    def test_a_lost_journal_with_requests_in_flight_halts(self):
        self.crash("stop")
        self.journal_path.unlink()
        self.assertEqual(self.new_runner().tick(), "halted")


class WhileApplyingTest(MaintenanceCase):
    def test_a_request_being_applied_cannot_be_denied_overridden_or_removed(self):
        (rid,) = self.queued(CONNECTED)
        self.queue.advance([rid], ("queued",), "testing", batch=1)
        self.assertEqual(self.act(rid, "deny", {"reason": "no"})[0], 409)
        self.assertEqual(self.act(rid, "remove")[0], 409)
        self.assertEqual(self.act(rid, "override", {"version": "x"})[0], 409)
        self.assertEqual(self.ask(name="Fake_Friend")[0], 409)


class MetafileTest(MaintenanceCase):
    def test_the_metafiles_are_the_ones_packwiz_writes(self):
        modrinth = tomllib.loads(metafile_text({
            "name": "Create: Connected", "source": "modrinth", "projectId": "Vg5TIO6d", "versionId": "Xe7EqzfQ",
            "fileName": "create_connected-1.3.3.jar", "side": "both", "downloadUrl": "https://cdn.modrinth.com/x.jar",
            "fileHash": "ab12", "hashFormat": "sha512"}))
        self.assertEqual(modrinth["download"], {"url": "https://cdn.modrinth.com/x.jar", "hash-format": "sha512", "hash": "ab12"})
        self.assertEqual(modrinth["update"]["modrinth"], {"mod-id": "Vg5TIO6d", "version": "Xe7EqzfQ"})
        curse = tomllib.loads(metafile_text({
            "name": "Some Mod", "source": "curseforge", "projectId": "947914", "versionId": "8777573",
            "fileName": "x.jar", "side": "client", "fileHash": "72c3", "hashFormat": "sha1"}))
        self.assertEqual(curse["download"]["mode"], "metadata:curseforge")
        self.assertEqual(curse["update"]["curseforge"], {"file-id": 8777573, "project-id": 947914})


class UpstreamApplyTest(MaintenanceCase):
    """An approved upstream update goes through the same runner: add, change, remove, pins kept, alone."""

    def setUp(self) -> None:
        super().setUp()
        from datetime import datetime as dt
        from zoneinfo import ZoneInfo
        from mod_browser.upstream import Release, UpstreamChecker, UpstreamMod, version_of
        pack = self.dir / "pack"
        (pack / "mods" / "old-base-mod.pw.toml").write_text(
            'name = "Old Base Mod"\nfilename = "oldbase-2.0.jar"\nside = "both"\n\n[download]\nhash-format = "sha1"\nhash = "11"\n'
            'mode = "metadata:curseforge"\n\n[update]\n[update.curseforge]\nfile-id = 555001\nproject-id = 5555\n')
        self.git("add", "-A")
        subprocess.run(["git", "-C", str(pack), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--amend", "--no-edit"], check=True)

        def mod(project, file, name, filename):
            return UpstreamMod(project, file, name, filename, version_of(filename), "aa", "sha1")
        release = Release("9400000", "All of Create - Aeronautics v2.10", "2.10", "2026-11-20T10:00:00Z", (
            mod("947914", "8777574", "Create: Connected", "create_connected-1.3.4-mc1.21.1.jar"),
            mod("999111", "7", "Create Deep Seas", "create_submarine-3.3.0.jar"),
            mod("4242", "11", "Straw Statues", "strawstatues-1.0.jar")))
        self.checker = UpstreamChecker(self.queue, type("S", (), {"latest": lambda _self: release})(), self.app.catalog,
                                       now=lambda: dt(2026, 11, 20, 4, 5, tzinfo=ZoneInfo("America/New_York")))
        self.checker.check()
        self.upstream = self.queue.of_kind("upstream")[0].id

    def test_the_update_is_written_into_the_pack_added_changed_removed_and_the_pin_left_alone(self):
        self.act(self.upstream, "approve")
        self.owner_asks()
        self.assertEqual(self.runner.tick(), "applied")
        files = self.git("ls-files", "mods").split()
        self.assertIn("mods/straw-statues.pw.toml", files)
        self.assertNotIn("mods/old-base-mod.pw.toml", files)
        changed = tomllib.loads((self.dir / "pack" / "mods" / "create-connected.pw.toml").read_text())
        self.assertEqual(changed["update"]["curseforge"]["file-id"], 8777574)
        self.assertEqual(tomllib.loads((self.dir / "pack" / "mods" / "create-deep-seas.pw.toml").read_text())["filename"],
                         "create_submarine-3.4.5.jar")
        listed = {f["file"] for f in tomllib.loads((self.dir / "pack" / "index.toml").read_text())["files"]}
        self.assertNotIn("mods/old-base-mod.pw.toml", listed)

    def test_a_failed_live_boot_puts_the_old_base_pack_back(self):
        self.act(self.upstream, "approve")
        before = self.git("ls-files", "-s", "mods")
        self.owner_asks()
        self.host.boots = [False, True]
        self.assertEqual(self.runner.tick(), "rolled-back")
        self.assertEqual(self.git("ls-files", "-s", "mods"), before)

    def test_it_goes_alone_and_the_friends_request_follows_on_the_same_ask(self):
        (friend,) = self.queued(PONDER)
        self.act(self.upstream, "approve")
        self.owner_asks()
        self.assertEqual(self.runner.tick(), "applied")
        self.assertEqual((self.queue.get(self.upstream).state, self.state_of(friend)), ("applied", "queued"))
        self.assertEqual(self.runner.tick(), "applied")
        self.assertEqual(self.state_of(friend), "applied")
        self.assertEqual(self.host.calls.count("test_boot"), 2)
