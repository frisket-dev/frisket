from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from frisket.actions.model_rows import FILE_SOURCE_CONVERSION_HINT
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import typed_action_for_request
from frisket.actions.types import ColumnRef
from frisket.ai.llm import LLMResponse, ModelRouter
from frisket.engine.executor import run_action_spec
from frisket.engine.executor.map_rows_action import (
    _typed_map_rows_plan,
    build_typed_map_rows_plan,
    typed_program_from_runner_spec,
    validate_typed_project_references,
)
from frisket.engine.store import Project
from frisket.server.app import create_app


class RecordingAdapter:
    def __init__(self):
        self.requests = []

    async def complete(self, request, client):
        self.requests.append(request)
        name = next(iter(request.schema["properties"]))
        data = {name: "Document content"}
        return LLMResponse(
            content=json.dumps(data),
            data=data,
            model=request.model,
            tokens_in=10,
            tokens_out=3,
            cost=0.001,
        )


def router_and_adapter():
    adapter = RecordingAdapter()
    router = ModelRouter(
        keys={"openai": "fixture"},
        use_env_keys=False,
        cache=None,
        cache_mode="off",
        max_retries=0,
    )
    router._adapters["openai"] = adapter
    return router, adapter


def request(kind, sheet_id, source):
    return {
        "action_id": kind,
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": source,
            "model": "openai/gpt-5-mini",
            **(
                {"question": "What does this say?"}
                if kind == "map.ask"
                else {"engine": "llm", "labels": ["PERSON"]}
                if kind == "map.ner"
                else {"engine": "llm", "target_language": "French"}
                if kind == "map.translate"
                else {}
            ),
        },
        "idempotency_key": "file-source-test",
    }


@pytest.mark.parametrize(
    "kind", ["map.ask", "map.summarize", "map.translate", "map.ner"]
)
@pytest.mark.parametrize("source", [["document"], {"text": "Read {{document}}"}])
def test_file_sources_refuse_at_http_and_worker_reconstruction_before_provider(
    tmp_path, kind, source
):
    router, adapter = router_and_adapter()
    with TestClient(create_app(tmp_path, router=router)) as client:
        project_id = client.post("/api/projects", json={"name": "File input"}).json()[
            "id"
        ]
        project = client.app.state.workspace.get(project_id)
        sheet_id = project.add_sheet("Rows")
        column_id = project.add_column(sheet_id, "document", type="file")
        digest = project.add_blob(
            b"File content", filename="note.txt", mime="text/plain"
        )
        project.add_rows(
            sheet_id,
            [{"document": {"blob": digest, "mime": "text/plain"}}],
            {"document": column_id},
        )
        body = request(kind, sheet_id, source)
        response = client.post(f"/api/projects/{project_id}/actions/v1/run", json=body)
        assert response.status_code == 400, response.text
        result = response.json()
        assert result["errors"][0]["code"] == "invalid_input_ref"
        assert result["errors"][0]["message"] == FILE_SOURCE_CONVERSION_HINT
        assert result["run_id"] is None
        assert adapter.requests == []
        assert len(project.columns(sheet_id)) == 1
        # A durable pre-cutover spec cannot bypass live reference validation.
        spec = _typed_map_rows_plan(typed_action_for_request(body)).spec_dict()
        with pytest.raises(ValueError, match="To markdown or OCR"):
            typed_program_from_runner_spec(project, spec)
        assert adapter.requests == []


