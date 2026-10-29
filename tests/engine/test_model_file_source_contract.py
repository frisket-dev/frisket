from __future__ import annotations

import base64
import json
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from pypdf import PdfWriter

from http_test_helpers import drain_queue, post_v1_action_with_exact_confirmation

from frisket.actions.model_rows import FILE_SOURCE_CONVERSION_HINT
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import typed_action_for_request
from frisket.actions.types import ColumnRef, RowError
from frisket.ai.llm import LLMResponse, ModelRouter
from frisket.engine.executor import run_action_spec
from frisket.engine.executor.map_rows_action import (
    _typed_map_rows_plan,
    build_typed_map_rows_plan,
    typed_program_from_runner_spec,
    validate_typed_project_references,
)
from frisket.engine.store import Project
from frisket.engine.runner.row_inputs import row_values
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


@pytest.mark.parametrize("kind", ["map.translate", "map.ner"])
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
@pytest.mark.parametrize("source", [["document"], {"text": "Read {{document}}"}])
def test_pdf_sources_survive_http_and_worker_reconstruction_and_publish_output(
    tmp_path, kind, source
):
    stream = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=200)
    writer.write(stream)
    pdf = stream.getvalue()
    router, adapter = router_and_adapter()
    with TestClient(create_app(tmp_path, router=router)) as client:
        project_id = client.post("/api/projects", json={"name": "PDF input"}).json()[
            "id"
        ]
        project = client.app.state.workspace.get(project_id)
        sheet_id = project.add_sheet("Rows")
        column_id = project.add_column(sheet_id, "document", type="file")
        digest = project.add_blob(pdf, filename="note.pdf", mime="application/pdf")
        rows = project.add_rows(
            sheet_id,
            [
                {
                    "document": {
                        "blob": digest,
                        "filename": "note.pdf",
                        "mime": "application/pdf",
                    }
                }
            ],
            {"document": column_id},
        )
        body = request(kind, sheet_id, source)
        spec = build_typed_map_rows_plan(
            project, typed_action_for_request(body)
        ).spec_dict()
        worker = typed_program_from_runner_spec(project, spec)
        assert worker is not None
        values = row_values(project, worker, spec, {"document": column_id}, rows[0])
        call = worker.render(values, spec)
        parts = call.messages[1]["content"]
        document = next(part for part in parts if part["type"] == "document")
        assert document["media_type"] == "application/pdf"
        assert base64.b64decode(document["data"]) == pdf
        assert not any(part["type"] == "image" for part in parts)
        assert adapter.requests == []
        response = post_v1_action_with_exact_confirmation(client, project_id, body)
        assert response.status_code == 200, response.text
        queued = response.json()
        assert queued["status"] == "queued"
        drain_queue(client)
        receipt = client.get(
            f"/api/projects/{project_id}/actions/v1/receipts/{queued['receipt_id']}"
        ).json()
        assert receipt["status"] == "completed", receipt["errors"]
        assert len(adapter.requests) == 1
        sent = next(
            part
            for part in adapter.requests[0].messages[1]["content"]
            if part["type"] == "document"
        )
        assert base64.b64decode(sent["data"]) == pdf
        output_name = "answer" if kind == "map.ask" else "summary"
        output = next(
            column
            for column in project.columns(sheet_id)
            if column["name"] == output_name
        )
        assert project.get_values(sheet_id, output["id"], row_ids=rows) == {
            rows[0]: "Document content"
        }


