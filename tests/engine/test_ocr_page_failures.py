"""Fatal local failures cross worker envelopes without becoming page warnings."""

import asyncio
import errno
import io
import json
import sys
from types import SimpleNamespace

import pytest

from frisket.engine import pdf_render
from frisket.engine._workers import pdf_render_worker, tesseract_worker
from frisket.engine.sandbox.shim import SandboxResult
from frisket.ops import ocr_engines, ocr_engines_local
from frisket.ops.base import RecipeInvocationHalt


def test_renderer_storage_failure_crosses_worker_envelope(
    tmp_path, monkeypatch, capsys
):
    closed = []

    class Document:
        def __len__(self):
            return 30

        def close(self):
            closed.append(True)

    def render(*args, **kwargs):
        raise OSError(errno.ENOSPC, "private document path")

    monkeypatch.setitem(
        sys.modules, "pypdfium2", SimpleNamespace(PdfDocument=lambda path: Document())
    )
    monkeypatch.setattr(pdf_render_worker, "render_page", render)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "source": str(tmp_path / "source.pdf"),
                    "output": str(tmp_path),
                    "dpi": 200,
                    "pages": [1],
                    "page_limit": None,
                    "max_edge": 2000,
                }
            )
        ),
    )
    pdf_render_worker.main()
    wire = capsys.readouterr().out
    assert json.loads(wire) == {"ok": False, "error": "environment_failed"}
    assert closed == [True]

    async def launch(*args, **kwargs):
        return SandboxResult(returncode=0, stdout=wire, stderr="")

    monkeypatch.setattr(pdf_render, "run_sandboxed", launch)
    monkeypatch.setattr(ocr_engines, "render_pdf_pages", pdf_render.render_pdf_pages)
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.4\nfake document")
    with pytest.raises(RecipeInvocationHalt, match="rendering environment failed"):
        asyncio.run(
            ocr_engines.OcrEngines()._page_images(
                source,
                {"mime": "application/pdf"},
                {"dpi": 200, "_selected_pages": [1]},
                tmp_path,
                engine="tesseract",
            )
        )


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("private interpreter path"),
        ValueError("invalid worker runtime"),
    ],
)
def test_renderer_launcher_failure_is_fatal(tmp_path, monkeypatch, error):
    if isinstance(error, ValueError):

        def argv(*args, **kwargs):
            raise error

        monkeypatch.setattr(pdf_render, "worker_argv", argv)
    else:

        async def launch(*args, **kwargs):
            raise error

        monkeypatch.setattr(pdf_render, "run_sandboxed", launch)
    with pytest.raises(pdf_render.PdfRenderEnvironmentError, match="could not start"):
        asyncio.run(
            pdf_render.render_pdf_pages(
                tmp_path / "source.pdf", tmp_path, dpi=200, pages=[1]
            )
        )


def test_tesseract_storage_failure_uses_pipe_when_result_file_cannot_be_written(
    tmp_path, monkeypatch, capsys
):
    def recognize(payload):
        raise OSError(errno.ENOSPC, "private storage path")

    monkeypatch.setattr(tesseract_worker, "_recognize", recognize)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    tesseract_worker.main()
    wire = capsys.readouterr().out
    assert json.loads(wire) == {"error": "environment_failed"}

    async def launch(*args, **kwargs):
        return SandboxResult(returncode=0, stdout=wire, stderr="")

    monkeypatch.setattr(ocr_engines_local, "tesseract_available", lambda: (True, None))
    monkeypatch.setattr(ocr_engines_local, "run_sandboxed", launch)
    with pytest.raises(RecipeInvocationHalt, match="OCR environment failed"):
        asyncio.run(ocr_engines_local.ocr_tesseract([tmp_path / "page.png"], tmp_path))


@pytest.mark.parametrize("failure", ["launcher", "pool"])
def test_missing_local_worker_authority_halts_immediately(
    tmp_path, monkeypatch, failure
):
    async def launch(*args, **kwargs):
        raise FileNotFoundError("private interpreter path")

    monkeypatch.setattr(ocr_engines_local, "tesseract_available", lambda: (True, None))
    monkeypatch.setattr(ocr_engines_local, "run_sandboxed", launch)
    with pytest.raises(RecipeInvocationHalt):
        asyncio.run(
            ocr_engines_local.ocr_rapidocr(None, [], tmp_path)
            if failure == "pool"
            else ocr_engines_local.ocr_tesseract([tmp_path / "page.png"], tmp_path)
        )


def test_bad_single_page_oserror_is_not_environment_failure():
    assert not pdf_render_worker.is_environment_failure(OSError("unreadable image"))
    assert not pdf_render_worker.is_environment_failure(
        FileNotFoundError(errno.ENOENT, "missing page image")
    )
