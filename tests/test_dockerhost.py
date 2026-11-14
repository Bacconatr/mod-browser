"""The real host adapter against a fake `docker` (and real files, tar and cp).

`FakeDocker` answers the docker commands `DockerHost` sends, from a small model
of containers: running or not, when each start began, what each start logged.
Everything that is not `docker` (tar, cp, chown) runs for real in a temporary
directory. Backup and restore need GNU tar (guest 120 and CI have it; macOS
has bsdtar, so those tests skip there).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from mod_browser.dockerhost import LABEL, SAVED, TEST_NAME, DockerHost, IptablesGate
from mod_browser.ports import HostError

GNU_TAR = "GNU tar" in subprocess.run(["tar", "--version"], capture_output=True, text=True).stdout


class Done:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class FakeDocker:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.boxes: dict[str, dict] = {}
        self.calls: list[list[str]] = []
        self.starts = 0
        self.stop_exit = 0
        self.writes_saved = True
        self.boot_logs = "Done (12.345s)! For help, type \"help\"\n"
        self.test_behaviour = "done"   # done | hang | exit
        self.rm_fails = False
        self.players = "§6There are §c0§6 of a max of §c10§6 players online: "

    def add(self, name, running=True, image="itzg/minecraft-server:2026.10.1-java21",
            env=("EULA=TRUE", "RCON_PASSWORD_FILE=/run/secrets/aeronautics/rcon", "MEMORY=6G", "PACKWIZ_URL=/data/pack/pack.toml")):
        self.starts += 1
        self.boxes[name] = {"Running": running, "Status": "running" if running else "exited", "ExitCode": 0, "OOMKilled": False,
                            "StartedAt": f"2026-11-13T20:00:{self.starts:02d}Z", "Image": image, "Env": list(env),
                            "logs": {f"2026-11-13T20:00:{self.starts:02d}Z": "old start\nDone (40.1s)! For help\n"}}

    def __call__(self, argv, capture_output=True, text=True, timeout=None, input=None, **kw):
        if argv[0] != "docker":
            return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        self.calls.append(argv[1:])
        cmd, args = argv[1], argv[2:]
        if cmd == "inspect":
            box = self.boxes.get(args[-1])
            if box is None:
                return Done(1, "", "Error: No such object")
            state = {k: box[k] for k in ("Running", "Status", "ExitCode", "OOMKilled", "StartedAt")}
            return Done(0, json.dumps([{"State": state, "Config": {"Image": box["Image"], "Env": box["Env"]}}]))
        if cmd == "exec":
            if args[1:] == ["rcon-cli", "list"]:
                return Done(0, self.players)
            return Done(0)
        if cmd == "stop":
            box = self.boxes[args[-1]]
            box.update(Running=False, Status="exited", ExitCode=self.stop_exit)
            if args[-1] == "aeronautics":
                (self.data_dir / "logs").mkdir(exist_ok=True)
                (self.data_dir / "logs" / "latest.log").write_text("Stopping the server\n" + (f"[Server thread/INFO]: ThreadedAnvilChunkStorage: {SAVED}\n" if self.writes_saved else ""))
            return Done(0)
        if cmd == "start":
            box = self.boxes[args[-1]]
            self.starts += 1
            box.update(Running=True, Status="running", StartedAt=f"2026-11-13T21:00:{self.starts:02d}Z")
            box["logs"][box["StartedAt"]] = self.boot_logs
            return Done(0)
        if cmd == "logs":
            box = self.boxes.get(args[-1])
            if box is None:
                return Done(1, "", "No such container")
            if args[0] == "--since":
                return Done(0, "".join(t for at, t in box["logs"].items() if at >= args[1]))
            return Done(0, "".join(box["logs"].values()))
        if cmd == "run":
            name = args[args.index("--name") + 1]
            if "--rm" in args:
                return Done(0, "Finished successfully!")
            self.add(name, env=())
            self.boxes[name]["Labels"] = args[args.index("--label") + 1]
            text = {"done": "Loading\nERROR one\nDone (31.0s)! For help\n", "hang": "Loading mods...\n",
                    "exit": "Exception in server tick loop\n"}[self.test_behaviour]
            self.boxes[name]["logs"] = {"now": text}
            if self.test_behaviour == "exit":
                self.boxes[name].update(Running=False, Status="exited", ExitCode=1)
            return Done(0, "abc123")
        if cmd == "kill":
            if args[-1] in self.boxes:
                self.boxes[args[-1]].update(Running=False, Status="exited", ExitCode=137)
            return Done(0)
        if cmd == "rm":
            if not self.rm_fails:
                self.boxes.pop(args[-1], None)
            return Done(0)
        if cmd == "ps":
            label = args[args.index("--filter") + 1].split("=", 1)[1]
            return Done(0, "".join(f"{n}\n" for n, b in self.boxes.items() if b.get("Labels", "").startswith(label)))
        raise AssertionError(f"unexpected docker {argv}")


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class HostCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data = self.root / "data"
        (self.data / "world" / "region").mkdir(parents=True)
        (self.data / "world" / "level.dat").write_bytes(b"level-v1")
        (self.data / "world" / "region" / "r.0.0.mca").write_bytes(b"chunks-v1" * 100)
        (self.data / "world" / "data").mkdir()
        (self.data / "world" / "data" / "DistantHorizons.sqlite").write_bytes(b"lod" * 1000)
        (self.data / "mods").mkdir()
        (self.data / "mods" / "create.jar").write_bytes(b"create")
        (self.data / "config").mkdir()
        (self.data / "config" / "a.toml").write_text("a = 1\n")
        (self.data / "packwiz.json").write_text("{}")
        (self.data / "packwiz-installer-v0.5.14-dist.jar").write_bytes(b"jar")
        (self.data / "libraries").mkdir()
        (self.data / "pack").mkdir()
        self.docker = FakeDocker(self.data)
        self.docker.add("aeronautics")
        self.docker.add("aeronautics-backup")
        self.clock = Clock()
        self.host = DockerHost(data_dir=self.data, work_dir=self.root / "work", run=self.docker, sleep=self.clock.sleep,
                               clock=self.clock, poll=5, settle=1)


class LiveServerTest(HostCase):
    def test_a_clean_stop_stops_the_sidecar_too_and_is_verified(self):
        self.host.stop_live()
        self.assertFalse(self.docker.boxes["aeronautics"]["Running"])
        self.assertFalse(self.docker.boxes["aeronautics-backup"]["Running"])
        self.assertEqual([c[0] for c in self.docker.calls if c[0] == "stop"], ["stop", "stop"])

    def test_a_stop_that_was_a_kill_or_did_not_save_raises(self):
        self.docker.stop_exit = 137
        with self.assertRaisesRegex(HostError, "exited 137"):
            self.host.stop_live()
        self.docker.add("aeronautics")
        self.docker.stop_exit, self.docker.writes_saved = 0, False
        with self.assertRaisesRegex(HostError, SAVED):
            self.host.stop_live()

    def test_a_rollback_needs_only_a_stopped_container(self):
        self.docker.stop_exit, self.docker.writes_saved = 255, False    # stopped in the middle of a boot
        self.host.stop_live(clean=False)
        self.assertFalse(self.docker.boxes["aeronautics"]["Running"])
        with self.assertRaises(HostError):
            self.host.stop_live()

    def test_done_from_an_earlier_start_is_not_evidence(self):
        self.host.stop_live()
        self.docker.boot_logs = "Loading mods...\n"          # this start never reaches Done; the old one did
        token = self.host.start_live()
        result = self.host.wait_fresh_done(token, 30)
        self.assertFalse(result.ok)
        self.assertIn("no Done within 30", result.reason)

    def test_a_fresh_done_is_evidence_but_a_restart_is_not(self):
        self.host.stop_live()
        token = self.host.start_live()
        self.assertTrue(self.host.wait_fresh_done(token, 30).ok)
        self.docker("docker start aeronautics".split())       # restarted by something else
        self.assertFalse(self.host.wait_fresh_done(token, 30).ok)

    def test_players_are_read_through_the_colour_codes_and_say_takes_plain_words_only(self):
        self.assertEqual(self.host.players(), 0)
        self.assertTrue(self.host.say("The server restarts in 1 minute to add new mods."))
        self.assertFalse(self.host.say("hi @a; op me"))
        self.docker.boxes["aeronautics"]["Running"] = False
        self.assertIsNone(self.host.players())


class PromotionTest(HostCase):
    def test_regular_manifest_is_published(self):
        stage = self.root / "stage" / "data"
        (stage / "mods").mkdir(parents=True)
        (stage / "packwiz.json").write_text('{"new": true}')
        self.host.promote_mods(stage.parent)
        self.assertEqual((self.data / "packwiz.json").read_text(), '{"new": true}')

    def test_staged_manifest_symlink_is_rejected(self):
        stage = self.root / "stage" / "data"
        (stage / "mods").mkdir(parents=True)
        (stage / "packwiz.json").symlink_to(self.data / "packwiz.json")
        with self.assertRaises(HostError):
            self.host.promote_mods(stage.parent)
        self.assertEqual((self.data / "packwiz.json").read_text(), "{}")

    def test_live_manifest_symlink_is_rejected_without_touching_target(self):
        stage = self.root / "stage" / "data"
        (stage / "mods").mkdir(parents=True)
        (stage / "packwiz.json").write_text('{"new": true}')
        victim = self.root / "victim"
        victim.write_text("untouched")
        (self.data / "packwiz.json").unlink()
        (self.data / "packwiz.json").symlink_to(victim)
        with self.assertRaises(HostError):
            self.host.promote_mods(stage.parent)
        self.assertEqual(victim.read_text(), "untouched")

class TestBootTest(HostCase):
    manifest = None

    def make_manifest(self):
        if not GNU_TAR:
            self.skipTest("needs GNU tar")
        self.host.stop_live()
        return self.host.backup(self.root / "backups" / "batch-1")

    def stage(self):
        stage = self.root / "work" / "stage-1"
        (stage / "data" / "mods").mkdir(parents=True)
        (stage / "data" / "mods" / "new.jar").write_bytes(b"new")
        return stage

    def test_a_good_boot_runs_beside_nothing_without_rcon_secrets_or_ports(self):
        manifest = self.make_manifest()
        result = self.host.test_boot(self.data / "pack", self.stage(), manifest, 60)
        self.assertTrue(result.ok, result.reason)
        run = next(c for c in self.docker.calls if c[0] == "run")
        self.assertNotIn("-p", run)
        self.assertNotIn("RCON_PASSWORD_FILE=/run/secrets/aeronautics/rcon", run)
        self.assertIn("ENABLE_RCON=false", run)
        self.assertIn(f"{LABEL}=test", run)
        copy = self.root / "work" / "test" / "data"
        self.assertEqual((copy / "world" / "level.dat").read_bytes(), b"level-v1")      # restored from the backup
        self.assertFalse((copy / "world" / "data" / "DistantHorizons.sqlite").exists())
        self.assertEqual(sorted(p.name for p in (copy / "mods").iterdir()), ["new.jar"])  # the candidate's mods
        self.assertTrue(self.host.test_cleanup())
        self.assertFalse((self.root / "work" / "test").exists())
        self.assertFalse(self.host.test_leftover())

    def test_a_hung_boot_is_killed_by_the_timeout_and_cleanup_is_confirmed(self):
        manifest = self.make_manifest()
        self.docker.test_behaviour = "hang"
        result = self.host.test_boot(self.data / "pack", self.stage(), manifest, 60)
        self.assertFalse(result.ok)
        self.assertIn("killed", result.reason)
        self.assertIn(["kill", TEST_NAME], self.docker.calls)
        self.assertTrue(self.host.test_cleanup())

    def test_cleanup_that_cannot_confirm_the_container_is_gone_says_so(self):
        manifest = self.make_manifest()
        self.docker.test_behaviour = "hang"
        self.host.test_boot(self.data / "pack", self.stage(), manifest, 10)
        self.docker.rm_fails = True
        self.assertFalse(self.host.test_cleanup())
        self.assertTrue(self.host.test_leftover())

    def test_a_crashing_boot_fails_with_its_last_lines(self):
        manifest = self.make_manifest()
        self.docker.test_behaviour = "exit"
        result = self.host.test_boot(self.data / "pack", self.stage(), manifest, 60)
        self.assertFalse(result.ok)
        self.assertIn("Exception in server tick loop", result.reason)


@unittest.skipUnless(GNU_TAR, "needs GNU tar (guest 120 and CI have it)")
class BackupTest(HostCase):
    def test_restore_puts_the_backup_back_and_keeps_the_failed_world_aside(self):
        self.host.stop_live()
        manifest = self.host.backup(self.root / "backups" / "batch-1")
        self.assertEqual(manifest["files"]["members"], ["mods", "config", "packwiz.json"])
        # a failed boot with the new mods wrote into the world and the config
        (self.data / "world" / "level.dat").write_bytes(b"level-v2-broken")
        (self.data / "config" / "new-mod.toml").write_text("x = 1\n")
        shutil.rmtree(self.data / "mods")
        (self.data / "mods").mkdir()
        (self.data / "mods" / "new.jar").write_bytes(b"new")
        (self.data / "kubejs").mkdir()
        (self.data / "kubejs" / "new.js").write_text("candidate")
        self.host.restore(manifest, self.root / "work" / "failed-1")
        self.assertFalse((self.data / "kubejs").exists())
        self.assertTrue((self.root / "work" / "failed-1" / "attempt-1" / "kubejs" / "new.js").exists())
        self.assertEqual((self.data / "world" / "level.dat").read_bytes(), b"level-v1")
        self.assertEqual(sorted(p.name for p in (self.data / "mods").iterdir()), ["create.jar"])
        self.assertFalse((self.data / "config" / "new-mod.toml").exists())
        aside = self.root / "work" / "failed-1" / "attempt-1"
        self.assertEqual((aside / "world" / "level.dat").read_bytes(), b"level-v2-broken")
        self.assertTrue((aside / "mods" / "new.jar").exists())

    def test_a_backup_whose_file_changed_is_refused(self):
        self.host.stop_live()
        manifest = self.host.backup(self.root / "backups" / "batch-1")
        with open(manifest["world"]["file"], "ab") as fh:
            fh.write(b"x")
        with self.assertRaisesRegex(HostError, "checksum"):
            self.host.restore(manifest, self.root / "work" / "failed-1")
        self.assertEqual((self.data / "world" / "level.dat").read_bytes(), b"level-v1")   # nothing moved


class GateTest(unittest.TestCase):
    def test_the_block_is_added_once_verified_and_removed_until_gone(self):
        rules: list[list[str]] = []

        def run(argv, **kw):
            op, rule = argv[3], argv[6:] if argv[3] == "-I" else argv[5:]
            if op == "-C":
                return Done(0 if rule in rules else 1)
            if op == "-I":
                rules.append(rule)
            if op == "-D" and rule in rules:
                rules.remove(rule)
            return Done(0)

        gate = IptablesGate(25565, run=run)
        gate.block()
        gate.block()
        self.assertEqual(len(rules), 1)
        self.assertIn("NEW", rules[0])
        gate.allow()
        self.assertEqual(rules, [])

    def test_a_block_that_will_not_go_away_raises(self):
        gate = IptablesGate(25565, run=lambda argv, **kw: Done(0))
        with self.assertRaises(HostError):
            gate.allow()