@pytest.mark.parametrize("kind", ["map.ask", "map.summarize"])
def test_model_sources_still_send_text_pixels_and_ordinary_json(
    tmp_path, kind, monkeypatch
):
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    router, adapter = router_and_adapter()
    project = Project.create(tmp_path / "inputs.frisket")
    try:
        sheet_id = project.add_sheet("Rows")
        columns = {
            name: project.add_column(sheet_id, name, type=type_)
            for name, type_ in [
                ("body", "text"),
                ("picture", "image"),
                ("metadata", "json"),
            ]
        }
        pixels = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
        )
        digest = project.add_blob(pixels, mime="image/png", filename="pixel.png")
        project.add_rows(
            sheet_id,
            [
                {
                    "body": "Actual document text",
                    "picture": {"blob": digest, "mime": "image/png"},
                    "metadata": {"blob": "an ordinary metadata identifier"},
                }
            ],
            columns,
        )
        body = request(kind, sheet_id, list(columns))
        quote = run_action_spec(project, body, project_id="test", router=router)
        assert quote.status == "needs_confirmation", quote.errors
        assert adapter.requests == []
        body["confirmation"] = quote.errors[0].details["promise_set_hash"]
        result = run_action_spec(project, body, project_id="test", router=router)
        assert result.status == "completed", result.errors
        assert len(adapter.requests) == 1
        parts = adapter.requests[0].messages[1]["content"]
        assert any(
            part["type"] == "text"
            and "Actual document text" in part["text"]
            and "ordinary metadata identifier" in part["text"]
            for part in parts
        )
        image = next(part for part in parts if part["type"] == "image")
        assert base64.b64decode(image["data"]) == pixels
    finally:
        project.close()


@pytest.mark.parametrize(
    "kind",
    [
        "map.ask",
        "map.summarize",
        "map.classify",
        "map.extract",
        "map.judge",
        "research.answer",
        "map.mcp_extract",
        "map.translate",
    ],
)
def test_shared_model_catalog_requires_explicit_file_conversion(kind):
    entry = ACTION_REGISTRY.get(kind).catalog_entry()
    source = next(
        item
        for item in entry["ui_hints"]["source_requirements"]
        if item["param"] == "source"
    )
    assert "file" not in source["accepted_column_types"]
    assert {"text", "image", "json", "number", "boolean"} <= set(
        source["accepted_column_types"]
    )
    assert (
        entry["input_schema"]["properties"]["source"]["description"]
        == FILE_SOURCE_CONVERSION_HINT
    )


@pytest.mark.parametrize("kind", ["media.to_markdown", "media.ocr", "media.transcribe"])
def test_document_readers_keep_file_support(kind):
    entry = ACTION_REGISTRY.get(kind).catalog_entry()
    assert any(
        "file" in source.get("accepted_column_types", [])
        for source in entry["ui_hints"]["source_requirements"]
    )


def test_nontext_reference_keeps_incompatible_type_diagnostic(tmp_path):
    class NumericParams(BaseModel):
        source: ColumnRef[int]

    project = Project.create(tmp_path / "numeric.frisket")
    try:
        sheet_id = project.add_sheet("Rows")
        project.add_column(sheet_id, "document", type="file")
        with pytest.raises(ValueError, match="incompatible types"):
            validate_typed_project_references(
                project, sheet_id, NumericParams.model_validate({"source": "document"})
            )
    finally:
        project.close()


@pytest.mark.parametrize("type_", ["image", "link", "json", "text"])
def test_translation_templates_keep_supported_rich_references(tmp_path, type_):
    project = Project.create(tmp_path / "translation.frisket")
    try:
        sheet_id = project.add_sheet("Rows")
        project.add_column(sheet_id, "content", type=type_)
        body = request("map.translate", sheet_id, {"text": "Read {{content}}"})
        spec = build_typed_map_rows_plan(
            project, typed_action_for_request(body)
        ).spec_dict()
        assert typed_program_from_runner_spec(project, spec) is not None
    finally:
        project.close()


def test_group_summary_file_source_refuses_before_provider(tmp_path):
    project = Project.create(tmp_path / "group-file.frisket")
    router, adapter = router_and_adapter()
    try:
        sheet_id = project.add_sheet("Rows")
        column_id = project.add_column(sheet_id, "document", type="file")
        digest = project.add_blob(b"Actual document text", mime="text/plain")
        project.add_rows(
            sheet_id, [{"document": {"blob": digest}}], {"document": column_id}
        )
        body = request("reduce.group_summary", sheet_id, ["document"])
        body["sheet_name"] = "Summary"
        body["params"]["instruction"] = "Summarize the document"
        result = run_action_spec(project, body, project_id="test", router=router)
        assert result.status == "failed", result
        assert result.errors[0].code == "invalid_input_ref"
        assert result.errors[0].message == FILE_SOURCE_CONVERSION_HINT
        assert result.run_id is None
        assert adapter.requests == []
        assert len(project.sheets()) == 1
    finally:
        project.close()


