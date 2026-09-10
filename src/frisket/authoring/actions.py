"""Stable JSON-first action payloads for programmatic callers."""

from __future__ import annotations

import json
from typing import Any

from frisket.authoring.action_metadata import (
    action_metadata_for_action_kind,
    run_row_action_kind,
)
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from frisket.operability.trace import read_trace, read_trace_row

ACTION_SCHEMA_VERSION = "frisket.actions.v1"

ACTION_NAMES = (
    "describe_project",
    "list_sheets",
    "read_range",
    "estimate_run",
    "run_action",
    "run_status",
    "run_trace",
    "run_trace_row",
    "export_project",
)

TRACE_ROW_EVIDENCE_STATUSES = (
    "recorded",
    "not_recorded",
    "row_not_in_run",
    "missing",
)


def action_schema() -> dict[str, Any]:
    return {
        "schema_version": ACTION_SCHEMA_VERSION,
        "actions": [
            {
                "name": name,
                "payload": "application/json",
                "response": "application/json",
            }
            for name in ACTION_NAMES
        ],
    }


def _project_name(project: Project, project_id: str) -> str:
    return str(project.get_meta("name", project_id) or project_id)


def _column_payload(column: Any) -> dict[str, Any]:
    keys = set(column.keys()) if hasattr(column, "keys") else set()
    return {
        "id": int(column["id"]),
        "name": column["name"],
        "type": column["type"],
        "ai_generated": bool(column["ai_generated"])
        if "ai_generated" in keys
        else False,
        "format": column["format"] if "format" in keys else None,
        "current_run_id": column["current_run_id"]
        if "current_run_id" in keys
        else None,
    }


def list_sheets(project_id: str, project: Project) -> dict[str, Any]:
    return {
        "schema_version": ACTION_SCHEMA_VERSION,
        "action": "list_sheets",
        "project_id": project_id,
        "sheets": [
            {
                "id": int(sheet["id"]),
                "name": sheet["name"],
                "parent_sheet_id": sheet["parent_sheet_id"],
                "rows": project.row_count(sheet["id"]),
                "columns": [
                    _column_payload(column)
                    for column in project.columns(int(sheet["id"]))
                ],
            }
            for sheet in project.sheets()
        ],
    }


def describe_project(project_id: str, project: Project) -> dict[str, Any]:
    sheets = list_sheets(project_id, project)["sheets"]
    runs = project.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 20").fetchall()
    return {
        "schema_version": ACTION_SCHEMA_VERSION,
        "action": "describe_project",
        "project": {
            "id": project_id,
            "name": _project_name(project, project_id),
            "format_version": project.get_meta("format_version"),
        },
        "sheets": sheets,
        "runs": [
            {
                "id": int(run["id"]),
                "sheet_id": int(run["sheet_id"]),
                **action_metadata_for_action_kind(run_row_action_kind(run)),
                "status": run["status"],
                "total_rows": int(run["total_rows"] or 0),
                "completed_rows": int(run["completed_rows"] or 0),
                "failed_rows": int(run["failed_rows"] or 0),
            }
            for run in runs
        ],
        "actions": list(ACTION_NAMES),
    }


def read_range(
    project_id: str,
    project: Project,
    *,
    sheet_id: int,
    offset: int = 0,
    limit: int = 50,
    columns: list[str] | None = None,
) -> dict[str, Any]:
    offset = max(0, int(offset))
    limit = min(max(1, int(limit)), 1000)
    sheet = next((s for s in project.sheets() if int(s["id"]) == sheet_id), None)
    if sheet is None:
        raise KeyError(f"no sheet '{sheet_id}'")
    all_columns = project.columns(sheet_id)
    wanted = set(columns or [])
    selected = [
        column for column in all_columns if not wanted or column["name"] in wanted
    ]
    rows = project.db.execute(
        "SELECT id, position FROM rows WHERE sheet_id=? AND hidden=0 "
        "ORDER BY position ASC LIMIT ? OFFSET ?",
        (sheet_id, limit, offset),
    ).fetchall()
    row_ids = [int(row["id"]) for row in rows]
    values_by_column = {
        column["name"]: project.get_values(sheet_id, column["id"], row_ids=row_ids)
        for column in selected
    }
    return {
        "schema_version": ACTION_SCHEMA_VERSION,
        "action": "read_range",
        "project_id": project_id,
        "sheet": {"id": int(sheet["id"]), "name": sheet["name"]},
        "offset": offset,
        "limit": limit,
        "total": project.row_count(sheet_id),
        "columns": [_column_payload(column) for column in selected],
        "rows": [
            {
                "row_id": int(row["id"]),
                "row_index": int(row["position"]),
                "cells": {
                    column["name"]: values_by_column[column["name"]].get(int(row["id"]))
                    for column in selected
                },
            }
            for row in rows
        ],
    }


