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
