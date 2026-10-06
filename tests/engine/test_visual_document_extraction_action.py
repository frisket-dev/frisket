"""Visual extraction admission, preview and table publication share one matcher."""

from contextlib import closing
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.actions.document_extract import DocumentExtractParams
from frisket.actions.document_extraction_types import ExtractionTemplate
from frisket.actions.system import typed_action_for_request
from frisket.engine.executor.document_extraction_read import load_positioned_document
from frisket.engine.executor.table_action import run_typed_create_sheet_action
from frisket.engine.store import Project
from frisket.engine.store.evidence import (
    record_source_artifact,
    record_source_span,
    list_cell_evidence,
    resolve_evidence_viewer,
)
from frisket.server.routes.document_extraction import (
    register_document_extraction_routes,
)
from frisket.server.services.document_extraction import DocumentExtractionService
from frisket.server.workspace import Workspace


def seed(project, count=2, *, repeats=False, engine="tesseract"):
    sheet = project.add_sheet("Documents")
    column = project.add_column(sheet, "document", "file")
    blobs = []
    for index in range(count):
        blob = project.add_blob(
            f"image {index}".encode(), filename=f"form-{index}.png", mime="image/png"
        )
        blobs.append(blob)
        artifact = record_source_artifact(
            project,
            artifact_kind="file",
            blob_hash=blob,
            media_type="image/png",
            metadata={
                "engine": engine,
                "page_images": {"1": {"source_width": 100, "source_height": 100}},
            },
        )
        words = [
            ("NAME", 0.1, 0.1, 0.2),
            (f"Person{index}", 0.1, 0.35, 0.55),
            ("ARRESTED", 0.2, 0.1, 0.3),
        ]
        if index == 0:
            words.append(("X", 0.2, 0.35, 0.4))
        if repeats:
            words.extend(
                [
                    ("NAME", 0.4, 0.1, 0.2),
                    (f"Other{index}", 0.4, 0.35, 0.55),
                    ("ARRESTED", 0.5, 0.1, 0.3),
                ]
            )
        for text, y, x0, x1 in words:
            record_source_span(
                project,
                artifact_id=artifact["id"],
                span_kind="region",
                page_start=1,
                page_end=1,
                bbox=[
                    {
                        "x0": x0,
                        "y0": y,
                        "x1": x1,
                        "y1": y + 0.03,
                        "space": "page_normalized",
                    }
                ],
                quote=text,
            )
    rows = project.add_rows(
        sheet,
        [
            {
                "document": {
                    "blob": blob,
                    "filename": f"form-{i}.png",
                    "mime": "image/png",
                }
            }
            for i, blob in enumerate(blobs)
        ],
        {"document": column},
    )
    doc = load_positioned_document(project, blobs[0])

    def region(x0, y0, x1, y1):
        return {"page": 1, "box": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}}

    fields = [
        {
            "id": "name",
            "name": "Name",
            "key": region(0.09, 0.09, 0.22, 0.14),
            "value": region(0.34, 0.09, 0.65, 0.14),
        },
        {
            "id": "arrested",
            "name": "Arrested",
            "key": region(0.09, 0.19, 0.31, 0.24),
            "value": region(0.34, 0.19, 0.65, 0.24),
        },
    ]
    sections = []
    if repeats:
        for field in fields:
            field["section_id"] = "people"
        sections = [
            {
                "id": "people",
                "first": {
                    "start": {"page": 1, "y": 0.08},
                    "end": {"page": 1, "y": 0.27},
                },
                "rest": {"start": {"page": 1, "y": 0.3}, "end": {"page": 1, "y": 0.9}},
            }
        ]
    template = ExtractionTemplate(
        reference_blob_id=blobs[0],
        reference_fingerprint=doc.document.source_fingerprint,
        fields=fields,
        sections=sections,
    )
    return sheet, column, rows, template


def request(sheet, template, *, rows=None, key="visual", rename=None):
    return {
        "action_id": "media.extract_document",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet, "row_ids": rows},
        "params": {
            "source": "document",
            "template": template.model_dump(mode="json"),
            "repeat_group_id": "people" if template.sections else None,
        },
        "output_names": rename or {},
        "sheet_name": key,
        "idempotency_key": key,
    }


def seed_page_scoped_pdf(project):
    sheet = project.add_sheet("PDF pages")
    column = project.add_column(sheet, "document", "file")
    blob = project.add_blob(
        b"page-scoped fixture", filename="report.pdf", mime="application/pdf"
    )
    artifact = record_source_artifact(
        project,
        artifact_kind="file",
        blob_hash=blob,
        media_type="application/pdf",
        page_count=10,
        metadata={
            "engine": "tesseract",
            "page_images": {
                "1": {"source_width": 100, "source_height": 100},
                "2": {"source_width": 100, "source_height": 100},
            },
        },
    )
    for page, value in ((1, "FIRST-PAGE"), (2, "SECOND-PAGE")):
        for text, x0 in (("VALUE", 0.1), (value, 0.4)):
            record_source_span(
                project,
                artifact_id=artifact["id"],
                span_kind="region",
                page_start=page,
                page_end=page,
                bbox=[
                    {
                        "x0": x0,
                        "y0": 0.1,
                        "x1": x0 + 0.2,
                        "y1": 0.15,
                        "space": "page_normalized",
                    }
                ],
                quote=text,
            )
    rows = project.add_rows(
        sheet,
        [
            {
                "document": {
                    "blob": blob,
                    "filename": "report.pdf",
                    "mime": "application/pdf",
                    "page": page,
                }
            }
            for page in (1, 2)
        ],
        {"document": column},
    )
    reference = load_positioned_document(project, blob, page=2)
    template = ExtractionTemplate(
        reference_blob_id=blob,
        reference_page=2,
        reference_fingerprint=reference.document.source_fingerprint,
        fields=[
            {
                "id": "value",
                "name": "Value",
                "key": {
                    "page": 2,
                    "box": {"x0": 0.09, "y0": 0.09, "x1": 0.31, "y1": 0.16},
                },
                "value": {
                    "page": 2,
                    "box": {"x0": 0.39, "y0": 0.09, "x1": 0.7, "y1": 0.16},
                },
            }
        ],
    )
    return sheet, column, rows, blob, template


