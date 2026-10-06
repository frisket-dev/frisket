from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from frisket.actions.types import PdfPage
from frisket.contracts.action import Receipt
from frisket.engine.executor import run_action_spec
from frisket.engine.executor.pdf_page_read import AdmittedPdfPageRenderer
from frisket.engine.store import Project

pypdf = pytest.importorskip("pypdf")

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00"
    b"\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _pdf_bytes() -> bytes:
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = pypdf.PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    for text in ("First page", "Second page"):
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(content)
    output = io.BytesIO()
    writer.write(output)
    writer.close()
    return output.getvalue()


def _action(path: Path, **params) -> dict:
    return {
        "action_id": "import.pdf",
        "scope": {"kind": "project"},
        "sheet_name": "Pages",
        "params": {
            "source": {"kind": "file", "path": str(path), "label": "docket.pdf"},
            **params,
        },
        "idempotency_key": "pdf-pages",
    }


def _counts(project: Project) -> dict[str, int]:
    return {
        table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "sheets",
            "columns",
            "rows",
            "ops",
            "receipts",
            "blobs",
            "source_artifacts",
            "source_spans",
            "evidence_links",
            "prepared_page_versions",
            "prepared_content_sets",
            "prepared_content_refs",
        )
    }


@pytest.mark.parametrize("render", ["images", "fallback", "disabled"])
def test_pdf_publication_retains_pages_original_and_replays_without_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, render: str
) -> None:
    raw = _pdf_bytes()
    source = tmp_path / "uploaded.pdf"
    source.write_bytes(raw)
    calls = []

    def render_pages(self, document, *, dpi):
        calls.append(dpi)
        with self._blobs.open_binary(document) as stream:
            assert stream.read() == raw
        if render == "fallback":
            return {}
        return {
            page: self._blobs.stage(
                io.BytesIO(PNG),
                filename=f"docket-p{page:04d}.png",
                mime="image/png",
                role=PdfPage(document=document, page=page),
            )
            for page in (1, 2)
        }

    monkeypatch.setattr(AdmittedPdfPageRenderer, "render", render_pages)
    project = Project.create(tmp_path / "pdf.frisket", name="PDF")
    try:
        action = _action(source, render_pages=render != "disabled", dpi=200)
        result = run_action_spec(project, action, project_id="pdf")
        assert result.status == "completed", result.errors
        sheet_id = result.outputs[0].sheet_id
        columns = result.outputs[0].ref["columns"]
        assert list(columns) == ["page", "text", "source"] + (
            ["page_image"] if render != "disabled" else []
        )
        assert project.row_count(sheet_id) == 2
        assert list(project.get_values(sheet_id, columns["page"]).values()) == [1, 2]
        assert [
            value.strip()
            for value in project.get_values(sheet_id, columns["text"]).values()
        ] == ["First page", "Second page"]
        digest = hashlib.sha256(raw).hexdigest()
        assert list(project.get_values(sheet_id, columns["source"]).values()) == [
            {
                "blob": digest,
                "filename": "docket.pdf",
                "mime": "application/pdf",
                "page": page,
            }
            for page in (1, 2)
        ]
        assert calls == ([] if render == "disabled" else [200])
        if render == "fallback":
            assert list(
                project.get_values(sheet_id, columns["page_image"]).values()
            ) == [None, None]
        artifact = project.db.execute(
            "SELECT * FROM source_artifacts WHERE blob_hash=?", (digest,)
        ).fetchone()
        assert artifact is not None
        assert artifact["artifact_kind"] == "file"
        assert artifact["media_type"] == "application/pdf"
        assert artifact["filename"] == "docket.pdf"
        assert artifact["page_count"] == 2
        assert artifact["source_sheet_id"] == sheet_id
        receipt = Receipt.model_validate(
            json.loads(
                project.db.execute(
                    "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
                ).fetchone()[0]
            )
        )
        assert bool(receipt.warnings) == (render == "fallback")
        document = next(
            item.ref
            for item in receipt.evidence
            if item.ref["kind"] == "imported_pdf_document"
        )
        assert document["hash"] == digest
        assert document["artifact_id"] == artifact["id"]
        assert document["size"] == len(raw)
        native_pages = [
            item.ref
            for item in receipt.evidence
            if item.ref["kind"] == "imported_pdf_page"
        ]
        assert [ref["page"] for ref in native_pages] == [1, 2]
        assert all(ref["hash"] == digest for ref in native_pages)
        assert all(ref["artifact_id"] == artifact["id"] for ref in native_pages)
        assert len({ref["evidence_link_id"] for ref in native_pages}) == 2
        stored_text = project.db.execute(
            "SELECT cell.value_kind,cell.value FROM cells AS cell "
            "JOIN rows AS row ON row.id=cell.row_id "
            "WHERE row.sheet_id=? AND cell.column_id=? ORDER BY row.position",
            (sheet_id, columns["text"]),
        ).fetchall()
        assert [row["value_kind"] for row in stored_text] == [
            "prepared_content_ref",
            "prepared_content_ref",
        ]
        assert project.db.execute(
            "SELECT COUNT(*) FROM prepared_content_sets"
        ).fetchone()[0] == 1
        prepared_pages = project.db.execute(
            "SELECT page_number,prepared_text FROM prepared_page_versions "
            "ORDER BY page_number"
        ).fetchall()
        assert [tuple(row) for row in prepared_pages] == [
            (1, "First page"),
            (2, "Second page"),
        ]
        file_read = next(
            item.ref for item in receipt.inputs if item.ref["kind"] == "local_file_read"
        )
        assert file_read["sha256"] == "sha256:" + digest
        assert file_read["byte_count"] == len(raw)
        if render == "images":
            images = list(project.get_values(sheet_id, columns["page_image"]).values())
            assert images == [
                {
                    "blob": hashlib.sha256(PNG).hexdigest(),
                    "filename": f"docket-p{page:04d}.png",
                    "mime": "image/png",
                }
                for page in (1, 2)
            ]
            refs = [
                item.ref
                for item in receipt.evidence
                if item.ref["kind"] == "imported_pdf_page_image"
            ]
            assert [ref["page"] for ref in refs] == [1, 2]
            assert all(ref["artifact_id"] == artifact["id"] for ref in refs)
            assert len({ref["evidence_link_id"] for ref in refs}) == 2
        # The original is not a cell value, but must remain a live GC root.
        assert project.gc_blobs()["blobs_removed"] == 0
        with project.materialize_blob(digest) as stored:
            assert stored.read_bytes() == raw
        before = _counts(project)
        source.unlink()

        def forbidden(*args, **kwargs):
            raise AssertionError("receipt replay must not render or reread its source")

        monkeypatch.setattr(AdmittedPdfPageRenderer, "render", forbidden)
        replay = run_action_spec(project, action, project_id="pdf")
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == result.receipt_id
        assert replay.op_ids == result.op_ids
        assert _counts(project) == before
        bundle_path = project.path
        project.close()
        project = Project(bundle_path)
        assert [
            value.strip()
            for value in project.get_values(sheet_id, columns["text"]).values()
        ] == ["First page", "Second page"]
        assert project.undo() == result.op_ids[0]
        assert project.redo() == result.op_ids[0]
        assert [
            value.strip()
            for value in project.get_values(sheet_id, columns["text"]).values()
        ] == ["First page", "Second page"]
    finally:
        project.close()


