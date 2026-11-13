"""Upstream All of Create: Aeronautics releases, offered to the owner, at the HTTP API.

The CurseForge side is hand-written from its documented schema (the key
cannot try anything live here), so what these tests prove is the comparison,
the request and the safety around them, not that CurseForge answers this way.
"""

from __future__ import annotations

import io
import unittest
import json
import logging
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo

from mod_browser.upstream import CurseForgeUpstream, Release, UpstreamChecker, UpstreamMod
from tests.fakes import _git
from tests.test_requests import RequestsCase

EST = ZoneInfo("America/New_York")
OLD_BASE_MOD = '''name = "Old Base Mod"
filename = "oldbase-2.0.jar"
side = "both"

[download]
hash-format = "sha1"
hash = "11"
mode = "metadata:curseforge"

[update]
[update.curseforge]
file-id = 555001
project-id = 5555
'''


def mod(project: str, file: str, name: str, filename: str, sha1: str = "aa") -> UpstreamMod:
    from mod_browser.upstream import version_of
    return UpstreamMod(project, file, name, filename, version_of(filename), sha1, "sha1")


#: Upstream v2.10: Create: Connected bumped, Deep Seas bundled OLDER than the owner's pin, one new mod,
#: and Old Base Mod (in the base pack) dropped. Create Collision Crashfix (an owner pin) is not upstream at all.
RELEASE = Release("9400000", "All of Create - Aeronautics v2.10", "2.10", "2026-11-20T10:00:00Z", (
    mod("947914", "8777574", "Create: Connected", "create_connected-1.3.4-mc1.21.1.jar"),
    mod("999111", "7", "Create Deep Seas", "create_submarine-3.3.0.jar"),
    mod("4242", "11", "Straw Statues", "strawstatues-1.0.jar"),
))


class FakeUpstream:
    def __init__(self, release: Release | Exception):
        self.release = release
        self.calls = 0

    def latest(self) -> Release:
        self.calls += 1
        if isinstance(self.release, Exception):
            raise self.release
        return self.release


class UpstreamCase(RequestsCase):
    def setUp(self) -> None:
        self.start()
        pack = self.dir / "pack"
        (pack / "mods" / "old-base-mod.pw.toml").write_text(OLD_BASE_MOD)
        _git(pack, "add", "-A")
        _git(pack, "commit", "-q", "--amend", "--no-edit")      # part of the migrated base pack
        self.source = FakeUpstream(RELEASE)
        self.moment = datetime(2026, 11, 20, 4, 5, tzinfo=EST)
        self.checker = UpstreamChecker(self.queue, self.source, self.app.catalog, now=lambda: self.moment)
        self.app.upstream = self.checker

    def requests(self, base: str | None = None) -> list[dict]:
        return self.call("GET", (base or self.local) + "/admin-api/v1/requests?since=0")[1]["requests"]

    def only(self) -> dict:
        found = [r for r in self.requests() if r["kind"] == "upstream"]
        self.assertEqual(len(found), 1, found)
        return found[0]


class DifferenceTest(UpstreamCase):
    def test_a_newer_release_shows_what_was_added_removed_changed_and_the_pin_that_is_kept(self):
        self.assertEqual(self.checker.check()["lastResult"], "new-request")
        diff = self.only()["meta"]["diff"]
        self.assertEqual([m["name"] for m in diff["added"]], ["Straw Statues"])
        self.assertEqual(diff["removed"], [{"name": "Old Base Mod", "version": "2.0"}])
        self.assertEqual(diff["changed"], [{"name": "Create: Connected", "from": "1.3.3-mc1.21.1", "to": "1.3.4-mc1.21.1"}])
        # Deep Seas: the owner pins 3.4.5 (a Modrinth file); upstream bundles 3.3.0 under a different id. The pin stays.
        self.assertEqual(diff["kept"], [{"name": "Create Deep Seas [BETA]", "pack": "3.4.5", "upstream": "3.3.0", "upstreamNewer": False}])
        self.assertNotIn("Create Collision Crashfix", json.dumps(diff))   # an extra upstream has never heard of is left alone
        self.assertNotIn("Complementary", json.dumps(diff))               # a resource pack is not part of the comparison

    def test_the_pack_operations_carry_what_the_applier_needs(self):
        self.checker.check()
        ops = {o["name"]: o for o in self.only()["added"]}
        self.assertEqual(ops["Straw Statues"]["op"], "add")
        self.assertEqual((ops["Straw Statues"]["projectId"], ops["Straw Statues"]["versionId"], ops["Straw Statues"]["fileHash"]),
                         ("4242", "11", "aa"))
        self.assertEqual((ops["Create: Connected"]["op"], ops["Create: Connected"]["metafile"]),
                         ("change", "mods/create-connected.pw.toml"))
        self.assertEqual(ops["Old Base Mod"], {**ops["Old Base Mod"], "op": "remove", "metafile": "mods/old-base-mod.pw.toml"})
        self.assertNotIn("Create Deep Seas", ops)                          # the pin is not an operation at all

    def test_a_pin_newer_than_upstream_is_kept_and_a_pin_older_is_kept_but_flagged(self):
        older = Release("9400000", "v2.10", "2.10", "2026-11-20T10:00:00Z", (mod("999111", "7", "Create Deep Seas", "create_submarine-4.0.0.jar"),))
        self.source.release = older
        self.checker.check()
        kept = self.only()["meta"]["diff"]["kept"]
        self.assertEqual((kept[0]["pack"], kept[0]["upstream"], kept[0]["upstreamNewer"]), ("3.4.5", "4.0.0", True))
        self.assertEqual([o["name"] for o in self.only()["added"] if o["op"] != "remove"], [])  # nothing to apply for it


