"""Writing the pack: one commit per request, staged on a branch, promoted only once it has booted.

The pack is a packwiz repository. The live server reads its working tree, so
a candidate is NEVER built in it: `stage` makes a git worktree on a throwaway
branch beside it, commits there, and hands the worktree's path to the test
boot. `promote` merges the branch into the live tree (a merge commit, so the
whole batch reverts with one `git revert -m 1`). `discard` removes the
worktree and branch.

Metafiles are written here rather than by the `packwiz` binary, which the
image does not carry (standard library only). They are the same files packwiz
writes: `[download]` with url and hash for Modrinth, `mode =
"metadata:curseforge"` for CurseForge, and an `[update]` table. `index.toml`
is regenerated from the metafiles' hashes and `pack.toml`'s index hash is
refreshed, which is what `packwiz refresh` does.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ENV = {"GIT_AUTHOR_NAME": "mod-browser", "GIT_AUTHOR_EMAIL": "mod-browser@aeronautics.invalid",
        "GIT_COMMITTER_NAME": "mod-browser", "GIT_COMMITTER_EMAIL": "mod-browser@aeronautics.invalid"}
_FOLDER = "mods"
#: A folder a new metafile may go in: "" (the pack root) or plain lower-case names, never `..` or a dot folder.
_FOLDER_NAME = re.compile(r"^(?:[a-z0-9][a-z0-9_-]*(?:/[a-z0-9][a-z0-9_-]*)*)?$")


class StageError(Exception):
    """The batch cannot be written into the pack; nothing live was touched."""


@dataclass(frozen=True)
class Staged:
    batch: int
    branch: str
    path: Path


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "mod"


def metafiles(tree: Path) -> list[Path]:
    """Every packwiz metafile in the pack, in any folder (mods/, resourcepacks/, the root...), never under .git."""
    return sorted(p for p in tree.rglob("*.pw.toml")
                  if p.is_file() and not any(part.startswith(".") for part in p.relative_to(tree).parts[:-1]))


def identity(path: Path) -> set[tuple[str, str]]:
    """The projects a metafile names: ("modrinth", mod-id) and/or ("curseforge", project-id)."""
    try:
        update = tomllib.loads(path.read_text()).get("update", {})
    except (OSError, tomllib.TOMLDecodeError):
        return set()
    out = set()
    for source, key in (("modrinth", "mod-id"), ("curseforge", "project-id")):
        table = update.get(source)
        if isinstance(table, dict) and table.get(key) is not None:
            out.add((source, str(table[key])))
    return out


def op_identity(mod: dict[str, Any]) -> set[tuple[str, str]]:
    """The projects a request entry names: its own source and id, plus the CurseForge ids a Modrinth twin keeps."""
    out = {(str(mod["source"]), str(mod["projectId"]))} if mod.get("projectId") is not None else set()
    cf = mod.get("curseforge")
    if isinstance(cf, dict) and cf.get("projectId") is not None:
        out.add(("curseforge", str(cf["projectId"])))
    return out


def _ids(ids: set[tuple[str, str]]) -> str:
    return ", ".join(f"{source} {pid}" for source, pid in sorted(ids)) or "no project id"


def _q(value: str) -> str:
    return json.dumps(value)  # a TOML basic string: JSON escapes are valid TOML escapes


def metafile_text(mod: dict[str, Any]) -> str:
    """The packwiz metafile for one added mod (a dict from the resolver's snapshot)."""
    if not mod.get("fileHash") or not mod.get("hashFormat"):
        raise StageError(f"{mod['name']} has no file hash from {mod['source']}, so its metafile cannot be written.")
    lines = [f"name = {_q(mod['name'])}", f"filename = {_q(mod['fileName'])}", f"side = {_q(mod.get('side') or 'both')}", "",
             "[download]"]
    if mod["source"] == "modrinth":
        if not mod.get("downloadUrl"):
            raise StageError(f"{mod['name']} has no download URL.")
        lines += [f"url = {_q(mod['downloadUrl'])}", f"hash-format = {_q(mod['hashFormat'])}", f"hash = {_q(mod['fileHash'])}",
                  "", "[update]", "[update.modrinth]", f"mod-id = {_q(mod['projectId'])}", f"version = {_q(mod['versionId'])}"]
        cf = mod.get("curseforge")
        if cf:  # a CurseForge-blocked file fetched from Modrinth keeps the ids the upstream check matches by
            lines += ["", "[update.curseforge]", f"file-id = {int(cf['fileId'])}", f"project-id = {int(cf['projectId'])}"]
    elif mod["source"] == "curseforge":
        lines += [f"hash-format = {_q(mod['hashFormat'])}", f"hash = {_q(mod['fileHash'])}", 'mode = "metadata:curseforge"',
                  "", "[update]", "[update.curseforge]", f"file-id = {int(mod['versionId'])}", f"project-id = {int(mod['projectId'])}"]
    else:
        raise StageError(f"unknown source {mod['source']!r}")
    return "\n".join(lines) + "\n"


def sides(side: str) -> set[str]:
    return {"client", "server"} if side.lower() == "both" else {side.lower()}


def pack_sides(tree: Path) -> dict[str, set[str]]:
    """Intersect duplicate filename restrictions; ambiguity must never widen availability."""
    found = {}
    for path in metafiles(tree):
        data = tomllib.loads(path.read_text())
        filename = data.get("filename")
        available = sides(data.get("side", "both"))
        found[filename] = found.get(filename, available) & available
    return found


class GitPackRepo:
    def __init__(self, root: Path, work: Path):
        self.root, self.work = root, work

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        try:
            return subprocess.run(["git", "-c", "safe.directory=*", "-C", str(cwd or self.root), *args], check=True,
                                  capture_output=True, text=True, timeout=60, env={**_ENV, "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(self.work)}).stdout
        except subprocess.CalledProcessError as exc:
            raise StageError(f"git {args[0]} failed: {exc.stderr.strip()[:300]}") from exc

    # ---- staging ----
    def _existing(self, tree: Path) -> dict[tuple[str, str], Path]:
        """(source, project id) -> metafile, for every project the pack already tracks, in whatever folder."""
        found: dict[tuple[str, str], Path] = {}
        for path in metafiles(tree):
            for key in identity(path):
                found.setdefault(key, path)
        return found

    def stage(self, batch: int, requests: list[Any]) -> Staged:
        """Build the batch on a branch in its own worktree. `requests` carry `.added` and `.requester`."""
        branch = f"candidate-{batch}"
        path = self.work / branch
        self.work.mkdir(parents=True, exist_ok=True)
        self.discard(Staged(batch, branch, path))
        self._git("worktree", "add", "-q", "-b", branch, str(path), "HEAD")
        written: dict[Path, str] = {}  # metafile -> the text a request of this batch wrote into it
        try:
            for request in requests:
                changed = self._write(path, request.added, written)
                self._refresh(path)
                self._git("add", "-A", cwd=path)
                names = ", ".join(a["name"] for a in request.added)
                verb = "Update to" if getattr(request, "kind", "mod") == "upstream" else "Add"
                body = f"{verb} {request.mod_name}\n\nRequested-by: {request.requester}\nRequest: {request.id}\nWith: {names}\n"
                if not changed:
                    raise StageError(f"request {request.id} changed nothing")
                if not self._git("status", "--porcelain", cwd=path).strip():
                    body += "Already-in: an earlier request of this batch added all of it\n"
                self._git("commit", "-q", "--allow-empty", "-m", body, cwd=path)
        except Exception:
            self.discard(Staged(batch, branch, path))
            raise
        return Staged(batch, branch, path)

    def _write(self, tree: Path, added: list[dict[str, Any]], written: dict[Path, str] | None = None) -> bool:
        """Apply a request's entries to the tree. An entry is `add` (the default), or, for an upstream
        update, `change` or `remove` of the metafile it names.

        A project is known by its ids, never by its file name (#1789): an `add` of a project the pack
        already tracks, in any folder, is refused; a `change` or `remove` aimed at a metafile of a
        different project (a translation pack named like the mod it translates) is refused; and a new
        metafile never takes a name another project's metafile has. The one exception: an `add` an
        earlier request of the same batch already wrote, file for file, is that request's work, not a
        second copy, so it is skipped and still counts as this request done (2026-11-16, batch 3:
        Starcatcher, asked for, then needed by Starcatcher's Delight, failed the batch).

        Removals go first, so a project coming back takes the name its metafile had (Boatload's
        mods/boatload.pw.toml, once the translation written over it is removed)."""
        wrote = False
        removals = [m for m in added if m.get("op", "add") == "remove"]
        existing: dict[tuple[str, str], Path] = {}
        taken: set[str] = set()
        for i, mod in enumerate(removals + [m for m in added if m.get("op", "add") != "remove"]):
            if i == len(removals):  # what the pack holds once the removals are done
                existing = self._existing(tree)
                taken = {p.relative_to(tree).as_posix() for p in metafiles(tree)}
            op = mod.get("op", "add")
            if op in ("change", "remove"):
                target = self._metafile(tree, mod, op)
                theirs, ours = identity(target), op_identity(mod)
                shared = {s for s, _ in theirs} & {s for s, _ in ours}
                if shared and not theirs & ours:
                    raise StageError(f"{mod['name']}: {mod['metafile']} is a different project ({_ids(theirs)}, "
                                     f"not {_ids(ours)}); refusing to {op} it.")
                if op == "remove":
                    target.unlink()
                else:
                    target.write_text(metafile_text(mod))
                wrote = True
                continue
            if op != "add":
                raise StageError(f"unknown operation {op!r} for {mod['name']}")
            known = next((existing[k] for k in sorted(op_identity(mod)) if k in existing), None)
            replaced_file = mod.get("replacesFile")
            if known is None and mod.get("replaces") and replaced_file:
                matches = [p for p in metafiles(tree)
                           if tomllib.loads(p.read_text()).get("filename") == replaced_file]
                if len(matches) != 1:
                    raise StageError(f"{mod['name']}: the pack must track exactly one copy of {replaced_file} to replace.")
                known = matches[0]
            if known is not None and mod.get("replaces"):
                previous = tomllib.loads(known.read_text())
                # Keep the pack owner's side restriction when upgrading a dependency.
                mod = {**mod, "side": previous.get("side", mod.get("side", "both"))}
            if mod.get("replaces") and "requiredSides" not in mod:
                raise StageError(f"{mod['name']}: dependency upgrade has no required side evidence; requeue it first.")
            required = set(mod.get("requiredSides", []))
            if not required <= sides(mod.get("side", "both")):
                raise StageError(f"{mod['name']}: pack side restriction excludes required side {', '.join(sorted(required))}; "
                                 "the owner must change that restriction explicitly.")
            text = metafile_text(mod)
            if known is not None and written is not None and written.get(known) == text == known.read_text():
                wrote = True  # already done by this batch: nothing to write, nothing missing
                continue
            if known is not None and mod.get("replaces") and replaced_file and previous.get("filename") != replaced_file:
                raise StageError(f"{mod['name']}: the pack's file changed since this dependency upgrade was resolved.")
            if known is not None and not mod.get("replaces"):
                raise StageError(f"{mod['name']} is already tracked by the pack as {known.relative_to(tree).as_posix()}.")
            if known is None and mod.get("replaces"):
                raise StageError(f"{mod['name']} would replace version {mod['replaces']} in the pack, but that mod was "
                                 "not added through Modrinth or CurseForge, so the owner has to swap it by hand.")
            target = known
            if target is None:
                folder = self._folder(mod)
                prefix = f"{folder}/" if folder else ""
                name = f"{prefix}{slug(mod['name'])}.pw.toml"
                n = 2
                while name in taken:
                    name = f"{prefix}{slug(mod['name'])}-{n}.pw.toml"
                    n += 1
                taken.add(name)
                target = tree / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
            if written is not None:
                written[target] = text
            for key in op_identity(mod):
                existing.setdefault(key, target)
            wrote = True
        return wrote

    @staticmethod
    def _metafile(tree: Path, mod: dict[str, Any], op: str) -> Path:
        """The pack metafile a change or remove names: a `.pw.toml` inside the pack, never under a dot folder."""
        rel = str(mod.get("metafile") or "")
        root = tree.resolve()
        target = (tree / rel).resolve()
        if (root not in target.parents or not rel.endswith(".pw.toml") or not target.is_file()
                or any(part.startswith(".") for part in target.relative_to(root).parts)):
            raise StageError(f"{mod['name']}: the pack has no metafile {mod.get('metafile')!r} to {op}.")
        return target

    @staticmethod
    def _folder(mod: dict[str, Any]) -> str:
        """Where a new metafile goes: the `folder` an upstream update names for its type, else mods/."""
        folder = mod.get("folder", _FOLDER)
        if folder is None:
            raise StageError(f"{mod['name']} ({mod.get('fileName')}) is a {mod.get('type') or 'type of project'} the "
                             "pack keeps nowhere yet, so the owner has to add it by hand.")
        if not isinstance(folder, str) or not _FOLDER_NAME.match(folder):
            raise StageError(f"{mod['name']}: {folder!r} is not a pack folder.")
        if folder == _FOLDER and not str(mod.get("fileName", "")).lower().endswith(".jar"):
            raise StageError(f"{mod['name']} ({mod.get('fileName')}) is not a mod jar; refusing to put it in mods/.")
        return folder

    def _refresh(self, tree: Path) -> None:
        """Regenerate index.toml from the metafiles on disk, then pack.toml's hash of it (packwiz refresh)."""
        pack_text = (tree / "pack.toml").read_text()
        pack = tomllib.loads(pack_text)
        index_name = pack.get("index", {}).get("file", "index.toml")
        index_path = tree / index_name
        old = tomllib.loads(index_path.read_text())
        if old.get("hash-format", "sha256") != "sha256" or pack.get("index", {}).get("hash-format", "sha256") != "sha256":
            raise StageError("the pack's index does not use sha256; refusing to rewrite it")
        # A file the batch deleted leaves the index; one still on disk keeps its entry.
        entries = {f["file"]: f for f in old.get("files", []) if (tree / f["file"]).exists()}
        for meta in metafiles(tree):
            rel = meta.relative_to(tree).as_posix()
            entries.setdefault(rel, {"file": rel, "metafile": True})
            entries[rel]["hash"] = hashlib.sha256(meta.read_bytes()).hexdigest()
            entries[rel]["metafile"] = True
        out = ['hash-format = "sha256"', ""]
        for rel in sorted(entries):
            e = entries[rel]
            out += ["[[files]]", f"file = {_q(rel)}", f"hash = {_q(e['hash'])}"]
            for key in ("alias", "preserve"):
                if key in e:
                    out.append(f"{key} = {_q(e[key]) if isinstance(e[key], str) else str(e[key]).lower()}")
            if e.get("metafile"):
                out.append("metafile = true")
            out.append("")
        index_path.write_text("\n".join(out))
        digest = hashlib.sha256(index_path.read_bytes()).hexdigest()
        new_pack, count = re.subn(r'(\[index\][^\[]*?hash\s*=\s*)"[^"]*"', lambda m: f'{m.group(1)}"{digest}"', pack_text, count=1, flags=re.S)
        if count != 1:
            raise StageError("could not find the index hash in pack.toml")
        (tree / "pack.toml").write_text(new_pack)

    # ---- promote / revert / discard ----
    def head(self) -> str:
        return self._git("rev-parse", "HEAD").strip()

    def tree(self, rev: str = "HEAD") -> str:
        """The tree a commit holds: what "the matched pack" is compared by, whatever history says."""
        return self._git("rev-parse", f"{rev}^{{tree}}").strip()

    def promote(self, staged: Staged) -> str:
        """Merge the candidate into the live tree. Returns the merge commit, the one to revert."""
        if self._git("status", "--porcelain").strip():
            raise StageError("the live pack has uncommitted changes; refusing to merge into it")
        self._git("merge", "--no-ff", "-q", "-m", f"Apply batch {staged.batch}", staged.branch)
        return self.head()

    def promoted(self, batch: int) -> bool:
        """Whether the live pack holds batch `batch`'s merge (its `promote` ran)."""
        try:
            subjects = self._git("log", "--merges", "--format=%s", "-n", "200").splitlines()
        except StageError:
            return True  # cannot tell: treat as promoted, which halts rather than guesses
        return f"Apply batch {batch}" in subjects

    def revert(self, merge: str) -> str:
        self._git("revert", "-m", "1", "--no-edit", merge)
        return self.head()

    def discard(self, staged: Staged) -> None:
        if staged.path.exists():
            subprocess.run(["git", "-c", "safe.directory=*", "-C", str(self.root), "worktree", "remove", "--force", str(staged.path)],
                           capture_output=True, env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(self.work)})
            shutil.rmtree(staged.path, ignore_errors=True)
        subprocess.run(["git", "-c", "safe.directory=*", "-C", str(self.root), "worktree", "prune"], capture_output=True,
                       env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(self.work)})
        subprocess.run(["git", "-c", "safe.directory=*", "-C", str(self.root), "branch", "-D", staged.branch], capture_output=True,
                       env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(self.work)})
