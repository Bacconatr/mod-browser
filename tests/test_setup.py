"""The Prism instance, the public pack files and "did the pack change", at the HTTP API."""

from __future__ import annotations

import io
import json
import tempfile
import tomllib
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from mod_browser.pack import PackCatalog
from mod_browser.prism import PackFiles
from mod_browser.status import StatusMonitor
from mod_browser.web import App, Servers, Setup
from tests.fakes import LATER_MOD, FakeServer, _git, _write_index, add_mod, make_pack

BOOTSTRAP = b"PK-not-really-a-jar-but-bytes-we-can-compare"
PACK_URL = "https://mods.example.com/pack/pack.toml"


class SetupCase(unittest.TestCase):
    def start(self, jar: bool = True, later_mod: bool = False) -> None:
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
                       setup=Setup(files=PackFiles(self.pack), pack_url=PACK_URL, bootstrap_jar=jar_path, min_mb=2048, max_mb=6144))
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

    def upstream_update(self) -> None:
        """An upstream bump like v3.0HF's: one mod updated, one removed, the pack's version moved."""
        connected = self.pack / "mods/create-connected.pw.toml"
        connected.write_text(connected.read_text().replace("create_connected-1.3.3-mc1.21.1.jar", "create_connected-1.4.0-mc1.21.1.jar"))
        (self.pack / "mods/create-deep-seas.pw.toml").unlink()
        toml = self.pack / "pack.toml"
        toml.write_text(toml.read_text().replace('version = "2.9-1"', 'version = "3.0HF"'))
        _write_index(self.pack)
        index = self.pack / "index.toml"
        index.write_text(index.read_text() + '\n[[files]]\nfile = "config/some file.toml"\nhash = "0"\n')
        _git(self.pack, "add", "-A")
        _git(self.pack, "commit", "-q", "-m", "Update to Upstream update: v3.0HF", date="2026-11-14T01:02:02-05:00")

    def test_an_upstream_update_lists_updated_and_removed_mods_and_the_new_pack_version(self):
        self.start(later_mod=True)
        self.upstream_update()
        r = self.json("/api/pack/changes?since=2026-11-14")
        self.assertTrue(r["changed"])
        self.assertEqual(r["updated"], [{"name": "Create: Connected", "from": "1.3.3", "to": "1.4.0"}])
        self.assertEqual(r["removed"], [{"name": "Create Deep Seas [BETA]"}])
        self.assertEqual(r["added"], [])  # Straw Statues came the day before
        self.assertEqual(r["pack"], {"version": {"from": "2.9-1", "to": "3.0HF"}})
        self.assertEqual(r["counts"], {"added": 0, "updated": 1, "removed": 1})
        self.assertEqual([e["title"] for e in r["events"]], ["Upstream update: v3.0HF"])

    def test_changes_on_the_day_they_last_played_count(self):
        self.start(later_mod=True)
        self.upstream_update()
        r = self.json("/api/pack/changes?since=2026-11-13")
        self.assertEqual([m["name"] for m in r["added"]], ["Straw Statues"])
        self.assertEqual(r["counts"], {"added": 1, "updated": 1, "removed": 1})
        self.assertEqual(len(r["events"]), 2)

    def test_since_before_the_pack_existed_never_lists_the_base_pack_as_added(self):
        self.start(later_mod=True)
        r = self.json("/api/pack/changes?since=2026-01-01")
        self.assertEqual([m["name"] for m in r["added"]], ["Straw Statues"])
        self.assertEqual((r["updated"], r["removed"]), ([], []))

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

    def test_the_server_only_mods_are_marked_server_so_the_client_side_installer_skips_them(self):
        self.start()
        _, _, body = self.get("/pack/mods/create-collision-crashfix.pw.toml")
        self.assertEqual(tomllib.loads(body.decode())["side"], "server")  # `-s client` leaves it out


if __name__ == "__main__":
    unittest.main()