@pytest.mark.parametrize("dpi", [49, 601])
def test_pdf_invalid_dpi_refuses_before_source_or_publication(
    tmp_path: Path, dpi: int
) -> None:
    project = Project.create(tmp_path / "invalid.frisket", name="PDF")
    try:
        result = run_action_spec(
            project, _action(tmp_path / "missing.pdf", dpi=dpi), project_id="pdf"
        )
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_action_request"
        assert not any(_counts(project).values())
    finally:
        project.close()


def test_pdf_parse_failure_leaves_no_published_blob_or_hidden_sheet(
    tmp_path: Path,
) -> None:
    source = tmp_path / "invalid.pdf"
    source.write_bytes(b"not a PDF")
    project = Project.create(tmp_path / "invalid.frisket", name="PDF")
    try:
        result = run_action_spec(project, _action(source), project_id="pdf")
        assert result.status == "failed"
        assert result.errors[0].code == "pdf_parse_failed"
        assert not any(_counts(project).values())
    finally:
        project.close()


def test_pdf_page_text_failure_keeps_empty_cell_without_prepared_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(_pdf_bytes())
    original = pypdf._page.PageObject.extract_text
    calls = 0

    def extract_text(page, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("synthetic extraction failure")
        return original(page, *args, **kwargs)

    monkeypatch.setattr(pypdf._page.PageObject, "extract_text", extract_text)
    project = Project.create(tmp_path / "partial.frisket", name="PDF")
    try:
        result = run_action_spec(
            project,
            _action(source, render_pages=False),
            project_id="pdf",
        )
        assert result.status == "completed", result.errors
        sheet_id = result.outputs[0].sheet_id
        columns = result.outputs[0].ref["columns"]
        assert list(project.get_values(sheet_id, columns["text"]).values()) == [
            "First page",
            "",
        ]
        stored = project.db.execute(
            "SELECT cell.value_kind FROM cells AS cell "
            "JOIN rows AS row ON row.id=cell.row_id "
            "WHERE row.sheet_id=? AND cell.column_id=? ORDER BY row.position",
            (sheet_id, columns["text"]),
        ).fetchall()
        assert [row["value_kind"] for row in stored] == [
            "prepared_content_ref",
            "text",
        ]
        assert project.db.execute(
            "SELECT COUNT(*) FROM prepared_page_versions"
        ).fetchone()[0] == 1
        assert [
            value["page"]
            for value in project.get_values(sheet_id, columns["source"]).values()
        ] == [1, 2]
    finally:
        project.close()


@pytest.mark.parametrize("target", ["document", "page"])
@pytest.mark.parametrize("damage", ["metadata", "missing", "corrupt"])
def test_pdf_replay_refuses_damaged_document_or_page_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str, damage: str
) -> None:
    source = tmp_path / "source.pdf"
    raw = _pdf_bytes()
    source.write_bytes(raw)

    def render_pages(self, document, *, dpi):
        return {
            1: self._blobs.stage(
                io.BytesIO(PNG),
                filename="docket-p0001.png",
                mime="image/png",
                role=PdfPage(document=document, page=1),
            )
        }

    monkeypatch.setattr(AdmittedPdfPageRenderer, "render", render_pages)
    project = Project.create(tmp_path / "damaged.frisket", name="PDF")
    try:
        action = _action(source)
        first = run_action_spec(project, action, project_id="pdf")
        assert first.status == "completed", first.errors
        digest = hashlib.sha256(raw if target == "document" else PNG).hexdigest()
        if damage == "metadata":
            project.db.execute("DELETE FROM blobs WHERE hash=?", (digest,))
            project.db.commit()
        else:
            with project.materialize_blob(digest) as stored:
                if damage == "missing":
                    stored.unlink()
                else:
                    stored.write_bytes(b"corrupt published bytes")
        before = _counts(project)
        source.unlink()
        replay = run_action_spec(project, action, project_id="pdf")
        assert replay.status == "failed"
        assert replay.errors[0].code == "stale_replay"
        assert _counts(project) == before
    finally:
        project.close()
