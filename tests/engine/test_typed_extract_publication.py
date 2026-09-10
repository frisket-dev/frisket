from __future__ import annotations

import pytest

from frisket.actions.core import RegisteredAction
from frisket.actions.extract import EXTRACT
from frisket.actions.types import ActionRequest
from frisket.engine.executor.extract_evidence import _source_artifact
from frisket.engine.executor.extract_result_evidence import ExtractResultEvidence
from frisket.engine.store import Project
from frisket.engine.store.evidence import list_cell_evidence
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
        "source": [source_name],
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
    assert values(source)["published_events"] == members
    assert values(source)["events_confidence"] == 0.8
    project, sheet, _, row = source
    column = next(c for c in project.columns(sheet) if c["name"] == "published_events")
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
                    "evidence": [{"segment_indices": [2, 3]}],
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
