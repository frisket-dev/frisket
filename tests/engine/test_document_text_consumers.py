from __future__ import annotations

from contextlib import closing

import pytest

from frisket.engine.executor.find_plan import FindOperation
from frisket.engine.executor.map_find_source import resolve_find_sources
from frisket.contracts.action import ActionError
from frisket.engine.store import Project
from frisket.engine.store.document_text import ensure_document_text_role
from frisket.engine.store.evidence import (
    record_evidence_link,
    record_source_artifact,
    record_source_span,
)
from frisket.engine.store.prepared_content import (
    PreparedContentStore,
    PreparedPageDraft,
)
from frisket.engine.store.prepared_readiness import prepared_source_readiness_ctes
from frisket.retrieval.source_adapter import resolve_cell_blob_source
from frisket.server.services import search_preparation_planning as planning
from frisket.server.services.project_qa_sources import (
    SourceUnavailableError,
    read_source_text,
    read_source_with_prepared,
)


def _text_output(project, source_cell, text_column, blob, pages, *, page=None):
    with project.db:
        op = project.append_op("test.document_text", commit=False)
        artifact = record_source_artifact(
            project,
            artifact_kind="document",
            media_type="application/pdf",
            blob_hash=blob,
            page_count=len(pages),
            source_sheet_id=source_cell[0],
            source_row_id=source_cell[1],
            source_column_id=source_cell[2],
        )
        ref = PreparedContentStore(project).stage_reference(
            source_artifact_id=artifact["id"],
            producing_op_id=op,
            pages=[
                PreparedPageDraft(index, text) for index, text in enumerate(pages, 1)
            ],
            page_number=page,
        )
    project.apply_edits(
        [{"row_id": source_cell[1], "column_id": text_column, "value": ref}]
    )
    _, refs = project.get_values_with_refs(
        source_cell[0], text_column, [source_cell[1]]
    )
    span = record_source_span(
        project,
        artifact_id=artifact["id"],
        span_kind="page_range",
        page_start=page or 1,
        page_end=page or len(pages),
        quote=pages[page - 1] if page else "\n\n".join(pages),
    )
    link = record_evidence_link(
        project,
        subject_kind="cell_value",
        subject_ref=refs[source_cell[1]],
        spans=[{"span_id": span["id"]}],
        sheet_id=source_cell[0],
        row_id=source_cell[1],
        column_id=text_column,
        link_role="primary_support",
    )
    return ref.ref_id, artifact["id"], span, link


def _document(project, *, page=None):
    sheet = project.add_sheet("Documents")
    source = project.add_column(sheet, "File", type="file")
    text = project.add_column(sheet, "Text", type="text")
    other = project.add_column(sheet, "Comparison", type="text")
    blob = project.add_blob(b"two-page fixture", mime="application/pdf")
    [row] = project.add_rows(
        sheet,
        [{"File": {"blob": blob, **({"page": page} if page else {})}}],
        {"File": source},
    )
    ensure_document_text_role(
        project, source_column_id=source, text_column_id=text, commit=True
    )
    cell = (sheet, row, source)
    chosen = _text_output(
        project, cell, text, blob, ["chosen first", "chosen second"], page=page
    )
    alternative = _text_output(
        project, cell, other, blob, ["newest alternative", "wrong second"], page=page
    )
    return cell, text, other, blob, chosen, alternative


@pytest.mark.parametrize("page", [None, 2])
def test_ask_uses_designated_text_and_matching_page_not_newest_evidence(tmp_path, page):
    with closing(Project.create(tmp_path / "ask-role.frisket")) as project:
        cell, text, _other, _blob, chosen, _alternative = _document(project, page=page)

        target, _source, prepared = read_source_with_prepared(project, cell)

        assert target == (cell[0], cell[1], text)
        assert prepared is not None
        assert prepared["prepared_value_ref"]["prepared_ref_id"] == chosen[0]
        assert read_source_text(project, target)["text"] == (
            "chosen second" if page else "chosen first\n\nchosen second"
        )


def test_stale_manual_text_is_unavailable_without_alternative_fallback(tmp_path):
    with closing(Project.create(tmp_path / "stale-manual.frisket")) as project:
        cell, text, _other, _blob, _chosen, _alternative = _document(project)
        project.apply_edits(
            [{"row_id": cell[1], "column_id": text, "value": "manual correction"}]
        )
        target, _source, prepared = read_source_with_prepared(project, cell)
        assert prepared is not None
        assert read_source_text(project, target)["text"] == "manual correction"
        source = resolve_cell_blob_source(
            project, sheet_id=cell[0], row_id=cell[1], column_id=cell[2]
        )
        assert planning._has_current_result(project, source, "native_pdf")
        project.apply_edits(
            [
                {
                    "row_id": cell[1],
                    "column_id": cell[2],
                    "value": {
                        "blob": project.add_blob(
                            b"changed file", mime="application/pdf"
                        )
                    },
                }
            ]
        )

        with pytest.raises(SourceUnavailableError):
            read_source_with_prepared(project, cell)
        source = resolve_cell_blob_source(
            project, sheet_id=cell[0], row_id=cell[1], column_id=cell[2]
        )
        assert not planning._has_current_result(project, source, "native_pdf")


