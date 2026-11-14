"""The Prism instance, the public pack files and "did the pack change", at the HTTP API."""

from __future__ import annotations

import io
import json
import tempfile
import time
import tomllib
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from mod_browser.pack import PackCatalog
from mod_browser.prism import PackFiles
from mod_browser.status import StatusMonitor
from mod_browser.curseforge import CurseForgeCatalog
from mod_browser.web import App, Servers, Setup
from tests.fakes import LATER_MOD, FakeServer, add_mod, make_pack

BOOTSTRAP = b"PK-not-really-a-jar-but-bytes-we-can-compare"
PACK_URL = "https://mods.example.com/pack/pack.toml"


class SetupCase(unittest.TestCase):
    def start(self, jar: bool = True, later_mod: bool = False, blocked_files=None) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.pack = make_pack(self.dir / "pack")
        (self.pack / "secret.txt").write_text("not in the index")
        (self.pack / "config").mkdir()
        (self.pack / "config" / "some file.toml").write_text("a = 1\n")
        if later_mod:
            add_mod(self.pack, "mods/straw-statues.pw.toml", LATER_MOD, "Add Straw Statues\n\nRequested-by: Fake_Friend\n",
                    "2026-11-13T09:00:00-05:00")
        index = self.pack / "index.toml"
        index.write_text(index.read_text() + '\n[[files]]\nfile = "config/some file.toml"\nhash = "0"\n')
        jar_path = self.dir / "bootstrap.jar"
        if jar:
            jar_path.write_bytes(BOOTSTRAP)
        monitor = StatusMonitor(FakeServer(), address="play.example.com", max_age=0)
        self.app = App(monitor, PackCatalog(self.pack, max_age=0),
                       setup=Setup(files=PackFiles(self.pack), pack_url=PACK_URL, bootstrap_jar=jar_path, min_mb=2048, max_mb=6144,
                                   blocked_files=blocked_files))
        self.servers = Servers(self.app, public=("127.0.0.1", 0), admin=("127.0.0.1", 0)).start()
        self.addCleanup(self.servers.stop)
        self.base = f"http://127.0.0.1:{self.servers.public.server_address[1]}"

    def get(self, path: str):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def json(self, path: str) -> dict:
        code, _, body = self.get(path)
        self.assertEqual(code, 200, body)
        return json.loads(body)


class InstanceZipTest(SetupCase):
    def test_the_zip_has_the_neoforge_component_memory_and_a_prelaunch_that_runs_the_bootstrap_against_the_public_pack(self):
        self.start()
        code, headers, body = self.get("/prism/instance.zip")
        self.assertEqual((code, headers["Content-Type"]), (200, "application/zip"))
        self.assertIn('filename="Tel-Aviv.zip"', headers["Content-Disposition"])
        zf = zipfile.ZipFile(io.BytesIO(body))
        self.assertEqual(sorted(zf.namelist()), [".minecraft/packwiz-installer-bootstrap.jar", "instance.cfg", "mmc-pack.json"])
        self.assertEqual(zf.read(".minecraft/packwiz-installer-bootstrap.jar"), BOOTSTRAP)
        components = {c["uid"]: c["version"] for c in json.loads(zf.read("mmc-pack.json"))["components"]}
        self.assertEqual(components, {"net.minecraft": "1.21.1", "net.neoforged": "21.1.264"})
        cfg = zf.read("instance.cfg").decode()
        self.assertIn("OverrideMemory=true", cfg)
        self.assertIn("MaxMemAlloc=6144", cfg)
        self.assertIn("MinMemAlloc=2048", cfg)
        self.assertIn(r'PreLaunchCommand="\"$INST_JAVA\" -jar packwiz-installer-bootstrap.jar -s client ' + PACK_URL + '"', cfg)
        self.assertIn("OverrideCommands=true", cfg)
        self.assertIn("name=Tel Aviv", cfg)

    def test_the_zip_is_byte_identical_between_downloads(self):
        self.start()
        self.assertEqual(self.get("/prism/instance.zip")[2], self.get("/prism/instance.zip")[2])

    def test_without_the_bootstrap_jar_there_is_no_zip_and_the_page_says_so(self):
        self.start(jar=False)
        self.assertEqual(self.get("/prism/instance.zip")[0], 404)
        self.assertFalse(self.json("/api/setup")["instance"]["available"])

    def test_the_setup_page_shows_the_server_address_and_is_linked_from_the_front_page(self):
        self.start()
        code, headers, body = self.get("/setup")
        self.assertEqual(code, 200)
        self.assertIn(b"play.example.com", body)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertIn(b'href="/setup"', self.get("/")[2])


