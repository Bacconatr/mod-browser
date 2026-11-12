"""Both catalogs behind one port, and the readers of jars and of the installed set.

One catalog being down is a warning, not an outage.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Any

from .catalog import InstalledMod, Project, Version, norm
from .jar import JarError, JarInfo, read_jar_info

log = logging.getLogger("mod_browser")
_ID = re.compile(r"^(?:curseforge:)?(\d{3,9})$", re.I)
_SLUG_URL = re.compile(r"curseforge\.com/minecraft/mc-mods/([a-z0-9-]+)", re.I)
_ASK_FOR_ID = "Paste the project ID from the mod's CurseForge page (About panel, right side) instead."


class CompositeCatalog:
    """Modrinth plus, when a key is configured, CurseForge.

    `curseforge_search` is off in production: CurseForge's /v1/mods/search
    returns 403 for our key (Homelab #1680, 2026-11-12), while `/mods/{id}`
    and `/mods/{id}/files` work. So a CurseForge-only mod comes in by pasted
    project id. Turn it on when search starts answering 200.
    """

    def __init__(self, modrinth: Any, curseforge: Any | None, curseforge_search: bool = False):
        self.modrinth, self.curseforge, self.curseforge_search = modrinth, curseforge, curseforge_search

    def _pasted(self, text: str) -> tuple[list[Project], list[str]] | None:
        m_id, m_slug = _ID.match(text.strip()), _SLUG_URL.search(text)
        if not m_id and not m_slug:
            return None
        if self.curseforge is None:
            return [], ["CurseForge is not configured, so a CurseForge mod cannot be looked up."]
        if m_id:
            try:
                return [self.curseforge.project(m_id.group(1))], []
            except Exception:  # noqa: BLE001 - any failure reads as "no such mod" to a friend
                log.exception("CurseForge lookup of %s failed", m_id.group(1))
                return [], [f"CurseForge has no mod with ID {m_id.group(1)}."]
        # A slug is only a search variant, which 403s for our key.
        if self.curseforge_search:
            try:
                found = self.curseforge.search(m_slug.group(1))
                exact = [p for p in found if p.slug == m_slug.group(1).lower()]
                if exact:
                    return exact[:1], []
            except Exception:  # noqa: BLE001
                log.exception("CurseForge slug lookup failed")
        return [], [f"That link could not be looked up. {_ASK_FOR_ID}"]

    def search(self, query: str) -> tuple[list[Project], list[str]]:
        pasted = self._pasted(query)
        if pasted is not None:
            return pasted
        asked: list[tuple[str, Any]] = [("Modrinth", self.modrinth)]
        warnings: list[str] = []
        if self.curseforge is not None and self.curseforge_search:
            asked.append(("CurseForge", self.curseforge))
        elif self.curseforge is None:
            warnings.append("CurseForge is not configured, so only Modrinth was searched.")
        projects: list[Project] = []
        for name, catalog in asked:
            try:
                projects.extend(catalog.search(query))
            except Exception:  # noqa: BLE001
                log.exception("%s search failed", name)
                warnings.append(f"{name} could not be searched right now.")
        return projects, warnings

    def _of(self, source: str) -> Any:
        if source == "modrinth":
            return self.modrinth
        if self.curseforge is None:
            raise LookupError("CurseForge is not configured")
        return self.curseforge

    def project(self, source: str, project_id: str) -> Project:
        return self._of(source).project(project_id)

    def versions(self, source: str, project_id: str) -> list[Version]:
        return self._of(source).versions(project_id)

    def find_by_mod_id(self, mod_id: str, hints: list[str]) -> list[Project]:
        """Projects that could be the mod with this id.

        Hints (names of projects the platform lists as dependencies) only widen
        the SEARCH: a modId like "functionalstorage" finds nothing on Modrinth
        while "Functional Storage" does. A project is accepted only if it is
        named like the mod id itself.
        """
        want = norm(mod_id)
        found: dict[tuple[str, str], Project] = {}
        for name in dict.fromkeys([*hints[:3], mod_id]):
            projects, _ = self.search(name)
            for p in projects:
                if norm(p.slug) == want or norm(p.name) == want:
                    found[(p.source, p.id)] = p
        return list(found.values())


class HttpJars:
    """A candidate's jar, downloaded once and read for its metadata."""

    def __init__(self, http: Any, size: int = 64):
        self.http, self.size = http, size
        self._cache: dict[tuple[str, str], JarInfo | None] = {}
        self._lock = threading.Lock()

    def info(self, version: Version) -> JarInfo | None:
        if version.blocked or not version.download_url:
            return None
        key = (version.source, version.id)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        try:
            with self.http.fetch_jar(version.download_url) as jar:
                info = read_jar_info(jar)
        except (JarError, OSError, ValueError) as exc:  # FetchError is an Exception, not an OSError
            log.warning("unreadable jar %s: %s", version.file_name, exc)
            info = None
        except Exception:  # noqa: BLE001
            log.exception("fetching jar %s failed", version.file_name)
            return None  # not cached: a network failure is not a property of the jar
        with self._lock:
            if len(self._cache) >= self.size:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = info
        return info


class DirInstalled:
    """What the server has installed: every jar in its mods directory, read for
    its mod ids, versions and declared dependencies. Re-read only for jars that
    are new or changed, so a page view costs a directory listing."""

    def __init__(self, directory: Path):
        self.directory = directory
        self._seen: dict[str, tuple[tuple[int, int], list[InstalledMod]]] = {}
        self._lock = threading.Lock()

    def mods(self) -> list[InstalledMod]:
        with self._lock:
            files = sorted(p for p in self.directory.iterdir() if p.suffix == ".jar" and p.is_file())
            out: list[InstalledMod] = []
            for path in files:
                stat = path.stat()
                sig = (stat.st_size, stat.st_mtime_ns)
                entry = self._seen.get(path.name)
                if entry is None or entry[0] != sig:
                    entry = (sig, self._read(path))
                    self._seen[path.name] = entry
                out.extend(entry[1])
            for gone in set(self._seen) - {p.name for p in files}:
                del self._seen[gone]
            return out

    @staticmethod
    def _read(path: Path) -> list[InstalledMod]:
        try:
            with path.open("rb") as handle:
                info = read_jar_info(handle)
        except (JarError, OSError) as exc:
            log.warning("unreadable installed jar %s: %s", path.name, exc)
            return []
        if info is None:
            return []
        primary = info.own[0].mod_id if info.own else None
        return [InstalledMod(m.mod_id, m.version, path.name,
                             tuple(info.dependencies) if m.mod_id == primary else ())
                for m in info.provides]