def run_status(project_id: str, project: Project, run_id: int) -> dict[str, Any]:
    row = project.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(f"no run '{run_id}'")
    metadata = action_metadata_for_action_kind(run_row_action_kind(row))
    action_kind = metadata["action_kind"]
    run_payload: dict[str, Any] = {
        "id": int(row["id"]),
        "sheet_id": int(row["sheet_id"]),
        "action_kind": action_kind,
        "action_name": metadata["action_name"],
        "status": row["status"],
        "total_rows": int(row["total_rows"] or 0),
        "completed_rows": int(row["completed_rows"] or 0),
        "failed_rows": int(row["failed_rows"] or 0),
        "cost_actual": row["cost_actual"],
        "cost_estimate": row["cost_estimate"],
    }
    # Distinct per-row failure messages grouped with counts + example row
    # ids. Omitted (not stamped {}) when nothing failed.
    row_errors = RunResultStore(project).row_error_summary(run_id)
    if row_errors is not None:
        run_payload["row_errors"] = row_errors
    return {
        "schema_version": ACTION_SCHEMA_VERSION,
        "action": "run_status",
        "project_id": project_id,
        "run": run_payload,
    }


def run_trace(project_id: str, project: Project, run_id: int) -> dict[str, Any]:
    row = project.db.execute("SELECT id FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(f"no run '{run_id}'")
    trace = read_trace(project.path, run_id)
    if trace is None:
        raise FileNotFoundError("no trace recorded for this run")
    metadata = action_metadata_for_action_kind(trace.get("action_kind"))
    action_kind = metadata["action_kind"]
    public_trace = dict(trace)
    public_trace["action_kind"] = action_kind
    public_trace["action_name"] = metadata["action_name"]
    return {
        "schema_version": ACTION_SCHEMA_VERSION,
        "action": "run_trace",
        "project_id": project_id,
        "run_id": run_id,
        "recorded": True,
        "trace": public_trace,
    }


def _run_scope_contains_row(project: Project, run: Any, row_id: int) -> bool:
    marker = project.db.execute(
        "SELECT 1 FROM run_scopes WHERE run_id=? LIMIT 1",
        (run["id"],),
    ).fetchone()
    if marker is not None:
        return (
            project.db.execute(
                "SELECT 1 FROM run_rows WHERE run_id=? AND row_id=? LIMIT 1",
                (run["id"], row_id),
            ).fetchone()
            is not None
        )
    try:
        params = json.loads(run["params"] or "{}")
    except (TypeError, json.JSONDecodeError):
        params = {}
    row_ids = params.get("row_ids") if isinstance(params, dict) else None
    if isinstance(row_ids, list):
        parsed_row_ids: set[int] = set()
        for candidate in row_ids:
            try:
                parsed_row_ids.add(int(candidate))
            except (TypeError, ValueError):
                continue
        return row_id in parsed_row_ids
    return (
        project.db.execute(
            "SELECT 1 FROM rows WHERE id=? AND sheet_id=? LIMIT 1",
            (row_id, run["sheet_id"]),
        ).fetchone()
        is not None
    )


def _result_cell_payload(
    project: Project, *, run_id: int, row_id: int, column_id: int | None
) -> dict[str, Any] | None:
    if column_id is None:
        return None
    row = project.db.execute(
        "SELECT value, error, confidence, justification, review_state "
        "FROM results WHERE run_id=? AND row_id=? AND column_id=?",
        (run_id, row_id, column_id),
    ).fetchone()
    if row is None:
        return None
    value = json.loads(row["value"]) if row["value"] is not None else None
    return {
        "row_id": row_id,
        "column_id": column_id,
        "value": value,
        "error": row["error"],
        "confidence": row["confidence"],
        "justification": row["justification"],
        "review_state": row["review_state"],
    }


def _trace_row_absence_payload(
    project_id: str,
    run: Any,
    *,
    row_id: int,
    column_id: int | None,
    status: str,
    absence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in TRACE_ROW_EVIDENCE_STATUSES:
        raise ValueError(f"unknown trace row status {status!r}")
    metadata = action_metadata_for_action_kind(run_row_action_kind(run))
    return {
        "schema_version": ACTION_SCHEMA_VERSION,
        "action": "run_trace_row",
        "project_id": project_id,
        "run_id": int(run["id"]),
        "row_id": row_id,
        "column_id": column_id,
        "recorded": False,
        "status": status,
        "state": status,
        # Run-level facts remain available when a best-effort trace was
        # unavailable, so "Explain this cell" can show useful provenance for a
        # deterministic (non-LLM) op — what ran, on which engine, how long it
        # took, what it cost — instead of a dead-end "no trace" message.
        "run": {
            "id": int(run["id"]),
            "sheet_id": int(run["sheet_id"]),
            "action_kind": metadata["action_kind"],
            "action_name": metadata["action_name"],
            "status": run["status"],
            "model": run["model"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "cost_actual": run["cost_actual"],
        },
        "trace": None,
        "cell": None,
        "absence": absence,
    }


def run_trace_row(
    project_id: str,
    project: Project,
    run_id: int,
    row_id: int,
    *,
    column_id: int | None = None,
) -> dict[str, Any]:
    run = project.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise KeyError(f"no run '{run_id}'")
    if not _run_scope_contains_row(project, run, row_id):
        return _trace_row_absence_payload(
            project_id,
            run,
            row_id=row_id,
            column_id=column_id,
            status="row_not_in_run",
        )
    trace = read_trace_row(project.path, run_id, row_id)
    if trace is None:
        return _trace_row_absence_payload(
            project_id,
            run,
            row_id=row_id,
            column_id=column_id,
            status="not_recorded",
        )
    if trace.get("row") is None:
        return _trace_row_absence_payload(
            project_id,
            run,
            row_id=row_id,
            column_id=column_id,
            status="missing",
        )
    action_kind = trace.get("action_kind") or run_row_action_kind(run)
    metadata = action_metadata_for_action_kind(action_kind)
    action_kind = metadata["action_kind"]
    public_trace = dict(trace)
    public_trace["action_kind"] = action_kind
    public_trace["action_name"] = metadata["action_name"]
    return {
        "schema_version": ACTION_SCHEMA_VERSION,
        "action": "run_trace_row",
        "project_id": project_id,
        "run_id": run_id,
        "row_id": row_id,
        "column_id": column_id,
        "recorded": True,
        "status": "recorded",
        "state": "recorded",
        "run": {
            "id": int(run["id"]),
            "sheet_id": int(run["sheet_id"]),
            "action_kind": action_kind,
            "action_name": metadata["action_name"],
            "status": run["status"],
            "model": run["model"],
        },
        "trace": public_trace,
        "cell": _result_cell_payload(
            project, run_id=run_id, row_id=row_id, column_id=column_id
        ),
        "absence": None,
    }


def export_links(project_id: str) -> dict[str, Any]:
    base = f"/api/projects/{project_id}"
    return {
        "schema_version": ACTION_SCHEMA_VERSION,
        "action": "export_project",
        "project_id": project_id,
        "exports": {
            "bundle": f"{base}/export",
            "database": f"{base}/export?mode=db",
            "sheet_csv": f"{base}/exports/sheets?sheet_id={{sheet_id}}&format=csv",
            "work_log_markdown": f"{base}/export/work-log.md",
            "work_log_html": f"{base}/export/work-log.html",
            "work_log_pdf": f"{base}/export/work-log.pdf",
        },
    }


def stable_json(data: dict[str, Any]) -> str:
    """CLI/MCP helper: one-line deterministic JSON for an action payload."""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