@pytest.mark.parametrize("page", [None, 2])
def test_preparation_readiness_and_preview_follow_only_designated_text(tmp_path, page):
    with closing(Project.create(tmp_path / "planning-role.frisket")) as project:
        cell, _text, _other, blob, chosen, _alternative = _document(project, page=page)
        source = resolve_cell_blob_source(
            project, sheet_id=cell[0], row_id=cell[1], column_id=cell[2]
        )
        assert source is not None
        assert planning._has_current_result(project, source, "native_pdf")
        prepared = planning._current_prepared_result(
            project,
            sheet_id=cell[0],
            row_id=cell[1],
            column_id=cell[2],
            blob_hash=blob,
            page_number=page,
        )
        assert prepared["prepared_ref_id"] == chosen[0]
        samples = project.db.execute(
            "WITH scope(sheet_id,row_id) AS (VALUES (?,?)), "
            + prepared_source_readiness_ctes(scope_relation="scope")
            + " SELECT DISTINCT prepared_ref_id FROM current_prepared_sources "
            "WHERE source_column_id=?",
            (*cell[:2], cell[2]),
        ).fetchall()
        assert [row[0] for row in samples] == [chosen[0]]
        item = {
            "row_id": cell[1],
            "column_id": cell[2],
            "blob_hash": blob,
            "page_number": page,
        }
        assert planning.sample_pdf(project, cell[0], item, page or 1)[:3] == (
            "chosen second" if page else "chosen first",
            2,
            "prepared",
        )


@pytest.mark.parametrize("grounded", [False, True])
def test_find_file_uses_selected_text_with_ordinary_text_publication_coordinates(
    tmp_path, grounded
):
    with closing(Project.create(tmp_path / "find-role.frisket")) as project:
        cell, text, _other, _blob, _chosen, _alternative = _document(project, page=2)
        if grounded:
            span = record_source_span(
                project,
                artifact_id=_chosen[1],
                span_kind="region",
                page_start=2,
                page_end=2,
                quote="chosen second",
                bbox=[
                    {
                        "x0": 0.1,
                        "y0": 0.1,
                        "x1": 0.7,
                        "y1": 0.2,
                        "space": "page_normalized",
                    }
                ],
            )
            _, refs = project.get_values_with_refs(cell[0], text, [cell[1]])
            record_evidence_link(
                project,
                subject_kind="cell",
                subject_ref=refs[cell[1]],
                spans=[{"span_id": span["id"]}],
                sheet_id=cell[0],
                row_id=cell[1],
                column_id=text,
                link_role="media_ocr_grounding",
                producer={"action_kind": "media.read_pdf_text"},
            )
            _, alternatives = project.get_values_with_refs(cell[0], _other, [cell[1]])
            record_evidence_link(
                project,
                subject_kind="cell",
                subject_ref=alternatives[cell[1]],
                spans=[{"span_id": _alternative[2]["id"]}],
                sheet_id=cell[0],
                row_id=cell[1],
                column_id=_other,
                link_role="media_ocr_grounding",
                producer={"action_kind": "media.ocr"},
            )
        params = FindOperation(
            action_kind="map.find",
            request={},
            sheet_id=cell[0],
            row_ids=[cell[1]],
            source_column="File",
            model="test/model",
            instruction="Find chosen",
            fields=[],
            target_sheet_name="Findings",
            output_names={"match": "Match"},
        )

        sources = resolve_find_sources(project, params)

        assert isinstance(sources, tuple), sources
        assert len(sources) == 1
        assert sources[0].column_id == text
        assert sources[0].column_type == "text"
        assert [unit.text for unit in sources[0].units] == ["chosen second"]
        assert sources[0].value_ref["prepared_ref_id"] == _chosen[0]
        if grounded:
            assert sources[0].artifact_id == _chosen[1]
            assert sources[0].units[0].span_ids == (span["id"],)
        else:
            assert all(span_id < 0 for span_id in sources[0].units[0].span_ids)

        project.apply_edits(
            [
                {
                    "row_id": cell[1],
                    "column_id": cell[2],
                    "value": {
                        "blob": project.add_blob(
                            b"changed file", mime="application/pdf"
                        ),
                        "page": 2,
                    },
                }
            ]
        )
        stale = resolve_find_sources(project, params)
        assert isinstance(stale, ActionError)
        assert stale.code == "source_not_addressable"
