"""The pack's mod list, read from the packwiz repo (mounted read-only).

Every `*.pw.toml` metafile is one entry. Who added it and when come from the
pack's git history: the latest commit that added the metafile. A commit body
may carry a `Requested-by: <name>` trailer (the request queue will write
one); otherwise the commit's author is named.

The repo's first commit holds the whole pack as it was migrated, the base
pack plus the mods that had been pinned on top of it before packwiz. Those
two are told apart by the pack's NOTES.md, whose "Mods added on top of"
section names each extra's metafile and usually the date it was asked for.

Rebuilt only when the repo's HEAD moves, checked at most every `max_age`
seconds.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
import tomllib
from pathlib import Path
from typing import Any, Callable

_KINDS = {"mods": "mod", "resourcepacks": "resource pack", "shaderpacks": "shader pack"}
_LOADERS = {"neoforge", "forge", "fabric", "quilt", "mc"}
_VERSIONISH = re.compile(r"^[vr]?\d+(?:\.\d+)*[a-z]*(?:\+[\w.]+)*$", re.I)
_QUALIFIER = re.compile(r"^(?:alpha|beta|rc\d*|pre\d*|snapshot|hotfix\d*)$", re.I)
_DATE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
_TRAILER = re.compile(r"^Requested-by:\s*(.+?)\s*$", re.M | re.I)
_EXTRA_SECTION = re.compile(r"^## Mods added on top of.*?$(.*?)(?=^## |\Z)", re.M | re.S)
_METAFILE = re.compile(r"`((?:[\w.-]+/)*[\w.-]+\.pw\.toml)`")


def version_from_filename(filename: str, minecraft: str = "1.21.1") -> str | None:
    """Best guess at a mod's own version from its jar name.

    create_connected-1.3.3-mc1.21.1.jar -> 1.3.3
    DnDesires-1.21.1-2.3a-BETA.jar      -> 2.3a-BETA
    """
    stem = re.sub(r"\.(jar|zip)$", "", filename, flags=re.I)
    major = ".".join(minecraft.split(".")[:2])
    skip = _LOADERS | {minecraft, major, "mc" + minecraft, "mc" + major}
    tokens = [t for t in re.split(r"[-_ ]+", stem) if t and t.lower() not in skip]
    for index, token in enumerate(tokens):
        if index and _VERSIONISH.match(token) and any(c.isdigit() for c in token):
            version = token
            for extra in tokens[index + 1:]:
                if not _QUALIFIER.match(extra):
                    break
                version += "-" + extra
            return version
    return None


def _source(meta: dict[str, Any]) -> dict[str, Any]:
    update = meta.get("update") or {}
    if "modrinth" in update:
        mod_id = update["modrinth"].get("mod-id", "")
        return {"site": "Modrinth", "url": f"https://modrinth.com/project/{mod_id}" if mod_id else None}
    if "curseforge" in update:
        project = update["curseforge"].get("project-id")
        return {"site": "CurseForge", "url": f"https://www.curseforge.com/projects/{project}" if project else None}
    return {"site": "Direct download", "url": None}


def extras_from_notes(notes: str) -> dict[str, str | None]:
    """Metafile path -> the first date its note mentions, for the pre-packwiz extras."""
    section = _EXTRA_SECTION.search(notes)
    if not section:
        return {}
    extras: dict[str, str | None] = {}
    # One "### `<jar>`" block per extra: a side line naming the metafile, then its note.
    for block in re.split(r"^### ", section.group(1), flags=re.M)[1:]:
        paths = _METAFILE.findall(block)
        if not paths:
            continue
        date = _DATE.search(block)
        extras[paths[0]] = date.group(1) if date else None
    return extras


class PackUnreadable(Exception):
    """The pack repo could not be read, and no earlier read succeeded."""


class PackCatalog:
    def __init__(self, root: Path, max_age: float = 30.0,
                 clock: Callable[[], float] = time.monotonic):
        self.root, self.max_age, self._clock = root, max_age, clock
        self._lock = threading.Lock()
        self._head: str | None = None
        self._checked = float("-inf")
        self._cache: dict[str, Any] | None = None
        self._error: Exception | None = None

    def _git(self, *args: str) -> str:
        # safe.directory: the repo is root's, mounted read-only, and the app
        # runs unprivileged, which git otherwise refuses as dubious ownership.
        return subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", str(self.root), *args],
            check=True, capture_output=True, text=True, timeout=20,
        ).stdout

    def catalog(self) -> dict[str, Any]:
        """The current catalog. A failed read is remembered for `max_age` too,
        so a broken pack costs one git run per interval, not one per request;
        the last good catalog keeps being served meanwhile, if there is one."""
        with self._lock:
            if self._clock() - self._checked >= self.max_age:
                self._checked = self._clock()
                try:
                    head = self._git("rev-parse", "HEAD").strip()
                    if head != self._head or self._cache is None:
                        self._cache = self._build(head)
                        self._head = head
                    self._error = None
                except Exception as exc:
                    self._error = exc
            if self._cache is None:
                raise PackUnreadable(str(self._error))
            return self._cache

    def _history(self) -> tuple[dict[str, dict[str, Any]], str | None]:
        """Path -> the latest commit that added it, and the root commit's sha."""
        log = self._git("log", "--diff-filter=A", "--no-renames", "--name-only",
                        "--format=%x1e%H%x1f%P%x1f%an%x1f%aI%x1f%B%x1f")
        added: dict[str, dict[str, Any]] = {}
        root = None
        for record in log.split("\x1e")[1:]:
            sha, parents, author, date, body, files = record.split("\x1f", 5)
            if not parents.strip():
                root = sha
            trailer = _TRAILER.search(body)
            commit = {"sha": sha, "by": trailer.group(1) if trailer else author, "on": date[:10]}
            for path in (line.strip() for line in files.splitlines()):
                if not path:
                    continue
                added.setdefault(path, commit)  # log is newest first
        return added, root

    def _build(self, head: str) -> dict[str, Any]:
        pack = tomllib.loads((self.root / "pack.toml").read_text())
        index = tomllib.loads((self.root / pack.get("index", {}).get("file", "index.toml")).read_text())
        notes_file = self.root / "NOTES.md"
        extras = extras_from_notes(notes_file.read_text()) if notes_file.exists() else {}
        added, root_sha = self._history()
        base_name = f"{pack.get('name', 'the base pack')}"
        owner = pack.get("author") or "the owner"

        minecraft = pack.get("versions", {}).get("minecraft", "1.21.1")
        entries = []
        for item in index.get("files", []):
            path = item.get("file", "")
            if not item.get("metafile") and not path.endswith(".pw.toml"):
                continue
            meta = tomllib.loads((self.root / path).read_text())
            commit = added.get(path)
            if commit is None or commit["sha"] == root_sha:
                if path in extras:
                    origin, by, on = "extra", owner, extras[path]
                else:
                    origin, by, on = "base", None, None
            else:
                origin, by, on = "added", commit["by"], commit["on"]
            folder = path.split("/", 1)[0] if "/" in path else ""
            filename = meta.get("filename", "")
            entries.append({
                "id": path.rsplit("/", 1)[-1].removesuffix(".pw.toml"),
                "name": meta.get("name", filename),
                "filename": filename,
                "version": version_from_filename(filename, minecraft),
                "side": meta.get("side", "both"),
                "kind": _KINDS.get(folder, "data pack"),
                "source": _source(meta),
                "origin": origin,
                "addedBy": by,
                "addedOn": on,
            })
        entries.sort(key=lambda e: e["name"].lower())
        versions = pack.get("versions", {})
        return {
            "pack": {
                "name": base_name,
                "version": pack.get("version"),
                "minecraft": versions.get("minecraft"),
                "neoforge": versions.get("neoforge"),
                "commit": head[:12],
            },
            "count": len(entries),
            "mods": entries,
        }


def search(entries: list[dict[str, Any]], query: str = "", side: str = "", kind: str = "") -> list[dict[str, Any]]:
    """Case-insensitive match on every word of `query` against name, id and filename."""
    words = query.lower().split()
    found = []
    for entry in entries:
        if side and entry["side"] != side:
            continue
        if kind and entry["kind"] != kind:
            continue
        haystack = " ".join((entry["name"], entry["id"], entry["filename"])).lower()
        if all(word in haystack for word in words):
            found.append(entry)
    return found
