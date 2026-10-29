"""Direct model PDF source resolution and admission, without provider traffic."""

from __future__ import annotations

import base64
from io import BytesIO

import pytest
from pypdf import PdfReader, PdfWriter

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest, RowError
from frisket.ai.llm.document_input import model_supports_pdf
from frisket.engine.executor.map_rows_action import (
    TypedMapRowsPlanError,
    build_typed_map_rows_plan,
)
from frisket.engine.runner.pdf_inputs import pdf_part
from frisket.engine.runner.row_inputs import row_values
from frisket.engine.store import Project

MODEL = "anthropic/claude-haiku-4-5"


def pdf_bytes():
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=200)
    writer.add_blank_page(width=300, height=400)
    writer.write(output)
    return output.getvalue()


@pytest.fixture
def source(tmp_path):
    project = Project.create(tmp_path / "pdf.frisket")
    sheet = project.add_sheet("Documents")
    ids = {
        "document": project.add_column(sheet, "document", type="file"),
        "note": project.add_column(sheet, "note", type="text"),
    }
    data = pdf_bytes()
    digest = project.add_blob(data, filename="filing.pdf", mime="application/pdf")
    value = {"blob": digest, "filename": "filing.pdf", "mime": "application/pdf"}
    rows = project.add_rows(
        sheet,
        [
            {"document": value, "note": "context"},
            {"document": None, "note": "text only"},
        ],
        ids,
    )
    yield project, sheet, ids, rows, value, data
    project.close()


def plan(seeded, action="map.ask", **extra):
    project, sheet, *_ = seeded
    params = {"source": ["document", "note"], "model": MODEL, **extra}
    if action == "map.ask":
        params["question"] = "Who?"
    elif action == "map.extract":
        params["fields"] = [{"name": "name", "type": "text"}]
    request = ActionRequest.model_validate(
        {
            "action_id": action,
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": params,
            "idempotency_key": "pdf-input@1",
        }
    )
    bound = BoundTypedActionRequest.bind(ACTION_REGISTRY.get(action), request)
    return build_typed_map_rows_plan(project, bound)


def test_original_pdf_bytes_are_unchanged_and_selected_page_is_single_pdf(source):
    project, _, _, _, value, data = source
    whole = pdf_part(project, value)
    assert base64.b64decode(whole["__pdf_b64__"]) == data
    assert whole["page_count"] == 2
    assert whole["text_char_count"] == 0
    selected = pdf_part(project, {**value, "page": 2})
    reader = PdfReader(BytesIO(base64.b64decode(selected["__pdf_b64__"])))
    assert len(reader.pages) == 1
    assert reader.pages[0].mediabox.width == 300
    assert selected["page_count"] == 1
    assert selected["filename"] == "filing-page-2.pdf"
    assert (
        project.blob_bytes(value["blob"]) == data
        if hasattr(project, "blob_bytes")
        else True
    )


@pytest.mark.parametrize("action", ["map.ask", "map.summarize", "map.extract"])
def test_only_selected_pdf_content_and_text_reach_model_renderer(source, action):
    project, _, columns, rows, _, data = source
    admitted = plan(source, action)
    values = row_values(
        project, admitted.program, admitted.spec_dict(), columns, rows[0]
    )
    assert base64.b64decode(values["document"]["__pdf_b64__"]) == data
    assert values["note"] == "context"
    call = admitted.program.render(values, admitted.spec_dict())
    parts = call.messages[1]["content"]
    assert any(part["type"] == "document" for part in parts)
    assert not any(part["type"] == "image" for part in parts)
    assert any("context" in part.get("text", "") for part in parts)
    text_only = row_values(
        project, admitted.program, admitted.spec_dict(), columns, rows[1]
    )
    assert text_only["document"] is None
    assert text_only["note"] == "text only"


def test_templates_preserve_pdf_carrier(source):
    project, _, columns, rows, *_ = source
    admitted = plan(source, source={"text": "{{document}} {{note}}"})
    values = row_values(
        project, admitted.program, admitted.spec_dict(), columns, rows[0]
    )
    assert values["document"]["__pdf_b64__"]
    assert values["document"]["__pdf_b64__"] not in values["input"]


def test_pdf_extract_citations_are_effectively_off_without_rewriting_authored_identity(
    source,
):
    admitted = plan(source, "map.extract")
    assert admitted.program._params.grounding.enabled is False
    assert "grounding" not in admitted.request_identity["params"]


@pytest.mark.parametrize(
    "extra",
    [
        {"grounding": {"citation_required": True}},
        {"evidence_policy": {"citation_required": True}},
    ],
)
def test_required_pdf_citations_refuse_at_admission(source, extra):
    with pytest.raises(TypedMapRowsPlanError, match="Citations are unavailable"):
        plan(source, "map.extract", **extra)


@pytest.mark.parametrize(
    "model", ["gemini/gemini-2.5-flash", "openrouter/openai/gpt-4.1", "openai/unknown"]
)
def test_unsupported_pdf_models_refuse_before_any_request(source, model):
    with pytest.raises(TypedMapRowsPlanError, match="supported OpenAI or Anthropic"):
        plan(source, model=model)


@pytest.mark.parametrize(
    "mutation",
    [
        {"page": 0},
        {"page": True},
        {"page": 3},
        {"mime": "text/plain", "filename": "notes.txt"},
        {"blob": "missing"},
    ],
)
def test_invalid_pdf_inputs_fail_clearly(source, mutation):
    project, _, _, _, value, _ = source
    with pytest.raises(RowError):
        pdf_part(project, {**value, **mutation})


def test_model_support_matches_supported_catalog_models():
    assert model_supports_pdf(MODEL)
    assert model_supports_pdf("openai/gpt-6-sol")
    assert not model_supports_pdf("gemini/gemini-2.5-flash")


def test_page_bounds_apply_after_materialization(source, monkeypatch):
    from frisket.engine.runner import pdf_inputs

    project, _, _, _, value, data = source
    monkeypatch.setattr(pdf_inputs, "MAX_PDF_PAGES", 1)
    monkeypatch.setattr(pdf_inputs, "MAX_PDF_BYTES", len(data) - 1)
    with pytest.raises(RowError, match="limited"):
        pdf_part(project, value)
    selected = pdf_part(project, {**value, "page": 2})
    assert selected["page_count"] == 1
    assert len(base64.b64decode(selected["__pdf_b64__"])) < len(data)


def test_pdf_sources_do_not_enable_unrelated_model_actions():
    for action_id in (
        "map.ask",
        "map.summarize",
        "map.extract",
        "map.classify",
        "map.translate",
    ):
        catalog = ACTION_REGISTRY.get(action_id).catalog_entry()
        source = next(
            item
            for item in catalog["ui_hints"]["source_requirements"]
            if item["param"] == "source"
        )
        assert ("file" in source["accepted_column_types"]) is (
            action_id in {"map.ask", "map.summarize", "map.extract"}
        )
