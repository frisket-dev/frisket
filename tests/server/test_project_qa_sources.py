"""Project Ask sources, source limits, search integrity, search purchase, contracts, and capabilities tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

import httpx
import pytest
from pydantic import ValidationError
from pydantic_ai.workspaces import ReadOnlyWorkspace, WorkspaceReadOnlyError
from pydantic_ai_harness import Skills
from pydantic_ai_harness.compaction import SummarizingCompaction, TieredCompaction

import frisket.retrieval.service as retrieval_service
from frisket.ai.research.search import SearchService
from frisket.contracts.http.project_qa import AskCitation, AskScope, AskTurnRequest
from frisket.engine.store.citations import (
    record_citation,
    record_source_artifact,
    record_source_span,
)
from frisket.engine.store.media_blobs import media_cell
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.engine.store.transactions import write_transaction
from frisket.execution.dispatch_control import STANDALONE, TransientDispatch
from frisket.execution.purchases import (
    PurchaseAdmission,
    PurchasePolicyRequired,
    PurchaseRecordingError,
)
from frisket.retrieval.contracts import (
    BindingRevision,
    IndexedPart,
    LocatedHit,
    PageRange,
    ResultGroup,
    RetrievalContribution,
    ScopedBinding,
    SearchCoverage,
    SearchResponse,
    SourceLocator,
    SourceRevision,
    TextQuery,
    TimeRange,
)
from frisket.search import drain_index
from frisket.server.services.project_qa_capabilities import (
    ProjectQACapabilityError,
    compose_project_qa_capabilities,
)
from frisket.server.services.project_qa_citations import resolve_citation
from frisket.server.services.project_qa_runner import _search_with_purchase
from frisket.server.services.project_qa_sources import (
    MAX_SCAN_CHARS,
    find_source_text,
    read_source_text,
)
from frisket.server.services.project_qa_tools import ProjectQAScopeError, ProjectQATools

# --- sources ---


@pytest.fixture
def source(make_project):
    project = make_project("Sources")
    sheet = project.add_sheet("Documents")
    column = project.add_column(sheet, "Text")
    [row] = project.add_rows(
        sheet,
        [{"Text": "opening " * 10000 + "NEEDLE decisive passage " + "ending " * 2000}],
        {"Text": column},
    )
    store = ProjectQAStore(project)
    thread = store.create_thread(title="Ask")
    turn = store.submit_turn(
        thread["id"], request_id="one", question="Find the evidence"
    )
    drain_index(project)
    yield project, store, thread, turn, sheet, column, row


def test_search_opens_late_passage_and_continues_without_repeating_prefix(source):
    project, store, _, turn, sheet, _, _ = source
    tools = ProjectQATools(project, turn, store)
    hit = tools.search_cells("NEEDLE", sheet)["hits"][0]
    opened = tools.open_source(hit["citation_id"])
    assert "NEEDLE decisive passage" in opened["passages"][0]["text"]
    assert opened["range"]["start"] > 50000
    assert sum(len(p["text"]) for p in opened["passages"]) <= 8000
    continued = tools.open_source(hit["citation_id"], cursor=opened["next_cursor"])
    assert continued["range"]["start"] == opened["range"]["end"]


def test_keyword_search_uses_search_revision_not_unrelated_operation_cursor(source):
    project, store, _, turn, sheet, _, _ = source
    project.db.execute(
        "UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='change_cursor'"
    )
    project.db.commit()
    result = ProjectQATools(project, turn, store).search_cells("NEEDLE", sheet)
    assert result["hits"]
    assert result["coverage"]["state"] == "complete"
    assert result["coverage"]["requested_mode"] == "lexical"


def test_exact_cell_scope_still_searches_its_original_text(source):
    project, store, _, turn, sheet, column, row = source
    turn = {
        **turn,
        "scope": {
            "kind": "sources",
            "sources": [
                {"kind": "file", "sheet_id": sheet, "row_id": row, "column_id": column}
            ],
        },
    }
    tools = ProjectQATools(project, turn, store)
    [hit] = tools.search_cells("NEEDLE", sheet)["hits"]
    assert hit["column_id"] == column
    assert "NEEDLE" in tools.open_source(hit["citation_id"])["passages"][0]["text"]


def test_search_skips_a_file_target_hidden_after_scope_admission(source):
    project, store, _, turn, sheet, column, row = source
    [hidden_row] = project.add_rows(
        sheet, [{"Text": "unrelated file target"}], {"Text": column}
    )
    scoped_turn = {
        **turn,
        "scope": {
            "kind": "sources",
            "sources": [
                {
                    "kind": "file",
                    "sheet_id": sheet,
                    "row_id": row,
                    "column_id": column,
                },
                {
                    "kind": "file",
                    "sheet_id": sheet,
                    "row_id": hidden_row,
                    "column_id": column,
                },
            ],
        },
    }
    tools = ProjectQATools(project, scoped_turn, store)
    project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (hidden_row,))
    project.db.commit()

    result = tools.search_cells("NEEDLE", sheet)

    assert [hit["row_id"] for hit in result["hits"]] == [row]
    assert result["partial"] is True
    assert result["coverage"]["reason"] == "source_changed"
    with pytest.raises(ProjectQAScopeError, match="hidden or missing"):
        ProjectQATools(project, scoped_turn, store)


def test_prior_sources_reopen_only_in_current_scope_and_metadata_is_filtered(source):
    project, store, thread, turn, sheet, column, row = source
    first = ProjectQATools(project, turn, store)
    cid = first.search_cells("NEEDLE", sheet)["hits"][0]["citation_id"]
    store.finish_turn(turn["id"], status="completed")
    second = store.submit_turn(
        thread["id"], request_id="two", question="Read that again"
    )
    tools = ProjectQATools(project, second, store)
    opened = tools.open_source(cid)
    assert opened["citation_id"] != cid
    assert opened["citation_id"] in tools.citation_ids
    assert tools.open_source(cid)["citation_id"] == opened["citation_id"]
    other = project.add_sheet("Other")
    store.finish_turn(second["id"], status="completed")
    narrowed = store.submit_turn(
        thread["id"],
        request_id="three",
        question="Other only",
        scope={"kind": "sources", "sources": [{"kind": "sheet", "sheet_id": other}]},
    )
    restricted = ProjectQATools(project, narrowed, store)
    assert restricted.list_sources()["sources"] == []
    with pytest.raises(ProjectQAScopeError):
        restricted.open_source(cid)


def test_literal_find_returns_exact_offsets_and_scan_coverage(source):
    project, store, _, turn, sheet, _, _ = source
    tools = ProjectQATools(project, turn, store)
    cid = tools.search_cells("NEEDLE", sheet)["hits"][0]["citation_id"]
    result = tools.find_in_source(cid, "NEEDLE decisive")
    assert result["matches"][0]["start"] == 80000
    assert result["reached_end"] is True
    assert result["matches"][0]["citation_id"] in tools.citation_ids


def test_action_discovery_is_bounded_and_obeys_suggestions_setting(source):
    project, store, _, turn, sheet, _, _ = source
    tools = ProjectQATools(
        project,
        turn,
        store,
        estimate_provider=lambda _action: {
            "estimate": {
                "billed_cost": 17,
                "policy_id": "normal-policy",
                "promise_set_hash": "confirmation-hash",
                "requires_confirmation": True,
            }
        },
    )
    found = tools.search_actions("transcribe", limit=3)
    assert 0 < len(found["actions"]) <= 3
    assert all("input_schema" not in item for item in found["actions"])
    transcribe = next(
        item for item in found["actions"] if item["action_id"] == "media.transcribe"
    )
    assert transcribe["reference"] == "[Transcribe](#action/media.transcribe)"
    described = tools.describe_action(transcribe["action_id"])
    assert described["input_schema"]
    assert described["output_schema"]
    markdown = tools.describe_action("media.to_markdown")
    assert {item["key"] for item in markdown["logical_outputs"]} >= {"markdown"}
    assert tools.describe_action("map.extract")["dynamic_outputs"] is True
    assert described["reference"] == transcribe["reference"]
    assert described["required_params"] == ["source"]
    assert described["defaults"]["engine"] == "parakeet-tdt"
    assert described["source_requirements"] == [
        {
            "id": "source",
            "param": "source",
            "label": "Source",
            "mode": "column",
            "min": 1,
            "accepted_column_types": ["audio", "video", "file"],
        }
    ]
    assert described["proposal_contract"] == {
        "fixed_fields": {"action_id": "media.transcribe"},
        "scope": {"kind": "sheet_rows", "requires_sheet_id": True},
        "params": {
            "required": ["source"],
            "optional": [
                "language",
                "model_size",
                "context",
                "clean",
                "vad",
                "diarize",
                "num_speakers",
                "min_speakers",
                "max_speakers",
                "engine",
            ],
        },
        "output_names": "optional logical-output to column-name mapping",
    }
    assert not [
        event
        for event in store.events(turn["thread_id"])["events"]
        if event["kind"] == "action_proposal"
    ]

    project.add_column(sheet, "Council audio", type="audio")
    draft = {
        "action_id": "media.transcribe",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {"source": "Council audio"},
        "output_names": {},
    }
    prepared = tools.propose_action(
        "Transcribe council audio",
        draft,
    )
    event = store.events(turn["thread_id"])["events"][-1]
    assert event["kind"] == "action_proposal"
    assert prepared["proposal"] == event["payload"]["proposal"]
    assert prepared["proposal"] == {
        "title": "Transcribe council audio",
        "spec": draft,
    }
    assert event["payload"]["prepared_action"]["estimate"]["estimate"] == {
        "billed_cost": 17,
        "policy_id": "normal-policy",
        "promise_set_hash": "confirmation-hash",
        "requires_confirmation": True,
    }
    assert prepared["reference"] == (
        f"[Transcribe council audio](#action-{event['seq']})"
    )

    false_value = tools.propose_action(
        "Extract text matches",
        {
            "action_id": "map.regex_extract",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {
                "input_columns": ["Text"],
                "pattern": "NEEDLE",
                "all_matches": False,
            },
            "output_names": {},
        },
    )["proposal"]
    assert false_value["spec"]["params"]["all_matches"] is False

    zero = tools.propose_action(
        "Extract entities",
        {
            "action_id": "map.ner",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {
                "source": ["Text"],
                "labels": ["PERSON"],
                "engine": "gliner",
                "threshold": 0,
            },
            "output_names": {},
        },
    )["proposal"]
    assert zero["spec"]["params"]["threshold"] == 0

    before = len(store.events(turn["thread_id"])["events"])
    for invalid in [
        {
            "action_kind": "media.transcribe",
            "sheet_id": sheet,
            "source": "Council audio",
        },
        {**draft, "confirmation": "consent-hash"},
        {**draft, "idempotency_key": "ask-must-not-authorize"},
        {**draft, "confirmed": True},
        {**draft, "consented_promise_set_hash": "consent-hash"},
        {**draft, "authoring_contract_version": "v1"},
        {**draft, "action_id": "media.not_registered"},
        {**draft, "params": {"source": "Missing media"}},
    ]:
        with pytest.raises(ProjectQAScopeError):
            tools.propose_action("Invalid proposal", invalid)
    assert len(store.events(turn["thread_id"])["events"]) == before
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    disabled = ProjectQATools(project, {**turn, "suggest_actions": False}, store)
    with pytest.raises(ProjectQAScopeError):
        disabled.search_actions("transcribe")
    with pytest.raises(ProjectQAScopeError):
        disabled.describe_action(found["actions"][0]["action_id"])


def test_continuation_survives_unrelated_edits_but_refuses_changed_source(source):
    project, store, _, turn, sheet, column, row = source
    tools = ProjectQATools(project, turn, store)
    cid = tools.search_cells("NEEDLE", sheet)["hits"][0]["citation_id"]
    opened = tools.open_source(cid)
    other = project.add_column(sheet, "Notes")
    project.apply_edits([{"row_id": row, "column_id": other, "value": "new note"}])
    assert (
        tools.open_source(cid, opened["next_cursor"])["range"]["start"]
        == opened["range"]["end"]
    )
    project.apply_edits(
        [{"row_id": row, "column_id": column, "value": "replacement text"}]
    )
    with pytest.raises(ValueError, match="source_changed"):
        tools.open_source(cid, opened["next_cursor"])
    reopened = tools.open_source(cid)
    assert reopened["source_changed"] is True
    assert reopened["passages"][0]["text"] == "replacement text"


def test_reopening_an_old_offset_after_a_shorter_edit_returns_current_source(source):
    project, store, _, turn, sheet, column, row = source
    original = read_source_text(project, (sheet, row, column), limit=1)
    citation = store.add_citation(
        turn["id"],
        label="Late passage",
        source_kind="cell",
        locator={
            "sheet_id": sheet,
            "row_id": row,
            "column_id": column,
            "value_ref": original["value_ref"],
            "source_version": original["version"],
            "char_start": 80_000,
        },
        excerpt="NEEDLE decisive passage",
    )
    tools = ProjectQATools(project, turn, store)
    opened = tools.open_source(citation["id"])
    project.apply_edits(
        [{"row_id": row, "column_id": column, "value": "replacement text"}]
    )

    with pytest.raises(ValueError, match="source_changed"):
        tools.open_source(citation["id"], opened["next_cursor"])
    reopened = tools.open_source(citation["id"])

    assert reopened["source_changed"] is True
    assert reopened["range"]["start"] == 0
    assert reopened["passages"][0]["text"] == "replacement text"
    assert reopened["reached_end"] is True


def test_source_text_cannot_override_an_explicit_citation_offset(source):
    project, store, _, turn, sheet, column, row = source
    text = "Methodology appears early. " + "x" * 10_000 + "<b>Methodology</b> late."
    project.apply_edits([{"row_id": row, "column_id": column, "value": text}])
    source_meta = read_source_text(project, (sheet, row, column), limit=1)
    late_start = text.index("<b>Methodology</b>")
    citation = store.add_citation(
        turn["id"],
        label="Late methodology",
        source_kind="cell",
        locator={
            "sheet_id": sheet,
            "row_id": row,
            "column_id": column,
            "value_ref": source_meta["value_ref"],
            "source_version": source_meta["version"],
            "char_start": late_start,
        },
        excerpt="<b>Methodology</b> late.",
    )

    opened = ProjectQATools(project, turn, store).open_source(citation["id"])

    assert opened["range"]["start"] == late_start
    assert "<b>Methodology</b> late." in opened["passages"][0]["text"]


def test_exact_fts_location_bypasses_fallback_anchor(source):
    project, store, _, turn, sheet, column, row = source
    text = (
        "Methodology decoy. "
        + "filler " * 500
        + "<b>Methodology</b> immediately before ACTUALNEEDLE"
    )
    project.apply_edits([{"row_id": row, "column_id": column, "value": text}])
    drain_index(project)
    tools = ProjectQATools(project, turn, store)

    [hit] = tools.search_cells('"ACTUALNEEDLE"', sheet)["hits"]
    citation = store.get_citation(hit["citation_id"])
    opened = tools.open_source(hit["citation_id"])

    assert "<b>Methodology</b>" in citation["excerpt"]
    assert "fts_anchor" not in citation["metadata"]
    assert opened["range"]["start"] == text.index("ACTUALNEEDLE")
    assert "ACTUALNEEDLE" in opened["passages"][0]["text"]


def test_small_source_read_keeps_anchor_in_the_returned_text(source):
    project, _, _, _, sheet, column, row = source

    opened = read_source_text(project, (sheet, row, column), anchor="NEEDLE", limit=20)

    assert "NEEDLE" in opened["text"]


def test_keyword_result_reports_when_its_ranked_examples_hit_the_limit(source):
    project, store, _, turn, sheet, _, _ = source

    result = ProjectQATools(project, turn, store).search_cells("NEEDLE", sheet, limit=1)

    assert len(result["hits"]) == 1
    assert result["coverage"]["state"] == "complete"
    assert result["coverage"]["is_top_k"] is True
    assert result["coverage"]["searched_channels"] == ["lexical"]


def test_citation_freshness_uses_the_saved_source_version(source):
    project, store, thread, turn, sheet, column, row = source
    source_meta = read_source_text(project, (sheet, row, column), limit=1)
    citation = store.add_citation(
        turn["id"],
        label="Source",
        source_kind="cell",
        locator={
            "sheet_id": sheet,
            "row_id": row,
            "column_id": column,
            "value_ref": source_meta["value_ref"],
            "source_version": {
                **source_meta["version"],
                "base_producer_id": -1,
            },
        },
        excerpt="opening",
    )

    resolved = resolve_citation(project, thread["id"], citation["id"])

    assert resolved["status"] == "changed"


def test_literal_find_crosses_scan_boundary_and_preserves_unicode_offsets(source):

    project, _, _, _, sheet, column, row = source
    start = MAX_SCAN_CHARS - 3
    text = "é" * start + "🦊NEEDLE" + "end"
    project.apply_edits([{"row_id": row, "column_id": column, "value": text}])
    first = find_source_text(project, (sheet, row, column), "🦊NEEDLE")
    assert first["matches"] == []
    assert first["reached_end"] is False
    second = find_source_text(
        project, (sheet, row, column), "🦊NEEDLE", cursor=first["next_cursor"]
    )
    assert second["matches"][0]["start"] == start
    assert second["reached_end"] is True


def test_read_budget_reports_incomplete_coverage(source):
    project, store, _, turn, sheet, _, _ = source
    tools = ProjectQATools(project, turn, store)
    cid = tools.search_cells("opening", sheet)["hits"][0]["citation_id"]
    cursor = None
    read_chars = 0
    for _ in range(20):
        result = tools.open_source(cid, cursor)
        read_chars += sum(len(p["text"]) for p in result["passages"])
        if result.get("coverage"):
            assert "budget exhausted" in result["coverage"]
            assert result["truncated"] is True
            break
        assert result["reached_end"] is False
        cursor = result["next_cursor"]
    else:
        pytest.fail("source budget did not stop reading")
    assert read_chars == 64000


def test_cancelled_source_read_stops_before_storage_work(source):
    project, store, _, turn, sheet, _, _ = source
    tools = ProjectQATools(project, turn, store)
    cid = tools.search_cells("NEEDLE", sheet)["hits"][0]["citation_id"]
    tools.cancel_event.set()
    with pytest.raises(InterruptedError):
        tools.open_source(cid)


def test_open_text_uses_bounded_storage_reads(source, monkeypatch):
    project, store, _, turn, sheet, _, _ = source
    tools = ProjectQATools(project, turn, store)
    cid = tools.search_cells("NEEDLE", sheet)["hits"][0]["citation_id"]

    def whole_value_read(*args, **kwargs):
        pytest.fail("source reader loaded the whole cell into Python")

    monkeypatch.setattr(project, "get_values", whole_value_read)
    monkeypatch.setattr(project, "get_values_with_refs", whole_value_read)
    assert "NEEDLE" in tools.open_source(cid)["passages"][0]["text"]


def test_hidden_source_is_omitted_before_returning_its_label(source):
    project, store, _, turn, sheet, _, row = source
    tools = ProjectQATools(project, turn, store)
    cid = tools.search_cells("NEEDLE", sheet)["hits"][0]["citation_id"]
    project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (row,))
    project.db.commit()
    assert tools.list_sources()["sources"] == []
    with pytest.raises(ProjectQAScopeError, match="hidden"):
        tools.open_source(cid)


def test_late_prepared_quote_keeps_timestamps_without_full_viewer_load(
    source, monkeypatch
):

    project, store, _, turn, sheet, column, row = source
    _, refs = project.get_values_with_refs(sheet, column, [row])
    with write_transaction(project.db):
        artifact = record_source_artifact(
            project, artifact_kind="audio", media_type="audio/wav", title="Interview"
        )
    with write_transaction(project.db):
        span = record_source_span(
            project,
            artifact_id=artifact["id"],
            span_kind="audio",
            quote="NEEDLE decisive passage",
            start_ms=12000,
            end_ms=15000,
        )
    with write_transaction(project.db):
        record_citation(
            project,
            subject_kind="cell_value",
            subject_ref=refs[row],
            spans=[{"span_id": span["id"]}],
            sheet_id=sheet,
            row_id=row,
            column_id=column,
        )
    tools = ProjectQATools(project, turn, store)
    cid = tools.search_cells("NEEDLE", sheet)["hits"][0]["citation_id"]
    monkeypatch.setattr(
        project, "get_values_with_refs", lambda *a, **kw: pytest.fail("whole cell load")
    )
    opened = tools.open_source(cid)
    [prepared] = [p for p in opened["passages"] if p["kind"] == "prepared_evidence"]
    assert (prepared["start_ms"], prepared["end_ms"]) == (12000, 15000)
    assert prepared["text"] == "NEEDLE decisive passage"
    assert prepared["citation_id"] in tools.citation_ids


def test_file_scope_reads_current_prepared_text_and_finds_it(make_project):

    project = make_project("Prepared")
    sheet = project.add_sheet("Documents")
    source_column = project.add_column(sheet, "Document", type="file")
    text_column = project.add_column(sheet, "Text result")
    blob = project.add_blob(
        b"fixture pdf", filename="document.pdf", mime="application/pdf"
    )
    [row] = project.add_rows(
        sheet,
        [{"Document": media_cell(blob, filename="document.pdf")}],
        {"Document": source_column},
    )
    prepared_text = (
        "opening " * 10_000 + "NEEDLE decisive prepared evidence." + " ending" * 2_000
    )
    project.apply_edits(
        [
            {
                "row_id": row,
                "column_id": text_column,
                "value": prepared_text,
            }
        ]
    )
    _, refs = project.get_values_with_refs(sheet, text_column, [row])
    with write_transaction(project.db):
        artifact = record_source_artifact(
            project,
            artifact_kind="ocr_document",
            media_type="application/pdf",
            blob_hash=blob,
            source_sheet_id=sheet,
            source_row_id=row,
            source_column_id=source_column,
            title="document.pdf",
        )
    with write_transaction(project.db):
        span = record_source_span(
            project,
            artifact_id=artifact["id"],
            span_kind="page",
            quote="A grounded page quote outside this search passage.",
            page_start=1,
            page_end=1,
        )
    with write_transaction(project.db):
        record_citation(
            project,
            subject_kind="cell_value",
            subject_ref=refs[row],
            spans=[{"span_id": span["id"]}],
            sheet_id=sheet,
            row_id=row,
            column_id=text_column,
        )
    store = ProjectQAStore(project)
    thread = store.create_thread(title="Ask")
    turn = store.submit_turn(
        thread["id"],
        request_id="file",
        question="Read the document",
        scope={
            "kind": "sources",
            "sources": [
                {
                    "kind": "file",
                    "sheet_id": sheet,
                    "row_id": row,
                    "column_id": source_column,
                }
            ],
        },
    )
    tools = ProjectQATools(project, turn, store)
    drain_index(project)
    [hit] = tools.search_cells("NEEDLE", sheet)["hits"]
    assert hit["column_id"] == source_column
    citation_id = hit["citation_id"]

    opened = tools.open_source(citation_id)

    assert opened["needs_preparation"] is False
    assert "NEEDLE decisive prepared evidence." in opened["passages"][0]["text"]
    assert opened["range"]["start"] > 50_000
    assert all(part["kind"] != "prepared_evidence" for part in opened["passages"])
    found = tools.find_in_source(citation_id, "NEEDLE")
    assert "NEEDLE decisive prepared evidence." in found["matches"][0]["text"]
    resolved = resolve_citation(project, turn["thread_id"], citation_id)
    assert resolved["status"] == "current"
    assert resolved["target"] is not None
    assert {
        key: resolved["target"][key]
        for key in ("kind", "sheet_id", "row_id", "column_id")
    } == {
        "kind": "cell",
        "sheet_id": sheet,
        "row_id": row,
        "column_id": text_column,
    }
    resolved_range = resolved["target"]["text_range"]
    assert prepared_text[resolved_range["start"] : resolved_range["end"]] == ("NEEDLE")
    with pytest.raises(ProjectQAScopeError, match="file scope"):
        tools.read_rows(sheet, [row], [text_column])

    project.apply_edits(
        [{"row_id": row, "column_id": text_column, "value": "replacement"}]
    )
    stale_output = tools.open_source(citation_id)
    assert stale_output["source_changed"] is True
    assert stale_output["needs_preparation"] is True
    assert all(part["kind"] != "prepared_evidence" for part in stale_output["passages"])
    assert (
        resolve_citation(project, turn["thread_id"], citation_id)["status"] == "changed"
    )

    project.apply_edits(
        [
            {
                "row_id": row,
                "column_id": source_column,
                "value": media_cell(
                    project.add_blob(
                        b"replacement pdf",
                        filename="replacement.pdf",
                        mime="application/pdf",
                    ),
                    filename="replacement.pdf",
                ),
            }
        ]
    )
    stale_file = tools.open_source(citation_id)
    assert stale_file["needs_preparation"] is True
    assert all(part["kind"] != "prepared_evidence" for part in stale_file["passages"])


# --- source limits ---


def test_read_rows_clips_each_value_without_stopping_later_rows(make_project):
    project = make_project("Source limits")
    sheet = project.add_sheet("Documents")
    column = project.add_column(sheet, "Text")
    rows = project.add_rows(
        sheet,
        [{"Text": "a" * 4_000}, {"Text": "b" * 4_000}],
        {"Text": column},
    )
    store = ProjectQAStore(project)
    thread = store.create_thread(title="Ask")
    turn = store.submit_turn(thread["id"], request_id="rows", question="Read")

    result = ProjectQATools(project, turn, store).read_rows(sheet, rows, [column])

    assert [row["row_id"] for row in result["rows"]] == rows
    assert all(row["cells"][0]["truncated"] for row in result["rows"])
    assert result["observation_truncated"] is False


# --- search integrity ---


def _response(
    sheet_id: int,
    row_id: int,
    column_id: int,
    revision: int,
    *,
    locator: SourceLocator | None = None,
    dependencies: tuple[BindingRevision, ...] | None = None,
    mode: Literal["lexical", "semantic"] = "lexical",
) -> SearchResponse:
    hit = LocatedHit(
        part=IndexedPart(
            part_id="part:oldneedle",
            source=SourceRevision(
                source_id=f"cell:{row_id}:{column_id}",
                source_kind="text",
                version_kind="retrieval_revision",
                version_value=revision,
            ),
            producer_slice_id="slice:text-v1",
            part_kind="text",
            locator=locator or SourceLocator(kind="text"),
        ),
        binding=ScopedBinding(
            binding_id="binding:oldneedle",
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=column_id,
            dependencies=dependencies
            or (
                BindingRevision(
                    row_id=row_id,
                    column_id=column_id,
                    retrieval_revision=revision,
                ),
            ),
        ),
        contributions=(
            RetrievalContribution(method=mode, channel="text", rank=1, score=1.0),
        ),
        rank_score=1.0,
        precision="source_unit",
        snippet="oldneedle",
    )
    return SearchResponse(
        results=(ResultGroup(group_key="source:text", primary=hit),),
        coverage=SearchCoverage(
            requested_mode=mode,
            effective_modes=(mode,),
            state="complete",
            searched_channels=("text",),
            is_top_k=True,
            truncated=False,
        ),
    )


def _ask_project(make_project):
    project = make_project("Race")
    sheet = project.add_sheet("Documents")
    column = project.add_column(sheet, "Text")
    [row] = project.add_rows(sheet, [{"Text": "oldneedle"}], {"Text": column})
    store = ProjectQAStore(project)
    thread = store.create_thread(title="Ask")
    turn = store.submit_turn(thread["id"], request_id="one", question="Find it")
    revision = int(
        project.db.execute(
            "SELECT retrieval_revision FROM current_cells "
            "WHERE row_id=? AND column_id=?",
            (row, column),
        ).fetchone()[0]
    )
    return project, store, turn, sheet, row, column, revision


def test_keyword_search_does_not_pair_stale_excerpt_with_new_value_ref(
    make_project, monkeypatch
):
    project, store, turn, sheet, row, column, revision = _ask_project(make_project)

    def search_then_edit(_project, request, **_kwargs):
        assert isinstance(request.query, TextQuery)
        project.apply_edits(
            [{"row_id": row, "column_id": column, "value": "replacement"}]
        )
        return _response(sheet, row, column, revision)

    monkeypatch.setattr(retrieval_service, "search_with_encoding", search_then_edit)
    result = ProjectQATools(project, turn, store).search_cells("oldneedle", sheet)
    assert result["hits"] == []
    assert result["partial"] is True
    assert result["coverage"]["reason"] == "source_changed"
    assert (
        project.db.execute(
            "SELECT count(*) FROM project_qa_citations WHERE turn_id=?",
            (turn["id"],),
        ).fetchone()[0]
        == 0
    )


def test_keyword_search_mints_citation_after_dependency_recheck(
    make_project, monkeypatch
):
    project, store, turn, sheet, row, column, revision = _ask_project(make_project)
    monkeypatch.setattr(
        retrieval_service,
        "search_with_encoding",
        lambda _project, _request, **_kwargs: _response(sheet, row, column, revision),
    )
    result = ProjectQATools(project, turn, store).search_cells("oldneedle", sheet)
    [hit] = result["hits"]
    citation = store.get_citation(hit["citation_id"])
    assert citation["source_kind"] == "cell"
    assert citation["locator"]["sheet_id"] == sheet
    assert citation["locator"]["row_id"] == row
    assert citation["locator"]["column_id"] == column
    assert citation["locator"]["retrieval_source"] == {
        "source_id": f"cell:{row}:{column}",
        "source_kind": "text",
        "version_kind": "retrieval_revision",
        "version_value": revision,
    }

    def whole_source_read(*_args, **_kwargs):
        raise AssertionError("citation resolution loaded the whole source")

    monkeypatch.setattr(project, "get_values_with_refs", whole_source_read)
    assert (
        resolve_citation(project, turn["thread_id"], hit["citation_id"])["status"]
        == "current"
    )
    assert result["partial"] is False


def test_semantic_search_uses_shared_runtime_and_mints_a_citation(
    make_project, monkeypatch
):
    project, store, turn, sheet, row, column, revision = _ask_project(make_project)
    runtime = object()
    observed = []

    def semantic_search(_project, request, **kwargs):
        observed.append((request.mode, kwargs["runtime"]))
        return _response(sheet, row, column, revision, mode="semantic")

    monkeypatch.setattr(retrieval_service, "search_with_encoding", semantic_search)
    result = ProjectQATools(
        project,
        turn,
        store,
        embedding_runtime=runtime,
        setup_scope="workspace",
    ).search_cells("oldneedle", sheet, mode="semantic")

    assert observed == [("semantic", runtime)]
    [hit] = result["hits"]
    assert store.get_citation(hit["citation_id"])["source_kind"] == "cell"
    assert result["coverage"]["requested_mode"] == "semantic"
    assert result["partial"] is False


def test_located_media_hit_mints_navigable_evidence_citation(make_project, monkeypatch):

    project, store, turn, sheet, row, column, _revision = _ask_project(make_project)
    _, refs = project.get_values_with_refs(sheet, column, [row])
    with write_transaction(project.db):
        artifact = record_source_artifact(
            project,
            artifact_kind="video",
            media_type="video/mp4",
            source_sheet_id=sheet,
            source_row_id=row,
            source_column_id=column,
        )
    with write_transaction(project.db):
        span = record_source_span(
            project,
            artifact_id=artifact["id"],
            span_kind="video_clip",
            page_start=2,
            page_end=2,
            start_ms=4250,
            end_ms=9500,
        )
    with write_transaction(project.db):
        link = record_citation(
            project,
            subject_kind="cell_value",
            subject_ref=refs[row],
            spans=[{"span_id": span["id"]}],
            sheet_id=sheet,
            row_id=row,
            column_id=column,
        )
    revision = int(
        project.db.execute(
            "SELECT retrieval_revision FROM current_cells "
            "WHERE row_id=? AND column_id=?",
            (row, column),
        ).fetchone()[0]
    )
    located = SourceLocator(
        kind="video_clip",
        pages=PageRange(2, 2),
        time=TimeRange(4.25, 9.5),
        source_span_ids=(span["stable_id"],),
    )
    monkeypatch.setattr(
        retrieval_service,
        "search_with_encoding",
        lambda _project, _request, **_kwargs: _response(
            sheet, row, column, revision, locator=located
        ),
    )
    result = ProjectQATools(project, turn, store).search_cells("oldneedle", sheet)
    citation = store.get_citation(result["hits"][0]["citation_id"])
    assert citation["source_kind"] == "citation"
    assert citation["locator"]["citation_id"] == link["stable_id"]
    assert citation["locator"]["artifact_id"] == artifact["stable_id"]
    assert citation["locator"]["span_id"] == span["stable_id"]
    assert citation["locator"]["location"]["time"] == {
        "start_seconds": 4.25,
        "end_seconds": 9.5,
    }


def test_page_time_hit_without_evidence_keeps_document_navigation(
    make_project, monkeypatch
):
    project, store, turn, sheet, row, column, revision = _ask_project(make_project)
    located = SourceLocator(
        kind="video_clip",
        pages=PageRange(3, 3),
        time=TimeRange(12.5, 18.0),
    )
    monkeypatch.setattr(
        retrieval_service,
        "search_with_encoding",
        lambda _project, _request, **_kwargs: _response(
            sheet, row, column, revision, locator=located
        ),
    )
    [hit] = ProjectQATools(project, turn, store).search_cells("oldneedle", sheet)[
        "hits"
    ]
    citation = store.get_citation(hit["citation_id"])
    assert citation["source_kind"] == "cell"

    current = resolve_citation(project, turn["thread_id"], hit["citation_id"])
    assert current["status"] == "current"
    typed = AskCitation.model_validate(current)
    assert typed.target is not None
    assert current["target"]["navigation"] == {
        "kind": "document",
        "sheet_id": sheet,
        "row_id": row,
        "column_id": column,
        "page": 3,
        "time_seconds": 12.5,
    }

    project.apply_edits([{"row_id": row, "column_id": column, "value": "replacement"}])
    changed = resolve_citation(project, turn["thread_id"], hit["citation_id"])
    assert changed["status"] == "changed"
    assert "navigation" not in changed["target"]


def test_citation_becomes_stale_when_any_binding_dependency_changes(
    make_project, monkeypatch
):
    project, store, turn, sheet, row, column, revision = _ask_project(make_project)
    context_column = project.add_column(sheet, "Context")
    project.apply_edits(
        [{"row_id": row, "column_id": context_column, "value": "supporting"}]
    )
    context_revision = int(
        project.db.execute(
            "SELECT retrieval_revision FROM current_cells "
            "WHERE row_id=? AND column_id=?",
            (row, context_column),
        ).fetchone()[0]
    )
    dependencies = (
        BindingRevision(row, column, revision),
        BindingRevision(row, context_column, context_revision),
    )
    monkeypatch.setattr(
        retrieval_service,
        "search_with_encoding",
        lambda _project, _request, **_kwargs: _response(
            sheet,
            row,
            column,
            revision,
            locator=SourceLocator(kind="document", pages=PageRange(1, 1)),
            dependencies=dependencies,
        ),
    )
    [hit] = ProjectQATools(project, turn, store).search_cells("oldneedle", sheet)[
        "hits"
    ]
    assert (
        resolve_citation(project, turn["thread_id"], hit["citation_id"])["status"]
        == "current"
    )

    project.apply_edits(
        [{"row_id": row, "column_id": context_column, "value": "changed"}]
    )
    changed = resolve_citation(project, turn["thread_id"], hit["citation_id"])
    assert changed["status"] == "changed"
    assert "navigation" not in changed["target"]


# --- search purchase ---


class RecordingPolicy:
    def __init__(self, *, fail_record=False):
        self.fail_record = fail_record
        self.admissions = []
        self.observations = []
        self.failures = []

    def admit_purchase(self, request):
        self.admissions.append(request)
        return PurchaseAdmission("metered", "operation-test")

    def record_purchase(self, request, admission, observation):
        self.observations.append(observation)
        if self.fail_record:
            raise RuntimeError("private accounting failure details")

    def fail_purchase(self, request, admission, error):
        self.failures.append((request, admission, error))


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_when", ["before", "after_response"])
async def test_search_cancellation_stops_new_work_but_keeps_returned_usage(cancel_when):
    calls = []
    cancelled = cancel_when == "before"
    policy = RecordingPolicy()

    def transport(request):
        nonlocal cancelled
        calls.append(request)
        cancelled = True
        return httpx.Response(
            200, json={"results": [], "costDollars": {"total": 0.007}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        service = SearchService(
            preference="exa",
            effective_keys={"exa": "fake-key"},
            credential_source="platform_key",
            http=http,
        )
        operation = _search_with_purchase(
            service,
            "query",
            policy=policy,
            dispatch=TransientDispatch(lambda: cancelled),
        )
        if cancel_when == "before":
            with pytest.raises(asyncio.CancelledError):
                await operation
            assert calls == policy.admissions == policy.observations == []
            assert policy.failures == []
        else:
            result = await operation
            assert result.usage.provider_cost_usd == 0.007
            assert len(calls) == len(policy.admissions) == len(policy.observations) == 1
            assert policy.observations[0].provider_cost_micro == 7000
            assert policy.failures == []


@pytest.mark.asyncio
async def test_search_accounting_failure_retains_result_without_retry(caplog):
    calls = []
    policy = RecordingPolicy(fail_record=True)

    def transport(request):
        calls.append(request)
        return httpx.Response(
            200, json={"results": [], "costDollars": {"total": 0.007}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        service = SearchService(
            preference="exa",
            effective_keys={"exa": "fake-key"},
            credential_source="platform_key",
            http=http,
        )
        result = await _search_with_purchase(
            service,
            "private search query",
            policy=policy,
            dispatch=STANDALONE,
        )
    assert result.usage.provider_cost_usd == 0.007
    assert len(calls) == len(policy.admissions) == len(policy.observations) == 1
    assert len(policy.failures) == 1
    request, admission, error = policy.failures[0]
    assert request is policy.admissions[0]
    assert admission.operation_id == "operation-test"
    assert isinstance(error, PurchaseRecordingError)
    assert error.provider_result is result
    assert error.observation is policy.observations[0]
    assert error.observation.provider_cost_micro == 7000
    assert "purchase accounting could not be recorded" in caplog.text
    assert "private search query" not in caplog.text
    assert "private accounting failure details" not in caplog.text


@pytest.mark.asyncio
async def test_search_without_accounting_owner_refuses_before_egress():
    calls = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: calls.append(request))
    ) as http:
        service = SearchService(
            preference="exa",
            effective_keys={"exa": "fake-key"},
            credential_source="platform_key",
            http=http,
        )
        with pytest.raises(PurchasePolicyRequired):
            await _search_with_purchase(
                service,
                "query",
                policy=None,
                dispatch=STANDALONE,
            )
    assert calls == []


# --- contracts ---


def test_turn_snapshots_existing_source_scope_and_options():
    request = AskTurnRequest.model_validate(
        {
            "request_id": "first-question",
            "question": "What changed?",
            "scope": {
                "kind": "sources",
                "sources": [
                    {"kind": "rows", "sheet_id": 1, "row_ids": [2, 3]},
                    {"kind": "file", "sheet_id": 4, "row_id": 5, "column_id": 6},
                ],
            },
            "web": False,
            "suggest_actions": True,
        }
    )
    assert request.scope.kind == "sources"
    assert request.model is None
    assert request.web is False


@pytest.mark.parametrize(
    "extra", [{"confirmed": True}, {"execute": True}, {"project_id": "other"}]
)
def test_question_cannot_grant_write_authority_or_change_project(extra):
    with pytest.raises(ValidationError):
        AskTurnRequest.model_validate(
            {
                "request_id": "question",
                "question": "Read this",
                "scope": {"kind": "project"},
                **extra,
            }
        )


@pytest.mark.parametrize(
    "scope",
    [
        {"kind": "sources", "sources": []},
        {"kind": "project", "sources": [{"kind": "sheet", "sheet_id": 1}]},
        {"kind": "sources", "sources": [{"kind": "file", "path": "/secret"}]},
        {
            "kind": "sources",
            "sources": [{"kind": "rows", "sheet_id": 1, "row_ids": [1, 1]}],
        },
        {
            "kind": "sources",
            "sources": [
                {"kind": "rows", "sheet_id": 1, "row_ids": list(range(1, 602))},
                {"kind": "rows", "sheet_id": 2, "row_ids": list(range(1, 602))},
            ],
        },
    ],
)
def test_invalid_or_overlarge_source_scope_is_not_silently_broadened(scope):
    with pytest.raises(ValidationError):
        AskScope.model_validate(scope)


# --- capabilities ---


class _Model:
    def __init__(self, context_window: int | None):
        self.context_window = context_window


def _skill(name: str = "source-check") -> dict[str, object]:
    return {
        "name": name,
        "revision": 7,
        "content": (
            "---\n"
            f"name: {name}\n"
            "description: Check claims against exact source material.\n"
            "---\n\n"
            "Inspect the cited passage before accepting a claim.\n"
        ),
    }


@pytest.mark.anyio
async def test_composition_materializes_a_read_only_progressive_skill_library() -> None:
    library_root: Path | None = None

    with compose_project_qa_capabilities(_Model(1_000_000), [_skill()]) as capabilities:
        assert [type(capability) for capability in capabilities] == [
            Skills,
            TieredCompaction,
        ]
        skills = capabilities[0]
        assert isinstance(skills, Skills)
        assert isinstance(skills.workspace, ReadOnlyWorkspace)
        assert skills.workspace.read_only is True
        assert skills.workspace.ref is not None
        library_root = Path(skills.workspace.ref.id)
        assert (library_root / "skills/source-check/SKILL.md").read_text() == _skill()[
            "content"
        ]

        with pytest.raises(WorkspaceReadOnlyError):
            await skills.workspace.write_text("skills/source-check/SKILL.md", "changed")
        with pytest.raises(WorkspaceReadOnlyError):
            await skills.workspace.run(["pwd"])

        compaction = capabilities[1]
        assert isinstance(compaction, TieredCompaction)
        assert compaction.target_fraction == 0.75
        [summary] = compaction.tiers
        assert isinstance(summary, SummarizingCompaction)
        assert summary.model is None
        assert summary.receipts is True
        assert "source and citation IDs" in summary.summary_prompt
        assert "pending action and tool-call references" in summary.summary_prompt

    assert library_root is not None
    assert not library_root.exists()


def test_composition_rejects_unsafe_or_ambiguous_skill_snapshots() -> None:
    with pytest.raises(ProjectQACapabilityError, match="canonical"):
        with compose_project_qa_capabilities(_Model(1_000_000), [_skill("../escape")]):
            pass

    duplicate = _skill()
    with pytest.raises(ProjectQACapabilityError, match="duplicate"):
        with compose_project_qa_capabilities(
            _Model(1_000_000), [duplicate, dict(duplicate)]
        ):
            pass

    invalid_revision = _skill()
    invalid_revision["revision"] = "7"
    with pytest.raises(ProjectQACapabilityError, match="revision"):
        with compose_project_qa_capabilities(_Model(1_000_000), [invalid_revision]):
            pass


def test_compaction_requires_known_capacity_or_explicit_disable() -> None:
    with pytest.raises(ProjectQACapabilityError, match="context capacity"):
        with compose_project_qa_capabilities(_Model(None), []):
            pass

    with compose_project_qa_capabilities(
        _Model(None), [_skill()], enable_compaction=False
    ) as capabilities:
        assert [type(capability) for capability in capabilities] == [Skills]