def test_page_scoped_pdf_geometry_and_run_only_read_selected_page(tmp_path):
    from frisket.actions.types import TableError
    from frisket.contracts.http.document_extraction import (
        ExtractionPreviewRequest,
        ExtractionTemplateSave,
    )

    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    workspace.create("Test", project_id="p")
    project = workspace.get("p")
    sheet, column, rows, blob, template = seed_page_scoped_pdf(project)
    service = DocumentExtractionService(workspace)

    geometry = service.document("p", sheet_id=sheet, column_id=column, row_id=rows[1])
    assert geometry.blob_id == blob
    assert geometry.reference_page == 2
    assert geometry.page_count == 10
    assert [page.page for page in geometry.document.pages] == [2]
    assert [token.text for token in geometry.document.pages[0].tokens] == [
        "VALUE",
        "SECOND-PAGE",
    ]
    saved = service.save(
        "p",
        ExtractionTemplateSave(
            sheet_id=sheet,
            source="document",
            reference_row_id=rows[1],
            draft=template.model_dump(mode="json"),
        ),
    )
    assert saved.draft.reference_page == 2
    [reloaded] = service.templates("p", sheet, "document").templates
    assert reloaded.id == saved.id
    assert reloaded.draft.reference_page == 2
    unsaved_draft = template.model_copy(update={"expand_values": True})
    preview = service.preview(
        "p",
        ExtractionPreviewRequest(
            sheet_id=sheet,
            source="document",
            scope={"kind": "this", "row_id": rows[1]},
            layout_id=saved.id,
            template=unsaved_draft,
        ),
    )
    assert preview.documents[0].result.records[0].cells["value"].text == "SECOND-PAGE"
    action = request(sheet, template, key="page-two")
    action["params"].update(
        layout_id=saved.id,
        extraction_scope={"kind": "this", "row_id": rows[1]},
    )
    result = run_typed_create_sheet_action(
        project,
        "p",
        typed_action_for_request(action),
    )
    assert result.status == "completed", result.errors
    output = result.outputs[0].ref
    assert list(
        project.get_values(output["sheet_id"], output["columns"]["Value"]).values()
    ) == ["SECOND-PAGE"]
    assert "FIRST-PAGE" not in str(result.model_dump(mode="json"))
    citation = project.db.execute(
        "SELECT sp.page_start,sa.page_count,sa.metadata FROM evidence_links el "
        "JOIN evidence_link_spans els ON els.link_id=el.id "
        "JOIN source_spans sp ON sp.id=els.span_id "
        "JOIN source_artifacts sa ON sa.id=sp.artifact_id "
        "WHERE el.sheet_id=? AND el.column_id=?",
        (output["sheet_id"], output["columns"]["Value"]),
    ).fetchone()
    assert (citation["page_start"], citation["page_count"]) == (2, 10)
    assert json.loads(citation["metadata"])["source_page"] == 2
    project.apply_edits(
        [
            {
                "row_id": rows[1],
                "column_id": column,
                "value": {
                    "blob": blob,
                    "filename": "report.pdf",
                    "mime": "application/pdf",
                    "page": 1,
                },
            }
        ]
    )
    with pytest.raises(TableError, match="Reference document no longer matches"):
        service.save(
            "p",
            ExtractionTemplateSave(
                id=saved.id,
                sheet_id=sheet,
                source="document",
                reference_row_id=rows[1],
                draft=template.model_dump(mode="json"),
            ),
        )


def test_page_scoped_pdf_revalidation_rejects_same_blob_page_change(tmp_path):
    from frisket.actions.types import SheetRows, TableError
    from frisket.engine.executor.document_extraction_read import (
        AdmittedPositionedDocumentReader,
    )

    with closing(Project.create(tmp_path / "project")) as project:
        sheet, column, rows, blob, template = seed_page_scoped_pdf(project)
        params = DocumentExtractParams(source="document", template=template)
        reader = AdmittedPositionedDocumentReader(
            project,
            scope=SheetRows(sheet_id=sheet, row_ids=[rows[1]]),
            params=params,
        )
        try:
            [(source, _loaded, extracted)] = list(reader.document_results(params))
            assert extracted.records[0].cells["value"].text == "SECOND-PAGE"
            project.apply_edits(
                [
                    {
                        "row_id": source.row_id,
                        "column_id": column,
                        "value": {
                            "blob": blob,
                            "filename": "report.pdf",
                            "mime": "application/pdf",
                            "page": 1,
                        },
                    }
                ]
            )
            with pytest.raises(TableError, match="Source document changed"):
                reader.revalidate()
        finally:
            reader.close()


def test_native_pdf_page_is_selected_before_positioned_text_extraction(
    tmp_path, monkeypatch
):
    import io

    from pypdf import PdfReader, PdfWriter

    from frisket.actions.document_extraction_types import (
        Box,
        PositionedPage,
        PositionedToken,
    )
    from frisket.engine import pdf_text

    writer = PdfWriter()
    writer.add_blank_page(width=111, height=222)
    writer.add_blank_page(width=333, height=444)
    raw = io.BytesIO()
    writer.write(raw)
    writer.close()

    seen = {}

    async def extract_selected(path, *, should_cancel=None):
        del should_cancel
        selected = PdfReader(path)
        seen["pages"] = len(selected.pages)
        seen["width"] = float(selected.pages[0].mediabox.width)
        return [
            PositionedPage(
                page=1,
                width=333,
                height=444,
                tokens=[
                    PositionedToken(
                        text="SECOND-PAGE",
                        box=Box(x0=0.1, y0=0.1, x1=0.5, y1=0.2),
                    )
                ],
            )
        ]

    monkeypatch.setattr(pdf_text, "extract_pdf_text", extract_selected)
    with closing(Project.create(tmp_path / "project")) as project:
        blob = project.add_blob(
            raw.getvalue(), filename="native.pdf", mime="application/pdf"
        )
        loaded = load_positioned_document(project, blob, page=2)

    assert seen == {"pages": 1, "width": 333.0}
    assert loaded.page == 2
    assert loaded.page_count == 2
    assert [page.page for page in loaded.document.pages] == [2]
    assert loaded.document.pages[0].tokens[0].text == "SECOND-PAGE"


