from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from pypdf import PdfReader, PdfWriter

from frisket.engine.store.media_blobs import MediaBlobStore, media_cell
from frisket.ops.ocr_engines import (
    RAPIDOCR_MODELS_NOT_PROVISIONED,
    OcrEngines,
    rapidocr_models_present,
)
from frisket.engine.store import Project

from helpers import stub_rapidocr_run_scope


DPI = 200


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _source_pdf(n_pages: int = 1, w: float = 612.0, h: float = 792.0) -> bytes:
    writer = PdfWriter()
    for _ in range(n_pages):
        writer.add_blank_page(width=w, height=h)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _line_block(text: str, x0: int, y0: int, x1: int, y1: int) -> dict[str, Any]:
    return {
        "text": text,
        "bbox": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
        "score": 0.99,
    }


def _stub_page(text: str) -> dict[str, Any]:
    return {"text": text, "blocks": [_line_block(text, 100, 100, 700, 170)]}


def _project_with_media(
    tmp_path: Path,
    payload: bytes,
    *,
    filename: str = "scan.pdf",
    mime: str = "application/pdf",
) -> tuple[Project, dict[str, Any]]:
    project = Project.create(tmp_path / "ocr-pdf.frisket", name="OCR PDF")
    digest = project.add_blob(payload, filename=filename, mime=mime)
    cell = media_cell(digest, mime=mime, filename=filename)
    return project, cell


def _patch_engine(monkeypatch: pytest.MonkeyPatch, pages: list[dict[str, Any]]) -> None:
    async def fake_page_images(self, path, media, spec, scratch):  # noqa: ANN001
        del self, media, spec, scratch
        # one path per page is enough — the stubbed engine ignores it.
        return [path for _ in pages]

    async def fake_rapidocr(self, page_paths, scratch, language=None):  # noqa: ANN001
        del self, page_paths, scratch, language
        return [dict(p) for p in pages]

    monkeypatch.setattr(OcrEngines, "_page_images", fake_page_images)
    monkeypatch.setattr(OcrEngines, "_ocr_rapidocr", fake_rapidocr)
    stub_rapidocr_run_scope(monkeypatch)


def _run(project, cell, **params):
    from frisket.engine.executor import run_action_spec

    sheet = project.add_sheet("Scans")
    column = project.add_column(sheet, "media", type="file")
    row_id = project.add_rows(sheet, [{"media": cell}], {"media": column})[0]
    names = {"text": "ocr_text", "blocks": "ocr_text_blocks"}
    if params.get("searchable_pdf"):
        names["pdf"] = "ocr_text_pdf"
    result = run_action_spec(
        project,
        {
            "action_id": "media.ocr",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"source": "media", **params},
            "output_names": names,
            "idempotency_key": "ocr-pdf-domain",
        },
        project_id="ocr-pdf",
    )
    assert result.status in {"completed", "partial"}, result.errors
    values, errors = {}, {}
    for field in project.db.execute(
        "SELECT * FROM columns WHERE sheet_id=?", (sheet,)
    ).fetchall():
        if field["name"] == "media":
            continue
        values[field["name"]] = project.get_values(sheet, field["id"]).get(row_id)
        error = project.db.execute(
            "SELECT error FROM results WHERE run_id=? AND row_id=? AND column_id=?",
            (result.run_id, row_id, field["id"]),
        ).fetchone()
        if error and error["error"]:
            errors[field["name"]] = error["error"]
    return result, values, errors


def _fields(**params):
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.types import ActionRequest

    return ACTION_REGISTRY.get("media.ocr").bind_request(
        ActionRequest(
            action_id="media.ocr",
            idempotency_key="ocr-fields-test",
            scope={"kind": "sheet_rows", "sheet_id": 1},
            params={"source": "media", **params},
        )
    )[1]


def test_output_fields_default_off_is_unchanged() -> None:
    assert [f.key for f in _fields()] == ["text", "blocks"]
    assert _fields() == _fields(searchable_pdf=False)


def test_output_fields_searchable_pdf_appends_file_column() -> None:
    fields = _fields(searchable_pdf=True)
    assert [f.key for f in fields] == ["text", "blocks", "pdf"]
    assert fields[-1].column_type == "file"
    assert fields[-1].schema["type"] == "object"


