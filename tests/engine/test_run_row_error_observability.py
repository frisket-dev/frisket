"""Row-error observability for queued runs.

A queued ``media.transcribe`` run can fail every row when an undo empties the input
media column. The receipt said only "media.transcribe failed for every
target row"; `run_rows` carries no error column; the actual per-row
exception text existed nowhere a human (or agent) could read it. The human
burned 20 minutes where 5 seconds should do.

This file pins three things:

1. PERSISTENCE: MapRunner's existing per-row failure path (map_runner.py's
   `_execute_row`/`_normalize_batch_row`) now writes `results.error_code`
   alongside the pre-existing `results.error` (RunResultStore.write_results,
   store/runs.py) — reusing `results` as the row-level error store rather
   than adding new `run_rows` columns (run_rows is a thin (run_id, row_id,
   position) join table; `results` already carries per-cell error text at
   row-failure time, it just was never summarized or surfaced anywhere).

2. SURFACING: `RunResultStore.row_error_summary` groups those per-row
   errors by exact message (count + example row ids), and that summary now
   rides on THREE wire surfaces: `actions.run_status`'s "run" dict (the
   run-detail region), `project_run_status_payload`'s "public_status" dict
   (what `getRunProgress` maps into `RunProgress.rowErrors` for the
   Jobs/Errors bottom-dock), and `history_page_payload`'s per-op "run" dict
   (the History panel's run-detail deep link). All three are pinned here at
   the API-shape level.

3. FAIL-FAST PRECHECK: `MapRunner._validate_spec` (the one seam every
   recipe kind passes through, direct or queued) now raises
   `EmptyInputColumns` when a fresh (non-resume) launch's resolved source
   column(s) are empty across EVERY target row, refusing at request time
   with zero `runs` rows created — instead of queuing N rows that would
   each fail identically. Partial emptiness (some rows have data) does NOT
   block.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import ChaosConfig, ModelRouter, ResponseCache
from frisket.ops.base import Recipe
from frisket.engine.runner import CostGate, MapRunner
from frisket.server.app import create_app
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from typed_model_fixtures import prepare_model_run
from typed_model_fixtures import run_with_output_claim
from helpers import write_claimed_test_results

MODEL = "anthropic/claude-haiku-4-5"


# --------------------------------------------------------------------------
# Part 1 + 2: persistence + the grouped summary shape (direct MapRunner —
# deterministic chaos-forced failures, no network).
# --------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path):
    p = Project.create(tmp_path / "p.frisket")
    yield p
    p.close()


def _classify_spec(sheet_id: int) -> dict[str, Any]:
    return {
        "action_kind": "map.classify",
        "model": MODEL,
        "sheet_id": sheet_id,
        "input_columns": ["text"],
        "context": "Test rows.",
        "fields": [
            {"name": "relevance", "type": "score", "description": "0-10 relevance"}
        ],
    }


def _seed_sheet(p: Project, texts: list[str]) -> tuple[int, dict[str, int]]:
    sheet = p.add_sheet("data")
    cols = {"text": p.add_column(sheet, "text")}
    p.add_rows(sheet, [{"text": t} for t in texts], cols)
    return sheet, cols


def _confirmed_remote_spec(runner: MapRunner, spec: dict[str, Any]) -> dict[str, Any]:
    """Quote once, then echo the exact scope-and-cost token under test."""
    with pytest.raises(CostGate) as quote:
        prepare_model_run(runner, spec)
    assert quote.value.promise_set_hash
    return {
        **spec,
        "consented_promise_set_hash": quote.value.promise_set_hash,
    }


def _run_all_failing(project: Project, tmp_path: Path, row_count: int) -> int:
    """An N-row classify run where EVERY row fails identically (chaos
    fail_rate=1.0, zero cache hits) — the deterministic stand-in for the live
    incident's "every row fails the same way" shape."""
    sheet, _ = _seed_sheet(project, [f"row {i}" for i in range(row_count)])
    spec = _classify_spec(sheet)
    cache = ResponseCache(tmp_path / "c.db")
    router = ModelRouter(
        keys={"anthropic": "k"},
        cache=cache,
        cache_mode="replay",
        chaos=ChaosConfig(seed=3, enabled=True, fail_rate=1.0),
        max_retries=0,
    )
    runner = MapRunner(project, router, authority=UnroutedOnlyAuthority(project))
    progress = asyncio.run(
        run_with_output_claim(
            runner,
            _confirmed_remote_spec(runner, spec),
            confirmed=True,
        )
    )
    assert progress.failed == row_count
    return progress.run_id