def test_page_scoped_ocr_only_suppresses_native_fallback_for_its_page(
    tmp_path, monkeypatch
):
    import io

    from pypdf import PdfReader, PdfWriter

    from frisket.actions.document_extraction_types import (
        Box,
        PositionedPage,
        PositionedToken,
    )
    from frisket.engine import pdf_text

    writer = PdfWriter()
    writer.add_blank_page(width=111, height=222)
    writer.add_blank_page(width=333, height=444)
    raw = io.BytesIO()
    writer.write(raw)
    writer.close()
    native_calls = []

    async def extract_selected(path, *, should_cancel=None):
        del should_cancel
        page_count = len(PdfReader(path).pages)
        native_calls.append(page_count)
        return [
            PositionedPage(
                page=page,
                width=111 if page == 1 else 333,
                height=222 if page == 1 else 444,
                tokens=[
                    PositionedToken(
                        text=f"NATIVE-PAGE{page}",
                        box=Box(x0=0.1, y0=0.1, x1=0.5, y1=0.2),
                    )
                ],
            )
            for page in range(1, page_count + 1)
        ]

    monkeypatch.setattr(pdf_text, "extract_pdf_text", extract_selected)
    with closing(Project.create(tmp_path / "project")) as project:
        blob = project.add_blob(
            raw.getvalue(), filename="mixed.pdf", mime="application/pdf"
        )
        artifact = record_source_artifact(
            project,
            artifact_kind="file",
            blob_hash=blob,
            media_type="application/pdf",
            page_count=2,
            metadata={
                "engine": "tesseract",
                "page_images": {"2": {"source_width": 333, "source_height": 444}},
            },
        )
        record_source_span(
            project,
            artifact_id=artifact["id"],
            span_kind="region",
            page_start=2,
            page_end=2,
            bbox=[
                {
                    "x0": 0.1,
                    "y0": 0.1,
                    "x1": 0.5,
                    "y1": 0.2,
                    "space": "page_normalized",
                }
            ],
            quote="OCR-PAGE2",
        )

        ocr = load_positioned_document(project, blob, page=2)
        native = load_positioned_document(project, blob, page=1)
        whole = load_positioned_document(project, blob)

    assert native_calls == [1, 2]
    assert ocr.artifact_id == artifact["id"]
    assert ocr.document.pages[0].page == 2
    assert ocr.document.pages[0].tokens[0].text == "OCR-PAGE2"
    assert native.artifact_id is None
    assert native.document.pages[0].page == 1
    assert native.document.pages[0].tokens[0].text == "NATIVE-PAGE1"
    assert whole.artifact_id is None
    assert [page.page for page in whole.document.pages] == [1, 2]
    assert [page.tokens[0].text for page in whole.document.pages] == [
        "NATIVE-PAGE1",
        "NATIVE-PAGE2",
    ]


def test_actual_run_blank_citation_and_replay(tmp_path):
    with closing(Project.create(tmp_path / "project")) as project:
        sheet, column, row_ids, template = seed(project)
        bound = typed_action_for_request(
            request(sheet, template, rename={"Arrested": "Mark"})
        )
        result = run_typed_create_sheet_action(project, "p", bound)
        assert result.status == "completed", result.errors
        output = result.outputs[0].ref
        assert output["row_count"] == 2
        values = project.get_values(output["sheet_id"], output["columns"]["Mark"])
        assert list(values.values()) == ["X", ""]
        empty_row = list(values)[1]
        links = list_cell_evidence(
            project,
            sheet_id=output["sheet_id"],
            row_id=empty_row,
            column_id=output["columns"]["Mark"],
            project_id="p",
        )
        assert len(links["links"]) == 1
        viewer = resolve_evidence_viewer(
            project, links["links"][0]["id"], project_id="p"
        )
        assert viewer
        replay = run_typed_create_sheet_action(project, "p", bound)
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == result.receipt_id
        assert project.db.execute("SELECT count(*) FROM sheets").fetchone()[0] == 2


def test_repetition_one_aggregate_table(tmp_path):
    with closing(Project.create(tmp_path / "project")) as project:
        sheet, column, row_ids, template = seed(project, count=3, repeats=True)
        result = run_typed_create_sheet_action(
            project, "p", typed_action_for_request(request(sheet, template))
        )
        assert result.status == "completed", result.errors
        assert result.outputs[0].ref["row_count"] == 6
        assert project.db.execute("SELECT count(*) FROM sheets").fetchone()[0] == 2


def test_rerun_creates_fresh_sheet_and_leaves_manual_corrections(tmp_path):
    with closing(Project.create(tmp_path / "project")) as project:
        sheet, _column, _rows, template = seed(project)
        first = run_typed_create_sheet_action(
            project,
            "p",
            typed_action_for_request(request(sheet, template, key="first")),
        )
        assert first.status == "completed", first.errors
        original = first.outputs[0].ref
        original_values = project.get_values(
            original["sheet_id"], original["columns"]["Name"]
        )
        corrected_row = next(iter(original_values))
        project.apply_edits(
            [
                {
                    "row_id": corrected_row,
                    "column_id": original["columns"]["Name"],
                    "value": "Corrected",
                }
            ]
        )
        second = run_typed_create_sheet_action(
            project,
            "p",
            typed_action_for_request(request(sheet, template, key="second")),
        )
        assert second.status == "completed", second.errors
        fresh = second.outputs[0].ref
        assert fresh["sheet_id"] != original["sheet_id"]
        assert project.get_values(
            original["sheet_id"], original["columns"]["Name"]
        ) == {**original_values, corrected_row: "Corrected"}
        assert list(
            project.get_values(fresh["sheet_id"], fresh["columns"]["Name"]).values()
        ) == list(original_values.values())


