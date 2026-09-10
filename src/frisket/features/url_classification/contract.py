"""The URL classification contract + the exported snapshot serializer (§1.5, §2).

``classify_url`` returns ``UrlClassification`` — the shared shape both the
backend (authoritative, on submit) and the exported frontend snapshot agree on.
The snapshot is matcher METADATA only (never executable predicates): the web
affordance layer classifies a URL cell client-side for the default-affordance
hint, and the backend re-classifies authoritatively. A parity test plus the
check.cmd's ``git diff --exit-code`` guard pin the two together (§3, §7 item 2).

Regenerate the snapshot after changing the first-party matcher table with::

    uv run python -m frisket.features.url_classification

and commit ``src/frisket/data/url_classification_matchers.json``. The command
is deterministic (sorted keys, sorted matchers, trailing newline).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from frisket.features.url_classification.psl import PSL_SNAPSHOT_VERSION

SNAPSHOT_SCHEMA_VERSION = "frisket.url_classification_matchers.v1"
SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "url_classification_matchers.json"
)


class ExpansionHint(BaseModel):
    unit: Literal["video", "post", "item"]
    enumerator: str
    default_cap: int
    hard_cap: int | None
    requires_preview_count: bool = True


class ClassificationHandler(BaseModel):
    action_kind: str
    params_hints: dict[str, Any] = {}
    may_feed: list[str] = []


class UrlClassification(BaseModel):
    kind: Literal["media", "collection", "scraper", "page"]
    provider: str
    matcher_id: str
    source: Literal["first_party", "plugin"]
    origin_plugin_id: str | None = None
    capabilities: list[str] = []
    handler: ClassificationHandler
    expansion: ExpansionHint | None = None
    confidence: Literal["exact", "heuristic"] = "exact"


def _matcher_metadata(matcher: Any) -> dict[str, Any]:
    """Project one UrlMatcher into its serializable metadata (no callables)."""
    expansion = matcher.expansion
    return {
        "matcher_id": matcher.matcher_id,
        "provider": matcher.provider,
        "registered_domains": list(matcher.registered_domains),
        "kind": matcher.kind,
        "path_prefix": list(matcher.path_prefix),
        "query_requires": list(matcher.query_requires),
        "query_forbids": list(matcher.query_forbids),
        "path_regex": matcher.path_regex,
        "handler_action_kind": matcher.handler_action_kind,
        "params_hints": dict(matcher.params_hints),
        "capabilities": list(matcher.capabilities),
        "expansion": dict(expansion) if expansion is not None else None,
        "priority": matcher.priority,
        "source": matcher.source,
        "origin_plugin_id": matcher.origin_plugin_id,
    }


def build_snapshot() -> dict[str, Any]:
    """Build the snapshot dict from the registered FIRST-PARTY matchers.

    Plugin matchers are runtime-only and never exported (they load per-install
    from their manifests, not this checked-in artifact).
    """
    from frisket.features.url_classification.registry import registered_matchers

    matchers = sorted(
        (m for m in registered_matchers() if m.source == "first_party"),
        key=lambda m: m.matcher_id,
    )
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "psl_snapshot_version": PSL_SNAPSHOT_VERSION,
        "matchers": [_matcher_metadata(m) for m in matchers],
    }


def serialize_snapshot() -> str:
    """Deterministic JSON text of the snapshot (sorted keys, trailing newline)."""
    return json.dumps(build_snapshot(), indent=2, sort_keys=True) + "\n"


def write_snapshot() -> Path:
    """Regenerate the checked-in snapshot artifact. Returns the path written."""
    SNAPSHOT_PATH.write_text(serialize_snapshot(), encoding="utf-8")
    return SNAPSHOT_PATH