def test_output_fields_pdf_column_respects_output_name() -> None:
    pdf = _fields(searchable_pdf=True)[-1]
    assert pdf.materialized_name({"pdf": "receipt_pdf"}) == "receipt_pdf"


# --------------------------------------------------------------------------
# params contract + remote-VLM rejection
# --------------------------------------------------------------------------


def _params(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source": "media",
        "engine": "rapidocr",
    }
    base.update(over)
    return base


def test_params_default_searchable_pdf_false() -> None:
    from frisket.actions.media import OcrParams

    params = OcrParams.model_validate(_params())
    assert params.searchable_pdf is False


def test_params_allows_searchable_pdf_for_local_engine() -> None:
    from frisket.actions.media import OcrParams

    params = OcrParams.model_validate(_params(engine="rapidocr", searchable_pdf=True))
    assert params.searchable_pdf is True


def test_params_allows_searchable_pdf_for_sidecar_engine() -> None:
    from frisket.actions.media import OcrParams

    params = OcrParams.model_validate(_params(engine="dots.mocr", searchable_pdf=True))
    assert params.searchable_pdf is True


def test_params_rejects_searchable_pdf_for_remote_vlm() -> None:
    from pydantic import ValidationError

    from frisket.actions.media import OcrParams

    with pytest.raises(ValidationError) as excinfo:
        OcrParams.model_validate(
            _params(engine="gemini/gemini-2.5-flash", searchable_pdf=True)
        )
    assert "searchable_pdf_unsupported_engine" in str(excinfo.value)


def test_params_remote_vlm_without_searchable_pdf_is_ok() -> None:
    from frisket.actions.media import OcrParams

    params = OcrParams.model_validate(_params(engine="gemini/gemini-2.5-flash"))
    assert params.searchable_pdf is False


def test_validate_action_spec_rejects_remote_vlm_searchable_pdf() -> None:
    from frisket.actions.system import (
        validate_root_action as validate_action_spec,
    )

    spec = {
        "action_id": "media.ocr",
        "scope": {"kind": "sheet_rows", "sheet_id": 12},
        "params": {
            "source": "media",
            "engine": "openai/gpt-4o",
            "searchable_pdf": True,
        },
        "idempotency_key": "ocr@sha256:x",
    }
    result = validate_action_spec(spec)
    assert result.ok is False
    assert result.error.code == "invalid_action_request"
    assert "searchable_pdf_unsupported_engine" in result.error.message


# --------------------------------------------------------------------------
# execute — compose path (dep-less: stub blocks + real compositor)
# --------------------------------------------------------------------------


def test_execute_default_off_produces_no_pdf_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_engine(monkeypatch, [_stub_page("HELLO WORLD")])
    project, cell = _project_with_media(tmp_path, _source_pdf(1))
    spec = {"engine": "rapidocr", "dpi": DPI}
    result, out, field_errors = _run(project, cell, **spec)
    assert set(out) == {"ocr_text", "ocr_text_blocks"}
    assert "ocr_text_pdf" not in out


def test_execute_pdf_mode_produces_searchable_pdf_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_engine(monkeypatch, [_stub_page("HELLO WORLD")])
    project, cell = _project_with_media(tmp_path, _source_pdf(1))
    spec = {
        "engine": "rapidocr",
        "dpi": DPI,
        "searchable_pdf": True,
    }
    result, out, field_errors = _run(project, cell, **spec)
    # text + blocks succeed exactly as before.
    assert out["ocr_text"] == "HELLO WORLD"
    assert out["ocr_text_blocks"][0]["blocks"][0]["text"] == "HELLO WORLD"
    # the _pdf cell is a real blob envelope pointing at a searchable PDF.
    pdf_cell = out["ocr_text_pdf"]
    assert isinstance(pdf_cell, dict)
    assert pdf_cell["mime"] == "application/pdf"
    pdf_bytes = project.read_blob(pdf_cell["blob"])
    assert pdf_bytes.startswith(b"%PDF-")
    layer = PdfReader(io.BytesIO(pdf_bytes)).pages[0].extract_text()
    assert "HELLO WORLD" in layer.replace("\n", " ")
    # degraded-page bookkeeping stays available on the blob metadata (SS9).
    assert set(pdf_cell) == {"blob", "mime", "filename"}
    assert MediaBlobStore(project).probe_metadata(pdf_cell["blob"])["pages"] == 1


