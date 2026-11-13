"""Requests, the queue and /admin, at the HTTP API.

Both listeners run for real; the catalogs, the jars and the mods directory
are the recorded fixtures from `browse_fakes`, and the whitelist is a fake
behind its port.
"""

from __future__ import annotations

import base64
import json
import socket
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from mod_browser.curseforge import CurseForgeCatalog
from mod_browser.intake import Intake
from mod_browser.limits import RateLimiter
from mod_browser.modrinth import ModrinthCatalog
from mod_browser.pack import PackCatalog
from mod_browser.queue import Queue
from mod_browser.sources import CompositeCatalog, DirInstalled, HttpJars
from mod_browser.status import StatusMonitor
from mod_browser.web import App, Browse, Requests, Servers
from tests.browse_fakes import (BASE_INSTALLED, RecordedHttp, build_jar, modrinth_url, mods_toml, recorded_json,
                                toml, write_installed)
from tests.fakes import FakeServer, make_pack

PASSWORD = "correct horse battery staple 42"
CONNECTED = ("Vg5TIO6d", "Xe7EqzfQ")  # Create: Connected 1.3.3
PONDER = ("I5JahfLa", "grCjEcqZ")      # Ponder for Kaleidoscope Cookery (client-only)
RIVAL_JAR = "https://cdn.modrinth.com/data/RIVAL001/versions/RIVALV01/rival-1.0.0.jar"


class FakeWhitelist:
    """Behind WhitelistPort: it can answer a name, never list them."""

    def __init__(self, *names: str):
        self._names = {n.casefold(): n for n in names}

    def canonical(self, name: str) -> str | None:
        return self._names.get(name.casefold())


