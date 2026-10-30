from __future__ import annotations

from pathlib import Path
import copy
import json
from typing import Any

from fastapi.testclient import TestClient
import pytest

from frisket.actions.registry import ACTION_REGISTRY
from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.engine.executor import resolve_map_preview
from frisket.engine.executor.actions import MapPreviewPlan
from frisket.engine.runner.preview import PreviewColumn, PreviewResult
from frisket.server.app import create_app
from frisket.server.services.action_compare import ActionCompareService
from frisket.server.services.action_preview_runs import ActionPreviewRunService
from frisket.server.workspace import Workspace
from http_test_helpers import (
    drain_queue,
    post_cell_edit_as_v1_action,
    post_column_add_as_v1_action,
    post_operation_undo_as_v1_action,
    post_review_decision_as_v1_action,
    post_v1_action_with_exact_confirmation,
)


class _ClassifyAdapter:
    async def complete(self, request: LLMRequest, client: Any) -> LLMResponse:
        del client
        data = {"beat": "civic"}
        return LLMResponse(
            content=json.dumps(data),
            data=data,
            tokens_in=5,
            tokens_out=2,
            cost=0.0,
            model=request.model,
        )


class _PartialClassifyAdapter:
    async def complete(self, request: LLMRequest, client: Any) -> LLMResponse:
        del client
        data = (
            {"beat": "civic"}
            if "Transit opened." in str(request.messages)
            else {"beat": "civic", "tone": "neutral"}
        )
        return LLMResponse(
            content=json.dumps(data),
            data=data,
            tokens_in=5,
            tokens_out=2,
            cost=0.0,
            model=request.model,
        )


