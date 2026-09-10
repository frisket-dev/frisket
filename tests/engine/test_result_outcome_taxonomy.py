from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from frisket.ai.llm import ModelRouter
from frisket.engine.runner.row_execution import AdaptiveThrottle, execute_row
from frisket.engine.store import Project
from frisket.engine.store.runs import (
    FAILURE_OUTCOMES,
    TERMINAL_FAILURE_OUTCOMES,
    TERMINAL_OUTCOMES,
    RunResultStore,
    _result_outcome,
)
from frisket.engine.runner.batch_normalization import normalize_batch_row
from frisket.ops.base import OpContext, Recipe
from helpers import write_claimed_test_results


@dataclass
class _LegacyOmissionRecipe(Recipe):
    name: str = "extract"
    llm: bool = False

    def output_fields(self, spec: dict[str, Any]) -> list[dict[str, Any]]:
        del spec
        return [
            {"name": "present", "column_type": "text"},
            {"name": "missing", "column_type": "text"},
        ]

    async def execute(
        self,
        values: dict[str, Any],
        spec: dict[str, Any],
        ctx: OpContext,
    ) -> dict[str, Any]:
        del values, ctx
        return {
            "present": "value",
            **({"error": "legacy error"} if spec.get("with_error") else {}),
        }


def _seed(tmp_path: Path) -> tuple[Project, int, int, list[int], int]:
    project = Project.create(tmp_path / "outcomes.frisket", name="O")
    sheet_id = project.add_sheet("Rows")
    text = project.add_column(sheet_id, "text", type="text")
    out = project.add_column(sheet_id, "out", type="text", ai_generated=True)
    project.add_rows(
        sheet_id,
        [{"text": t} for t in ("a", "b", "c", "d", "e", "f")],
        {"text": text},
    )
    row_ids = [
        int(r["id"])
        for r in project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (sheet_id,)
        )
    ]
    op_id = project.append_op("map", {"recipe": "x"}, label="x")
    return project, sheet_id, out, row_ids, op_id


def test_outcome_constants_partition_failure_and_terminal() -> None:
    # the axes must not overlap (a result is failed XOR terminal XOR both-by-
    # design: the terminal-failure bucket is its own disjoint set, failed AND
    # terminal — empty-output terminal-row contract, 2026-07-17)
    assert set(FAILURE_OUTCOMES).isdisjoint(TERMINAL_OUTCOMES)
    assert set(TERMINAL_FAILURE_OUTCOMES).isdisjoint(FAILURE_OUTCOMES)
    assert set(TERMINAL_FAILURE_OUTCOMES).isdisjoint(TERMINAL_OUTCOMES)
    assert "withheld_unverified" in TERMINAL_OUTCOMES
    # a kept-but-uncited research answer: terminal, NOT a failure, value kept.
    assert "unverified_memory" in TERMINAL_OUTCOMES
    assert set(FAILURE_OUTCOMES) == {"model_error", "invalid_output", "row_error"}
    assert set(TERMINAL_FAILURE_OUTCOMES) == {"empty_output"}


def test_derive_shim_classifies_value_error_empty() -> None:
    assert _result_outcome({"value": "v"}) == "ok"
    assert _result_outcome({"value": None, "error": "boom"}) == "model_error"
    assert _result_outcome({"value": None}) == "empty"
    # an explicit outcome always wins over the derivation
    assert _result_outcome({"value": None, "outcome": "withheld_unverified"}) == (
        "withheld_unverified"
    )


def test_result_sink_stores_redacted_detail_without_prefix(tmp_path: Path) -> None:
    secret = "checkpoint1b-result-sink-secret"
    project, sheet_id, out, row_ids, op_id = _seed(tmp_path)
    store = RunResultStore(project)
    run_id = store.start_run(
        op_id, sheet_id, "test.outcome", total_rows=6, row_ids=row_ids
    )
    write_claimed_test_results(
        project,
        run_id,
        [
            {
                "row_id": row_ids[0],
                "column_id": out,
                "error": f"provider echoed api_key={secret}",
                "error_code": "model_error",
            }
        ],
    )
    stored = project.db.execute(
        "SELECT error, error_code FROM results WHERE run_id=? AND row_id=?",
        (run_id, row_ids[0]),
    ).fetchone()
    assert secret not in stored["error"]
    assert not stored["error"].startswith("model_error:")
    assert stored["error_code"] == "model_error"


