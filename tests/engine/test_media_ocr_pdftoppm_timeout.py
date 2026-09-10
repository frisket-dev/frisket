"""OCR's one actual rasterization is bounded and cooperatively cancellable."""

import asyncio

import pytest

from frisket.ops import ocr_engines
from frisket.engine.sandbox.shim import SandboxResult


@pytest.mark.parametrize("cancelled", [False, True])
def test_pdf_rasterization_does_not_continue_after_timeout_or_cancel(
    tmp_path, monkeypatch, cancelled
):
    source = tmp_path / "malformed.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    calls = []

    async def bounded_run(argv, *, policy, should_cancel):
        assert policy.wall_seconds == 600
        assert policy.cpu_seconds == 600
        assert policy.allow_network is False
        assert should_cancel() is cancelled
        calls.append(argv)
        return SandboxResult(
            returncode=-9,
            timed_out=not cancelled,
            cancelled=cancelled,
            stdout="",
            stderr="render stopped",
        )

    monkeypatch.setattr(ocr_engines.shutil, "which", lambda name: "/usr/bin/pdftoppm")
    monkeypatch.setattr(ocr_engines, "run_sandboxed", bounded_run)
    engine = ocr_engines.OcrEngines(cancelled=lambda: cancelled)
    expected = ocr_engines.OcrCancelled if cancelled else RuntimeError
    with pytest.raises(expected):
        asyncio.run(
            engine._page_images(
                source, {"mime": "application/pdf"}, {"dpi": 150}, tmp_path
            )
        )
    assert len(calls) == 1