def test_successful_input_membership_includes_zero_records_and_excludes_errors(
    tmp_path,
):
    from frisket.actions.types import SheetRows
    from frisket.engine.executor.document_extraction_read import (
        AdmittedPositionedDocumentReader,
    )

    with closing(Project.create(tmp_path / "project")) as project:
        sheet, column, rows, template = seed(project, count=3, repeats=True)
        values = project.get_values(sheet, column)
        zero_blob = values[rows[1]]["blob"]
        failed_blob = values[rows[2]]["blob"]
        project.db.execute(
            "DELETE FROM source_spans WHERE quote IN ('NAME','ARRESTED') AND artifact_id IN "
            "(SELECT id FROM source_artifacts WHERE blob_hash=?)",
            (zero_blob,),
        )
        project.db.execute(
            "DELETE FROM source_spans WHERE artifact_id IN "
            "(SELECT id FROM source_artifacts WHERE blob_hash=?)",
            (failed_blob,),
        )
        project.db.commit()
        params = DocumentExtractParams(
            source="document", template=template, repeat_group_id="people"
        )
        reader = AdmittedPositionedDocumentReader(
            project, scope=SheetRows(sheet_id=sheet), params=params
        )
        try:
            results = list(reader.document_results(params))
            assert [result.outcome for _source, _loaded, result in results] == [
                "extracted",
                "zero_records",
                "error",
            ]
            assert reader.successful_row_ids == rows[:2]
            assert reader.outcome_counts == {
                "extracted": 1,
                "zero_records": 1,
                "error": 1,
            }
        finally:
            reader.close()


def save_layout(project, sheet, rows, template):
    from frisket.engine.store.extraction_layouts import save_layout as save

    return save(
        project,
        sheet_id=sheet,
        source="document",
        reference_row_id=rows[0],
        draft=template.model_dump(mode="json"),
        repeat_group_id="people" if template.sections else None,
    )["id"]


def layout_request(sheet, template, layout_id, *, selection, key):
    body = request(sheet, template, key=key)
    body["params"].update(layout_id=layout_id, extraction_scope=selection)
    return typed_action_for_request(body)


def layout_rows(project, sheet, layout_id):
    from frisket.actions.document_extraction_types import ExtractionScope
    from frisket.engine.store.extraction_layouts import resolve_document_scope

    return resolve_document_scope(
        project,
        sheet_id=sheet,
        source="document",
        scope=ExtractionScope(kind="layout", layout_id=layout_id),
    )


def test_layout_run_remembers_zero_output_and_moves_only_applied_documents(tmp_path):
    from frisket.engine.store.extraction_layouts import get_layout

    with closing(Project.create(tmp_path / "project")) as project:
        sheet, column, rows, template = seed(project, count=3, repeats=True)
        values = project.get_values(sheet, column)
        project.db.execute(
            "DELETE FROM source_spans WHERE quote IN ('NAME','ARRESTED') AND artifact_id IN "
            "(SELECT id FROM source_artifacts WHERE blob_hash=?)",
            (values[rows[1]]["blob"],),
        )
        project.db.execute(
            "DELETE FROM source_spans WHERE artifact_id IN "
            "(SELECT id FROM source_artifacts WHERE blob_hash=?)",
            (values[rows[2]]["blob"],),
        )
        project.db.commit()
        first_layout = save_layout(project, sheet, rows, template)
        second_layout = save_layout(project, sheet, rows, template)
        first = run_typed_create_sheet_action(
            project,
            "p",
            layout_request(
                sheet,
                template,
                first_layout,
                selection={"kind": "all"},
                key="Layout 1 results",
            ),
        )
        assert first.status == "completed", first.errors
        original = first.outputs[0].ref
        assert original["row_count"] == 2
        assert layout_rows(project, sheet, first_layout) == rows[:2]
        assert get_layout(project, first_layout)["has_applied"]
        assert not get_layout(project, second_layout)["has_applied"]
        original_values = project.get_values(
            original["sheet_id"], original["columns"]["Name"]
        )
        corrected_row = next(iter(original_values))
        project.apply_edits(
            [
                {
                    "row_id": corrected_row,
                    "column_id": original["columns"]["Name"],
                    "value": "Corrected",
                }
            ]
        )
        moved = run_typed_create_sheet_action(
            project,
            "p",
            layout_request(
                sheet,
                template,
                second_layout,
                selection={"kind": "this", "row_id": rows[0]},
                key="Layout 2 results",
            ),
        )
        assert moved.status == "completed", moved.errors
        assert layout_rows(project, sheet, first_layout) == [rows[1]]
        assert layout_rows(project, sheet, second_layout) == [rows[0]]
        rerun = layout_request(
            sheet,
            template,
            first_layout,
            selection={"kind": "layout"},
            key="Layout 1 results (2)",
        )
        fresh = run_typed_create_sheet_action(project, "p", rerun)
        assert fresh.status == "completed", fresh.errors
        assert fresh.outputs[0].ref["row_count"] == 0
        assert fresh.outputs[0].ref["sheet_id"] != original["sheet_id"]
        assert project.get_values(
            original["sheet_id"], original["columns"]["Name"]
        ) == {**original_values, corrected_row: "Corrected"}
        assert layout_rows(project, sheet, first_layout) == [rows[1]]
        replay = run_typed_create_sheet_action(project, "p", rerun)
        assert replay.receipt_id == fresh.receipt_id
        assert project.db.execute("SELECT count(*) FROM sheets").fetchone()[0] == 4


def test_result_undo_keeps_last_successful_layout_affinity(tmp_path):
    with closing(Project.create(tmp_path / "project")) as project:
        sheet, _column, rows, template = seed(project)
        layout_id = save_layout(project, sheet, rows, template)
        result = run_typed_create_sheet_action(
            project,
            "p",
            layout_request(
                sheet,
                template,
                layout_id,
                selection={"kind": "this", "row_id": rows[1]},
                key="Undoable layout results",
            ),
        )
        assert result.status == "completed", result.errors
        result_sheet_id = result.outputs[0].ref["sheet_id"]
        assert (
            project.db.execute(
                "SELECT hidden FROM sheets WHERE id=?", (result_sheet_id,)
            ).fetchone()[0]
            == 0
        )
        assert layout_rows(project, sheet, layout_id) == [rows[1]]
        result_op_id = project.undo()
        assert result_op_id in result.op_ids
        assert (
            project.db.execute(
                "SELECT hidden FROM sheets WHERE id=?", (result_sheet_id,)
            ).fetchone()[0]
            == 1
        )
        assert layout_rows(project, sheet, layout_id) == [rows[1]]
        assert project.redo() == result_op_id
        assert (
            project.db.execute(
                "SELECT hidden FROM sheets WHERE id=?", (result_sheet_id,)
            ).fetchone()[0]
            == 0
        )
        assert layout_rows(project, sheet, layout_id) == [rows[1]]


