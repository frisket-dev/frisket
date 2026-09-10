"""Grounded-extract support (segment injection, evidence unwrapping, image
parts), extracted from map_runner.py so leaf callers need not import the
engine. Operates on an explicit ``project`` rather than an implicit ``self``.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from frisket.engine.store import Project
from frisket.engine.store.blob_backend import BlobNotFoundError

MAX_IMAGE_BYTES = 5 * 1024 * 1024


def grounding_enabled(spec: dict[str, Any]) -> bool:
    return (
        spec.get("action_kind") == "map.extract"
        and isinstance(spec.get("grounding"), dict)
        and bool(spec["grounding"].get("enabled"))
    )


def looks_like_segment_list(value: Any) -> bool:
    """A transcribe-shaped segment list (``[{text, start, ...}]``) — the shape
    ExtractRecipe already numbers via the sibling-column W2.3 path. Kept in sync
    with ``sdk.ops.extract._segment_stream_columns``'s detection."""
    return (
        isinstance(value, list)
        and bool(value)
        and all(
            isinstance(item, dict) and "text" in item and "start" in item
            for item in value
        )
    )


def unwrap_grounded_value(
    field_name: str,
    raw_value: Any,
) -> tuple[Any, dict[str, Any] | None]:
    if isinstance(raw_value, dict) and (
        "value" in raw_value or "evidence" in raw_value or "warnings" in raw_value
    ):
        evidence = raw_value.get("evidence")
        warnings = raw_value.get("warnings")
        return raw_value.get("value"), {
            "field": field_name,
            "evidence": evidence if isinstance(evidence, list) else [],
            "warnings": warnings if isinstance(warnings, list) else [],
            "raw": {
                key: value
                for key, value in raw_value.items()
                if key not in {"value", "evidence", "warnings"}
            },
        }
    if raw_value is None:
        return None, None
    return raw_value, {
        "field": field_name,
        "evidence": [],
        "warnings": ["evidence_missing"],
        "raw": {},
    }


def inject_grounding_segments(
    project: Project, spec: dict, row_id: int, values: dict[str, Any]
) -> None:
    """extract-scalar-temporal-anchors-v1 (segment-index default): when a
    grounded extract's row resolves a transcript segment stream, expose the
    numbered segments to the prompt EVEN IF the selected input columns are
    only the free-text ``transcript`` (never the sibling
    ``transcript_segments`` json). ExtractRecipe.render then numbers them and
    asks the model to cite ``segment_indices`` (strategy B, the reliable
    lookup path) instead of a positionless quote that a long/multi-segment
    answer cannot align (the ZERO-links live failure: the model's one big
    quote spanned ~40s and matched no single segment at threshold 0.8). The
    stream is resolved independent of column selection so the indices match
    the transcribe writer's own spans that resolution reuses."""
    if not grounding_enabled(spec):
        return
    # Skip when a segment-list column is already present (the model already
    # sees numbered segments via the existing sibling-column path).
    if any(looks_like_segment_list(value) for value in values.values()):
        return
    from frisket.engine.store.transcript_segment_stream import (
        resolve_transcript_segment_stream,
    )

    resolved = resolve_transcript_segment_stream(
        project, sheet_id=spec["sheet_id"], row_id=row_id
    )
    if resolved is None:
        return
    segments: list[dict[str, Any]] = []
    for unit in resolved.anchors.units:
        span = unit.spans[0] if unit.spans else None
        start_ms = span.start_ms if span is not None else None
        end_ms = span.end_ms if span is not None else None
        segment: dict[str, Any] = {
            "segment_index": unit.unit_id,
            "text": unit.text,
            "start": start_ms / 1000 if start_ms is not None else None,
            "end": end_ms / 1000 if end_ms is not None else None,
        }
        # Preserve the per-turn speaker so grounded extraction attributes
        # a cited statement to the right speaker.
        if unit.speaker:
            segment["speaker"] = unit.speaker
        segments.append(segment)
    if not segments:
        return
    key = "transcript_segments"
    while key in values:
        key = f"_{key}"
    values[key] = segments


def image_part(project: Project, val: Any, *, max_image_bytes: int) -> dict | None:
    """Image columns reach LLM recipes as base64 parts, not text."""
    mime = "image/jpeg"
    if isinstance(val, dict) and val.get("blob"):
        mime = val.get("mime", mime)
        try:
            with project.materialize_blob(str(val["blob"])) as raw_path:
                path = Path(raw_path)
                if path.stat().st_size > max_image_bytes:
                    return None
                data = path.read_bytes()
        except (BlobNotFoundError, OSError, ValueError):
            return None
        return {
            "__image_b64__": base64.b64encode(data).decode(),
            "mime": mime,
        }
    elif isinstance(val, str) and not val.startswith(("http://", "https://")):
        path = Path(val)
        if path.suffix.lower() == ".png":
            mime = "image/png"
    else:
        return None
    if not path.exists():
        return None
    if path.stat().st_size > max_image_bytes:
        return None  # text reference beats blowing the context window
    data = path.read_bytes()
    return {"__image_b64__": base64.b64encode(data).decode(), "mime": mime}
