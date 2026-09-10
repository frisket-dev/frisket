"""Canonical embedding source payloads + hashes (shared by refresh and similarity).

The source payload for a row is the joined text of its selected columns; its
``source_hash`` is a canonical hash over (index source-policy hash, ordered
(column, value) pairs). ``embedding.index_refresh`` uses this to skip rows whose
content is unchanged; ``embedding_similarity`` uses the SAME builder to detect a
stale anchor / stale candidate (sidecar ``status='ready'`` alone is not enough —
the underlying cell may have changed since the vector was written).

This lives in ``frisket.embeddings`` so the resolver and the executor share one
definition instead of the resolver importing executor-private helpers.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from frisket.engine.store import Project

from .spaces import canonical_json


# Media column types whose cell is a blob envelope / URI, not embeddable text.
_MEDIA_COLUMN_TYPES = frozenset({"image", "audio", "video", "file"})

# A well-formed raw blob content digest: the 64-hex sha256 returned by
# Project.add_blob. Anything else must not reach the materialization port.
_VALID_BLOB_DIGEST = re.compile(r"[0-9a-f]{64}")


def source_hash(policy_hash: str | None, pairs: list[list[Any]]) -> str:
    """Canonical content hash for one row's selected (column, value) pairs."""
    digest = hashlib.sha256(
        canonical_json({"policy_hash": policy_hash, "values": pairs}).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


def _media_source_ref(
    project: Project, column: str, value: Any
) -> dict[str, Any] | None:
    """Resolve a media cell to a blob-backed ref. The hash basis is the blob CONTENT
    digest (envelope ``blob``) or the URI — NOT the stringified envelope — so editing
    the underlying bytes (a new blob hash) is detected as a content change."""
    if isinstance(value, dict) and value.get("blob"):
        blob_hash = str(value["blob"])
        # Persist only the immutable content identity. Consumers materialize the
        # blob for their own call lifetime; a host temp path is never durable data.
        valid_digest = _VALID_BLOB_DIGEST.fullmatch(blob_hash) is not None
        return {
            "kind": "media_blob",
            "column": column,
            "blob_hash": blob_hash,
            "content_id": blob_hash,
            "mime": value.get("mime"),
            "filename": value.get("filename"),
            "materializable": valid_digest,
        }
    if isinstance(value, str) and value.strip():
        return {
            "kind": "media_uri",
            "column": column,
            "blob_hash": None,
            "content_id": value,
            "uri": value,
        }
    return None


def build_source_payloads(
    project: Project,
    index: Any,
    source_columns: list[str],
    row_ids: list[int],
) -> list[dict[str, Any]]:
    """Build ``{source_key, row_id, payload, source_hash, source_ref}`` for each
    of ``row_ids`` whose selected columns yield non-empty text. Rows with no
    payload are skipped (no embeddable content)."""
    sheet_id = index["sheet_id"]
    if sheet_id is None or not row_ids:
        return []
    columns = {
        c["name"]: (int(c["id"]), str(c["type"]))
        for c in project.columns(sheet_id, include_hidden=True)
    }
    col_meta = [(name, columns.get(name)) for name in source_columns]
    # one whole-column fetch per source column, regardless of how many rows
    values = {
        meta[0]: project.get_values(sheet_id, meta[0])
        for _name, meta in col_meta
        if meta is not None
    }
    policy_hash = index["source_policy_hash"]
    out: list[dict[str, Any]] = []
    for row_id in row_ids:
        pairs: list[list[Any]] = []
        parts: list[str] = []
        media_refs: list[dict[str, Any]] = []
        for name, meta in col_meta:
            if meta is None:
                pairs.append([name, None])
                continue
            col_id, col_type = meta
            value = values.get(col_id, {}).get(row_id)
            if col_type in _MEDIA_COLUMN_TYPES:
                ref = _media_source_ref(project, name, value)
                if ref is not None:
                    media_refs.append(ref)
                    # hash over the blob content id / URI, not the envelope repr
                    pairs.append([name, ref["content_id"]])
                else:
                    pairs.append([name, None])
                continue
            pairs.append([name, value])
            if value is not None and str(value).strip():
                parts.append(str(value))
        if not parts and not media_refs:
            continue
        payload = "\n".join(parts) if parts else (media_refs[0]["content_id"] or "")
        source_ref: dict[str, Any] = {
            "sheet_id": sheet_id,
            "row_id": row_id,
            "columns": list(source_columns),
        }
        if media_refs:
            source_ref["media"] = media_refs
        out.append(
            {
                "source_key": str(row_id),
                "row_id": row_id,
                "payload": payload,
                "source_hash": source_hash(policy_hash, pairs),
                "source_ref": source_ref,
            }
        )
    return out


def source_hash_for_row(
    project: Project,
    index: Any,
    source_columns: list[str],
    row_id: int,
) -> str | None:
    """Current source hash for one row, or None when it has no embeddable
    payload (so a stored vector for it is stale by definition)."""
    payloads = build_source_payloads(project, index, source_columns, [row_id])
    return payloads[0]["source_hash"] if payloads else None
