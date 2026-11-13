"""The real `MaintenanceHost`: guest 120's Docker, files and firewall, driven as root by the runner.

Never imported by the web app (it has no Docker socket); only `maintenance.main` builds one.
Every change is verified before the method returns, and an unverifiable one raises HostError.

Names on 120: the live container `aeronautics` (itzg/minecraft-server, data dir
/opt/aeronautics/data, the pack read-only at /data/pack), its backup sidecar
`aeronautics-backup`. The test boot is `aeronautics-maint-test`, labelled
`mod-maintenance=test`, with no published port.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .ports import BootResult, HostError
from .status import parse_list

TEST_NAME = "aeronautics-maint-test"
STAGE_NAME = "aeronautics-maint-stage"
LABEL = "mod-maintenance"
_COLOUR = re.compile(r"\x1b\[[0-9;]*m|§.")
_DONE = re.compile(r"Done \(\d+(\.\d+)?s\)!")
SAVED = "All dimensions are saved"
#: Regenerated from the world, GBs, and excluded by mc-backup too. A restored world starts without it.
WORLD_EXCLUDES = ("DistantHorizons.sqlite*",)
#: Server files that a candidate can change and a rollback must put back, besides the world.
SERVER_FILES = ("mods", "config", "defaultconfigs", "kubejs", "packwiz.json", "server.properties")
#: Not copied into the test boot's copy: the world comes from the backup; the rest is not the server's.
NOT_COPIED = ("world", "logs", "crash-reports", "mods", "pack", "mod-browser", "packwiz.json")
#: The live container's environment the test boot must not inherit.
TEST_ENV_DROPPED = ("RCON_PASSWORD_FILE", "RCON_PASSWORD", "ENABLE_RCON")


class NoGate:
    """No join block (a dry run on a machine without iptables)."""

    def block(self) -> None: ...

    def allow(self) -> None: ...


class IptablesGate:
    """New TCP connections to the game's container port are refused in DOCKER-USER (which Docker consults
    before its own accept), so nobody joins between the last player check and the stop. Players already
    connected stay until the stop kicks them. Tagged by comment so recovery finds and removes it."""

    def __init__(self, port: int, run: Callable[..., Any] = subprocess.run):
        self.rule = ["-p", "tcp", "--dport", str(port), "-m", "conntrack", "--ctstate", "NEW",
                     "-m", "comment", "--comment", LABEL, "-j", "REJECT", "--reject-with", "tcp-reset"]
        self._run = run

    def _ipt(self, *args: str) -> int:
        return self._run(["iptables", "-w", "5", *args], capture_output=True, text=True, timeout=30).returncode

    def present(self) -> bool:
        return self._ipt("-C", "DOCKER-USER", *self.rule) == 0

    def block(self) -> None:
        if not self.present() and self._ipt("-I", "DOCKER-USER", "1", *self.rule) != 0:
            raise HostError("iptables refused the join block")
        if not self.present():
            raise HostError("the join block is not in DOCKER-USER after adding it")

    def allow(self) -> None:
        for _ in range(10):
            if not self.present():
                return
            self._ipt("-D", "DOCKER-USER", *self.rule)
        raise HostError("the join block is still in DOCKER-USER")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


class DockerHost:
    def __init__(self, *, data_dir: Path, work_dir: Path, live: str = "aeronautics", sidecar: str | None = "aeronautics-backup",
                 gate: Any = None, test_memory: str = "9g", stop_timeout: int = 300,
                 run: Callable[..., Any] = subprocess.run, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, poll: float = 5.0, settle: float = 15.0):
        self.data_dir, self.work_dir, self.live, self.sidecar = data_dir, work_dir, live, sidecar
        self.gate = gate or NoGate()
        self.test_memory, self.stop_timeout = test_memory, stop_timeout
        self._run, self._sleep, self._clock, self.poll, self.settle = run, sleep, clock, poll, settle

    # ---- plumbing ----
    def _cmd(self, argv: list[str], timeout: float = 120, check: bool = True, **kw: Any) -> subprocess.CompletedProcess:
        try:
            done = self._run(argv, capture_output=True, text=True, timeout=timeout, **kw)
        except subprocess.TimeoutExpired as exc:
            raise HostError(f"{argv[0]} {argv[1] if len(argv) > 1 else ''} did not finish in {int(timeout)} s") from exc
        if check and done.returncode != 0:
            raise HostError(f"{' '.join(argv[:3])} exited {done.returncode}: {(done.stderr or done.stdout).strip()[-300:]}")
        return done

    def _docker(self, *args: str, timeout: float = 120, check: bool = True) -> subprocess.CompletedProcess:
        return self._cmd(["docker", *args], timeout=timeout, check=check)

    def _inspect(self, name: str) -> dict[str, Any] | None:
        done = self._docker("inspect", "--type", "container", name, check=False)
        if done.returncode != 0:
            return None
        try:
            return json.loads(done.stdout)[0]
        except (ValueError, IndexError):
            raise HostError(f"docker inspect {name} printed no JSON") from None

    def _owner(self) -> str:
        st = self.data_dir.stat()
        return f"{st.st_uid}:{st.st_gid}"

    def _chown(self, path: Path) -> None:
        self._cmd(["chown", "-R", self._owner(), str(path)], timeout=600)

    def _copy(self, src: Path, dest: Path) -> None:
        self._cmd(["cp", "-a", str(src), str(dest)], timeout=1800)

    def _remove(self, path: Path) -> bool:
        if path.exists() or path.is_symlink():
            shutil.rmtree(path, ignore_errors=True) if path.is_dir() and not path.is_symlink() else path.unlink(missing_ok=True)
        return not (path.exists() or path.is_symlink())

    # ---- the live server ----
    def running(self) -> bool:
        info = self._inspect(self.live)
        return bool(info and info["State"]["Running"])

    def players(self) -> int | None:
        if not self.running():
            return None
        done = self._docker("exec", self.live, "rcon-cli", "list", timeout=20, check=False)
        parsed = parse_list(_COLOUR.sub("", done.stdout)) if done.returncode == 0 else None
        return parsed["online"] if parsed else None

    def say(self, text: str) -> bool:
        if not re.fullmatch(r"[A-Za-z0-9 .,:'!-]{1,200}", text):
            return False
        return self._docker("exec", self.live, "rcon-cli", "say", text, timeout=20, check=False).returncode == 0

    def block_joins(self) -> None:
        self.gate.block()

    def allow_joins(self) -> None:
        self.gate.allow()

    def stop_live(self, clean: bool = True) -> None:
        if self.sidecar and self._inspect(self.sidecar):
            self._docker("stop", "-t", "60", self.sidecar, timeout=120)
        info = self._inspect(self.live)
        if info is None:
            raise HostError(f"there is no container {self.live}")
        log = self.data_dir / "logs" / "latest.log"
        if info["State"]["Running"]:
            began = time.time()
            self._docker("stop", "-t", str(self.stop_timeout), self.live, timeout=self.stop_timeout + 120)
            info = self._inspect(self.live) or {}
            if clean and log.is_file() and log.stat().st_mtime < began - 1:
                raise HostError("the server log was not written during the stop")
        state = info.get("State", {})
        if state.get("Running") or state.get("Status") not in ("exited", "created"):
            raise HostError(f"the container is {state.get('Status')}, not exited")
        if not clean:
            return
        if state.get("OOMKilled") or state.get("ExitCode") != 0:
            raise HostError(f"the server exited {state.get('ExitCode')} (OOM killed: {state.get('OOMKilled')}), not a clean stop")
        if not log.is_file() or SAVED not in log.read_text(errors="replace")[-200_000:]:
            raise HostError(f"the server log does not say '{SAVED}'")

    def start_live(self) -> str:
        before = (self._inspect(self.live) or {}).get("State", {}).get("StartedAt")
        self._docker("start", self.live, timeout=120)
        info = self._inspect(self.live)
        token = (info or {}).get("State", {}).get("StartedAt")
        if not info or not info["State"]["Running"] or not token or token == before:
            raise HostError("the container did not start")
        if self.sidecar and self._inspect(self.sidecar):
            self._docker("start", self.sidecar, timeout=120, check=False)  # not a boot requirement; it waits 15 min anyway
        return token

    def wait_fresh_done(self, token: str, timeout: float) -> BootResult:
        """Done must appear in THIS start's output: `docker logs --since <its start>`, and the container must
        still be that start (a crash loop restarts it with a new StartedAt)."""
        deadline = self._clock() + timeout
        while True:
            info = self._inspect(self.live)
            state = (info or {}).get("State", {})
            if state.get("StartedAt") != token:
                return BootResult(False, "the container restarted during the boot (or was replaced)")
            if not state.get("Running"):
                return BootResult(False, f"the server exited {state.get('ExitCode')} during the boot")
            out = self._docker("logs", "--since", token, self.live, timeout=60, check=False)
            if _DONE.search(_COLOUR.sub("", out.stdout + out.stderr)):
                return BootResult(True, "Done")
            if self._clock() >= deadline:
                return BootResult(False, f"no Done within {int(timeout)} s")
            self._sleep(self.poll)

    # ---- staging ----
    def _image(self) -> str:
        info = self._inspect(self.live)
        if info is None:
            raise HostError(f"there is no container {self.live}")
        return info["Config"]["Image"]

    def _installer(self) -> Path:
        jars = sorted(self.data_dir.glob("packwiz-installer-v*-dist.jar"))
        if not jars:
            raise HostError("the data dir has no packwiz-installer jar (the live server downloads it on its first start)")
        return jars[-1]

    def preflight(self, stage_dir: Path) -> None:
        self._image()
        world = self.data_dir / "world"
        if not (world / "level.dat").is_file():
            raise HostError(f"{world} has no level.dat")
        need = _size(world) + _size(self.data_dir / "mods") * 3 + _size(self.data_dir / "libraries") + (2 << 30)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.work_dir).free
        if free < need:
            raise HostError(f"{free >> 20} MiB free under {self.work_dir}, need {need >> 20}")

    def download(self, pack_dir: Path, stage_dir: Path) -> None:
        self.discard_stage(stage_dir)
        data = stage_dir / "data"
        data.mkdir(parents=True)
        self._copy(self.data_dir / "mods", data / "mods")
        if (self.data_dir / "packwiz.json").is_file():
            self._copy(self.data_dir / "packwiz.json", data / "packwiz.json")
        installer = self._installer()
        self._copy(installer, data / installer.name)
        self._chown(stage_dir)
        self._docker("rm", "-f", STAGE_NAME, check=False)
        done = self._docker("run", "--rm", "--name", STAGE_NAME, "--label", f"{LABEL}=stage", "--user", self._owner(),
                            "--entrypoint", "java", "-w", "/data", "-v", f"{data}:/data", "-v", f"{pack_dir}:/data/pack:ro",
                            self._image(), "-Djava.awt.headless=true", "-cp", f"/data/{installer.name}",
                            "link.infra.packwiz.installer.Main", "-s", "server", "/data/pack/pack.toml",
                            timeout=1800, check=False)
        if done.returncode != 0:
            raise HostError(f"packwiz-installer exited {done.returncode}: {(done.stdout + done.stderr).strip()[-400:]}")

    def discard_stage(self, stage_dir: Path) -> None:
        self._docker("rm", "-f", STAGE_NAME, check=False)
        self._remove(stage_dir)

    def promote_mods(self, stage_dir: Path) -> None:
        incoming, live = self.data_dir / "mods.incoming", self.data_dir / "mods"
        self._remove(incoming)
        self._copy(stage_dir / "data" / "mods", incoming)
        old = self.data_dir / "mods.replaced"
        self._remove(old)
        live.rename(old)
        incoming.rename(live)
        self._remove(old)
        if (stage_dir / "data" / "packwiz.json").is_file():
            shutil.copy2(stage_dir / "data" / "packwiz.json", self.data_dir / "packwiz.json")
        want = sorted(p.name for p in (stage_dir / "data" / "mods").iterdir())
        if sorted(p.name for p in live.iterdir()) != want:
            raise HostError("the live mods/ does not match the downloaded one after the swap")

    # ---- backup and restore ----
    def _tar(self, args: list[str], timeout: float = 3600) -> None:
        self._cmd(["tar", *args], timeout=timeout)

    def _tar_create(self, args: list[str]) -> None:
        """GNU tar exits 1 when a file's ctime moved while it was read. The server is stopped, so that is
        a file system settling the server's last writes (seen on the Mac's file sharing in the dry run,
        2026-11-13): try again after a pause. `--compare` afterwards is what decides."""
        for attempt in range(3):
            done = self._cmd(["tar", *args], timeout=3600, check=False)
            if done.returncode == 0:
                return
            if done.returncode != 1 or "file changed as we read it" not in done.stderr or attempt == 2:
                raise HostError(f"tar --create exited {done.returncode}: {done.stderr.strip()[-300:]}")
            self._sleep(10)

    def backup(self, dest: Path) -> dict:
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        try:
            return self._backup(dest)
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)  # half a backup is not one: never left to look like it
            raise

    def _backup(self, dest: Path) -> dict:
        excludes = [f"--exclude={e}" for e in WORLD_EXCLUDES]
        world_tar, files_tar = dest / "world.tar.gz", dest / "server-files.tar.gz"
        members = [m for m in SERVER_FILES if (self.data_dir / m).exists()]
        self._cmd(["sync"], timeout=300, check=False)
        self._tar_create(["--create", "--gzip", "--file", str(world_tar), *excludes, "-C", str(self.data_dir), "world"])
        self._tar_create(["--create", "--gzip", "--file", str(files_tar), "-C", str(self.data_dir), *members])
        # Verified against the disk it came from: the server is stopped, so any difference is a bad copy.
        self._tar(["--compare", "--gzip", "--file", str(world_tar), "-C", str(self.data_dir)])
        self._tar(["--compare", "--gzip", "--file", str(files_tar), "-C", str(self.data_dir)])
        manifest = {"dir": str(dest), "world": {"file": str(world_tar), "sha256": _sha256(world_tar)},
                    "files": {"file": str(files_tar), "sha256": _sha256(files_tar), "members": members},
                    "excluded": list(WORLD_EXCLUDES), "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        (dest / "manifest.json").write_text(json.dumps(manifest, indent=1))
        return manifest

    def _check(self, manifest: dict) -> None:
        for part in ("world", "files"):
            path = Path(manifest[part]["file"])
            if not path.is_file() or _sha256(path) != manifest[part]["sha256"]:
                raise HostError(f"the backup's {path.name} is missing or does not match its checksum")

    def restore(self, manifest: dict, aside: Path) -> None:
        self._check(manifest)
        aside.mkdir(parents=True, exist_ok=True)
        n = 1
        while (aside / f"attempt-{n}").exists():
            n += 1
        keep = aside / f"attempt-{n}"
        keep.mkdir()
        for name in ("world", *manifest["files"]["members"]):
            if (self.data_dir / name).exists():
                (self.data_dir / name).rename(keep / name)  # the failed world is kept, never deleted here
        self._tar(["--extract", "--gzip", "--same-owner", "--file", manifest["world"]["file"], "-C", str(self.data_dir)])
        self._tar(["--extract", "--gzip", "--same-owner", "--file", manifest["files"]["file"], "-C", str(self.data_dir)])
        # The server runs as the data dir's owner: make sure of it rather than trusting the archive's ids.
        for name in ("world", *manifest["files"]["members"]):
            self._cmd(["chown", "-R", self._owner(), str(self.data_dir / name)], timeout=1800)
        self._compare(manifest["world"]["file"])
        self._compare(manifest["files"]["file"])

    def _compare(self, archive: str) -> None:
        """Every file of the archive is back with its content, size and times. Owner lines alone are not a
        difference: ownership was set above (the Mac's file sharing ignores chown, seen in the dry run)."""
        done = self._cmd(["tar", "--compare", "--gzip", "--file", archive, "-C", str(self.data_dir)], timeout=3600, check=False)
        if done.returncode == 0:
            return
        lines = [ln for ln in (done.stdout + done.stderr).splitlines() if ln.strip()]
        real = [ln for ln in lines if not ln.endswith(("Uid differs", "Gid differs"))]
        if done.returncode != 1 or real or not lines:
            raise HostError(f"the restored files differ from the backup: {' | '.join(real or lines)[-300:]}")

    def prune_backups(self, backup_dir: Path, keep: int) -> None:
        sets = sorted((p for p in backup_dir.glob("batch-*") if (p / "manifest.json").is_file()),
                      key=lambda p: p.stat().st_mtime)
        for old in sets[:-keep] if keep > 0 else []:
            shutil.rmtree(old, ignore_errors=True)

    # ---- the test boot ----
    def _scratch(self) -> Path:
        return self.work_dir / "test"

    def test_boot(self, pack_dir: Path, stage_dir: Path, manifest: dict, timeout: float) -> BootResult:
        try:
            self._check(manifest)
            scratch = self._scratch() / "data"
            if self._scratch().exists():
                raise HostError("a previous test copy is still there")
            scratch.mkdir(parents=True)
            # The world comes from the backup, which proves the backup restores; the rest is the live server's.
            self._tar(["--extract", "--gzip", "--same-owner", "--file", manifest["world"]["file"], "-C", str(scratch)])
            for entry in self.data_dir.iterdir():
                if entry.name in NOT_COPIED or entry.name.startswith(("world.", "mods.")):
                    continue
                self._copy(entry, scratch / entry.name)
            self._copy(stage_dir / "data" / "mods", scratch / "mods")
            if (stage_dir / "data" / "packwiz.json").is_file():
                self._copy(stage_dir / "data" / "packwiz.json", scratch / "packwiz.json")
            self._chown(self._scratch())
            info = self._inspect(self.live) or {}
            env = [e for e in info.get("Config", {}).get("Env", []) if e.split("=", 1)[0] not in TEST_ENV_DROPPED]
            argv = ["run", "-d", "--name", TEST_NAME, "--label", f"{LABEL}=test", "--restart", "no",
                    "--memory", self.test_memory, "--memory-swap", self.test_memory,
                    "-v", f"{scratch}:/data", "-v", f"{pack_dir}:/data/pack:ro"]
            for e in [*env, "ENABLE_RCON=false"]:
                argv += ["-e", e]
            self._docker(*argv, self._image(), timeout=120)
        except (HostError, OSError) as exc:
            return BootResult(False, f"the test boot could not start: {exc}")
        began, deadline = self._clock(), self._clock() + timeout
        while True:
            info = self._inspect(TEST_NAME) or {}
            state = info.get("State", {})
            out = self._docker("logs", "--tail", "4000", TEST_NAME, timeout=60, check=False)
            text = _COLOUR.sub("", out.stdout + out.stderr)
            if not state.get("Running"):
                return BootResult(False, f"the test server exited {state.get('ExitCode')}: {_tail(text)}")
            if _DONE.search(text):
                # Some mods fail in the first ticks after Done: it must still be up a little later.
                self._sleep(self.settle)
                if not ((self._inspect(TEST_NAME) or {}).get("State", {}).get("Running")):
                    return BootResult(False, f"the test server stopped right after Done: {_tail(text)}")
                errors = text[:_DONE.search(text).start()].count("ERROR")
                return BootResult(True, f"Done in {int(self._clock() - began)} s; {errors} ERROR lines before Done")
            if self._clock() >= deadline:
                self._docker("kill", TEST_NAME, timeout=60, check=False)
                return BootResult(False, f"no Done within {int(timeout)} s; the test server was killed")
            self._sleep(self.poll)

    def test_cleanup(self) -> bool:
        for name in (TEST_NAME, STAGE_NAME):
            self._docker("rm", "-f", name, timeout=120, check=False)
        left = self._docker("ps", "-a", "-q", "--filter", f"label={LABEL}", timeout=60, check=False)
        if left.returncode != 0 or left.stdout.strip():
            return False
        if self._inspect(TEST_NAME) is not None:
            return False
        return self._remove(self._scratch())

    def test_leftover(self) -> bool:
        left = self._docker("ps", "-a", "-q", "--filter", f"label={LABEL}", timeout=60, check=False)
        return left.returncode != 0 or bool(left.stdout.strip()) or self._scratch().exists()


def _tail(text: str, lines: int = 6) -> str:
    return " | ".join(text.strip().splitlines()[-lines:])[-600:]