def test_batch_preview_and_persisted_cells_use_safe_detail() -> None:
    secret = "checkpoint1b-batch-cell-secret"
    cells, failed, _cost = normalize_batch_row(
        {"__error__": f"api_key={secret}"}, ["answer"]
    )
    assert failed
    assert secret not in cells["answer"]["error"]
    assert not cells["answer"]["error"].startswith("batch_recipe_error:")
    assert cells["answer"]["error_code"] == "batch_recipe_error"

    metadata_cells, failed, _cost = normalize_batch_row(
        ({"answer": "ok"}, {"error": f"api_key={secret}"}), ["answer"]
    )
    assert not failed
    assert secret not in metadata_cells["answer"]["error"]
    assert metadata_cells["answer"]["value"] == "ok"

    none_error_cells, failed, _cost = normalize_batch_row(
        ({"answer": "ok"}, {"error": None}), ["answer"]
    )
    assert not failed
    assert none_error_cells["answer"]["error"] is None
    assert _result_outcome(none_error_cells["answer"]) == "ok"


def test_batch_missing_siblings_respect_producer_requiredness() -> None:
    cells, failed, _cost = normalize_batch_row(
        (
            {"present": "value"},
            {"cost": 1.25, "model_calls": [{"id": "call-1"}]},
        ),
        ["optional", "present", "required"],
        required_field_names={"present", "required"},
        managed_publication=True,
    )

    assert cells["present"]["publication_effect"] == "publish_value"
    assert cells["present"]["cost"] == 1.25
    assert cells["present"]["model_calls"] == [{"id": "call-1"}]
    assert "optional" not in cells
    assert cells["required"]["publication_effect"] == "publish_error"
    assert cells["required"]["error_code"] == "invalid_output"
    assert failed


def test_missing_outputs_publish_explicit_null_or_error(tmp_path: Path) -> None:
    batch_cells, failed, _cost = normalize_batch_row(
        {"present": "value"},
        ["present", "missing"],
        required_field_names={"present", "missing"},
        managed_publication=True,
    )
    assert failed
    assert batch_cells["missing"]["value"] is None
    assert batch_cells["missing"]["publication_effect"] == "publish_error"

    project = Project.create(tmp_path / "legacy-missing-output.frisket")
    try:
        row_cells = asyncio.run(
            execute_row(
                project,
                ModelRouter(keys={}),
                AdaptiveThrottle(),
                _LegacyOmissionRecipe(),
                {"seed": "value"},
                {"action_kind": "map.extract", "with_error": True},
                OpContext(project=project, http=None, extras={}),
                output_field_names=("present", "missing"),
                required_output_field_names=frozenset({"present", "missing"}),
                managed_publication=True,
            )
        )
        assert row_cells["present"]["value"] is None
        assert row_cells["present"]["publication_effect"] == "publish_error"
        assert row_cells["missing"]["value"] is None
        assert row_cells["missing"]["publication_effect"] == "publish_error"
        assert all("publication_effect" in cell for cell in row_cells.values())
    finally:
        project.close()


def test_result_and_batch_error_boundaries_survive_hostile_stringification(
    tmp_path: Path,
) -> None:
    secret = "checkpoint1b-hostile-str-secret"

    class Evil:
        def __str__(self) -> str:
            raise RuntimeError(secret)

    cells, failed, _cost = normalize_batch_row({"__error__": Evil()}, ["answer"])
    assert failed
    assert cells["answer"]["error"] == "[UNPRINTABLE Evil]"
    assert secret not in cells["answer"]["error"]

    project, sheet_id, out, row_ids, op_id = _seed(tmp_path)
    store = RunResultStore(project)
    run_id = store.start_run(
        op_id, sheet_id, "test.outcome", total_rows=6, row_ids=row_ids
    )
    write_claimed_test_results(
        project,
        run_id,
        [
            {
                "row_id": row_ids[0],
                "column_id": out,
                "error": Evil(),
                "error_code": "model_error",
            }
        ],
    )
    stored = project.db.execute(
        "SELECT error FROM results WHERE run_id=? AND row_id=?",
        (run_id, row_ids[0]),
    ).fetchone()["error"]
    assert stored == "[UNPRINTABLE Evil]"
    assert secret not in stored


