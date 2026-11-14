"""Upstream updates know a project by its id and put it where its type belongs (Homelab #1789).

v3.0HF, applied as batch 1 on 2026-11-14, wrote 37 resource and data packs the
pack already kept in resourcepacks/ (or its root) into mods/ a second time, and
changed mods/boatload.pw.toml, the Boatload mod, into "Boatload ZH_HK
Translation" because the two files' names start alike. These go through the
same daily check, approval and maintenance runner as the real update did: the
check against the pack, then the applier writing the batch.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from datetime import datetime
from zoneinfo import ZoneInfo

from mod_browser.upstream import Release, UpstreamChecker, UpstreamMod, version_of
from tests.test_maintenance import MaintenanceCase

BOATLOAD = '''name = "Boatload"
filename = "boatload-1.21.1-6.0.2.jar"
side = "both"

[download]
hash-format = "sha1"
hash = "fffd4975bdf2b1525e93b94c823ba4ae1649fbf7"
mode = "metadata:curseforge"

[update]
[update.curseforge]
file-id = 7118750
project-id = 337396
'''

MOD, RESOURCE_PACK, SHADER, DATA_PACK = 6, 12, 6552, 6945


def up(project: str, file: str, name: str, filename: str, class_id: int | None) -> UpstreamMod:
    return UpstreamMod(project, file, name, filename, version_of(filename), "aa", "sha1", None, class_id)


#: What the pack already holds, as upstream lists it, unchanged: Create: Connected and Boatload (mods), and
#: Complementary (a shader pack, which this pack keeps in resourcepacks/).
KEPT = (up("947914", "8777573", "Create: Connected", "create_connected-1.3.3-mc1.21.1.jar", MOD),
        UpstreamMod("337396", "7118750", "Boatload", "boatload-1.21.1-6.0.2.jar", "1.21.1-6.0.2",
                    "fffd4975bdf2b1525e93b94c823ba4ae1649fbf7", "sha1", None, MOD),
        up("627557", "1", "Complementary Shaders - Reimagined", "ComplementaryReimagined_r5.5.1.zip", SHADER))
TRANSLATION = up("1455793", "7586163", "Boatload ZH_HK Translation", "boatload_1.21_ZH_HK_Translation.zip", RESOURCE_PACK)


class UpstreamFolderCase(MaintenanceCase):
    def setUp(self) -> None:
        super().setUp()
        pack = self.dir / "pack"
        (pack / "mods" / "boatload.pw.toml").write_text(BOATLOAD)
        self.git("add", "-A")
        subprocess.run(["git", "-C", str(pack), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--amend",
                        "--no-edit"], check=True)                      # part of the migrated base pack

    def check(self, *mods: UpstreamMod) -> dict:
        release = Release("9400000", "All of Create - Aeronautics v3.0HF", "3.0HF", "2026-11-14T00:00:00Z", KEPT + mods)
        checker = UpstreamChecker(self.queue, type("S", (), {"latest": lambda _self: release})(), self.app.catalog,
                                  now=lambda: datetime(2026, 11, 14, 4, 5, tzinfo=ZoneInfo("America/New_York")))
        self.assertEqual(checker.check()["lastResult"], "new-request")
        (request,) = self.queue.of_kind("upstream")
        return request.admin()

    def file_by_hand(self, ops: list[dict]) -> int:
        """An upstream request with the operations an older build computed (v3.0HF's, from its request)."""
        commit = self.app.catalog.catalog()["pack"]["commit"]
        return self.queue.create_upstream("1518930", "9400000", "Upstream update: v3.0HF", "3.0HF", ops,
                                          {"baseCommit": commit, "diff": {}}).id

    def apply(self, request_id: int) -> str:
        self.assertEqual(self.act(request_id, "approve")[1]["request"]["state"], "queued")
        self.assertEqual(self.owner_asks()[0], 200)
        return self.runner.tick()

    def pack(self, path: str) -> dict:
        return tomllib.loads((self.dir / "pack" / path).read_text())

    def indexed(self) -> list[str]:
        return [f["file"] for f in self.pack("index.toml")["files"]]

    def projects(self) -> list[str]:
        """Every CurseForge project the index lists, once per metafile that names it."""
        out = []
        for path in self.indexed():
            cf = self.pack(path).get("update", {}).get("curseforge")
            if cf:
                out.append(str(cf["project-id"]))
        return out


class TypeFolderTest(UpstreamFolderCase):
    """Failure mode 1: a resource, shader or data pack written into mods/."""

    def test_a_new_resource_pack_goes_in_resourcepacks_and_mods_gets_nothing_but_jars(self):
        request = self.check(up("412189", "5", "GUI Retextures", "guiretextures-1.11.zip", RESOURCE_PACK),
                             up("4242", "11", "Straw Statues", "strawstatues-1.0.jar", MOD))
        folders = {o["name"]: o.get("folder") for o in request["added"] if o["op"] == "add"}
        self.assertEqual(folders, {"GUI Retextures": "resourcepacks", "Straw Statues": "mods"})
        self.assertEqual(self.apply(request["id"]), "applied")
        self.assertIn("resourcepacks/gui-retextures.pw.toml", self.indexed())
        self.assertIn("mods/straw-statues.pw.toml", self.indexed())
        self.assertEqual([f for f in self.indexed() if f.startswith("mods/") and not
                          self.pack(f)["filename"].endswith(".jar")], [])

    def test_a_type_goes_where_the_pack_already_keeps_that_type(self):
        # This pack keeps its one shader pack (Complementary) in resourcepacks/, not shaderpacks/.
        request = self.check(up("385587", "6", "Complementary Shaders - Unbound", "ComplementaryUnbound_r5.9.3.zip", SHADER))
        (op,) = [o for o in request["added"] if o["op"] == "add"]
        self.assertEqual(op["folder"], "resourcepacks")
        self.assertEqual(self.apply(request["id"]), "applied")
        self.assertIn("resourcepacks/complementary-shaders-unbound.pw.toml", self.indexed())

    def test_a_type_the_pack_keeps_nowhere_is_refused_with_the_pack_untouched(self):
        before = self.tree()
        request = self.check(up("979544", "8663871", "All of Create Data", "aoc_data_1.21.1_v5.zip", DATA_PACK))
        self.assertEqual(self.apply(request["id"]), "test-failed")
        self.assertIn("data pack the pack keeps nowhere", self.queue.get(request["id"]).reason)
        self.assertEqual((self.tree(), self.host.calls.count("stop")), (before, 0))

    def test_the_old_builds_zip_into_mods_is_refused(self):
        # v3.0HF's request, as the old build wrote it: an add with no folder, so mods/.
        before = self.tree()
        rid = self.file_by_hand([{"op": "add", "name": "GUI Retextures", "source": "curseforge", "projectId": "412189",
                                  "versionId": "5", "versionNumber": "1.11", "channel": "release",
                                  "fileName": "guiretextures-1.11.zip", "fileHash": "aa", "hashFormat": "sha1",
                                  "side": "both", "reason": "upstream"}])
        self.assertEqual(self.apply(rid), "test-failed")
        self.assertIn("not a mod jar", self.queue.get(rid).reason)
        self.assertEqual(self.tree(), before)


class DuplicateTest(UpstreamFolderCase):
    """Failure mode 2: a project the pack already keeps outside mods/ added again as a new mods/ metafile."""

    def test_a_project_kept_in_resourcepacks_is_matched_not_added_again(self):
        newer = up("627557", "2", "Complementary Shaders - Reimagined", "ComplementaryReimagined_r5.9.3.zip", SHADER)
        release_mods = tuple(m for m in KEPT if m.project_id != "627557") + (newer,)
        release = Release("9400000", "v3.0HF", "3.0HF", "2026-11-14T00:00:00Z", release_mods)
        checker = UpstreamChecker(self.queue, type("S", (), {"latest": lambda _self: release})(), self.app.catalog,
                                  now=lambda: datetime(2026, 11, 14, 4, 5, tzinfo=ZoneInfo("America/New_York")))
        checker.check()
        (request,) = [r.admin() for r in self.queue.of_kind("upstream")]
        self.assertEqual(request["meta"]["diff"]["added"], [])
        self.assertEqual([(o["op"], o["metafile"]) for o in request["added"]],
                         [("change", "resourcepacks/complementary-reimagined.pw.toml")])
        self.assertEqual(self.apply(request["id"]), "applied")
        self.assertEqual(self.pack("resourcepacks/complementary-reimagined.pw.toml")["filename"],
                         "ComplementaryReimagined_r5.9.3.zip")
        self.assertEqual(self.projects().count("627557"), 1)
        self.assertNotIn("mods/complementary-shaders-reimagined.pw.toml", self.indexed())

    def test_a_release_that_only_lists_what_the_pack_keeps_outside_mods_changes_nothing(self):
        release = Release("9400000", "v3.0HF", "3.0HF", "2026-11-14T00:00:00Z", KEPT)
        checker = UpstreamChecker(self.queue, type("S", (), {"latest": lambda _self: release})(), self.app.catalog,
                                  now=lambda: datetime(2026, 11, 14, 4, 5, tzinfo=ZoneInfo("America/New_York")))
        self.assertEqual(checker.check()["lastResult"], "nothing-to-change")

    def test_the_old_builds_second_copy_is_refused(self):
        # v3.0HF's add of Complementary, which the pack already tracks in resourcepacks/.
        before = self.tree()
        rid = self.file_by_hand([{"op": "add", "name": "Complementary Shaders - Reimagined", "source": "curseforge",
                                  "projectId": "627557", "versionId": "8884654", "versionNumber": "r5.9.3",
                                  "channel": "release", "fileName": "ComplementaryReimagined_r5.9.3.zip",
                                  "fileHash": "aa", "hashFormat": "sha1", "side": "both", "folder": "resourcepacks",
                                  "reason": "upstream"}])
        self.assertEqual(self.apply(rid), "test-failed")
        self.assertIn("already tracked by the pack as resourcepacks/complementary-reimagined.pw.toml",
                      self.queue.get(rid).reason)
        self.assertEqual(self.tree(), before)


class RepairTest(UpstreamFolderCase):
    """The pack as v3.0HF left it, and the next upstream update putting it right through the runner."""

    def setUp(self) -> None:
        super().setUp()
        pack = self.dir / "pack"
        complementary = (pack / "resourcepacks" / "complementary-reimagined.pw.toml").read_text()
        translation = BOATLOAD.replace('"Boatload"', '"Boatload ZH_HK Translation"').replace(
            "boatload-1.21.1-6.0.2.jar", "boatload_1.21_ZH_HK_Translation.zip").replace("7118750", "7586163").replace(
            "337396", "1455793")
        (pack / "resourcepacks" / "boatload-zh-hk-translation.pw.toml").write_text(translation)
        self.git("add", "-A")
        subprocess.run(["git", "-C", str(pack), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--amend",
                        "--no-edit"], check=True)                      # the base pack keeps the translation in resourcepacks/
        # Batch 1: a second Complementary in mods/, and Boatload's metafile turned into the translation.
        (pack / "mods" / "complementary-shaders-reimagined.pw.toml").write_text(complementary)
        (pack / "mods" / "boatload.pw.toml").write_text(translation)
        self.repo._refresh(pack)
        self.git("add", "-A")
        subprocess.run(["git", "-C", str(pack), "-c", "user.name=mod-browser", "-c", "user.email=m@b", "commit", "-q",
                        "-m", "Update to Upstream update: v3.0HF\n\nRequested-by: Upstream\n"], check=True)
        self.assertEqual(sorted(self.projects()).count("627557"), 2)

    def test_the_next_update_removes_the_second_copies_and_brings_boatload_back_under_its_own_name(self):
        release = Release("9611275", "All of Create Aeronautics - v3.1", "3.1", "2026-11-14T12:00:00Z", KEPT + (TRANSLATION,))
        checker = UpstreamChecker(self.queue, type("S", (), {"latest": lambda _self: release})(), self.app.catalog,
                                  now=lambda: datetime(2026, 11, 15, 4, 5, tzinfo=ZoneInfo("America/New_York")))
        checker.check()
        (request,) = [r.admin() for r in self.queue.of_kind("upstream")]
        self.assertEqual(sorted((o["op"], o.get("metafile") or o.get("folder")) for o in request["added"]),
                         [("add", "mods"), ("remove", "mods/boatload.pw.toml"),
                          ("remove", "mods/complementary-shaders-reimagined.pw.toml")])
        self.assertEqual(self.apply(request["id"]), "applied")      # with the usual backup and test boot
        self.assertEqual(self.host.calls.count("backup"), 1)
        self.assertEqual(self.pack("mods/boatload.pw.toml"), tomllib.loads(BOATLOAD))
        self.assertEqual(sorted(self.projects()), sorted(set(self.projects())))      # no project twice
        self.assertEqual([f for f in self.indexed() if f.startswith("mods/") and not
                          self.pack(f)["filename"].endswith(".jar")], [])


class SlugCollisionTest(UpstreamFolderCase):
    """Failure mode 3: a different project whose name collides takes over an existing metafile."""

    def test_a_translation_named_like_the_mod_is_added_beside_it_and_the_mod_is_left_alone(self):
        request = self.check(TRANSLATION)
        self.assertEqual(request["meta"]["diff"]["changed"], [])
        self.assertEqual([(o["op"], o["name"], o.get("folder")) for o in request["added"]],
                         [("add", "Boatload ZH_HK Translation", "resourcepacks")])
        self.assertEqual(self.apply(request["id"]), "applied")
        self.assertEqual(self.pack("mods/boatload.pw.toml"), tomllib.loads(BOATLOAD))
        self.assertEqual(self.pack("resourcepacks/boatload-zh-hk-translation.pw.toml")["update"]["curseforge"]["project-id"],
                         1455793)

    def test_the_old_builds_change_of_boatload_into_its_translation_is_refused(self):
        # v3.0HF's request held exactly this: change mods/boatload.pw.toml to the translation's file.
        before = self.tree()
        rid = self.file_by_hand([{"op": "change", "name": "Boatload ZH_HK Translation", "source": "curseforge",
                                  "projectId": "1455793", "versionId": "7586163", "versionNumber": "1.21_ZH_HK_Translation",
                                  "channel": "release", "fileName": "boatload_1.21_ZH_HK_Translation.zip",
                                  "fileHash": "4ffa633500ffdabc4513610a866639f4894d1291", "hashFormat": "sha1",
                                  "side": "both", "metafile": "mods/boatload.pw.toml", "replaces": "1.21.1-6.0.2",
                                  "reason": "upstream"}])
        self.assertEqual(self.apply(rid), "test-failed")
        reason = self.queue.get(rid).reason
        self.assertIn("mods/boatload.pw.toml is a different project", reason)
        self.assertIn("curseforge 337396", reason)
        self.assertEqual((self.tree(), self.pack("mods/boatload.pw.toml")), (before, tomllib.loads(BOATLOAD)))

    def test_a_different_mod_with_the_same_name_gets_its_own_file(self):
        request = self.check(up("999999", "1", "Boatload", "boatload-fork-1.0.jar", MOD))
        self.assertEqual(self.apply(request["id"]), "applied")
        self.assertEqual(self.pack("mods/boatload.pw.toml"), tomllib.loads(BOATLOAD))
        self.assertEqual(self.pack("mods/boatload-2.pw.toml")["update"]["curseforge"]["project-id"], 999999)
        self.assertEqual(sorted(self.projects()), sorted(set(self.projects())), json.dumps(self.indexed()))