def _router(adapter: Any | None = None) -> ModelRouter:
    router = ModelRouter(keys={"anthropic": "test"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = adapter or _ClassifyAdapter()  # noqa: SLF001
    return router


def _workspace(root: Path) -> tuple[Workspace, int, list[int]]:
    workspace = Workspace(root, enable_local_model_pull=False)
    workspace.create("Compare", project_id="compare")
    project = workspace.get("compare")
    sheet_id = project.add_sheet("Stories")
    text_id = project.add_column(sheet_id, "text", type="text")
    row_ids = project.add_rows(
        sheet_id,
        [{"text": "Alpha"}, {"text": "Beta"}],
        {"text": text_id},
    )
    return workspace, sheet_id, row_ids


def _ask_draft(sheet_id: int, row_ids: list[int]) -> dict:
    return {
        "action_id": "map.ask",
        "scope": {
            "kind": "sheet_rows",
            "sheet_id": sheet_id,
            "row_ids": row_ids,
        },
        "params": {
            "source": ["text"],
            "model": "anthropic/claude-haiku-4-5",
            "question": "What is the subject?",
        },
        "output_names": {"answer": "subject"},
    }


def _completed_classify_run(
    client: TestClient,
    project_id: str,
    project: Any,
    *,
    fields: list[dict[str, Any]],
    idempotency_key: str,
) -> tuple[int, list[int], int, dict[str, int]]:
    sheet_id = project.add_sheet("Stories")
    source_id = project.add_column(sheet_id, "story", type="text")
    row_ids = project.add_rows(
        sheet_id,
        [{"story": "City hall voted."}, {"story": "Transit opened."}],
        {"story": source_id},
    )
    started = post_v1_action_with_exact_confirmation(
        client,
        project_id,
        {
            "action_id": "map.classify",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {
                "source": ["story"],
                "engine": "llm",
                "model": "anthropic/claude-haiku-4-5",
                "fields": fields,
            },
            "idempotency_key": idempotency_key,
        },
    )
    assert started.status_code == 200, started.text
    run_id = int(started.json()["run_id"])
    drain_queue(client)
    output_ids = {
        str(column["name"]): int(column["id"])
        for column in project.columns(sheet_id)
        if column["name"] != "story"
    }
    return sheet_id, row_ids, run_id, output_ids


def test_catalog_has_one_typed_row_overlay_opt_in_for_initial_actions() -> None:
    eligible = {
        entry.action_id
        for entry in ACTION_REGISTRY.actions
        if entry.catalog_entry()["ui_hints"].get("comparison")
        == {"kind": "row_overlay"}
    }
    assert eligible == {"map.extract", "map.ask", "map.classify"}
    structural = {
        entry.action_id: entry.definition.comparison.structural_params
        for entry in ACTION_REGISTRY.actions
        if entry.action_id in eligible and entry.definition.comparison is not None
    }
    assert structural == {
        "map.ask": (
            "response_type",
            "include_confidence",
            "include_reasoning",
        ),
        "map.extract": ("fields", "include_confidence"),
        "map.classify": (
            "fields",
            "include_confidence",
            "include_justification",
        ),
    }


def test_draft_context_is_bounded_canonical_and_read_only(tmp_path: Path) -> None:
    workspace, sheet_id, row_ids = _workspace(tmp_path / "workspace")
    project = workspace.get("compare")
    before = {
        table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("ops", "runs", "results", "receipts", "model_calls")
    }

    payload = ActionCompareService(workspace).context(
        "compare",
        {
            "source": {"kind": "draft", "draft": _ask_draft(sheet_id, row_ids)},
            "sample_row_ids": row_ids,
        },
    )

    assert payload["schema_version"] == "frisket.action_compare_context.v1"
    assert payload["available"] is True
    assert payload["baseline_mode"] == "preview"
    assert payload["comparison_basis"] == {
        "kind": "draft",
        "draft": payload["draft"],
    }
    assert payload["sample_row_ids"] == row_ids
    assert payload["locked_params"] == [
        "include_confidence",
        "include_reasoning",
        "response_type",
        "source",
    ]
    assert payload["source_columns"] == [
        {
            "param": "source",
            "column_id": 1,
            "name": "text",
            "column_type": "text",
            "format": None,
        }
    ]
    assert payload["output_columns"][0]["logical_key"] == "answer"
    assert payload["replacement_targets"] == []
    assert payload["baseline_rows"] is None
    assert payload["sample_fingerprint"]
    assert {
        table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in before
    } == before


def test_context_refuses_non_opted_in_action_without_running_it(tmp_path: Path) -> None:
    workspace, sheet_id, row_ids = _workspace(tmp_path / "workspace")
    payload = ActionCompareService(workspace).context(
        "compare",
        {
            "source": {
                "kind": "draft",
                "draft": {
                    "action_id": "map.template",
                    "scope": {
                        "kind": "sheet_rows",
                        "sheet_id": sheet_id,
                        "row_ids": row_ids,
                    },
                    "params": {"template": {"text": "{{text}}"}},
                    "output_names": {"rendered": "copy"},
                },
            },
            "sample_row_ids": row_ids,
        },
    )
    assert payload["available"] is False
    assert payload["reason"]["code"] == "comparison_not_supported"


def test_run_context_uses_complete_binding_and_current_renamed_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = create_app(
        tmp_path / "http-workspace",
        router=_router(),
        serve_spa=False,
        enable_provider_config=False,
    )
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={"name": "Compare run"}).json()["id"]
        project = app.state.workspace.get(pid)
        sheet_id = project.add_sheet("Stories")
        source_id = project.add_column(sheet_id, "story", type="text")
        row_ids = project.add_rows(
            sheet_id,
            [{"story": "City hall voted."}, {"story": "Transit opened."}],
            {"story": source_id},
        )
        started = post_v1_action_with_exact_confirmation(
            client,
            pid,
            {
                "action_id": "map.classify",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {
                    "source": ["story"],
                    "engine": "llm",
                    "model": "anthropic/claude-haiku-4-5",
                    "fields": [
                        {
                            "name": "beat",
                            "type": "category",
                            "labels": ["civic", "private"],
                        }
                    ],
                },
                "idempotency_key": "action-compare-run@1",
            },
        )
        assert started.status_code == 200, started.text
        run_id = int(started.json()["run_id"])
        drain_queue(client)
        output = next(
            column for column in project.columns(sheet_id) if column["name"] == "beat"
        )
        with project.db:
            project.db.execute(
                "UPDATE columns SET name='reporting_beat' WHERE id=?",
                (int(output["id"]),),
            )

        response = client.post(
            f"/api/projects/{pid}/actions/v1/compare-context",
            json={
                "source": {
                    "kind": "run",
                    "run_id": run_id,
                    "column_id": int(output["id"]),
                },
                "sample_row_ids": row_ids,
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["baseline_mode"] == "current_values"
        assert payload["draft"]["output_names"] == {"beat": "reporting_beat"}
        assert payload["replacement_targets"] == [
            {
                "logical_key": "beat",
                "current_name": "reporting_beat",
                "column_id": int(output["id"]),
            }
        ]
        assert {row["beat"]["state"] for row in payload["baseline_rows"].values()} == {
            "value"
        }
        assert payload["origin_summary"] == {
            "mixed_runs": False,
            "includes_manual_edits": False,
            "includes_reject_clear": False,
        }

        replacement = post_v1_action_with_exact_confirmation(
            client,
            pid,
            {
                "action_id": "map.classify",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {
                    "source": ["story"],
                    "engine": "llm",
                    "model": "anthropic/claude-haiku-4-5",
                    "fields": [
                        {
                            "name": "beat",
                            "type": "category",
                            "labels": ["civic", "private"],
                        }
                    ],
                },
                "output_names": {"beat": "reporting_beat"},
                "replace_existing": True,
                "expected_output_columns": {
                    "reporting_beat": int(output["id"]),
                },
                "idempotency_key": "action-compare-run@replacement",
            },
        )
        assert replacement.status_code == 200, replacement.text
        drain_queue(client)
        replaced_context = client.post(
            f"/api/projects/{pid}/actions/v1/compare-context",
            json={
                "source": {
                    "kind": "run",
                    "run_id": run_id,
                    "column_id": int(output["id"]),
                },
                "sample_row_ids": row_ids,
            },
        )
        assert replaced_context.status_code == 200, replaced_context.text
        assert replaced_context.json()["origin_summary"]["mixed_runs"] is True

        monkeypatch.setattr(
            "frisket.server.services.action_compare._compatibility_key",
            lambda **_kwargs: "sha256:" + "0" * 64,
        )
        incompatible = client.post(
            f"/api/projects/{pid}/actions/v1/compare-context",
            json={
                "source": {
                    "kind": "run",
                    "run_id": run_id,
                    "column_id": int(output["id"]),
                },
                "sample_row_ids": row_ids,
            },
        )
        assert incompatible.status_code == 409


def test_run_context_accepts_a_real_backfill_successor_receipt(tmp_path: Path) -> None:
    app = create_app(
        tmp_path / "backfill-workspace",
        router=_router(),
        serve_spa=False,
        enable_provider_config=False,
    )
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={"name": "Backfill compare"}).json()[
            "id"
        ]
        project = app.state.workspace.get(pid)
        sheet_id = project.add_sheet("Stories")
        source_id = project.add_column(sheet_id, "story", type="text")
        row_ids = project.add_rows(
            sheet_id,
            [{"story": "City hall voted."}],
            {"story": source_id},
        )
        original = post_v1_action_with_exact_confirmation(
            client,
            pid,
            {
                "action_id": "map.classify",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet_id,
                    "row_ids": row_ids,
                },
                "params": {
                    "source": ["story"],
                    "engine": "llm",
                    "model": "anthropic/claude-haiku-4-5",
                    "fields": [
                        {
                            "name": "beat",
                            "type": "category",
                            "labels": ["civic", "private"],
                        }
                    ],
                },
                "idempotency_key": "action-compare-backfill@original",
            },
        )
        assert original.status_code == 200, original.text
        drain_queue(client)
        output_id = int(
            next(
                column
                for column in project.columns(sheet_id)
                if column["name"] == "beat"
            )["id"]
        )
        successor = post_v1_action_with_exact_confirmation(
            client,
            pid,
            {
                "action_id": "run.backfill",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet_id,
                    "row_ids": row_ids,
                },
                "params": {"column": "beat"},
                "idempotency_key": "action-compare-backfill@successor",
            },
        )
        assert successor.status_code == 200, successor.text
        successor_run_id = int(successor.json()["run_id"])
        drain_queue(client)

        context_response = client.post(
            f"/api/projects/{pid}/actions/v1/compare-context",
            json={
                "source": {
                    "kind": "run",
                    "run_id": successor_run_id,
                    "column_id": output_id,
                },
                "sample_row_ids": row_ids,
            },
        )

        assert context_response.status_code == 200, context_response.text
        assert context_response.json()["draft"]["action_id"] == "map.classify"