class RequestsCase(unittest.TestCase):
    def start(self, installed=None, password: str | None = PASSWORD, limit: int = 30, whitelist=("Test_Player", "Fake_Friend")) -> None:
        self.http = RecordedHttp()
        self.http.jars[modrinth_url(*CONNECTED)] = build_jar(toml("create-connected"))
        self.http.jars[modrinth_url(*PONDER)] = build_jar(toml("ponder-for-kaleidoscope-cookery"))
        self.http.jars[RIVAL_JAR] = build_jar(mods_toml("rivalmod", "1.0.0", [{"modId": "create", "type": "incompatible"}]))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        mods = self.dir / "mods"
        mods.mkdir()
        self.mods = mods
        write_installed(mods, BASE_INSTALLED if installed is None else installed)
        browse = Browse(
            catalog=CompositeCatalog(ModrinthCatalog(self.http), CurseForgeCatalog(self.http, "test-key"), curseforge_search=False),
            jars=HttpJars(self.http), installed=DirInstalled(mods))
        self.queue = Queue(self.dir / "requests.db")
        self.addCleanup(self.queue._db.close)
        intake = Intake(self.queue, browse.resolve, FakeWhitelist(*whitelist), RateLimiter(limit, 60.0))
        monitor = StatusMonitor(FakeServer(), address="play.example.com", max_age=0)
        self.app = App(monitor, PackCatalog(make_pack(self.dir / "pack"), max_age=0), browse=browse,
                       requests=Requests(queue=self.queue, intake=intake), admin_password=password)
        self.servers = Servers(self.app, public=("127.0.0.1", 0), admin=("127.0.0.1", 0)).start()
        self.addCleanup(self.servers.stop)
        self.public = f"http://127.0.0.1:{self.servers.public.server_address[1]}"
        self.local = f"http://127.0.0.1:{self.servers.admin.server_address[1]}"

    # ---- calling ----
    def call(self, method: str, url: str, body=None, headers: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        h = {"Content-Type": "application/json", **(headers or {})} if data is not None else dict(headers or {})
        request = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw, code, hdrs = response.read(), response.status, dict(response.headers)
        except urllib.error.HTTPError as error:
            raw, code, hdrs = error.read(), error.code, dict(error.headers)
        try:
            return code, json.loads(raw), hdrs
        except ValueError:
            return code, raw.decode(), hdrs

    def owner(self, password: str = PASSWORD) -> dict:
        return {"Authorization": "Basic " + base64.b64encode(f"owner:{password}".encode()).decode()}

    def ask(self, name="Test_Player", ids=CONNECTED, **extra):
        return self.call("POST", self.public + "/api/requests",
                         {"name": name, "source": "modrinth", "id": ids[0], "version": ids[1], **extra})

    def act(self, request_id: int, action: str, body=None, base: str | None = None):
        path = "/admin-api/v1" if base == "local" else "/admin/api"
        url = (self.local if base == "local" else self.public) + f"{path}/requests/{request_id}/{action}"
        return self.call("POST", url, body or {}, None if base == "local" else self.owner())

    def state_of(self, request_id: int) -> str:
        _, body, _ = self.call("GET", self.public + "/api/requests")
        return next(r["state"] for r in body["requests"] if r["id"] == request_id)


class AskTest(RequestsCase):
    def test_a_whitelisted_name_is_accepted_in_the_whitelists_own_spelling(self):
        self.start()
        code, body, _ = self.ask(name="test_player")
        self.assertEqual(code, 201, body)
        self.assertEqual(body["request"]["requester"], "Test_Player")
        self.assertEqual(body["request"]["state"], "pending")
        self.assertEqual(body["request"]["mod"]["name"], "Create: Connected")

    def test_a_name_that_is_not_on_the_whitelist_is_refused_and_nothing_is_stored(self):
        self.start()
        code, body, _ = self.ask(name="Stranger_9")
        self.assertEqual(code, 403)
        self.assertIn("not on the whitelist", body["error"])
        self.assertEqual(self.queue.all(), [])
        self.assertEqual(self.ask(name="not a name!")[0], 400)

    def test_the_whitelist_is_never_published_as_a_list(self):
        self.start()
        self.ask()
        for path in ("/api/requests", "/queue", "/api/status", "/api/mods"):
            code, body, _ = self.call("GET", self.public + path)
            self.assertNotIn("Fake_Friend", body if isinstance(body, str) else json.dumps(body), path)

    def test_a_mod_already_in_the_pack_a_duplicate_and_an_incompatible_mod_are_refused_with_the_reason(self):
        self.start(installed=[*BASE_INSTALLED, ("create_connected-1.3.3.jar", build_jar(toml("create-connected")))])
        code, body, _ = self.ask()
        self.assertEqual((code, body["error"]), (409, "That mod is already in the pack."))
        self.assertEqual(self.ask(ids=PONDER)[0], 201)
        code, body, _ = self.ask(name="Fake_Friend", ids=PONDER)
        self.assertEqual(code, 409)
        self.assertIn("already requested by Test_Player (pending)", body["error"])
        code, body, _ = self.call("POST", self.public + "/api/requests",
                                  {"name": "Test_Player", "source": "modrinth", "id": "RIVAL001"})
        self.assertEqual(code, 422)
        self.assertIn("incompatible with create", body["problems"][0])

    def test_a_curseforge_mod_whose_author_blocks_outside_downloads_is_refused(self):
        self.start()
        code, body, _ = self.call("POST", self.public + "/api/requests",
                                  {"name": "Test_Player", "source": "curseforge", "id": "1793084"})
        self.assertEqual(code, 422)
        self.assertIn("author blocks downloads outside CurseForge", body["error"])

    def test_guessing_names_is_cut_off_so_the_whitelist_cannot_be_probed(self):
        self.start()
        codes = [self.ask(name=f"Guess_{i:03d}")[0] for i in range(22)]
        self.assertEqual(codes[:20], [403] * 20)
        self.assertEqual(codes[20:], [429, 429])
        # A real name is also held back until the window passes: the cost of the guard.
        self.assertEqual(self.ask()[0], 429)

    def test_asking_is_rate_limited(self):
        self.start(limit=2)
        self.assertEqual(self.ask()[0], 201)
        self.assertEqual(self.ask(ids=PONDER)[0], 201)
        self.assertEqual(self.ask(name="Fake_Friend", ids=("Vg5TIO6d", "Xe7EqzfQ"))[0], 429)

    def test_a_write_must_be_json_from_this_site(self):
        self.start()
        req = urllib.request.Request(self.public + "/api/requests", data=b'{"name":"Test_Player"}', method="POST",
                                     headers={"Content-Type": "text/plain"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req)
        self.assertEqual(caught.exception.code, 415)
        code, _, _ = self.call("POST", self.public + "/api/requests", {"name": "Test_Player"},
                               {"Origin": "https://evil.example"})
        self.assertEqual(code, 403)

    def test_an_oversized_or_malformed_body_is_refused(self):
        self.start()
        with socket.create_connection(("127.0.0.1", int(self.public.rsplit(":", 1)[1]))) as s:
            s.sendall(b"POST /api/requests HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: 999999\r\n\r\n")
            self.assertIn(b"413", s.recv(200))
        self.assertEqual(self.call("POST", self.public + "/api/requests", ["not", "an", "object"])[0], 400)


class DecisionTest(RequestsCase):
    def test_approve_queues_it_and_the_public_page_shows_each_step(self):
        self.start()
        _, made, _ = self.ask()
        rid = made["request"]["id"]
        self.assertEqual(self.state_of(rid), "pending")
        code, body, _ = self.act(rid, "approve")
        self.assertEqual((code, body["request"]["state"]), (200, "queued"), body)
        self.assertEqual(self.state_of(rid), "queued")

    def test_deny_needs_a_reason_and_shows_it_publicly(self):
        self.start()
        rid = self.ask()[1]["request"]["id"]
        self.assertEqual(self.act(rid, "deny", {})[0], 400)
        self.assertEqual(self.act(rid, "deny", {"reason": "Too heavy for the server."})[0], 200)
        _, body, _ = self.call("GET", self.public + "/api/requests")
        self.assertEqual((body["requests"][0]["state"], body["requests"][0]["reason"]), ("denied", "Too heavy for the server."))

    def test_override_changes_the_version_shown_publicly(self):
        self.start()
        versions = recorded_json("modrinth/versions/Vg5TIO6d.json")
        older = next(v for v in versions if v["id"] != CONNECTED[1])
        self.http.jars[next(f["url"] for f in older["files"] if f.get("primary"))] = build_jar(toml("create-connected"))
        rid = self.ask()[1]["request"]["id"]
        code, body, _ = self.act(rid, "override", {"version": older["id"]})
        self.assertEqual(code, 200, body)
        _, listing, _ = self.call("GET", self.public + "/api/requests")
        self.assertEqual(listing["requests"][0]["mod"]["versionNumber"], older["version_number"])
        self.assertEqual(listing["requests"][0]["state"], "pending")

    def test_remove_takes_an_approved_request_out_of_the_queue(self):
        self.start()
        rid = self.ask()[1]["request"]["id"]
        self.act(rid, "approve")
        code, body, _ = self.act(rid, "remove")
        self.assertEqual((code, body["request"]["state"]), (200, "denied"))
        self.assertIn("Removed from the queue", body["request"]["reason"])

    def test_the_owner_can_add_a_mod_directly_and_it_is_queued(self):
        self.start()
        code, body, _ = self.call("POST", self.public + "/admin/api/requests",
                                  {"source": "modrinth", "id": PONDER[0], "version": PONDER[1]}, self.owner())
        self.assertEqual((code, body["request"]["state"], body["request"]["requester"]), (201, "queued", "Owner"), body)

    def test_a_request_the_pack_has_outgrown_stays_approved_with_a_note(self):
        self.start()
        rid = self.ask()[1]["request"]["id"]
        write_installed(self.mods, [("create_connected-1.3.3.jar", build_jar(toml("create-connected")))])
        code, body, _ = self.act(rid, "approve")
        self.assertEqual((code, body["request"]["state"]), (200, "approved"))
        self.assertIn("already in the pack", body["request"]["note"])
        # It is not public: only the owner sees the note.
        _, listing, _ = self.call("GET", self.public + "/api/requests")
        self.assertNotIn("note", listing["requests"][0])

    def test_the_states_only_move_forward(self):
        self.start()
        rid = self.ask()[1]["request"]["id"]
        self.assertEqual(self.act(rid, "remove")[0], 409)      # pending cannot be "removed from the queue"
        self.assertEqual(self.act(rid, "requeue")[0], 409)
        self.act(rid, "approve")
        self.assertEqual(self.act(rid, "approve")[0], 409)     # already queued
        self.act(rid, "deny", {"reason": "no"})
        self.assertEqual(self.act(rid, "approve")[0], 409)
        self.assertEqual(self.act(rid, "override", {"version": "abc"})[0], 409)
        self.assertEqual(self.act(99, "approve")[0], 404)


class AdminAccessTest(RequestsCase):
    def test_admin_without_the_password_is_401_and_with_it_is_the_page(self):
        self.start()
        for method, path in (("GET", "/admin"), ("GET", "/admin/api/requests"), ("POST", "/admin/api/requests/1/approve")):
            code, _, headers = self.call(method, self.public + path, {} if method == "POST" else None)
            self.assertEqual(code, 401, path)
            self.assertIn("Basic", headers["WWW-Authenticate"])
        self.assertEqual(self.call("GET", self.public + "/admin", headers=self.owner("wrong password, long enough!!"))[0], 401)
        code, page, _ = self.call("GET", self.public + "/admin", headers=self.owner())
        self.assertEqual(code, 200)
        self.assertIn("Tel Aviv", page)

    def test_a_wrong_password_guessed_too_often_locks_the_door_for_a_minute(self):
        self.start()
        for _ in range(10):
            self.call("GET", self.public + "/admin", headers=self.owner("guess guess guess guess guess"))
        self.assertEqual(self.call("GET", self.public + "/admin", headers=self.owner())[0], 429)

    def test_without_a_long_enough_password_configured_admin_is_shut_not_open(self):
        for password in (None, "short"):
            self.start(password=password)
            self.assertEqual(self.call("GET", self.public + "/admin")[0], 404)
            self.assertEqual(self.call("GET", self.public + "/admin", headers=self.owner("short"))[0], 404)

    def test_the_local_admin_api_is_not_reachable_through_the_public_listener(self):
        self.start()
        self.ask()
        for method, path in (("GET", "/admin-api/v1/requests"), ("GET", "/admin-api/v1/health"),
                             ("POST", "/admin-api/v1/requests/1/approve")):
            code, _, _ = self.call(method, self.public + path, {} if method == "POST" else None, self.owner())
            self.assertIn(code, (404, 405), path)
        self.assertEqual(self.state_of(1), "pending")

    def test_the_local_admin_api_lists_changes_since_a_poll_and_acts_without_a_password(self):
        self.start()
        first = self.ask()[1]["request"]["id"]
        code, body, _ = self.call("GET", self.local + "/admin-api/v1/requests?since=0")
        self.assertEqual(code, 200)
        self.assertEqual([r["id"] for r in body["requests"]], [first])
        seen = body["seq"]
        self.assertEqual(self.call("GET", self.local + f"/admin-api/v1/requests?since={seen}")[1]["requests"], [])
        second = self.ask(ids=PONDER)[1]["request"]["id"]
        self.act(first, "approve", base="local")
        _, changed, _ = self.call("GET", self.local + f"/admin-api/v1/requests?since={seen}")
        self.assertEqual(sorted(r["id"] for r in changed["requests"]), [first, second])
        self.assertEqual(self.state_of(first), "queued")

    def test_the_queue_page_is_public(self):
        self.start()
        code, page, _ = self.call("GET", self.public + "/queue")
        self.assertEqual(code, 200)
        self.assertIn("Mod requests", page)

    def test_a_server_restart_keeps_the_queue(self):
        self.start()
        rid = self.ask()[1]["request"]["id"]
        self.act(rid, "approve")
        reopened = Queue(self.dir / "requests.db")
        self.addCleanup(reopened._db.close)
        self.assertEqual([(r.id, r.state) for r in reopened.all()], [(rid, "queued")])


if __name__ == "__main__":
    unittest.main()
