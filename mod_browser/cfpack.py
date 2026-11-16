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

Pinned Modrinth-only jars are verified against their pack hash. Byte-identical
CurseForge twins are matched by fingerprint AND sha1 and named in the manifest;
small unmatched jars go in overrides/mods (16 MiB total). Other unmapped files
keep the download unavailable; a failed verification never produces a partial ZIP.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import struct
import zipfile
from typing import Any
from urllib.parse import quote, unquote

_EPOCH = (1980, 1, 1, 0, 0, 0)  # a fixed timestamp keeps the zip byte-identical between downloads


def player_entries(mods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What a player installs: everything but the server-only entries."""
    return [m for m in mods if m.get("side") != "server"]


def missing(mods: list[dict[str, Any]], *, direct: bool = False) -> list[dict[str, Any]]:
    """The player-side entries the CurseForge app could not be told about."""
    return [m for m in player_entries(mods) if not m.get("curseforge") and not (direct and m.get("exportOverride"))]


MAX_OVERRIDES = 16 * 1024 * 1024


def override_spec(meta: dict[str, Any], path: str) -> dict[str, str] | None:
    """Only pinned Modrinth mod jars can be included as client overrides."""
    if not path.startswith("mods/"):
        return None
    name = meta.get("filename", "")
    mr = meta.get("update", {}).get("modrinth", {})
    download = meta.get("download", {})
    pid, vid = mr.get("mod-id", ""), mr.get("version", "")
    if (not isinstance(name, str) or not name.endswith(".jar") or "/" in name or "\\" in name
            or not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9]+", v) for v in (pid, vid))):
        return None
    url = download.get("url", "")
    expected = f"https://cdn.modrinth.com/data/{pid}/versions/{vid}/{quote(name, safe='')}"
    # Upstream may leave '+' unescaped, which is equivalent in a URL path.
    if not isinstance(url, str) or unquote(url) != unquote(expected):
        return None
    algorithm, digest = download.get("hash-format"), download.get("hash", "")
    if algorithm not in ("sha512", "sha1") or not isinstance(digest, str):
        return None
    if not re.fullmatch(r"[a-fA-F0-9]{%d}" % (128 if algorithm == "sha512" else 40), digest):
        return None
    return {"path": "mods/" + name, "url": url, "algorithm": algorithm, "hash": digest.lower()}


def fingerprint(handle) -> int:
    """CurseForge's MurmurHash2 over bytes excluding tab, LF, CR and space.

    Two passes retain a bounded chunk in RAM even for Cobblemon's 135 MiB jar.
    """
    length = 0
    handle.seek(0)
    while chunk := handle.read(65536):
        length += len(chunk.translate(None, b"\x09\x0a\x0d\x20"))
    h, tail = (1 ^ length) & 0xffffffff, b""
    handle.seek(0)
    while chunk := handle.read(65536):
        chunk = tail + chunk.translate(None, b"\x09\x0a\x0d\x20")
        end = len(chunk) // 4 * 4
        for (k,) in struct.iter_unpack("<I", chunk[:end]):
            k = k * 0x5bd1e995 & 0xffffffff
            k ^= k >> 24
            k = k * 0x5bd1e995 & 0xffffffff
            h = ((h * 0x5bd1e995) ^ k) & 0xffffffff
        tail = chunk[end:]
    if tail:
        h ^= int.from_bytes(tail, "little")
        h = h * 0x5bd1e995 & 0xffffffff
    h ^= h >> 13
    h = h * 0x5bd1e995 & 0xffffffff
    return h ^ (h >> 15)


def prepare(mods: list[dict[str, Any]], http: Any, match=None) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    """Verify every direct jar; prefer an exact CF twin, else a small ZIP override.

    No partial ZIP is produced on a failed download, checksum, mapping or cap.
    The HTTP adapter retains its host allow-list, deadline and download cap.
    """
    prepared, overrides, total = [], {}, 0
    for mod in mods:
        if mod.get("side") == "server" or mod.get("curseforge"):
            prepared.append(mod)
            continue
        spec = mod.get("exportOverride")
        if not spec:
            raise ValueError("a player-side file has no supported export source")
        with http.fetch_jar(spec["url"]) as body:
            digest, sha1 = hashlib.new(spec["algorithm"]), hashlib.sha1()
            while chunk := body.read(65536):
                digest.update(chunk)
                sha1.update(chunk)
            if digest.hexdigest() != spec["hash"]:
                raise ValueError("a Modrinth jar does not match the pack checksum")
            twin = match(fingerprint(body), sha1.hexdigest()) if match else None
            if twin:
                prepared.append({**mod, "curseforge": twin})
                continue
            body.seek(0)
            data = body.read(MAX_OVERRIDES - total + 1)
            total += len(data)
            if total > MAX_OVERRIDES:
                raise ValueError("direct client overrides exceed 16 MiB and have no verified CurseForge twin")
            path = spec["path"]
            if path in overrides:
                raise ValueError("duplicate client override path")
            overrides[path] = data
            prepared.append(mod)
    return prepared, overrides


def modpack_zip(pack: dict[str, Any], mods: list[dict[str, Any]], overrides: dict[str, bytes], *,
                name: str, version: str) -> bytes:
    versions = pack.get("versions", {})
    minecraft, neoforge = versions.get("minecraft"), versions.get("neoforge")
    if not minecraft or not neoforge:
        raise ValueError("pack.toml names no minecraft or neoforge version")
    for mod in missing(mods):
        spec = mod.get("exportOverride")
        data = overrides.get(spec["path"]) if spec else None
        if data is None or hashlib.new(spec["algorithm"], data).hexdigest() != spec["hash"]:
            raise ValueError("some player-side entries have no verified export source")
    files = sorted(({"projectID": m["curseforge"]["project"], "fileID": m["curseforge"]["file"], "required": True}
                    for m in player_entries(mods) if m.get("curseforge")), key=lambda f: (f["projectID"], f["fileID"]))
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