def test_run_context_reports_real_overlays_and_complete_partial_output_family(
    tmp_path: Path,
) -> None:
    app = create_app(
        tmp_path / "partial-family-workspace",
        router=_router(_PartialClassifyAdapter()),
        serve_spa=False,
        enable_provider_config=False,
    )
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={"name": "Compare family"}).json()["id"]
        project = app.state.workspace.get(pid)
        _sheet_id, row_ids, run_id, output_ids = _completed_classify_run(
            client,
            pid,
            project,
            fields=[
                {
                    "name": "beat",
                    "type": "category",
                    "labels": ["civic", "private"],
                },
                {
                    "name": "tone",
                    "type": "category",
                    "labels": ["neutral", "urgent"],
                },
            ],
            idempotency_key="action-compare-family@run",
        )
        edited = post_cell_edit_as_v1_action(
            client,
            pid,
            [
                {
                    "row_id": row_ids[0],
                    "column_id": output_ids["tone"],
                    "value": "urgent",
                }
            ],
            idempotency_key="action-compare-family@edit",
        )
        assert edited.status_code == 200, edited.text
        rejected = post_review_decision_as_v1_action(
            client,
            pid,
            run_id=run_id,
            row_id=row_ids[0],
            column_id=output_ids["beat"],
            decision="reject_clear",
            idempotency_key="action-compare-family@reject-clear",
        )
        assert rejected.status_code == 200, rejected.text

        response = client.post(
            f"/api/projects/{pid}/actions/v1/compare-context",
            json={
                "source": {
                    "kind": "run",
                    "run_id": run_id,
                    "column_id": output_ids["beat"],
                },
                "sample_row_ids": row_ids,
            },
        )

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["origin_summary"] == {
            "mixed_runs": False,
            "includes_manual_edits": True,
            "includes_reject_clear": True,
        }
        assert [column["logical_key"] for column in payload["output_columns"]] == [
            "beat",
            "tone",
        ]
        assert {
            target["logical_key"]: target["column_id"]
            for target in payload["replacement_targets"]
        } == {
            "beat": output_ids["beat"],
            "tone": output_ids["tone"],
        }
        first = payload["baseline_rows"][str(row_ids[0])]
        second = payload["baseline_rows"][str(row_ids[1])]
        assert first["tone"] == {
            "state": "value",
            "value": "urgent",
            "origin": {
                "kind": "manual_edit",
                "run_id": None,
                "op_id": edited.json()["op_ids"][0],
                "review_decision": "edit",
            },
        }
        assert first["beat"] == {
            "state": "null",
            "value": None,
            "origin": {
                "kind": "manual_edit",
                "run_id": None,
                "op_id": rejected.json()["op_ids"][0],
                "review_decision": "reject_clear",
            },
        }
        assert second["beat"]["state"] == "error"
        assert second["tone"]["state"] == "error"


