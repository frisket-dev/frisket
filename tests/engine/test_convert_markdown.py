"""Engine-selectable document-to-Markdown conversion.

- markitdown (no-ML) converts html/docx/text-PDF offline in the base env,
  from inline content, local files, or ingested blobs
- the output column lands as text with format='markdown' (markdown-cells)
- docling lives in the frisket-models sidecar (POST /to-markdown)
- renamed from convert_markdown; the retired id is noncanonical and resolves
  no action
"""

import zipfile

import pytest

from frisket.ai.llm import ModelRouter
from frisket.engine.store import Project
from frisket.engine.executor import run_action_spec

HTML = (
    "<html><body><h1>Quarterly Report</h1>"
    "<p>Revenue <b>doubled</b> this year.</p>"
    "<ul><li>north region</li><li>south region</li></ul>"
    "</body></html>"
)


def _project(tmp_path, rows, coltype="text"):
    p = Project.create(tmp_path / "t.frisket", name="t")
    sheet = p.add_sheet("data")
    cols = {k: p.add_column(sheet, k, type=coltype) for k in rows[0]}
    p.add_rows(sheet, rows, cols)
    return p, sheet


def _spec(sheet, engine=None, **extra):
    spec = {
        "action_id": "media.to_markdown",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {"source": "doc"},
        "idempotency_key": "markdown-conversion",
    }
    if engine:
        spec["params"]["engine"] = engine
    if "output_name" in extra:
        spec["output_names"] = {"markdown": extra.pop("output_name")}
    spec.update(extra)
    return spec


def _run(p, spec, router=None):
    router = router or ModelRouter(cache=None, cache_mode="off")
    return run_action_spec(p, spec, project_id="markdown", router=router)


def _col(p, sheet, name):
    return next(c for c in p.columns(sheet) if c["name"] == name)


def _col_value(p, sheet, name):
    (val,) = p.get_values(sheet, _col(p, sheet, name)["id"]).values()
    return val


def _make_docx(path):
    """Minimal hand-built docx (mammoth-parsable): heading + paragraph."""
    ct = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
        'content-types">'
        '<Default Extension="rels" ContentType="application/'
        'vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/'
        "vnd.openxmlformats-officedocument.wordprocessingml.document"
        '.main+xml"/></Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/'
        'package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    )
    doc = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body>'
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        "<w:r><w:t>Budget Memo</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Spending rose sharply.</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)
    return path


