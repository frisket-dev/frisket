"""Deterministic fingerprint-clustering helpers (OpenRefine-style "key
collision" + n-gram methods): group near-duplicate string values in a column
into clusters of likely-the-same thing.

Everything here is pure and network-free — no project writes, no model call
— so it is shared, as-is, by every layer that needs to group column values:
the engine's cluster/resolve op (``frisket.engine.runner.entities``), the
semantic-dedupe fallback shape (``frisket.ops.cluster``), the read-only
cluster preview (``frisket.preview.cluster``), and column-cleaning key
derivation (``frisket.actions.cleanup``).
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, OrderedDict
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from frisket.engine.store import Project

_WS = re.compile(r"\s+")
_NONWORD = re.compile(r"[^\w\s]", re.UNICODE)


def _normalize(value: str) -> str:
    """Lowercase, strip accents + punctuation, collapse whitespace."""
    s = unicodedata.normalize("NFKD", value)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = _NONWORD.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    return s


def fingerprint(value: str) -> str:
    """OpenRefine 'key collision' fingerprint: normalize, split into tokens,
    de-duplicate, sort, rejoin. 'Jon  Smith' and 'Smith, Jon' collide."""
    norm = _normalize(value)
    if not norm:
        return ""
    tokens = sorted(set(norm.split(" ")))
    return " ".join(tokens)


def ngram_fingerprint(value: str, ngram_size: int = 2) -> str:
    """OpenRefine 'n-gram fingerprint' key: lowercase, strip accents +
    punctuation + ALL whitespace, take the character n-grams, de-duplicate,
    sort, and rejoin. Catches typos and word-order/spacing variants the token
    fingerprint misses ('Krzysztof' / 'Kryzysztof', 'Sao Paulo' / 'SaoPaulo').

    Mirrors OpenRefine: for n=2 the string 'Paris' -> 'arispari' (2-grams
    {pa, ar, ri, is} sorted-unique -> ar, is, pa, ri). For strings shorter than
    ``ngram_size`` the whole normalized string is its own single gram.
    """
    if ngram_size < 1:
        raise ValueError("ngram_size must be >= 1")
    # accent-strip + lowercase + drop every non-word char INCLUDING whitespace
    s = unicodedata.normalize("NFKD", value)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = _NONWORD.sub("", s)
    s = _WS.sub("", s)
    if not s:
        return ""
    if len(s) <= ngram_size:
        return s
    grams = {s[i : i + ngram_size] for i in range(len(s) - ngram_size + 1)}
    return "".join(sorted(grams))


def column_values(project: Project, sheet_id: int, column: str) -> dict[int, str]:
    """row_id -> raw string value for the named column of a sheet."""
    col = None
    for c in project.columns(sheet_id):
        if c["name"] == column:
            col = c
            break
    if col is None:
        raise KeyError(column)
    vals = project.get_values(sheet_id, col["id"])
    out: dict[int, str] = {}
    for rid, v in vals.items():
        if v is None:
            continue
        s = str(v).strip()
        if s:
            out[rid] = s
    return out


def canonical(surface_counts: Counter[str]) -> str:
    """Pick a canonical surface form: the MOST FREQUENT member form, ties
    broken by the SHORTEST form, then lexicographically so the result is
    deterministic.

    Frequency-then-shortest is deliberate. In a high-cardinality cluster whose
    members are near-unique long phrases — e.g. an 18-form
    "President of <country>" group where every surface has count 1 — a
    longest-wins tie-break suggests the single most specific member
    ("President of the Republic of Trinidad and Tobago") as the canonical for
    them all, which is exactly backwards. Shortest-on-tie surfaces the shared
    stem instead. A genuinely more frequent form still wins outright."""
    return min(
        surface_counts.items(),
        key=lambda kv: (-kv[1], len(kv[0]), kv[0]),
    )[0]


def compute_clusters(
    project: Project,
    sheet_id: int,
    column: str,
    min_size: int = 2,
    key_fn: Callable[[str], str] = fingerprint,
    *,
    values: dict[int, str] | None = None,
    derive_key: Callable[[str], str] | None = None,
) -> list[dict[str, Any]]:
    """Group the column's values by a collision key. Returns one entry per
    cluster of >= ``min_size`` distinct surface forms, ordered largest-first.

    ``key_fn`` is the deterministic keying function (default: the token
    ``fingerprint``; pass ``ngram_fingerprint`` for the n-gram method). Each
    cluster carries: a stable ``key`` (the collision key), the suggested
    ``canonical`` value, the distinct ``values`` (with per-value row counts),
    the contributing ``row_ids``, and a ``size`` (number of rows).

    ``derive_key`` (cluster-by-key) transforms each ORIGINAL surface into the
    text the method actually keys on — so ``key_fn`` sees the derived form while
    the cluster's ``values``/``canonical``/``row_ids`` stay the ORIGINAL forms.
    This is what lets "President" and "President of Honduras" collide under a
    ``before:" of "`` key while the review surface still shows/merges the
    originals. ``None`` clusters on the surface directly.

    ``values`` (row_id -> non-empty stripped surface) may be a pre-read
    snapshot so the caller can read the column exactly once (the preview reads
    it once for both the value-hash and the clustering — no split read a
    concurrent edit could tear)."""
    if values is None:
        values = column_values(project, sheet_id, column)
    # collision-key -> {surface: [row_ids]}. The surface stored is always the
    # ORIGINAL; only the collision key is derived.
    groups: OrderedDict[str, dict[str, list[int]]] = OrderedDict()
    for rid, surface in values.items():
        keyed = derive_key(surface) if derive_key is not None else surface
        fp = key_fn(keyed)
        if not fp:
            continue
        groups.setdefault(fp, {}).setdefault(surface, []).append(rid)

    clusters: list[dict[str, Any]] = []
    for fp, surfaces in groups.items():
        if len(surfaces) < min_size:
            continue
        counts: Counter[str] = Counter()
        row_ids: list[int] = []
        for surface, rids in surfaces.items():
            counts[surface] = len(rids)
            row_ids.extend(rids)
        clusters.append(
            {
                "key": fp,
                "canonical": canonical(counts),
                "size": len(row_ids),
                "values": [
                    {"value": s, "count": counts[s]}
                    for s in sorted(counts, key=lambda s: (-counts[s], s))
                ],
                "row_ids": sorted(row_ids),
            }
        )
    clusters.sort(key=lambda c: (-c["size"], c["key"]))
    return clusters