class TestPerRowErrorPersistence:
    def test_failure_persists_error_text_and_code(
        self, project: Project, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
        run_id = _run_all_failing(project, tmp_path, 6)
        rows = project.db.execute(
            "SELECT error, error_code FROM results WHERE run_id=?", (run_id,)
        ).fetchall()
        assert len(rows) == 6
        for row in rows:
            # the raw exception text is persisted (not a generic aggregate)
            assert row["error"]
            # and a classifier code rides alongside it (RemediatedError.code)
            assert row["error_code"]


class TestNonLlmExecuteFailurePersistence:
    """DIAGNOSIS CORRECTION follow-up (coordinator, live-verified): `results`
    already carries per-row error text for LLM (chaos-forced) failures —
    verify a non-LLM `execute()`-raising recipe (the ACTUAL shape of the
    incident: ops/transcribe.py:555's `raise ValueError("no media value in
    input columns")`, reached via MapRunner._execute_row's non-agent
    non-llm branch, not the LLM branch the chaos tests above exercise)
    funnels through the SAME catch-and-persist path, not a bypass."""

    def test_execute_style_recipe_failure_persists_like_transcribe(
        self, project: Project, monkeypatch: Any
    ) -> None:
        class _FakeEmptyInputRecipe(Recipe):
            consumes_resolution = False  # required declaration (Recipe)
            cost_class = "free"  # required declaration (Recipe)

            async def execute(self, values: dict, spec: dict, ctx: Any) -> dict:
                # ops/transcribe.py:555's exact shape: a non-LLM execute()
                # raising ValueError for a missing/empty source value.
                raise ValueError("no media value in input columns")

        import frisket.engine.runner.validation as validation_module

        fake_recipe = _FakeEmptyInputRecipe(name="fake_empty_input", llm=False)
        fake_recipe.source_columns = lambda spec: ["text"]  # type: ignore[method-assign]
        fake_recipe.output_fields = lambda spec: [  # type: ignore[method-assign]
            {"name": "out", "column_type": "text"}
        ]
        monkeypatch.setattr(validation_module, "get_recipe", lambda name: fake_recipe)
        # a non-empty source value: this test is about the execute()-raises
        # path, not the (separately tested) all-empty precheck.
        sheet, _ = _seed_sheet(project, ["some text"])
        spec = {
            "action_kind": "test.fake_empty_input",
            "sheet_id": sheet,
            "input_columns": ["text"],
        }
        router = ModelRouter(keys={}, cache=None, cache_mode="off")
        runner = MapRunner(project, router, authority=UnroutedOnlyAuthority(project))
        progress = asyncio.run(run_with_output_claim(runner, spec))
        assert progress.failed == 1
        row = project.db.execute(
            "SELECT error, error_code FROM results WHERE run_id=?",
            (progress.run_id,),
        ).fetchone()
        assert row["error"] == "no media value in input columns"
        assert row["error_code"]


class TestRowErrorSummaryShape:
    def test_groups_by_message_with_counts_and_row_id_examples(
        self, project: Project, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
        run_id = _run_all_failing(project, tmp_path, 8)
        summary = RunResultStore(project).row_error_summary(run_id)
        assert summary is not None
        assert summary["total_failed_rows"] == 8
        assert len(summary["groups"]) >= 1
        group = summary["groups"][0]
        assert set(group) == {
            "message",
            "count",
            "code",
            "outcome",
            "terminal",
            "row_ids",
        }
        assert isinstance(group["message"], str) and group["message"]
        # chaos-forced provider failures classify as retryable model_error —
        # the taxonomy bucket rides the group so triage never parses messages.
        assert group["outcome"] == "model_error"
        assert group["terminal"] is False
        # every one of the 8 rows shares the SAME chaos-injected failure, so
        # they collapse into one group with the full count...
        assert group["count"] == 8
        # ...but row_ids stays capped to the example limit, not all 8.
        assert 0 < len(group["row_ids"]) <= 5
        assert len(set(group["row_ids"])) == len(group["row_ids"])

    def test_empty_output_group_is_marked_terminal(
        self, project: Project, tmp_path: Path
    ) -> None:
        sheet, cols = _seed_sheet(project, ["a", "b"])
        row_ids = [
            int(r["id"])
            for r in project.db.execute(
                "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (sheet,)
            )
        ]
        ai_col = project.add_column(sheet, "result", type="text", ai_generated=True)
        op = project.append_op("map", {"action_kind": "map.classify"})
        store = RunResultStore(project)
        run_id = store.start_run(op, sheet, "map.classify", total_rows=2)
        write_claimed_test_results(
            project,
            run_id,
            [
                {
                    "row_id": row_ids[0],
                    "column_id": ai_col,
                    "value": None,
                    "error": "model returned no output for a non-empty source",
                    "error_code": "empty_output",
                    "outcome": "empty_output",
                },
                {
                    "row_id": row_ids[1],
                    "column_id": ai_col,
                    "value": None,
                    "error": "provider rate limited",
                    "error_code": "provider_rate_limited",
                    "outcome": "model_error",
                },
            ],
        )
        store.finish_run(run_id)
        summary = store.row_error_summary(run_id)
        assert summary is not None
        by_outcome = {g["outcome"]: g for g in summary["groups"]}
        assert by_outcome["empty_output"]["terminal"] is True
        assert by_outcome["model_error"]["terminal"] is False

    def test_no_failed_rows_returns_none(
        self, project: Project, tmp_path: Path
    ) -> None:
        sheet, _ = _seed_sheet(project, ["a", "b"])
        spec = _classify_spec(sheet)
        cache = ResponseCache(tmp_path / "c.db")
        from frisket.ai.llm import LLMRequest, LLMResponse, request_key
        from typed_model_fixtures import model_plan

        classify = model_plan(spec).program
        for text in ["a", "b"]:
            call = classify.render({"text": text}, spec)
            req = LLMRequest(
                model=MODEL,
                messages=call.messages,
                schema=call.schema,
                max_tokens=call.max_tokens,
            )
            cache.put(
                request_key(req, classify.version),
                LLMResponse(
                    content=None,
                    data={"relevance": 5},
                    tokens_in=10,
                    tokens_out=5,
                    cost=0.0001,
                    model=MODEL,
                ),
            )
        router = ModelRouter(
            keys={"anthropic": "k"}, cache=cache, cache_mode="replay_strict"
        )
        runner = MapRunner(project, router, authority=UnroutedOnlyAuthority(project))
        progress = asyncio.run(run_with_output_claim(runner, spec))
        assert progress.failed == 0
        assert RunResultStore(project).row_error_summary(progress.run_id) is None

    def test_scope_excludes_carry_forward_errors_but_legacy_runs_keep_them(
        self, project: Project, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
        run_id = _run_all_failing(project, tmp_path, 2)
        result = project.db.execute(
            "SELECT c.sheet_id,res.column_id FROM results res "
            "JOIN columns c ON c.id=res.column_id WHERE res.run_id=? LIMIT 1",
            (run_id,),
        ).fetchone()
        assert result is not None
        text_column = project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='text'",
            (result["sheet_id"],),
        ).fetchone()
        assert text_column is not None
        outside_row_id = project.add_rows(
            result["sheet_id"],
            [{"text": "added after the run"}],
            {"text": text_column["id"]},
        )[0]
        store = RunResultStore(project)
        assert store.row_error_summary(run_id)["total_failed_rows"] == 2
        assert (
            store.batch_row_error_summaries([run_id])[run_id]["total_failed_rows"] == 2
        )

        # Runs created before immutable row scopes existed have no generation
        # or scope marker, so their historical behavior remains inclusive.
        legacy_op_id = project.append_op("map")
        legacy_run_id = store.start_run(
            legacy_op_id,
            int(result["sheet_id"]),
            "test.legacy_error_probe",
            total_rows=3,
        )
        scoped_rows = [
            int(row["row_id"])
            for row in project.db.execute(
                "SELECT row_id FROM run_rows WHERE run_id=? ORDER BY position",
                (run_id,),
            ).fetchall()
        ]
        project.db.executemany(
            "INSERT INTO results "
            "(run_id,row_id,column_id,value,error,error_code,outcome) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                (
                    legacy_run_id,
                    row_id,
                    result["column_id"],
                    None,
                    "legacy display-state error",
                    "synthetic_error",
                    "model_error",
                )
                for row_id in [*scoped_rows, outside_row_id]
            ],
        )
        project.db.commit()
        assert store.row_error_summary(legacy_run_id)["total_failed_rows"] == 3
        assert (
            store.batch_row_error_summaries([legacy_run_id])[legacy_run_id][
                "total_failed_rows"
            ]
            == 3
        )


