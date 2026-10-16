from __future__ import annotations

from contextlib import closing
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from frisket.actions.document_extraction_types import (
    Box,
    PositionedPage,
    PositionedToken,
)
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import validate_root_action
from frisket.engine.executor import run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.prepared_content import PreparedContentStore
from frisket.engine.store.runs import RunResultStore
from frisket.search import drain_index, search_project


PROJECT_ID = "native-pdf-text"


def _page(number: int, text: str) -> PositionedPage:
    return PositionedPage(
        page=number,
        width=612,
        height=792,
        tokens=(
            [
                PositionedToken(
                    text=text,
                    box=Box(x0=0.1, y0=0.1, x1=0.8, y1=0.2),
                )
            ]
            if text
            else []
        ),
    )


def _pdf_bytes(*texts: str) -> bytes:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    for text in texts:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        if text:
            content = DecodedStreamObject()
            content.set_data(f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode("ascii"))
            page[NameObject("/Contents")] = writer._add_object(content)
    from io import BytesIO

    output = BytesIO()
    writer.write(output)
    writer.close()
    return output.getvalue()


def _source(
    project: Project,
    tmp_path: Path,
    *,
    page: int | None = None,
    pdf: bytes | None = None,
):
    sheet_id = project.add_sheet("Documents")
    source_id = project.add_column(sheet_id, "source", type="file")
    source = tmp_path / f"source-{sheet_id}.pdf"
    source.write_bytes(pdf if pdf is not None else _pdf_bytes("", ""))
    digest = project.add_blob(
        source.read_bytes(), filename="source.pdf", mime="application/pdf"
    )
    value = {
        "blob": digest,
        "filename": "source.pdf",
        "mime": "application/pdf",
        **({"page": page} if page is not None else {}),
    }
    row_id = project.add_rows(sheet_id, [{"source": value}], {"source": source_id})[0]
    return sheet_id, source_id, row_id, digest


def _request(sheet_id: int, row_id: int, *, key: str) -> dict:
    return {
        "action_id": "media.read_pdf_text",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": [row_id]},
        "params": {"source": "source"},
        "output_names": {"text": "prepared text"},
        "idempotency_key": key,
    }


def test_native_pdf_action_has_typed_public_contract() -> None:
    assert "media.read_pdf_text" in ACTION_REGISTRY.action_ids
    assert validate_root_action(
        {
            "action_id": "media.read_pdf_text",
            "scope": {"kind": "sheet_rows", "sheet_id": 1},
            "params": {"source": "document"},
            "idempotency_key": "native-pdf-contract",
        }
    ).ok
    registered = ACTION_REGISTRY.get("media.read_pdf_text")
    assert registered.catalog_entry()["ui_hints"]["source_requirements"] == [
        {
            "id": "source",
            "param": "source",
            "label": "Source",
            "mode": "column",
            "min": 1,
            "accepted_column_types": ["file"],
        }
    ]


def test_native_pdf_text_publishes_prepared_pages_evidence_and_search(
    tmp_path: Path, monkeypatch
) -> None:
    from frisket.engine import pdf_text

    observed: list[Path] = []

    async def extract(source: Path, **_kwargs):
        observed.append(source)
        return [_page(1, "ALPHA native words"), _page(2, "OMEGA searchable words")]

    monkeypatch.setattr(pdf_text, "extract_pdf_text", extract)
    with closing(Project.create(tmp_path / "native.frisket")) as project:
        sheet_id, _source_id, row_id, digest = _source(project, tmp_path)
        result = run_action_spec(
            project,
            _request(sheet_id, row_id, key="native-success"),
            project_id=PROJECT_ID,
        )

        assert result.status == "completed", result.errors
        assert len(observed) == 1
        output = next(item for item in result.outputs if item.name == "prepared text")
        assert project.get_values(sheet_id, output.column_id, [row_id])[row_id] == (
            "ALPHA native words\n\nOMEGA searchable words"
        )
        raw = project.db.execute(
            "SELECT value_kind,value FROM results WHERE run_id=? AND row_id=? "
            "AND column_id=?",
            (result.run_id, row_id, output.column_id),
        ).fetchone()
        assert raw["value_kind"] == "prepared_content_ref"
        prepared = PreparedContentStore(project).resolve(int(raw["value"]))
        assert [pin.page_number for pin in prepared.pins] == [1, 2]
        artifact = project.db.execute(
            "SELECT blob_hash,page_count FROM source_artifacts WHERE id=?",
            (prepared.source_artifact_id,),
        ).fetchone()
        assert (artifact["blob_hash"], artifact["page_count"]) == (digest, 2)
        assert {
            row["page_start"]
            for row in project.db.execute(
                "SELECT span.page_start FROM source_spans AS span "
                "JOIN evidence_link_spans AS member ON member.span_id=span.id "
                "JOIN evidence_links AS link ON link.id=member.link_id "
                "WHERE link.run_id=? AND link.column_id=?",
                (result.run_id, output.column_id),
            )
        } == {1, 2}

        drain_index(project)
        hits = search_project(project, "OMEGA", rerank="off")
        assert any(
            hit["row_id"] == row_id and hit["column_id"] == output.column_id
            for hit in hits
        )