def test_execute_pdf_mode_multi_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_engine(monkeypatch, [_stub_page("PAGE ONE"), _stub_page("PAGE TWO")])
    project, cell = _project_with_media(tmp_path, _source_pdf(2))
    spec = {
        "engine": "rapidocr",
        "dpi": DPI,
        "searchable_pdf": True,
    }
    result, out, field_errors = _run(project, cell, **spec)
    pdf_bytes = project.read_blob(out["ocr_text_pdf"]["blob"])
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) == 2
    assert "PAGE ONE" in reader.pages[0].extract_text().replace("\n", " ")
    assert "PAGE TWO" in reader.pages[1].extract_text().replace("\n", " ")


def test_execute_uses_output_name_template_for_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_engine(monkeypatch, [_stub_page("HELLO")])
    project, cell = _project_with_media(
        tmp_path, _source_pdf(1), filename="quarterly-report.pdf"
    )
    spec = {
        "engine": "rapidocr",
        "dpi": DPI,
        "searchable_pdf": True,
    }
    result, out, field_errors = _run(project, cell, **spec)
    fname = out["ocr_text_pdf"]["filename"]
    assert fname.startswith("quarterly-report")
    assert fname.endswith(".pdf")
    assert "searchable" in fname


# --------------------------------------------------------------------------
# execute — failure semantics (per-cell _pdf error, text/blocks intact)
# --------------------------------------------------------------------------


def test_execute_compose_failure_errors_pdf_cell_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_engine(monkeypatch, [_stub_page("HELLO WORLD")])
    # a corrupt "PDF" body: OCR text/blocks were produced from stubs, but the
    # compositor cannot parse the source, so only the _pdf cell fails.
    project, cell = _project_with_media(tmp_path, b"%PDF-1.4 not really a pdf")
    spec = {
        "engine": "rapidocr",
        "dpi": DPI,
        "searchable_pdf": True,
    }
    result, out, field_errors = _run(project, cell, **spec)
    assert out["ocr_text"] == "HELLO WORLD"
    assert out["ocr_text_blocks"][0]["blocks"][0]["text"] == "HELLO WORLD"
    assert out.get("ocr_text_pdf") is None
    assert "ocr_text_pdf" in field_errors
    assert field_errors["ocr_text_pdf"]


def test_execute_image_input_errors_pdf_cell_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_engine(monkeypatch, [_stub_page("HELLO WORLD")])
    # a single image has no source PDF to sandwich onto (v1 non-goal, SS12).
    project, cell = _project_with_media(
        tmp_path, b"\x89PNG\r\n\x1a\n", filename="scan.png", mime="image/png"
    )
    spec = {
        "engine": "rapidocr",
        "dpi": DPI,
        "searchable_pdf": True,
    }
    result, out, field_errors = _run(project, cell, **spec)
    assert out["ocr_text"] == "HELLO WORLD"
    assert out.get("ocr_text_pdf") is None
    assert "ocr_text_pdf" in field_errors


# --------------------------------------------------------------------------
# executor integration — full action run, per-cell partial semantics
# --------------------------------------------------------------------------


def _seed_pdf_project(tmp_path: Path) -> tuple[Project, int, list[int]]:
    project = Project.create(tmp_path / "action.frisket", name="OCR action")
    sheet_id = project.add_sheet("Scans")
    cols = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "media": project.add_column(sheet_id, "media", type="file"),
    }
    digest = project.add_blob(
        _source_pdf(1), filename="scan.pdf", mime="application/pdf"
    )
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "title": "Scan",
                "media": media_cell(
                    digest, mime="application/pdf", filename="scan.pdf"
                ),
            }
        ],
        cols,
    )
    return project, sheet_id, row_ids


