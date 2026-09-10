"""Spend dashboard services."""

from __future__ import annotations

from typing import Any

from frisket.ai.llm.pricing import audio_price, model_price
from frisket.server.workspace import Workspace
from frisket.engine.store import Project


class SpendService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def spend_dashboard(self) -> dict[str, Any]:
        """Workspace spend dashboard: what was spent, never what might be.

        No spend forecast. The projection was
        a second money surface that nothing rendered and that would disagree
        with the receipt the moment either drifted.
        """
        rows: list[dict[str, Any]] = []
        total = 0.0
        unknown_models: set[str] = set()
        for meta in self._workspace.list():
            pid = meta["id"]
            project = self._workspace.get(pid)
            for row in project.db.execute(
                "SELECT strftime('%Y-%m', started_at) AS month, model, "
                "COUNT(*) AS runs, "
                "COALESCE(SUM(completed_rows), 0) AS rows, "
                "COALESCE(SUM(cost_actual), 0) AS cost "
                "FROM runs GROUP BY month, model ORDER BY month, model"
            ):
                cost = float(row["cost"] or 0.0)
                total += cost
                rows.append(
                    {
                        "project": pid,
                        "model": row["model"],
                        "month": row["month"],
                        "runs": row["runs"],
                        "rows": row["rows"],
                        "cost": cost,
                    }
                )
            unknown_models |= _unpriced_models_with_usage(project)
        return {
            "rows": rows,
            "total_cost": total,
            "has_unknown_costs": bool(unknown_models),
            "unknown_cost_models": sorted(unknown_models),
        }


def _unpriced_models_with_usage(project: Project) -> set[str]:
    """Every engine on this project whose spend we could not price.

    TWO SURFACES, ONE ANSWER (bug pattern 2). ``has_unknown_costs`` is
    ``bool()`` of this set, and it used to be derived from ``runs.model``
    alone — which a routed capability run leaves NULL. A real billed
    vision-OCR call recorded 257/35 tokens and no cost,
    its own attempt receipt said ``cost_basis: unpriceable, unmetered_calls:
    1``, and this dashboard answered ``has_unknown_costs: false`` about the
    same run. Two computations of one fact, disagreeing.

    So the second half reads the FACTS the receipt's own unmetered counter
    is derived from: a ``model_calls`` row with a NULL ``provider_cost_usd``
    is precisely "a call we could not price". Local and sidecar facts are
    never NULL there (``ModelCallMeta.local``/``.sidecar`` write an explicit
    0.0, ``cost_source="free_local"``), so genuinely free work does not
    appear; cache hits are excluded because a replayed answer costs the
    provider nothing.
    """
    out: set[str] = set()
    for row in project.db.execute(
        "SELECT DISTINCT r.model FROM runs r "
        "WHERE r.model IS NOT NULL AND EXISTS ("
        "  SELECT 1 FROM results res WHERE res.run_id = r.id "
        "  AND (COALESCE(res.tokens_in, 0) > 0 OR COALESCE(res.tokens_out, 0) > 0))"
    ):
        model = row["model"]
        if model_price(model) is None and audio_price(model) is None:
            out.add(model)
    for row in project.db.execute(
        "SELECT DISTINCT engine FROM model_calls "
        "WHERE provider_cost_usd IS NULL AND credential_source != 'cache'"
    ):
        engine = row["engine"]
        if engine:
            out.add(str(engine))
    return out
