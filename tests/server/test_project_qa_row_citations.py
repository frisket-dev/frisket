from __future__ import annotations

import frisket.retrieval.service as retrieval_service
import pytest

from frisket.engine.store import Project
from frisket.engine.store.evidence import (
    record_evidence_link,
    record_source_artifact,
    record_source_span,
)
from frisket.engine.store.media_blobs import media_cell
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.engine.store.runs import RunResultStore
from frisket.retrieval.contracts import (
    BindingRevision,
    PageRange,
    SourceLocator,
    TimeRange,
)
from frisket.search import drain_index
from frisket.server.services.project_qa_citations import resolve_citation
from frisket.server.services.project_qa_tools import ProjectQAScopeError, ProjectQATools
from tests.server.test_project_qa_search_integrity import _response

from helpers import write_claimed_test_results


def _turn(project: Project, *, scope: dict | None = None):
    store = ProjectQAStore(project)
    thread = store.create_thread(title="Ask", scope=scope)
    turn = store.submit_turn(
        thread["id"],
        request_id="one",
        question="Find the cited passage",
        scope=scope,
    )
    return store, turn


def _cell(read: dict, name: str) -> dict:
    return next(
        cell for row in read["rows"] for cell in row["cells"] if cell["name"] == name
    )


def _seed_grounded_ocr(project: Project) -> dict[str, object]:
    sheet = project.add_sheet("Scans")
    source_column = project.add_column(sheet, "document", type="file")
    text_column = project.add_column(sheet, "ocr_text", type="text", ai_generated=True)
    blocks_column = project.add_column(
        sheet, "ocr_blocks", type="json", ai_generated=True
    )
    json_column = project.add_column(sheet, "review_metadata", type="json")
    blob = project.add_blob(
        b"%PDF-1.4\n% grounded Ask fixture\n",
        filename="harbor.pdf",
        mime="application/pdf",
    )
    ordinary_json = {"reviewed": True, "tags": ["harbor", "approved"]}
    [row] = project.add_rows(
        sheet,
        [
            {
                "document": media_cell(
                    blob, mime="application/pdf", filename="harbor.pdf"
                ),
                "review_metadata": ordinary_json,
            }
        ],
        {"document": source_column, "review_metadata": json_column},
    )

    output_names = {"text": "ocr_text", "blocks": "ocr_blocks"}
    op = project.append_op(
        "media.ocr",
        {"action_id": "media.ocr", "output_names": output_names},
        label="OCR",
    )
    runs = RunResultStore(project)
    run = runs.start_run(
        op,
        sheet,
        "media.ocr",
        params={"output_names": output_names},
        total_rows=1,
        row_ids=[row],
    )
    text = "The harbor needs a turquoise lighthouse."
    blocks = [
        {
            "page": 2,
            "engine": "rapidocr",
            "blocks": [
                {
                    "text": text,
                    "bbox": [[108, 310], [1140, 312], [1140, 364], [108, 361]],
                    "score": 0.9692,
                }
            ],
        }
    ]
    write_claimed_test_results(
        project,
        run,
        [
            {"row_id": row, "column_id": text_column, "value": text},
            {"row_id": row, "column_id": blocks_column, "value": blocks},
        ],
    )
    runs.finish_run(run)

    artifact = record_source_artifact(
        project,
        artifact_kind="ocr_document",
        media_type="application/pdf",
        blob_hash=blob,
        filename="harbor.pdf",
        page_count=2,
        source_sheet_id=sheet,
        source_row_id=row,
        source_column_id=source_column,
        metadata={"engine": "rapidocr", "page_images": {}},
    )
    span = record_source_span(
        project,
        artifact_id=int(artifact["id"]),
        span_kind="region",
        page_start=2,
        page_end=2,
        quote=text,
        bbox=[{"x0": 0.1, "y0": 0.3, "x1": 0.8, "y1": 0.4}],
    )
    _values, refs = project.get_values_with_refs(sheet, text_column, row_ids=[row])
    link = record_evidence_link(
        project,
        subject_kind="cell",
        subject_ref=refs[row],
        spans=[{"span_id": int(span["id"])}],
        sheet_id=sheet,
        row_id=row,
        column_id=text_column,
        run_id=run,
        op_id=op,
        link_role="media_ocr_grounding",
        producer={"action_kind": "media.ocr", "engine": "rapidocr"},
    )
    return {
        "sheet": sheet,
        "row": row,
        "source_column": source_column,
        "text_column": text_column,
        "blocks_column": blocks_column,
        "json_column": json_column,
        "ordinary_json": ordinary_json,
        "blocks": blocks,
        "artifact": artifact,
        "span": span,
        "link": link,
    }


