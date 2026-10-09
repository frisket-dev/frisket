from __future__ import annotations

import sys
from types import SimpleNamespace

from frisket_models import engines, lightonocr, pdfium_lock


class _AuditedLock:
    def __init__(self) -> None:
        self.depth = 0
        self.entries = 0

    def __enter__(self):
        self.depth += 1
        self.entries += 1
        return self

    def __exit__(self, *_exc_info) -> None:
        self.depth -= 1

    def assert_held(self) -> None:
        assert self.depth > 0


def _adapter() -> lightonocr.LightOnOCRAdapter:
    return lightonocr.LightOnOCRAdapter(
        engine="lightonocr-3-0.8b",
        model=None,
        processor=None,
        torch_module=None,
        device="cpu",
    )


def test_lighton_pdfium_calls_use_process_guard(monkeypatch) -> None:
    guard = _AuditedLock()
    monkeypatch.setattr(pdfium_lock, "PDFIUM_LOCK", guard)

    class Bitmap:
        def to_pil(self):
            guard.assert_held()
            return SimpleNamespace(
                convert=lambda _mode: image,
                close=lambda: None,
            )

        def close(self) -> None:
            guard.assert_held()

    image = SimpleNamespace(
        convert=lambda _mode: image,
        thumbnail=lambda _size: None,
        close=lambda: None,
    )

    class Page:
        def get_size(self):
            guard.assert_held()
            return (100, 200)

        def render(self, *, scale):
            del scale
            guard.assert_held()
            return Bitmap()

        def close(self) -> None:
            guard.assert_held()

    class PdfDocument:
        def __init__(self, _data):
            guard.assert_held()

        def __len__(self):
            guard.assert_held()
            return 1

        def __getitem__(self, _index):
            guard.assert_held()
            return Page()

        def close(self) -> None:
            guard.assert_held()

    monkeypatch.setitem(
        sys.modules, "pypdfium2", SimpleNamespace(PdfDocument=PdfDocument)
    )
    adapter = _adapter()

    def generate(_image, _prompt):
        assert guard.depth == 0
        return "page"

    monkeypatch.setattr(adapter, "_generate", generate)

    assert adapter.to_markdown("doc.pdf", b"%PDF-1.7") == {
        "markdown": "page",
        "ocr_used": [True],
    }
    assert guard.entries >= 1


def test_docling_conversion_uses_same_process_guard(monkeypatch) -> None:
    guard = _AuditedLock()
    monkeypatch.setattr(pdfium_lock, "PDFIUM_LOCK", guard)

    class DocumentConverter:
        def convert(self, _path):
            guard.assert_held()
            return SimpleNamespace(
                document=SimpleNamespace(export_to_markdown=lambda: "page"),
                pages=[],
            )

    monkeypatch.setitem(sys.modules, "docling", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "docling.document_converter",
        SimpleNamespace(DocumentConverter=DocumentConverter),
    )

    to_markdown = engines.load_docling()

    assert to_markdown("doc.pdf", b"%PDF-1.7") == {
        "markdown": "page",
        "ocr_used": [],
    }
    assert guard.entries == 1


def test_chandra_pdf_rasterization_uses_same_process_guard(monkeypatch) -> None:
    guard = _AuditedLock()
    monkeypatch.setattr(pdfium_lock, "PDFIUM_LOCK", guard)

    def load_file(_path, _options):
        guard.assert_held()
        return ["image"]

    class InferenceManager:
        def __init__(self, *, method):
            assert method == "hf"

        def generate(self, _batch, *, include_images):
            assert include_images is False
            return [SimpleNamespace(markdown="page")]

    monkeypatch.setitem(sys.modules, "chandra", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules, "chandra.input", SimpleNamespace(load_file=load_file)
    )
    monkeypatch.setitem(
        sys.modules,
        "chandra.model",
        SimpleNamespace(InferenceManager=InferenceManager),
    )
    monkeypatch.setitem(
        sys.modules,
        "chandra.model.schema",
        SimpleNamespace(BatchInputItem=lambda **options: options),
    )

    to_markdown = engines.load_chandra()

    assert to_markdown("doc.pdf", b"%PDF-1.7") == {
        "markdown": "page",
        "ocr_used": [True],
    }
    assert guard.entries == 1
