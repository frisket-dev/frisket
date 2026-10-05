from __future__ import annotations

import pytest

from frisket.actions.core import RegisteredAction
from frisket.actions.extract import EXTRACT
from frisket.actions.types import ActionRequest
from frisket.engine.executor.extract_evidence import (
    _captured_text_spans,
    _source_artifact,
)
from frisket.engine.executor.extract_result_evidence import (
    ExtractResultEvidence,
    _grounding_artifact_source,
)
from frisket.engine.store import Project
from frisket.engine.store.evidence import list_cell_evidence, resolve_evidence_viewer
from frisket.engine.store.receipts import ReceiptStore
from test_model_rows_actions import _DataAdapter, _run_third_party
from frisket.ai.llm import ModelRouter


REGISTERED = RegisteredAction("map.extract", EXTRACT)


@pytest.fixture
def source(tmp_path):
    project = Project.create(tmp_path / "extract.frisket")
    sheet = project.add_sheet("Documents")
    column = project.add_column(sheet, "body", type="text")
    row = project.add_rows(
        sheet, [{"body": "Ada launched a rocket."}], {"body": column}
    )[0]
    try:
        yield project, sheet, column, row
    finally:
        project.close()


def run_extract(
    source,
    fields,
    reply,
    *,
    grounding=False,
    citation_required=False,
    confidence=False,
    source_name="body",
):
    project, sheet, _, row = source
    params = {
        "source": (
            source_name if isinstance(source_name, (list, dict)) else [source_name]
        ),
        "model": "anthropic/claude-haiku-4-5",
        "fields": fields,
        "grounding": {"enabled": grounding, "citation_required": citation_required},
        "include_confidence": confidence,
    }
    names = {field["name"]: f"published_{field['name']}" for field in fields}
    if confidence:
        name = fields[0]["name"] + "_confidence"
        names[name] = name
    request = ActionRequest(
        action_id="map.extract",
        scope={"kind": "sheet_rows", "sheet_id": sheet, "row_ids": [row]},
        params=params,
        output_names=names,
        idempotency_key="extract@1",
    )
    requests = []
    router = ModelRouter(keys={"anthropic": "stub"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = _DataAdapter(requests, reply)
    gated = _run_third_party(project, request, router, REGISTERED)
    assert gated.status == "needs_confirmation", gated.errors
    assert requests == []
    request = request.model_copy(
        update={"confirmation": gated.errors[0].details["promise_set_hash"]}
    )
    result = _run_third_party(project, request, router, REGISTERED)
    return result, requests


def values(source):
    project, sheet, _, row = source
    return {
        column["name"]: project.get_values(sheet, column["id"], row_ids=[row])[row]
        for column in project.columns(sheet, include_hidden=True)
    }


def test_typed_extract_preserves_types_and_withholds_required_null(source):
    result, requests = run_extract(
        source,
        [
            {"name": "missing", "type": "text", "required": True},
            {"name": "zero", "type": "integer"},
            {"name": "flag", "type": "boolean"},
            {
                "name": "details",
                "type": "json",
                "properties": {"name": {"type": "string"}},
            },
        ],
        {"missing": None, "zero": 0, "flag": False, "details": {"name": "Ada"}},
    )
    assert result.status == "completed", result.errors
    assert len(requests) == 1
    assert values(source) == {
        "body": "Ada launched a rocket.",
        "published_missing": None,
        "published_zero": 0,
        "published_flag": False,
        "published_details": {"name": "Ada"},
    }
    receipt = ReceiptStore(source[0]).parsed_by_id(result.receipt_id)
    assert any(error.code == "required_field_missing" for error in receipt.errors)


@pytest.mark.parametrize("members", [[], ["launch", "landing"]])
def test_typed_extract_list_citations_and_confidence_policy(source, members):
    result, _ = run_extract(
        source,
        [{"name": "events", "type": "list"}],
        {
            "events": {
                "value": members,
                "evidence": [[{"quote": "Ada launched a rocket.", "source_id": 999}]]
                if members
                else [],
            },
            "events_confidence": 0.8,
        },
        grounding=True,
        citation_required=True,
        confidence=True,
    )
    assert result.status == "completed", result.errors
    assert values(source)["published_events"] == (None if members else members)
    assert values(source)["events_confidence"] == 0.8
    project, sheet, _, row = source
    column = next(
        c
        for c in project.columns(sheet, include_hidden=True)
        if c["name"] == "published_events"
    )
    links = list_cell_evidence(
        project, sheet_id=sheet, row_id=row, column_id=column["id"]
    )
    assert len(links["links"]) == bool(members)
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert any(
        output.ref.get("schema") == "events_list"
        for output in receipt.outputs
        if isinstance(output.ref, dict)
    )
    if members:
        fact = next(
            item.ref
            for item in receipt.evidence
            if item.ref.get("kind") == "map_extract_grounding_links"
        )
        assert fact["link_refs"][0]["item_index"] == 0
        assert fact["unsupported_values"][0]["item_index"] == 1
        assert any(error.code == "evidence_required" for error in receipt.errors)


def test_text_citation_projects_utf16_ranges_then_marks_changed_source_stale(source):
    project, sheet, column, row = source
    text = "🚀 Ada\t Lovelace. Ada  Lovelace."
    project.apply_edits([{"row_id": row, "column_id": column, "value": text}])

    result, _ = run_extract(
        source,
        [{"name": "person", "type": "text"}],
        {
            "person": {
                "value": "Ada Lovelace",
                "evidence": [
                    {
                        "source": "body",
                        "quote": "Ada Lovelace.",
                        "segment_indices": [0],
                    }
                ],
            }
        },
        grounding=True,
        citation_required=True,
    )

    assert result.status == "completed", result.errors
    output = next(c for c in project.columns(sheet) if c["name"] == "published_person")
    evidence = list_cell_evidence(
        project, sheet_id=sheet, row_id=row, column_id=output["id"]
    )
    assert len(evidence["links"]) == 1
    viewer = resolve_evidence_viewer(project, evidence["links"][0]["stable_id"])
    artifact = viewer["artifacts"][0]
    assert artifact["title"] == "body"
    assert artifact["text_context"]["text"] == text
    assert artifact["text_context"]["offset_unit"] == "utf16_code_unit"
    assert artifact["text_context"]["ranges"] == [
        {
            "span_id": artifact["spans"][0]["stable_id"],
            "start": 3,
            "end": 17,
        },
        {
            "span_id": artifact["spans"][1]["stable_id"],
            "start": 18,
            "end": 32,
        },
    ]

    project.apply_edits(
        [{"row_id": row, "column_id": column, "value": "changed later"}]
    )
    edited = resolve_evidence_viewer(project, evidence["links"][0]["stable_id"])
    assert edited["artifacts"][0]["text_context"] is None
    assert edited["artifacts"][0]["text_context_status"] == "stale"
    assert [span["quote"] for span in edited["artifacts"][0]["spans"]] == [
        "Ada\t Lovelace.",
        "Ada  Lovelace.",
    ]
    assert edited["link"]["text_layer_hash_mismatch"] is True


def test_text_citation_repairs_malformed_source_and_keeps_anchors(source):
    project, sheet, column, row = source
    project.apply_edits(
        [{"row_id": row, "column_id": column, "value": "before \ud800 after 🚀"}]
    )

    result, _ = run_extract(
        source,
        [{"name": "summary", "type": "text"}],
        {
            "summary": {
                "value": "found",
                "evidence": [
                    {"source": "body", "quote": "before \ufffd"},
                    {"source": "body", "quote": "after 🚀"},
                ],
            }
        },
        grounding=True,
        citation_required=True,
    )

    assert result.status == "completed", result.errors
    output = next(c for c in project.columns(sheet) if c["name"] == "published_summary")
    links = list_cell_evidence(
        project, sheet_id=sheet, row_id=row, column_id=output["id"]
    )["links"]
    artifacts = [
        resolve_evidence_viewer(project, link["stable_id"])["artifacts"][0]
        for link in links
    ]

    assert len(artifacts) == 2
    assert {artifact["text_context"]["text"] for artifact in artifacts} == {
        "before \ufffd after 🚀"
    }
    anchored = sorted(
        (
            artifact["spans"][0]["quote"],
            artifact["text_context"]["ranges"][0]["start"],
            artifact["text_context"]["ranges"][0]["end"],
        )
        for artifact in artifacts
    )
    assert anchored == [("after 🚀", 9, 17), ("before \ufffd", 0, 8)]


def test_capture_repairs_malformed_model_quote_before_alignment(source):
    project, sheet, column, row = source
    captured = {
        "column_id": column,
        "captured_text": "before \ud800 after",
        "composite": True,
    }
    artifact = _source_artifact(
        project,
        sheet_id=sheet,
        row_id=row,
        source_columns=["body"],
        input_column_ids={"body": column},
        artifact_cache={},
        captured_sources={"body": captured},
    )

    spans = _captured_text_spans(
        project,
        artifact=artifact,
        source=captured,
        source_label="body",
        sheet_id=sheet,
        row_id=row,
        entry={"quote": "before \ud800"},
        rank=0,
    )

    assert spans is not None
    assert [span["quote"] for span in spans] == ["before \ufffd"]
    assert (spans[0]["char_start"], spans[0]["char_end"]) == (0, 8)
    assert spans[0]["metadata"]["raw"]["quote"] == "before \ufffd"


def test_text_claims_resolve_per_visible_source_and_per_deliberate_claim(source):
    project, sheet, _, row = source
    notes = project.add_column(sheet, "notes", type="text")
    project.apply_edits(
        [{"row_id": row, "column_id": notes, "value": "Dear Ada. Ada replied."}]
    )
    result, _ = run_extract(
        source,
        [{"name": "person", "type": "text"}],
        {
            "person": {
                "value": "Ada",
                "evidence": [
                    {"source": "body", "quote": "Ada"},
                    {"source": "notes", "quote": "Dear Ada"},
                ],
            }
        },
        grounding=True,
        citation_required=True,
        source_name=["body", "notes"],
    )
    assert result.status == "completed", result.errors
    output = next(c for c in project.columns(sheet) if c["name"] == "published_person")
    links = list_cell_evidence(
        project, sheet_id=sheet, row_id=row, column_id=output["id"]
    )["links"]
    assert len(links) == 2
    assert {
        resolve_evidence_viewer(project, link["stable_id"])["artifacts"][0]["title"]
        for link in links
    } == {"body", "notes"}


def test_real_multi_text_reply_ignores_spurious_segment_hints(source):
    project, sheet, _, row = source
    filing = project.add_column(sheet, "filing_text", type="text", format="markdown")
    note = project.add_column(sheet, "clerk_note", type="text")
    project.apply_edits(
        [
            {
                "row_id": row,
                "column_id": filing,
                "value": "📄 COMPLAINT\n\nLeena\nPatel v. Cedar Bridge.\nLeena Patel filed.",
            },
            {
                "row_id": row,
                "column_id": note,
                "value": "Clerk note:\r\n\r\nLeena\tPatel filed.\nLeena\u00a0Patel requested a copy.",
            },
        ]
    )
    reply = {
        "person": {
            "value": "Leena Patel",
            "evidence": [
                {
                    "source": "filing_text",
                    "segment_indices": [0],
                    "quote": "Leena Patel",
                },
                {
                    "source": "clerk_note",
                    "segment_indices": [0],
                    "quote": "Leena Patel",
                },
            ],
            "warnings": [],
        }
    }
    result, _ = run_extract(
        source,
        [{"name": "person", "type": "text"}],
        reply,
        grounding=True,
        citation_required=True,
        source_name=["filing_text", "clerk_note"],
    )
    assert result.status == "completed", result.errors
    assert values(source)["published_person"] == "Leena Patel"
    output = next(c for c in project.columns(sheet) if c["name"] == "published_person")
    links = list_cell_evidence(
        project, sheet_id=sheet, row_id=row, column_id=output["id"]
    )["links"]
    assert len(links) == 2
    contexts = {
        viewer["artifacts"][0]["title"]: viewer["artifacts"][0]["text_context"]
        for viewer in (
            resolve_evidence_viewer(project, link["stable_id"]) for link in links
        )
    }
    assert len(contexts["filing_text"]["ranges"]) == 2
    assert len(contexts["clerk_note"]["ranges"]) == 2


def test_ambiguous_or_missing_text_quote_does_not_satisfy_require(source):
    project, sheet, _, row = source
    notes = project.add_column(sheet, "notes", type="text")
    project.apply_edits(
        [{"row_id": row, "column_id": notes, "value": "Ada appears here too."}]
    )
    result, _ = run_extract(
        source,
        [{"name": "person", "type": "text"}],
        {"person": {"value": "Ada", "evidence": [{"quote": "Ada"}]}},
        grounding=True,
        citation_required=True,
        source_name=["body", "notes"],
    )
    assert result.status == "completed", result.errors
    assert values(source)["published_person"] is None
    assert project.db.execute("SELECT COUNT(*) FROM evidence_links").fetchone()[0] == 0


def test_optional_unmatched_text_quote_keeps_value_without_located_link(source):
    result, _ = run_extract(
        source,
        [{"name": "person", "type": "text"}],
        {
            "person": {
                "value": "Grace",
                "evidence": [{"source": "body", "quote": "Grace"}],
            }
        },
        grounding=True,
    )
    assert result.status == "completed", result.errors
    assert values(source)["published_person"] == "Grace"
    assert (
        source[0].db.execute("SELECT COUNT(*) FROM evidence_links").fetchone()[0] == 0
    )


def test_template_input_is_captured_as_one_citable_composite(source):
    project, sheet, _, row = source
    result, _ = run_extract(
        source,
        [{"name": "person", "type": "text"}],
        {
            "person": {
                "value": "Ada",
                "evidence": [{"source": "input", "quote": "Subject: Ada"}],
            }
        },
        grounding=True,
        citation_required=True,
        source_name={"text": "Subject: {{ body }}"},
    )
    assert result.status == "completed", result.errors
    output = next(c for c in project.columns(sheet) if c["name"] == "published_person")
    link = list_cell_evidence(
        project, sheet_id=sheet, row_id=row, column_id=output["id"]
    )["links"][0]
    artifact = resolve_evidence_viewer(project, link["stable_id"])["artifacts"][0]
    assert artifact["title"] == "input"
    assert artifact["source_cell"] is None
    assert artifact["text_context"]["text"] == "Subject: Ada launched a rocket."


def test_generated_markdown_quote_remains_primary_with_auxiliary_pdf():
    sources = {
        "markdown": {
            "model_visible": True,
            "captured_text": "Leena Patel filed the complaint.",
            "value": "Leena Patel filed the complaint.",
            "value_ref": {"kind": "run_result", "run_id": 7},
            "producer_action_kind": "media.to_markdown",
        },
        "original_pdf": {
            "model_visible": False,
            "value": {"blob": "sha256:pdf"},
        },
    }

    assert (
        _grounding_artifact_source(
            sources,
            "markdown",
            {"quote": "Leena Patel"},
            ("original_pdf",),
        )
        == "markdown"
    )


def test_page_claim_does_not_guess_between_multiple_auxiliary_files():
    sources = {
        "markdown": {"model_visible": True, "value": "Page text"},
        "first_pdf": {"model_visible": False, "value": {"blob": "sha256:first"}},
        "second_pdf": {
            "model_visible": False,
            "value": {"blob": "sha256:second"},
        },
    }

    assert (
        _grounding_artifact_source(
            sources,
            "markdown",
            {"page": 1, "quote": "Page text"},
            ("first_pdf", "second_pdf"),
        )
        is None
    )


def test_typed_extract_required_citation_withholds_only_unsupported_field(source):
    result, _ = run_extract(
        source,
        [
            {"name": "supported", "type": "text"},
            {"name": "unsupported", "type": "text"},
        ],
        {
            "supported": {"value": "Ada", "evidence": [{"quote": "Ada"}]},
            "unsupported": {"value": "NASA", "evidence": []},
        },
        grounding=True,
        citation_required=True,
    )
    assert result.status == "completed", result.errors
    assert values(source)["published_supported"] == "Ada"
    assert values(source)["published_unsupported"] is None
    receipt = ReceiptStore(source[0]).parsed_by_id(result.receipt_id)
    assert any(error.code == "evidence_required" for error in receipt.errors)


@pytest.mark.parametrize("grounding", [False, True])
@pytest.mark.parametrize("confidence", [0.0, 0.8, None])
def test_extract_confidence_persists_on_every_output_cell(
    source, grounding, confidence
):
    reply = {"person": "Ada", "vehicle": "rocket"}
    if grounding:
        reply = {
            name: {"value": value, "evidence": [{"quote": value}]}
            for name, value in reply.items()
        }
    result, _ = run_extract(
        source,
        [{"name": "person", "type": "text"}, {"name": "vehicle", "type": "text"}],
        {**reply, "person_confidence": confidence},
        grounding=grounding,
        confidence=True,
    )
    assert result.status == "completed", result.errors
    assert values(source)["person_confidence"] == confidence
    cells = (
        source[0]
        .db.execute(
            "SELECT c.name, r.confidence FROM results r JOIN columns c ON c.id=r.column_id "
            "WHERE r.run_id=?",
            (result.run_id,),
        )
        .fetchall()
    )
    assert {cell["name"]: cell["confidence"] for cell in cells} == {
        "published_person": confidence,
        "published_vehicle": confidence,
        "person_confidence": confidence,
    }


def test_grounded_fields_share_one_captured_source_artifact_per_write(source):
    result, _ = run_extract(
        source,
        [{"name": "person", "type": "text"}, {"name": "vehicle", "type": "text"}],
        {
            "person": {"value": "Ada", "evidence": [{"quote": "Ada"}]},
            "vehicle": {"value": "rocket", "evidence": [{"quote": "rocket"}]},
        },
        grounding=True,
        citation_required=True,
    )
    assert result.status == "completed", result.errors
    project = source[0]
    assert (
        project.db.execute("SELECT COUNT(*) FROM source_artifacts").fetchone()[0] == 1
    )
    spans = project.db.execute(
        "SELECT s.artifact_id,s.quote FROM evidence_links l "
        "JOIN evidence_link_spans ls ON ls.link_id=l.id JOIN source_spans s ON s.id=ls.span_id "
        "WHERE l.run_id=?",
        (result.run_id,),
    ).fetchall()
    assert len({span["artifact_id"] for span in spans}) == 1
    assert {span["quote"] for span in spans} == {"Ada", "rocket"}


def test_extraction_source_capture_freezes_values_before_grounding(source, monkeypatch):
    project, sheet, column, row = source
    captured = {
        "body": {
            "column_id": column,
            "value": {"nested": ["original"]},
            "value_ref": {"kind": "source_cell"},
        }
    }
    results = {"answer": {"value": "Ada"}}
    writer = ExtractResultEvidence(project, fields={}, output_names={})
    writer.capture({}, captured, results, {}, row_id=row)
    captured["body"]["value"]["nested"][0] = "changed"
    monkeypatch.setattr(
        project,
        "get_values",
        lambda *args, **kwargs: pytest.fail("must not reread mutable source cells"),
    )
    artifact = _source_artifact(
        project,
        sheet_id=sheet,
        row_id=row,
        source_columns=["body"],
        input_column_ids={"body": column},
        artifact_cache={},
        captured_sources=results["answer"]["extraction_sources"],
    )
    assert artifact["metadata"]["captured_sources"]["body"]["value"] == {
        "nested": ["original"]
    }


def test_grounding_publication_rolls_back_links_with_failed_receipt_write(
    source, monkeypatch
):
    original = ReceiptStore._record_writer_evidence

    def fail_after_link(self, ref, **kwargs):
        if ref.get("kind") == "map_extract_grounding_links":
            assert (
                self.db.execute("SELECT COUNT(*) FROM evidence_links").fetchone()[0]
                == 1
            )
            raise RuntimeError("receipt evidence failed")
        return original(self, ref, **kwargs)

    monkeypatch.setattr(ReceiptStore, "_record_writer_evidence", fail_after_link)
    result, requests = run_extract(
        source,
        [{"name": "person", "type": "text"}],
        {"person": {"value": "Ada", "evidence": [{"quote": "Ada"}]}},
        grounding=True,
    )
    assert result.status == "failed"
    assert [error.code for error in result.errors] == ["project_write_failed"]
    assert result.receipt_id is not None
    assert len(requests) == 1
    assert source[0].db.execute("SELECT status FROM receipts").fetchone()[0] == "failed"
    assert values(source)["published_person"] is None
    for table in ("source_artifacts", "source_spans", "evidence_links"):
        assert source[0].db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("source_name", ["transcript", "transcript_segments"])
def test_transcript_prompt_and_evidence_use_captured_version(
    tmp_path, monkeypatch, source_name
):
    import json

    from frisket.engine.store.evidence import record_source_artifact, record_source_span
    from frisket.engine.executor.extract_result_evidence import _captured_transcript
    from test_extract_scalar_temporal_anchors import (
        _seed_transcript_project,
        _run_transcribe,
    )

    seeded = _seed_transcript_project(tmp_path)
    project = seeded["project"]
    try:
        _run_transcribe(seeded, monkeypatch)
        sheet, row = seeded["sheet_id"], seeded["row_ids"][0]
        column = next(c for c in project.columns(sheet) if c["name"] == source_name)
        source = (project, sheet, column["id"], row)
        source_values, source_refs = project.get_values_with_refs(
            sheet, column["id"], row_ids=[row]
        )
        captured = {
            source_name: {
                "column_id": column["id"],
                "value": source_values[row],
                "value_ref": source_refs[row],
            }
        }
        pinned = _captured_transcript(project, captured, sheet_id=sheet, row_id=row)
        assert pinned is not None
        unrelated = record_source_artifact(
            project,
            artifact_kind="av",
            media_type="audio/wav",
            source_sheet_id=sheet,
            source_row_id=row,
        )
        record_source_span(
            project,
            artifact_id=unrelated["id"],
            span_kind="temporal",
            start_ms=1000,
            end_ms=2000,
            quote="Unrelated newer transcript",
            selector={"segment_index": 2},
        )
        result, requests = run_extract(
            source,
            [{"name": "claim", "type": "text"}],
            {
                "claim": {
                    "value": "vote was rigged",
                    "evidence": [
                        {
                            "source": (
                                "transcript_segments"
                                if source_name == "transcript"
                                else source_name
                            ),
                            "segment_indices": [2, 3],
                        }
                    ],
                }
            },
            grounding=True,
            citation_required=True,
            source_name=source_name,
        )
        assert result.status == "completed", result.errors
        assert values(source)["published_claim"] == "vote was rigged"
        prompt = json.dumps(requests[0].messages)
        assert "[2]" in prompt and "[3]" in prompt
        assert "Unrelated newer transcript" not in prompt
        target = next(
            c for c in project.columns(sheet) if c["name"] == "published_claim"
        )
        spans = project.db.execute(
            "SELECT s.artifact_id,s.start_ms,s.end_ms FROM evidence_links l "
            "JOIN evidence_link_spans ls ON ls.link_id=l.id JOIN source_spans s ON s.id=ls.span_id "
            "WHERE l.column_id=? ORDER BY ls.rank",
            (target["id"],),
        ).fetchall()
        assert [(span["start_ms"], span["end_ms"]) for span in spans] == [
            (4000, 6000),
            (6000, 8000),
        ]
        assert all(span["artifact_id"] != unrelated["id"] for span in spans)
        assert {span["artifact_id"] for span in spans} == {pinned.artifact_id}
        # A different producing run must not borrow the row's older (or newer)
        # transcript evidence even when that row has valid temporal artifacts.
        captured[source_name]["value_ref"] = {
            **source_refs[row],
            "run_id": result.run_id,
        }
        assert (
            _captured_transcript(project, captured, sheet_id=sheet, row_id=row) is None
        )
    finally:
        project.close()