class OwnerOnlyTest(UpstreamCase):
    def test_it_appears_on_admin_and_the_poll_feed_but_not_on_the_public_queue(self):
        self.checker.check()
        listed = self.call("GET", self.public + "/admin/api/requests", headers=self.owner())[1]["requests"]
        self.assertEqual([r["kind"] for r in listed], ["upstream"])
        self.assertEqual(listed[0]["state"], "pending")
        self.assertEqual(listed[0]["requester"], "Upstream")
        public = self.call("GET", self.public + "/api/requests")[1]["requests"]
        self.assertEqual(public, [])
        self.assertNotIn("Upstream update", self.call("GET", self.public + "/queue")[1])

    def test_it_does_nothing_until_approved(self):
        pack_before = self.git_head()
        self.checker.check()
        self.assertEqual((self.waiting(), self.git_head()), ([], pack_before))   # nothing queued, nothing written
        rid = self.only()["id"]
        code, body, _ = self.act(rid, "approve")
        self.assertEqual((code, body["request"]["state"]), (200, "queued"))
        self.assertEqual(self.git_head(), pack_before)                                # approving only queues it

    def test_it_has_no_other_version_to_pick(self):
        self.checker.check()
        self.assertEqual(self.act(self.only()["id"], "override", {"version": "abc"})[0], 409)

    def test_a_difference_computed_against_an_older_pack_is_held_for_the_next_check(self):
        self.checker.check()
        self.app.requests.intake._pack_head = lambda: "deadbeefcafe"      # the pack moved on since
        code, body, _ = self.act(self.only()["id"], "approve")
        self.assertEqual((body["request"]["state"], code), ("approved", 200))
        self.assertIn("pack changed after", body["request"]["note"])
        self.assertEqual(self.waiting(), [])

    def test_the_owner_can_deny_it_and_it_is_not_asked_again(self):
        self.checker.check()
        self.act(self.only()["id"], "deny", {"reason": "Not this month."})
        self.assertEqual(self.checker.check()["lastResult"], "up-to-date")
        self.assertEqual(len(self.queue.of_kind("upstream")), 1)

    def waiting(self) -> list:
        return [r for r in self.queue.all() if r.state == "queued"]

    def git_head(self) -> str:
        import subprocess
        return subprocess.run(["git", "-C", str(self.dir / "pack"), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


class DailyTest(UpstreamCase):
    def test_the_same_release_twice_is_one_request(self):
        self.checker.check()
        self.assertEqual(self.checker.check()["lastResult"], "up-to-date")
        self.assertEqual(len(self.queue.of_kind("upstream")), 1)

    def test_a_still_newer_release_replaces_the_one_nobody_decided_on(self):
        self.checker.check()
        first = self.only()["id"]
        self.source.release = Release("9500000", "v2.11", "2.11", "2026-11-27T10:00:00Z", (
            mod("947914", "8777575", "Create: Connected", "create_connected-1.3.5-mc1.21.1.jar"),))
        self.checker.check()
        states = {r.id: r.state for r in self.queue.of_kind("upstream")}
        self.assertEqual(states[first], "denied")
        self.assertEqual(sorted(states.values()), ["denied", "pending"])
        self.assertIn("replaced", self.queue.get(first).reason)

    def test_a_release_that_changes_nothing_files_nothing(self):
        same = Release("9400000", "v2.9", "2.9", "2026-11-12T10:00:00Z", (
            mod("947914", "8777573", "Create: Connected", "create_connected-1.3.3-mc1.21.1.jar"),
            mod("5555", "555001", "Old Base Mod", "oldbase-2.0.jar")))
        self.source.release = same
        self.assertEqual(self.checker.check()["lastResult"], "nothing-to-change")
        self.assertEqual(self.queue.of_kind("upstream"), [])

    def test_a_curseforge_outage_is_reported_once_a_day_at_most(self):
        self.source.release = RuntimeError("CurseForge answered 503")
        with self.assertLogs("mod_browser.upstream", level="ERROR") as logs:
            for _ in range(5):                                  # every minute for a while
                self.checker.check()
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(self.call("GET", self.local + "/admin-api/v1/upstream")[1]["upstream"]["error"], "RuntimeError: CurseForge answered 503")
        self.moment = datetime(2026, 11, 21, 4, 5, tzinfo=EST)  # the next day, still down
        with self.assertLogs("mod_browser.upstream", level="ERROR") as logs:
            self.checker.check()
            self.checker.check()
        self.assertEqual(len(logs.records), 1)
        # And back up: the error clears and the release is filed.
        self.source.release = RELEASE
        self.assertEqual(self.checker.check()["lastResult"], "new-request")
        self.assertIsNone(self.checker.status()["error"])

    def test_a_restart_the_same_day_does_not_report_the_outage_again(self):
        self.source.release = RuntimeError("down")
        with self.assertLogs("mod_browser.upstream", level="ERROR"):
            self.checker.check()
        again = UpstreamChecker(self.queue, self.source, self.app.catalog, now=lambda: self.moment)   # a new process
        with self.assertRaises(AssertionError):                  # assertLogs fails when nothing is logged
            with self.assertLogs("mod_browser.upstream", level="ERROR"):
                again.check()

    def test_without_a_curseforge_key_the_check_says_so_and_does_nothing(self):
        checker = UpstreamChecker(self.queue, None, self.app.catalog, now=lambda: self.moment)
        self.assertEqual(checker.check()["lastResult"], "not-configured")
        self.assertFalse(checker.status()["configured"])
        self.assertEqual(self.queue.of_kind("upstream"), [])

    def test_check_now_needs_the_password_and_a_configured_check(self):
        self.assertEqual(self.call("POST", self.public + "/admin/api/upstream/check", {})[0], 401)
        code, body, _ = self.call("POST", self.public + "/admin/api/upstream/check", {}, self.owner())
        self.assertEqual((code, body["upstream"]["lastResult"]), (200, "new-request"))
        self.app.upstream = None
        self.assertEqual(self.call("POST", self.public + "/admin/api/upstream/check", {}, self.owner())[0], 409)


class CurseForgeAdapterTest(UpstreamCase):
    """CurseForge's answers, hand-written from its documented schema: the modpack file, its manifest, and the batch lookups."""

    class Http:
        def __init__(self) -> None:
            self.posts: list[tuple[str, dict]] = []
            manifest = {"files": [{"projectID": 947914, "fileID": 8777574, "required": True},
                                  {"projectID": 4242, "fileID": 11, "required": True}]}
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("manifest.json", json.dumps(manifest))
            self.pack_zip = buf.getvalue()

        def json(self, url, headers=None):
            assert "/mods/1518930/files" in url and headers["x-api-key"] == "k"
            return {"data": [
                {"id": 9300000, "displayName": "All of Create - Aeronautics v2.9", "fileName": "v2.9.zip", "releaseType": 1,
                 "fileDate": "2026-11-12T00:00:00Z", "isAvailable": True, "downloadUrl": "https://mediafilez.forgecdn.net/old.zip"},
                {"id": 9400000, "displayName": "All of Create - Aeronautics v2.10", "fileName": "All+of+Create+-+Aeronautics+v2.10.zip",
                 "releaseType": 1, "fileDate": "2026-11-20T00:00:00Z", "isAvailable": True, "downloadUrl": "https://mediafilez.forgecdn.net/new.zip"},
                {"id": 9500000, "displayName": "v2.11 beta", "fileName": "v2.11.zip", "releaseType": 2,
                 "fileDate": "2026-11-25T00:00:00Z", "isAvailable": True, "downloadUrl": "https://mediafilez.forgecdn.net/beta.zip"}]}

        def fetch_jar(self, url):
            assert url == "https://mediafilez.forgecdn.net/new.zip"      # the newest RELEASE, not the beta
            return io.BytesIO(self.pack_zip)

        def post_json(self, url, body, headers=None):
            self.posts.append((url, body))
            if url.endswith("/mods/files"):
                return {"data": [{"id": 8777574, "modId": 947914, "fileName": "create_connected-1.3.4-mc1.21.1.jar",
                                  "hashes": [{"value": "ab", "algo": 1}, {"value": "cd", "algo": 2}]},
                                 {"id": 11, "modId": 4242, "fileName": "strawstatues-1.0.jar", "displayName": "strawstatues-1.0.jar", "hashes": []}]}
            return {"data": [{"id": 947914, "name": "Create: Connected"}, {"id": 4242, "name": "Straw Statues"}]}

    def test_reads_the_newest_release_its_manifest_and_names_every_mod(self):
        http = self.Http()
        release = CurseForgeUpstream(http, "k").latest()
        self.assertEqual((release.file_id, release.version), ("9400000", "2.10"))
        self.assertEqual([(m.project_id, m.name, m.version, m.file_hash) for m in release.mods], [
            ("947914", "Create: Connected", "1.3.4-mc1.21.1", "ab"), ("4242", "Straw Statues", "1.0", None)])
        self.assertEqual([u.rsplit("/", 1)[1] for u, _ in http.posts], ["files", "mods"])


if __name__ == "__main__":
    unittest.main()
