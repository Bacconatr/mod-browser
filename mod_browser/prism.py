"""The Prism Launcher instance friends import once, and the pack files it reads.

The instance is a small zip: the Minecraft and NeoForge components (read from
the pack's own `pack.toml`, so they never drift from the server), a memory
setting, `packwiz-installer-bootstrap.jar`, and a pre-launch command that runs
it against the public `pack.toml` on every launch. packwiz-installer then
makes the mods folder match the pack: it adds, updates and removes, and with
`-s client` it skips mods marked server-only.

The bootstrap jar is not in this repository: the deploy puts a pinned copy on
disk and `MOD_BROWSER_BOOTSTRAP_JAR` names it. Without it the zip is not
offered (404) rather than built broken.

The pack's own files are served from `/pack/` so the launcher has a public
`pack.toml`: only `pack.toml`, `index.toml` and the files `index.toml` lists
(metafiles and overrides), never the git history or NOTES.md.
"""

from __future__ import annotations

import io
import tomllib
import zipfile
from pathlib import Path
from typing import Any

JAR_NAME = "packwiz-installer-bootstrap.jar"
_EPOCH = (1980, 1, 1, 0, 0, 0)  # a fixed timestamp keeps the zip byte-identical between downloads


def _cfg(value: str) -> str:
    """A value for Prism's instance.cfg (an INI file read as Qt settings): quoted, with \\ and " escaped."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def instance_zip(pack: dict[str, Any], bootstrap: bytes, *, name: str, pack_url: str, max_mb: int, min_mb: int) -> bytes:
    versions = pack.get("versions", {})
    minecraft, neoforge = versions.get("minecraft"), versions.get("neoforge")
    if not minecraft or not neoforge:
        raise ValueError("pack.toml names no minecraft or neoforge version")
    components = [
        {"cachedName": "Minecraft", "cachedVersion": minecraft, "important": True, "uid": "net.minecraft", "version": minecraft},
        {"cachedName": "NeoForge", "cachedVersion": neoforge, "uid": "net.neoforged", "version": neoforge},
    ]
    import json
    mmc_pack = json.dumps({"components": components, "formatVersion": 1}, indent=4)
    command = f'"$INST_JAVA" -jar {JAR_NAME} -s client {pack_url}'
    config = "\n".join([
        "[General]",
        "ConfigVersion=1.2",
        "InstanceType=OneSix",
        f"name={name}",
        "iconKey=default",
        "OverrideMemory=true",
        f"MinMemAlloc={min_mb}",
        f"MaxMemAlloc={max_mb}",
        "OverrideCommands=true",
        f"PreLaunchCommand={_cfg(command)}",
        "",
    ])
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, data in (("instance.cfg", config.encode()), ("mmc-pack.json", mmc_pack.encode()),
                           (f".minecraft/{JAR_NAME}", bootstrap)):
            info = zipfile.ZipInfo(path, _EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    return out.getvalue()


class PackFiles:
    """The files of the pack a launcher may fetch, found through its own index."""

    def __init__(self, root: Path):
        self.root = root
        self._listed: tuple[tuple[int, int], frozenset[str]] | None = None

    def _allowed(self) -> frozenset[str]:
        index = self.root / "index.toml"
        stat = index.stat()
        sig = (stat.st_size, stat.st_mtime_ns)
        if self._listed is None or self._listed[0] != sig:
            files = {f["file"] for f in tomllib.loads(index.read_text()).get("files", []) if isinstance(f.get("file"), str)}
            self._listed = (sig, frozenset(files | {"pack.toml", "index.toml"}))
        return self._listed[1]

    def read(self, rel: str) -> bytes | None:
        """The file's bytes, or None when it is not part of the pack."""
        try:
            if rel not in self._allowed():
                return None
            target = (self.root / rel).resolve()
            if self.root.resolve() not in target.parents or not target.is_file():
                return None
            return target.read_bytes()
        except (OSError, ValueError, tomllib.TOMLDecodeError):
            return None

    def overrides(self) -> dict[str, bytes]:
        """The pack's own files that are not metafiles (config and the like), by their path in the pack."""
        index = tomllib.loads((self.root / "index.toml").read_text())
        found: dict[str, bytes] = {}
        for item in index.get("files", []):
            rel = item.get("file")
            if not isinstance(rel, str) or item.get("metafile") or rel.endswith(".pw.toml"):
                continue
            data = self.read(rel)
            if data is not None:
                found[rel] = data
        return found