def test_compare_preview_refuses_a_source_retired_after_context_capture(
    tmp_path: Path,
) -> None:
    app = create_app(
        tmp_path / "retired-source-workspace",
        serve_spa=False,
        enable_provider_config=False,
    )
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={"name": "Retired source"}).json()["id"]
        project = app.state.workspace.get(pid)
        sheet_id = project.add_sheet("Stories")
        base_id = project.add_column(sheet_id, "base")
        row_ids = project.add_rows(
            sheet_id,
            [{"base": "Alpha"}, {"base": "Beta"}],
            {"base": base_id},
        )
        added = post_column_add_as_v1_action(
            client,
            pid,
            sheet_id,
            "text",
            idempotency_key="action-compare-source@add",
        )
        assert added.status_code == 200, added.text
        context_response = client.post(
            f"/api/projects/{pid}/actions/v1/compare-context",
            json={
                "source": {
                    "kind": "draft",
                    "draft": _ask_draft(sheet_id, row_ids),
                },
                "sample_row_ids": row_ids,
            },
        )
        assert context_response.status_code == 200, context_response.text
        context = context_response.json()

        retired = post_operation_undo_as_v1_action(
            client,
            pid,
            expected_op_id=added.json()["op_ids"][0],
            idempotency_key="action-compare-source@retire",
        )
        assert retired.status_code == 200, retired.text
        assert [column["name"] for column in project.columns(sheet_id)] == ["base"]
        receipt_count = project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[
            0
        ]

        preview = client.post(
            f"/api/projects/{pid}/actions/v1/compare-preview",
            json={
                "action": {
                    **context["draft"],
                    "idempotency_key": "action-compare-source@preview",
                },
                "comparison": {
                    "basis": context["comparison_basis"],
                    "expected_output_signature": context["expected_output_signature"],
                },
            },
        )

        assert 400 <= preview.status_code < 500, preview.text
        assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == (
            receipt_count
        )


def test_output_signature_preserves_description_property_and_semantic_type(
    tmp_path: Path,
) -> None:
    workspace, sheet_id, row_ids = _workspace(tmp_path / "workspace")
    compare = ActionCompareService(workspace)
    draft = {
        "action_id": "map.extract",
        "scope": {
            "kind": "sheet_rows",
            "sheet_id": sheet_id,
            "row_ids": row_ids,
        },
        "params": {
            "source": ["text"],
            "engine": "llm",
            "model": "anthropic/claude-haiku-4-5",
            "fields": [
                {
                    "name": "record",
                    "type": "json",
                    "description": "Editable annotation",
                    "properties": {"description": {"type": "string"}},
                }
            ],
        },
        "output_names": {"record": "record"},
    }
    context = compare.context(
        "compare",
        {
            "source": {"kind": "draft", "draft": draft},
            "sample_row_ids": row_ids,
        },
    )

    def plan_for(candidate: dict) -> MapPreviewPlan:
        plan = resolve_map_preview(
            workspace.get("compare"),
            {**candidate, "idempotency_key": "compare-signature-candidate"},
        )
        assert isinstance(plan, MapPreviewPlan), plan
        return plan

    admission = {
        "basis": context["comparison_basis"],
        "expected_output_signature": context["expected_output_signature"],
    }
    edited_description = json.loads(json.dumps(context["draft"]))
    edited_description["params"]["fields"][0]["description"] = "New annotation"
    assert (
        compare.validate_preview_admission(
            "compare",
            action=edited_description,
            comparison=admission,
            plan=plan_for(edited_description),
        )
        is None
    )

    changed_property = json.loads(json.dumps(context["draft"]))
    changed_property["params"]["fields"][0]["properties"]["description"] = {
        "type": "integer"
    }
    refusal = compare.validate_preview_admission(
        "compare",
        action=changed_property,
        comparison=admission,
        plan=plan_for(changed_property),
    )
    assert refusal is not None
    assert refusal["code"] == "comparison_output_mismatch"

    semantic_plan = plan_for(context["draft"])
    semantic_plan.output_fields = tuple(
        {**field, "semantic_type": "changed-semantic-type"}
        for field in semantic_plan.output_fields
    )
    semantic_refusal = compare.validate_preview_admission(
        "compare",
        action=context["draft"],
        comparison=admission,
        plan=semantic_plan,
    )
    assert semantic_refusal is not None
    assert semantic_refusal["code"] == "comparison_output_mismatch"


