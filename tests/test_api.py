"""The app as a caller sees it: real HTTP against both listeners, fakes behind the ports."""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from mod_browser.pack import PackCatalog
from mod_browser.status import StatusMonitor
from mod_browser.web import App, Servers
from tests.fakes import LATER_MOD, FakeServer, add_mod, make_pack


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pack = make_pack(Path(self.tmp.name) / "pack")
        self.server = FakeServer()
        # max_age=0: every request reads the ports afresh, so a test sees a
        # change on its next call. Production uses 10 s.
        monitor = StatusMonitor(self.server, address="play.example.com", max_age=0)
        self.app = App(monitor, PackCatalog(self.pack, max_age=0))
        self.servers = Servers(self.app, public=("127.0.0.1", 0), admin=("127.0.0.1", 0)).start()
        self.addCleanup(self.servers.stop)
        self.public = f"http://127.0.0.1:{self.servers.public.server_address[1]}"
        self.admin = f"http://127.0.0.1:{self.servers.admin.server_address[1]}"

    def get(self, url: str, method: str = "GET") -> tuple[int, dict[str, str], bytes]:
        request = urllib.request.Request(url, method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def json(self, path: str, base: str | None = None) -> dict:
        status, _, body = self.get((base or self.public) + path)
        self.assertEqual(status, 200, body)
        return json.loads(body)

    # ---- status ----
    def test_online_server_shows_players_against_list_and_tps(self):
        self.server.players = ["Test_Player", "Fake_Friend"]
        self.server.started = datetime(2026, 11, 12, 13, 58, 51, tzinfo=ZoneInfo("America/New_York"))
        status = self.json("/api/status")
        self.assertEqual(status["state"], "online")
        self.assertEqual(status["players"], {"online": 2, "max": 10, "names": ["Fake_Friend", "Test_Player"]})
        self.assertEqual(status["tps"], {"tps": 19.41, "msPerTick": 51.51})
        self.assertEqual(status["lastRestart"], "2026-11-12T13:58:51-05:00")
        self.assertEqual(status["address"], "play.example.com")
        self.assertIsNotNone(status["checkedAt"])

    def test_empty_server_is_online_with_nobody(self):
        status = self.json("/api/status")
        self.assertEqual(status["state"], "online")
        self.assertEqual(status["players"], {"online": 0, "max": 10, "names": []})

    def test_stopped_container_reads_down_on_the_next_check(self):
        self.assertEqual(self.json("/api/status")["state"], "online")
        self.server.running = False
        status = self.json("/api/status")
        self.assertEqual(status["state"], "down")
        self.assertIsNone(status["players"])
        self.assertIsNone(status["tps"])
        self.assertEqual(status["address"], "play.example.com")

    def test_container_up_but_rcon_silent_is_starting(self):
        self.server.rcon_up = False
        self.server.started = datetime(2026, 11, 12, 15, 0, tzinfo=ZoneInfo("America/New_York"))
        status = self.json("/api/status")
        self.assertEqual(status["state"], "starting")
        self.assertIsNone(status["players"])
        self.assertEqual(status["lastRestart"], "2026-11-12T15:00:00-05:00")

    def test_tps_failure_still_reports_online(self):
        self.server.tps_up = False
        status = self.json("/api/status")
        self.assertEqual(status["state"], "online")
        self.assertIsNone(status["tps"])

    def test_status_is_cached_for_max_age(self):
        now = [100.0]
        monitor = StatusMonitor(self.server, address="x", max_age=10, clock=lambda: now[0])
        self.assertEqual(monitor.snapshot()["state"], "online")
        self.server.running = False
        now[0] = 109.9
        self.assertEqual(monitor.snapshot()["state"], "online")
        now[0] = 110.0
        self.assertEqual(monitor.snapshot()["state"], "down")

    def test_only_read_only_commands_reach_rcon(self):
        self.json("/api/status")
        self.assertEqual(set(self.server.commands), {"list", "neoforge tps"})

    # ---- mods ----
    def mod(self, mod_id: str) -> dict:
        mods = {m["id"]: m for m in self.json("/api/mods")["mods"]}
        return mods[mod_id]

    def test_search_finds_create_connected_shipped_with_the_base_pack(self):
        found = self.json("/api/mods?q=create%20connected")
        self.assertEqual(found["matched"], 1)
        mod = found["mods"][0]
        self.assertEqual(mod["name"], "Create: Connected")
        self.assertEqual(mod["origin"], "base")
        self.assertIsNone(mod["addedBy"])
        self.assertEqual(mod["version"], "1.3.3")
        self.assertEqual(mod["side"], "both")
        self.assertEqual(mod["kind"], "mod")
        self.assertEqual(mod["source"], {"site": "CurseForge", "url": "https://www.curseforge.com/projects/947914"})

    def test_pre_packwiz_extras_name_the_owner_and_the_date_asked(self):
        deep_seas = self.mod("create-deep-seas")
        self.assertEqual((deep_seas["origin"], deep_seas["addedBy"], deep_seas["addedOn"]),
                         ("extra", "Bacconatr", "2026-10-19"))
        self.assertEqual(deep_seas["source"]["url"], "https://modrinth.com/project/mva5q4qZ")
        crashfix = self.mod("create-collision-crashfix")
        self.assertEqual((crashfix["origin"], crashfix["addedOn"], crashfix["side"]), ("extra", None, "server"))

    def test_later_commit_names_the_requester_and_its_date(self):
        add_mod(self.pack, "mods/straw-statues.pw.toml", LATER_MOD,
                "Add Straw Statues\n\nRequested-by: Test_Player\n", date="2026-11-20T05:00:00-05:00")
        mod = self.mod("straw-statues")
        self.assertEqual((mod["origin"], mod["addedBy"], mod["addedOn"]), ("added", "Test_Player", "2026-11-20"))
        self.assertEqual(mod["version"], "v21.1.1")

    def test_later_commit_without_a_trailer_names_its_author(self):
        add_mod(self.pack, "mods/straw-statues.pw.toml", LATER_MOD, "Add Straw Statues",
                date="2026-11-21T05:00:00-05:00")
        self.assertEqual(self.mod("straw-statues")["addedBy"], "Ansible (aeronautics role)")

    def test_mod_list_carries_the_pack_and_every_metafile(self):
        data = self.json("/api/mods")
        self.assertEqual(data["count"], 4)
        self.assertEqual(data["pack"]["minecraft"], "1.21.1")
        self.assertEqual(data["pack"]["neoforge"], "21.1.264")
        self.assertEqual(len(data["pack"]["commit"]), 12)
        self.assertEqual(self.mod("complementary-reimagined")["kind"], "resource pack")

    def test_filters_by_side_and_every_word(self):
        self.assertEqual([m["id"] for m in self.json("/api/mods?side=server")["mods"]], ["create-collision-crashfix"])
        self.assertEqual(self.json("/api/mods?q=deep%20create")["matched"], 1)
        self.assertEqual(self.json("/api/mods?q=no-such-mod")["mods"], [])

    # ---- the page and its boundaries ----
    def test_page_is_served_with_a_strict_csp(self):
        status, headers, body = self.get(self.public + "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertIn(b"play.example.com", body)
        for asset in ("/static/app.css", "/static/app.js"):
            self.assertEqual(self.get(self.public + asset)[0], 200, asset)

    def test_static_files_cannot_escape_their_directory(self):
        for path in ("/static/../web.py", "/static/%2e%2e/web.py", "/static/"):
            self.assertEqual(self.get(self.public + path)[0], 404, path)

    def test_admin_is_a_seam_not_a_route_yet(self):
        self.assertEqual(self.get(self.public + "/admin")[0], 404)
        self.assertEqual(self.get(self.public + "/admin-api/v1/health")[0], 404)
        self.assertEqual(self.json("/admin-api/v1/health", base=self.admin), {"ok": True})
        self.assertEqual(self.get(self.admin + "/api/status")[0], 404)
        self.assertEqual(self.get(self.admin + "/")[0], 404)

    def test_writes_are_refused(self):
        for method in ("POST", "PUT", "DELETE"):
            self.assertEqual(self.get(self.public + "/api/mods", method=method)[0], 405, method)

    def test_a_broken_pack_answers_503_and_the_listener_lives(self):
        (self.pack / "pack.toml").write_text("not = [toml")
        add_mod(self.pack, "mods/x.pw.toml", LATER_MOD, "break", date="2026-11-22T00:00:00Z")
        self.assertEqual(self.get(self.public + "/api/mods")[0], 503)
        self.assertEqual(self.json("/api/status")["state"], "online")


if __name__ == "__main__":
    unittest.main()
