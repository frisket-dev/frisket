"""map.extract null-string coercion + per-field `required` withholding.

Covers the live incident where the model emitted the STRING ``"null"`` for an
absent field (``{"head of state name": "null", ...}``): the 4-char text sat in
the grid indistinguishable from a real empty cell. The routing-boundary
coercion turns whole-value ``"null"``/``"NULL"``/``"None"`` into a real empty
cell, and a field marked ``required`` whose value is null/missing is WITHHELD
(terminal ``withheld_unverified`` + a ``required_field_missing`` action error),
never silently empty and never a row hard-failure.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from executor_harness import run_action_with_confirmation
from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.engine.store import Project
from frisket.actions.extract import ExtractParams
from frisket.actions.extraction_types import ExtractField
from frisket.ops.extraction import normalize_extracted_value

PROJECT_ID = "project-null-required"


class _StubAdapter:
    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        self.requests.append(req)
        return LLMResponse(
            content=json.dumps(self.reply),
            data=dict(self.reply),
            tokens_in=11,
            tokens_out=7,
            cost=0.001,
            model=req.model,
        )


def _stub_router(reply: dict[str, Any]) -> tuple[ModelRouter, _StubAdapter]:
    router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
    adapter = _StubAdapter(reply)
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    return router, adapter


def _seed(tmp_path: Path) -> tuple[Path, int]:
    project_path = tmp_path / "null-required.frisket"
    project = Project.create(project_path, name="null-required")
    try:
        sheet_id = project.add_sheet("Docs")
        snippet_id = project.add_column(sheet_id, "snippet", "text")
        project.add_rows(
            sheet_id,
            [{"snippet": "A short document with no head of state named."}],
            {"snippet": snippet_id},
        )
        return project_path, sheet_id
    finally:
        project.close()


def _action(
    sheet_id: int,
    *,
    key: str,
    fields: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "action_id": "map.extract",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["snippet"],
            "model": "anthropic/claude-haiku-4-5",
            "instruction": "Extract the requested fields.",
            "fields": fields,
        },
        "idempotency_key": key,
    }


def _column_result(
    project: Project, sheet_id: int, run_id: int, name: str
) -> dict[str, Any]:
    column = project.db.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name=?", (sheet_id, name)
    ).fetchone()
    row = project.db.execute(
        "SELECT value, outcome, error FROM results WHERE run_id=? AND column_id=?",
        (run_id, int(column["id"])),
    ).fetchone()
    return {
        "value": json.loads(row["value"]) if row["value"] is not None else None,
        "outcome": row["outcome"],
        "error": row["error"],
    }


# --- unit: the recipe-owned coercion pass -----------------------------------------
@pytest.mark.parametrize("token", ["null", "NULL", "None", "  null  ", "none"])
def test_postprocess_coerces_whole_value_null_tokens(token: str) -> None:
    assert normalize_extracted_value("who", token, {"type": "string"}) is None


@pytest.mark.parametrize(
    "value",
    ["nullish", "the value is null", "N/A", "nil", "n/a", "", "Ada"],
)
def test_postprocess_leaves_non_sentinel_strings(value: str) -> None:
    assert normalize_extracted_value("who", value, {"type": "string"}) == value


def test_postprocess_preserves_category_null_label() -> None:
    # Honest exception: a category enum may legitimately carry "null" as a
    # label -- then it is a real value, not stringified absence.
    schema = ExtractField(
        name="status", type="category", labels=["null", "ok"]
    ).value_schema()
    assert normalize_extracted_value("status", "null", schema) == "null"


def test_postprocess_ignores_non_strings() -> None:
    assert normalize_extracted_value("count", 0, {"type": "integer"}) == 0
    assert normalize_extracted_value("items", [], {"type": "array"}) == []


# --- schema round-trip ------------------------------------------------------------
def test_required_field_round_trips_through_params() -> None:
    assert ExtractField(name="x", type="text").required is False
    params = ExtractParams(
        source=["snippet"],
        model="anthropic/claude-haiku-4-5",
        fields=[
            {"name": "a", "type": "text", "required": True},
            {"name": "b", "type": "text"},
        ],
    )
    dumped = params.model_dump(mode="json")
    assert dumped["fields"][0]["required"] is True
    assert dumped["fields"][1]["required"] is False
    # round-trips back through validation
    again = ExtractParams.model_validate(dumped)
    assert [f.required for f in again.fields] == [True, False]


# --- executor: coercion at the routing boundary -----------------------------------
def test_null_string_becomes_genuinely_empty_cell(tmp_path: Path) -> None:
    project_path, sheet_id = _seed(tmp_path)
    action = _action(
        sheet_id,
        key="map_extract@sha256:null-coerce",
        fields=[
            {"name": "head_of_state_name", "type": "text"},
            {"name": "head_of_state_title", "type": "text"},
        ],
    )
    router, _adapter = _stub_router(
        {"head_of_state_name": "null", "head_of_state_title": "None"}
    )
    project = Project(project_path)
    try:
        result = run_action_with_confirmation(
            project,
            action,
            project_id=PROJECT_ID,
            router=router,
        )
        assert result.status == "completed", result.errors
        name = _column_result(project, sheet_id, result.run_id, "head_of_state_name")
        title = _column_result(project, sheet_id, result.run_id, "head_of_state_title")
        # Genuinely empty -- NOT the 4-char string "null".
        assert name["value"] is None
        assert title["value"] is None
        # Not required -> a plain empty cell, never withheld.
        assert name["outcome"] == "empty"
        assert title["outcome"] == "empty"
    finally:
        project.close()


# --- executor: required-missing withhold ------------------------------------------
def test_required_missing_field_is_withheld(tmp_path: Path) -> None:
    project_path, sheet_id = _seed(tmp_path)
    action = _action(
        sheet_id,
        key="map_extract@sha256:required-missing",
        fields=[
            {"name": "head_of_state_name", "type": "text", "required": True},
            {"name": "summary", "type": "text"},
        ],
    )
    # Required field comes back as the "null" string; optional field is real.
    router, _adapter = _stub_router(
        {"head_of_state_name": "null", "summary": "A short document."}
    )
    project = Project(project_path)
    try:
        result = run_action_with_confirmation(
            project,
            action,
            project_id=PROJECT_ID,
            router=router,
        )
        # Withholding is a policy hold, not a run failure.
        assert result.status == "completed", result.errors
        required = _column_result(
            project, sheet_id, result.run_id, "head_of_state_name"
        )
        summary = _column_result(project, sheet_id, result.run_id, "summary")
        assert required["value"] is None
        assert required["outcome"] == "withheld_unverified"
        assert required["error"]
        # The optional field is untouched.
        assert summary["value"] == "A short document."
        assert summary["outcome"] != "withheld_unverified"
        # A declared wire code surfaces on the action result.
        codes = {err.code for err in result.errors}
        assert "required_field_missing" in codes
    finally:
        project.close()


def test_required_field_found_is_not_withheld(tmp_path: Path) -> None:
    project_path, sheet_id = _seed(tmp_path)
    action = _action(
        sheet_id,
        key="map_extract@sha256:required-found",
        fields=[{"name": "head_of_state_name", "type": "text", "required": True}],
    )
    router, _adapter = _stub_router({"head_of_state_name": "Ada Lovelace"})
    project = Project(project_path)
    try:
        result = run_action_with_confirmation(
            project,
            action,
            project_id=PROJECT_ID,
            router=router,
        )
        assert result.status == "completed", result.errors
        cell = _column_result(project, sheet_id, result.run_id, "head_of_state_name")
        assert cell["value"] == "Ada Lovelace"
        assert cell["outcome"] != "withheld_unverified"
        assert "required_field_missing" not in {err.code for err in result.errors}
    finally:
        project.close()
