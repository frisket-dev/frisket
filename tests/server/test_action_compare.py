from __future__ import annotations

from pathlib import Path
import json
from typing import Any

from fastapi.testclient import TestClient

from frisket.actions.registry import ACTION_REGISTRY
from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.engine.runner.preview import PreviewColumn, PreviewResult
from frisket.server.app import create_app
from frisket.server.services.action_compare import ActionCompareService
from frisket.server.services.action_preview_runs import ActionPreviewRunService
from frisket.server.workspace import Workspace
from http_test_helpers import drain_queue, post_v1_action_with_exact_confirmation


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


def _router() -> ModelRouter:
    router = ModelRouter(keys={"anthropic": "test"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = _ClassifyAdapter()  # noqa: SLF001
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


def test_catalog_has_one_typed_row_overlay_opt_in_for_initial_actions() -> None:
    eligible = {
        entry.action_id
        for entry in ACTION_REGISTRY.actions
        if entry.catalog_entry()["ui_hints"].get("comparison")
        == {"kind": "row_overlay"}
    }
    assert eligible == {"map.extract", "map.ask", "map.classify"}


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
    assert payload["locked_params"] == ["source"]
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


def test_compare_preview_revalidates_fingerprint_before_start(tmp_path: Path) -> None:
    workspace, sheet_id, row_ids = _workspace(tmp_path / "workspace")
    compare = ActionCompareService(workspace)
    context = compare.context(
        "compare",
        {
            "source": {"kind": "draft", "draft": _ask_draft(sheet_id, row_ids)},
            "sample_row_ids": row_ids,
        },
    )
    starts = 0
    preview = ActionPreviewRunService(workspace)
    original_start = preview._registry.start

    def counted_start(*args, **kwargs):
        nonlocal starts
        starts += 1
        return original_start(*args, **kwargs)

    preview._registry.start = counted_start  # type: ignore[method-assign]
    response = preview.start_compare_preview(
        "compare",
        {
            "action": {
                **context["draft"],
                "idempotency_key": "compare-preview@1",
            },
            "comparison": {
                "basis": context["comparison_basis"],
                "expected_output_signature": context["expected_output_signature"],
                "expected_sample_fingerprint": "stale",
            },
        },
    )
    assert response.status_code == 409
    assert response.payload["error"]["code"] == "comparison_stale"
    assert starts == 0


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
                "expected_sample_fingerprint": context["sample_fingerprint"],
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
                "expected_sample_fingerprint": context["sample_fingerprint"],
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
                "expected_sample_fingerprint": context["sample_fingerprint"],
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
                "expected_sample_fingerprint": context["sample_fingerprint"],
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
