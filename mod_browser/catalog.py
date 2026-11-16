"""What the catalogs say: projects, versions and the choices between them.

Plain data. The ports that fetch it are in `ports.py`; the Modrinth and
CurseForge adapters are `modrinth.py` and `curseforge.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

Source = Literal["modrinth", "curseforge"]
Channel = Literal["release", "beta", "alpha"]
SOURCES = ("modrinth", "curseforge")


@dataclass(frozen=True)
class Project:
    source: str
    id: str
    slug: str
    name: str
    description: str
    icon_url: str | None
    downloads: int
    updated_at: str
    #: What the platform says; CurseForge says nothing, so "unknown".
    side: str


@dataclass(frozen=True)
class PlatformDep:
    source: str
    project_id: str
    type: str  # required | optional | incompatible | embedded


@dataclass(frozen=True)
class Version:
    source: str
    project_id: str
    id: str
    #: The platform label for display and ordering. Ranges use jar-provided versions.
    version_number: str
    channel: str
    published_at: str
    file_name: str
    #: None when the author does not allow downloads outside the platform.
    download_url: str | None
    blocked: bool
    deps: tuple[PlatformDep, ...] = field(default_factory=tuple)
    #: The file's hash as the platform states it, with its algorithm ("sha512", "sha1"), for the pack's metafile.
    file_hash: str | None = None
    hash_format: str | None = None


@dataclass(frozen=True)
class InstalledMod:
    mod_id: str
    version: str
    file: str
    dependencies: tuple[Any, ...] = field(default_factory=tuple)
    primary_id: str | None = None


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def default_version(versions: list[Version]) -> tuple[Version, str | None] | None:
    """The newest release, or the newest beta (flagged) when there is no release.

    `versions` is newest first.
    """
    for v in versions:
        if v.channel == "release":
            return v, None
    for v in versions:
        if v.channel == "beta":
            return v, "beta"
    return None


def camel(value: Any) -> Any:
    """Dataclasses and dicts to JSON-ready data with camelCase keys."""
    if hasattr(value, "__dataclass_fields__"):
        return {_camel_key(k): camel(getattr(value, k)) for k in value.__dataclass_fields__}
    if isinstance(value, dict):
        return {_camel_key(k) if isinstance(k, str) else k: camel(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [camel(v) for v in value]
    return value


def _camel_key(key: str) -> str:
    head, *rest = key.split("_")
    return head + "".join(p.title() for p in rest)