@pytest.mark.parametrize("hidden_target", ["sheet", "source"])
def test_context_maps_hidden_inputs_to_public_4xx(
    tmp_path: Path,
    hidden_target: str,
) -> None:
    app = create_app(
        tmp_path / f"hidden-{hidden_target}",
        serve_spa=False,
        enable_provider_config=False,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        pid = client.post("/api/projects", json={"name": "Hidden input"}).json()["id"]
        project = app.state.workspace.get(pid)
        sheet_id = project.add_sheet("Stories")
        source_id = project.add_column(sheet_id, "text", type="text")
        row_ids = project.add_rows(
            sheet_id,
            [{"text": "Alpha"}],
            {"text": source_id},
        )
        with project.db:
            if hidden_target == "sheet":
                project.db.execute("UPDATE sheets SET hidden=1 WHERE id=?", (sheet_id,))
            else:
                project.db.execute(
                    "UPDATE columns SET hidden=1 WHERE id=?", (source_id,)
                )

        response = client.post(
            f"/api/projects/{pid}/actions/v1/compare-context",
            json={
                "source": {
                    "kind": "draft",
                    "draft": _ask_draft(sheet_id, row_ids),
                },
                "sample_row_ids": row_ids,
            },
        )

        assert 400 <= response.status_code < 500, response.text


def test_compare_preview_allows_source_value_changes_after_context_capture(
    tmp_path: Path,
) -> None:
    app = create_app(
        tmp_path / "changed-value-workspace",
        router=_router(),
        serve_spa=False,
        enable_provider_config=False,
    )
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={"name": "Changed value"}).json()["id"]
        project = app.state.workspace.get(pid)
        sheet_id = project.add_sheet("Stories")
        source_id = project.add_column(sheet_id, "text", type="text")
        row_ids = project.add_rows(
            sheet_id,
            [{"text": "Alpha"}, {"text": "Beta"}],
            {"text": source_id},
        )
        context_request = {
            "source": {"kind": "draft", "draft": _ask_draft(sheet_id, row_ids)},
            "sample_row_ids": row_ids,
        }
        context_response = client.post(
            f"/api/projects/{pid}/actions/v1/compare-context",
            json=context_request,
        )
        assert context_response.status_code == 200, context_response.text
        context = context_response.json()

        edited = post_cell_edit_as_v1_action(
            client,
            pid,
            [{"row_id": row_ids[0], "column_id": source_id, "value": "Changed"}],
            idempotency_key="compare-preview-value@edit",
        )
        assert edited.status_code == 200, edited.text
        refreshed_response = client.post(
            f"/api/projects/{pid}/actions/v1/compare-context",
            json=context_request,
        )
        assert refreshed_response.status_code == 200, refreshed_response.text
        assert (
            refreshed_response.json()["sample_fingerprint"]
            != context["sample_fingerprint"]
        )

        body = {
            "action": {
                **context["draft"],
                "idempotency_key": "compare-preview-value@preview",
            },
            "comparison": {
                "basis": context["comparison_basis"],
                "expected_output_signature": context["expected_output_signature"],
            },
        }
        response = client.post(
            f"/api/projects/{pid}/actions/v1/compare-preview", json=body
        )
        if response.status_code == 402:
            body["action"]["confirmation"] = response.json()["error"]["details"][
                "promise_set_hash"
            ]
            response = client.post(
                f"/api/projects/{pid}/actions/v1/compare-preview", json=body
            )
        assert response.status_code == 202, response.text
        preview_url = (
            f"/api/projects/{pid}/actions/v1/preview/{response.json()['preview_id']}"
        )
        cancelled = client.delete(preview_url)
        assert cancelled.status_code == 204, cancelled.text
        discarded = client.delete(
            preview_url,
            params={"discard": "true"},
        )
        assert discarded.status_code == 204, discarded.text