def test_layout_autosave_change_before_publication_is_stale(tmp_path, monkeypatch):
    from frisket.engine.executor.document_extraction_read import (
        AdmittedPositionedDocumentReader,
    )
    from frisket.engine.store.extraction_layouts import get_layout

    with closing(Project.create(tmp_path / "project")) as project:
        sheet, _column, rows, template = seed(project)
        layout_id = save_layout(project, sheet, rows, template)
        original = AdmittedPositionedDocumentReader.revalidate
        changed = template.model_copy(update={"expand_values": True}).model_dump(
            mode="json"
        )

        def autosave_then_revalidate(reader):
            project.db.execute(
                "UPDATE extraction_layouts SET draft=? WHERE id=?",
                (json.dumps(changed, allow_nan=False), layout_id),
            )
            return original(reader)

        monkeypatch.setattr(
            AdmittedPositionedDocumentReader,
            "revalidate",
            autosave_then_revalidate,
        )
        result = run_typed_create_sheet_action(
            project,
            "p",
            layout_request(
                sheet,
                template,
                layout_id,
                selection={"kind": "all"},
                key="Stale layout results",
            ),
        )
        assert result.status == "failed"
        assert result.errors[0].code == "stale_input"
        assert project.db.execute("SELECT count(*) FROM sheets").fetchone()[0] == 1
        assert layout_rows(project, sheet, layout_id) == []
        assert not get_layout(project, layout_id)["has_applied"]


def test_filter_scope_is_resolved_once_and_frozen_at_reader_admission(tmp_path):
    from frisket.actions.types import SheetRows
    from frisket.engine.executor.document_extraction_read import (
        AdmittedPositionedDocumentReader,
    )

    with closing(Project.create(tmp_path / "project")) as project:
        sheet, _column, rows, template = seed(project)
        selected_column = project.add_column(sheet, "selected", "text")
        project.apply_edits(
            [
                {"row_id": row, "column_id": selected_column, "value": value}
                for row, value in zip(rows, ["yes", "no"], strict=True)
            ]
        )
        layout_id = save_layout(project, sheet, rows, template)
        params = DocumentExtractParams(
            source="document",
            template=template,
            layout_id=layout_id,
            extraction_scope={"kind": "filter", "filter": {"selected": {"eq": "yes"}}},
        )
        reader = AdmittedPositionedDocumentReader(
            project, scope=SheetRows(sheet_id=sheet), params=params
        )
        project.apply_edits(
            [
                {"row_id": row, "column_id": selected_column, "value": value}
                for row, value in zip(rows, ["no", "yes"], strict=True)
            ]
        )
        try:
            results = list(reader.document_results(params))
            assert [source.row_id for source, _loaded, _result in results] == rows[:1]
            selection_fact = next(
                fact
                for fact in reader.facts
                if fact["kind"] == "document_extraction_scope"
            )
            assert selection_fact["selection"]["filter"] == {"selected": {"eq": "yes"}}
            read_fact = next(
                fact for fact in reader.facts if fact["kind"] == "sheet_rows_read"
            )
            assert read_fact["row_ids"] == rows[:1]
        finally:
            reader.close()
        assert layout_rows(project, sheet, layout_id) == []
        result = run_typed_create_sheet_action(
            project,
            "p",
            layout_request(
                sheet,
                template,
                layout_id,
                selection={"kind": "filter", "filter": {"selected": {"eq": "yes"}}},
                key="Filtered results",
            ),
        )
        assert result.status == "completed", result.errors
        assert result.outputs[0].ref["row_count"] == 1
        assert layout_rows(project, sheet, layout_id) == rows[1:]


def test_empty_layout_scope_refuses_without_expanding_to_all(tmp_path):
    from frisket.engine.store.extraction_layouts import get_layout

    with closing(Project.create(tmp_path / "project")) as project:
        sheet, _column, rows, template = seed(project)
        layout_id = save_layout(project, sheet, rows, template)
        result = run_typed_create_sheet_action(
            project,
            "p",
            layout_request(
                sheet,
                template,
                layout_id,
                selection={"kind": "layout"},
                key="Empty scope",
            ),
        )
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_input_ref"
        assert project.db.execute("SELECT count(*) FROM sheets").fetchone()[0] == 1
        assert not get_layout(project, layout_id)["has_applied"]
        assert layout_rows(project, sheet, layout_id) == []


def test_preview_and_all_document_errors_never_assign_layout(tmp_path):
    from frisket.contracts.http.document_extraction import ExtractionPreviewRequest
    from frisket.engine.store.extraction_layouts import get_layout

    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    workspace.create("Test", project_id="p")
    project = workspace.get("p")
    sheet, column, rows, template = seed(project)
    layout_id = save_layout(project, sheet, rows, template)
    preview = DocumentExtractionService(workspace).preview(
        "p",
        ExtractionPreviewRequest(
            sheet_id=sheet,
            source="document",
            template=template,
            layout_id=layout_id,
            scope={"kind": "all"},
        ),
    )
    assert len(preview.documents) == 2
    assert project.db.execute("SELECT count(*) FROM sheets").fetchone()[0] == 1
    assert layout_rows(project, sheet, layout_id) == []
    assert not get_layout(project, layout_id)["has_applied"]
    failed_blob = project.get_values(sheet, column)[rows[1]]["blob"]
    project.db.execute(
        "DELETE FROM source_spans WHERE artifact_id IN (SELECT id FROM source_artifacts WHERE blob_hash=?)",
        (failed_blob,),
    )
    project.db.commit()
    failed = run_typed_create_sheet_action(
        project,
        "p",
        layout_request(
            sheet,
            template,
            layout_id,
            selection={"kind": "this", "row_id": rows[1]},
            key="Unreadable document",
        ),
    )
    assert failed.status == "completed", failed.errors
    assert failed.outputs[0].ref["row_count"] == 0
    assert any(
        f"Document row {rows[1]}: error" in warning for warning in failed.warnings
    )
    assert layout_rows(project, sheet, layout_id) == []
    assert not get_layout(project, layout_id)["has_applied"]


