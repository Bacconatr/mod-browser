"""The applier, at the HTTP API: queued requests apply, or fail without touching the live server.

The host (test boot, backup, restart) is a fake that can be told to succeed,
fail or hang; the pack is a real git repository written by the real
`GitPackRepo`; the clock and the sleeps are fakes, so a five-minute countdown
takes no time.
"""

from __future__ import annotations

import subprocess
import threading
import tomllib
from datetime import datetime
from zoneinfo import ZoneInfo

from mod_browser.applier import Applier
from mod_browser.packrepo import GitPackRepo, metafile_text
from mod_browser.ports import BootResult
from tests.test_requests import CONNECTED, PONDER, RequestsCase

EST = ZoneInfo("America/New_York")


class FakeHost:
    """The machine the server runs on, scripted by the test."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.test_result = BootResult(True)
        self.hang_test = threading.Event()  # set = hang forever (until the test ends)
        self.hanging = False
        self.backup_error: str | None = None
        self.live_results = [BootResult(True)]  # one per wait_live_done, last one repeats
        self.test_dirs: list = []

    def test_boot(self, pack_dir, timeout):
        self.calls.append("test_boot")
        self.test_dirs.append(pack_dir)
        # the candidate really is a pack: a metafile for what was asked for
        self.candidate_mods = sorted(p.name for p in (pack_dir / "mods").glob("*.pw.toml"))
        if self.hanging:
            threading.Event().wait(3)
        return self.test_result

    def backup_world(self):
        self.calls.append("backup")
        if self.backup_error:
            raise RuntimeError(self.backup_error)
        return "backup-1"

    def restart_live(self):
        self.calls.append("restart")

    def wait_live_done(self, timeout):
        self.calls.append("wait_done")
        return self.live_results.pop(0) if len(self.live_results) > 1 else self.live_results[0]


class ApplierCase(RequestsCase):
    def setUp(self) -> None:
        self.start()
        self.host = FakeHost()
        self.sleeps: list[float] = []
        self.moment = datetime(2026, 11, 13, 14, 0, tzinfo=EST)  # mid-afternoon, not the nightly slot
        self.repo = GitPackRepo(self.dir / "pack", self.dir / "work")
        self.applier = Applier(self.queue, self.repo, self.host, self.fake_server, enabled=True,
                               now=lambda: self.moment, sleep=self.sleeps.append, test_timeout=0.2, boot_timeout=0.2, grace=0.1)
        self.app.applier = self.applier

    def queued(self, *ids, name="Test_Player") -> list[int]:
        """Ask for each mod and have the owner approve it: now `queued`."""
        made = []
        for who, mod in zip((name, "Fake_Friend"), ids):
            rid = self.ask(name=who, ids=mod)[1]["request"]["id"]
            self.assertEqual(self.act(rid, "approve")[1]["request"]["state"], "queued")
            made.append(rid)
        return made

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.dir / "pack"), *args], capture_output=True, text=True, check=True).stdout

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").strip()

    def api_status(self) -> dict:
        return self.call("GET", self.public + "/api/status")[1]


class ApplyTest(ApplierCase):
    def test_a_good_batch_is_test_booted_backed_up_applied_and_shows_in_the_mod_list(self):
        ids = self.queued(CONNECTED, PONDER)
        before = self.head()
        self.applier.tick()          # the server is empty: apply now
        for rid in ids:
            self.assertEqual(self.state_of(rid), "applied")
        self.assertEqual(self.host.calls, ["test_boot", "backup", "restart", "wait_done"])
        self.assertIn("create-connected.pw.toml", self.host.candidate_mods)
        self.assertNotEqual(self.head(), before)
        # one commit per request, each naming who asked, under one merge commit
        log = self.git("log", "--format=%s|%b", "--no-merges", "-n", "2")
        self.assertIn("Requested-by: Test_Player", log)
        self.assertIn("Requested-by: Fake_Friend", log)
        # the site's own mod list now shows them, added by the requester
        _, mods, _ = self.call("GET", self.public + "/api/mods?q=create%20connected")
        entry = mods["mods"][0]
        self.assertEqual((entry["origin"], entry["addedBy"]), ("added", "Test_Player"))
        # the index and pack.toml hashes were refreshed, and the live tree is clean
        pack = tomllib.loads((self.dir / "pack" / "pack.toml").read_text())
        self.assertNotEqual(pack["index"]["hash"], "0")
        self.assertEqual(self.git("status", "--porcelain"), "")
        # the candidate worktree and branch are gone
        self.assertEqual(list((self.dir / "work").glob("candidate-*")), [])
        self.assertNotIn("candidate-", self.git("branch"))

    def test_the_status_page_walks_through_the_phases(self):
        self.assertEqual(self.api_status()["apply"]["phase"], "idle")
        self.queued(CONNECTED)
        self.fake_server.players = ["Test_Player"]
        self.applier.tick()
        status = self.api_status()["apply"]
        self.assertEqual(status["phase"], "queued")
        self.assertIn("05:00", status["detail"])
        self.assertEqual(status["nextAt"], "2026-11-14T05:00:00-05:00")
        self.fake_server.players = []
        self.applier.tick()
        status = self.api_status()["apply"]
        self.assertEqual(status["phase"], "online")
        self.assertIn("Applied 1 request", status["detail"])

    def test_a_batch_that_fails_its_test_boot_never_touches_live(self):
        (rid,) = self.queued(CONNECTED)
        before = self.head()
        self.host.test_result = BootResult(False, "ERROR [create_connected] a missing registry entry")
        self.applier.tick()
        self.assertEqual(self.state_of(rid), "test-failed")
        _, listing, _ = self.call("GET", self.public + "/api/requests")
        self.assertIn("missing registry entry", listing["requests"][0]["reason"])
        self.assertEqual(self.host.calls, ["test_boot"])          # no backup, no restart
        self.assertEqual(self.head(), before)                      # the live pack is untouched
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(list((self.dir / "work").glob("candidate-*")), [])
        self.assertEqual(self.api_status()["apply"]["phase"], "failed")

    def test_a_batch_that_fails_its_live_boot_is_reverted_and_the_server_restarted_again(self):
        (rid,) = self.queued(CONNECTED)
        before_files = self.git("ls-files")
        self.host.live_results = [BootResult(False, "crashed loading create_connected"), BootResult(True)]
        self.applier.tick()
        self.assertEqual(self.state_of(rid), "rolled-back")
        _, listing, _ = self.call("GET", self.public + "/api/requests")
        self.assertIn("crashed loading create_connected", listing["requests"][0]["reason"])
        self.assertEqual(self.host.calls, ["test_boot", "backup", "restart", "wait_done", "restart", "wait_done"])
        self.assertEqual(self.git("ls-files"), before_files)       # the pack is back to what it was
        self.assertIn("Revert", self.git("log", "-1", "--format=%s"))
        self.assertEqual(self.api_status()["apply"]["phase"], "failed")
        self.assertIn("rolled back", self.api_status()["apply"]["detail"])

    def test_if_even_the_rollback_does_not_boot_the_applier_halts_and_says_so(self):
        (rid,) = self.queued(CONNECTED)
        self.host.live_results = [BootResult(False, "boom"), BootResult(False, "still boom")]
        self.applier.tick()
        self.assertEqual(self.state_of(rid), "rolled-back")
        status = self.api_status()["apply"]
        self.assertEqual(status["phase"], "halted")
        self.assertIn("still boom", status["halted"])
        calls = list(self.host.calls)
        self.queued(PONDER, name="Fake_Friend")
        self.applier.tick()
        self.assertEqual(self.host.calls, calls)                    # it does nothing more by itself

    def test_no_backup_no_restart(self):
        (rid,) = self.queued(CONNECTED)
        before = self.head()
        self.host.backup_error = "disk full"
        self.applier.tick()
        self.assertEqual(self.state_of(rid), "test-failed")
        self.assertEqual(self.host.calls, ["test_boot", "backup"])
        self.assertEqual(self.head(), before)
        _, listing, _ = self.call("GET", self.public + "/api/requests")
        self.assertIn("backup failed", listing["requests"][0]["reason"])

    def test_a_hung_test_boot_times_out_and_is_reported_once(self):
        (rid,) = self.queued(CONNECTED)
        self.host.hanging = True
        self.applier.tick()
        self.assertEqual(self.state_of(rid), "test-failed")
        _, listing, _ = self.call("GET", self.public + "/api/requests")
        self.assertIn("did not finish", listing["requests"][0]["reason"])
        _, seen, _ = self.call("GET", self.local + "/admin-api/v1/requests?since=0")
        seq = seen["seq"]
        for _ in range(3):                                           # a minute later, and later still
            self.applier.tick()
        self.assertEqual(self.host.calls, ["test_boot"])
        _, again, _ = self.call("GET", self.local + f"/admin-api/v1/requests?since={seq}")
        self.assertEqual(again["requests"], [])                      # nothing new to report

    def test_a_request_that_cannot_be_written_into_the_pack_fails_before_the_test_boot(self):
        # A dependency that would replace a jar the pack did not get from Modrinth or CurseForge.
        snapshot = {"side": "both", "added": [
            {"name": "Orphan Mod", "source": "modrinth", "projectId": "ORPHAN01", "versionId": "v1", "versionNumber": "2.0",
             "channel": "release", "fileName": "orphan-2.0.jar", "reason": "requested", "replaces": "1.0",
             "downloadUrl": "https://cdn.modrinth.com/data/ORPHAN01/versions/v1/orphan-2.0.jar", "fileHash": "ab", "hashFormat": "sha512", "side": "both"}]}
        request = self.queue.create("Test_Player", False, snapshot)
        self.queue.approve(request.id)
        self.queue.queue(request.id)
        self.applier.tick()
        self.assertEqual(self.state_of(request.id), "test-failed")
        _, listing, _ = self.call("GET", self.public + "/api/requests")
        self.assertIn("swap it by hand", listing["requests"][0]["reason"])
        self.assertEqual(self.host.calls, [])

    def test_a_site_restart_in_the_middle_of_a_batch_fails_it_safely(self):
        (rid,) = self.queued(CONNECTED)
        self.queue.advance([rid], ("queued",), "applying", batch=7)  # the process died here
        self.applier.tick()
        self.assertEqual(self.state_of(rid), "test-failed")
        self.assertEqual(self.host.calls, [])

    def test_the_off_switch_means_nothing_runs_and_the_page_shows_no_apply(self):
        self.queued(CONNECTED)
        self.applier.enabled = False
        self.applier.tick()
        self.assertEqual(self.host.calls, [])
        self.assertNotIn("apply", self.api_status())


class WhenTest(ApplierCase):
    def test_with_players_online_it_waits_for_the_nightly_slot_then_counts_down_in_game(self):
        (rid,) = self.queued(CONNECTED)
        self.fake_server.players = ["Test_Player"]
        self.applier.tick()
        self.assertEqual((self.state_of(rid), self.host.calls), ("queued", []))
        self.moment = datetime(2026, 11, 14, 5, 10, tzinfo=EST)
        self.applier.tick()
        self.assertEqual(self.state_of(rid), "applied")
        says = [c for c in self.fake_server.commands if c.startswith("say ")]
        self.assertEqual(len(says), 3)
        self.assertIn("5 minutes", says[0])
        self.assertIn("1 minute", says[1])
        self.assertIn("10 seconds", says[2])
        self.assertEqual(self.sleeps, [240, 50, 10])
        self.assertLess(self.host.calls.index("test_boot"), self.host.calls.index("backup"))

    def test_restart_now_skips_the_wait_but_never_the_test_boot_or_the_backup(self):
        (rid,) = self.queued(CONNECTED)
        self.fake_server.players = ["Test_Player"]
        self.applier.tick()
        self.assertEqual(self.state_of(rid), "queued")
        code, body, _ = self.call("POST", self.public + "/admin/api/restart-now", {}, self.owner())
        self.assertEqual(code, 200, body)
        self.applier.tick()
        self.assertEqual(self.state_of(rid), "applied")
        self.assertEqual(self.host.calls, ["test_boot", "backup", "restart", "wait_done"])
        says = [c for c in self.fake_server.commands if c.startswith("say ")]
        self.assertEqual((len(says), self.sleeps), (1, [30]))       # a short warning, not five minutes

    def test_restart_now_with_a_failing_test_boot_still_does_not_touch_live(self):
        (rid,) = self.queued(CONNECTED)
        self.fake_server.players = ["Test_Player"]
        self.host.test_result = BootResult(False, "nope")
        self.call("POST", self.local + "/admin-api/v1/restart-now", {})
        self.applier.tick()
        self.assertEqual((self.state_of(rid), self.host.calls), ("test-failed", ["test_boot"]))

    def test_restart_now_needs_the_password_something_queued_and_a_running_applier(self):
        self.assertEqual(self.call("POST", self.public + "/admin/api/restart-now", {})[0], 401)
        self.assertEqual(self.call("POST", self.public + "/admin/api/restart-now", {}, self.owner())[0], 409)  # nothing queued
        self.queued(CONNECTED)
        self.applier.enabled = False
        self.assertEqual(self.call("POST", self.public + "/admin/api/restart-now", {}, self.owner())[0], 409)

    def test_a_stopped_server_is_applied_at_once_but_a_booting_one_is_waited_for(self):
        (rid,) = self.queued(CONNECTED)
        self.fake_server.rcon_up = False       # container up, RCON silent: booting
        self.applier.tick()
        self.assertEqual(self.state_of(rid), "queued")
        self.fake_server.running = False       # container stopped: nobody can be on
        self.applier.tick()
        self.assertEqual(self.state_of(rid), "applied")


class WhileApplyingTest(ApplierCase):
    def test_a_request_being_applied_cannot_be_denied_overridden_or_removed(self):
        (rid,) = self.queued(CONNECTED)
        self.queue.advance([rid], ("queued",), "testing", batch=1)
        self.assertEqual(self.act(rid, "deny", {"reason": "no"})[0], 409)
        self.assertEqual(self.act(rid, "remove")[0], 409)
        self.assertEqual(self.act(rid, "override", {"version": "x"})[0], 409)
        self.assertEqual(self.ask(name="Fake_Friend")[0], 409)     # and asking again says it is already requested


class MetafileTest(ApplierCase):
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
        self.assertEqual(curse["side"], "client")
