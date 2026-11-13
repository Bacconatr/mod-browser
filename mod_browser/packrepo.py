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


class StageError(Exception):
    """The batch cannot be written into the pack; nothing live was touched."""


@dataclass(frozen=True)
class Staged:
    batch: int
    branch: str
    path: Path


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "mod"


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
    elif mod["source"] == "curseforge":
        lines += [f"hash-format = {_q(mod['hashFormat'])}", f"hash = {_q(mod['fileHash'])}", 'mode = "metadata:curseforge"',
                  "", "[update]", "[update.curseforge]", f"file-id = {int(mod['versionId'])}", f"project-id = {int(mod['projectId'])}"]
    else:
        raise StageError(f"unknown source {mod['source']!r}")
    return "\n".join(lines) + "\n"


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
        """(source, project id) -> metafile, for every mod the pack already tracks."""
        found: dict[tuple[str, str], Path] = {}
        for path in (tree / _FOLDER).glob("*.pw.toml"):
            try:
                update = tomllib.loads(path.read_text()).get("update", {})
            except (OSError, tomllib.TOMLDecodeError):
                continue
            if "modrinth" in update:
                found[("modrinth", str(update["modrinth"].get("mod-id")))] = path
            if "curseforge" in update:
                found[("curseforge", str(update["curseforge"].get("project-id")))] = path
        return found

    def stage(self, batch: int, requests: list[Any]) -> Staged:
        """Build the batch on a branch in its own worktree. `requests` carry `.added` and `.requester`."""
        branch = f"candidate-{batch}"
        path = self.work / branch
        self.work.mkdir(parents=True, exist_ok=True)
        self.discard(Staged(batch, branch, path))
        self._git("worktree", "add", "-q", "-b", branch, str(path), "HEAD")
        try:
            for request in requests:
                changed = self._write(path, request.added)
                self._refresh(path)
                self._git("add", "-A", cwd=path)
                names = ", ".join(a["name"] for a in request.added)
                body = f"Add {request.mod_name}\n\nRequested-by: {request.requester}\nRequest: {request.id}\nWith: {names}\n"
                if not changed:
                    raise StageError(f"request {request.id} changed nothing")
                self._git("commit", "-q", "-m", body, cwd=path)
        except Exception:
            self.discard(Staged(batch, branch, path))
            raise
        return Staged(batch, branch, path)

    def _write(self, tree: Path, added: list[dict[str, Any]]) -> bool:
        existing = self._existing(tree)
        taken = {p.name for p in (tree / _FOLDER).glob("*.pw.toml")}
        wrote = False
        for mod in added:
            known = existing.get((mod["source"], str(mod["projectId"])))
            if known is not None and not mod.get("replaces"):
                raise StageError(f"{mod['name']} is already tracked by the pack as {known.name}.")
            if known is None and mod.get("replaces"):
                raise StageError(f"{mod['name']} would replace version {mod['replaces']} in the pack, but that mod was "
                                 "not added through Modrinth or CurseForge, so the owner has to swap it by hand.")
            target = known
            if target is None:
                name = f"{slug(mod['name'])}.pw.toml"
                n = 2
                while name in taken:
                    name = f"{slug(mod['name'])}-{n}.pw.toml"
                    n += 1
                taken.add(name)
                target = tree / _FOLDER / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(metafile_text(mod))
            wrote = True
        return wrote

    def _refresh(self, tree: Path) -> None:
        """Regenerate index.toml from the metafiles on disk, then pack.toml's hash of it (packwiz refresh)."""
        pack_text = (tree / "pack.toml").read_text()
        pack = tomllib.loads(pack_text)
        index_name = pack.get("index", {}).get("file", "index.toml")
        index_path = tree / index_name
        old = tomllib.loads(index_path.read_text())
        if old.get("hash-format", "sha256") != "sha256" or pack.get("index", {}).get("hash-format", "sha256") != "sha256":
            raise StageError("the pack's index does not use sha256; refusing to rewrite it")
        entries = {f["file"]: f for f in old.get("files", [])}
        for meta in (tree / _FOLDER).glob("*.pw.toml"):
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

    def promote(self, staged: Staged) -> str:
        """Merge the candidate into the live tree. Returns the merge commit, the one to revert."""
        if self._git("status", "--porcelain").strip():
            raise StageError("the live pack has uncommitted changes; refusing to merge into it")
        self._git("merge", "--no-ff", "-q", "-m", f"Apply batch {staged.batch}", staged.branch)
        return self.head()

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
