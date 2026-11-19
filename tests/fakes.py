"""In-memory fakes behind the ports, and a throwaway packwiz repo."""

from __future__ import annotations

import subprocess
import textwrap
from datetime import datetime
from pathlib import Path

from mod_browser.ports import ServerUnavailable

# Captured from the live server on 2026-11-12 (rcon-cli's colour codes removed).
TPS_REPLY = textwrap.dedent("""\
    Overworld: 20.000 TPS (0.301 ms/tick)
    northstar:earth_orbit: 20.000 TPS (0.078 ms/tick)
    The End: 20.000 TPS (0.036 ms/tick)
    The Nether: 20.000 TPS (0.037 ms/tick)
    Overall: 19.412 TPS (51.507 ms/tick)
    """)


class FakeServer:
    """A game server the test can boot, stop, or leave half-started."""

    def __init__(self) -> None:
        self.running = True
        self.rcon_up = True
        self.tps_up = True
        self.players: list[str] = []
        self.max_players = 10
        self.started: datetime | None = None
        self.commands: list[str] = []

    def container_running(self) -> bool:
        return self.running

    def rcon(self, command: str) -> str:
        self.commands.append(command)
        if not (self.running and self.rcon_up):
            raise ServerUnavailable("connection refused")
        if command == "list":
            return (f"There are {len(self.players)} of a max of {self.max_players} players online: "
                    + ", ".join(self.players))
        if command == "neoforge tps":
            if not self.tps_up:
                raise ServerUnavailable("timed out")
            return TPS_REPLY
        if command.startswith("say "):
            return ""
        raise ValueError(command)

    def last_start(self) -> datetime | None:
        return self.started


PACK_TOML = textwrap.dedent("""\
    name = "All of Create: Aeronautics (Tel Aviv)"
    author = "Bacconatr"
    version = "2.9-1"
    pack-format = "packwiz:1.1.0"

    [index]
    file = "index.toml"
    hash-format = "sha256"
    hash = "0"

    [versions]
    minecraft = "1.21.1"
    neoforge = "21.1.264"
    """)

# Real metafiles from the pack as migrated.
METAFILES = {
    "mods/create-connected.pw.toml": textwrap.dedent("""\
        name = "Create: Connected"
        filename = "create_connected-1.3.3-mc1.21.1.jar"
        side = "both"

        [download]
        hash-format = "sha1"
        hash = "72c3653116562dbab38dac123f8effc73c747cc9"
        mode = "metadata:curseforge"

        [update]
        [update.curseforge]
        file-id = 8777573
        project-id = 947914
        """),
    "mods/create-deep-seas.pw.toml": textwrap.dedent("""\
        name = "Create Deep Seas [BETA]"
        filename = "create_submarine-3.4.5.jar"
        side = "both"

        [download]
        url = "https://cdn.modrinth.com/data/mva5q4qZ/versions/twCxciVp/create_submarine-3.4.5.jar"
        hash-format = "sha512"
        hash = "48ba"

        [update]
        [update.modrinth]
        mod-id = "mva5q4qZ"
        version = "twCxciVp"
        """),
    "mods/create-collision-crashfix.pw.toml": textwrap.dedent("""\
        name = "Create Collision Crashfix"
        filename = "create-collision-fix-1.0.0.jar"
        side = "server"

        [download]
        url = "https://cdn.modrinth.com/data/x/versions/y/create-collision-fix-1.0.0.jar"
        hash-format = "sha512"
        hash = "00"

        [update]
        [update.modrinth]
        mod-id = "abcdEFGH"
        version = "y"
        """),
    "resourcepacks/complementary-reimagined.pw.toml": textwrap.dedent("""\
        name = "Complementary Shaders - Reimagined"
        filename = "ComplementaryReimagined_r5.5.1.zip"
        side = "client"

        [download]
        hash-format = "sha1"
        hash = "00"
        mode = "metadata:curseforge"

        [update]
        [update.curseforge]
        file-id = 1
        project-id = 627557
        """),
}

NOTES = textwrap.dedent("""\
    # Tel Aviv's pack: notes

    ## What the pack is

    Built on 2026-11-12. Not an extra: `mods/create-connected.pw.toml`.

    ## Mods added on top of v2.9

    ### `create_submarine-3.4.5.jar`

    side `both`, `mods/create-deep-seas.pw.toml`

    Create: Deep Seas, asked for by a friend 2026-10-19.

    ### `create-collision-fix-1.0.0.jar`

    side `server`, `mods/create-collision-crashfix.pw.toml`

    A crash fix with no date in its note.

    ## Distribution-blocked mods

    The authors of these forbid hosting outside CurseForge: `mods/create-connected.pw.toml`.
    """)

LATER_MOD = textwrap.dedent("""\
    name = "Straw Statues"
    filename = "strawstatues-v21.1.1-1.21.1-NeoForge.jar"
    side = "both"

    [download]
    url = "https://cdn.modrinth.com/data/s/versions/v/strawstatues-v21.1.1-1.21.1-NeoForge.jar"
    hash-format = "sha512"
    hash = "00"

    [update]
    [update.modrinth]
    mod-id = "strawSTA"
    version = "v"
    """)


def _git(root: Path, *args: str, date: str | None = None) -> None:
    env = {"GIT_AUTHOR_NAME": "Ansible (aeronautics role)", "GIT_AUTHOR_EMAIL": "ansible@aeronautics.invalid",
           "GIT_COMMITTER_NAME": "Ansible (aeronautics role)", "GIT_COMMITTER_EMAIL": "ansible@aeronautics.invalid",
           "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(root)}
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)


def _write_index(root: Path) -> None:
    files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*.pw.toml"))
    body = 'hash-format = "sha256"\n' + "".join(
        f'\n[[files]]\nfile = "{f}"\nhash = "0"\nmetafile = true\n' for f in files)
    (root / "index.toml").write_text(body)


def make_pack(root: Path) -> Path:
    """The migrated pack as one root commit, like the role's first commit on 120."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "pack.toml").write_text(PACK_TOML)
    (root / "NOTES.md").write_text(NOTES)
    for path, body in METAFILES.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(body)
    _write_index(root)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "All of Create: Aeronautics v2.9 plus the extras Ansible pinned",
         date="2026-11-12T13:57:53-05:00")
    return root


def add_mod(root: Path, path: str, body: str, message: str, date: str) -> None:
    """A later pack change, the way the request queue will commit one."""
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(body)
    _write_index(root)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message, date=date)