def test_file_excluding_refs_keep_late_plugin_types_and_file_group_keys(tmp_path):
    from frisket.actions.group_summary import GroupSummaryParams
    from frisket.actions.ner import NerParams
    from frisket.actions.types import discover_references
    from frisket.authoring.column_types import (
        register_column_type,
        unregister_column_type,
    )
    from frisket.engine.executor.group_summary_action import (
        prepare_group_summary_action,
    )

    custom_type = "file_policy_test_custom"
    register_column_type(custom_type, plugin="file-policy-test")
    project = Project.create(tmp_path / "custom.frisket")
    try:
        sheet_id = project.add_sheet("Rows")
        columns = {
            "content": project.add_column(sheet_id, "content", type=custom_type),
            "group_key": project.add_column(sheet_id, "group_key", type="file"),
        }
        digest = project.add_blob(b"Grouping key only", mime="text/plain")
        project.add_rows(
            sheet_id,
            [{"content": {"value": "custom content"}, "group_key": {"blob": digest}}],
            columns,
        )
        ner = NerParams(
            source={"text": "{{content}}"},
            labels=["PERSON"],
            engine="llm",
            model="openai/gpt-5-mini",
        )
        group = GroupSummaryParams(
            source=["content"],
            group_by="group_key",
            instruction="Summarize",
            model="openai/gpt-5-mini",
        )
        for params in (ner, group):
            references = discover_references(params)
            content = next(ref for ref in references if ref.column == "content")
            assert custom_type in content.accepted_column_types
            assert "file" not in content.accepted_column_types
            validate_typed_project_references(project, sheet_id, params)
        for kind, key in (
            ("map.ner", "template_accepted_column_types"),
            ("reduce.group_summary", "accepted_column_types"),
        ):
            entry = ACTION_REGISTRY.get(kind).catalog_entry()
            source = next(
                item
                for item in entry["ui_hints"]["source_requirements"]
                if item["param"] == "source"
            )
            assert custom_type in source[key]
            assert "file" not in source[key]
        body = request("reduce.group_summary", sheet_id, ["content"])
        body["params"] = group.model_dump(mode="json")
        body["sheet_name"] = "Summary"
        prepared = prepare_group_summary_action(project, typed_action_for_request(body))
        assert prepared.operation.group_by == "group_key"
        assert prepared.operation.input_columns == ["content"]
    finally:
        project.close()
        unregister_column_type(custom_type)


def test_explicit_reference_types_are_intersected_with_exclusions():
    from typing import Any, ClassVar
    from frisket.actions.types import column_ref_types, template_ref_types, Template

    class SelectedColumn(ColumnRef[Any]):
        accepted_column_types: ClassVar[tuple[str, ...]] = ("text", "file")
        excluded_column_types: ClassVar[tuple[str, ...]] = ("file",)

    class SelectedTemplate(Template[Any]):
        accepted_column_types: ClassVar[tuple[str, ...]] = ("text", "file")
        excluded_column_types: ClassVar[tuple[str, ...]] = ("file",)

    assert column_ref_types(SelectedColumn) == ("text",)
    assert template_ref_types(SelectedTemplate) == ("text",)


def test_inferred_reference_types_are_intersected_with_exclusions(monkeypatch):
    from frisket.actions.types import (
        Template,
        TextLike,
        column_ref_types,
        template_ref_types,
    )

    numeric_column = ColumnRef[int]
    text_template = Template[TextLike]
    monkeypatch.setattr(
        numeric_column, "excluded_column_types", ("file",), raising=False
    )
    monkeypatch.setattr(
        text_template, "excluded_column_types", ("number",), raising=False
    )

    assert column_ref_types(numeric_column) == ("integer",)
    assert template_ref_types(text_template) == (
        "category",
        "date",
        "integer",
        "link",
        "text",
    )