def test_native_pdf_action_uses_real_fenced_extractor(tmp_path: Path) -> None:
    with closing(Project.create(tmp_path / "real-native.frisket")) as project:
        sheet_id, _source_id, row_id, _digest = _source(
            project,
            tmp_path,
            pdf=_pdf_bytes("REALNATIVEONE", "REALNATIVETWO"),
        )
        result = run_action_spec(
            project,
            _request(sheet_id, row_id, key="real-native-extractor"),
            project_id=PROJECT_ID,
        )

        assert result.status == "completed", result.errors
        output = next(item for item in result.outputs if item.name == "prepared text")
        assert project.get_values(sheet_id, output.column_id, [row_id])[row_id] == (
            "REALNATIVEONE\n\nREALNATIVETWO"
        )


def test_blank_selected_page_reports_needs_ocr_without_prepared_value(
    tmp_path: Path, monkeypatch
) -> None:
    from frisket.engine import pdf_text

    async def extract(_source: Path, **_kwargs):
        return [_page(1, "native"), _page(2, "")]

    monkeypatch.setattr(pdf_text, "extract_pdf_text", extract)
    with closing(Project.create(tmp_path / "mixed.frisket")) as project:
        sheet_id, _source_id, row_id, _digest = _source(project, tmp_path)
        result = run_action_spec(
            project,
            _request(sheet_id, row_id, key="native-needs-ocr"),
            project_id=PROJECT_ID,
        )

        assert result.status == "failed"
        assert result.errors[0].code == "map_rows_failed"
        assert result.errors[0].details == {
            "total_rows": 1,
            "completed_rows": 1,
            "failed_rows": 1,
        }
        summary = RunResultStore(project).row_error_summary(result.run_id)
        assert summary is not None and summary["total_failed_rows"] == 1
        assert summary["groups"][0]["code"] == "needs_ocr"
        assert summary["groups"][0]["message"] == "PDF page 2 has no native text"
        assert summary["groups"][0]["row_ids"] == [row_id]
        assert (
            project.db.execute("SELECT COUNT(*) FROM prepared_content_refs").fetchone()[
                0
            ]
            == 0
        )


def test_page_scoped_cell_keeps_original_page_identity(
    tmp_path: Path, monkeypatch
) -> None:
    from frisket.engine import pdf_text

    observed: list[Path] = []

    async def extract(source: Path, **_kwargs):
        observed.append(source)
        return [_page(1, "SECOND PAGE NATIVE")]

    monkeypatch.setattr(pdf_text, "extract_pdf_text", extract)
    with closing(Project.create(tmp_path / "selected.frisket")) as project:
        sheet_id, _source_id, row_id, digest = _source(project, tmp_path, page=2)
        result = run_action_spec(
            project,
            _request(sheet_id, row_id, key="native-selected-page"),
            project_id=PROJECT_ID,
        )

        assert result.status == "completed", result.errors
        assert len(observed) == 1 and observed[0].name == "selected-page.pdf"
        output = next(item for item in result.outputs if item.name == "prepared text")
        raw = project.db.execute(
            "SELECT value FROM results WHERE run_id=? AND row_id=? AND column_id=?",
            (result.run_id, row_id, output.column_id),
        ).fetchone()
        prepared = PreparedContentStore(project).resolve(int(raw["value"]))
        assert prepared.page_number == 2
        assert [pin.page_number for pin in prepared.pins] == [2]
        artifact = project.db.execute(
            "SELECT blob_hash,page_count FROM source_artifacts WHERE id=?",
            (prepared.source_artifact_id,),
        ).fetchone()
        assert (artifact["blob_hash"], artifact["page_count"]) == (digest, 2)
        assert (
            project.db.execute(
                "SELECT span.page_start FROM source_spans AS span "
                "JOIN evidence_link_spans AS member ON member.span_id=span.id "
                "JOIN evidence_links AS link ON link.id=member.link_id "
                "WHERE link.run_id=? AND link.column_id=? "
                "ORDER BY span.id LIMIT 1",
                (result.run_id, output.column_id),
            ).fetchone()[0]
            == 2
        )
