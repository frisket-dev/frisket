"""Direct PDF evidence uses captured file/geometry authority, never live provider calls."""

from __future__ import annotations

import json

import pytest

from executor_harness import run_action_with_confirmation
from tests.engine.test_direct_pdf_inputs import MODEL, plan, source as source
from frisket.ai.llm import LLMResponse, ModelRouter
from frisket.engine.runner.row_inputs import row_values
from frisket.engine.store.cell_writes import EditCellWrite, insert_edits
from frisket.engine.store.citations import (
    record_source_artifact,
    resolve_citation_viewer,
)
from frisket.engine.store.document_text import ensure_document_text_role
from frisket.engine.store.prepared_content import (
    PreparedContentStore,
    PreparedPageDraft,
)
from frisket.engine.store.transactions import write_transaction
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY


def _prepared(source, text="Contract amount: $42", *, positioned=True):
    project, sheet, columns, rows, value, _data = source
    text_column = next(
        (c["id"] for c in project.columns(sheet) if c["name"] == "prepared"), None
    )
    if text_column is None:
        text_column = project.add_column(sheet, "prepared", type="text")
    with write_transaction(project.db):
        ensure_document_text_role(
            project, source_column_id=columns["document"], text_column_id=text_column
        )
        change = project.append_change_in_transaction("media.ocr")
        artifact = record_source_artifact(
            project,
            artifact_kind="document",
            media_type="application/pdf",
            blob_hash=value["blob"],
            page_count=2,
            source_sheet_id=sheet,
            source_row_id=rows[0],
            source_column_id=columns["document"],
        )
        ref = PreparedContentStore(project).stage_reference(
            source_artifact_id=artifact["id"],
            producing_change_id=change,
            pages=[
                PreparedPageDraft(1, "Cover"),
                PreparedPageDraft(
                    2,
                    text,
                    {
                        "engine": "native_pdf_text",
                        "blocks": [
                            {
                                "text": word,
                                "bbox": {
                                    "space": "page_normalized",
                                    "x0": 0.1 + index * 0.15,
                                    "y0": 0.2,
                                    "x1": 0.24 + index * 0.15,
                                    "y1": 0.3,
                                },
                            }
                            for index, word in enumerate(text.split())
                        ],
                    }
                    if positioned
                    else None,
                ),
            ],
        )
        insert_edits(
            project.db,
            change_id=change,
            edits=[EditCellWrite(rows[0], text_column, ref)],
        )
    return ref.ref_id


def _run(source, evidence, *, before_reply=None, inputs=None):
    project, sheet, _columns, rows, _value, _data = source
    requests = []

    class Adapter:
        async def complete(self, request, client):
            del client
            requests.append(request)
            if before_reply:
                before_reply()
            return LLMResponse(
                content=None,
                data={"amount": {"value": "$42", "evidence": [evidence]}},
                tokens_in=10,
                tokens_out=10,
                cost=0.001,
                model=request.model,
            )

    router = ModelRouter(
        model_call_policy=LOCAL_PURCHASE_POLICY,
        keys={"anthropic": "stub"},
        cache=None,
        cache_mode="off",
    )
    router._adapters["anthropic"] = Adapter()
    result = run_action_with_confirmation(
        project,
        {
            "action_id": "map.extract",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet, "row_ids": [rows[0]]},
            "params": {
                "source": inputs or ["document"],
                "model": MODEL,
                "fields": [{"name": "amount", "type": "text"}],
            },
            "idempotency_key": "direct-pdf-citations",
        },
        project_id="p",
        router=router,
    )
    assert result.status == "completed", result.errors
    assert len(requests) == 1
    spans = project.db.execute(
        "SELECT span.*,artifact.blob_hash,artifact.source_column_id FROM citations link "
        "JOIN citation_spans member ON member.citation_id=link.id "
        "JOIN source_spans span ON span.id=member.span_id "
        "JOIN source_artifacts artifact ON artifact.id=span.artifact_id WHERE link.run_id=?",
        (result.run_id,),
    ).fetchall()
    return result, requests[0], spans


def test_pdf_optional_citation_keeps_original_file_and_compact_schema(source):
    _project, _sheet, columns, _rows, value, _data = source
    result, request, spans = _run(
        source, {"source": "document", "page": 2, "quote": "Contract amount: $42"}
    )
    assert spans[0]["blob_hash"] == value["blob"]
    assert spans[0]["source_column_id"] == columns["document"]
    assert spans[0]["span_kind"] == "page_range"
    assert spans[0]["page_start"] == 2
    claim = request.schema["properties"]["amount"]["properties"]["evidence"]["anyOf"][
        0
    ]["items"]
    assert set(claim["properties"]) == {"source", "quote", "page"}
    assert "bbox" not in json.dumps(request.schema)
    link = (
        source[0]
        .db.execute("SELECT stable_id FROM citations WHERE run_id=?", (result.run_id,))
        .fetchone()[0]
    )
    viewer = resolve_citation_viewer(source[0], link)
    assert "no_word_stream" in viewer["warnings"]