@pytest.mark.parametrize("kind", ["map.ask", "map.summarize"])
@pytest.mark.parametrize("source", [["document"], {"text": "Read {{document}}"}])
def test_non_pdf_file_rows_refuse_without_sending_metadata_to_model(
    tmp_path, kind, source
):
    project = Project.create(tmp_path / "non-pdf.frisket")
    router, adapter = router_and_adapter()
    try:
        sheet_id = project.add_sheet("Rows")
        column_id = project.add_column(sheet_id, "document", type="file")
        digest = project.add_blob(
            b"File content", filename="note.txt", mime="text/plain"
        )
        rows = project.add_rows(
            sheet_id,
            [
                {
                    "document": {
                        "blob": digest,
                        "filename": "note.txt",
                        "mime": "text/plain",
                    }
                }
            ],
            {"document": column_id},
        )
        bound = typed_action_for_request(request(kind, sheet_id, source))
        # Form/project-reference validation is metadata-only. File-content
        # failures remain expected row errors at the actual resolution boundary.
        plan = build_typed_map_rows_plan(project, bound)
        with pytest.raises(RowError, match="not a PDF") as error:
            row_values(
                project,
                plan.program,
                plan.spec_dict(),
                {"document": column_id},
                rows[0],
            )
        assert error.value.code == "pdf_input_invalid"
        assert adapter.requests == []
        assert len(project.columns(sheet_id)) == 1
        assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    finally:
        project.close()


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
def test_shared_model_catalog_limits_pdf_support_to_three_model_actions(kind):
    entry = ACTION_REGISTRY.get(kind).catalog_entry()
    source = next(
        item
        for item in entry["ui_hints"]["source_requirements"]
        if item["param"] == "source"
    )
    pdf_supported = kind in {"map.ask", "map.summarize", "map.extract"}
    assert ("file" in source["accepted_column_types"]) is pdf_supported
    assert {"text", "image", "json", "number", "boolean"} <= set(
        source["accepted_column_types"]
    )
    description = entry["input_schema"]["properties"]["source"]["description"]
    if pdf_supported:
        assert "PDF files" in description
        assert "Other files need To markdown or OCR first" in description
    else:
        assert description == FILE_SOURCE_CONVERSION_HINT


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


@pytest.mark.parametrize("kind", ["map.ask", "map.summarize"])
@pytest.mark.parametrize("endpoint", ["run", "preview", "estimate"])
@pytest.mark.parametrize(
    "filename,mime,raw",
    [
        ("note.txt", "text/plain", b"File content"),
        ("broken.pdf", "application/pdf", b"%PDF-1.4 invalid document bytes"),
    ],
)
def test_invalid_pdf_preflight_returns_structured_http_error_without_provider(
    tmp_path, kind, endpoint, filename, mime, raw
):
    router, adapter = router_and_adapter()
    with TestClient(create_app(tmp_path, router=router)) as client:
        project_id = client.post("/api/projects", json={"name": "Invalid PDF"}).json()[
            "id"
        ]
        project = client.app.state.workspace.get(project_id)
        sheet_id = project.add_sheet("Rows")
        column_id = project.add_column(sheet_id, "document", type="file")
        digest = project.add_blob(raw, filename=filename, mime=mime)
        rows = project.add_rows(
            sheet_id,
            [{"document": {"blob": digest, "filename": filename, "mime": mime}}],
            {"document": column_id},
        )
        body = request(kind, sheet_id, ["document"])
        body["scope"]["row_ids"] = rows
        response = client.post(
            f"/api/projects/{project_id}/actions/v1/{endpoint}",
            json={"action": body} if endpoint == "estimate" else body,
        )
        assert response.status_code == 400, response.text
        payload = response.json()
        error = (
            payload["errors"][0]
            if endpoint == "run"
            else payload["error"]
            if endpoint == "preview"
            else payload["detail"]
        )
        assert error["code"] == "pdf_input_invalid"
        assert error["field"] == "params.source"
        assert error["message"]
        assert adapter.requests == []
        assert len(project.columns(sheet_id)) == 1
        assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_pdf_row_aggregate_page_limit_is_a_structured_http_refusal(tmp_path):
    stream = BytesIO()
    writer = PdfWriter()
    for _ in range(51):
        writer.add_blank_page(width=100, height=200)
    writer.write(stream)
    router, adapter = router_and_adapter()
    with TestClient(create_app(tmp_path, router=router)) as client:
        project_id = client.post(
            "/api/projects", json={"name": "PDF aggregate limit"}
        ).json()["id"]
        project = client.app.state.workspace.get(project_id)
        sheet_id = project.add_sheet("Rows")
        columns = {
            name: project.add_column(sheet_id, name, type="file")
            for name in ("first", "second")
        }
        digest = project.add_blob(
            stream.getvalue(), filename="pages.pdf", mime="application/pdf"
        )
        cell = {"blob": digest, "filename": "pages.pdf", "mime": "application/pdf"}
        project.add_rows(sheet_id, [{name: cell for name in columns}], columns)
        response = client.post(
            f"/api/projects/{project_id}/actions/v1/run",
            json=request("map.ask", sheet_id, list(columns)),
        )
        assert response.status_code == 400, response.text
        error = response.json()["errors"][0]
        assert error["code"] == "pdf_input_too_large"
        assert error["field"] == "params.source"
        assert "across selected files" in error["message"]
        assert adapter.requests == []
        assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert len(project.columns(sheet_id)) == 2


