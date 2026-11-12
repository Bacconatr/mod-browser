"""Maven-style version ordering and range matching, the kind NeoForge's
`versionRange` uses.

A version is `-` or `_` separated segments of dotted tokens: "1.21-1.5.7" is
the segments [1, 21] and [1, 5, 7]. Segments compare in order, so 1.21-1.5.7
sorts BELOW 1.21.1-1.5.8 (the main part 1.21 < 1.21.1 decides it) even though
5.7 < 5.8 would not. That is what makes Modrinth's Functional Storage 1.5.7
fail a `[1.21.1-1.5.8,)` requirement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

Token = int | str

_TOKEN = re.compile(r"\d+|[a-zA-Z]+")
_BARE = re.compile(r"^[\w.+-]+$")
_INTERVAL = re.compile(r"([\[(])\s*([^,\])]*?)\s*(?:,\s*([^\])]*?)\s*)?([\])])")


def _tokens(version: str) -> list[list[Token]]:
    text = re.sub(r"^v", "", version.strip(), flags=re.I)
    text = re.sub(r"\+.*$", "", text)  # build metadata ("6.0.10+mc1.21.1") never decides an order
    return [[int(t) if t.isdigit() else t.lower() for t in _TOKEN.findall(seg)] for seg in re.split(r"[-_]", text)]


def _cmp_token(a: Token | None, b: Token | None) -> int:
    # A missing token is 0, or "no qualifier": a release sorts above any word.
    if a is None:
        return 1 if isinstance(b, str) else _cmp_token(0, b)
    if b is None:
        return -1 if isinstance(a, str) else _cmp_token(a, 0)
    if isinstance(a, int) and isinstance(b, int):
        return (a > b) - (a < b)
    if isinstance(a, int):
        return 1
    if isinstance(b, int):
        return -1
    return (a > b) - (a < b)


def compare_versions(a: str, b: str) -> int:
    sa, sb = _tokens(a), _tokens(b)
    for i in range(max(len(sa), len(sb))):
        ta = sa[i] if i < len(sa) else []
        tb = sb[i] if i < len(sb) else []
        for j in range(max(len(ta), len(tb))):
            c = _cmp_token(ta[j] if j < len(ta) else None, tb[j] if j < len(tb) else None)
            if c:
                return c
    return 0


@dataclass(frozen=True)
class Interval:
    lo: str | None
    lo_inclusive: bool
    hi: str | None
    hi_inclusive: bool


def parse_range(text: str) -> list[Interval] | None:
    """None when the range cannot be understood (an unexpanded `${placeholder}`, say)."""
    r = text.strip()
    if r in ("", "*"):
        return [Interval(None, True, None, True)]
    if "${" in r:
        return None
    out: list[Interval] = []
    for m in _INTERVAL.finditer(r):
        opening, lo, hi, closing = m.groups()
        if hi is None:  # [1.0] is exactly 1.0
            out.append(Interval(lo, True, lo, True))
        else:
            out.append(Interval(lo or None, opening == "[", hi or None, closing == "]"))
    if out:
        return out
    # A bare version means "this or newer".
    return [Interval(r, True, None, True)] if _BARE.match(r) else None


def satisfies(version: str, version_range: str) -> bool:
    """True when the version satisfies the range, or the range cannot be read."""
    intervals = parse_range(version_range)
    if intervals is None:
        return True
    for i in intervals:
        if i.lo is not None:
            c = compare_versions(version, i.lo)
            if c < 0 or (c == 0 and not i.lo_inclusive):
                continue
        if i.hi is not None:
            c = compare_versions(version, i.hi)
            if c > 0 or (c == 0 and not i.hi_inclusive):
                continue
        return True
    return False