def _ocr_action(sheet_id: int, row_ids: list[int]) -> dict[str, Any]:
    return {
        "action_id": "media.ocr",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": row_ids},
        "output_names": {
            "text": "ocr_text",
            "blocks": "ocr_text_blocks",
            "pdf": "ocr_text_pdf",
        },
        "params": {
            "source": "media",
            "engine": "rapidocr",
            "dpi": DPI,
            "searchable_pdf": True,
        },
        "idempotency_key": "media_ocr_searchable@sha256:one",
    }


def test_action_run_creates_pdf_file_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.executor import run_action_spec

    _patch_engine(monkeypatch, [_stub_page("HELLO WORLD")])
    project, sheet_id, row_ids = _seed_pdf_project(tmp_path)
    result = run_action_spec(
        project,
        _ocr_action(sheet_id, row_ids),
        project_id="proj-ocr-pdf",
    )
    assert result.status == "completed"
    assert {o.name for o in result.outputs} == {
        "ocr_text",
        "ocr_text_blocks",
        "ocr_text_pdf",
    }
    columns = {
        str(c["name"]): c
        for c in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=?", (sheet_id,)
        ).fetchall()
    }
    assert columns["ocr_text_pdf"]["type"] == "file"
    pdf_cell = project.get_values(
        sheet_id, int(columns["ocr_text_pdf"]["id"]), row_ids=[row_ids[0]]
    )[row_ids[0]]
    assert pdf_cell["mime"] == "application/pdf"
    pdf_bytes = project.read_blob(pdf_cell["blob"])
    assert pdf_bytes.startswith(b"%PDF-")


def test_action_run_compose_failure_errors_only_pdf_cell_text_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.executor import run_action_spec

    _patch_engine(monkeypatch, [_stub_page("HELLO WORLD")])
    project = Project.create(tmp_path / "action-fail.frisket", name="OCR fail")
    sheet_id = project.add_sheet("Scans")
    cols = {"media": project.add_column(sheet_id, "media", type="file")}
    digest = project.add_blob(
        b"%PDF-1.4 broken", filename="broken.pdf", mime="application/pdf"
    )
    row_ids = project.add_rows(
        sheet_id,
        [{"media": media_cell(digest, mime="application/pdf", filename="broken.pdf")}],
        cols,
    )
    run_action_spec(
        project,
        _ocr_action(sheet_id, row_ids),
        project_id="proj-ocr-fail",
    )
    columns = {
        str(c["name"]): c
        for c in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=?", (sheet_id,)
        ).fetchall()
    }
    # text + blocks still landed for the row (composition is a post-step, SS9).
    text_val = project.get_values(
        sheet_id, int(columns["ocr_text"]["id"]), row_ids=[row_ids[0]]
    )[row_ids[0]]
    assert text_val == "HELLO WORLD"

    def _result(col_name: str):
        return project.db.execute(
            "SELECT error, outcome FROM results WHERE row_id=? AND column_id=?",
            (row_ids[0], int(columns[col_name]["id"])),
        ).fetchone()

    # only the _pdf cell carries the error; text/blocks are clean.
    pdf_res = _result("ocr_text_pdf")
    assert pdf_res["error"]
    assert pdf_res["outcome"] == "model_error"
    assert _result("ocr_text")["error"] is None
    assert _result("ocr_text_blocks")["error"] is None


def test_zero_success_pdf_field_error_keeps_successful_text_outputs_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.executor import run_action_spec

    _patch_engine(monkeypatch, [_stub_page("HELLO WORLD")])
    project = Project.create(
        tmp_path / "action-zero-success-field-aware.frisket",
        name="OCR field-aware",
    )
    sheet_id = project.add_sheet("Scans")
    cols = {"media": project.add_column(sheet_id, "media", type="file")}
    digest = project.add_blob(
        b"%PDF-1.4 broken", filename="broken.pdf", mime="application/pdf"
    )
    row_ids = project.add_rows(
        sheet_id,
        [{"media": media_cell(digest, mime="application/pdf", filename="broken.pdf")}],
        cols,
    )

    run_action_spec(
        project,
        _ocr_action(sheet_id, row_ids),
        project_id="proj-ocr-field-aware",
    )

    columns = {
        str(c["name"]): c
        for c in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=?", (sheet_id,)
        ).fetchall()
    }
    assert int(columns["ocr_text"]["hidden"]) == 0
    assert int(columns["ocr_text_blocks"]["hidden"]) == 0
    assert int(columns["ocr_text_pdf"]["hidden"]) == 1
    assert (
        project.get_values(
            sheet_id, int(columns["ocr_text"]["id"]), row_ids=[row_ids[0]]
        )[row_ids[0]]
        == "HELLO WORLD"
    )


