"""The two ports the app talks to the world through.

`ServerPort` is the game; `CatalogPort`, `JarPort` and `InstalledPort` are
Modrinth and CurseForge, a candidate's jar, and the server's mods directory.
Each has a real adapter beside it and a fake in `tests/fakes.py`.

Tests drive the HTTP API with a fake behind each port, so a new feature is
tested by telling the fake what the world looks like and asserting on what a
caller of the API sees.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol

from .catalog import InstalledMod, Project, Version
from .jar import JarInfo


class ServerUnavailable(Exception):
    """The game server did not answer: container down, still booting, or RCON refused."""


class ServerPort(Protocol):
    """The Minecraft server on this guest: its container and its RCON console."""

    def container_running(self) -> bool:
        """True while the game's container is running, booted or not."""

    def rcon(self, command: str) -> str:
        """Run one console command and return its reply with colour codes removed.

        Raises ServerUnavailable when the server cannot be reached, and
        ValueError for a command the adapter does not allow.
        """

    def last_start(self) -> datetime | None:
        """When the server process last started (timezone-aware), if known."""


class CatalogPort(Protocol):
    """Modrinth and CurseForge, narrowed to NeoForge 1.21.1."""

    def search(self, query: str) -> tuple[list[Project], list[str]]:
        """Projects matching a query (or a pasted CurseForge id or link), and warnings."""

    def project(self, source: str, project_id: str) -> Project: ...

    def versions(self, source: str, project_id: str) -> list[Version]:
        """Newest first."""

    def find_by_mod_id(self, mod_id: str, hints: list[str]) -> list[Project]:
        """Projects on either platform that could be the mod with this id."""


class JarPort(Protocol):
    def info(self, version: Version) -> JarInfo | None:
        """The candidate's jar read for its mods.toml; None when it cannot be."""


class InstalledPort(Protocol):
    def mods(self) -> list[InstalledMod]:
        """Every mod the server has installed, read from its mods directory."""


class WhitelistPort(Protocol):
    def canonical(self, name: str) -> str | None:
        """The whitelist's spelling of a Minecraft name, or None when it is not on the list.

        There is deliberately no way to list the whitelist.
        """


class BootResult:
    """How a boot ended: `ok`, or why not (the first mod-sourced ERROR, a timeout, ...)."""

    def __init__(self, ok: bool, reason: str = ""):
        self.ok, self.reason = ok, reason


class HostError(Exception):
    """A host step did not happen, or could not be verified to have happened."""


class MaintenanceHost(Protocol):
    """Guest 120 as the maintenance runner drives it (`dockerhost.DockerHost`; `FakeHost` in the tests).
    The app never holds one: it has no Docker socket. Every method that changes something verifies it
    and raises HostError when it cannot."""

    def players(self) -> int | None:
        """How many are online; None when the server cannot say (down, booting, RCON silent)."""

    def running(self) -> bool:
        """True while the live container runs, booted or not."""

    def say(self, text: str) -> bool:
        """A message in game; False if it could not be sent."""

    def preflight(self, stage_dir: Path) -> None:
        """Room on disk for the stage, the backup and the test copy; the live container exists."""

    def download(self, pack_dir: Path, stage_dir: Path) -> None:
        """Fill a copy of the live mods/ for the candidate pack (packwiz-installer), live server untouched."""

    def block_joins(self) -> None: ...

    def allow_joins(self) -> None: ...

    def stop_live(self, clean: bool = True) -> None:
        """Graceful stop (and the backup sidecar). Returns only once the container exited; with `clean`, only
        once it exited 0 having saved the world (a rollback, which restores the backup, needs just stopped)."""

    def backup(self, dest: Path) -> dict:
        """A cold backup of the world and server files, verified against the disk. Returns its manifest."""

    def test_boot(self, pack_dir: Path, stage_dir: Path, manifest: dict, timeout: float) -> BootResult:
        """The candidate against a disposable copy restored from `manifest`. Must return by `timeout`,
        having killed the test container if it had to."""

    def test_cleanup(self) -> bool:
        """Remove the test container and its copy; True only when both are confirmed gone."""

    def test_leftover(self) -> bool: ...

    def promote_mods(self, stage_dir: Path) -> None:
        """Put the downloaded mods in place of the live ones (the live server is stopped)."""

    def restore(self, manifest: dict, aside: Path) -> None:
        """Move the failed world and server files under `aside`, restore the backup, verify it."""

    def start_live(self) -> str:
        """Start the live server; return a token naming THIS start (its start time)."""

    def wait_fresh_done(self, token: str, timeout: float) -> BootResult:
        """ok once the start named by `token` logged Done and is still that start."""

    def discard_stage(self, stage_dir: Path) -> None: ...

    def prune_backups(self, backup_dir: Path, keep: int) -> None: ...