def test_pdf_form_validation_does_not_materialize_files(tmp_path, monkeypatch):
    router, adapter = router_and_adapter()
    with TestClient(create_app(tmp_path, router=router)) as client:
        pid = client.post(
            "/api/projects", json={"name": "Passive PDF validation"}
        ).json()["id"]
        project = client.app.state.workspace.get(pid)
        sheet_id = project.add_sheet("Rows")
        column_id = project.add_column(sheet_id, "document", type="file")
        digest = project.add_blob(
            b"not a readable PDF", filename="broken.pdf", mime="application/pdf"
        )
        project.add_rows(
            sheet_id,
            [{"document": {"blob": digest, "mime": "application/pdf"}}],
            {"document": column_id},
        )

        def forbidden(*args, **kwargs):
            raise AssertionError("form validation must not read PDF bytes")

        monkeypatch.setattr(Project, "materialize_blob", forbidden)
        response = client.post(
            f"/api/projects/{pid}/actions/v1/validate-params",
            json={"action": request("map.ask", sheet_id, ["document"])},
        )
        assert response.status_code == 200, response.text
        assert response.json()["diagnostics"] == {}
        assert adapter.requests == []


def test_pdf_preview_only_resolves_its_bounded_sample(tmp_path, monkeypatch):
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    stream = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=200)
    writer.write(stream)
    router, adapter = router_and_adapter()
    with TestClient(create_app(tmp_path, router=router)) as client:
        pid = client.post("/api/projects", json={"name": "Bounded PDF preview"}).json()[
            "id"
        ]
        project = client.app.state.workspace.get(pid)
        sheet_id = project.add_sheet("Rows")
        column_id = project.add_column(sheet_id, "document", type="file")
        good = project.add_blob(
            stream.getvalue(), filename="good.pdf", mime="application/pdf"
        )
        bad = project.add_blob(
            b"not a readable PDF", filename="bad.pdf", mime="application/pdf"
        )
        project.add_rows(
            sheet_id,
            [
                {"document": {"blob": digest, "mime": "application/pdf"}}
                for digest in [good] * 20 + [bad]
            ],
            {"document": column_id},
        )
        original = Project.materialize_blob
        reads = []

        def bounded(project, digest, *args, **kwargs):
            assert digest != bad, "Preview must not inspect the unselected 21st PDF"
            reads.append(digest)
            return original(project, digest, *args, **kwargs)

        monkeypatch.setattr(Project, "materialize_blob", bounded)
        # No authored row_ids: the preview endpoint must choose its <=20-row
        # sample before any file bytes are resolved.
        response = client.post(
            f"/api/projects/{pid}/actions/v1/preview",
            json=request("map.ask", sheet_id, ["document"]),
        )
        assert response.status_code == 402, response.text
        assert response.json()["error"]["code"] == "cost_gate"
        assert reads and set(reads) == {good}
        assert adapter.requests == []
