"""Given a catalog version and the pack, say what adding it would do.

Returns the added set (the mod plus its required dependencies), what is
already installed, declared incompatibilities and unsatisfied version ranges,
the mod's side, and "author blocks outside downloads". It reads the jars' own
`neoforge.mods.toml`: the platforms' dependency lists are only used to widen a
search for a dependency, never to decide anything.

Bounded, because the request comes from the public Internet: at most
`MAX_ADDED` mods added and `MAX_LOOKUPS` upstream searches per resolve.
Candidate jars and dependency combinations are capped as well.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from functools import cmp_to_key
from typing import Any

from .catalog import InstalledMod, Project, Version, default_version, norm
from .jar import JarInfo
from .packrepo import export_replacement_problem, metafiles, pack_sides, sides
from .versions import compare_versions, parse_range, satisfies

#: Provided by the game or the loader, never a download.
BUILT_IN = frozenset({"minecraft", "neoforge", "forge", "javafml", "lowcodefml", "mclanguage", "fml"})
MAX_ADDED = 25
#: Upstream searches one resolve may start, counting the ones that find nothing.
MAX_LOOKUPS = 10
MAX_CANDIDATES = 50
MAX_ATTEMPTS = 200
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
    replaces_file: str | None = None
    required_sides: list[str] = field(default_factory=list)


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
            version_id: str | None, pack_root=None) -> Verdict:
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
    if jar is None or not jar.provides:
        return Verdict("refused", head, version, project.side,
                       problems=[Problem("unreadable", f"{project.name}'s jar has no NeoForge mod metadata, so it cannot be checked.")])

    restrictions = pack_sides(pack_root) if pack_root is not None else {}
    pack_entries = {}
    if pack_root is not None:
        for path in metafiles(pack_root):
            meta = tomllib.loads(path.read_text())
            pack_entries.setdefault(meta.get("filename"), []).append(meta)
    installed_list: list[InstalledMod] = installed_port.mods()
    installed = {m.mod_id: m for m in sorted(installed_list, key=lambda m: not m.bundled)}
    already = installed.get((jar.own or jar.provides)[0].mod_id)
    if already:
        return Verdict("already-installed", head, version, project.side,
                       installed_as={"modId": already.mod_id, "version": already.version})

    # Search complete dependency trees. Backtracking keeps a shared dependency at
    # one version and lets installed dependents constrain an upgrade as well.
    versions_cache: dict[tuple[str, str], list[Version]] = {}
    candidates_cache: dict[tuple[str, tuple[tuple[str, str], ...]], list[tuple[Project, Version]]] = {}
    jar_cache: dict[tuple[str, str], JarInfo | None] = {(chosen.source, chosen.id): jar}
    lookups = 0
    attempts = 0

    def candidates_for(mod_id: str, parent: Version) -> list[tuple[Project, Version]]:
        nonlocal lookups
        hints = tuple((d.source, d.project_id) for d in parent.deps if d.type == "required")
        key = (mod_id, hints)
        if key in candidates_cache:
            return candidates_cache[key]
        lookups += 1
        if lookups > MAX_LOOKUPS:
            return []
        projects = {}
        # The installed pack already identifies its CurseForge project. Query
        # that id directly even when CF text search is disabled; jar metadata
        # must still prove that each candidate provides the required mod/range.
        old = installed.get(mod_id)
        known_cf = set()
        if old:
            for meta in pack_entries.get(old.file, []):
                cf = meta.get("update", {}).get("curseforge", {})
                if cf.get("project-id") is not None:
                    known_cf.add(str(cf["project-id"]))
        project_hints = list(dict.fromkeys([*(("curseforge", pid) for pid in sorted(known_cf)[:3]), *hints[:3]]))
        for hint_source, hint_id in project_hints:
            try:
                p = catalog.project(hint_source, hint_id)
                projects[(p.source, p.id)] = p
            except Exception:  # noqa: BLE001 - one unavailable hint does not lose the others
                continue
        names = [p.name for p in projects.values()]
        for p in catalog.find_by_mod_id(mod_id, names):
            projects[(p.source, p.id)] = p
        candidates = []
        for key_, p in projects.items():
            if key_ not in versions_cache:
                try:
                    versions_cache[key_] = catalog.versions(p.source, p.id)
                except Exception:  # noqa: BLE001
                    versions_cache[key_] = []
            candidates.extend((p, v) for v in versions_cache[key_])
        candidates.sort(key=cmp_to_key(_better))
        # Try name matches before unrelated platform hints, whose jars still
        # may provide the requested id but can otherwise consume the budget.
        # An exact name comes first: a search for "cobblemon" returns dozens of
        # addons named after it whose higher version numbers would otherwise
        # spend the whole jar budget before Cobblemon itself is tried.
        want = norm(mod_id)
        candidates.sort(key=lambda c: 0 if want in (norm(c[0].slug), norm(c[0].name))
                        else 1 if want in norm(c[0].slug) or want in norm(c[0].name) else 2)
        candidates_cache[key] = candidates
        return candidates_cache[key]

    # Each member retains its own metadata and the reason it was selected.
    def search(members: list[tuple[Project, Version, JarInfo, AddedMod]]) -> tuple[list[AddedMod], list[Problem]]:
        nonlocal attempts
        planned = {}
        replaced_files = {entry.replaces_file for _, _, _, entry in members if entry.replaces_file}
        remaining = [m for m in installed_list if m.file not in replaced_files]
        removed_ids = {m.mod_id for m in installed_list if m.file in replaced_files}
        # JarJar selects one bundled artifact by coordinates and the intersection
        # of every embedding jar's supported ranges, before mod-id validation.
        # https://docs.neoforged.net/toolchain/docs/plugins/mdg/#jar-in-jar
        bundles = [(b, p.name, v) for p, v, info, _ in members for b in info.bundles]
        bundles.extend((b, m.file, chosen) for m in remaining for b in m.bundles)
        groups = {}
        selected_bundles = []
        for b, label, parent in bundles:
            if b.identity is None:
                selected_bundles.append((b, label, parent))
            else:
                groups.setdefault(b.identity, []).append((b, label, parent))
        for identity, offers in groups.items():
            usable = [offer for offer in offers if offer[0].artifact_version and
                      all(b.version_range and parse_range(b.version_range) is not None and
                          satisfies(offer[0].artifact_version,
                                    b.version_range if b.version_range.startswith(("[", "(")) else "*")
                          for b, _, _ in offers)]
            if not usable:
                return [], [Problem("incompatible", f"No bundled artifact version satisfies every range for {':'.join(identity)}.")]
            usable.sort(key=cmp_to_key(lambda a, b: compare_versions(b[0].artifact_version, a[0].artifact_version)))
            selected_bundles.append(usable[0])
        # JarSelector strips selected embedded jars whose complete mod-id identity
        # is already a top-level source. The source version wins, even when older.
        # https://github.com/neoforged/JarJar/blob/main/selector/src/main/java/net/neoforged/jarjar/selection/JarSelector.java
        source_ids = {}
        for m in remaining:
            if not m.bundled:
                source_ids.setdefault(m.file, []).append(m.mod_id)
        top_identities = {"".join(ids) for ids in source_ids.values()}
        top_identities.update("".join(m.mod_id for m in info.own) for _, _, info, _ in members)
        selected_bundles = [(b, label, parent) for b, label, parent in selected_bundles
                            if not b.provides or "".join(m.mod_id for m in b.provides) not in top_identities]
        # FML 4's UniqueModListBuilder then groups files by their FIRST mod id
        # and keeps the newest. Forgified Fabric API added a secondary id, so
        # its older embedded copy has a different JarJar identity but is dropped
        # by this second stage. Keep conservative refusal if an embedded copy
        # would supersede the owner's standalone file.
        top_versions = {m.mod_id: m.version for m in remaining
                        if not m.bundled and m.mod_id == m.primary_id}
        top_versions.update({info.own[0].mod_id: info.own[0].version for _, _, info, _ in members if info.own})
        selected_bundles = [(b, label, parent) for b, label, parent in selected_bundles
                            if not b.provides or b.provides[0].mod_id not in top_versions
                            or compare_versions(top_versions[b.provides[0].mod_id], b.provides[0].version) < 0]
        providers = [(m.mod_id, m.version, m.file) for m in remaining if not m.bundled]
        providers.extend((m.mod_id, m.version, p.name) for p, _, info, _ in members for m in info.own)
        providers.extend((m.mod_id, m.version, label) for b, label, _ in selected_bundles for m in b.provides)
        effective = {}
        for mod_id, version_, label in providers:
            if mod_id in effective:
                return [], [Problem("incompatible", f"More than one jar provides {mod_id}.")]
            effective[mod_id] = (version_, label)
        for _, _, info, _ in members:
            for m in info.provides:
                if m.mod_id in effective:
                    planned[m.mod_id] = effective[m.mod_id]
        member_sides = {entry.file_name: restrictions.get(entry.replaces_file, sides(entry.side))
                        for _, _, _, entry in members}
        provider_sides = {m.mod_id: restrictions.get(m.file, sides("both")) for m in remaining}
        provider_sides.update({m.mod_id: member_sides[entry.file_name]
                               for _, _, info, entry in members for m in info.provides})
        requirements = {}
        checks = [(p.name, v, info.own_dependencies if info.own_dependencies is not None else info.dependencies, False) for p, v, info, _ in members]
        checks.extend((m.file, chosen, m.dependencies, True) for m in remaining)
        checks.extend((label, parent, b.dependencies, label not in {p.name for p, _, _, _ in members})
                      for b, label, parent in selected_bundles)
        missing = None
        incompatibilities = []
        for label, parent, dependencies, is_installed in checks:
            # Finish the new tree before checking jars that an upcoming
            # dependency upgrade may replace.
            if is_installed and missing is not None:
                continue
            for dep in dependencies:
                if dep.mod_id in BUILT_IN:
                    continue
                have = effective.get(dep.mod_id)
                if dep.type == "incompatible":
                    incompatibilities.append((label, dep, is_installed))
                if dep.type != "required":
                    continue
                if is_installed and dep.mod_id not in planned and dep.mod_id not in removed_ids:
                    continue
                parent_sides = (restrictions.get(label, sides("both")) if is_installed else
                                next(member_sides[e.file_name] for p, _, _, e in members if p.name == label))
                needed = parent_sides & sides(dep.side)
                if not needed:
                    continue
                requirements.setdefault(dep.mod_id, set()).update(needed)
                if have and not needed <= provider_sides.get(dep.mod_id, sides("both")):
                    return [], [Problem("incompatible", f"{label} needs {dep.mod_id} on side {', '.join(sorted(needed))}, "
                                        "but its pack side restriction excludes it; the owner must change that restriction explicitly.")]
                if have and satisfies(have[0], dep.version_range):
                    continue
                if dep.mod_id in planned:
                    return [], [Problem("version-range", f"{label} needs {dep.mod_id} {dep.version_range}, "
                                        f"but the selected jar provides {have[0]}.")]
                if missing is None:
                    missing = (label, parent, dep)
        if missing is None:
            for label, dep, is_installed in incompatibilities:
                have = effective.get(dep.mod_id)
                if have is None or (have[0] and not satisfies(have[0], dep.version_range)):
                    continue
                if is_installed:
                    if dep.mod_id not in planned:
                        continue
                    msg = f"{label} (already in the pack) declares itself incompatible with {have[1]}."
                else:
                    rng = f" {dep.version_range}" if dep.version_range else ""
                    has = f" ({have[0]})" if have[0] else ""
                    msg = f"{label} declares itself incompatible with {dep.mod_id}{rng}, which the pack has{has}."
                return [], [Problem("incompatible", msg)]
            for _, _, info, entry in members:
                entry.required_sides = sorted(set().union(*(requirements.get(m.mod_id, set()) for m in info.provides)))
            return [entry for _, _, _, entry in members], []
        label, parent, dep = missing
        if len(members) >= MAX_ADDED:
            return [], [Problem("not-found", f"This would add more than {MAX_ADDED} mods; refusing to follow dependencies further.")]
        candidates = candidates_for(dep.mod_id, parent)
        if lookups > MAX_LOOKUPS:
            return [], [Problem("not-found", f"{project.name} needs too many mods that are not in the pack; refusing to look up more than {MAX_LOOKUPS}.")]
        failures = []
        unreadable = []
        saw_provider = False
        for p, v in candidates:
            if v.blocked:
                failures = [_blocked(p.name, f"needed by {label}")]
                continue
            key = (v.source, v.id)
            if key not in jar_cache:
                if len(jar_cache) >= MAX_CANDIDATES:
                    return [], [Problem("not-found", "Too many candidate jars to check safely; try a different version.")]
                jar_cache[key] = jars.info(v)
            info = jar_cache[key]
            if info is None or not info.provides:
                if norm(dep.mod_id) in norm(p.name) or norm(dep.mod_id) in norm(p.slug):
                    unreadable = [Problem("unreadable", f"{p.name}'s jar has no readable NeoForge mod metadata.")]
                continue
            provided = next((m for m in info.provides if m.mod_id == dep.mod_id), None)
            if provided is None:
                continue
            saw_provider = True
            if not provided.version or not satisfies(provided.version, dep.version_range):
                continue
            attempts += 1
            if attempts > MAX_ATTEMPTS:
                return [], [Problem("not-found", "Too many dependency combinations to check safely; try a different version.")]
            entry = _added(p.name, v, f"required by {label}", p.side)
            old = next((installed[m.mod_id] for m in (info.own or info.provides[:1]) if m.mod_id in installed
                        and installed[m.mod_id].primary_id in (None, m.mod_id)), None)
            if old:
                entry.replaces = old.version
                entry.replaces_file = old.file
                problems = [export_replacement_problem(meta, {"name": entry.name, "source": entry.source})
                            for meta in pack_entries.get(old.file, [])]
                if any(problems):
                    failures = [Problem("incompatible", problem) for problem in problems if problem]
                    continue
            result, failures = search([*members, (p, v, info, entry)])
            if not failures:
                return result, []
        if failures:
            return [], failures
        if unreadable and not saw_provider:
            return [], unreadable
        inst = installed.get(dep.mod_id)
        if inst:
            msg = f"{label} needs {dep.mod_id} {dep.version_range}, but the pack has {inst.version} and nothing newer fits."
        elif saw_provider:
            msg = f"{label} needs {dep.mod_id} {dep.version_range}, and no published version fits."
        else:
            return [], [Problem("not-found", f"{label} needs {dep.mod_id}, which is on neither Modrinth nor CurseForge for NeoForge 1.21.1.")]
        return [], [Problem("version-range", msg)]

    root = _added(project.name, chosen, "requested", project.side)
    added, problems = search([(project, chosen, jar, root)])
    return Verdict("refused" if problems else "ok", head, version, project.side,
                   added=added or [root], problems=problems)


def _better(a: tuple[Project, Version], b: tuple[Project, Version]) -> int:
    """Orders candidates best first: release before beta, then the higher version, then the later upload."""
    x, y = a[1], b[1]
    rank = _CHANNEL_RANK.get(x.channel, 3) - _CHANNEL_RANK.get(y.channel, 3)
    if rank:
        return rank
    return compare_versions(y.version_number, x.version_number) or (y.published_at > x.published_at) - (y.published_at < x.published_at)
