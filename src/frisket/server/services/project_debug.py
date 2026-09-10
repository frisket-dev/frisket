"""Project debug service for local server routes."""

from __future__ import annotations

import json
from typing import Any

from frisket.authoring.action_metadata import (
    action_metadata_for_action_kind,
    run_row_action_kind,
)
from frisket.ai.llm import ResponseCache
from frisket.engine.store.runs import FAILURE_OUTCOMES, outcome_sql_list
from frisket.server.workspace import Workspace


def _spec(raw: str | None) -> Any:
    try:
        return json.loads(raw or "{}")
    except ValueError:
        return {}


class ProjectDebugService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def debug(self, project_id: str) -> dict[str, Any]:
        p = self._workspace.get(project_id)
        cursor = p.op_cursor

        ops = [
            {
                "id": o["id"],
                "kind": o["kind"],
                "label": o["label"],
                "spec": _spec(o["spec"]),
                "status": o["status"],
                "barrier": bool(o["barrier"]),
                "at_cursor": o["id"] == cursor,
                "created_at": o["created_at"],
            }
            for o in p.history()
        ]

        run_rows = p.db.execute("SELECT * FROM runs ORDER BY id").fetchall()
        runs = [self._debug_run(project_id, row) for row in run_rows]

        results_summary = [
            {
                "run_id": r["run_id"],
                "cells": r["cells"],
                "errors": r["errors"],
                "columns": r["columns"],
                "tokens_in": r["tokens_in"],
                "tokens_out": r["tokens_out"],
            }
            for r in p.db.execute(
                "SELECT run_id, COUNT(*) AS cells, "
                f"SUM(CASE WHEN outcome IN ({outcome_sql_list(FAILURE_OUTCOMES)}) "
                "THEN 1 ELSE 0 END) AS errors, "
                "COUNT(DISTINCT column_id) AS columns, "
                "COALESCE(SUM(tokens_in), 0) AS tokens_in, "
                "COALESCE(SUM(tokens_out), 0) AS tokens_out "
                "FROM results GROUP BY run_id ORDER BY run_id"
            )
        ]

        run_by_id = {r["id"]: r for r in run_rows}
        lineage = []
        for sheet in p.sheets():
            cols = []
            for column in p.columns(sheet["id"], include_hidden=True):
                entry: dict[str, Any] = {
                    "id": column["id"],
                    "name": column["name"],
                    "type": column["type"],
                    "ai_generated": bool(column["ai_generated"]),
                    "hidden": bool(column["hidden"]),
                    "current_run_id": column["current_run_id"],
                }
                src = run_by_id.get(column["current_run_id"])
                if src is not None:
                    entry["derived_from"] = self._derived_from_payload(src)
                cols.append(entry)
            lineage.append(
                {
                    "sheet_id": sheet["id"],
                    "name": sheet["name"],
                    "parent_sheet_id": sheet["parent_sheet_id"],
                    "parent_op_id": sheet["parent_op_id"],
                    "columns": cols,
                }
            )

        return {
            "project_id": project_id,
            "name": getattr(p, "name", None) or project_id,
            "op_cursor": cursor,
            "ops": ops,
            "runs": runs,
            "results": results_summary,
            "cache": self._cache_stats(p.path),
            "lineage": lineage,
            "active_runs": sum(
                1 for prog in self._workspace.active_runs.values() if not prog.done
            ),
        }

    def _debug_run(self, project_id: str, row: Any) -> dict[str, Any]:
        action_metadata = action_metadata_for_action_kind(run_row_action_kind(row))
        return {
            "run_id": row["id"],
            "op_id": row["op_id"],
            "sheet_id": row["sheet_id"],
            **action_metadata,
            "action_version": row["action_version"],
            "model": row["model"],
            "status": row["status"],
            "total_rows": row["total_rows"],
            "completed_rows": row["completed_rows"],
            "failed_rows": row["failed_rows"],
            "cost_estimate": row["cost_estimate"],
            "cost_actual": row["cost_actual"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "live": (prog := self._workspace.active_runs.get((project_id, row["id"])))
            is not None
            and not prog.done,
        }

    def _cache_stats(self, project_path: Any) -> dict[str, Any]:
        if self._workspace._router is not None:  # noqa: SLF001
            rcache = self._workspace._router.cache  # noqa: SLF001
            return {
                "mode": self._workspace._router.cache_mode,  # noqa: SLF001
                "enabled": rcache is not None,
                "entries": rcache.count() if rcache is not None else 0,
            }

        cache_file = project_path / "project.cache.db"
        cache_stats = {
            "mode": "replay",
            "enabled": cache_file.exists(),
            "entries": 0,
        }
        if cache_file.exists():
            cache = ResponseCache(cache_file)
            cache_stats["entries"] = cache.count()
            cache.close()
        return cache_stats

    def _derived_from_payload(self, row: Any) -> dict[str, Any]:
        action_metadata = action_metadata_for_action_kind(run_row_action_kind(row))
        return {
            "run_id": row["id"],
            **action_metadata,
            "action_version": row["action_version"],
            "model": row["model"],
            "input_columns": _spec(row["params"]).get("input_columns") or [],
        }
