"""`map.ner` canonical scope and fresh-column lifecycle guards."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from frisket.ai.llm import ModelRouter
from typed_model_fixtures import model_request, model_plan
from frisket.engine.store import Project


PROJECT_ID = "project-ner-lifecycle"


class _SidecarResponse:
    status_code = 200
    text = "ok"

    def json(self) -> dict[str, Any]:
        return {
            "results": [
                [
                    {
                        "text": "Ada Lovelace",
                        "label": "person",
                        "start": 0,
                        "end": 12,
                        "score": 0.98,
                    }
                ]
            ]
        }


class _SidecarHttp:
    is_closed = False

    async def post(self, url: str, **kwargs: Any) -> _SidecarResponse:
        return _SidecarResponse()


def _router() -> ModelRouter:
    router = ModelRouter(cache=None, cache_mode="off")
    router._client = _SidecarHttp()  # noqa: SLF001
    return router


def _seed(project_path: Path) -> int:
    project = Project.create(project_path, name="NER lifecycle")
    try:
        sheet_id = project.add_sheet("Transcripts")
        body = project.add_column(sheet_id, "body", type="text")
        project.add_rows(
            sheet_id,
            [
                {"body": "Ada Lovelace wrote notes."},
                {"body": "Ada Lovelace met Babbage."},
            ],
            {"body": body},
        )
        return sheet_id
    finally:
        project.close()


def _ner_action(sheet_id: int, **param_overrides: Any) -> dict[str, Any]:
    return model_request(
        {
            "action_kind": "map.ner",
            "sheet_id": sheet_id,
            "input_columns": ["body"],
            "labels": ["person"],
            "engine": "gliner",
            "idempotency_key": "map_ner_lifecycle@sha256:stable",
            **param_overrides,
        }
    ).model_dump(mode="json", exclude_none=True)


def _backfill_action(sheet_id: int, column: str, **params: Any) -> dict[str, Any]:
    return {
        "action_id": "run.backfill",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, **params},
        "params": {"column": column},
        "idempotency_key": "run_backfill_ner@sha256:stable",
    }


def test_map_ner_resolver_keeps_canonical_runner_scope_authoritative(tmp_path):
    project = Project.create(tmp_path / "ner-resolve-scope.frisket")
    try:
        sheet = project.add_sheet("Transcripts")
        project.add_column(sheet, "body", type="text")
        plan = model_plan(
            {
                "action_kind": "map.ner",
                "sheet_id": sheet,
                "input_columns": ["body"],
                "labels": ["person"],
            },
            project,
        )
        assert plan.spec["sheet_id"] == sheet
        assert "row_ids" not in plan.spec
        assert "row_ids" not in plan.request.params
    finally:
        project.close()


# ---------- selected scope writes a fresh partial column ----------


def test_map_ner_exact_scope_runs_only_selected_rows_into_a_fresh_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.executor import actions as executor_actions

    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "secret")

    project_path = tmp_path / "ner-row-ids.frisket"
    sheet_id = _seed(project_path)
    project = Project(project_path)
    try:
        row_ids = project.visible_row_ids(sheet_id)
        result = executor_actions.run_action_spec(
            project,
            _ner_action(sheet_id, row_ids=[row_ids[0]]),
            project_id=PROJECT_ID,
            router=_router(),
        )
        assert result.status == "completed", result.errors
        entities = next(c for c in project.columns(sheet_id) if c["name"] == "entities")
        values = project.get_values(sheet_id, int(entities["id"]), row_ids=row_ids)
        assert values[row_ids[0]]
        assert values[row_ids[1]] is None
        run = project.db.execute(
            "SELECT params FROM runs WHERE id=?", (entities["current_run_id"],)
        ).fetchone()
        assert run is not None and f'"row_ids": [{row_ids[0]}]' in run["params"]
    finally:
        project.close()


# ---------- door 2: run.backfill creates a fresh entity generation ----------


def test_run_backfill_creates_a_fresh_map_ner_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Entity output families use the same explicitly scoped recovery path."""
    from frisket.engine.executor import actions as executor_actions

    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "secret")

    project_path = tmp_path / "ner-backfill.frisket"
    sheet_id = _seed(project_path)
    project = Project(project_path)
    try:
        ner = executor_actions.run_action_spec(
            project,
            _ner_action(sheet_id),
            project_id=PROJECT_ID,
            router=_router(),
        )
        assert ner.status == "completed", ner.errors

        # A new row arrives; backfill creates a successor generation for it.
        entities_col = next(
            c for c in project.columns(sheet_id) if c["name"] == "entities"
        )
        assert entities_col["ai_generated"] == 1
        body_col = next(c for c in project.columns(sheet_id) if c["name"] == "body")
        project.add_rows(
            sheet_id,
            [{"body": "Babbage wrote back."}],
            {"body": int(body_col["id"])},
        )

        result = executor_actions.run_action_spec(
            project,
            _backfill_action(sheet_id, "entities"),
            project_id=PROJECT_ID,
            router=_router(),
        )
        assert result.status == "completed", result.errors
        assert result.run_id != ner.run_id

        # An explicit retry is another fresh generation over exactly that row.
        retry_action = _backfill_action(sheet_id, "entities", row_ids=[1])
        retry_action["idempotency_key"] = "run_backfill_ner_retry@sha256:stable"
        retry = executor_actions.run_action_spec(
            project,
            retry_action,
            project_id=PROJECT_ID,
            router=_router(),
        )
        assert retry.status == "completed", retry.errors
        assert retry.run_id not in {ner.run_id, result.run_id}
    finally:
        project.close()


def test_backfill_guard_allows_canonical_map_ner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backfill guard reads the runner spec's sole canonical identity."""
    from frisket.engine.executor.action_families.runs import _guard_backfill_run_spec

    monkeypatch.setenv("FRISKET_ALLOW_CODE_RECIPES", "1")
    assert _guard_backfill_run_spec({"action_kind": "map.ner"}) is None
    assert _guard_backfill_run_spec({"action_kind": "map.classify"}) is None


# ---------- MCP uses the canonical action boundary ----------------


def test_mcp_local_backend_accepts_exact_scope_before_project_lookup(
    tmp_path: Path,
) -> None:
    import asyncio

    from frisket.server.mcp import LocalBackend

    backend = LocalBackend(tmp_path / "workspace")
    with pytest.raises(Exception) as missing_project:  # noqa: B017
        asyncio.run(
            backend.run_action(
                "project-never-opened",
                _ner_action(1, row_ids=[1, 2]),
            )
        )
    assert "row_scope" not in str(missing_project.value)
    assert "no project" in str(missing_project.value)