def test_compare_preview_refuses_changed_output_family_before_cost_gate(
    tmp_path: Path,
) -> None:
    workspace, sheet_id, row_ids = _workspace(tmp_path / "workspace")
    compare = ActionCompareService(workspace)
    context = compare.context(
        "compare",
        {
            "source": {"kind": "draft", "draft": _ask_draft(sheet_id, row_ids)},
            "sample_row_ids": row_ids,
        },
    )
    preview = ActionPreviewRunService(workspace)
    changed = {**context["draft"], "idempotency_key": "compare-preview@shape"}
    changed["params"] = {**changed["params"], "include_reasoning": True}
    response = preview.start_compare_preview(
        "compare",
        {
            "action": changed,
            "comparison": {
                "basis": context["comparison_basis"],
                "expected_output_signature": context["expected_output_signature"],
            },
        },
    )
    assert response.status_code == 409
    assert response.payload["error"]["code"] == "comparison_output_mismatch"
    project = workspace.get("compare")
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_compare_preview_refuses_changed_action_before_start(tmp_path: Path) -> None:
    workspace, sheet_id, row_ids = _workspace(tmp_path / "workspace")
    compare = ActionCompareService(workspace)
    context = compare.context(
        "compare",
        {
            "source": {"kind": "draft", "draft": _ask_draft(sheet_id, row_ids)},
            "sample_row_ids": row_ids,
        },
    )
    preview = ActionPreviewRunService(workspace)
    response = preview.start_compare_preview(
        "compare",
        {
            "action": {
                "action_id": "map.extract",
                "scope": context["draft"]["scope"],
                "params": {
                    "source": ["text"],
                    "model": "anthropic/claude-haiku-4-5",
                    "fields": [{"name": "answer", "type": "text"}],
                },
                "output_names": {"answer": "subject"},
                "idempotency_key": "compare-preview@other-action",
            },
            "comparison": {
                "basis": context["comparison_basis"],
                "expected_output_signature": context["expected_output_signature"],
            },
        },
    )
    assert response.status_code == 409
    assert response.payload["error"]["code"] == "comparison_action_mismatch"
    assert (
        workspace.get("compare")
        .db.execute("SELECT COUNT(*) FROM receipts")
        .fetchone()[0]
        == 0
    )


def test_compare_preview_refuses_changed_output_name_before_start(
    tmp_path: Path,
) -> None:
    workspace, sheet_id, row_ids = _workspace(tmp_path / "workspace")
    compare = ActionCompareService(workspace)
    context = compare.context(
        "compare",
        {
            "source": {"kind": "draft", "draft": _ask_draft(sheet_id, row_ids)},
            "sample_row_ids": row_ids,
        },
    )
    preview = ActionPreviewRunService(workspace)
    changed = {**context["draft"], "idempotency_key": "compare-preview@renamed"}
    changed["output_names"] = {"answer": "different_subject"}
    response = preview.start_compare_preview(
        "compare",
        {
            "action": changed,
            "comparison": {
                "basis": context["comparison_basis"],
                "expected_output_signature": context["expected_output_signature"],
            },
        },
    )
    assert response.status_code == 409
    assert response.payload["error"]["code"] == "comparison_output_mismatch"
    assert (
        workspace.get("compare")
        .db.execute("SELECT COUNT(*) FROM receipts")
        .fetchone()[0]
        == 0
    )


def test_compare_preview_refuses_changed_source_template_before_start(
    tmp_path: Path,
) -> None:
    workspace, sheet_id, row_ids = _workspace(tmp_path / "workspace")
    draft = _ask_draft(sheet_id, row_ids)
    draft["params"] = {
        **draft["params"],
        "source": {"text": "Headline: {{text}}"},
    }
    compare = ActionCompareService(workspace)
    context = compare.context(
        "compare",
        {
            "source": {"kind": "draft", "draft": draft},
            "sample_row_ids": row_ids,
        },
    )
    changed = {**context["draft"], "idempotency_key": "compare-preview@source"}
    changed["params"] = {
        **changed["params"],
        "source": {"text": "Body: {{text}}"},
    }
    response = ActionPreviewRunService(workspace).start_compare_preview(
        "compare",
        {
            "action": changed,
            "comparison": {
                "basis": context["comparison_basis"],
                "expected_output_signature": context["expected_output_signature"],
            },
        },
    )
    assert response.status_code == 409
    assert response.payload["error"]["code"] == "comparison_source_mismatch"
    assert (
        workspace.get("compare")
        .db.execute("SELECT COUNT(*) FROM receipts")
        .fetchone()[0]
        == 0
    )


