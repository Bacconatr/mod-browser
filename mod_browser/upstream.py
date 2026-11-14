"""Upstream releases of All of Create: Aeronautics, offered to the owner as updates.

A daily check reads the newest release of the CurseForge modpack (project
1518930), downloads its `manifest.json`, and compares it with the pack mod by
mod. A newer release becomes ONE owner-only request, `pending`, showing the
difference: added, removed, changed (version from -> to), and kept. It does
nothing until the owner approves it, and then it goes through the same
applier as any request (test boot, backup, rollback). Never automatic.

Matching a pack mod to an upstream mod: by CurseForge project id, over every
metafile in the pack (resource packs, shaders and data packs too, wherever the
pack keeps them); failing that by the jar's name without its version, because
the owner's own pins are often Modrinth files (Create Deep Seas) that
CurseForge knows under another id. The name match is only for a metafile with
no CurseForge id, and only between files of one kind (jar or not): a metafile
that names a CurseForge project is that project and no other. v3.0HF (Homelab
#1789) is why: the comparison saw only mods/, so 37 resource and data packs
came in again as new mods, and "Boatload ZH_HK Translation" matched the
Boatload mod by name and replaced it.

A project upstream adds goes in the folder its CurseForge class belongs in:
mods in mods/, other types where the pack already keeps that type (learned
from the projects of that class it already has), else resourcepacks/ or
shaderpacks/. A type the pack keeps nowhere is left for the owner: the applier
refuses it rather than guess.

What the pack's owner pinned is never touched. A mod the pack holds as an
extra, or that was added later, is `kept` even when upstream ships a newer (or
older) build: Deep Seas 3.4.5 stays against a bundled 3.3.0. Only mods that
came with the base pack are changed or removed, and only those that came from
CurseForge: a base mod with no upstream match is reported `removed` because the
new release dropped it, never because a pin replaced it.

A file whose author blocks outside downloads (CurseForge gives it no URL) is
written as a Modrinth entry when Modrinth hosts the byte-identical file (same
sha1), so packwiz-installer downloads it instead of asking every player to fetch
it by hand. On 2026-11-14 that was 10 of the pack's 13 blocked files, Create
Aeronautics among them; an update must not put them back behind CurseForge.
The entry keeps its CurseForge ids too, so the next release matches it as
before. The sha1 match proves the bytes, not who runs the Modrinth project:
its [update.modrinth] id is followed by nothing automatic here, and the 10
twins of 2026-11-14 were checked by hand to be their authors' own projects.

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
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .catalog import norm
from .curseforge import API
from .modrinth import API as MODRINTH_API
from .packrepo import metafiles
from .queue import Queue
from .versions import compare_versions

log = logging.getLogger("mod_browser.upstream")
PROJECT_ID = "1518930"
CHECK_AFTER = (4, 0)
_BASE_ORIGINS = ("base",)
_VERSION_START = re.compile(r"[-_+ ]v?\d")
#: CurseForge's Minecraft classes, and the folder each goes in when the pack holds none of that class yet.
MOD_CLASS = 6
CLASS_NAMES = {6: "mod", 12: "resource pack", 6552: "shader pack", 6945: "data pack"}
DEFAULT_FOLDERS = {6: "mods", 12: "resourcepacks", 6552: "shaderpacks"}


@dataclass(frozen=True)
class UpstreamMod:
    project_id: str
    file_id: str
    name: str
    file_name: str
    version: str
    file_hash: str | None
    hash_format: str | None
    #: For a file CurseForge gives no URL: Modrinth's copy of the same bytes (projectId, versionId, url).
    modrinth: tuple[str, str, str] | None = None
    #: CurseForge's class for the project (6 mod, 12 resource pack, 6552 shader, 6945 data pack); None if unknown.
    class_id: int | None = None


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

    @property
    def folder(self) -> str:
        """The folder the metafile is in: "mods", "resourcepacks", "" for the pack root."""
        return self.path.rsplit("/", 1)[0] if "/" in self.path else ""

    @property
    def misplaced(self) -> bool:
        """Something that is not a jar, in mods/: v3.0HF's duplicates (#1789)."""
        return self.folder == "mods" and not _is_jar(self.file_name)


