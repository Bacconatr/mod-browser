"""The CurseForge-app modpack: one zip a friend imports, and the app downloads every mod itself.

The CurseForge app is the one launcher CurseForge lets download the mods whose
authors block other launchers (Loconautics, among others), so this import has
no download-by-hand step. It is the format CurseForge's own modpacks use: a
`manifest.json` naming Minecraft, the NeoForge version (both read from the
pack's `pack.toml`, so they never drift from the server) and every player-side
entry by CurseForge project and file id, plus an `overrides/` folder with the
pack's non-metafile files (config, if any).

It is built from the live pack on request, so it is never staler than the pack.
It does not update itself: when the pack changes, a friend imports it again.

An entry with no CurseForge ids cannot be listed, and a zip without it would
fail the login with a missing mod, the very thing this replaces. So the zip is
only offered while every player-side entry has ids; `missing` names the rest.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

_EPOCH = (1980, 1, 1, 0, 0, 0)  # a fixed timestamp keeps the zip byte-identical between downloads


def player_entries(mods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What a player installs: everything but the server-only entries."""
    return [m for m in mods if m.get("side") != "server"]


def missing(mods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The player-side entries the CurseForge app could not be told about."""
    return [m for m in player_entries(mods) if not m.get("curseforge")]


def modpack_zip(pack: dict[str, Any], mods: list[dict[str, Any]], overrides: dict[str, bytes], *,
                name: str, version: str) -> bytes:
    versions = pack.get("versions", {})
    minecraft, neoforge = versions.get("minecraft"), versions.get("neoforge")
    if not minecraft or not neoforge:
        raise ValueError("pack.toml names no minecraft or neoforge version")
    if missing(mods):
        raise ValueError("some player-side entries have no CurseForge ids")
    files = sorted(({"projectID": m["curseforge"]["project"], "fileID": m["curseforge"]["file"], "required": True}
                    for m in player_entries(mods)), key=lambda f: (f["projectID"], f["fileID"]))
    manifest = {
        "minecraft": {"version": minecraft, "modLoaders": [{"id": f"neoforge-{neoforge}", "primary": True}]},
        "manifestType": "minecraftModpack",
        "manifestVersion": 1,
        "name": name,
        "version": version,
        "author": pack.get("author") or "",
        "files": files,
        "overrides": "overrides",
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        entries = [("manifest.json", json.dumps(manifest, indent=2).encode())]
        entries += [(f"overrides/{path}", data) for path, data in sorted(overrides.items())]
        for path, data in entries:
            info = zipfile.ZipInfo(path, _EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    return out.getvalue()
