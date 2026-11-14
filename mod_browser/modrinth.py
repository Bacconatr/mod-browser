"""Modrinth, narrowed to NeoForge 1.21.1. No key needed."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, urlencode

from .catalog import PlatformDep, Project, Version

API = "https://api.modrinth.com/v2"
FACETS = json.dumps([["project_type:mod"], ["versions:1.21.1"], ["categories:neoforge"]])
_DEP_TYPES = ("required", "optional", "incompatible", "embedded")


def _side(client: Any, server: Any) -> str:
    if not client or not server:
        return "unknown"
    if server == "unsupported":
        return "client"
    if client == "unsupported":
        return "server"
    return "both"


def to_project(p: dict[str, Any]) -> Project:
    return Project(
        source="modrinth",
        id=str(p.get("project_id") or p.get("id") or ""),
        slug=str(p.get("slug") or ""),
        name=str(p.get("title") or ""),
        description=str(p.get("description") or ""),
        icon_url=p.get("icon_url") or None,
        downloads=int(p.get("downloads") or 0),
        updated_at=str(p.get("date_modified") or p.get("updated") or ""),
        side=_side(p.get("client_side"), p.get("server_side")),
    )


def to_version(v: dict[str, Any]) -> Version:
    files = v.get("files") or []
    file = next((f for f in files if f.get("primary")), files[0] if files else {})
    kind = v.get("version_type")
    deps = tuple(
        PlatformDep("modrinth", str(d["project_id"]), d["dependency_type"])
        for d in v.get("dependencies") or []
        if d.get("project_id") and d.get("dependency_type") in _DEP_TYPES
    )
    return Version(
        source="modrinth",
        project_id=str(v.get("project_id") or ""),
        id=str(v.get("id") or ""),
        version_number=str(v.get("version_number") or ""),
        channel=kind if kind in ("release", "beta") else "alpha",
        published_at=str(v.get("date_published") or ""),
        file_name=str(file.get("filename") or ""),
        download_url=file.get("url") or None,
        blocked=False,
        deps=deps,
        file_hash=(file.get("hashes") or {}).get("sha512"),
        hash_format="sha512" if (file.get("hashes") or {}).get("sha512") else None,
    )


class ModrinthCatalog:
    def __init__(self, http: Any):
        self.http = http

    def search(self, query: str) -> list[Project]:
        q = urlencode({"query": query, "facets": FACETS, "limit": "20"})
        res = self.http.json(f"{API}/search?{q}")
        return [to_project(h) for h in (res.get("hits") or [])]

    def project(self, project_id: str) -> Project:
        return to_project(self.http.json(f"{API}/project/{quote(project_id, safe='')}"))

    def versions(self, project_id: str) -> list[Version]:
        q = urlencode({"loaders": '["neoforge"]', "game_versions": '["1.21.1"]'})
        res = self.http.json(f"{API}/project/{quote(project_id, safe='')}/version?{q}")
        return sorted((to_version(v) for v in res), key=lambda v: v.published_at, reverse=True)