class PublicPackTest(SetupCase):
    def test_pack_toml_the_index_and_the_files_it_lists_are_public(self):
        self.start()
        for path in ("/pack/pack.toml", "/pack/index.toml", "/pack/mods/create-connected.pw.toml"):
            code, _, body = self.get(path)
            self.assertEqual(code, 200, path)
        self.assertEqual(tomllib.loads(self.get("/pack/pack.toml")[2].decode())["versions"]["neoforge"], "21.1.264")
        self.assertEqual(self.get("/pack/config/some%20file.toml")[2], b"a = 1\n")

    def test_nothing_else_in_the_pack_directory_is_served(self):
        self.start()
        for path in ("/pack/secret.txt", "/pack/NOTES.md", "/pack/.git/config", "/pack/../pack.toml",
                     "/pack/mods/../NOTES.md", "/pack/%2e%2e/NOTES.md", "/pack/mods%5C..%5CNOTES.md", "/pack/", "/pack/mods/nope.pw.toml"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_pack_and_index_are_never_cached_but_metafiles_may_be_for_a_minute(self):
        self.start()
        self.assertEqual(self.get("/pack/pack.toml")[1]["Cache-Control"], "no-cache")
        self.assertIn("max-age=60", self.get("/pack/mods/create-connected.pw.toml")[1]["Cache-Control"])


class ChangesTest(SetupCase):
    def test_a_pack_changed_since_a_date_lists_what_was_added_by_whom(self):
        self.start(later_mod=True)
        r = self.json("/api/pack/changes?since=2026-11-12")
        self.assertTrue(r["changed"])
        self.assertEqual(r["latest"], "2026-11-13")
        self.assertIn({"name": "Straw Statues", "on": "2026-11-13", "by": "Fake_Friend"}, r["added"])

    def test_nothing_added_since_a_later_date_is_not_a_change(self):
        self.start(later_mod=True)
        r = self.json("/api/pack/changes?since=2026-11-14")
        self.assertEqual((r["changed"], r["added"]), (False, []))

    def test_a_bad_date_is_refused(self):
        self.start()
        self.assertEqual(self.get("/api/pack/changes?since=yesterday")[0], 400)
        self.assertEqual(self.get("/api/pack/changes")[0], 400)


class ManualDownloadTest(SetupCase):
    def test_base_pack_mods_whose_authors_block_outside_downloads_are_listed_for_download_by_hand(self):
        self.start()
        manual = self.json("/api/setup")["manual"]
        self.assertEqual([m["name"] for m in manual], ["Create: Connected"])
        self.assertEqual(manual[0]["filename"], "create_connected-1.3.3-mc1.21.1.jar")
        self.assertTrue(manual[0]["url"].startswith("https://www.curseforge.com/"))

    def manual_names(self, want: int | None = None) -> list[str]:
        """The /setup list, waiting briefly for the background CurseForge check to land."""
        deadline = time.monotonic() + 5
        while True:
            names = [m["name"] for m in self.json("/api/setup")["manual"]]
            if want is None or len(names) >= want or time.monotonic() > deadline:
                return names
            time.sleep(0.05)

    def test_curseforge_files_it_hands_out_no_url_for_are_listed_even_when_the_notes_do_not_name_them(self):
        # The live pack's NOTES.md names its 13 blocked mods in prose, with no metafile paths,
        # so the page listed none (2026-11-14); CurseForge itself says which ones are blocked.
        # The real adapter runs, over a fake CurseForge that has no URL for the shader pack's file.
        posts = []
        class FakeCurseForge:
            def post_json(self, url, body, headers=None):
                posts.append((url, body["fileIds"]))
                return {"data": [{"id": f, "downloadUrl": None if f == 1 else f"https://edge.forgecdn.net/{f}.jar"}
                                 for f in body["fileIds"]]}
        self.start(blocked_files=CurseForgeCatalog(FakeCurseForge(), "key").blocked_files)
        self.assertEqual(self.manual_names(want=2), ["Complementary Shaders - Reimagined", "Create: Connected"])
        self.json("/api/setup")
        self.assertEqual(len(posts), 1, "asked once per pack commit, not per page view")
        url, ids = posts[0]
        self.assertEqual(url, "https://api.curseforge.com/v1/mods/files")
        self.assertIn(1, ids)
        self.assertTrue(all(isinstance(i, int) for i in ids))

    def test_when_curseforge_does_not_answer_the_notes_list_stands_and_it_is_asked_again_later(self):
        calls = []
        def down(ids):
            calls.append(1)
            raise OSError("api.curseforge.com: timed out")
        self.start(blocked_files=down)
        self.assertEqual(self.manual_names(), ["Create: Connected"])
        deadline = time.monotonic() + 5
        while not calls and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.manual_names(), ["Create: Connected"])
        self.assertEqual(len(calls), 1, "a failure waits BLOCKED_RETRY before the next try")

    def test_the_server_only_mods_are_marked_server_so_the_client_side_installer_skips_them(self):
        self.start()
        _, _, body = self.get("/pack/mods/create-collision-crashfix.pw.toml")
        self.assertEqual(tomllib.loads(body.decode())["side"], "server")  # `-s client` leaves it out


if __name__ == "__main__":
    unittest.main()
