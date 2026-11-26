"""A row error raised after its model call returned fails only that row.

The completion rejects a schema-valid answer for one row; the call already
happened, so its cost and call record are kept on the failed row, the next
row still runs, and the run finishes with a receipt.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from executor_harness import case_env
from frisket.actions.core import RegisteredAction
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.types import DynamicOutput, Row, RowError, RowResult
from frisket.engine.runner import MapRunner
from frisket.engine.store.runs import EXPECTED_ROW_ERROR, RunResultStore
from model_doubles import CannedReplyAdapter
from test_map_translate_executor import CASES, _map_translate_action, _translate_router

_REJECTED = "the answer for this row does not fit its column"


def _rejecting_translate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register map.translate with a completion that refuses one row."""
    registered = ACTION_REGISTRY.get("map.translate")
    run = registered.definition.run
    complete = run.complete

    def reject_first_row(params: Any, row: Row, response: DynamicOutput) -> RowResult:
        if "Bonjour le monde" in json.dumps(dict(row.values), default=str):
            raise RowError("return_schema_mismatch", _REJECTED)
        return complete(params, row, response)

    definition = dataclasses.replace(
        registered.definition, run=dataclasses.replace(run, complete=reject_first_row)
    )
    monkeypatch.setattr(
        ACTION_REGISTRY,
        "_actions",
        {
            **ACTION_REGISTRY._actions,
            "map.translate": RegisteredAction("map.translate", definition),
        },
    )


def _run_rows_with_concurrency(monkeypatch: pytest.MonkeyPatch, concurrency: int):
    init = MapRunner.__init__

    def with_concurrency(self: MapRunner, *args: Any, **kwargs: Any) -> None:
        init(self, *args, **kwargs)
        self.concurrency = concurrency

    monkeypatch.setattr(MapRunner, "__init__", with_concurrency)


@pytest.mark.parametrize("concurrency", [1, 8], ids=["sequential", "parallel"])
def test_row_error_after_the_model_call_keeps_the_call_and_finishes_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, concurrency: int
) -> None:
    _rejecting_translate(monkeypatch)
    _run_rows_with_concurrency(monkeypatch, concurrency)
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        project, sheet_id = env.project, env.seeded["sheet_id"]
        adapter = CannedReplyAdapter(
            {"translation": "Hello world", "detected_language": "fr"},
            tokens_in=54,
            tokens_out=18,
            cost=0.003,
        )
        env.run_kwargs["router"] = _translate_router(adapter, max_retries=0)
        result = env.run(
            _map_translate_action(
                sheet_id,
                idempotency_key=f"map_translate@sha256:row-error-{concurrency}",
            )
        )

        assert result.status == "partial", result.errors
        assert len(adapter.requests) == 2
        assert result.run_id is not None and result.receipt_id is not None
        statement = next(
            c["id"] for c in project.columns(sheet_id) if c["name"] == "statement"
        )
        statements = project.get_values(sheet_id, int(statement))
        translation = next(
            c["id"] for c in project.columns(sheet_id) if c["name"] == "translation"
        )
        rows = {
            statements[int(row["row_id"])]: row
            for row in project.db.execute(
                "SELECT row_id, value, outcome, error, error_code FROM results "
                "WHERE run_id=? AND column_id=?",
                (result.run_id, translation),
            )
        }
        failed = rows["Bonjour le monde"]
        assert failed["outcome"] == EXPECTED_ROW_ERROR
        assert failed["error_code"] == "return_schema_mismatch"
        assert _REJECTED in failed["error"]
        assert failed["value"] is None
        assert rows["Gracias por venir"]["outcome"] == "ok"
        assert rows["Gracias por venir"]["value"] == "Hello world"

        run = project.db.execute(
            "SELECT status, failed_rows, completed_rows, cost_actual FROM runs "
            "WHERE id=?",
            (result.run_id,),
        ).fetchone()
        assert run["status"] != "running"
        assert int(run["failed_rows"]) == 1
        assert int(run["completed_rows"]) == 2
        assert run["cost_actual"] == 0.006
        calls = RunResultStore(project).model_calls(result.run_id)
        assert len(calls) == 2
        assert {call["provider_cost_usd"] for call in calls} == {0.003}
        failed_row_calls = [
            call for call in calls if call["row_id"] == failed["row_id"]
        ]
        assert len(failed_row_calls) == 1
        assert failed_row_calls[0]["provider_cost_usd"] == 0.003
        assert failed_row_calls[0]["attempt_id"] is not None
        assert json.loads(failed_row_calls[0]["units"]) == {
            "tokens_in": 54,
            "tokens_out": 18,
        }
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM receipts WHERE id=?", (result.receipt_id,)
            ).fetchone()[0]
            == 1
        )