# --------------------------------------------------------------------------
# Part 2 (continued): the three wire surfaces — run_status endpoint (feeds
# both actions.run_status's "run" dict and project_run_status_payload's
# "public_status", the latter is what getRunProgress maps into
# RunProgress.rowErrors for the Jobs/Errors bottom-dock), and the history
# endpoint (the History panel's run-detail deep link).
# --------------------------------------------------------------------------


def _http_client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))


class TestSurfacedViaStatusAndHistoryEndpoints:
    def test_status_endpoint_carries_row_errors_on_both_run_dicts(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
        client = _http_client(tmp_path)
        project_id = client.post("/api/projects", json={"name": "Row errors"}).json()[
            "id"
        ]
        project = client.app.state.workspace.get(project_id)
        run_id = _run_all_failing(project, tmp_path, 5)

        status = client.get(f"/api/projects/{project_id}/actions/runs/{run_id}/status")
        assert status.status_code == 200, status.text
        body = status.json()

        # Surface A: actions.run_status's "run" dict (run-detail region).
        run_row_errors = body["run"]["row_errors"]
        assert run_row_errors["total_failed_rows"] == 5
        assert run_row_errors["groups"][0]["count"] == 5
        assert len(run_row_errors["groups"][0]["row_ids"]) <= 5

        # Surface B: project_run_status_payload's "public_status" dict — the
        # SAME shape getRunProgress (web/src/api/real.ts) maps into
        # RunProgress.rowErrors, which both the Jobs tab and the Errors tab
        # (WorkbenchBottomDock.tsx's shared renderJobDetail) render.
        public_row_errors = body["run"]["public_status"]["row_errors"]
        assert public_row_errors == run_row_errors

    def test_history_endpoint_carries_row_errors_for_the_op(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
        client = _http_client(tmp_path)
        project_id = client.post(
            "/api/projects", json={"name": "Row errors history"}
        ).json()["id"]
        project = client.app.state.workspace.get(project_id)
        run_id = _run_all_failing(project, tmp_path, 4)

        history = client.get(f"/api/projects/{project_id}/history")
        assert history.status_code == 200, history.text
        ops = history.json()["ops"]
        op = next(
            o for o in ops if o["run"] is not None and o["run"]["run_id"] == run_id
        )

        # Surface C: history_page_payload's per-op "run" dict — the History
        # panel's run-detail deep link (HistoryPanel.tsx's renderDetail).
        row_errors = op["run"]["row_errors"]
        assert row_errors["total_failed_rows"] == 4
        assert row_errors["groups"][0]["count"] == 4


# --------------------------------------------------------------------------
# Part 3: the fail-fast empty-input precheck.
# --------------------------------------------------------------------------


def _empty_input_probe_action(*, sheet_id: int, idempotency_key: str) -> dict[str, Any]:
    return {
        "action_id": "map.python",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "input_columns": ["note"],
            "code": "result = {'phone': row['note']}",
            "return_schema": {
                "type": "object",
                "properties": {"phone": {"type": "string"}},
                "required": ["phone"],
            },
            "output_routes": [
                {
                    "name": "phone",
                    "path": "$.phone",
                    "target": {"kind": "column", "type": "text"},
                }
            ],
        },
        "idempotency_key": idempotency_key,
    }


class TestEmptyInputPrecheck:
    def test_all_empty_input_column_refuses_with_zero_runs_created(
        self, tmp_path: Path
    ) -> None:
        client = _http_client(tmp_path)
        project_id = client.post("/api/projects", json={"name": "All empty"}).json()[
            "id"
        ]
        project = client.app.state.workspace.get(project_id)
        sheet = project.add_sheet("data")
        col = project.add_column(sheet, "note")
        # every row's "note" cell is empty — the undo-emptied-column shape.
        project.add_rows(sheet, [{}, {}, {}], {"note": col})

        before = project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        response = client.post(
            f"/api/projects/{project_id}/actions/v1/run",
            json=_empty_input_probe_action(
                sheet_id=sheet, idempotency_key="empty-precheck@sha256:all"
            ),
        )
        assert response.status_code == 400, response.text
        result = response.json()
        assert result["status"] == "failed"
        assert result["errors"], result
        error = result["errors"][0]
        assert error["code"] == "empty_input_column"
        lowered = error["message"].lower()
        assert "note" in lowered
        assert "undone" in lowered or "populated" in lowered

        after = project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        assert after == before, "the precheck must create ZERO run rows"

    def test_partial_empty_input_column_proceeds(self, tmp_path: Path) -> None:
        client = _http_client(tmp_path)
        project_id = client.post(
            "/api/projects", json={"name": "Partial empty"}
        ).json()["id"]
        project = client.app.state.workspace.get(project_id)
        sheet = project.add_sheet("data")
        col = project.add_column(sheet, "note")
        # only ONE row has data — partial emptiness must NOT block.
        project.add_rows(
            sheet,
            [{"note": "Call 212-555-0123"}, {}, {}],
            {"note": col},
        )

        before = project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        response = client.post(
            f"/api/projects/{project_id}/actions/v1/run",
            json=_empty_input_probe_action(
                sheet_id=sheet, idempotency_key="empty-precheck@sha256:partial"
            ),
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] != "failed", result
        assert result.get("errors") in (None, [])

        after = project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        assert after == before + 1, "a partial-empty launch must still create a run"