def _is_jar(file_name: str) -> bool:
    return bool(re.search(r"\.jar(\.disabled)?$", file_name, flags=re.I))


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
    """Every metafile in the pack, in any folder (mods, resource packs, shaders, data packs), with where it
    came from (from the catalog's history reading)."""
    by_path = {e["path"]: e for e in entries if e.get("path")}
    out = []
    for path in metafiles(root):
        meta = tomllib.loads(path.read_text())
        update = meta.get("update", {})
        source = "curseforge" if "curseforge" in update else "modrinth" if "modrinth" in update else None
        cf = update.get("curseforge", {})
        entry = by_path.get(path.relative_to(root).as_posix(), {})
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
    # A Modrinth twin keeps its CurseForge identity ([update.curseforge] beside [update.modrinth]),
    # so a later release still matches it by project id, and can change or remove it.
    where = ({"source": "modrinth", "projectId": mod.modrinth[0], "versionId": mod.modrinth[1], "downloadUrl": mod.modrinth[2],
              "curseforge": {"projectId": mod.project_id, "fileId": mod.file_id}}
             if mod.modrinth else {"source": "curseforge", "projectId": mod.project_id, "versionId": mod.file_id})
    return {"op": op, "name": mod.name, **where,
            "versionNumber": mod.version, "channel": "release", "fileName": mod.file_name, "fileHash": mod.file_hash,
            "hashFormat": mod.hash_format, "side": "both", "reason": "upstream", **extra}


