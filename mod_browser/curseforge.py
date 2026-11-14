"""CurseForge, narrowed to NeoForge 1.21.1. Needs an API key.

Our key gets 403 from `/v1/mods/search` though `/v1/mods/{id}` and
`/v1/mods/{id}/files` answer 200 (Homelab #1680, measured 2026-11-12). So
text search is a switch that is off by default, and a CurseForge-only mod
comes in by its project id. The responses recorded for the tests are
hand-written from the documented schema: none could be recorded.
"""

from __future__ import annotations

from typing import Any
import re
from urllib.parse import quote, urlencode

from .catalog import PlatformDep, Project, Version
from .pack import version_from_filename

API = "https://api.curseforge.com/v1"
MINECRAFT = "432"
MODS_CLASS = "6"
NEOFORGE = "6"
# CurseForge's relationType: 3 required, 2 optional, 5 incompatible, 1 embedded.
_REL = {1: "embedded", 2: "optional", 3: "required", 5: "incompatible"}


def to_project(p: dict[str, Any]) -> Project:
    logo = p.get("logo") or {}
    return Project(
        source="curseforge",
        id=str(p.get("id")),
        slug=str(p.get("slug") or ""),
        name=str(p.get("name") or ""),
        description=str(p.get("summary") or ""),
        icon_url=logo.get("thumbnailUrl") or None,
        downloads=int(p.get("downloadCount") or 0),
        updated_at=str(p.get("dateModified") or ""),
        side="unknown",
    )


class CurseForgeCatalog:
    def __init__(self, http: Any, api_key: str):
        self.http = http
        self._headers = {"x-api-key": api_key, "Accept": "application/json"}

    def search(self, query: str) -> list[Project]:
        q = urlencode({"gameId": MINECRAFT, "classId": MODS_CLASS, "gameVersion": "1.21.1",
                       "modLoaderType": NEOFORGE, "searchFilter": query, "sortField": "2",
                       "sortOrder": "desc", "pageSize": "20"})
        res = self.http.json(f"{API}/mods/search?{q}", self._headers)
        return [to_project(p) for p in (res.get("data") or [])]

    def project(self, project_id: str) -> Project:
        res = self.http.json(f"{API}/mods/{quote(project_id, safe='')}", self._headers)
        return to_project(res["data"])

    def versions(self, project_id: str) -> list[Version]:
        q = urlencode({"gameVersion": "1.21.1", "modLoaderType": NEOFORGE, "pageSize": "50"})
        res = self.http.json(f"{API}/mods/{quote(project_id, safe='')}/files?{q}", self._headers)
        out = []
        for f in res.get("data") or []:
            if f.get("isAvailable") is False:
                continue
            release = f.get("releaseType")
            url = f.get("downloadUrl") or None
            name = str(f.get("fileName") or "")
            sha1 = next((h.get("value") for h in f.get("hashes") or [] if h.get("algo") == 1), None)
            out.append(Version(
                source="curseforge",
                project_id=project_id,
                id=str(f.get("id")),
                version_number=(re.search(r"1\.21\.1-[0-9][A-Za-z0-9.+-]*", name.removesuffix(".jar")) or [None])[0] or version_from_filename(name or str(f.get("displayName") or "")) or "",
                channel="release" if release == 1 else "beta" if release == 2 else "alpha",
                published_at=str(f.get("fileDate") or ""),
                file_name=name,
                download_url=url,
                # CurseForge hands out no URL for a file whose author disallows outside downloads.
                blocked=url is None,
                deps=tuple(PlatformDep("curseforge", str(d["modId"]), _REL[d["relationType"]])
                           for d in f.get("dependencies") or [] if d.get("relationType") in _REL),
                file_hash=sha1,
                hash_format="sha1" if sha1 else None,
            ))
        return sorted(out, key=lambda v: v.published_at, reverse=True)
