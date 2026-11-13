"""Upstream releases of All of Create: Aeronautics, offered to the owner as updates.

A daily check reads the newest release of the CurseForge modpack (project
1518930), downloads its `manifest.json`, and compares it with the pack mod by
mod. A newer release becomes ONE owner-only request, `pending`, showing the
difference: added, removed, changed (version from -> to), and kept. It does
nothing until the owner approves it, and then it goes through the same
applier as any request (test boot, backup, rollback). Never automatic.

Matching a pack mod to an upstream mod: by CurseForge project id; failing that
by the jar's name without its version, because the owner's own pins are often
Modrinth files (Create Deep Seas) that CurseForge knows under another id.

What the pack's owner pinned is never touched. A mod the pack holds as an
extra, or that was added later, is `kept` even when upstream ships a newer (or
older) build: Deep Seas 3.4.5 stays against a bundled 3.3.0. Only mods that
came with the base pack are changed or removed, and only those that came from
CurseForge: a base mod with no upstream match is reported `removed` because the
new release dropped it, never because a pin replaced it.

CurseForge answers here are hand-written from its documented schema in the
tests: the key cannot be tried live from the tests.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import tomllib
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .catalog import norm
from .curseforge import API
from .queue import Queue
from .versions import compare_versions

log = logging.getLogger("mod_browser.upstream")
PROJECT_ID = "1518930"
CHECK_AFTER = (4, 0)
_BASE_ORIGINS = ("base",)
_VERSION_START = re.compile(r"[-_+ ]v?\d")


@dataclass(frozen=True)
class UpstreamMod:
    project_id: str
    file_id: str
    name: str
    file_name: str
    version: str
    file_hash: str | None
    hash_format: str | None


@dataclass(frozen=True)
class Release:
    file_id: str
    title: str
    version: str
    published_at: str
    mods: tuple[UpstreamMod, ...]


@dataclass(frozen=True)
class PackMod:
    path: str            # the metafile, relative to the pack root
    name: str
    file_name: str
    version: str
    side: str
    origin: str          # base | extra | added
    source: str | None   # curseforge | modrinth | None
    project_id: str | None
    file_id: str | None


def version_of(file_name: str) -> str:
    """The version part of a jar name, from the first `-1.2` / `_v1.2` on: create_connected-1.3.3-mc1.21.1.jar -> 1.3.3-mc1.21.1."""
    base = re.sub(r"\.(jar|zip)(\.disabled)?$", "", file_name, flags=re.I)
    m = re.search(r"[-_+ ]v?(\d.*)$", base)
    return m.group(1) if m else base


def jar_stem(file_name: str) -> str:
    """The jar's name without its version: create_submarine-3.4.5.jar -> createsubmarine."""
    base = re.sub(r"\.(jar|zip)(\.disabled)?$", "", file_name, flags=re.I)
    m = _VERSION_START.search(base)
    return norm(base[:m.start()] if m else base)


def pack_mods(root: Path, entries: list[dict[str, Any]]) -> list[PackMod]:
    """Every mod metafile in the pack, with where it came from (from the catalog's history reading)."""
    by_id = {e["id"]: e for e in entries}
    out = []
    for path in sorted((root / "mods").glob("*.pw.toml")):
        meta = tomllib.loads(path.read_text())
        update = meta.get("update", {})
        source = "curseforge" if "curseforge" in update else "modrinth" if "modrinth" in update else None
        cf = update.get("curseforge", {})
        entry = by_id.get(path.name.removesuffix(".pw.toml"), {})
        out.append(PackMod(
            path=path.relative_to(root).as_posix(), name=meta.get("name", path.stem), file_name=meta.get("filename", ""),
            version=version_of(meta.get("filename", "")), side=meta.get("side", "both"), origin=entry.get("origin", "base"),
            source=source, project_id=str(cf["project-id"]) if "project-id" in cf else None,
            file_id=str(cf["file-id"]) if "file-id" in cf else None))
    return out


