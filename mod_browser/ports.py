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


class HostPort(Protocol):
    """What the applier needs from the machine the server runs on. No adapter is built yet:
    restarting the live container and booting a throwaway one needs a way to reach Docker that
    this app deliberately does not have (no socket). See README, "The applier"."""

    def test_boot(self, pack_dir: "Path", timeout: float) -> BootResult:
        """Boot a throwaway container with the candidate pack against a COPY of the world.

        ok means it reached Done with no new mod-sourced ERRORs against the live baseline.
        Must return (not raise) after `timeout` seconds with ok=False.
        """

    def backup_world(self) -> str:
        """Take a world backup now and return its id. Raises if it could not."""

    def restart_live(self) -> None:
        """Restart the live container (it pulls the pack from PACKWIZ_URL on start)."""

    def wait_live_done(self, timeout: float) -> BootResult:
        """Wait for the live server to reach Done after `restart_live`."""
