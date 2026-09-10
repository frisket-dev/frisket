from __future__ import annotations

import io
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from frisket.actions.import_media import (
    ImportFilesParams,
    ImportPdfParams,
    file_columns,
    import_files,
    import_pdf,
)
from frisket.actions.types import PdfDocument, PdfPage, StagedFile, TableError
from frisket.engine.executor.local_file_read import AdmittedLocalFileReader


class _Stager:
    def __init__(self):
        self.files = {}
        self.roles = {}
        self.names = {}

    def stage(self, stream, *, filename, mime, role=None):
        data = b"".join(iter(lambda: stream.read(64 * 1024), b""))
        file = StagedFile(size=len(data))
        self.files[file] = data
        self.roles[file] = role
        self.names[file] = (filename, mime)
        return file

    @contextmanager
    def open_binary(self, file):
        with io.BytesIO(self.files[file]) as stream:
            yield stream


class _Renderer:
    def __init__(self, blobs, pages=()):
        self.blobs, self.pages = blobs, pages

    def render(self, document, *, dpi):
        return {
            page: self.blobs.stage(
                io.BytesIO(data),
                filename=f"document-p{page:04d}.png",
                mime="image/png",
                role=PdfPage(document=document, page=page),
            )
            for page, data in self.pages
        }


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        ([{"path": "/upload/source", "filename": "voice.wav"}], "audio"),
        ([{"path": "/upload/source", "filename": "movie.mp4"}], "video"),
        ([{"path": "/upload/card.png"}], "image"),
        ([{"path": "/upload/unrecognized.unknown-extension"}], "file"),
        ([{"path": "/upload/voice.wav", "mime": "audio"}], "file"),
        (
            [{"path": "/upload/voice.wav", "mime": "image/png"}],
            "image",
        ),
        ([{"path": "one.wav"}, {"path": "two.png"}], "file"),
    ],
)
def test_files_schema_uses_declared_mime_and_filename_fallbacks(sources, expected):
    columns = file_columns(ImportFilesParams(files=sources))
    assert [(column.key, column.type) for column in columns] == [
        ("filename", "text"),
        ("media", expected),
        ("size", "integer"),
    ]
    assert columns[2].format == "filesize"


def test_files_keep_input_order_and_emit_opaque_handles(tmp_path):
    first, second = tmp_path / "first.bin", tmp_path / "second.bin"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    params = ImportFilesParams(
        files=[{"path": str(second)}, {"path": str(first), "filename": "named.png"}]
    )
    reader, blobs = AdmittedLocalFileReader(), _Stager()
    try:
        table = import_files(params, reader, blobs)
        rows = [row.output.root for row in table.rows]
        assert len(blobs.files) == 2
        assert [row["filename"] for row in rows] == ["second.bin", "named.png"]
        assert [row["size"] for row in rows] == [6, 5]
        assert [blobs.files[row["media"]] for row in rows] == [b"second", b"first"]
        assert blobs.names[rows[1]["media"]] == ("named.png", "image/png")
        assert all(role is None for role in blobs.roles.values())
    finally:
        reader.close()


def test_files_open_and_stage_only_rows_consumed(tmp_path):
    first = tmp_path / "first.bin"
    first.write_bytes(b"first")
    missing = tmp_path / "not-opened.bin"
    params = ImportFilesParams(files=[{"path": str(first)}, {"path": str(missing)}])
    reader, blobs = AdmittedLocalFileReader(), _Stager()
    try:
        table = import_files(params, reader, blobs)
        rows = iter(table.rows)
        assert reader.facts == []
        assert blobs.files == {}

        first_row = next(rows).output.root
        assert first_row["filename"] == "first.bin"
        assert blobs.files[first_row["media"]] == b"first"
        assert [fact["path"] for fact in reader.facts] == [str(first.resolve())]

        with pytest.raises(TableError, match="readable local file"):
            next(rows)
        assert len(blobs.files) == 1
        assert [fact["path"] for fact in reader.facts] == [str(first.resolve())]
    finally:
        reader.close()


def test_pdf_extraction_failure_keeps_row_and_original_document(tmp_path, monkeypatch):
    pypdf = pytest.importorskip("pypdf")
    source = tmp_path / "document.pdf"
    source.write_bytes(b"test PDF input")

    def broken_text():
        raise ValueError("unsupported page text")

    monkeypatch.setattr(
        pypdf,
        "PdfReader",
        lambda stream: SimpleNamespace(
            pages=[
                SimpleNamespace(extract_text=lambda: "first page"),
                SimpleNamespace(extract_text=broken_text),
            ]
        ),
    )
    params = ImportPdfParams(source={"kind": "file", "path": str(source)})
    reader, blobs = AdmittedLocalFileReader(), _Stager()
    try:
        table = import_pdf(params, reader, blobs, _Renderer(blobs))
        assert not blobs.files  # Source staging and text extraction are lazy.
        assert [column.key for column in table.schema] == [
            "page",
            "text",
            "source",
            "page_image",
        ]
        assert [row.output.root for row in table.rows] == [
            {
                "page": 1,
                "text": "first page",
                "source": "document.pdf",
                "page_image": None,
            },
            {"page": 2, "text": "", "source": "document.pdf", "page_image": None},
        ]
        assert len(table.warnings) == 1
        assert len(blobs.files) == 1
        document = next(iter(blobs.files))
        assert isinstance(blobs.roles[document], PdfDocument)
        assert blobs.files[document] == b"test PDF input"
    finally:
        reader.close()


def test_pdf_identical_page_bytes_keep_distinct_document_page_roles(
    tmp_path, monkeypatch
):
    pypdf = pytest.importorskip("pypdf")
    source = tmp_path / "document.pdf"
    source.write_bytes(b"test PDF input")
    monkeypatch.setattr(
        pypdf,
        "PdfReader",
        lambda stream: SimpleNamespace(
            pages=[SimpleNamespace(extract_text=lambda: "")] * 2
        ),
    )

    reader, blobs = AdmittedLocalFileReader(), _Stager()
    try:
        table = import_pdf(
            ImportPdfParams(source={"kind": "file", "path": str(source)}),
            reader,
            blobs,
            _Renderer(blobs, [(1, b"same image"), (2, b"same image")]),
        )
        images = [row.output.root["page_image"] for row in table.rows]
        assert images[0] is not images[1]
        document = next(
            file for file, role in blobs.roles.items() if isinstance(role, PdfDocument)
        )
        assert [blobs.roles[image] for image in images] == [
            PdfPage(document=document, page=1),
            PdfPage(document=document, page=2),
        ]
        assert [blobs.names[image][0] for image in images] == [
            "document-p0001.png",
            "document-p0002.png",
        ]
    finally:
        reader.close()


def test_pdf_without_pages_refuses(tmp_path, monkeypatch):
    pypdf = pytest.importorskip("pypdf")
    source = tmp_path / "empty.pdf"
    source.write_bytes(b"empty PDF input")
    monkeypatch.setattr(pypdf, "PdfReader", lambda stream: SimpleNamespace(pages=[]))
    reader = AdmittedLocalFileReader()
    blobs = _Stager()
    try:
        with pytest.raises(TableError) as caught:
            table = import_pdf(
                ImportPdfParams(source={"kind": "file", "path": str(source)}),
                reader,
                blobs,
                _Renderer(blobs),
            )
            list(table.rows)
        assert caught.value.code == "pdf_parse_failed"
    finally:
        reader.close()
