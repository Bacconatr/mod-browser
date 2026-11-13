"""Given a catalog version and the pack, say what adding it would do.

Returns the added set (the mod plus its required dependencies), what is
already installed, declared incompatibilities and unsatisfied version ranges,
the mod's side, and "author blocks outside downloads". It reads the jars' own
`neoforge.mods.toml`: the platforms' dependency lists are only used to widen a
search for a dependency, never to decide anything.

Bounded, because the request comes from the public Internet: at most
`MAX_ADDED` mods added and `MAX_LOOKUPS` upstream searches per resolve, and a
dependency is examined once per (mod id, range).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cmp_to_key
from typing import Any

from .catalog import InstalledMod, Project, Version, default_version
from .jar import JarInfo
from .versions import compare_versions, satisfies

#: Provided by the game or the loader, never a download.
BUILT_IN = frozenset({"minecraft", "neoforge", "forge", "javafml", "lowcodefml", "mclanguage", "fml"})
MAX_ADDED = 25
#: Upstream searches one resolve may start, counting the ones that find nothing.
MAX_LOOKUPS = 10
_CHANNEL_RANK = {"release": 0, "beta": 1, "alpha": 2}


class NotFound(Exception):
    pass


class Busy(Exception):
    """Too many resolves are already running; ask again in a moment."""


@dataclass
class AddedMod:
    name: str
    source: str
    project_id: str
    version_id: str
    version_number: str
    channel: str
    file_name: str
    reason: str
    #: The version already in the pack that this replaces, when it is too old.
    replaces: str | None = None
    #: What the pack's metafile for it needs (the applier writes it, and never trusts a later lookup).
    download_url: str | None = None
    file_hash: str | None = None
    hash_format: str | None = None
    side: str = "both"


@dataclass
class Problem:
    kind: str  # incompatible | version-range | blocked-download | not-found | unreadable
    message: str


@dataclass
class Verdict:
    status: str  # ok | already-installed | refused | unavailable
    project: dict[str, str]
    version: dict[str, Any] | None
    side: str
    installed_as: dict[str, str] | None = None
    added: list[AddedMod] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)


def _blocked(name: str, why: str | None = None) -> Problem:
    return Problem("blocked-download",
                   f"{name}{f' ({why})' if why else ''} cannot be added: its author blocks downloads outside CurseForge.")


def _added(name: str, v: Version, reason: str, side: str = "both") -> AddedMod:
    return AddedMod(name, v.source, v.project_id, v.id, v.version_number, v.channel, v.file_name, reason,
                    download_url=v.download_url, file_hash=v.file_hash, hash_format=v.hash_format,
                    side=side if side in ("both", "client", "server") else "both")


def resolve(catalog: Any, jars: Any, installed_port: Any, source: str, project_id: str,
            version_id: str | None) -> Verdict:
    project = catalog.project(source, project_id)
    versions = catalog.versions(source, project_id)
    head = {"source": source, "id": project_id, "name": project.name}

    fallback: str | None = None
    if version_id:
        chosen = next((v for v in versions if v.id == version_id), None)
        if chosen is None:
            raise NotFound(f"{project.name} has no version {version_id} for NeoForge 1.21.1")
    else:
        pick = default_version(versions)
        if pick is None:
            return Verdict("unavailable", head, None, project.side,
                           problems=[Problem("not-found", f"{project.name} has no release or beta for NeoForge 1.21.1.")])
        chosen, fallback = pick
    version = {"id": chosen.id, "number": chosen.version_number, "channel": chosen.channel, "fallback": fallback}

    if chosen.blocked:
        return Verdict("unavailable", head, version, project.side, problems=[_blocked(project.name)])
    jar = jars.info(chosen)
    if jar is None or not jar.own:
        return Verdict("refused", head, version, project.side,
                       problems=[Problem("unreadable", f"{project.name}'s jar has no NeoForge mod metadata, so it cannot be checked.")])

    installed_list: list[InstalledMod] = installed_port.mods()
    installed = {m.mod_id: m for m in installed_list}
    already = installed.get(jar.own[0].mod_id)
    if already:
        return Verdict("already-installed", head, version, project.side,
                       installed_as={"modId": already.mod_id, "version": already.version})

    # Everything this request would put in the pack, and what it provides.
    planned: dict[str, tuple[str, str]] = {m.mod_id: (m.version, project.name) for m in jar.provides}
    added = [_added(project.name, chosen, "requested", project.side)]
    problems: list[Problem] = []
    members: list[tuple[str, JarInfo]] = [(project.name, jar)]

    versions_cache: dict[tuple[str, str], list[Version]] = {}

    def versions_of(p: Project) -> list[Version]:
        key = (p.source, p.id)
        if key not in versions_cache:
            try:
                versions_cache[key] = catalog.versions(p.source, p.id)
            except Exception:  # noqa: BLE001 - one project failing must not sink the resolve
                versions_cache[key] = []
        return versions_cache[key]

    def hints_for(v: Version) -> list[str]:
        names = []
        for d in v.deps:
            if d.type != "required":
                continue
            try:
                names.append(catalog.project(d.source, d.project_id).name)
            except Exception:  # noqa: BLE001
                continue
        return names

    lookups = 0
    seen: set[tuple[str, str]] = set()  # one check per (mod id, range): a stricter second ask still counts
    queue: list[tuple[Version, JarInfo, str]] = [(chosen, jar, project.name)]
    while queue:
        item_version, item_jar, label = queue.pop(0)
        hints: list[str] | None = None
        for dep in item_jar.dependencies:
            asked = (dep.mod_id, dep.version_range)
            if dep.type != "required" or dep.mod_id in BUILT_IN or asked in seen:
                continue
            seen.add(asked)
            have = planned.get(dep.mod_id)
            if have and (not have[0] or satisfies(have[0], dep.version_range)):
                continue
            inst = installed.get(dep.mod_id)
            if inst and satisfies(inst.version, dep.version_range):
                continue
            if len(added) >= MAX_ADDED:
                problems.append(Problem("not-found", f"This would add more than {MAX_ADDED} mods; refusing to follow dependencies further."))
                break
            lookups += 1
            if lookups > MAX_LOOKUPS:
                problems.append(Problem("not-found", f"{project.name} needs too many mods that are not in the pack; refusing to look up more than {MAX_LOOKUPS}."))
                break
            if hints is None:
                hints = hints_for(item_version)
            candidates = [(p, v) for p in catalog.find_by_mod_id(dep.mod_id, hints) for v in versions_of(p)]
            fits = sorted(
                (c for c in candidates if satisfies(c[1].version_number, dep.version_range)),
                key=cmp_to_key(_better),
            )
            if not fits:
                if inst:
                    msg = f"{label} needs {dep.mod_id} {dep.version_range}, but the pack has {inst.version} and nothing newer fits."
                    problems.append(Problem("version-range", msg))
                elif candidates:
                    problems.append(Problem("version-range", f"{label} needs {dep.mod_id} {dep.version_range}, and no published version fits."))
                else:
                    problems.append(Problem("not-found", f"{label} needs {dep.mod_id}, which is on neither Modrinth nor CurseForge for NeoForge 1.21.1."))
                continue
            pick_project, pick = fits[0]
            if pick.blocked:
                problems.append(_blocked(pick_project.name, f"needed by {label}"))
                continue
            dep_jar = jars.info(pick)
            entry = _added(pick_project.name, pick, f"required by {label}", pick_project.side)
            if inst:
                entry.replaces = inst.version
            added.append(entry)
            planned[dep.mod_id] = (pick.version_number, pick_project.name)
            if dep_jar:
                for m in dep_jar.provides:
                    planned.setdefault(m.mod_id, (m.version, pick_project.name))
                members.append((pick_project.name, dep_jar))
                queue.append((pick, dep_jar, pick_project.name))

    # Declared incompatibilities, in both directions.
    for label, member in members:
        for dep in member.dependencies:
            if dep.type != "incompatible":
                continue
            other = installed.get(dep.mod_id)
            other_version = other.version if other else (planned[dep.mod_id][0] if dep.mod_id in planned else None)
            if (other or dep.mod_id in planned) and (not other_version or satisfies(other_version, dep.version_range)):
                rng = f" {dep.version_range}" if dep.version_range else ""
                has = f" ({other_version})" if other_version else ""
                problems.append(Problem("incompatible", f"{label} declares itself incompatible with {dep.mod_id}{rng}, which the pack has{has}."))
    for inst in installed_list:
        for dep in inst.dependencies:
            if dep.type != "incompatible" or dep.mod_id not in planned:
                continue
            version_, who = planned[dep.mod_id]
            if not version_ or satisfies(version_, dep.version_range):
                problems.append(Problem("incompatible", f"{inst.file} (already in the pack) declares itself incompatible with {who}."))

    return Verdict("refused" if problems else "ok", head, version, project.side, added=added, problems=problems)


def _better(a: tuple[Project, Version], b: tuple[Project, Version]) -> int:
    """Orders candidates best first: release before beta, then the higher version, then the later upload."""
    x, y = a[1], b[1]
    rank = _CHANNEL_RANK.get(x.channel, 3) - _CHANNEL_RANK.get(y.channel, 3)
    if rank:
        return rank
    return compare_versions(y.version_number, x.version_number) or (y.published_at > x.published_at) - (y.published_at < x.published_at)