def test_unmatched_document_publishes_zero_rows_and_remains_in_layout_cohort(tmp_path):
    with closing(Project.create(tmp_path / "project")) as project:
        sheet, column, rows, template = seed(project)
        blob = project.get_values(sheet, column)[rows[1]]["blob"]
        project.db.execute(
            "DELETE FROM source_spans WHERE quote IN ('NAME','ARRESTED') AND artifact_id IN (SELECT id FROM source_artifacts WHERE blob_hash=?)",
            (blob,),
        )
        project.db.commit()
        layout_id = save_layout(project, sheet, rows, template)
        result = run_typed_create_sheet_action(
            project,
            "p",
            layout_request(
                sheet,
                template,
                layout_id,
                selection={"kind": "this", "row_id": rows[1]},
                key="No matches",
            ),
        )
        assert result.status == "completed", result.errors
        assert result.outputs[0].ref["row_count"] == 0
        assert layout_rows(project, sheet, layout_id) == [rows[1]]
        rerun = run_typed_create_sheet_action(
            project,
            "p",
            layout_request(
                sheet,
                template,
                layout_id,
                selection={"kind": "layout"},
                key="No matches (2)",
            ),
        )
        assert rerun.status == "completed", rerun.errors
        assert rerun.outputs[0].ref["row_count"] == 0
        assert layout_rows(project, sheet, layout_id) == [rows[1]]


def test_repeated_layout_mismatch_is_zero_output_with_diagnostic_and_cohort(tmp_path):
    from frisket.contracts.http.document_extraction import ExtractionPreviewRequest

    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    workspace.create("Test", project_id="p")
    project = workspace.get("p")
    sheet, column, rows, template = seed(project, repeats=True)
    blob = project.get_values(sheet, column)[rows[1]]["blob"]
    project.db.execute(
        "DELETE FROM source_spans WHERE quote='NAME' AND artifact_id IN "
        "(SELECT id FROM source_artifacts WHERE blob_hash=?)",
        (blob,),
    )
    project.db.commit()
    layout_id = save_layout(project, sheet, rows, template)
    preview = DocumentExtractionService(workspace).preview(
        "p",
        ExtractionPreviewRequest(
            sheet_id=sheet,
            source="document",
            template=template,
            repeat_group_id="people",
            layout_id=layout_id,
            scope={"kind": "this", "row_id": rows[1]},
        ),
    )
    assert preview.documents[0].result.outcome == "zero_records"
    assert preview.documents[0].result.records == []
    assert preview.documents[0].result.diagnostics == [
        "Repeated section start is missing, but later keys were found"
    ]
    assert layout_rows(project, sheet, layout_id) == []
    result = run_typed_create_sheet_action(
        project,
        "p",
        layout_request(
            sheet,
            template,
            layout_id,
            selection={"kind": "this", "row_id": rows[1]},
            key="Partial layout match",
        ),
    )
    assert result.status == "completed", result.errors
    assert result.outputs[0].ref["row_count"] == 0
    assert layout_rows(project, sheet, layout_id) == [rows[1]]
    assert any("later keys were found" in warning for warning in result.warnings)
    rerun = run_typed_create_sheet_action(
        project,
        "p",
        layout_request(
            sheet,
            template,
            layout_id,
            selection={"kind": "layout"},
            key="Partial layout match (2)",
        ),
    )
    assert rerun.status == "completed", rerun.errors
    assert rerun.outputs[0].ref["row_count"] == 0
    assert layout_rows(project, sheet, layout_id) == [rows[1]]


def test_layout_with_explicit_sheet_rows_records_its_scope_and_membership(tmp_path):
    with closing(Project.create(tmp_path / "project")) as project:
        sheet, _column, rows, template = seed(project)
        layout_id = save_layout(project, sheet, rows, template)
        body = request(sheet, template, rows=rows[1:])
        body["params"]["layout_id"] = layout_id
        result = run_typed_create_sheet_action(
            project, "p", typed_action_for_request(body)
        )
        assert result.status == "completed", result.errors
        assert layout_rows(project, sheet, layout_id) == rows[1:]
        reads = result.outputs[0].ref["reads"]
        assert next(
            fact for fact in reads if fact["kind"] == "document_extraction_scope"
        ) == {
            "kind": "document_extraction_scope",
            "sheet_id": sheet,
            "source": "document",
            "layout_id": layout_id,
            "selection": {"kind": "sheet_rows"},
        }
        assert (
            next(fact for fact in reads if fact["kind"] == "sheet_rows_read")["row_ids"]
            == rows[1:]
        )


def test_api_preview_bounded_parity_persistence_and_stale_reference(tmp_path):
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    workspace.create("Test", project_id="p")
    project = workspace.get("p")
    sheet, column, rows, template = seed(project, count=14)
    service = DocumentExtractionService(workspace)
    app = FastAPI()
    register_document_extraction_routes(app, service=service)
    from frisket.server.route_errors import register_route_error_handler

    register_route_error_handler(app)
    with TestClient(app) as client:
        prefix = "/api/projects/p/document-extraction"
        geometry = client.get(
            f"{prefix}/documents/{rows[0]}",
            params={"sheet_id": sheet, "column_id": column},
        )
        assert geometry.status_code == 200, geometry.text
        assert (
            geometry.json()["document"]["source_fingerprint"]
            == template.reference_fingerprint
        )
        body = {
            "sheet_id": sheet,
            "source": "document",
            "template": template.model_dump(mode="json"),
            "scope": {"kind": "all"},
        }
        preview = client.post(f"{prefix}/preview", json=body)
        assert preview.status_code == 200, preview.text
        assert len(preview.json()["documents"]) == 12
        assert preview.json()["truncated"] is True
        selected = client.post(
            f"{prefix}/preview",
            json={**body, "scope": {"kind": "filter", "scope_row_ids": rows[:2]}},
        )
        assert selected.json()["truncated"] is False
        assert (
            preview.json()["documents"][1]["result"]["records"][0]["cells"]["arrested"][
                "status"
            ]
            == "empty"
        )
        assert project.db.execute("SELECT count(*) FROM sheets").fetchone()[0] == 1
        save_body = {
            "sheet_id": sheet,
            "source": "document",
            "draft": body["template"],
            "reference_row_id": rows[0],
        }
        save = client.post(
            f"{prefix}/templates",
            json=save_body,
        )
        assert save.status_code == 200, save.text
        updated = client.post(
            f"{prefix}/templates",
            json={
                **save_body,
                "id": save.json()["id"],
            },
        )
        assert updated.status_code == 200, updated.text
        saved = client.get(
            f"{prefix}/templates", params={"sheet_id": sheet, "source": "document"}
        ).json()["templates"]
        assert len(saved) == 1 and saved[0]["name"] == "Layout 1"
        missing = client.post(
            f"{prefix}/templates",
            json={**save_body, "id": 9999},
        )
        assert missing.status_code == 404, missing.text
        body["template"]["reference_fingerprint"] = "stale"
        assert client.post(f"{prefix}/preview", json=body).status_code == 422