def _make_pdf(path, text="Audit Findings 2026"):
    """Minimal hand-built text-layer PDF (one Helvetica Tj op)."""
    content = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode()
    objs = [
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n",
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n",
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n",
        b"4 0 obj<</Length "
        + str(len(content)).encode()
        + b">>stream\n"
        + content
        + b"\nendstream\nendobj\n",
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for o in objs:
        offsets.append(len(out))
        out += o
    xref = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n"
    for off in offsets:
        out += (f"{off:010d} 00000 n \n").encode()
    out += (
        b"trailer<</Size 6/Root 1 0 R>>\nstartxref\n" + str(xref).encode() + b"\n%%EOF"
    )
    path.write_bytes(bytes(out))
    return path


def test_convert_inline_html_light_engine(tmp_path):
    """Raw HTML held in a text cell converts to markdown with structure
    preserved, and the output column is format='markdown'."""
    p, sheet = _project(tmp_path, [{"doc": HTML}])
    prog = _run(p, _spec(sheet, engine="markitdown"))
    assert prog.status == "completed", prog.errors
    text = _col_value(p, sheet, "markdown")
    assert "# Quarterly Report" in text
    assert "**doubled**" in text
    assert "north region" in text and "south region" in text
    col = _col(p, sheet, "markdown")
    assert col["type"] == "text" and col["format"] == "markdown"
    # ocr_used is a sidecar-contract field; markitdown never OCRs,
    # so no provenance column appears
    assert not any(c["name"] == "ocr_used" for c in p.columns(sheet))
    p.close()


def test_convert_docx_file_path(tmp_path):
    """A docx on disk converts offline — heading becomes an h1. Pins the
    path-probe's ALLOWED case: a bare path string in a media-typed ('file')
    column resolves to the file on trusted deploys."""
    docx = _make_docx(tmp_path / "memo.docx")
    p, sheet = _project(tmp_path, [{"doc": str(docx)}], coltype="file")
    prog = _run(p, _spec(sheet))  # markitdown is the default engine
    assert prog.status == "completed", prog.errors
    text = _col_value(p, sheet, "markdown")
    assert "# Budget Memo" in text
    assert "Spending rose sharply." in text
    p.close()


def test_convert_text_pdf(tmp_path):
    """A text-layer PDF converts via markitdown (no OCR/ML)."""
    pdf = _make_pdf(tmp_path / "audit.pdf")
    p, sheet = _project(tmp_path, [{"doc": str(pdf)}], coltype="file")
    prog = _run(p, _spec(sheet, engine="markitdown"))
    assert prog.status == "completed", prog.errors
    assert "Audit Findings 2026" in _col_value(p, sheet, "markdown")
    p.close()


def test_convert_text_column_path_is_content_not_file(tmp_path):
    """The path probe is gated on media-typed columns: a
    TEXT cell whose value happens to name a real file on disk is inline
    content to convert, not a file reference — otherwise any single-line
    text cell could read arbitrary server files on trusted deploys."""
    secret = tmp_path / "secret.html"
    secret.write_text("<html><body><p>TOPSECRET payroll</p></body></html>")
    p, sheet = _project(tmp_path, [{"doc": str(secret)}], coltype="text")
    prog = _run(p, _spec(sheet))
    assert prog.status == "completed", prog.errors
    text = _col_value(p, sheet, "markdown")
    assert "TOPSECRET" not in text  # file never read
    assert str(secret) in text  # the cell's literal value converted as text
    p.close()


def test_convert_blob_input_gets_mime_extension(tmp_path):
    """Ingested blobs (hash-named, no extension) convert using the mime
    hint; output_name is honored."""
    mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    docx = _make_docx(tmp_path / "memo.docx")
    p = Project.create(tmp_path / "t.frisket", name="t")
    sheet = p.add_sheet("data")
    cid = p.add_column(sheet, "doc", type="file")
    h = p.add_blob(docx.read_bytes(), filename="memo.docx", mime=mime)
    p.add_rows(sheet, [{"doc": {"blob": h, "mime": mime}}], {"doc": cid})
    prog = _run(p, _spec(sheet, output_name="doc_md"))
    assert prog.status == "completed", prog.errors
    assert "# Budget Memo" in _col_value(p, sheet, "doc_md")
    p.close()


def test_convert_unknown_engine_refuses_the_run(tmp_path):
    p, sheet = _project(tmp_path, [{"doc": HTML}])
    try:
        spec = _spec(sheet, engine="nonsense")
        result = _run(p, spec)
        if result.status == "needs_confirmation":
            result = _run(
                p,
                {**spec, "confirmation": result.errors[0].details["promise_set_hash"]},
            )
        assert result.status == "failed", result.errors
        assert "nonsense" in str(result.errors)
        assert not any(c["name"] == "markdown" for c in p.columns(sheet))
    finally:
        p.close()


def test_convert_docling_needs_sidecar_url(tmp_path, monkeypatch):
    monkeypatch.delenv("FRISKET_MODELS_URL", raising=False)
    p, sheet = _project(tmp_path, [{"doc": HTML}])
    try:
        result = _run(p, _spec(sheet, engine="docling"))
        assert result.status == "failed", result.errors
        assert "FRISKET_MODELS_URL" in str(result.errors)
        assert not any(c["name"] == "markdown" for c in p.columns(sheet))
    finally:
        p.close()


def test_convert_sidecar_posts_bytes_with_bearer(tmp_path, monkeypatch):
    """engine='docling' speaks the settled sidecar contract:
    POST /to-markdown with multipart blob bytes + bearer token. The sidecar
    egresses to the operator's own LAN service, but the known-free route
    remains below the confirmation threshold."""
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {
                "documents": [{"markdown": "# From Sidecar", "ocr_used": [True, False]}]
            }

    class FakeHttp:
        async def post(
            self,
            url,
            files=None,
            data=None,
            headers=None,
            follow_redirects=False,
            timeout=None,
        ):
            captured.update(
                url=url,
                files=files,
                data=data,
                headers=headers,
                follow_redirects=follow_redirects,
                timeout=timeout,
            )
            return FakeResponse()

    class StubRouter:
        client = FakeHttp()

    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:9000")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sekrit")
    monkeypatch.setenv("FRISKET_TRANSCRIPTION_SIDECAR_TIMEOUT_SECONDS", "47")
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")

    pdf = _make_pdf(tmp_path / "scan.pdf")
    p, sheet = _project(tmp_path, [{"doc": str(pdf)}], coltype="file")
    prog = _run(p, _spec(sheet, engine="docling"), StubRouter())
    assert prog.status == "completed", prog.errors
    assert captured["url"] == "http://models:9000/to-markdown"
    assert captured["headers"]["Authorization"] == "Bearer sekrit"
    assert captured["follow_redirects"] is True
    assert captured["timeout"].connect == 10.0
    assert captured["timeout"].read == 47.0
    assert captured["timeout"].write == 47.0
    assert captured["timeout"].pool == 47.0
    assert captured["data"]["engine"] == "docling"
    assert captured["files"][0][0] == "files"  # multipart document bytes
    assert _col_value(p, sheet, "markdown") == "# From Sidecar"
    assert _col_value(p, sheet, "ocr_used") == [True, False]
    assert _col(p, sheet, "ocr_used")["type"] == "json"
    p.close()


def test_convert_listed_in_action_catalog(tmp_path):
    """The document-conversion action is discoverable via the v1 catalog."""
    from fastapi.testclient import TestClient

    from frisket.server.app import create_app

    app = create_app(tmp_path / "ws", router=ModelRouter(cache=None, cache_mode="off"))
    client = TestClient(app)
    entries = {
        entry["kind"]: entry
        for entry in client.get("/api/actions/v1/catalog").json()["actions"]
    }
    assert "media.to_markdown" in entries
    to_markdown = entries["media.to_markdown"]
    assert to_markdown["ui_hints"]["uses_model"] is False
    assert "markdown" in to_markdown["description"].lower()
    engines = {e["id"]: e for e in to_markdown["ui_hints"]["engines"]}
    assert engines["markitdown"]["tier"] == "local"
    assert engines["markitdown"]["available"] is True
    assert engines["docling"]["tier"] == "sidecar"
    assert engines["docling"]["available"] is False
    # the old kind is a lookup-time alias, not a listed catalog action.
    assert "convert_markdown" not in entries


def test_convert_markdown_kind_is_retired_vocabulary(tmp_path):
    """Specs saved before the rename said recipe='convert_markdown'; the
    open-time bundle migration rewrites them, and the registry refuses the
    retired spelling outright."""
    from frisket.ops import get_recipe

    with pytest.raises(ValueError, match="not canonical"):
        get_recipe("convert_markdown")

    p, sheet = _project(tmp_path, [{"doc": HTML}])
    spec = _spec(sheet, engine="markitdown")
    assert spec["action_id"] == "media.to_markdown"
    prog = _run(p, spec)
    assert prog.status == "completed", prog.errors
    assert "# Quarterly Report" in _col_value(p, sheet, "markdown")
    p.close()