def test_failed_rows_counts_only_failures_and_backfill_skips_terminal(
    tmp_path: Path,
) -> None:
    project, sheet_id, out, row_ids, op_id = _seed(tmp_path)
    store = RunResultStore(project)
    run_id = store.start_run(
        op_id, sheet_id, "test.outcome", total_rows=6, row_ids=row_ids
    )

    write_claimed_test_results(
        project,
        run_id,
        [
            {"row_id": row_ids[0], "column_id": out, "value": "v"},  # -> ok (derived)
            {"row_id": row_ids[1], "column_id": out, "error": "boom"},  # -> model_error
            {
                "row_id": row_ids[2],
                "column_id": out,
                "outcome": "invalid_output",
                "error": "bad schema",
            },
            {
                "row_id": row_ids[3],
                "column_id": out,
                "outcome": "withheld_unverified",
                "error": "no citation",
            },
            {
                "row_id": row_ids[4],
                "column_id": out,
                "value": None,
            },  # -> empty (derived)
            {
                # terminal failure (empty-output terminal-row contract): failed
                # honestly AND never revisited by automation.
                "row_id": row_ids[5],
                "column_id": out,
                "outcome": "empty_output",
                "error": "engine returned an empty translation",
                "error_code": "empty_output",
            },
        ],
    )

    outcomes = {
        int(r["row_id"]): r["outcome"]
        for r in project.db.execute(
            "SELECT row_id, outcome FROM results WHERE run_id=?", (run_id,)
        )
    }
    assert outcomes == {
        row_ids[0]: "ok",
        row_ids[1]: "model_error",
        row_ids[2]: "invalid_output",
        row_ids[3]: "withheld_unverified",
        row_ids[4]: "empty",
        row_ids[5]: "empty_output",
    }

    # failed_rows counts model_error + invalid_output + empty_output (the
    # terminal failure is failed HONESTLY); withheld/empty/ok stay excluded.
    run = project.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    assert int(run["failed_rows"]) == 3
    assert store.result_row_failure_states(run_id, set(row_ids)) == {
        row_ids[0]: False,
        row_ids[1]: True,
        row_ids[2]: True,
        row_ids[3]: False,  # withheld is NOT a failure
        row_ids[4]: False,
        row_ids[5]: True,  # terminal failure IS a failure
    }

    # The fixture publishes a sealed generation. No outcome is resumed in
    # place; a retry is a separately scoped successor generation.
    assert store.pending_run_row_scope_count(run_id, [out]) == 0
    pending = store.pending_run_row_scope_page_after(
        run_id, [out], after_position=-1, limit=10
    )
    pending_row_ids = {rid for _pos, rid in pending}
    assert pending_row_ids == set()
    assert row_ids[3] not in pending_row_ids  # withheld is not re-run
    assert row_ids[5] not in pending_row_ids  # empty_output is not auto-re-run
    assert store.completed_result_row_ids(run_id, [out]) == set(row_ids)


def test_withhold_result_value_marks_terminal_without_failing(tmp_path: Path) -> None:
    project, sheet_id, out, row_ids, op_id = _seed(tmp_path)
    store = RunResultStore(project)
    run_id = store.start_run(
        op_id, sheet_id, "test.outcome", total_rows=6, row_ids=row_ids
    )
    # a value is written first (ok), then withheld on policy.
    write_claimed_test_results(
        project,
        run_id,
        [{"row_id": row_ids[0], "column_id": out, "value": "v"}],
    )
    assert (
        int(
            project.db.execute(
                "SELECT failed_rows FROM runs WHERE id=?", (run_id,)
            ).fetchone()["failed_rows"]
        )
        == 0
    )
    with pytest.raises(
        sqlite3.IntegrityError, match="published result semantics are immutable"
    ):
        store.withhold_result_value(run_id, row_ids[0], out, "no citation")
    row = project.db.execute(
        "SELECT value, error, outcome FROM results WHERE run_id=? AND row_id=?",
        (run_id, row_ids[0]),
    ).fetchone()
    assert json.loads(row["value"]) == "v"
    assert row["error"] is None
    assert row["outcome"] == "ok"
    assert store.pending_run_row_scope_count(run_id, [out]) == 5
    assert row_ids[0] in store.completed_result_row_ids(run_id, [out])
