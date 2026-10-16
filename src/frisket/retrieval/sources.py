"""Stable identities for rebuildable retrieval records.

The authoritative source version is supplied by its owning project writer.  This
module only turns that version, a producer recipe, and a locator into stable
derived identities; it never tries to infer freshness from mutable project rows.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from .contracts import SourceRevision


IDENTITY_VERSION = "frisket-retrieval-id-v1"


def canonical_json(value: Any) -> str:
    """Serialize an identity payload without platform- or insertion-order drift."""

    return json.dumps(
        _identity_value(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def identity_digest(kind: str, payload: Any) -> str:
    """Return a versioned SHA-256 identity for one retrieval-domain value."""

    if not kind or not kind.strip():
        raise ValueError("identity kind must not be empty")
    body = canonical_json(
        {"identity_version": IDENTITY_VERSION, "kind": kind, "payload": payload}
    )
    return f"sha256:{hashlib.sha256(body.encode('utf-8')).hexdigest()}"


def producer_recipe_id(recipe: Any) -> str:
    """Identity of embedding-affecting part production and sampling settings."""

    return identity_digest("producer_recipe", recipe)


def source_revision_id(source_revision: Any) -> str:
    """Identity of an immutable source revision supplied by its authority owner."""

    return identity_digest("source_revision", source_revision)


def part_id(source_revision: Any, recipe: Any, locator: Any, *, kind: str) -> str:
    """Identify a located part within one exact source/recipe revision."""

    return identity_digest(
        "part",
        {
            "source_revision": source_revision,
            "recipe": recipe,
            "locator": locator,
            "kind": kind,
        },
    )


def representation_id(*, space: Any, input_hash: str) -> str:
    """Content-address one encoded input under its complete embedding space."""

    if not input_hash:
        raise ValueError("input_hash must not be empty")
    return identity_digest("representation", {"space": space, "input_hash": input_hash})


def input_hash(value: bytes | str) -> str:
    """Hash exact encoder input bytes; text is encoded as UTF-8 without rewriting."""

    raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def row_composite_revision(members: Sequence[Any], *, recipe: Any) -> str:
    """Identify an ordered row composite from member revisions and its recipe."""

    if not members:
        raise ValueError("row composite requires at least one member revision")
    return identity_digest(
        "row_composite_revision", {"members": list(members), "recipe": recipe}
    )


def row_composite_source_revision(
    *,
    index_id: str,
    source_key: str,
    ordered_member_revisions: Sequence[Any],
    recipe: Any,
    content_hash: str,
) -> SourceRevision:
    """Construct the canonical authority identity for one named-index row payload."""

    for value, name in ((index_id, "index_id"), (source_key, "source_key")):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must not be empty")
    if not isinstance(content_hash, str) or not content_hash.strip():
        raise ValueError("content_hash must not be empty")
    return SourceRevision(
        source_id=f"embedding-index:{index_id}:row:{source_key}",
        source_kind="row_composite",
        version_kind="row_composite_revision",
        version_value=row_composite_revision(
            ordered_member_revisions,
            recipe=recipe,
        ),
        content_hash=content_hash,
    )


def _identity_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _identity_value(asdict(value))
    if isinstance(value, Enum):
        return _identity_value(value.value)
    if isinstance(value, Mapping):
        return {str(key): _identity_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_identity_value(item) for item in value]
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("identity values must contain only finite numbers")
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"unsupported identity value {type(value).__name__}")