def folders(pack: list[PackMod], release: Release) -> dict[int, str]:
    """CurseForge class -> the folder a new project of that class goes in: where the pack already keeps
    that class (most of its projects of that class, never mods/ for a non-mod), else the default."""
    classes = {up.project_id: up.class_id for up in release.mods if up.class_id is not None}
    seen: dict[int, Counter[str]] = {}
    for m in pack:
        cls = classes.get(m.project_id or "")
        if cls is not None and cls != MOD_CLASS and m.folder != "mods":
            seen.setdefault(cls, Counter())[m.folder] += 1
    out = dict(DEFAULT_FOLDERS)
    for cls, counts in seen.items():
        out[cls] = min(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    out[MOD_CLASS] = "mods"
    return out


def _folder_of(up: UpstreamMod, known: dict[int, str]) -> str | None:
    if up.class_id is None:  # CurseForge did not say: a jar is a mod; anything else is left to the owner
        return "mods" if _is_jar(up.file_name) else None
    return known.get(up.class_id)


def diff_release(pack: list[PackMod], release: Release) -> tuple[Diff, list[dict[str, Any]]]:
    """(the difference, and the pack operations that would make the pack match upstream)."""
    # A project the pack lists twice (v3.0HF's copies in mods/, #1789) matches the copy in its own folder.
    by_project: dict[tuple[str | None, str], PackMod] = {}
    for m in sorted(pack, key=lambda m: m.misplaced):
        if m.project_id:
            by_project.setdefault(("curseforge", m.project_id), m)
    # By name only a metafile that names no CurseForge project: one that does is that project and no other.
    by_stem: dict[tuple[str, bool], PackMod] = {}
    for m in pack:
        if m.project_id is None:
            by_stem.setdefault((jar_stem(m.file_name), _is_jar(m.file_name)), m)
    known_folders = folders(pack, release)
    matched: set[str] = set()
    diff, ops = Diff(), []
    for up in release.mods:
        mine = by_project.get(("curseforge", up.project_id))
        if mine is None:
            mine = by_stem.get((jar_stem(up.file_name), _is_jar(up.file_name)))
            if mine is not None and mine.path in matched:
                mine = None  # already another upstream project's
        if mine is None:
            diff.added.append({"name": up.name, "version": up.version})
            ops.append(_op(up, "add", folder=_folder_of(up, known_folders), type=CLASS_NAMES.get(up.class_id or 0)))
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
    # A non-jar in mods/ whose project the pack also keeps in its own folder is a second copy (v3.0HF wrote 37,
    # #1789): it goes, whoever added it. That is how the next update repairs the pack through the runner.
    proper = {m.project_id for m in pack if m.project_id and not m.misplaced}
    for mine in pack:
        if mine.path in matched or mine.source != "curseforge":
            continue
        duplicate = mine.misplaced and mine.project_id in proper
        # Otherwise only base mods are removed: a resource or data pack upstream stops listing stays for the owner.
        if duplicate or (mine.origin in _BASE_ORIGINS and mine.folder == "mods"):
            diff.removed.append({"name": mine.name, "version": f"{mine.version} (a second copy in mods/)" if duplicate
                                 else mine.version})
            ops.append({"op": "remove", "name": mine.name, "source": "curseforge", "projectId": mine.project_id,
                        "versionId": mine.file_id, "versionNumber": mine.version, "channel": "release",
                        "fileName": mine.file_name, "metafile": mine.path, "side": mine.side, "reason": "upstream"})
    return diff, ops


class CurseForgeUpstream:
    """Reads the modpack's newest release from CurseForge. Needs the API key."""

    def __init__(self, http: Any, api_key: str, project_id: str = PROJECT_ID):
        self.http, self.project_id = http, project_id
        self._headers = {"x-api-key": api_key, "Accept": "application/json"}

    def _modrinth_twins(self, sha1s: list[str]) -> dict[str, tuple[str, str, str]]:
        """sha1 -> (project, version, url) for the blocked files Modrinth hosts byte for byte.
        Modrinth not answering only means the files stay CurseForge entries, as before."""
        if not sha1s:
            return {}
        try:
            found = self.http.post_json(f"{MODRINTH_API}/version_files",  # never with the CurseForge key
                                        {"hashes": sha1s, "algorithm": "sha1"})
            out: dict[str, tuple[str, str, str]] = {}
            for sha1, v in (found.items() if isinstance(found, dict) else []):
                if sha1 not in sha1s or not isinstance(v, dict):
                    continue
                f = next((f for f in v.get("files") or [] if isinstance(f, dict)
                          and isinstance(f.get("hashes"), dict) and f["hashes"].get("sha1") == sha1), None)
                if f and f.get("url") and v.get("project_id") and v.get("id"):
                    out[sha1] = (str(v["project_id"]), str(v["id"]), str(f["url"]))
            return out
        except Exception:  # noqa: BLE001 - a malformed answer is the same as no answer
            log.warning("Modrinth did not answer the blocked-file lookup usably; those files stay CurseForge entries")
            return {}

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
        classes: dict[str, int] = {}
        for chunk in self._chunks(sorted({pid for pid, _ in entries})):
            for m in self.http.post_json(f"{API}/mods", {"modIds": chunk}, self._headers).get("data") or []:
                names[str(m["id"])] = str(m.get("name") or "")
                if isinstance(m.get("classId"), int) and not isinstance(m["classId"], bool):
                    classes[str(m["id"])] = m["classId"]
        mods = []
        sha1s: dict[str, str | None] = {}
        for pid, fid in entries:
            info = file_info.get(fid)
            if info is None:
                raise LookupError(f"CurseForge does not know file {fid} of mod {pid}")
            sha1s[fid] = next((h.get("value") for h in info.get("hashes") or [] if h.get("algo") == 1), None)
        # A file CurseForge hands out no URL for: its author blocks outside downloads.
        blocked = {fid: sha1 for fid, sha1 in sha1s.items() if sha1 and not file_info[fid].get("downloadUrl")}
        twins = self._modrinth_twins(sorted(set(blocked.values())))
        for pid, fid in entries:
            info, sha1 = file_info[fid], sha1s[fid]
            file_name = str(info.get("fileName") or "")
            mods.append(UpstreamMod(pid, fid, names.get(pid) or str(info.get("displayName") or file_name), file_name,
                                    version_of(file_name), sha1, "sha1" if sha1 else None,
                                    twins.get(blocked[fid]) if fid in blocked else None, classes.get(pid)))
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
