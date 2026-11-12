"""Search, version picker and resolver verdicts, as a caller of the HTTP API sees them.

The real Modrinth and CurseForge adapters, jar reader and installed-mods
reader run over recorded upstream answers and a temporary mods directory;
only the network is fake. See `browse_fakes.py` for what is real and what is
hand-written or SYNTHETIC.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from mod_browser.curseforge import CurseForgeCatalog
from mod_browser.modrinth import ModrinthCatalog
from mod_browser.pack import PackCatalog
from mod_browser.sources import CompositeCatalog, DirInstalled, HttpJars
from mod_browser.status import StatusMonitor
from mod_browser.web import App, Browse, Servers
from tests.browse_fakes import (BASE_INSTALLED, CF_FS_158, RecordedHttp, build_jar, modrinth_url, mods_toml,
                                recorded_json, toml, write_installed)
from tests.fakes import FakeServer, make_pack

RIVAL_JAR = "https://cdn.modrinth.com/data/RIVAL001/versions/RIVALV01/rival-1.0.0.jar"


def puzzles_url() -> str:
    newest = recorded_json("modrinth/versions/QAGBst4M.json")[0]
    return next(f["url"] for f in newest["files"] if f.get("primary"))


def puzzles_version() -> str:
    return recorded_json("modrinth/versions/QAGBst4M.json")[0]["version_number"]


class BrowseCase(unittest.TestCase):
    def start(self, installed=(), jars=None, curseforge=True, curseforge_search=True) -> None:
        self.http = RecordedHttp()
        for url, data in (jars or {}).items():
            self.http.jars[url] = data
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mods = Path(tmp.name) / "mods"
        mods.mkdir()
        write_installed(mods, list(installed))
        catalog = CompositeCatalog(ModrinthCatalog(self.http),
                                   CurseForgeCatalog(self.http, "test-key") if curseforge else None,
                                   curseforge_search=curseforge_search)
        pack = make_pack(Path(tmp.name) / "pack")
        monitor = StatusMonitor(FakeServer(), address="play.example.com", max_age=0)
        app = App(monitor, PackCatalog(pack, max_age=0),
                  browse=Browse(catalog=catalog, jars=HttpJars(self.http), installed=DirInstalled(mods)))
        self.servers = Servers(app, public=("127.0.0.1", 0), admin=("127.0.0.1", 0)).start()
        self.addCleanup(self.servers.stop)
        self.base = f"http://127.0.0.1:{self.servers.public.server_address[1]}"

    def get(self, path: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def raw(self, path: str) -> tuple[int, dict, str]:
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as response:
                return response.status, dict(response.headers), response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read().decode()


class SearchTest(BrowseCase):
    def test_shows_modrinth_and_curseforge_results_with_icon_description_downloads_and_update(self):
        self.start()
        code, r = self.get("/api/catalog/search?q=create%20connected")
        self.assertEqual(code, 200)
        modrinth = next(p for p in r["results"] if p["source"] == "modrinth" and p["slug"] == "create-connected")
        self.assertTrue(modrinth["iconUrl"].startswith("https://cdn.modrinth.com/"))
        self.assertGreater(len(modrinth["description"]), 10)
        self.assertGreater(modrinth["downloads"], 1_000_000)
        self.assertTrue(modrinth["updatedAt"].startswith("20"))
        self.assertTrue(any(p["source"] == "curseforge" and p["name"] == "Create: Connected" for p in r["results"]))
        self.assertEqual(r["warnings"], [])

    def test_create_loconautics_is_found_on_curseforge_only(self):
        self.start()
        _, r = self.get("/api/catalog/search?q=loconautics")
        self.assertEqual([(p["source"], p["name"]) for p in r["results"]], [("curseforge", "Create: Loconautics")])

    def test_still_answers_with_a_warning_when_curseforge_is_not_configured(self):
        self.start(curseforge=False)
        _, r = self.get("/api/catalog/search?q=create%20connected")
        self.assertTrue(r["results"] and all(p["source"] == "modrinth" for p in r["results"]))
        self.assertIn("CurseForge is not configured", r["warnings"][0])

    def test_a_catalog_that_is_down_is_a_warning_not_an_outage(self):
        self.start()
        real = self.http.json
        self.http.json = lambda url, headers=None: (_ for _ in ()).throw(RuntimeError("down")) if "curseforge" in url else real(url, headers)
        _, r = self.get("/api/catalog/search?q=create%20connected")
        self.assertTrue(r["results"])
        self.assertEqual(r["warnings"], ["CurseForge could not be searched right now."])

    def test_asks_for_at_least_two_characters(self):
        self.start()
        self.assertEqual(self.get("/api/catalog/search?q=a")[0], 400)


class VersionPickerTest(BrowseCase):
    def test_defaults_to_the_newest_release(self):
        self.start()
        _, r = self.get("/api/catalog/versions?source=modrinth&id=Vg5TIO6d")
        self.assertEqual(r["versions"][0]["versionNumber"], "1.3.3-mc1.21.1")
        self.assertEqual(r["default"], {"id": r["versions"][0]["id"], "fallback": None})

    def test_falls_back_to_a_labelled_beta_when_there_is_no_release(self):
        self.start()
        _, r = self.get("/api/catalog/versions?source=modrinth&id=cO40ZIg3")  # Functional Storage: all betas on Modrinth
        self.assertEqual(r["default"]["fallback"], "beta")
        self.assertEqual(next(v for v in r["versions"] if v["id"] == r["default"]["id"])["channel"], "beta")

    def test_rejects_a_malformed_id_or_source(self):
        self.start()
        self.assertEqual(self.get("/api/catalog/versions?source=modrinth&id=..%2Fetc")[0], 400)
        self.assertEqual(self.get("/api/catalog/versions?source=evil&id=abc")[0], 400)


class ResolveTest(BrowseCase):
    def test_create_connected_is_already_installed(self):
        self.start(installed=[*BASE_INSTALLED, ("create_connected-1.3.3.jar", build_jar(toml("create-connected")))],
                   jars={modrinth_url("Vg5TIO6d", "Xe7EqzfQ"): build_jar(toml("create-connected"))})
        code, r = self.get("/api/catalog/resolve?source=modrinth&id=Vg5TIO6d")
        self.assertEqual(code, 200)
        self.assertEqual(r["status"], "already-installed")
        self.assertEqual(r["installedAs"], {"modId": "create_connected", "version": "1.3.3-mc1.21.1"})

    def test_ponder_for_kaleidoscope_cookery_is_client_only(self):
        self.start(installed=BASE_INSTALLED,
                   jars={modrinth_url("I5JahfLa", "grCjEcqZ"): build_jar(toml("ponder-for-kaleidoscope-cookery"))})
        _, r = self.get("/api/catalog/resolve?source=modrinth&id=I5JahfLa")
        self.assertEqual(r["status"], "ok", r["problems"])
        self.assertEqual(r["side"], "client")

    def test_a_curseforge_file_whose_author_blocks_outside_downloads_is_unavailable(self):
        self.start(installed=BASE_INSTALLED)
        _, r = self.get("/api/catalog/resolve?source=curseforge&id=1793084")
        self.assertEqual(r["status"], "unavailable")
        self.assertEqual(r["problems"][0]["kind"], "blocked-download")
        self.assertIn("author blocks downloads outside CurseForge", r["problems"][0]["message"])
        self.assertEqual(r["added"], [])

    def test_unknown_version_is_404_and_a_catalog_that_does_not_answer_is_502(self):
        self.start(installed=BASE_INSTALLED)
        self.assertEqual(self.get("/api/catalog/resolve?source=modrinth&id=Vg5TIO6d&version=NOPE")[0], 404)
        self.assertEqual(self.get("/api/catalog/resolve?source=modrinth&id=MISSING1")[0], 502)


class DependencyTest(BrowseCase):
    AERO = ("DvdmuWPu", "3x3kSOMY")  # Functional Storage: Create & Aero

    def aero_jar(self) -> dict[str, bytes]:
        return {modrinth_url(*self.AERO): build_jar(toml("functional-storage-create-aero"))}

    def test_picks_curseforges_functional_storage_1_5_8_over_modrinths_1_5_7(self):
        self.start(installed=BASE_INSTALLED, jars={
            **self.aero_jar(),
            CF_FS_158: build_jar(toml("functional-storage-1.5.8"), "1.21.1-1.5.8"),
            modrinth_url("cO40ZIg3", "FWnouoF2"): build_jar(toml("functional-storage-1.5.7"), "1.21-1.5.7"),
        })
        _, r = self.get(f"/api/catalog/resolve?source=modrinth&id={self.AERO[0]}")
        self.assertEqual(r["status"], "ok", r["problems"])
        self.assertEqual([(a["name"], a["source"], a["versionNumber"]) for a in r["added"]], [
            ("Functional Storage: Create & Aero", "modrinth", "1.21-1.0.0"),
            ("Functional Storage", "curseforge", "1.21.1-1.5.8"),
        ])
        self.assertEqual(r["added"][1]["reason"], "required by Functional Storage: Create & Aero")
        self.assertEqual((r["version"]["channel"], r["version"]["fallback"]), ("beta", "beta"))

    def test_does_not_add_what_the_pack_already_has_at_a_version_that_fits(self):
        have = ("functionalstorage-1.21.1-1.5.8.jar", build_jar(toml("functional-storage-1.5.8"), "1.21.1-1.5.8"))
        self.start(installed=[*BASE_INSTALLED, have], jars=self.aero_jar())
        _, r = self.get(f"/api/catalog/resolve?source=modrinth&id={self.AERO[0]}")
        self.assertEqual(r["status"], "ok")
        self.assertEqual([a["name"] for a in r["added"]], ["Functional Storage: Create & Aero"])

    def test_refuses_with_the_range_when_the_packs_copy_is_too_old_and_nothing_newer_fits(self):
        old = ("functionalstorage-old.jar", build_jar(toml("functional-storage-1.5.7"), "1.21-1.5.7"))
        self.start(installed=[*BASE_INSTALLED, old], curseforge=False, jars=self.aero_jar())
        _, r = self.get(f"/api/catalog/resolve?source=modrinth&id={self.AERO[0]}")
        self.assertEqual(r["status"], "refused")
        self.assertEqual(r["problems"][0]["kind"], "version-range")
        self.assertIn("[1.21.1-1.5.8,1.21.2)", r["problems"][0]["message"])
        self.assertIn("1.21-1.5.7", r["problems"][0]["message"])

    def test_does_not_ask_for_statue_menus_separately_because_straw_statues_bundles_it(self):
        statue_menus = build_jar(toml("statuemenus"), "21.1.0")
        straw = build_jar(toml("straw-statues"), "21.1.0", nested={"statuemenus-neoforge-21.1.0.jar": statue_menus})
        self.start(installed=BASE_INSTALLED, jars={
            modrinth_url("2fltysAl", "C7j9WWVp"): straw,
            # Its other requirement, Puzzles Lib, is a different mod.
            puzzles_url(): build_jar(mods_toml("puzzleslib", puzzles_version(), [{"modId": "neoforge"}])),
        })
        _, r = self.get("/api/catalog/resolve?source=modrinth&id=2fltysAl")
        self.assertEqual(r["status"], "ok", r["problems"])
        names = [a["name"] for a in r["added"]]
        self.assertFalse(any("statue menus" in n.lower() for n in names), names)
        self.assertEqual(names, ["Straw Statues", "Puzzles Lib"])

    def test_still_checks_a_second_stricter_range_for_a_mod_another_dependency_already_asked_for(self):
        # SYNTHETIC: Rival Mod needs helper (any) and puzzleslib; puzzleslib needs helper >= 9.
        # The pack has helper 1.0, which satisfies the first ask and not the second.
        self.start(
            installed=[*BASE_INSTALLED, ("helper-1.0.jar", build_jar(mods_toml("helper", "1.0")))],
            jars={
                RIVAL_JAR: build_jar(mods_toml("rivalmod", "1.0.0", [{"modId": "helper", "range": "*"}, {"modId": "puzzleslib"}])),
                puzzles_url(): build_jar(mods_toml("puzzleslib", puzzles_version(), [{"modId": "helper", "range": "[9,)"}])),
            })
        _, r = self.get("/api/catalog/resolve?source=modrinth&id=RIVAL001")
        self.assertEqual(r["status"], "refused", r)
        self.assertTrue(any(p["kind"] == "version-range" and "helper [9,)" in p["message"] and "1.0" in p["message"]
                            for p in r["problems"]), r["problems"])

    def test_gives_up_on_a_mod_with_dozens_of_unknown_requirements_after_a_bounded_number_of_lookups(self):
        deps = [{"modId": f"unknown{i}", "range": "*"} for i in range(60)]
        # SYNTHETIC, and every requirement listed twice.
        self.start(installed=BASE_INSTALLED, jars={RIVAL_JAR: build_jar(mods_toml("rivalmod", "1.0.0", deps + deps))})
        _, r = self.get("/api/catalog/resolve?source=modrinth&id=RIVAL001")
        self.assertEqual(r["status"], "refused")
        self.assertTrue(any("too many" in p["message"].lower() for p in r["problems"]), r["problems"])
        self.assertLess(len(self.http.requests), 120, len(self.http.requests))


class IncompatibilityTest(BrowseCase):
    def test_refuses_a_mod_that_declares_itself_incompatible_with_an_installed_one_with_the_reason(self):
        # SYNTHETIC: Rival Mod is not a real mod; its toml declares Create incompatible.
        self.start(installed=BASE_INSTALLED, jars={RIVAL_JAR: build_jar(mods_toml(
            "rivalmod", "1.0.0", [{"modId": "create", "type": "incompatible", "range": "[6.0.0,)"}]))})
        _, r = self.get("/api/catalog/resolve?source=modrinth&id=RIVAL001")
        self.assertEqual(r["status"], "refused")
        self.assertEqual([p["kind"] for p in r["problems"]], ["incompatible"])
        self.assertIn("Rival Mod declares itself incompatible with create [6.0.0,), which the pack has (6.0.10)",
                      r["problems"][0]["message"])

    def test_refuses_when_an_installed_mod_declares_itself_incompatible_with_the_candidate(self):
        grumpy = ("grumpy-1.0.jar", build_jar(mods_toml("grumpy", "1.0", [{"modId": "rivalmod", "type": "incompatible"}])))
        self.start(installed=[*BASE_INSTALLED, grumpy], jars={RIVAL_JAR: build_jar(mods_toml("rivalmod", "1.0.0"))})
        _, r = self.get("/api/catalog/resolve?source=modrinth&id=RIVAL001")
        self.assertEqual(r["status"], "refused")
        self.assertIn("grumpy-1.0.jar (already in the pack) declares itself incompatible with Rival Mod",
                      r["problems"][0]["message"])


class CurseForgeSearchOffTest(BrowseCase):
    """The production setting while /v1/mods/search returns 403 for our key."""

    def test_never_calls_curseforge_search_and_searches_modrinth_only(self):
        self.start(curseforge_search=False)
        _, r = self.get("/api/catalog/search?q=create%20connected")
        self.assertTrue(r["results"] and all(p["source"] == "modrinth" for p in r["results"]))
        self.assertEqual(r["warnings"], [])
        self.assertEqual([u for u in self.http.requests if "curseforge.com" in u], [])

    def test_finds_create_loconautics_by_its_pasted_project_id(self):
        self.start(installed=BASE_INSTALLED, curseforge_search=False)
        for text in ("1793084", "curseforge:1793084"):
            _, r = self.get(f"/api/catalog/search?q={urllib.request.quote(text)}")
            self.assertEqual([(p["source"], p["name"]) for p in r["results"]], [("curseforge", "Create: Loconautics")])
        self.assertEqual(self.get("/api/catalog/resolve?source=curseforge&id=1793084")[1]["status"], "unavailable")
        self.assertEqual([u for u in self.http.requests if "/mods/search" in u], [])

    def test_asks_for_the_project_id_when_a_pasted_link_cannot_be_looked_up(self):
        self.start(curseforge_search=False)
        link = urllib.request.quote("https://www.curseforge.com/minecraft/mc-mods/create-loconautics")
        _, r = self.get(f"/api/catalog/search?q={link}")
        self.assertEqual(r["results"], [])
        self.assertIn("Paste the project ID", r["warnings"][0])
        self.assertEqual([u for u in self.http.requests if "curseforge.com" in u], [])

    def test_says_so_for_an_id_curseforge_does_not_have(self):
        self.start(curseforge_search=False)
        _, r = self.get("/api/catalog/search?q=99999999")
        self.assertEqual(r["results"], [])
        self.assertIn("no mod with ID 99999999", r["warnings"][0])


class PagesTest(BrowseCase):
    def test_the_find_page_is_served_with_the_strict_csp_and_the_index_links_to_it(self):
        self.start()
        code, headers, body = self.raw("/add")
        self.assertEqual(code, 200)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertIn("play.example.com", body)
        self.assertNotIn("<script>", body)
        self.assertIn('href="/add"', self.raw("/")[2])

    def test_without_browsing_there_is_no_catalog_at_all(self):
        self.start()
        self.servers.stop()
        monitor = StatusMonitor(FakeServer(), address="x", max_age=0)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = App(monitor, PackCatalog(make_pack(Path(tmp.name)), max_age=0))
        self.assertNotIn("/api/catalog/search", app.public_routes)
        self.assertNotIn("/add", app.public_routes)
        self.assertNotIn('href="/add"', app._page.decode())


if __name__ == "__main__":
    unittest.main()


class RedirectHeaderTest(unittest.TestCase):
    def test_catalog_key_is_removed_before_cross_origin_redirect(self):
        from io import BytesIO
        from mod_browser.net import FetchHttp

        seen = []

        class Response:
            def __init__(self, status, location=None):
                self.status, self.location, self.reads = status, location, 0
            def getheader(self, name):
                return self.location if name == "Location" else None
            def read(self, _size):
                self.reads += 1
                return b"{}" if self.status == 200 and self.reads == 1 else b""

        class Conn:
            def __init__(self, host):
                self.host = host
            def request(self, _method, _path, headers):
                seen.append((self.host, headers))
            def getresponse(self):
                return Response(302, "https://cdn.modrinth.com/data") if self.host == "api.curseforge.com" else Response(200)
            def close(self):
                pass

        http = FetchHttp()
        http._connect = lambda _scheme, host, _port, _timeout: Conn(host)
        self.assertEqual(http.json("https://api.curseforge.com/v1/mods/1", {"x-api-key": "fake-secret"}), {})
        self.assertEqual(seen[0][1]["x-api-key"], "fake-secret")
        self.assertNotIn("x-api-key", seen[1][1])
