"""The two ports the app talks to the world through.

Only `ServerPort` exists so far. The spec's `CatalogPort` (Modrinth and
CurseForge search) arrives with the request queue; it gets its own Protocol
here, a real adapter beside `rcon.py`, and a fake in `tests/fakes.py`.

Tests drive the HTTP API with a fake behind each port, so a new feature is
tested by telling the fake what the world looks like and asserting on what a
caller of the API sees.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


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