def test_context_bounds_aggregate_inline_source_values(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    workspace.create("Compare", project_id="compare")
    project = workspace.get("compare")
    sheet_id = project.add_sheet("Wide")
    names = [f"source_{index}" for index in range(4)]
    column_ids = {
        name: project.add_column(sheet_id, name, type="text") for name in names
    }
    value = "x" * 60_000
    row_ids = project.add_rows(
        sheet_id,
        [{name: value for name in names} for _ in range(5)],
        column_ids,
    )
    draft = _ask_draft(sheet_id, row_ids)
    draft["params"] = {**draft["params"], "source": names}

    payload = ActionCompareService(workspace).context(
        "compare",
        {
            "source": {"kind": "draft", "draft": draft},
            "sample_row_ids": row_ids,
        },
    )

    assert len(json.dumps(payload).encode("utf-8")) < 1024 * 1024
    assert any(
        cell.get("bounded_value") is not None
        for row in payload["source_rows"].values()
        for cell in row.values()
    )


def test_row_preview_spools_large_unicode_and_json_once(tmp_path: Path) -> None:
    text = "snowman ☃ " * 10_000
    structured = {"records": [{"name": "Альфа", "text": text}]}
    result = PreviewResult(
        sheet_id=1,
        columns=[
            PreviewColumn("text", "text"),
            PreviewColumn("structured", "json"),
        ],
        values={
            1: {
                "text": {"value": text},
                "structured": {"value": structured},
            }
        },
        row_ids=[1],
        sampled=1,
        total=1,
    )

    result.finalize_artifacts()

    assert len(result.artifacts) == 2
    assert result.values[1]["text"]["value"] is None
    text_artifact = result.values[1]["text"]["bounded_value"]
    json_artifact = result.values[1]["structured"]["bounded_value"]
    assert text_artifact.path.read_text() == text
    assert json_artifact.path.read_text().startswith('{"records":[')
    paths = [artifact.path for artifact in result.artifacts.values()]
    result.finalize_artifacts()
    assert [artifact.path for artifact in result.artifacts.values()] == paths
    result.close()
    assert all(not path.exists() for path in paths)


def test_comparison_context_accepts_500_rows_and_reports_limits(tmp_path: Path) -> None:
    workspace, sheet_id, _ = _workspace(tmp_path / "workspace")
    project = workspace.get("compare")
    source_id = next(
        column["id"] for column in project.columns(sheet_id) if column["name"] == "text"
    )
    rows = project.add_rows(
        sheet_id, [{"text": str(i)} for i in range(500)], {"text": source_id}
    )
    context = ActionCompareService(workspace).context(
        "compare",
        {
            "source": {"kind": "draft", "draft": _ask_draft(sheet_id, rows)},
            "sample_row_ids": rows,
        },
    )
    assert context["limits"] == {"max_sample_rows": 500, "max_challengers": 19}
    assert len(context["source_rows"]) == 500
    with pytest.raises(ValueError):
        ActionCompareService(workspace).context(
            "compare",
            {
                "source": {"kind": "draft", "draft": _ask_draft(sheet_id, rows)},
                "sample_row_ids": rows + [rows[0]],
            },
        )


def test_comparison_preview_captures_full_evaluation_metadata(tmp_path: Path) -> None:
    import time

    app = create_app(
        tmp_path / "adapter-workspace",
        router=_router(),
        serve_spa=False,
        enable_provider_config=False,
    )
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={"name": "Adapter"}).json()["id"]
        workspace = app.state.workspace
        project = workspace.get(pid)
        sheet_id = project.add_sheet("Stories")
        source_id = project.add_column(sheet_id, "text", type="text")
        rows = project.add_rows(sheet_id, [{"text": "Alpha"}], {"text": source_id})
        context = ActionCompareService(workspace).context(
            pid,
            {
                "source": {"kind": "draft", "draft": _ask_draft(sheet_id, rows)},
                "sample_row_ids": rows,
            },
        )
        service = ActionPreviewRunService(workspace)
        body = {
            "action": {**context["draft"], "idempotency_key": "eval-adapter"},
            "comparison": {
                "basis": context["comparison_basis"],
                "expected_output_signature": context["expected_output_signature"],
            },
        }
        started = service.start_compare_preview(pid, body)
        if started.status_code == 402:
            body["action"]["confirmation"] = started.payload["error"]["details"][
                "promise_set_hash"
            ]
            started = service.start_compare_preview(pid, body)
        assert started.status_code == 202, started.payload
        preview_id = started.payload["preview_id"]
        for _ in range(5000):
            status = service.get_preview(pid, preview_id).payload["status"]
            if status != "running":
                break
            time.sleep(0.002)
        result = service.evaluation_preview(pid, preview_id)
        assert result["sheet_id"] == sheet_id
        assert result["action_id"] == "map.ask"
        assert result["output_columns"][0]["logical_key"] == "answer"
        assert result["rows"][str(rows[0])]["answer"]["state"] in {
            "value",
            "error",
            "null",
        }
        assert result["source_fingerprints"][str(rows[0])]
        assert result["settings"]["params"] == body["action"]["params"]
        service._registry.shutdown()


