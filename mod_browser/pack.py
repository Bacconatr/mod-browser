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

import os
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
_BLOCKED_SECTION = re.compile(r"^## [^\n]*distribution[- ]blocked[^\n]*$(.*?)(?=^## |\Z)", re.M | re.S | re.I)
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


def _curseforge_file(meta: dict[str, Any]) -> str | None:
    if (meta.get("download") or {}).get("mode") != "metadata:curseforge":
        return None
    file_id = ((meta.get("update") or {}).get("curseforge") or {}).get("file-id")
    return str(file_id) if isinstance(file_id, int) else None


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


def blocked_from_notes(notes: str) -> set[str]:
    """Metafile paths the pack's NOTES.md lists as distribution-blocked: their author forbids
    hosting outside CurseForge, so a player's launcher asks them to download each by hand."""
    section = _BLOCKED_SECTION.search(notes)
    return set(_METAFILE.findall(section.group(1))) if section else set()


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
        self._changes_lock = threading.Lock()

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
        notes = notes_file.read_text() if notes_file.exists() else ""
        extras = extras_from_notes(notes)
        blocked = blocked_from_notes(notes)
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
                # Listed in the notes, and still a CurseForge-only entry: one now fetched from
                # Modrinth is not a download by hand any more.
                "manual": path in blocked and _curseforge_file(meta) is not None,
                # A metadata:curseforge entry's file; /api/setup asks CurseForge which of these it
                # hands out no URL for (the author blocks outside downloads), so a player is told.
                "curseforgeFile": _curseforge_file(meta),
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


    # ---- what changed since a date: the whole pack diffed, not just additions ----
    def _first_parent(self) -> list[tuple[str, str, str]]:
        """(sha, commit day in its own zone, subject), newest first, along main's first-parent line."""
        log = self._git("log", "--first-parent", "--format=%H%x1f%cI%x1f%s")
        return [tuple(line.split("\x1f", 2)) for line in log.splitlines() if line]  # type: ignore[misc]

    def _blobs(self, rev: str, paths: list[str]) -> dict[str, str]:
        """Each path's text at `rev`, in one `git cat-file --batch`; a path missing there is left out."""
        if not paths:
            return {}
        out = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", str(self.root), "cat-file", "--batch"],
            input="".join(f"{rev}:{p}\n" for p in paths).encode(), check=True, capture_output=True, timeout=20,
        ).stdout
        found, at = {}, 0
        for path in paths:
            end = out.index(b"\n", at)
            header = out[at:end].decode().split()
            at = end + 1
            if len(header) < 3 or header[1] == "missing":
                continue
            size = int(header[2])
            found[path] = out[at:at + size].decode("utf-8", "replace")
            at += size + 1
        return found

    def _state(self, rev: str) -> dict[str, Any]:
        """The pack's versions and every mod's name and filename at `rev`."""
        top = self._blobs(rev, ["pack.toml"])
        pack = tomllib.loads(top.get("pack.toml", ""))
        index_file = pack.get("index", {}).get("file", "index.toml")
        index = tomllib.loads(self._blobs(rev, [index_file]).get(index_file, ""))
        paths = [f.get("file", "") for f in index.get("files", [])
                 if f.get("metafile") or f.get("file", "").endswith(".pw.toml")]
        mods = {}
        for path, text in self._blobs(rev, paths).items():
            meta = tomllib.loads(text)
            mods[path] = {"name": meta.get("name", meta.get("filename", path)), "filename": meta.get("filename", "")}
        versions = pack.get("versions", {})
        return {"version": pack.get("version"), "minecraft": versions.get("minecraft"),
                "neoforge": versions.get("neoforge"), "mods": mods}

    def changes(self, since: str) -> dict[str, Any]:
        """Everything that differs between the pack as it stood before `since` (a YYYY-MM-DD day, in each
        commit's own zone, as the catalog's addedOn is) and the pack now: added, updated and removed mods,
        the pack's own version, and the commits in between. Before the pack's first commit, the first
        commit is the baseline, so the base pack never reads as "added"."""
        self.catalog()  # raises PackUnreadable the same way the rest of the API does
        commits = self._first_parent()
        head = commits[0][0]
        before = [c for c in commits if c[1][:10] < since]
        base = before[0][0] if before else commits[-1][0]
        key = (head, base)
        with self._changes_lock:
            return {"since": since, **self._diff(key, commits, base, head)}

    def _diff(self, key: tuple[str, str], commits: list[tuple[str, str, str]],
              base: str, head: str) -> dict[str, Any]:
        if getattr(self, "_changes_key", None) != key:
            old, new = self._state(base), self._state(head)
            minecraft = new["minecraft"] or "1.21.1"
            history, _ = self._history()

            def versions(before: str, after: str) -> tuple[str, str]:
                """Two readable versions; failing a parse, the filenames with what they share trimmed off."""
                a, b = version_from_filename(before, minecraft), version_from_filename(after, minecraft)
                if a and b and a != b:
                    return a, b
                lead = len(os.path.commonprefix([before, after]))
                lead = max(before.rfind("-", 0, lead), before.rfind("_", 0, lead), before.rfind(" ", 0, lead), -1) + 1
                tail = len(os.path.commonprefix([before[lead:][::-1], after[lead:][::-1]]))
                return before[lead:len(before) - tail] or before, after[lead:len(after) - tail] or after

            added, updated, removed = [], [], []
            for path, mod in new["mods"].items():
                was = old["mods"].get(path)
                if was is None:
                    commit = history.get(path, {})
                    added.append({"name": mod["name"], "on": commit.get("on"), "by": commit.get("by")})
                elif was["filename"] != mod["filename"]:
                    frm, to = versions(was["filename"], mod["filename"])
                    updated.append({"name": mod["name"], "from": frm, "to": to})
            for path, mod in old["mods"].items():
                if path not in new["mods"]:
                    removed.append({"name": mod["name"]})
            for group in (added, updated, removed):
                group.sort(key=lambda m: m["name"].lower())
            pack = {k: {"from": old[k], "to": new[k]} for k in ("version", "neoforge", "minecraft") if old[k] != new[k]}
            # The commits after the baseline, newest first: what a player would call "the updates". A batch
            # lands as a merge ("Apply batch 1"), so it is the commits inside it that say what happened.
            events = []
            if base != head:
                log = self._git("log", "--no-merges", "-n", "20", "--format=%x1e%cI%x1f%s%x1f%B", f"{base}..{head}")
                for record in log.split("\x1e")[1:]:
                    when, subject, body = record.split("\x1f", 2)
                    trailer = _TRAILER.search(body)
                    events.append({"on": when[:10], "title": re.sub(r"^Update to (?=Upstream update)", "", subject),
                                   "by": trailer.group(1) if trailer else None})
            self._changes_key, self._changes = key, {
                "added": added, "updated": updated, "removed": removed, "pack": pack, "events": events,
                "latest": commits[0][1][:10]}
        data = self._changes
        changed = bool(data["added"] or data["updated"] or data["removed"] or data["pack"])
        return {"changed": changed, **data,
                "counts": {k: len(data[k]) for k in ("added", "updated", "removed")}}


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
