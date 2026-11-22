"""End-to-end tests for Recommendation 2: Run on View & Backfill new rows in View."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.types import ActionRequest, SheetRows
from frisket.contracts.action import SheetRowScope, ViewSelector
from frisket.engine.executor.actions import resolve_map_preview, run_action_spec
from frisket.engine.jobs.search_index import process_index_batches
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore
from frisket.retrieval.encoding import TextEncodingInput
from frisket.server.app import create_app
from http_test_helpers import (
    confirmation_hash_from_action_result,
    drain_queue,
    post_row_add_as_v1_action,
    post_v1_action_with_exact_confirmation,
)
from model_doubles import canned_reply_router


def _unit_axis(dimensions: int, weights: dict[int, float]) -> tuple[float, ...]:
    vec = [0.0] * dimensions
    for idx, value in weights.items():
        vec[idx] = value
    norm = math.sqrt(sum(v * v for v in vec))
    return tuple(v / norm for v in vec)


class _LocalRetrievalTestEncoder:
    def __init__(self, *, is_available: bool = True) -> None:
        self._available = is_available
        self.calls = 0

    def available(self) -> bool:
        return self._available

    def encode(self, inputs, *, purpose, space, cancelled=None):
        del purpose
        assert cancelled is None or not cancelled()
        self.calls += 1
        out: list[tuple[float, ...]] = []
        for item in inputs:
            assert isinstance(item, TextEncodingInput)
            text = item.text.lower()
            if any(word in text for word in ("contract", "invoice", "procurement")):
                out.append(_unit_axis(space.dimensions, {0: 1.0}))
            else:
                out.append(_unit_axis(space.dimensions, {1: 1.0}))
        return out


def _client(tmp_path: Path) -> TestClient:
    router, _ = canned_reply_router(
        {"beat": "urgent_beat"}, tokens_in=25, tokens_out=10, cost=0.0
    )
    return TestClient(create_app(tmp_path / "workspace", router=router))


def _seed_priority_sheet(
    client: TestClient,
    *,
    rows: list[tuple[str, str]] | None = None,
) -> tuple[str, int, dict[str, int], list[int]]:
    pid = client.post("/api/projects", json={"name": "Run on View"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("Filings")
    cols = {
        "story": project.add_column(sheet_id, "story", type="text"),
        "priority": project.add_column(sheet_id, "priority", type="text"),
    }
    seed_rows = rows or [
        ("contract alpha 101", "high"),
        ("contract beta 202", "high"),
        ("routine memo 303", "low"),
        ("routine memo 404", "low"),
    ]
    row_ids = project.add_rows(
        sheet_id,
        [{"story": story, "priority": prio} for story, prio in seed_rows],
        cols,
    )
    return pid, sheet_id, cols, row_ids


def _save_priority_view(
    project: Any,
    *,
    sheet_id: int,
    priority_col_id: int,
    priority_value: str = "high",
    name: str = "High Priority",
) -> int:
    return project.add_view(
        name,
        {
            "selection": {
                "scope": {"kind": "sheet", "sheet_id": sheet_id},
                "filters": [
                    {
                        "column_id": priority_col_id,
                        "condition": {"eq": priority_value},
                    }
                ],
                "order": [],
                "limit": None,
            },
            "presentation": {},
        },
        sheet_id=sheet_id,
    )


def _classify_view_action(
    sheet_id: int,
    *,
    view_id: int,
    model: str = "anthropic/claude-haiku-4-5",
    key: str = "classify-view@v1",
) -> dict[str, Any]:
    return {
        "action_id": "map.classify",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "view_id": view_id},
        "params": {
            "engine": "llm",
            "model": model,
            "source": ["story"],
            "fields": [
                {
                    "name": "beat",
                    "type": "category",
                    "labels": ["urgent_beat", "other"],
                }
            ],
        },
        "idempotency_key": key,
    }


def _regex_view_action(
    sheet_id: int,
    *,
    view_id: int,
    key: str = "regex-view@v1",
) -> dict[str, Any]:
    return {
        "action_id": "map.regex_extract",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "view_id": view_id},
        "params": {"input_columns": ["story"], "pattern": r"\d+"},
        "output_names": {"extracted": "code_num"},
        "idempotency_key": key,
    }


def _populated_values(project: Any, sheet_id: int, column_id: int) -> dict[int, Any]:
    return {
        row_id: value
        for row_id, value in project.get_values(sheet_id, column_id).items()
        if value is not None
    }


def _drain_local_retrieval(project: Any, encoder: Any) -> None:
    for _ in range(32):
        _processed, complete = process_index_batches(project, embedding_encoder=encoder)
        if complete:
            return
    raise AssertionError("local retrieval preparation did not converge")


def _column_id(project: Any, sheet_id: int, name: str) -> int:
    return int(
        next(
            col
            for col in project.columns(sheet_id, include_hidden=True)
            if col["name"] == name
        )["id"]
    )


def test_sheet_row_scope_and_sheet_rows_view_id_contract() -> None:
    scope = SheetRowScope.model_validate(
        {"sheet_id": 1, "selector": {"kind": "view", "view_id": 42}}
    )
    assert isinstance(scope.selector, ViewSelector)
    assert scope.selector.kind == "view"
    assert scope.selector.view_id == 42

    for bad_view_id in (0, -3, True, "42"):
        with pytest.raises(ValidationError):
            SheetRowScope.model_validate(
                {"sheet_id": 1, "selector": {"kind": "view", "view_id": bad_view_id}}
            )
        with pytest.raises(ValidationError):
            SheetRows.model_validate({"sheet_id": 1, "view_id": bad_view_id})

    valid = SheetRows(sheet_id=1, view_id=42)
    assert valid.view_id == 42
    assert valid.row_ids is None
    assert valid.selection is None

    with pytest.raises(ValidationError, match="not both"):
        SheetRows(sheet_id=1, row_ids=(1,), view_id=42)
    with pytest.raises(ValidationError, match="not both"):
        SheetRows(
            sheet_id=1,
            view_id=42,
            selection={"scope": {"kind": "sheet", "sheet_id": 1}},
        )

    # RowScope.ALL_ROWS actions must reject view_id subsets.
    cluster_action = ACTION_REGISTRY.get("cluster.values")
    with pytest.raises(ValueError, match="all rows"):
        cluster_action.bind_request(
            ActionRequest(
                action_id="cluster.values",
                scope={"kind": "sheet_rows", "sheet_id": 1, "view_id": 42},
                params={"column": "story", "method": "fingerprint"},
                idempotency_key="cluster-view-forbidden",
            )
        )


def test_sync_and_queued_run_on_view_keeps_canonical_sheet_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path)
    try:
        pid, sheet_id, cols, row_ids = _seed_priority_sheet(client)
        project = client.app.state.workspace.get(pid)
        view_id = _save_priority_view(
            project, sheet_id=sheet_id, priority_col_id=cols["priority"]
        )

        # 1. Sync execution via run_action_spec on saved View.
        sync_body = _regex_view_action(sheet_id, view_id=view_id, key="sync-view@v1")
        sync_res = run_action_spec(project, sync_body, project_id=pid)
        assert sync_res.status == "completed", sync_res.errors
        assert sync_res.run_id is not None

        code_col = _column_id(project, sheet_id, "code_num")
        assert _populated_values(project, sheet_id, code_col) == {
            row_ids[0]: "101",
            row_ids[1]: "202",
        }
        assert project.get_values(sheet_id, code_col)[row_ids[2]] is None
        assert project.get_values(sheet_id, code_col)[row_ids[3]] is None
        assert RunResultStore(project).run_row_scope(sync_res.run_id) == row_ids[:2]
        run_params = json.loads(
            project.db.execute(
                "SELECT params FROM runs WHERE id=?", (sync_res.run_id,)
            ).fetchone()["params"]
        )
        assert run_params["row_scope"] == {"kind": "view", "view_id": view_id}
        assert run_params["row_ids"] == row_ids[:2]

        # Canonical sheet Review works directly on the view-scoped result row.
        review = run_action_spec(
            project,
            {
                "action_id": "review.decision",
                "scope": {"kind": "project"},
                "params": {
                    "run_id": sync_res.run_id,
                    "row_id": row_ids[0],
                    "column_id": code_col,
                    "decision": "accept",
                },
                "idempotency_key": "review-view-row-0",
            },
            project_id=pid,
        )
        assert review.status == "completed", review.errors

        # Replay with the same idempotency_key never re-evaluates the View.
        project.add_rows(
            sheet_id, [{"story": "contract 999", "priority": "high"}], cols
        )

        def _forbid_open_selection(*_args, **_kwargs):
            raise AssertionError("Replay must not re-evaluate saved View")

        monkeypatch.setattr(
            "frisket.engine.views.evaluate.open_selection", _forbid_open_selection
        )
        replay = run_action_spec(project, sync_body, project_id=pid)
        assert replay.receipt_id == sync_res.receipt_id
        monkeypatch.undo()

        # 2. Queued execution via HTTP ActionRunService on saved View.
        queued_body = _classify_view_action(
            sheet_id, view_id=view_id, key="queued-view@v1"
        )
        est = client.post(
            f"/api/projects/{pid}/actions/v1/estimate",
            json={"action": queued_body},
        )
        assert est.status_code == 200, est.text
        assert est.json()["estimate"]["rows"] == 3  # 2 original + row 999

        queued = post_v1_action_with_exact_confirmation(client, pid, queued_body)
        assert queued.status_code == 200, queued.text
        assert queued.json()["status"] == "queued"
        queued_run_id = int(queued.json()["run_id"])
        drain_queue(client)

        beat_col = _column_id(project, sheet_id, "beat")
        beat_values = _populated_values(project, sheet_id, beat_col)
        assert row_ids[0] in beat_values and row_ids[1] in beat_values
        assert row_ids[2] not in beat_values and row_ids[3] not in beat_values
        assert len(beat_values) == 3
        queued_params = json.loads(
            project.db.execute(
                "SELECT params FROM runs WHERE id=?", (queued_run_id,)
            ).fetchone()["params"]
        )
        assert queued_params["row_scope"] == {"kind": "view", "view_id": view_id}
    finally:
        client.close()


def test_run_on_local_retrieval_multimodal_view(tmp_path: Path) -> None:
    client = _client(tmp_path)
    try:
        pid, sheet_id, cols, row_ids = _seed_priority_sheet(
            client,
            rows=[
                ("procurement contract 111", "high"),
                ("invoice audit 222", "low"),
                ("forest wildlife 333", "low"),
            ],
        )
        project = client.app.state.workspace.get(pid)
        encoder = _LocalRetrievalTestEncoder(is_available=True)
        project._frisket_embedding_runtime = encoder
        _drain_local_retrieval(project, encoder)

        view_id = project.add_view(
            "Procurement Semantic View",
            {
                "selection": {
                    "scope": {"kind": "sheet", "sheet_id": sheet_id},
                    "filters": [],
                    "similarity": {
                        "kind": "local_retrieval",
                        "scope": {"kind": "sheet", "sheet_id": sheet_id},
                        "query_text": "procurement contract",
                        "min_score": 0.75,
                    },
                    "order": [{"score": "similarity", "dir": "desc"}],
                    "limit": None,
                },
                "presentation": {},
            },
            sheet_id=sheet_id,
        )

        body = _regex_view_action(sheet_id, view_id=view_id, key="local-retrieval-run")
        est = client.post(
            f"/api/projects/{pid}/actions/v1/estimate",
            json={"action": body},
        )
        assert est.status_code == 200, est.text
        assert est.json()["estimate"]["rows"] == 2

        res = client.post(f"/api/projects/{pid}/actions/v1/run", json=body)
        assert res.status_code == 200, res.text
        drain_queue(client)

        code_col = _column_id(project, sheet_id, "code_num")
        assert _populated_values(project, sheet_id, code_col) == {
            row_ids[0]: "111",
            row_ids[1]: "222",
        }
        assert project.get_values(sheet_id, code_col)[row_ids[2]] is None

        # Unavailable local model must fail closed with typed 400 error.
        project._frisket_embedding_runtime = _LocalRetrievalTestEncoder(
            is_available=False
        )
        unready_view_id = project.add_view(
            "Unencoded Query View",
            {
                "selection": {
                    "scope": {"kind": "sheet", "sheet_id": sheet_id},
                    "filters": [],
                    "similarity": {
                        "kind": "local_retrieval",
                        "scope": {"kind": "sheet", "sheet_id": sheet_id},
                        "query_text": "brand new uncached query",
                        "min_score": 0.75,
                    },
                    "order": [{"score": "similarity", "dir": "desc"}],
                    "limit": None,
                },
                "presentation": {},
            },
            sheet_id=sheet_id,
        )
        unready_body = _regex_view_action(
            sheet_id, view_id=unready_view_id, key="unready-local-retrieval"
        )
        unready_est = client.post(
            f"/api/projects/{pid}/actions/v1/estimate",
            json={"action": unready_body},
        )
        assert unready_est.status_code == 400
        assert unready_est.json()["code"] == "local_embedding_model_unavailable"
    finally:
        client.close()


def test_pending_filter_cell_guard_blocks_estimate_run_and_backfill(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    try:
        pid, sheet_id, cols, row_ids = _seed_priority_sheet(client)
        project = client.app.state.workspace.get(pid)

        # Create a generated column on rows 0..1 only, then run a downstream action on View V.
        triage_res = run_action_spec(
            project,
            {
                "action_id": "map.template",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet_id,
                    "row_ids": row_ids[:2],
                },
                "params": {"template": {"text": "{{priority}}"}},
                "output_names": {"rendered": "triage"},
                "idempotency_key": "seed-triage",
            },
            project_id=pid,
        )
        assert triage_res.status == "completed", triage_res.errors
        triage_col = _column_id(project, sheet_id, "triage")
        view_id = _save_priority_view(
            project,
            sheet_id=sheet_id,
            priority_col_id=triage_col,
            priority_value="high",
            name="Triage High",
        )

        # First run cleanly on View V so we can also test backfill under pending cells.
        first_run = post_v1_action_with_exact_confirmation(
            client,
            pid,
            _regex_view_action(sheet_id, view_id=view_id, key="triage-regex@v1"),
        )
        assert first_run.status_code == 200, first_run.text
        drain_queue(client)

        # Queue an in-flight run populating the missing row on the filter column
        # without draining it -> row_ids[2] now has outcome='pending' in current_cell_values.
        pending_triage = client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json={
                "action_id": "map.regex_extract",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet_id,
                    "row_ids": [row_ids[2]],
                },
                "params": {"input_columns": ["priority"], "pattern": r"[a-z]+"},
                "output_names": {"extracted": "triage"},
                "replace_existing": True,
                "idempotency_key": "pending-triage-inflight",
            },
        )
        assert pending_triage.status_code == 200, pending_triage.text
        assert pending_triage.json()["status"] == "queued"

        second_body = _classify_view_action(
            sheet_id, view_id=view_id, key="pending-guard-classify"
        )
        est = client.post(
            f"/api/projects/{pid}/actions/v1/estimate",
            json={"action": second_body},
        )
        assert est.status_code == 400, est.text
        assert est.json()["code"] == "selection_incomplete"

        run_resp = client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json=second_body,
        )
        assert run_resp.status_code == 400, run_resp.text
        assert run_resp.json()["errors"][0]["code"] == "selection_incomplete"

        sync_resp = run_action_spec(project, second_body, project_id=pid)
        assert sync_resp.status == "failed"
        assert sync_resp.errors[0].code == "selection_incomplete"

        backfill_body = {
            "action_id": "run.backfill",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {"column": "code_num"},
            "output_names": {},
            "idempotency_key": "pending-guard-backfill",
        }
        bf_est = client.post(
            f"/api/projects/{pid}/actions/v1/estimate",
            json={"action": backfill_body},
        )
        assert bf_est.status_code == 400, bf_est.text
        assert bf_est.json()["code"] == "selection_incomplete"

        bf_run = client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json=backfill_body,
        )
        assert bf_run.status_code == 400, bf_run.text
        assert bf_run.json()["errors"][0]["code"] == "selection_incomplete"
    finally:
        client.close()


def test_cross_sheet_project_scope_and_missing_view_rejected_with_400(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    try:
        pid, sheet_a, _cols, _row_ids = _seed_priority_sheet(client)
        project = client.app.state.workspace.get(pid)
        sheet_b = project.add_sheet("Other Sheet")
        other_col = project.add_column(sheet_b, "note", type="text")
        project.add_rows(sheet_b, [{"note": "x"}], {"note": other_col})

        other_sheet_view = project.add_view(
            "Other Sheet View",
            {
                "selection": {
                    "scope": {"kind": "sheet", "sheet_id": sheet_b},
                    "filters": [],
                    "order": [],
                    "limit": None,
                },
                "presentation": {},
            },
            sheet_id=sheet_b,
        )
        project_view = project.add_view(
            "Project Wide View",
            {
                "selection": {
                    "scope": {"kind": "project"},
                    "filters": [],
                    "order": [],
                    "limit": None,
                },
                "presentation": {},
            },
            sheet_id=None,
        )

        for bad_view_id, expected_code in (
            (other_sheet_view, "view_sheet_mismatch"),
            (project_view, "view_sheet_mismatch"),
            (999_999, "view_not_found"),
        ):
            body = _regex_view_action(
                sheet_a, view_id=bad_view_id, key=f"bad-view-{bad_view_id}"
            )
            est = client.post(
                f"/api/projects/{pid}/actions/v1/estimate",
                json={"action": body},
            )
            assert est.status_code == 400, est.text
            assert est.json()["code"] == expected_code

            run_resp = client.post(
                f"/api/projects/{pid}/actions/v1/run",
                json=body,
            )
            assert run_resp.status_code == 400, run_resp.text
            assert run_resp.json()["errors"][0]["code"] == expected_code
    finally:
        client.close()


def test_zero_matching_rows_view_estimate_run_and_later_backfill(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    try:
        pid, sheet_id, cols, _row_ids = _seed_priority_sheet(client)
        project = client.app.state.workspace.get(pid)
        empty_view_id = _save_priority_view(
            project,
            sheet_id=sheet_id,
            priority_col_id=cols["priority"],
            priority_value="critical",
            name="Critical Only",
        )

        body = _regex_view_action(sheet_id, view_id=empty_view_id, key="empty-view@v1")
        est = client.post(
            f"/api/projects/{pid}/actions/v1/estimate",
            json={"action": body},
        )
        assert est.status_code == 200, est.text
        assert est.json()["estimate"]["rows"] == 0

        res = client.post(f"/api/projects/{pid}/actions/v1/run", json=body)
        assert res.status_code == 200, res.text
        run_id = int(res.json()["run_id"])
        drain_queue(client)

        assert RunResultStore(project).run_row_scope(run_id) == []
        code_col = _column_id(project, sheet_id, "code_num")
        assert _populated_values(project, sheet_id, code_col) == {}

        # Replay on the same idempotency_key succeeds cleanly.
        replay = client.post(f"/api/projects/{pid}/actions/v1/run", json=body)
        assert replay.status_code == 200, replay.text
        assert replay.json()["receipt_id"] == res.json()["receipt_id"]

        # Append one matching row and one non-matching row, then backfill!
        added = project.add_rows(
            sheet_id,
            [
                {"story": "critical filing 777", "priority": "critical"},
                {"story": "low filing 888", "priority": "low"},
            ],
            cols,
        )
        backfill_body = {
            "action_id": "run.backfill",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {"column": "code_num"},
            "output_names": {},
            "idempotency_key": "empty-view-backfill@v1",
        }
        bf_est = client.post(
            f"/api/projects/{pid}/actions/v1/estimate",
            json={"action": backfill_body},
        )
        assert bf_est.status_code == 200, bf_est.text
        assert bf_est.json()["estimate"]["rows"] == 1

        bf_res = client.post(f"/api/projects/{pid}/actions/v1/run", json=backfill_body)
        assert bf_res.status_code == 200, bf_res.text
        assert bf_res.json()["outputs"][0]["ref"]["filled_row_ids"] == [added[0]]
        assert _populated_values(project, sheet_id, code_col) == {added[0]: "777"}
    finally:
        client.close()


def test_adr0002_race_new_matching_row_invalidates_confirmation_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    client = _client(tmp_path)
    try:
        pid, sheet_id, cols, row_ids = _seed_priority_sheet(client)
        project = client.app.state.workspace.get(pid)
        view_id = _save_priority_view(
            project, sheet_id=sheet_id, priority_col_id=cols["priority"]
        )

        # Unpriced model guarantees explicit confirmation (402 ClaimsGate).
        body = _classify_view_action(
            sheet_id,
            view_id=view_id,
            model="anthropic/unpriced-test-model",
            key="adr0002-race@v1",
        )
        est = client.post(
            f"/api/projects/{pid}/actions/v1/estimate",
            json={"action": body},
        )
        assert est.status_code == 200, est.text
        assert est.json()["estimate"]["rows"] == 2

        challenge = client.post(f"/api/projects/{pid}/actions/v1/run", json=body)
        assert challenge.status_code == 402, challenge.text
        first_hash = confirmation_hash_from_action_result(challenge.json())
        assert first_hash is not None
        assert challenge.json()["errors"][0]["details"]["estimate"]["rows"] == 2

        # Race: a new matching row arrives between quote/challenge and confirmation!
        new_rows = project.add_rows(
            sheet_id,
            [{"story": "late breaking high priority", "priority": "high"}],
            cols,
        )

        stale_confirm = client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json={**body, "confirmation": first_hash},
        )
        assert stale_confirm.status_code == 402, stale_confirm.text
        second_hash = confirmation_hash_from_action_result(stale_confirm.json())
        assert second_hash is not None
        assert second_hash != first_hash
        assert stale_confirm.json()["errors"][0]["details"]["estimate"]["rows"] == 3
        # Zero runs created during the rejected race attempt!
        assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0

        # Confirming with the fresh hash matching all 3 rows succeeds.
        confirmed = client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json={**body, "confirmation": second_hash},
        )
        assert confirmed.status_code == 200, confirmed.text
        run_id = int(confirmed.json()["run_id"])
        drain_queue(client)
        assert RunResultStore(project).run_row_scope(run_id) == [
            row_ids[0],
            row_ids[1],
            new_rows[0],
        ]
    finally:
        client.close()


def test_adr0002_queued_worker_executes_strictly_frozen_admitted_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    client = _client(tmp_path)
    try:
        pid, sheet_id, cols, row_ids = _seed_priority_sheet(client)
        project = client.app.state.workspace.get(pid)
        view_id = _save_priority_view(
            project, sheet_id=sheet_id, priority_col_id=cols["priority"]
        )

        body = _classify_view_action(
            sheet_id, view_id=view_id, key="queued-frozen-race@v1"
        )
        queued = post_v1_action_with_exact_confirmation(client, pid, body)
        assert queued.status_code == 200, queued.text
        assert queued.json()["status"] == "queued"
        run_id = int(queued.json()["run_id"])

        # Race: insert a new matching row AFTER admission/queueing, BEFORE worker runs!
        late_row = project.add_rows(
            sheet_id,
            [{"story": "arrived while queued", "priority": "high"}],
            cols,
        )[0]

        # Worker must NEVER re-evaluate the View or touch the late row.
        def _forbid_open_selection(*_args, **_kwargs):
            raise AssertionError("Queued worker must not re-evaluate View")

        monkeypatch.setattr(
            "frisket.engine.views.evaluate.open_selection", _forbid_open_selection
        )
        drain_queue(client)
        monkeypatch.undo()

        beat_col = _column_id(project, sheet_id, "beat")
        assert RunResultStore(project).run_row_scope(run_id) == row_ids[:2]
        assert _populated_values(project, sheet_id, beat_col) == {
            row_ids[0]: "urgent_beat",
            row_ids[1]: "urgent_beat",
        }
        assert project.get_values(sheet_id, beat_col)[late_row] is None
    finally:
        client.close()


def test_backfill_new_rows_in_view_repeated_backfills_and_deleted_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    client = _client(tmp_path)
    try:
        pid, sheet_id, cols, row_ids = _seed_priority_sheet(client)
        project = client.app.state.workspace.get(pid)
        view_id = _save_priority_view(
            project, sheet_id=sheet_id, priority_col_id=cols["priority"]
        )

        # Initial run on View V (matches rows 0, 1).
        first = post_v1_action_with_exact_confirmation(
            client,
            pid,
            _classify_view_action(
                sheet_id,
                view_id=view_id,
                model="anthropic/unpriced-test-model",
                key="initial-view-classify@v1",
            ),
        )
        assert first.status_code == 200, first.text
        first_run_id = int(first.json()["run_id"])
        drain_queue(client)

        beat_col = _column_id(project, sheet_id, "beat")
        assert _populated_values(project, sheet_id, beat_col) == {
            row_ids[0]: "urgent_beat",
            row_ids[1]: "urgent_beat",
        }

        # Append row 3 (matches V) and row 4 (does NOT match V).
        batch1 = project.add_rows(
            sheet_id,
            [
                {"story": "new high 1", "priority": "high"},
                {"story": "new low 1", "priority": "low"},
            ],
            cols,
        )
        row_match_1, row_skip_1 = batch1

        # Backfill #1 (implicit: inherits View V from original run!).
        bf1_action = {
            "action_id": "run.backfill",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {"column": "beat"},
            "output_names": {},
            "idempotency_key": "view-backfill-1@v1",
        }
        bf1_est = client.post(
            f"/api/projects/{pid}/actions/v1/estimate",
            json={"action": bf1_action},
        )
        assert bf1_est.status_code == 200, bf1_est.text
        assert bf1_est.json()["estimate"]["rows"] == 1

        # ADR-0002 check on backfill: challenge -> confirm succeeds for ONLY row_match_1.
        bf1_challenge = client.post(
            f"/api/projects/{pid}/actions/v1/run", json=bf1_action
        )
        assert bf1_challenge.status_code == 402, bf1_challenge.text
        bf1_hash = confirmation_hash_from_action_result(bf1_challenge.json())
        assert bf1_hash is not None

        bf1 = client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json={**bf1_action, "confirmation": bf1_hash},
        )
        assert bf1.status_code == 200, bf1.text
        bf1_run_id = int(bf1.json()["run_id"])
        assert bf1_run_id != first_run_id
        assert bf1.json()["outputs"][0]["ref"]["requested_row_ids"] == [row_match_1]
        assert bf1.json()["outputs"][0]["ref"]["filled_row_ids"] == [row_match_1]

        # Original rows keep their original run_id; row_skip_1 stays unrun!
        heads = ResultGenerationStore(project).read_cell_heads(beat_col)
        assert heads[row_ids[0]].run_id == first_run_id
        assert heads[row_ids[1]].run_id == first_run_id
        assert heads[row_match_1].run_id == bf1_run_id
        assert row_skip_1 not in heads
        assert row_ids[2] not in heads and row_ids[3] not in heads

        # Verify View V scope was preserved on the backfill run itself!
        bf1_params = json.loads(
            project.db.execute(
                "SELECT params FROM runs WHERE id=?", (bf1_run_id,)
            ).fetchone()["params"]
        )
        assert bf1_params["row_scope"] == {"kind": "view", "view_id": view_id}

        # Append row 5 (matches V) and row 6 (does NOT match V) -> Backfill #2!
        batch2 = project.add_rows(
            sheet_id,
            [
                {"story": "new high 2", "priority": "high"},
                {"story": "new low 2", "priority": "low"},
            ],
            cols,
        )
        row_match_2, row_skip_2 = batch2

        bf2_action = {
            "action_id": "run.backfill",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {"column": "beat"},
            "output_names": {},
            "idempotency_key": "view-backfill-2@v1",
        }
        bf2_est = client.post(
            f"/api/projects/{pid}/actions/v1/estimate",
            json={"action": bf2_action},
        )
        assert bf2_est.status_code == 200, bf2_est.text
        assert bf2_est.json()["estimate"]["rows"] == 1

        bf2 = post_v1_action_with_exact_confirmation(client, pid, bf2_action)
        assert bf2.status_code == 200, bf2.text
        assert bf2.json()["outputs"][0]["ref"]["requested_row_ids"] == [row_match_2]
        assert bf2.json()["outputs"][0]["ref"]["filled_row_ids"] == [row_match_2]
        assert project.get_values(sheet_id, beat_col)[row_skip_2] is None

        # Backfill #3 when no new matching rows exist: 0 rows cleanly.
        bf3_action = {
            "action_id": "run.backfill",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "view_id": view_id},
            "params": {"column": "beat"},
            "output_names": {},
            "idempotency_key": "view-backfill-3@v1",
        }
        bf3_est = client.post(
            f"/api/projects/{pid}/actions/v1/estimate",
            json={"action": bf3_action},
        )
        assert bf3_est.status_code == 200, bf3_est.text
        assert bf3_est.json()["estimate"]["rows"] == 0

        # Deleting View V causes subsequent backfill estimate & run to fail cleanly (400 view_not_found).
        project.delete_view(view_id)
        assert project.get_view(view_id) is None
        bf_deleted_action = {
            "action_id": "run.backfill",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {"column": "beat"},
            "output_names": {},
            "idempotency_key": "view-backfill-deleted@v1",
        }
        del_est = client.post(
            f"/api/projects/{pid}/actions/v1/estimate",
            json={"action": bf_deleted_action},
        )
        assert del_est.status_code == 400, del_est.text
        assert del_est.json()["code"] == "view_not_found"

        del_run = client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json=bf_deleted_action,
        )
        assert del_run.status_code == 400, del_run.text
        assert del_run.json()["errors"][0]["code"] == "view_not_found"
    finally:
        client.close()


def test_explicit_view_id_backfill_and_undo_redo_lifecycle(tmp_path: Path) -> None:
    client = _client(tmp_path)
    try:
        pid, sheet_id, cols, row_ids = _seed_priority_sheet(client)
        project = client.app.state.workspace.get(pid)
        view_id = _save_priority_view(
            project, sheet_id=sheet_id, priority_col_id=cols["priority"]
        )

        # Initial deterministic run on View V (matches rows 0, 1).
        first = run_action_spec(
            project,
            _regex_view_action(sheet_id, view_id=view_id, key="undo-view-init@v1"),
            project_id=pid,
        )
        assert first.status == "completed", first.errors
        code_col = _column_id(project, sheet_id, "code_num")
        assert _populated_values(project, sheet_id, code_col) == {
            row_ids[0]: "101",
            row_ids[1]: "202",
        }
        assert project.get_values(sheet_id, code_col)[row_ids[2]] is None
        assert project.get_values(sheet_id, code_col)[row_ids[3]] is None

        # Add a matching row via row.add so change history is ordered cleanly.
        added = post_row_add_as_v1_action(
            client, pid, sheet_id, {"story": "contract gamma 505", "priority": "high"}
        )
        assert added.status_code == 200, added.text
        new_row_id = int(added.json()["outputs"][0]["row_ids"][0])

        # Explicit view_id backfill fills ONLY the new matching row.
        bf = run_action_spec(
            project,
            {
                "action_id": "run.backfill",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet_id,
                    "view_id": view_id,
                },
                "params": {"column": "code_num"},
                "output_names": {},
                "idempotency_key": "undo-view-bf@v1",
            },
            project_id=pid,
        )
        assert bf.status == "completed", bf.errors
        assert _populated_values(project, sheet_id, code_col) == {
            row_ids[0]: "101",
            row_ids[1]: "202",
            new_row_id: "505",
        }

        # Undo backfill -> only new_row_id reverts; rows 0 & 1 remain intact.
        assert project.undo() is not None
        assert _populated_values(project, sheet_id, code_col) == {
            row_ids[0]: "101",
            row_ids[1]: "202",
        }

        # Redo backfill -> new_row_id restored.
        assert project.redo() is not None
        assert _populated_values(project, sheet_id, code_col) == {
            row_ids[0]: "101",
            row_ids[1]: "202",
            new_row_id: "505",
        }
    finally:
        client.close()


def test_adr0002_backfill_race_new_matching_row_invalidates_confirmation_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    client = _client(tmp_path)
    try:
        pid, sheet_id, cols, _row_ids = _seed_priority_sheet(client)
        project = client.app.state.workspace.get(pid)
        view_id = _save_priority_view(
            project, sheet_id=sheet_id, priority_col_id=cols["priority"]
        )

        first = post_v1_action_with_exact_confirmation(
            client,
            pid,
            _classify_view_action(
                sheet_id,
                view_id=view_id,
                model="anthropic/unpriced-test-model",
                key="bf-race-init@v1",
            ),
        )
        assert first.status_code == 200, first.text
        drain_queue(client)

        row_1 = project.add_rows(
            sheet_id, [{"story": "new high A", "priority": "high"}], cols
        )[0]
        bf_action = {
            "action_id": "run.backfill",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "view_id": view_id},
            "params": {"column": "beat"},
            "output_names": {},
            "idempotency_key": "bf-race@v1",
        }
        challenge = client.post(f"/api/projects/{pid}/actions/v1/run", json=bf_action)
        assert challenge.status_code == 402, challenge.text
        first_hash = confirmation_hash_from_action_result(challenge.json())
        assert first_hash is not None
        assert challenge.json()["errors"][0]["details"]["estimate"]["rows"] == 1

        # Race: another matching row arrives before confirmation!
        row_2 = project.add_rows(
            sheet_id, [{"story": "new high B", "priority": "high"}], cols
        )[0]
        stale = client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json={**bf_action, "confirmation": first_hash},
        )
        assert stale.status_code == 402, stale.text
        second_hash = confirmation_hash_from_action_result(stale.json())
        assert second_hash is not None and second_hash != first_hash
        assert stale.json()["errors"][0]["details"]["estimate"]["rows"] == 2

        confirmed = client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json={**bf_action, "confirmation": second_hash},
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["outputs"][0]["ref"]["filled_row_ids"] == [row_1, row_2]
    finally:
        client.close()


def test_true_mixed_origin_column_still_refuses_view_backfill(tmp_path: Path) -> None:
    client = _client(tmp_path)
    try:
        pid, sheet_id, cols, row_ids = _seed_priority_sheet(client)
        project = client.app.state.workspace.get(pid)
        view_id = _save_priority_view(
            project, sheet_id=sheet_id, priority_col_id=cols["priority"]
        )

        # First action writes rows 0, 1 via View V.
        first = run_action_spec(
            project,
            _regex_view_action(sheet_id, view_id=view_id, key="mixed-1@v1"),
            project_id=pid,
        )
        assert first.status == "completed", first.errors

        # Second UNRELATED action replaces row 0 with a different pattern.
        second = run_action_spec(
            project,
            {
                "action_id": "map.regex_extract",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet_id,
                    "row_ids": [row_ids[0]],
                },
                "params": {"input_columns": ["story"], "pattern": r"[a-z]+"},
                "output_names": {"extracted": "code_num"},
                "replace_existing": True,
                "idempotency_key": "mixed-2@v1",
            },
            project_id=pid,
        )
        assert second.status == "completed", second.errors

        project.add_rows(
            sheet_id, [{"story": "contract delta 606", "priority": "high"}], cols
        )
        refused = client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json={
                "action_id": "run.backfill",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet_id,
                    "view_id": view_id,
                },
                "params": {"column": "code_num"},
                "output_names": {},
                "idempotency_key": "mixed-bf-refused@v1",
            },
        )
        assert refused.status_code == 400, refused.text
        assert refused.json()["errors"][0]["code"] == "mixed_origin_column_unsupported"
    finally:
        client.close()


@pytest.mark.parametrize("kind", ["map.find", "reduce.group_summary"])
def test_estimate_find_and_group_summary_respect_view_scope_and_confirmation_parity(
    tmp_path: Path, kind: str
) -> None:
    client = _client(tmp_path)
    try:
        pid, sheet_id, cols, _row_ids = _seed_priority_sheet(client)
        project = client.app.state.workspace.get(pid)
        view_id = _save_priority_view(
            project, sheet_id=sheet_id, priority_col_id=cols["priority"]
        )
        other_sheet_id = project.add_sheet("Other Sheet")
        other_col_id = project.add_column(other_sheet_id, "label", "text")
        other_view_id = _save_priority_view(
            project,
            sheet_id=other_sheet_id,
            priority_col_id=other_col_id,
            name="Other Sheet View",
            priority_value="high",
        )

        def _request(target_view_id: int, *, key: str) -> dict[str, Any]:
            if kind == "map.find":
                return {
                    "action_id": "map.find",
                    "scope": {
                        "kind": "sheet_rows",
                        "sheet_id": sheet_id,
                        "view_id": target_view_id,
                    },
                    "sheet_name": "View Findings",
                    "params": {
                        "source": "story",
                        "instruction": "Find contract numbers.",
                        "fields": [],
                        "model": "anthropic/unpriced-test-model",
                    },
                    "idempotency_key": key,
                }
            return {
                "action_id": "reduce.group_summary",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet_id,
                    "view_id": target_view_id,
                },
                "sheet_name": "View Summary",
                "params": {
                    "source": ["story"],
                    "instruction": "Summarize contracts.",
                    "model": "anthropic/unpriced-test-model",
                },
                "idempotency_key": key,
            }

        valid_req = _request(view_id, key=f"est-{kind}-valid@v1")
        est = client.post(
            f"/api/projects/{pid}/actions/v1/estimate", json={"action": valid_req}
        )
        assert est.status_code == 200, est.text
        est_body = est.json()["estimate"]
        assert est_body["rows"] == 2
        est_hash = est_body.get("promise_set_hash")
        assert est_hash is not None

        # /run challenge MUST mint the exact same confirmation_hash as /estimate.
        challenge = client.post(f"/api/projects/{pid}/actions/v1/run", json=valid_req)
        assert challenge.status_code == 402, challenge.text
        run_hash = confirmation_hash_from_action_result(challenge.json())
        assert run_hash == est_hash
        if kind == "map.find":
            assert challenge.json()["errors"][0]["details"]["estimate"]["rows"] == 2

        # Missing view -> HTTP 400 view_not_found on scope.view_id.
        missing = client.post(
            f"/api/projects/{pid}/actions/v1/estimate",
            json={"action": _request(99999, key=f"est-{kind}-missing@v1")},
        )
        assert missing.status_code == 400, missing.text
        assert missing.json()["code"] == "view_not_found"
        assert missing.json()["field"] == "scope.view_id"

        # Cross-sheet view -> HTTP 400 view_sheet_mismatch on scope.view_id.
        cross = client.post(
            f"/api/projects/{pid}/actions/v1/estimate",
            json={"action": _request(other_view_id, key=f"est-{kind}-cross@v1")},
        )
        assert cross.status_code == 400, cross.text
        assert cross.json()["code"] == "view_sheet_mismatch"
        assert cross.json()["field"] == "scope.view_id"

        # Pending-cell filter input -> HTTP 400 selection_incomplete.
        queued_filter = client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json={
                "action_id": "map.regex_extract",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet_id,
                    "row_ids": [_row_ids[0]],
                },
                "params": {"input_columns": ["story"], "pattern": r"\d+"},
                "output_names": {"extracted": "pending_flag"},
                "idempotency_key": f"est-{kind}-seed-pending@v1",
            },
        )
        assert queued_filter.status_code == 200, queued_filter.text
        pending_col = _column_id(project, sheet_id, "pending_flag")
        pending_view_id = _save_priority_view(
            project,
            sheet_id=sheet_id,
            priority_col_id=pending_col,
            name="Pending Filter",
            priority_value="101",
        )
        incomplete = client.post(
            f"/api/projects/{pid}/actions/v1/estimate",
            json={"action": _request(pending_view_id, key=f"est-{kind}-pending@v1")},
        )
        assert incomplete.status_code == 400, incomplete.text
        assert incomplete.json()["code"] == "selection_incomplete"
    finally:
        client.close()


def test_zero_row_view_noop_backfill_does_not_brick_future_backfill(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    try:
        pid, sheet_id, cols, _row_ids = _seed_priority_sheet(client)
        project = client.app.state.workspace.get(pid)
        urgent_view_id = _save_priority_view(
            project,
            sheet_id=sheet_id,
            priority_col_id=cols["priority"],
            name="Urgent Only",
            priority_value="urgent",
        )

        # R1: 0-row View run.
        r1 = client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json=_regex_view_action(sheet_id, view_id=urgent_view_id, key="z0-r1@v1"),
        )
        assert r1.status_code == 200, r1.text
        drain_queue(client)

        # R2: No-op backfill while the View still matches 0 rows.
        r2 = client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json={
                "action_id": "run.backfill",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {"column": "code_num"},
                "output_names": {},
                "idempotency_key": "z0-r2-noop@v1",
            },
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["outputs"][0]["ref"]["filled_row_ids"] == []

        # New rows arrive: one matches Urgent, one does not.
        row_urgent = project.add_rows(
            sheet_id, [{"story": "urgent contract 777", "priority": "urgent"}], cols
        )[0]
        row_low = project.add_rows(
            sheet_id, [{"story": "low contract 888", "priority": "low"}], cols
        )[0]

        # R3: Future backfill MUST still work and populate ONLY row_urgent.
        r3 = client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json={
                "action_id": "run.backfill",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {"column": "code_num"},
                "output_names": {},
                "idempotency_key": "z0-r3-fill@v1",
            },
        )
        assert r3.status_code == 200, r3.text
        assert r3.json()["outputs"][0]["ref"]["filled_row_ids"] == [row_urgent]
        code_values = project.get_values(
            sheet_id, _column_id(project, sheet_id, "code_num")
        )
        assert code_values.get(row_urgent) == "777"
        assert code_values.get(row_low) is None

        # Undoing R3, the added rows, R2, and R1 removes published empty generations cleanly.
        for _ in range(5):
            assert project.undo() is not None
        after_undo = client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json={
                "action_id": "run.backfill",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {"column": "code_num"},
                "output_names": {},
                "idempotency_key": "z0-after-undo@v1",
            },
        )
        assert after_undo.status_code == 400, after_undo.text
    finally:
        client.close()


def test_adhoc_selection_and_view_backfill_preserve_inherited_lineage_row_scope(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    try:
        pid, sheet_id, cols, _row_ids = _seed_priority_sheet(client)
        project = client.app.state.workspace.get(pid)
        high_view_id = _save_priority_view(
            project,
            sheet_id=sheet_id,
            priority_col_id=cols["priority"],
            name="High Priority",
            priority_value="high",
        )

        # Scenario A: View-origin column + ad-hoc scope.selection backfill
        # MUST NOT widen subsequent plain backfill to all rows on the sheet!
        r1 = client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json=_regex_view_action(sheet_id, view_id=high_view_id, key="lin-a1@v1"),
        )
        assert r1.status_code == 200, r1.text
        drain_queue(client)

        row_med = project.add_rows(
            sheet_id, [{"story": "med contract 901", "priority": "medium"}], cols
        )[0]
        row_high = project.add_rows(
            sheet_id, [{"story": "high contract 902", "priority": "high"}], cols
        )[0]
        row_low = project.add_rows(
            sheet_id, [{"story": "low contract 903", "priority": "low"}], cols
        )[0]

        adhoc_sel = client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json={
                "action_id": "run.backfill",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet_id,
                    "selection": {
                        "scope": {"kind": "sheet", "sheet_id": sheet_id},
                        "filters": [
                            {
                                "column_id": cols["priority"],
                                "condition": {"eq": "medium"},
                            }
                        ],
                        "order": [],
                        "limit": None,
                    },
                },
                "params": {"column": "code_num"},
                "output_names": {},
                "idempotency_key": "lin-a2-adhoc-sel@v1",
            },
        )
        assert adhoc_sel.status_code == 200, adhoc_sel.text
        assert adhoc_sel.json()["outputs"][0]["ref"]["filled_row_ids"] == [row_med]

        plain_after_sel = client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json={
                "action_id": "run.backfill",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {"column": "code_num"},
                "output_names": {},
                "idempotency_key": "lin-a3-plain@v1",
            },
        )
        assert plain_after_sel.status_code == 200, plain_after_sel.text
        assert plain_after_sel.json()["outputs"][0]["ref"]["filled_row_ids"] == [
            row_high
        ]
        code_values = project.get_values(
            sheet_id, _column_id(project, sheet_id, "code_num")
        )
        assert code_values.get(row_high) == "902"
        assert code_values.get(row_low) is None

        # Scenario B: All-rows-origin column + ad-hoc scope.view_id backfill
        # MUST NOT narrow subsequent plain backfill to that View (nor break if deleted)!
        all_r1 = client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json={
                "action_id": "map.regex_extract",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {"input_columns": ["story"], "pattern": r"\d+"},
                "output_names": {"extracted": "all_code"},
                "idempotency_key": "lin-b1-all@v1",
            },
        )
        assert all_r1.status_code == 200, all_r1.text
        drain_queue(client)

        row_high2 = project.add_rows(
            sheet_id, [{"story": "high contract 911", "priority": "high"}], cols
        )[0]
        row_low2 = project.add_rows(
            sheet_id, [{"story": "low contract 912", "priority": "low"}], cols
        )[0]

        adhoc_view = client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json={
                "action_id": "run.backfill",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet_id,
                    "view_id": high_view_id,
                },
                "params": {"column": "all_code"},
                "output_names": {},
                "idempotency_key": "lin-b2-adhoc-view@v1",
            },
        )
        assert adhoc_view.status_code == 200, adhoc_view.text
        assert adhoc_view.json()["outputs"][0]["ref"]["filled_row_ids"] == [row_high2]

        # Delete the ad-hoc View; plain backfill on the all-rows column MUST still work!
        project.delete_view(high_view_id)
        plain_after_view = client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json={
                "action_id": "run.backfill",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {"column": "all_code"},
                "output_names": {},
                "idempotency_key": "lin-b3-plain@v1",
            },
        )
        assert plain_after_view.status_code == 200, plain_after_view.text
        assert plain_after_view.json()["outputs"][0]["ref"]["filled_row_ids"] == [
            row_low2
        ]
        all_values = project.get_values(
            sheet_id, _column_id(project, sheet_id, "all_code")
        )
        assert all_values.get(row_low2) == "912"
    finally:
        client.close()


def test_action_history_and_map_preview_view_validation(tmp_path: Path) -> None:
    client = _client(tmp_path)
    try:
        pid, sheet_id, cols, _row_ids = _seed_priority_sheet(client)
        project = client.app.state.workspace.get(pid)
        view_id = _save_priority_view(
            project, sheet_id=sheet_id, priority_col_id=cols["priority"]
        )
        other_sheet_id = project.add_sheet("Other Sheet")
        other_col_id = project.add_column(other_sheet_id, "label", "text")
        other_view_id = _save_priority_view(
            project,
            sheet_id=other_sheet_id,
            priority_col_id=other_col_id,
            name="Other View",
        )

        # resolve_map_preview reports field="scope.view_id" for missing/cross-sheet views.
        missing_err = resolve_map_preview(
            project,
            _regex_view_action(sheet_id, view_id=99999, key="prev-missing@v1"),
        )
        assert getattr(missing_err, "code", None) == "view_not_found"
        assert getattr(missing_err, "field", None) == "scope.view_id"

        cross_err = resolve_map_preview(
            project,
            _regex_view_action(sheet_id, view_id=other_view_id, key="prev-cross@v1"),
        )
        assert getattr(cross_err, "code", None) == "view_sheet_mismatch"
        assert getattr(cross_err, "field", None) == "scope.view_id"

        # Action history restores view_id while View exists and marks unavailable after delete.
        run_resp = client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json=_regex_view_action(sheet_id, view_id=view_id, key="hist-view@v1"),
        )
        assert run_resp.status_code == 200, run_resp.text

        history_resp = client.get(
            f"/api/projects/{pid}/actions/history",
            params={"action_id": "map.regex_extract"},
        )
        assert history_resp.status_code == 200, history_resp.text
        items = history_resp.json()["items"]
        assert items
        history_id = items[0]["id"]

        detail_before = client.get(f"/api/projects/{pid}/actions/history/{history_id}")
        assert detail_before.status_code == 200, detail_before.text
        assert detail_before.json()["unavailable_reason"] is None
        assert detail_before.json()["draft"]["scope"]["view_id"] == view_id

        project.delete_view(view_id)
        detail_after = client.get(f"/api/projects/{pid}/actions/history/{history_id}")
        assert detail_after.status_code == 200, detail_after.text
        assert detail_after.json()["draft"] is None
        assert detail_after.json()["unavailable_reason"] == (
            "The original view is no longer available."
        )
    finally:
        client.close()