def test_search_then_row_read_keeps_grounded_ocr_sources_and_hides_only_blocks(
    tmp_path,
):
    project = Project.create(tmp_path / "grounded-ocr.frisket", name="Grounded OCR")
    try:
        seeded = _seed_grounded_ocr(project)
        store, turn = _turn(project)
        assert drain_index(project) >= 1
        tools = ProjectQATools(project, turn, store)

        hits = tools.search_cells("turquoise lighthouse", int(seeded["sheet"]))["hits"]
        hit = next(item for item in hits if item["navigation"]["kind"] == "evidence")
        opened = tools.open_source(hit["citation_id"])
        prepared = next(
            passage
            for passage in opened["passages"]
            if passage["kind"] == "prepared_evidence"
        )
        read = tools.read_rows(int(seeded["sheet"]), [int(seeded["row"])])

        names = {cell["name"] for cell in read["rows"][0]["cells"]}
        assert "ocr_blocks" not in names
        assert _cell(read, "review_metadata")["value"] == seeded["ordinary_json"]

        text_cell = _cell(read, "ocr_text")
        assert text_cell["citation_id"] not in {
            hit["citation_id"],
            prepared["citation_id"],
        }
        whole_cell = store.get_citation(text_cell["citation_id"])
        assert whole_cell["source_kind"] == "cell"
        assert (
            not {
                "evidence_link_id",
                "artifact_id",
                "span_id",
                "navigation",
            }
            & whole_cell["locator"].keys()
        )

        source_ids = {source["citation_id"] for source in text_cell["sources"]}
        assert {hit["citation_id"], prepared["citation_id"]} <= source_ids
        assert all(
            set(source) == {"citation_id", "text", "label"}
            and source["text"]
            and source["label"]
            for source in text_cell["sources"]
        )
        assert all(
            not cell["sources"]
            for cell in read["rows"][0]["cells"]
            if cell["column_id"] != seeded["text_column"]
        )

        resolved = resolve_citation(project, turn["thread_id"], hit["citation_id"])
        assert resolved["status"] == "current"
        assert resolved["target"] == {
            "kind": "evidence",
            "sheet_id": seeded["sheet"],
            "row_id": seeded["row"],
            "column_id": seeded["text_column"],
            "evidence_link_id": seeded["link"]["stable_id"],
            "artifact_id": seeded["artifact"]["stable_id"],
            "span_id": seeded["span"]["stable_id"],
        }

        explicit = tools.read_rows(
            int(seeded["sheet"]),
            [int(seeded["row"])],
            [int(seeded["blocks_column"])],
        )
        assert _cell(explicit, "ocr_blocks")["value"] == seeded["blocks"]
    finally:
        project.close()