def test_preview_input_change_during_execution_invalidates_identity(
    tmp_path: Path,
) -> None:
    import threading
    import asyncio
    import time
    from frisket.server.services.evaluation_values import source_fingerprints

    entered, resume = threading.Event(), threading.Event()

    class WaitingAdapter(_ClassifyAdapter):
        async def complete(self, request, client):
            entered.set()
            await asyncio.to_thread(resume.wait, 10)
            return await super().complete(request, client)

    app = create_app(
        tmp_path / "change-workspace",
        router=_router(WaitingAdapter()),
        serve_spa=False,
        enable_provider_config=False,
    )
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={"name": "Change"}).json()["id"]
        workspace = app.state.workspace
        project = workspace.get(pid)
        sheet_id = project.add_sheet("Stories")
        source_id = project.add_column(sheet_id, "text", type="text")
        rows = project.add_rows(sheet_id, [{"text": "Alpha"}], {"text": source_id})
        before = source_fingerprints(project, sheet_id, [source_id], rows)[str(rows[0])]
        context = ActionCompareService(workspace).context(
            pid,
            {
                "source": {"kind": "draft", "draft": _ask_draft(sheet_id, rows)},
                "sample_row_ids": rows,
            },
        )
        service = ActionPreviewRunService(workspace)
        body = {
            "action": {**context["draft"], "idempotency_key": "eval-change"},
            "comparison": {
                "basis": context["comparison_basis"],
                "expected_output_signature": context["expected_output_signature"],
            },
        }
        started = service.start_compare_preview(pid, body)
        if started.status_code == 402:
            body["action"]["confirmation"] = started.payload["error"]["details"][
                "promise_set_hash"
            ]
            started = service.start_compare_preview(pid, body)
        assert started.status_code == 202, started.payload
        try:
            assert entered.wait(5)
            edit = post_cell_edit_as_v1_action(
                client,
                pid,
                [{"row_id": rows[0], "column_id": source_id, "value": "Changed"}],
                idempotency_key="eval-source-change",
            )
            assert edit.status_code == 200, edit.text
        finally:
            resume.set()
        preview_id = started.payload["preview_id"]
        for _ in range(5000):
            if service.get_preview(pid, preview_id).payload["status"] != "running":
                break
            time.sleep(0.002)
        fingerprint = service.evaluation_preview(pid, preview_id)[
            "source_fingerprints"
        ][str(rows[0])]
        assert fingerprint != before
        assert (
            fingerprint
            != source_fingerprints(project, sheet_id, [source_id], rows)[str(rows[0])]
        )
        service._registry.shutdown()


def test_compare_quote_is_write_free_and_its_confirmation_starts_same_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    app = create_app(
        tmp_path / "quote-workspace",
        router=_router(),
        serve_spa=False,
        enable_provider_config=False,
    )
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={"name": "Quote"}).json()["id"]
        workspace = app.state.workspace
        project = workspace.get(pid)
        sheet_id = project.add_sheet("Stories")
        source = project.add_column(sheet_id, "text", type="text")
        rows = project.add_rows(sheet_id, [{"text": "Alpha"}], {"text": source})
        context = ActionCompareService(workspace).context(
            pid,
            {
                "source": {"kind": "draft", "draft": _ask_draft(sheet_id, rows)},
                "sample_row_ids": rows,
            },
        )
        body = {
            "action": {**context["draft"], "idempotency_key": "eval-quote"},
            "comparison": {
                "basis": context["comparison_basis"],
                "expected_output_signature": context["expected_output_signature"],
            },
        }
        service = ActionPreviewRunService(workspace)
        quoted = service.quote_compare_preview(pid, body)
        assert quoted.status_code == 200, quoted.payload
        assert not service._registry._jobs
        wire_quote = client.post(
            f"/api/projects/{pid}/actions/v1/compare-preview-quote", json=body
        )
        assert wire_quote.status_code == 200, wire_quote.text
        assert wire_quote.json()["estimate"] == quoted.payload["estimate"]
        estimate = quoted.payload["estimate"]
        assert estimate["rows"] == 1
        assert "billed_cost" in estimate and estimate["policy_id"]
        assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
        assert estimate["requires_confirmation"]
        if estimate["requires_confirmation"]:
            assert estimate["promise_set_hash"]
            stale = copy.deepcopy(body)
            stale["action"]["params"]["question"] = (
                "A different question with different words"
            )
            stale["action"]["confirmation"] = estimate["promise_set_hash"]
            assert service.start_compare_preview(pid, stale).status_code == 402
            assert not service._registry._jobs
            body["action"]["confirmation"] = estimate["promise_set_hash"]
        started = service.start_compare_preview(pid, body)
        assert started.status_code == 202, started.payload
        service._registry.shutdown()
