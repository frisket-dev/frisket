"""Exact row provenance for durable temporal finder results.

Topic analysis facts travel through one hidden MapRunner output instead of the
editable ``timeline_ranges`` value.  Consumers resolve that sidecar by the
selected column's producing run and row.  The hidden column's *current* run is
irrelevant: a later finder invocation may legitimately repoint it, while old
run results remain addressable by their exact identity.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from frisket.engine.store.artifact_timeline import canonical_json_hash
from frisket.features.temporal_values import (
    canonical_temporal_hash,
    parse_temporal_value,
)
from frisket.features.topic_segmentation.contracts import (
    TOPIC_ANALYSIS_SIDECAR_COLUMN,
    TOPIC_ANALYSIS_SIDECAR_SCHEMA_VERSION,
)


@dataclass(frozen=True, slots=True)
class ResolvedTopicAnalysisSidecar:
    sheet_id: int
    row_id: int
    selection_column_id: int
    run_id: int
    sidecar_column_id: int
    payload: dict[str, Any]

    def receipt_ref(self) -> dict[str, Any]:
        """Return the small exact input ref embedded in downstream receipts."""

        return {
            "kind": "topic_analysis_snapshot",
            "sheet_id": self.sheet_id,
            "row_id": self.row_id,
            "selection_column_id": self.selection_column_id,
            "run_id": self.run_id,
            "sidecar_column_id": self.sidecar_column_id,
            "payload_hash": canonical_json_hash(self.payload),
            "transcript_evidence_id": self.payload["transcript_evidence_id"],
            "transcript_snapshot_hash": self.payload["transcript_snapshot_hash"],
        }


def resolve_topic_section_unit_ids(
    sidecar: ResolvedTopicAnalysisSidecar,
    selection_value: Any,
) -> dict[str, frozenset[str]] | None:
    """Return locked chunk identities only for the unchanged generated value."""

    try:
        ranges = parse_temporal_value("timeline_ranges", selection_value)
    except (LookupError, TypeError, ValueError, ValidationError):
        return None
    locking = sidecar.payload.get("locking")
    if not isinstance(locking, dict) or locking.get(
        "timeline_ranges_hash"
    ) != canonical_temporal_hash("timeline_ranges", ranges):
        return None
    raw = locking.get("section_unit_ids")
    if not isinstance(raw, dict):
        return None
    item_ids = {item.id for item in ranges.items}
    if set(raw) != item_ids:
        return None

    resolved: dict[str, frozenset[str]] = {}
    for item_id, values in raw.items():
        if (
            not isinstance(item_id, str)
            or not isinstance(values, list)
            or any(not isinstance(value, str) or not value for value in values)
            or len(values) != len(set(values))
        ):
            return None
        resolved[item_id] = frozenset(values)
    return resolved


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _valid_payload(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if value.get("schema_version") != TOPIC_ANALYSIS_SIDECAR_SCHEMA_VERSION:
        return None
    for key in (
        "transcript_evidence_id",
        "transcript_snapshot_hash",
        "transcript_value_hash",
        "artifact_stable_id",
        "engine_id",
        "engine_version",
    ):
        item = value.get(key)
        if not isinstance(item, str) or not item.strip():
            return None
    if not _positive_int(value.get("transcript_run_id")) or not _positive_int(
        value.get("transcript_column_id")
    ):
        return None
    if not isinstance(value.get("timeline"), dict):
        return None
    if not isinstance(value.get("transcript_value_ref"), dict):
        return None
    if not isinstance(value.get("resolved_settings"), dict):
        return None
    if not isinstance(value.get("native_candidates"), list):
        return None
    if not isinstance(value.get("locking"), dict):
        return None
    return dict(value)


def resolve_topic_analysis_sidecar(
    project: Any,
    *,
    sheet_id: int,
    row_id: int,
    selection_column_id: int,
) -> ResolvedTopicAnalysisSidecar | None:
    """Resolve the exact Topic finder sidecar behind one selected range cell.

    A manual edit remains attributable when it overlays a successful Topic
    finder result in the same column.  A copied/imported temporal value, a
    different action's output, a missing row result, or a changed timeline has
    no producing Topic analysis and returns ``None``.
    """

    selection_column = project.db.execute(
        "SELECT id, type FROM columns WHERE id=? AND sheet_id=? AND hidden=0",
        (selection_column_id, sheet_id),
    ).fetchone()
    if selection_column is None or str(selection_column["type"]) != "timeline_ranges":
        return None

    live_values, live_refs = project.get_values_with_refs(
        sheet_id,
        selection_column_id,
        row_ids=[row_id],
    )
    _base_values, base_refs = project.get_values_with_refs(
        sheet_id,
        selection_column_id,
        row_ids=[row_id],
        apply_edits=False,
    )
    base_ref = base_refs.get(row_id)
    if not isinstance(base_ref, dict) or base_ref.get("kind") != "run_result":
        return None
    run_id = base_ref.get("run_id")
    if not _positive_int(run_id):
        return None
    run_id = int(run_id)

    run = project.db.execute(
        "SELECT id, op_id, params, action_kind FROM runs WHERE id=? AND sheet_id=? "
        "AND status IN ('completed', 'partial')",
        (run_id, sheet_id),
    ).fetchone()
    if run is None:
        return None
    try:
        spec = json.loads(run["params"])
    except (TypeError, ValueError):
        return None
    sidecar_names = spec.get("topic_analysis_sidecars")
    typed = isinstance(sidecar_names, list) and all(
        isinstance(name, str) for name in sidecar_names
    )
    if not typed:
        if run["action_kind"] != "map.find_topic_sections":
            return None
        sidecar_names = [TOPIC_ANALYSIS_SIDECAR_COLUMN]
    live_ref = live_refs.get(row_id)
    if not isinstance(live_ref, dict):
        return None
    if live_ref.get("kind") == "run_result":
        if live_ref.get("run_id") != run_id:
            return None
    elif live_ref.get("kind") == "manual_edit":
        edit_op_id = live_ref.get("op_id")
        run_op_id = run["op_id"]
        if (
            not _positive_int(edit_op_id)
            or not _positive_int(run_op_id)
            or int(edit_op_id) <= int(run_op_id)
        ):
            # Edit refs do not retain their underlying generated-value ref.
            # A run created after the edit therefore cannot truthfully be
            # claimed as its producer.  Keep the useful edited-after-run case,
            # but fail closed once a later run repoints the column.
            return None
    else:
        return None
    try:
        live_value = parse_temporal_value(
            "timeline_ranges",
            live_values.get(row_id),
        ).model_dump(mode="json")
    except (LookupError, TypeError, ValueError, ValidationError):
        return None

    matches = []
    for result in project.db.execute(
        "SELECT c.id, c.name, r.value FROM columns c JOIN results r ON r.column_id=c.id "
        "WHERE c.sheet_id=? AND c.type='json' AND c.hidden=1 "
        "AND r.run_id=? AND r.row_id=? AND r.error IS NULL AND r.value IS NOT NULL",
        (sheet_id, run_id, row_id),
    ).fetchall():
        if result["name"] not in sidecar_names:
            continue
        try:
            payload = _valid_payload(json.loads(result["value"]))
        except (TypeError, ValueError):
            continue
        if payload is None or payload["timeline"] != live_value["timeline"]:
            continue
        if typed and payload.get("range_column_id") != selection_column_id:
            continue
        matches.append((int(result["id"]), payload))
    if len(matches) != 1:
        return None
    sidecar_column_id, payload = matches[0]

    return ResolvedTopicAnalysisSidecar(
        sheet_id=sheet_id,
        row_id=row_id,
        selection_column_id=selection_column_id,
        run_id=run_id,
        sidecar_column_id=sidecar_column_id,
        payload=payload,
    )


def resolve_topic_analysis_receipt_ref(
    project: Any,
    value: Any,
) -> ResolvedTopicAnalysisSidecar | None:
    """Re-resolve an exact downstream receipt ref through the current selection."""

    if not isinstance(value, dict) or value.get("kind") != "topic_analysis_snapshot":
        return None
    sheet_id = value.get("sheet_id")
    row_id = value.get("row_id")
    selection_column_id = value.get("selection_column_id")
    if not all(_positive_int(item) for item in (sheet_id, row_id, selection_column_id)):
        return None
    resolved = resolve_topic_analysis_sidecar(
        project,
        sheet_id=int(sheet_id),
        row_id=int(row_id),
        selection_column_id=int(selection_column_id),
    )
    if resolved is None or resolved.receipt_ref() != value:
        return None
    return resolved


__all__ = [
    "ResolvedTopicAnalysisSidecar",
    "resolve_topic_analysis_receipt_ref",
    "resolve_topic_analysis_sidecar",
    "resolve_topic_section_unit_ids",
]
