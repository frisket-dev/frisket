from __future__ import annotations

import json
from typing import Any

from .runs import FAILURE_OUTCOMES, outcome_sql_list

TranscriptStatus = str  # 'missing' | 'partial' | 'complete_visible' | 'complete_hidden'


def compute_transcript_statuses(
    project: Any, sheet_id: int, columns: list[Any]
) -> dict[int, TranscriptStatus]:
    """``column_id -> transcriptStatus`` for every audio/video column in
    ``columns``. Non-media columns are simply absent from the returned dict
    (the caller — ``sheet_grid._sheet_data_payload`` — reads it with
    ``.get(col_id)``, so a miss renders as ``None``)."""
    media_columns = [c for c in columns if c["type"] in ("audio", "video")]
    if not media_columns:
        return {}

    name_to_id = {
        c["name"]: int(c["id"]) for c in project.columns(sheet_id, include_hidden=True)
    }

    candidates = project.db.execute(
        "SELECT ops.spec AS spec, runs.id AS run_id "
        "FROM runs JOIN ops ON ops.id = runs.op_id "
        "WHERE runs.sheet_id = ? AND runs.action_kind = 'media.transcribe' "
        "AND ops.status != 'undone' "
        "ORDER BY runs.id DESC",
        (sheet_id,),
    ).fetchall()

    # Newest-wins: the first (highest run id) candidate that resolves to a
    # given media column id is kept; later (older) matches for the same
    # column id are ignored.
    newest_run_by_media_id: dict[int, int] = {}
    for row in candidates:
        try:
            spec = json.loads(row["spec"] or "{}")
        except (TypeError, ValueError):
            continue
        input_columns = spec.get("input_columns") or []
        if not input_columns:
            continue
        source_id = name_to_id.get(input_columns[0])
        if source_id is None or source_id in newest_run_by_media_id:
            continue
        newest_run_by_media_id[source_id] = int(row["run_id"])

    statuses: dict[int, TranscriptStatus] = {}
    for col in media_columns:
        col_id = int(col["id"])
        run_id = newest_run_by_media_id.get(col_id)
        if run_id is None:
            statuses[col_id] = "missing"
            continue
        statuses[col_id] = _status_for_run(project, sheet_id, col_id, run_id)
    return statuses


def _status_for_run(
    project: Any, sheet_id: int, media_column_id: int, run_id: int
) -> TranscriptStatus:
    row_ids = [
        int(r["row_id"])
        for r in project.db.execute(
            "SELECT row_id FROM run_rows WHERE run_id=? ORDER BY position",
            (run_id,),
        )
    ]
    if not row_ids:
        return "missing"

    media_values = project.get_values(sheet_id, media_column_id, row_ids=row_ids)
    non_empty_row_ids = [
        rid
        for rid in row_ids
        if rid in media_values and not _is_empty(media_values[rid])
    ]
    if not non_empty_row_ids:
        return "missing"

    transcript_col = project.db.execute(
        "SELECT column.id, column.hidden FROM run_output_generations generation "
        "JOIN columns column ON column.id=generation.column_id "
        "WHERE column.sheet_id=? AND generation.run_id=? "
        "AND column.type='timestamped_transcript'",
        (sheet_id, run_id),
    ).fetchone()
    if transcript_col is None:
        # Clean cutover: only a current first-class timestamped transcript is
        # a transcribe result. Legacy text/JSON output pairs are not inferred.
        return "missing"

    placeholders = ",".join("?" for _ in non_empty_row_ids)
    successful_count = project.db.execute(
        "SELECT COUNT(*) FROM results "
        f"WHERE run_id=? AND column_id=? AND row_id IN ({placeholders}) "
        f"AND outcome NOT IN ({outcome_sql_list(FAILURE_OUTCOMES)}) "
        "AND value IS NOT NULL",
        (run_id, transcript_col["id"], *non_empty_row_ids),
    ).fetchone()[0]

    if successful_count == 0:
        return "missing"
    if successful_count < len(non_empty_row_ids):
        return "partial"
    return "complete_hidden" if transcript_col["hidden"] else "complete_visible"


def _is_empty(value: Any) -> bool:
    """Same emptiness shape as ``map_runner._is_empty_cell_value``: None,
    blank string, or empty collection are empty; 0/False/non-blank text are
    legitimate data."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (dict, list, tuple)):
        return len(value) == 0
    return False
