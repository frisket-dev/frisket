"""Ask reads complete sources in bounded, versioned, scope-checked pieces."""

from __future__ import annotations

import pytest

from frisket.engine.store import Project
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.server.services.project_qa_tools import ProjectQAScopeError, ProjectQATools


@pytest.fixture
def source(tmp_path):
    project = Project.create(tmp_path / "sources.frisket", name="Sources")
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
    try:
        yield project, store, thread, turn, sheet, column, row
    finally:
        project.close()


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
    project, store, _, turn, _, _, _ = source
    tools = ProjectQATools(project, turn, store)
    found = tools.search_actions("transcribe", limit=3)
    assert 0 < len(found["actions"]) <= 3
    assert all("input_schema" not in item for item in found["actions"])
    assert tools.describe_action(found["actions"][0]["action_id"])["input_schema"]
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


def test_literal_find_crosses_scan_boundary_and_preserves_unicode_offsets(source):
    from frisket.server.services.project_qa_sources import (
        MAX_SCAN_CHARS,
        find_source_text,
    )

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
    from frisket.engine.store.evidence import (
        record_evidence_link,
        record_source_artifact,
        record_source_span,
    )

    project, store, _, turn, sheet, column, row = source
    _, refs = project.get_values_with_refs(sheet, column, [row])
    artifact = record_source_artifact(
        project, artifact_kind="audio", media_type="audio/wav", title="Interview"
    )
    span = record_source_span(
        project,
        artifact_id=artifact["id"],
        span_kind="audio",
        quote="NEEDLE decisive passage",
        start_ms=12000,
        end_ms=15000,
    )
    record_evidence_link(
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


def test_file_scope_reads_current_prepared_text_and_finds_it(tmp_path):
    from frisket.engine.store.evidence import (
        record_evidence_link,
        record_source_artifact,
        record_source_span,
    )
    from frisket.engine.store.media_blobs import media_cell

    project = Project.create(tmp_path / "prepared-file.frisket", name="Prepared")
    try:
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
        project.apply_edits(
            [
                {
                    "row_id": row,
                    "column_id": text_column,
                    "value": "Prepared text with NEEDLE evidence.",
                }
            ]
        )
        _, refs = project.get_values_with_refs(sheet, text_column, [row])
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
        span = record_source_span(
            project,
            artifact_id=artifact["id"],
            span_kind="page",
            quote="Prepared text with NEEDLE evidence.",
            page_start=1,
            page_end=1,
        )
        record_evidence_link(
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
        source = tools.read_rows(sheet, [row], [source_column])
        citation_id = source["rows"][0]["cells"][0]["citation_id"]

        opened = tools.open_source(citation_id)

        assert opened["needs_preparation"] is False
        assert opened["passages"][0]["text"] == "Prepared text with NEEDLE evidence."
        assert any(part["kind"] == "prepared_evidence" for part in opened["passages"])
        found = tools.find_in_source(citation_id, "NEEDLE")
        assert found["matches"][0]["text"] == "Prepared text with NEEDLE evidence."
        with pytest.raises(ProjectQAScopeError, match="file scope"):
            tools.read_rows(sheet, [row], [text_column])

        project.apply_edits(
            [{"row_id": row, "column_id": text_column, "value": "replacement"}]
        )
        stale_output = tools.open_source(citation_id)
        assert stale_output["needs_preparation"] is True
        assert all(
            part["kind"] != "prepared_evidence" for part in stale_output["passages"]
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
        assert all(
            part["kind"] != "prepared_evidence" for part in stale_file["passages"]
        )
    finally:
        project.close()


def test_semantic_hit_is_not_cited_with_new_reference_after_an_edit(
    source, monkeypatch
):
    import frisket.server.services.project_qa_tools as module

    project, store, _, turn, sheet, column, row = source
    tools = ProjectQATools(project, turn, store)

    def racing_search(*args, **kwargs):
        project.apply_edits([{"row_id": row, "column_id": column, "value": "changed"}])
        return {
            "hits": [
                {
                    "row_id": row,
                    "column_id": column,
                    "column_name": "Text",
                    "char_start": 0,
                    "char_end": 7,
                    "text": "opening",
                    "semantic": True,
                }
            ],
            "new_embeddings": 1,
            "coverage": {"complete": True, "semantic": True},
        }

    monkeypatch.setattr(module, "semantic_passage_search", racing_search)
    result = tools.search_cells("needle", sheet, mode="semantic")
    assert result["hits"] == []
    assert result["coverage"]["reason"] == "source_changed"