def test_row_sources_drop_a_semantic_document_hit_when_a_dependency_changes(
    tmp_path, monkeypatch
):
    project = Project.create(tmp_path / "stale-row-source.frisket", name="Stale")
    try:
        sheet = project.add_sheet("Documents")
        text_column = project.add_column(sheet, "Text")
        context_column = project.add_column(sheet, "Context")
        [row] = project.add_rows(
            sheet,
            [{"Text": "oldneedle", "Context": "supporting"}],
            {"Text": text_column, "Context": context_column},
        )
        revisions = {
            int(result["column_id"]): int(result["retrieval_revision"])
            for result in project.db.execute(
                "SELECT column_id,retrieval_revision FROM current_cells WHERE row_id=?",
                (row,),
            )
        }
        dependencies = (
            BindingRevision(row, text_column, revisions[text_column]),
            BindingRevision(row, context_column, revisions[context_column]),
        )
        monkeypatch.setattr(
            retrieval_service,
            "search_with_encoding",
            lambda _project, _request, **_kwargs: _response(
                sheet,
                row,
                text_column,
                revisions[text_column],
                locator=SourceLocator(
                    kind="video_clip",
                    pages=PageRange(3, 3),
                    time=TimeRange(12.5, 18.0),
                ),
                dependencies=dependencies,
                mode="semantic",
            ),
        )
        store, turn = _turn(project)
        tools = ProjectQATools(project, turn, store, embedding_runtime=object())

        [hit] = tools.search_cells("oldneedle", sheet, mode="semantic")["hits"]
        before = _cell(tools.read_rows(sheet, [row], [text_column]), "Text")
        assert hit["citation_id"] in {
            source["citation_id"] for source in before["sources"]
        }
        current = resolve_citation(project, turn["thread_id"], hit["citation_id"])
        assert current["status"] == "current"
        assert current["target"]["navigation"] == {
            "kind": "document",
            "sheet_id": sheet,
            "row_id": row,
            "column_id": text_column,
            "page": 3,
            "time_seconds": 12.5,
        }

        project.apply_edits(
            [{"row_id": row, "column_id": context_column, "value": "changed"}]
        )
        after = _cell(tools.read_rows(sheet, [row], [text_column]), "Text")
        assert hit["citation_id"] not in {
            source["citation_id"] for source in after["sources"]
        }
        changed = resolve_citation(project, turn["thread_id"], hit["citation_id"])
        assert changed["status"] == "changed"
        assert "navigation" not in changed["target"]
    finally:
        project.close()


def test_file_scope_attaches_prepared_search_source_only_to_its_file_cell(tmp_path):
    project = Project.create(tmp_path / "prepared-file-source.frisket", name="Prepared")
    try:
        sheet = project.add_sheet("Documents")
        source_column = project.add_column(sheet, "Document", type="file")
        text_column = project.add_column(sheet, "Prepared text")
        note_column = project.add_column(sheet, "Note")
        blob = project.add_blob(
            b"%PDF-1.4\n% prepared file fixture\n",
            filename="minutes.pdf",
            mime="application/pdf",
        )
        [row] = project.add_rows(
            sheet,
            [
                {
                    "Document": media_cell(
                        blob, mime="application/pdf", filename="minutes.pdf"
                    ),
                    "Prepared text": "The council approved the bluewharf repair.",
                    "Note": "unrelated sibling",
                }
            ],
            {
                "Document": source_column,
                "Prepared text": text_column,
                "Note": note_column,
            },
        )
        artifact = record_source_artifact(
            project,
            artifact_kind="document",
            media_type="application/pdf",
            blob_hash=blob,
            filename="minutes.pdf",
            page_count=2,
            source_sheet_id=sheet,
            source_row_id=row,
            source_column_id=source_column,
        )
        span = record_source_span(
            project,
            artifact_id=int(artifact["id"]),
            span_kind="page",
            page_start=2,
            page_end=2,
            quote="The council approved the bluewharf repair.",
        )
        _values, refs = project.get_values_with_refs(sheet, text_column, row_ids=[row])
        record_evidence_link(
            project,
            subject_kind="cell_value",
            subject_ref=refs[row],
            spans=[{"span_id": int(span["id"])}],
            sheet_id=sheet,
            row_id=row,
            column_id=text_column,
        )
        scope = {
            "kind": "sources",
            "sources": [
                {
                    "kind": "file",
                    "sheet_id": sheet,
                    "row_id": row,
                    "column_id": source_column,
                }
            ],
        }
        store, turn = _turn(project, scope=scope)
        assert drain_index(project) >= 1
        tools = ProjectQATools(project, turn, store)

        [hit] = tools.search_cells("bluewharf repair", sheet)["hits"]
        citation = store.get_citation(hit["citation_id"])
        assert citation["locator"]["column_id"] == source_column
        assert citation["locator"]["prepared_cell"] == {
            "sheet_id": sheet,
            "row_id": row,
            "column_id": text_column,
        }
        opened = tools.open_source(hit["citation_id"])
        read = tools.read_rows(sheet, [row])

        assert [cell["column_id"] for cell in read["rows"][0]["cells"]] == [
            source_column
        ]
        file_cell = _cell(read, "Document")
        source_ids = {source["citation_id"] for source in file_cell["sources"]}
        assert {hit["citation_id"], opened["citation_id"]} <= source_ids
        with pytest.raises(ProjectQAScopeError, match="file scope"):
            tools.read_rows(sheet, [row], [text_column])
    finally:
        project.close()