@pytest.mark.parametrize(
    "defect",
    [
        "empty_text",
        "missing_dimensions",
        "malformed_json",
        "null_page",
        "nonobject_box",
    ],
)
def test_newer_unusable_ocr_artifact_does_not_permanently_invalidate_template(
    tmp_path, defect
):
    with closing(Project.create(tmp_path / "project")) as project:
        sheet, column, rows, template = seed(project, count=1)
        metadata = {
            "engine": "tesseract",
            "page_images": {"1": {"width": 100, "height": 100}},
        }
        if defect == "missing_dimensions":
            metadata["page_images"]["1"]["width"] = None
        artifact = record_source_artifact(
            project,
            artifact_kind="file",
            blob_hash=template.reference_blob_id,
            media_type="image/png",
            metadata=metadata,
        )
        record_source_span(
            project,
            artifact_id=artifact["id"],
            span_kind="region",
            page_start=None if defect == "null_page" else 1,
            bbox=[42]
            if defect == "nonobject_box"
            else [{"x0": 0.1, "y0": 0.1, "x1": 0.2, "y1": 0.13}],
            quote=None if defect == "empty_text" else "NAME",
        )
        if defect == "malformed_json":
            project.db.execute(
                "UPDATE source_artifacts SET metadata='{' WHERE id=?", (artifact["id"],)
            )
            project.db.commit()
        loaded = load_positioned_document(project, template.reference_blob_id)
        assert loaded.document.source_fingerprint == template.reference_fingerprint
        result = run_typed_create_sheet_action(
            project, "p", typed_action_for_request(request(sheet, template))
        )
        assert result.status == "completed", result.errors
        assert result.outputs[0].ref["row_count"] == 1


def test_one_corrupt_document_does_not_discard_other_documents(tmp_path):
    with closing(Project.create(tmp_path / "project")) as project:
        sheet, column, rows, template = seed(project)
        project.db.execute(
            "UPDATE source_artifacts SET metadata='{' WHERE blob_hash != ?",
            (template.reference_blob_id,),
        )
        project.db.commit()
        result = run_typed_create_sheet_action(
            project, "p", typed_action_for_request(request(sheet, template))
        )
        assert result.status == "completed", result.errors
        assert result.outputs[0].ref["row_count"] == 1
        assert any(
            f"Document row {rows[1]}: error" in warning for warning in result.warnings
        )


def test_cancelled_template_save_does_not_persist(tmp_path):
    from frisket.actions.types import TableError
    from frisket.contracts.http.document_extraction import ExtractionTemplateSave

    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    workspace.create("Test", project_id="p")
    sheet, column, rows, template = seed(workspace.get("p"), count=1)
    service = DocumentExtractionService(workspace)
    body = ExtractionTemplateSave(
        sheet_id=sheet,
        source="document",
        reference_row_id=rows[0],
        draft=template.model_dump(),
    )
    with pytest.raises(TableError, match="cancelled"):
        service.save("p", body, cancelled=lambda: True)
    assert workspace.saved_recipes() == []


def test_missing_pdf_bytes_is_a_document_failure_not_a_batch_failure(
    tmp_path, monkeypatch
):
    with closing(Project.create(tmp_path / "project")) as project:
        sheet, column, rows, template = seed(project, count=1)
        missing_blob = project.add_blob(
            b"missing source", filename="missing.pdf", mime="application/pdf"
        )
        missing_row = project.add_rows(
            sheet,
            [
                {
                    "document": {
                        "blob": missing_blob,
                        "filename": "missing.pdf",
                        "mime": "application/pdf",
                    }
                }
            ],
            {"document": column},
        )[0]
        original = project.materialize_blob

        def materialize(blob_id):
            if blob_id == missing_blob:
                raise FileNotFoundError("unreadable bytes")
            return original(blob_id)

        monkeypatch.setattr(project, "materialize_blob", materialize)
        result = run_typed_create_sheet_action(
            project, "p", typed_action_for_request(request(sheet, template))
        )
        assert result.status == "completed", result.errors
        assert result.outputs[0].ref["row_count"] == 1
        assert any(
            f"Document row {missing_row}: error" in warning
            for warning in result.warnings
        )


def test_missing_geometry_is_not_an_empty_success(tmp_path):
    with closing(Project.create(tmp_path / "project")) as project:
        sheet, column, rows, template = seed(project)
        project.db.execute(
            "DELETE FROM source_spans WHERE artifact_id IN (SELECT id FROM source_artifacts WHERE blob_hash != ?)",
            (template.reference_blob_id,),
        )
        project.db.commit()
        from frisket.engine.executor.document_extraction_read import (
            AdmittedPositionedDocumentReader,
        )
        from frisket.actions.types import SheetRows

        params = DocumentExtractParams(source="document", template=template)
        reader = AdmittedPositionedDocumentReader(
            project, scope=SheetRows(sheet_id=sheet), params=params
        )
        try:
            results = list(reader.document_results(params))
            assert results[1][2].outcome == "error"
            assert results[1][2].error_code == "document_geometry_unavailable"
            assert results[1][2].records == []
        finally:
            reader.close()