def test_action_run_is_partial_when_only_some_rows_fail_to_compose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mix of a composable and a corrupt source PDF ⇒ partial (existing
    media_action_status): the good row gets its searchable PDF, the corrupt row
    keeps its text but errors the _pdf cell (spec SS9)."""
    from frisket.engine.executor import run_action_spec

    _patch_engine(monkeypatch, [_stub_page("HELLO WORLD")])
    project = Project.create(tmp_path / "action-mixed.frisket", name="OCR mixed")
    sheet_id = project.add_sheet("Scans")
    cols = {"media": project.add_column(sheet_id, "media", type="file")}
    good = project.add_blob(_source_pdf(1), filename="ok.pdf", mime="application/pdf")
    broken = project.add_blob(
        b"%PDF-1.4 broken", filename="broken.pdf", mime="application/pdf"
    )
    row_ids = project.add_rows(
        sheet_id,
        [
            {"media": media_cell(good, mime="application/pdf", filename="ok.pdf")},
            {
                "media": media_cell(
                    broken, mime="application/pdf", filename="broken.pdf"
                )
            },
        ],
        cols,
    )
    action = _ocr_action(sheet_id, row_ids)
    action["idempotency_key"] = "media_ocr_searchable@sha256:mixed"
    result = run_action_spec(
        project,
        action,
        project_id="proj-ocr-mixed",
    )
    assert result.status == "partial"
    columns = {
        str(c["name"]): c
        for c in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=?", (sheet_id,)
        ).fetchall()
    }
    pdf_col = int(columns["ocr_text_pdf"]["id"])
    good_pdf = project.get_values(sheet_id, pdf_col, row_ids=[row_ids[0]])[row_ids[0]]
    assert good_pdf and good_pdf["mime"] == "application/pdf"
    broken_pdf = project.get_values(sheet_id, pdf_col, row_ids=[row_ids[1]]).get(
        row_ids[1]
    )
    assert broken_pdf is None
    # the corrupt row still produced its text.
    text_col = int(columns["ocr_text"]["id"])
    assert (
        project.get_values(sheet_id, text_col, row_ids=[row_ids[1]])[row_ids[1]]
        == "HELLO WORLD"
    )


# --------------------------------------------------------------------------
# golden — the REAL rapidocr engine + pdftoppm (heavy; importorskip'd)
# --------------------------------------------------------------------------


def test_rapidocr_golden_real_searchable_pdf(tmp_path: Path) -> None:
    import shutil

    pytest.importorskip("rapidocr")
    Image = pytest.importorskip("PIL.Image")
    ImageDraw = pytest.importorskip("PIL.ImageDraw")
    if shutil.which("pdftoppm") is None:
        pytest.skip("poppler pdftoppm not on PATH")
    if not rapidocr_models_present()[0]:
        pytest.skip(RAPIDOCR_MODELS_NOT_PROVISIONED)

    # a single-page PDF whose visible content is a rasterized big-text image.
    img = Image.new("RGB", (1200, 300), "white")
    draw = ImageDraw.ImageDraw(img)
    draw.text((60, 110), "INVOICE", fill="black")
    pdf_io = io.BytesIO()
    img.save(pdf_io, format="PDF", resolution=200.0)
    project, cell = _project_with_media(
        tmp_path, pdf_io.getvalue(), filename="invoice.pdf"
    )
    spec = {
        "engine": "rapidocr",
        "dpi": 200,
        "searchable_pdf": True,
    }
    result, out, field_errors = _run(project, cell, **spec)
    pdf_cell = out["ocr_text_pdf"]
    assert isinstance(pdf_cell, dict)
    pdf_bytes = project.read_blob(pdf_cell["blob"])
    assert pdf_bytes.startswith(b"%PDF-")
    layer = PdfReader(io.BytesIO(pdf_bytes)).pages[0].extract_text()
    assert layer.strip()  # the invisible OCR layer carries real, searchable text
