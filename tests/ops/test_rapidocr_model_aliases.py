"""RapidOCR default-model preparation stays offline after publication."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from frisket.engine._workers import rapidocr_models
from frisket.engine._workers.rapidocr_models import rapidocr_bundled_model_aliases
from frisket.ops import ocr_engines_local as ocr_local


_V5_REQUESTED = (
    "ch_PP-OCRv4_det_mobile.onnx",
    "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    "ch_PP-OCRv5_rec_mobile.onnx",
)


def test_default_requirements_use_pinned_v5_but_explicit_language_stays_v4() -> None:
    pytest.importorskip("rapidocr")
    rapidocr_models.rapidocr_model_requirements.cache_clear()
    try:
        default = rapidocr_models.rapidocr_model_requirements()
        explicit = rapidocr_models.rapidocr_model_requirements("ch")
    finally:
        rapidocr_models.rapidocr_model_requirements.cache_clear()

    assert default is not None
    assert explicit is not None
    assert default[1] == _V5_REQUESTED
    assert explicit[1][:2] == default[1][:2]
    assert explicit[1][2] == "ch_PP-OCRv4_rec_mobile.onnx"


def test_bundled_alias_plan_seeds_only_v4_detector_and_classifier(tmp_path: Path) -> None:
    aliases = rapidocr_bundled_model_aliases(tmp_path, _V5_REQUESTED)

    assert aliases == (
        (
            tmp_path / "ch_PP-OCRv4_det_infer.onnx",
            _V5_REQUESTED[0],
            "d2a7720d45a54257208b1e13e36a8479894cb74155a5efe29462512d42f49da9",
        ),
        (
            tmp_path / "ch_ppocr_mobile_v2.0_cls_infer.onnx",
            _V5_REQUESTED[1],
            "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
        ),
    )


def test_default_recognizer_download_is_checked_before_atomic_publication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = b"verified recognizer"
    digest = sha256(payload).hexdigest()
    filename = "default-rec.onnx"
    for module in (ocr_local, rapidocr_models):
        monkeypatch.setattr(module, "RAPIDOCR_DEFAULT_RECOGNIZER_FILENAME", filename)
        monkeypatch.setattr(module, "RAPIDOCR_DEFAULT_RECOGNIZER_SHA256", digest)
    monkeypatch.setattr(
        ocr_local, "RAPIDOCR_DEFAULT_RECOGNIZER_URL", "https://example.invalid/rec"
    )
    for base in _V5_REQUESTED[:2]:
        (tmp_path / base).write_bytes(b"bundled")

    from rapidocr.utils.download_file import DownloadFile

    def fake_download(_cls, input_params):
        Path(input_params.save_path).write_bytes(payload)

    monkeypatch.setattr(DownloadFile, "run", classmethod(fake_download))
    ocr_local._provision_default_rapidocr_recognizer(
        tmp_path, (*_V5_REQUESTED[:2], filename)
    )

    assert (tmp_path / filename).read_bytes() == payload
    assert not list(tmp_path.glob(f".{filename}.*"))


def test_partial_recognizer_download_is_never_published(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    filename = "default-rec.onnx"
    for module in (ocr_local, rapidocr_models):
        monkeypatch.setattr(module, "RAPIDOCR_DEFAULT_RECOGNIZER_FILENAME", filename)
        monkeypatch.setattr(
            module, "RAPIDOCR_DEFAULT_RECOGNIZER_SHA256", sha256(b"whole").hexdigest()
        )
    monkeypatch.setattr(
        ocr_local, "RAPIDOCR_DEFAULT_RECOGNIZER_URL", "https://example.invalid/rec"
    )
    for base in _V5_REQUESTED[:2]:
        (tmp_path / base).write_bytes(b"bundled")

    from rapidocr.utils.download_file import DownloadFile

    def fake_download(_cls, input_params):
        Path(input_params.save_path).write_bytes(b"partial")

    monkeypatch.setattr(DownloadFile, "run", classmethod(fake_download))
    ocr_local._provision_default_rapidocr_recognizer(
        tmp_path, (*_V5_REQUESTED[:2], filename)
    )

    assert not (tmp_path / filename).exists()
    assert not list(tmp_path.glob(f".{filename}.*"))


def test_failed_recognizer_download_leaves_no_ready_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    filename = "default-rec.onnx"
    for module in (ocr_local, rapidocr_models):
        monkeypatch.setattr(module, "RAPIDOCR_DEFAULT_RECOGNIZER_FILENAME", filename)
        monkeypatch.setattr(
            module, "RAPIDOCR_DEFAULT_RECOGNIZER_SHA256", sha256(b"whole").hexdigest()
        )
    monkeypatch.setattr(
        ocr_local, "RAPIDOCR_DEFAULT_RECOGNIZER_URL", "https://example.invalid/rec"
    )
    for base in _V5_REQUESTED[:2]:
        (tmp_path / base).write_bytes(b"bundled")

    from rapidocr.utils.download_file import DownloadFile, DownloadFileException

    def failed_download(_cls, _input_params):
        raise DownloadFileException("offline")

    monkeypatch.setattr(DownloadFile, "run", classmethod(failed_download))
    ocr_local._provision_default_rapidocr_recognizer(
        tmp_path, (*_V5_REQUESTED[:2], filename)
    )

    assert not (tmp_path / filename).exists()
    assert not list(tmp_path.glob(f".{filename}.*"))