def test_public_queued_run_rejects_layout_changed_after_submission(tmp_path):
    from frisket.server.app import create_app
    from frisket.engine.jobs.worker import Worker

    with TestClient(create_app(tmp_path / "workspace")) as client:
        pid = client.post("/api/projects", json={"name": "Extraction"}).json()["id"]
        workspace = client.app.state.workspace
        project = workspace.get(pid)
        sheet, column, rows, template = seed(project)
        prefix = f"/api/projects/{pid}/document-extraction"
        empty = client.post(
            f"{prefix}/preview",
            json={
                "sheet_id": sheet,
                "source": "document",
                "scope": {"kind": "filter", "scope_row_ids": []},
                "template": template.model_dump(mode="json"),
            },
        )
        assert empty.status_code == 200, empty.text
        assert empty.json()["documents"] == []
        layout_id = save_layout(project, sheet, rows, template)
        run = request(sheet, template)
        run["params"].update(layout_id=layout_id, extraction_scope={"kind": "all"})
        launched = client.post(f"/api/projects/{pid}/actions/v1/run", json=run)
        assert launched.status_code == 200, launched.text
        assert launched.json()["status"] == "queued", launched.text
        from frisket.engine.store.extraction_layouts import save_layout as save

        save(project, sheet_id=sheet, source="document", layout_id=layout_id, draft={})
        worker = Worker(
            workspace.queue, workspace.registry, worker_id="visual-extraction-test"
        )
        assert worker.run_once()
        receipt = client.get(
            f"/api/projects/{pid}/actions/v1/receipts/{launched.json()['receipt_id']}"
        )
        assert receipt.status_code == 200, receipt.text
        assert receipt.json()["status"] == "failed", receipt.text
        assert receipt.json()["errors"][0]["code"] == "invalid_input_ref"
        assert project.db.execute("SELECT count(*) FROM sheets").fetchone()[0] == 1
        assert layout_rows(project, sheet, layout_id) == []


def test_partial_field_failure_warns_without_discarding_valid_fields(tmp_path):
    with closing(Project.create(tmp_path / "project")) as project:
        sheet, column, rows, template = seed(project)
        project.db.execute(
            "DELETE FROM source_spans WHERE quote='NAME' AND artifact_id IN "
            "(SELECT id FROM source_artifacts WHERE blob_hash != ?)",
            (template.reference_blob_id,),
        )
        project.db.commit()
        result = run_typed_create_sheet_action(
            project,
            "p",
            typed_action_for_request(request(sheet, template, rows=[rows[1]])),
        )
        assert result.status == "completed", result.errors
        output = result.outputs[0].ref
        assert list(
            project.get_values(output["sheet_id"], output["columns"]["Name"]).values()
        ) == [None]
        assert list(
            project.get_values(
                output["sheet_id"], output["columns"]["Arrested"]
            ).values()
        ) == [""]
        assert any(
            f"Document row {rows[1]}" in warning and "Name:" in warning
            for warning in result.warnings
        )
        assert any("1 unresolved fields" in warning for warning in result.warnings)


def test_multiregion_publication_does_not_repeat_whole_value_as_page_quote(
    tmp_path, monkeypatch
):
    from frisket.actions.document_extraction_types import (
        DocumentExtraction,
        ExtractedRecord,
        ExtractedCell,
    )
    from frisket.engine import document_extraction

    with closing(Project.create(tmp_path / "project")) as project:
        sheet, column, rows, template = seed(project, count=1)
        # The matcher separately tests page-spanning assembly. Exercise publication
        # of its multipart result without attributing the whole text to either box.
        regions = [
            template.fields[0].value,
            template.fields[0].value.model_copy(update={"page": 2}),
        ]
        monkeypatch.setattr(
            document_extraction,
            "extract_document",
            lambda *_args: DocumentExtraction(
                outcome="extracted",
                records=[
                    ExtractedRecord(
                        cells={
                            "name": ExtractedCell(
                                text="first\nsecond",
                                status="extracted",
                                regions=regions,
                                diagnostic="Text intersects the selected boundary",
                            ),
                            "arrested": ExtractedCell(
                                text="X",
                                status="extracted",
                                regions=[template.fields[1].value],
                            ),
                        }
                    )
                ],
            ),
        )
        result = run_typed_create_sheet_action(
            project, "p", typed_action_for_request(request(sheet, template))
        )
        assert result.status == "completed", result.errors
        output = result.outputs[0].ref
        assert list(
            project.get_values(output["sheet_id"], output["columns"]["Name"]).values()
        ) == ["first\nsecond"]
        spans = project.db.execute(
            "SELECT sp.page_start,sp.quote FROM source_spans sp "
            "JOIN evidence_link_spans els ON els.span_id=sp.id "
            "JOIN evidence_links el ON el.id=els.link_id "
            "WHERE el.sheet_id=? AND el.column_id=? ORDER BY els.rank",
            (output["sheet_id"], output["columns"]["Name"]),
        ).fetchall()
        assert [(span["page_start"], span["quote"]) for span in spans] == [
            (1, None),
            (2, None),
        ]
        assert any("Name: Text intersects" in warning for warning in result.warnings)


def test_zero_repeats_yield_no_fabricated_rows_but_document_outcome(tmp_path):
    from frisket.engine.executor.document_extraction_read import (
        AdmittedPositionedDocumentReader,
    )
    from frisket.actions.types import SheetRows

    with closing(Project.create(tmp_path / "project")) as project:
        sheet, column, rows, template = seed(project, repeats=True)
        # A legitimate different document with positioned text but no records.
        blob = project.add_blob(b"no records", filename="none.png", mime="image/png")
        artifact = record_source_artifact(
            project,
            artifact_kind="file",
            blob_hash=blob,
            media_type="image/png",
            metadata={
                "engine": "tesseract",
                "page_images": {"1": {"source_width": 100, "source_height": 100}},
            },
        )
        record_source_span(
            project,
            artifact_id=artifact["id"],
            span_kind="region",
            page_start=1,
            page_end=1,
            bbox=[{"x0": 0.1, "y0": 0.1, "x1": 0.2, "y1": 0.13}],
            quote="EMPTY",
        )
        zero_row = project.add_rows(
            sheet,
            [{"document": {"blob": blob, "filename": "none.png", "mime": "image/png"}}],
            {"document": column},
        )[0]
        params = DocumentExtractParams(
            source="document", template=template, repeat_group_id="people"
        )
        reader = AdmittedPositionedDocumentReader(
            project, scope=SheetRows(sheet_id=sheet, row_ids=[zero_row]), params=params
        )
        try:
            [_source_loaded_result] = list(reader.document_results(params))
            result = _source_loaded_result[2]
            assert result.records == []
            assert result.outcome == "zero_records"
            assert reader.documents[0]["row_id"] == zero_row
        finally:
            reader.close()
        result = run_typed_create_sheet_action(
            project,
            "p",
            typed_action_for_request(request(sheet, template, rows=[zero_row])),
        )
        assert result.status == "completed", result.errors
        assert result.outputs[0].ref["row_count"] == 0
        assert result.warnings
