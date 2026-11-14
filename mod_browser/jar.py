"""A mod jar's own metadata: ids, versions and declared dependencies.

Read straight from the jar's `META-INF/neoforge.mods.toml` (or `mods.toml`),
because Modrinth recognises only 177 of the pack's 213 jars. Only the
central directory and a few small entries are read, never the whole archive,
and every read is capped, so a hostile jar from a public search cannot
exhaust memory. A nested jar (jar-in-jar) is copied in chunks to a spooled
temporary file, never held whole in memory: a "bundled" jar nests jars of
30 MiB or more, and holding them as bytes cost 110 MiB of a 128 MiB
container for one 34 MiB jar (measured on guest 120, 2026-11-14), which
the kernel answered by killing the app.
"""

from __future__ import annotations

import re
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass, field
from typing import IO, Any

TOML_NAMES = ("META-INF/neoforge.mods.toml", "META-INF/mods.toml")
_MAX_TOML = 2 * 1024 * 1024
_MAX_SMALL = 256 * 1024
_MAX_NESTED_JAR = 64 * 1024 * 1024
_MAX_NESTED_COUNT = 16
_MAX_DEPTH = 2
#: A nested jar stays in memory only up to this; past it, the spooled file is on disk.
_SPOOL_IN_MEMORY = 1024 * 1024
_CHUNK = 64 * 1024


class JarError(Exception):
    """The jar could not be read as a mod."""


@dataclass(frozen=True)
class ProvidedMod:
    mod_id: str
    version: str


@dataclass(frozen=True)
class Dependency:
    owner: str
    mod_id: str
    type: str  # required | optional | incompatible | discouraged
    version_range: str
    side: str


@dataclass
class JarInfo:
    #: What this jar registers, and what it bundles inside itself (jar-in-jar).
    provides: list[ProvidedMod] = field(default_factory=list)
    #: Only the jar's own `[[mods]]`, the first one being its primary id.
    own: list[ProvidedMod] = field(default_factory=list)
    dependencies: list[Dependency] = field(default_factory=list)


def _read(zf: zipfile.ZipFile, name: str, limit: int) -> bytes:
    with zf.open(name) as entry:
        data = entry.read(limit + 1)
    if len(data) > limit:
        raise JarError(f"{name} is too large")
    return data


def _spooled(zf: zipfile.ZipFile, name: str, limit: int) -> IO[bytes]:
    """The entry as a seekable file, copied in chunks and capped. Caller closes it."""
    if zf.getinfo(name).file_size > limit:
        raise JarError(f"{name} is too large")
    out = tempfile.SpooledTemporaryFile(max_size=_SPOOL_IN_MEMORY)
    try:
        with zf.open(name) as entry:
            total = 0
            while chunk := entry.read(_CHUNK):
                total += len(chunk)
                if total > limit:  # the declared size can lie; the bytes cannot
                    raise JarError(f"{name} is too large")
                out.write(chunk)
        out.seek(0)
        return out
    except BaseException:
        out.close()
        raise


def _manifest_version(text: str) -> str:
    m = re.search(r"^Implementation-Version:\s*(.+)$", text, re.M)
    return m.group(1).strip() if m else ""


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def parse_mods_toml(text: str) -> tuple[list[dict[str, Any]], list[Dependency]]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise JarError(f"mods.toml is not valid TOML: {exc}") from exc
    mods = [m for m in data.get("mods", []) if isinstance(m, dict) and _str(m.get("modId"))]
    deps: list[Dependency] = []
    tables = data.get("dependencies", {})
    for owner, entries in (tables.items() if isinstance(tables, dict) else []):
        for d in entries if isinstance(entries, list) else []:
            if not isinstance(d, dict) or not _str(d.get("modId")):
                continue
            kind = _str(d.get("type")) or ("optional" if d.get("mandatory") is False else "required")
            deps.append(Dependency(owner, d["modId"], kind.lower(), _str(d.get("versionRange")),
                                   (_str(d.get("side")) or "BOTH").upper()))
    return mods, deps


def read_jar_info(source: IO[bytes], depth: int = 0) -> JarInfo | None:
    """None when the jar carries no mods.toml at all (so it is not a NeoForge mod)."""
    try:
        zf = zipfile.ZipFile(source)
    except zipfile.BadZipFile as exc:
        raise JarError("not a zip file") from exc
    with zf:
        names = set(zf.namelist())
        toml_name = next((n for n in TOML_NAMES if n in names), None)
        if toml_name is None:
            return None
        mods, deps = parse_mods_toml(_read(zf, toml_name, _MAX_TOML).decode("utf-8", "replace"))
        manifest = ""
        if "META-INF/MANIFEST.MF" in names:
            manifest = _manifest_version(_read(zf, "META-INF/MANIFEST.MF", _MAX_SMALL).decode("utf-8", "replace"))
        # `version = "${file.jarVersion}"` is expanded by the build into the manifest.
        own = []
        for m in mods:
            version = _str(m.get("version"))
            own.append(ProvidedMod(m["modId"], manifest if not version or "${" in version else version))
        provides = list(own)
        if depth < _MAX_DEPTH and "META-INF/jarjar/metadata.json" in names:
            provides.extend(_nested(zf, names, depth))
        return JarInfo(provides=provides, own=own, dependencies=deps)


def _nested(zf: zipfile.ZipFile, names: set[str], depth: int) -> list[ProvidedMod]:
    import json
    try:
        meta = json.loads(_read(zf, "META-INF/jarjar/metadata.json", _MAX_SMALL))
    except (ValueError, JarError):
        return []
    found: list[ProvidedMod] = []
    jars = meta.get("jars", []) if isinstance(meta, dict) else []
    for item in jars[:_MAX_NESTED_COUNT] if isinstance(jars, list) else []:
        path = item.get("path") if isinstance(item, dict) else None
        if not isinstance(path, str) or path not in names:
            continue
        try:
            with _spooled(zf, path, _MAX_NESTED_JAR) as nested:
                inner = read_jar_info(nested, depth + 1)
        except (JarError, zipfile.BadZipFile):
            continue
        if inner:
            found.extend(inner.provides)
    return found