@pytest.mark.parametrize(
    "page,expected", [(1, 2), (None, 2), (0, None), (-1, None), (999, None)]
)
def test_selected_pdf_pages_map_back_without_clamping_bad_claims(
    source, page, expected
):
    project, _sheet, columns, rows, value, _data = source
    with write_transaction(project.db):
        change = project.append_change_in_transaction("edit")
        insert_edits(
            project.db,
            change_id=change,
            edits=[EditCellWrite(rows[0], columns["document"], {**value, "page": 2})],
        )
    _result, request, spans = _run(
        source, {"source": "document", "page": page, "quote": "Contract amount: $42"}
    )
    assert spans[0]["page_start"] == expected
    assert spans[0]["page_end"] == expected
    if expected is None:
        metadata = json.loads(spans[0]["metadata"])
        assert "citation_page_out_of_range" in metadata["warnings"]
        assert metadata["alignment"] == "citation_page_out_of_range"
    document = next(
        part for part in request.messages[1]["content"] if part["type"] == "document"
    )
    assert document["page_count"] == 1


def test_pdf_geometry_is_pinned_before_provider_call_and_displays_matched_text(source):
    project, _sheet, columns, rows, _value, _data = source
    ref = _prepared(source)
    admitted = plan(source, "map.extract")
    values = row_values(
        project, admitted.program, admitted.spec_dict(), columns, rows[0]
    )
    assert values.source_cells["document"]["pdf_input"]["prepared_ref_id"] == ref
    _result, _request, spans = _run(
        source,
        {"source": "document", "page": 2, "quote": "Contract amount: $43"},
        before_reply=lambda: _prepared(source, text="Changed after model dispatch"),
    )
    assert spans[0]["span_kind"] == "region"
    assert spans[0]["quote"] == "Contract amount: $42"
    assert spans[0]["snippet"] == "Contract amount: $42"
    assert json.loads(spans[0]["selector_json"])["prepared_ref_id"] == ref


def test_pdf_without_designated_pair_does_not_search_other_same_blob_geometry(
    source, monkeypatch
):
    from frisket.engine.store import ocr_word_stream

    project, _sheet, columns, _rows, _value, _data = source
    _prepared(source)
    with project.db:
        project.db.execute(
            "DELETE FROM document_text_column_roles WHERE source_column_id=?",
            (columns["document"],),
        )

    def forbidden(*_args):
        raise AssertionError("direct PDF evidence must not choose latest same-blob OCR")

    monkeypatch.setattr(ocr_word_stream, "resolve_ocr_word_stream", forbidden)
    _result, _request, spans = _run(
        source, {"source": "document", "page": 2, "quote": "Contract amount: $42"}
    )
    assert spans[0]["span_kind"] == "page_range"
    assert spans[0]["bbox_json"] == "[]"


@pytest.mark.parametrize("label,has_citation", [("other", True), ("unselected", False)])
def test_same_filename_pdf_sources_remain_separately_labeled(
    source, label, has_citation
):
    project, sheet, _columns, rows, value, _data = source
    other = project.add_column(sheet, "other", type="file")
    with write_transaction(project.db):
        change = project.append_change_in_transaction("edit")
        insert_edits(
            project.db, change_id=change, edits=[EditCellWrite(rows[0], other, value)]
        )
    _result, request, spans = _run(
        source,
        {"source": label, "page": 2, "quote": "Contract amount: $42"},
        inputs=["document", "other"],
    )
    assert bool(spans) is has_citation
    if spans:
        assert spans[0]["source_column_id"] == other
    parts = request.messages[1]["content"]
    labels = [
        parts[i - 1]["text"]
        for i, part in enumerate(parts)
        if part["type"] == "document"
    ]
    assert labels == [
        "document: PDF attachment (filing.pdf)",
        "other: PDF attachment (filing.pdf)",
    ]


def test_pdf_prepared_text_without_positions_reports_no_word_stream(source):
    _prepared(source, positioned=False)
    _result, _request, spans = _run(
        source, {"source": "document", "page": 2, "quote": "Contract amount: $42"}
    )
    assert spans[0]["span_kind"] == "page_range"
    assert json.loads(spans[0]["metadata"])["alignment"] == "no_word_stream"


def test_default_pdf_citations_increase_estimate_for_schema_and_output(
    source, monkeypatch
):
    from types import SimpleNamespace

    from frisket.ai.llm.pdf_estimates import estimate_input_tokens
    from frisket.ai.llm.pricing import estimate_output_tokens
    from frisket.engine.runner import validation

    project, _sheet, columns, rows, _value, _data = source
    monkeypatch.setattr(
        validation,
        "model_pricing",
        lambda *_args: SimpleNamespace(
            price=(1.0, 2.0), cost_source="known", pricing_key=MODEL
        ),
    )
    estimates = []
    for options, expected_output_tokens in (
        ({}, 300),
        ({"grounding": {"enabled": False}}, 200),
    ):
        admitted = plan(source, "map.extract", **options)
        spec = admitted.spec_dict()
        values = row_values(project, admitted.program, spec, columns, rows[0])
        call = admitted.program.render(values, spec)
        assert estimate_output_tokens(call, spec=spec) == expected_output_tokens
        input_tokens = estimate_input_tokens(
            {"messages": call.messages, "schema": call.schema}
        )
        estimate = validation._estimate_run(
            project, spec, program=admitted.program, scope_row_ids=[rows[0]]
        ).presentation
        assert estimate["avg_input_tokens"] == input_tokens
        assert estimate["cost"] == round(
            (input_tokens + 2 * expected_output_tokens) / 1e6, 4
        )
        estimates.append(estimate)
    assert estimates[0]["avg_input_tokens"] > estimates[1]["avg_input_tokens"]
    assert estimates[0]["cost"] > estimates[1]["cost"]
