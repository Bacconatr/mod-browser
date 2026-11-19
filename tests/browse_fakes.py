"""The catalogs' world for tests: recorded upstream answers, and jars built around real mods.toml text.

Modrinth responses and the `tests/recorded/toml` files are REAL (recorded
2026-11-12), trimmed to the fields the code reads. CurseForge responses are
hand-written from its documented schema (none are recorded: its API terms
do not allow redistributing what it serves); they are unverified against
the live API. Anything marked
SYNTHETIC in a test is invented.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path
from typing import IO
from urllib.parse import parse_qs, urlsplit

RECORDED = Path(__file__).parent / "recorded"


def recorded(rel: str) -> str:
    return (RECORDED / rel).read_text()


def recorded_json(rel: str):
    return json.loads(recorded(rel))


def toml(name: str) -> str:
    return recorded(f"toml/{name}.toml")


def _words(query: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", query.lower()))


class RecordedHttp:
    """Answers from the recorded files. A search nobody recorded is an empty
    result; anything else unrecorded is an error."""

    def __init__(self) -> None:
        self.jars: dict[str, bytes] = {}
        self.requests: list[str] = []

    def json(self, url: str, headers: dict[str, str] | None = None):
        self.requests.append(url)
        u = urlsplit(url)
        q = parse_qs(u.query)
        if u.hostname == "api.modrinth.com":
            if u.path == "/v2/search":
                rel = f"modrinth/search/{_words(q.get('query', [''])[0])}.json"
                return recorded_json(rel) if (RECORDED / rel).exists() else {"hits": [], "total_hits": 0}
            if m := re.fullmatch(r"/v2/project/([^/]+)/version", u.path):
                return self._file(f"modrinth/versions/{m[1]}.json", url)
            if m := re.fullmatch(r"/v2/project/([^/]+)", u.path):
                return self._file(f"modrinth/project/{m[1]}.json", url)
        if u.hostname == "api.curseforge.com":
            if u.path == "/v1/mods/search":
                rel = f"curseforge/search/{_words(q.get('searchFilter', [''])[0])}.json"
                return recorded_json(rel) if (RECORDED / rel).exists() else {"data": []}
            if m := re.fullmatch(r"/v1/mods/(\d+)/files", u.path):
                return self._file(f"curseforge/files/{m[1]}.json", url)
            if m := re.fullmatch(r"/v1/mods/(\d+)", u.path):
                return self._file(f"curseforge/project/{m[1]}.json", url)
        raise AssertionError(f"unrecorded request: {url}")

    @staticmethod
    def _file(rel: str, url: str):
        if not (RECORDED / rel).exists():
            raise RuntimeError(f"404 for {url}")
        return recorded_json(rel)

    def fetch_jar(self, url: str) -> IO[bytes]:
        if url not in self.jars:
            raise RuntimeError(f"no jar recorded for {url}")
        return io.BytesIO(self.jars[url])


def mods_toml(mod_id: str, version: str, deps: list[dict] | None = None) -> str:
    """A mods.toml for a synthetic mod (the real ones are in tests/recorded/toml)."""
    lines = ['modLoader = "javafml"', "[[mods]]", f'modId = "{mod_id}"', f'version = "{version}"']
    for d in deps or []:
        lines += [f"[[dependencies.{mod_id}]]", f'modId = "{d["modId"]}"', f'type = "{d.get("type", "required")}"',
                  f'versionRange = "{d.get("range", "*")}"', f'side = "{d.get("side", "BOTH")}"']
    return "\n".join(lines)


def build_jar(toml_text: str, manifest_version: str = "", nested: dict[str, bytes] | None = None, jarjar: dict | None = None) -> bytes:
    """A mod jar with the metadata files the resolver reads."""
    import hashlib
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("META-INF/MANIFEST.MF", f"Manifest-Version: 1.0\nImplementation-Version: {manifest_version}\n")
        zf.writestr("META-INF/neoforge.mods.toml", toml_text)
        if nested:
            zf.writestr("META-INF/jarjar/metadata.json",
                        json.dumps(jarjar or {"jars": [{"path": f"META-INF/jars/{p}", "identifier": {"group": "fixture", "artifact": p},
                                                   "version": {"artifactVersion": hashlib.md5(data).hexdigest(),
                                                               "range": "[" + hashlib.md5(data).hexdigest() + "]"}}
                                                  for p, data in nested.items()]}))
            for path, data in nested.items():
                zf.writestr(f"META-INF/jars/{path}", data)
    return out.getvalue()


def modrinth_url(project_id: str, version_id: str) -> str:
    """The download URL a recorded Modrinth version points its primary file at."""
    version = next(v for v in recorded_json(f"modrinth/versions/{project_id}.json") if v["id"] == version_id)
    return next(f["url"] for f in version["files"] if f.get("primary"))


CF_FS_158 = "https://mediafilez.forgecdn.net/files/8459/97/functionalstorage-1.21.1-1.5.8.jar"

#: What a normal pack already has.
BASE_INSTALLED = [
    ("create-6.0.10.jar", build_jar(mods_toml("create", "6.0.10"), "6.0.10")),
    ("titanium-4.0.52.jar", build_jar(mods_toml("titanium", "1.21-4.0.52"))),
]


def write_installed(directory: Path, jars: list[tuple[str, bytes]]) -> None:
    for name, data in jars:
        (directory / name).write_bytes(data)
