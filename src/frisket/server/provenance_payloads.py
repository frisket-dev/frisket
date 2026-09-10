"""Project-level provenance manifest payloads."""

from __future__ import annotations

from typing import Any

from frisket.authoring.action_metadata import (
    action_metadata_for_action_kind,
    run_row_action_kind,
)
from frisket.server.paging import offset_page_meta
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore


def _run_summary(row: Any) -> dict[str, Any]:
    action_metadata = action_metadata_for_action_kind(run_row_action_kind(row))
    action_kind = action_metadata["action_kind"]
    model = row["model"]
    return {
        "run_id": row["id"],
        "sheet_id": row["sheet_id"],
        "action_kind": action_kind,
        "action_name": action_metadata["action_name"],
        "model": model,
        "provider": model.split("/", 1)[0] if model and "/" in model else None,
        "status": row["status"],
        "total_rows": row["total_rows"],
        "completed_rows": row["completed_rows"],
        "failed_rows": row["failed_rows"],
        # Null-preserving, matching run_status.py: an unknown provider cost
        # crosses the wire as null, never as a confident $0.
        "cost": None if row["cost_actual"] is None else float(row["cost_actual"]),
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


def provenance_manifest_payload(
    p: Project,
    project_id: str,
    *,
    runs_offset: int = 0,
    runs_limit: int = 25,
    receipts_offset: int = 0,
    receipts_limit: int = 25,
) -> dict[str, Any]:
    """Build the public project provenance manifest.

    Aggregates remain whole-project summaries. Recent run and receipt detail
    rows are page-bounded because those lists grow with long-lived projects.
    """
    model_rows = p.db.execute(
        "SELECT model, COUNT(*) AS runs, "
        "COALESCE(SUM(completed_rows), 0) AS rows, "
        "COALESCE(SUM(cost_actual), 0) AS cost "
        "FROM runs GROUP BY model ORDER BY model"
    ).fetchall()
    models = []
    touched = []
    total_cost = 0.0
    for row in model_rows:
        model = row["model"]
        if not model:
            continue
        provider = model.split("/", 1)[0] if "/" in model else None
        cost = float(row["cost"] or 0.0)
        total_cost += cost
        touched.append(model)
        models.append(
            {
                "model": model,
                "provider": provider,
                "runs": row["runs"],
                "rows": row["rows"],
                "cost": cost,
            }
        )

    action_kind_rows = p.db.execute(
        "SELECT action_kind, COUNT(*) AS runs, "
        "COALESCE(SUM(completed_rows), 0) AS rows, "
        "COALESCE(SUM(failed_rows), 0) AS failed_rows, "
        "COALESCE(SUM(cost_actual), 0) AS cost "
        "FROM runs GROUP BY action_kind ORDER BY action_kind"
    ).fetchall()
    action_kind_summaries: dict[str, dict[str, Any]] = {}
    for row in action_kind_rows:
        action_metadata = action_metadata_for_action_kind(run_row_action_kind(row))
        action_kind = action_metadata["action_kind"]
        summary = action_kind_summaries.setdefault(
            action_kind,
            {
                "action_kind": action_kind,
                "action_name": action_metadata["action_name"],
                "runs": 0,
                "rows": 0,
                "failed_rows": 0,
                "cost": 0.0,
            },
        )
        summary["runs"] += int(row["runs"] or 0)
        summary["rows"] += int(row["rows"] or 0)
        summary["failed_rows"] += int(row["failed_rows"] or 0)
        summary["cost"] += float(row["cost"] or 0.0)

    # Derived at run grain, not model_calls-fact grain (spend.py's flag),
    # because every aggregate in this payload sums runs.cost_actual: SUM
    # skips NULL rows, so this count is exactly what the totals excluded.
    unknown_row = p.db.execute(
        "SELECT COUNT(*) FROM runs WHERE cost_actual IS NULL"
    ).fetchone()
    unknown_cost_runs = int(unknown_row[0])

    runs_total = int(p.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
    receipt_store = ReceiptStore(p)
    receipts_total = receipt_store.count()
    run_rows = p.db.execute(
        "SELECT id, sheet_id, action_kind, model, status, total_rows, "
        "completed_rows, failed_rows, cost_actual, started_at, finished_at "
        "FROM runs ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
        (runs_limit, runs_offset),
    ).fetchall()
    receipt_rows = receipt_store.recent_metadata_page(
        limit=receipts_limit,
        offset=receipts_offset,
    )

    runs = [_run_summary(row) for row in run_rows]
    receipts = [
        {
            "receipt_id": row.id,
            "action_kind": row.action_kind,
            "status": row.status,
            "run_id": row.run_id,
            "created_at": row.created_at,
        }
        for row in receipt_rows
    ]

    return {
        "project_id": project_id,
        "models": models,
        "touched": touched,
        "providers": sorted({m["provider"] for m in models if m["provider"]}),
        "action_kinds": [
            {**summary, "cost": float(summary["cost"])}
            for summary in sorted(
                action_kind_summaries.values(),
                key=lambda item: item["action_kind"],
            )
        ],
        "runs": runs,
        "runs_page": offset_page_meta(
            schema_version="frisket.provenance_runs_page.v1",
            order="desc",
            offset=runs_offset,
            limit=runs_limit,
            total=runs_total,
            item_count=len(runs),
        ),
        "receipts": receipts,
        "receipts_page": offset_page_meta(
            schema_version="frisket.provenance_receipts_page.v1",
            order="desc",
            offset=receipts_offset,
            limit=receipts_limit,
            total=receipts_total,
            item_count=len(receipts),
        ),
        "total_cost": total_cost,
        "has_unknown_costs": unknown_cost_runs > 0,
        "unknown_cost_runs": unknown_cost_runs,
    }
