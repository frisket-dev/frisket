"""Stable cell-content identity, separate from current read diagnostics."""

import json
from typing import Any


def evidence_subject_ref(ref: dict[str, Any]) -> dict[str, Any]:
    """Evidence names a saved value; validity/outcome describe its current read."""
    return {
        key: value for key, value in ref.items() if key not in {"outcome", "validity"}
    }


def evidence_subject_ref_json(ref: dict[str, Any]) -> str:
    return json.dumps(
        evidence_subject_ref(ref),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