@dataclass
class Diff:
    added: list[dict[str, Any]] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)
    changed: list[dict[str, Any]] = field(default_factory=list)
    kept: list[dict[str, Any]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    def summary(self) -> dict[str, Any]:
        return {"added": self.added, "removed": self.removed, "changed": self.changed, "kept": self.kept}


def _op(mod: UpstreamMod, op: str, **extra: Any) -> dict[str, Any]:
    return {"op": op, "name": mod.name, "source": "curseforge", "projectId": mod.project_id, "versionId": mod.file_id,
            "versionNumber": mod.version, "channel": "release", "fileName": mod.file_name, "fileHash": mod.file_hash,
            "hashFormat": mod.hash_format, "side": "both", "reason": "upstream", **extra}


def diff_release(pack: list[PackMod], release: Release) -> tuple[Diff, list[dict[str, Any]]]:
    """(the difference, and the pack operations that would make the pack match upstream)."""
    by_project = {(m.source, m.project_id): m for m in pack if m.project_id}
    by_stem: dict[str, PackMod] = {}
    for m in pack:
        by_stem.setdefault(jar_stem(m.file_name), m)
    matched: set[str] = set()
    diff, ops = Diff(), []
    for up in release.mods:
        mine = by_project.get(("curseforge", up.project_id)) or by_stem.get(jar_stem(up.file_name))
        if mine is None:
            diff.added.append({"name": up.name, "version": up.version})
            ops.append(_op(up, "add"))
            continue
        matched.add(mine.path)
        if mine.origin not in _BASE_ORIGINS:
            # The owner's own pin: never touched, whichever side is newer.
            diff.kept.append({"name": mine.name, "pack": mine.version, "upstream": up.version,
                              "upstreamNewer": compare_versions(up.version, mine.version) > 0})
        elif mine.file_id == up.file_id or (mine.file_id is None and mine.version == up.version):
            continue
        else:
            diff.changed.append({"name": up.name, "from": mine.version, "to": up.version})
            ops.append(_op(up, "change", metafile=mine.path, side=mine.side, replaces=mine.version))
    for mine in pack:
        if mine.path not in matched and mine.origin in _BASE_ORIGINS and mine.source == "curseforge":
            diff.removed.append({"name": mine.name, "version": mine.version})
            ops.append({"op": "remove", "name": mine.name, "source": "curseforge", "projectId": mine.project_id,
                        "versionId": mine.file_id, "versionNumber": mine.version, "channel": "release",
                        "fileName": mine.file_name, "metafile": mine.path, "side": mine.side, "reason": "upstream"})
    return diff, ops


class CurseForgeUpstream:
    """Reads the modpack's newest release from CurseForge. Needs the API key."""

    def __init__(self, http: Any, api_key: str, project_id: str = PROJECT_ID):
        self.http, self.project_id = http, project_id
        self._headers = {"x-api-key": api_key, "Accept": "application/json"}

    def _chunks(self, ids: list[str], size: int = 100):
        for i in range(0, len(ids), size):
            yield [int(x) for x in ids[i:i + size]]

    def latest(self) -> Release:
        files = self.http.json(f"{API}/mods/{self.project_id}/files?pageSize=50", self._headers).get("data") or []
        releases = sorted((f for f in files if f.get("releaseType") == 1 and f.get("isAvailable") is not False),
                          key=lambda f: f.get("fileDate", ""), reverse=True)
        if not releases:
            raise LookupError("CurseForge lists no release of the modpack")
        newest = releases[0]
        if not newest.get("downloadUrl"):
            raise LookupError("CurseForge gives no download for the newest release")
        with self.http.fetch_jar(newest["downloadUrl"]) as pack_zip, zipfile.ZipFile(pack_zip) as zf:
            with zf.open("manifest.json") as entry:
                raw = entry.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise LookupError("the modpack's manifest is too large")
        entries = [(str(f["projectID"]), str(f["fileID"])) for f in json.loads(raw).get("files", [])]
        file_info: dict[str, dict[str, Any]] = {}
        for chunk in self._chunks([fid for _, fid in entries]):
            for f in self.http.post_json(f"{API}/mods/files", {"fileIds": chunk}, self._headers).get("data") or []:
                file_info[str(f["id"])] = f
        names: dict[str, str] = {}
        for chunk in self._chunks(sorted({pid for pid, _ in entries})):
            for m in self.http.post_json(f"{API}/mods", {"modIds": chunk}, self._headers).get("data") or []:
                names[str(m["id"])] = str(m.get("name") or "")
        mods = []
        for pid, fid in entries:
            info = file_info.get(fid)
            if info is None:
                raise LookupError(f"CurseForge does not know file {fid} of mod {pid}")
            sha1 = next((h.get("value") for h in info.get("hashes") or [] if h.get("algo") == 1), None)
            file_name = str(info.get("fileName") or "")
            mods.append(UpstreamMod(pid, fid, names.get(pid) or str(info.get("displayName") or file_name), file_name,
                                    version_of(file_name), sha1, "sha1" if sha1 else None))
        return Release(str(newest["id"]), str(newest.get("displayName") or newest.get("fileName") or ""),
                       version_of(str(newest.get("fileName") or "")) or str(newest.get("displayName") or ""),
                       str(newest.get("fileDate") or ""), tuple(mods))


class UpstreamChecker:
    def __init__(self, queue: Queue, source: Any | None, pack: Any, *, tz: str = "America/New_York",
                 now: Callable[[], datetime] | None = None, project_id: str = PROJECT_ID):
        self.queue, self.source, self.pack, self.project_id = queue, source, pack, project_id
        self._tz = ZoneInfo(tz)
        self._now = now or (lambda: datetime.now(self._tz))
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"configured": source is not None, "lastCheckedOn": None, "lastResult": None,
                                       "error": None, "errorReportedOn": None, "latest": None}

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def _set(self, **changes: Any) -> None:
        with self._lock:
            self._state.update(changes)

    def check(self) -> dict[str, Any]:
        """One check. Safe to call twice: a release already seen (in any state) is never requested again."""
        today = self._now().date().isoformat()
        if self.source is None:
            self._set(lastResult="not-configured")
            return self.status()
        try:
            release = self.source.latest()
        except Exception as exc:  # noqa: BLE001 - an outage is a report, not a crash
            message = f"{type(exc).__name__}: {exc}"
            # Once a day at most, even across restarts: the log line is what the host's alerting reads.
            first_today = self.queue.kv_get("upstream.errorReportedOn") != today
            if first_today:
                self.queue.kv_set("upstream.errorReportedOn", today)
                log.error("upstream check failed: %s", message)
            self._set(lastCheckedOn=today, lastResult="error", error=message, errorReportedOn=today)
            return self.status()
        self._set(lastCheckedOn=today, error=None, latest={"fileId": release.file_id, "version": release.version})
        known = [r for r in self.queue.of_kind("upstream") if r.version_id == release.file_id]
        if known:
            self._set(lastResult="up-to-date")
            return self.status()
        catalog = self.pack.catalog()
        diff, ops = diff_release(pack_mods(self.pack.root, catalog["mods"]), release)
        # A request for an older release is replaced, not left to be approved.
        for old in self.queue.of_kind("upstream"):
            if old.state in ("pending", "approved", "queued"):
                self.queue.deny(old.id, "A newer upstream release replaced this one.")
        if diff.empty:
            self._set(lastResult="nothing-to-change")
            return self.status()
        self.queue.create_upstream(self.project_id, release.file_id, f"Upstream update: {release.title}", release.version, ops, {
            "baseCommit": catalog["pack"]["commit"], "release": {"fileId": release.file_id, "title": release.title,
                                                                  "publishedAt": release.published_at},
            "diff": diff.summary()})
        self._set(lastResult="new-request")
        return self.status()

    def run_forever(self, stop: threading.Event, every: float = 900.0) -> None:
        while not stop.wait(every):
            now = self._now()
            due = now.replace(hour=CHECK_AFTER[0], minute=CHECK_AFTER[1], second=0, microsecond=0)
            if now >= due and self.status()["lastCheckedOn"] != now.date().isoformat():
                try:
                    self.check()
                except Exception:  # noqa: BLE001
                    log.exception("upstream check crashed")
