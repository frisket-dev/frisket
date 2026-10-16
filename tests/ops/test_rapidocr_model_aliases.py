"""RapidOCR default-model preparation stays offline after publication."""

from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path

import pytest

from frisket.engine._workers import rapidocr_models
from frisket.engine._workers.rapidocr_models import (
    rapidocr_bundled_model_aliases,
    rapidocr_model_root_complete,
)
from frisket.ops import ocr_engines_local as ocr_local


_V5_REQUESTED = (
    "ch_PP-OCRv4_det_mobile.onnx",
    "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    "ch_PP-OCRv5_rec_mobile.onnx",
)


def _write_complete_model_root(root: Path, recognizer: bytes) -> None:
    root.mkdir(exist_ok=True)
    for filename in _V5_REQUESTED[:2]:
        (root / filename).write_bytes(b"bundled")
    (root / _V5_REQUESTED[-1]).write_bytes(recognizer)


def test_model_root_reuses_verified_recognizer_hash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recognizer = b"recognizer"
    _write_complete_model_root(tmp_path, recognizer)
    monkeypatch.setattr(
        rapidocr_models,
        "RAPIDOCR_DEFAULT_RECOGNIZER_SHA256",
        sha256(recognizer).hexdigest(),
    )
    original = rapidocr_models._sha256_file
    reads: list[Path] = []

    def counted(path: Path) -> str:
        reads.append(path)
        return original(path)

    monkeypatch.setattr(rapidocr_models, "_sha256_file", counted)

    assert rapidocr_model_root_complete(tmp_path, _V5_REQUESTED) is True
    assert rapidocr_model_root_complete(tmp_path, _V5_REQUESTED) is True
    assert reads == [tmp_path / _V5_REQUESTED[-1]]


def test_model_root_rehashes_changed_recognizer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recognizer = b"recognizer"
    _write_complete_model_root(tmp_path, recognizer)
    monkeypatch.setattr(
        rapidocr_models,
        "RAPIDOCR_DEFAULT_RECOGNIZER_SHA256",
        sha256(recognizer).hexdigest(),
    )
    original = rapidocr_models._sha256_file
    reads = 0

    def counted(path: Path) -> str:
        nonlocal reads
        reads += 1
        return original(path)

    monkeypatch.setattr(rapidocr_models, "_sha256_file", counted)
    assert rapidocr_model_root_complete(tmp_path, _V5_REQUESTED) is True

    target = tmp_path / _V5_REQUESTED[-1]
    before = target.stat()
    target.write_bytes(
        b"RecognizeR"
    )  # same size; metadata, not size alone, invalidates
    os.utime(
        target,
        ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
    )
    assert target.stat().st_mtime_ns != before.st_mtime_ns

    assert rapidocr_model_root_complete(tmp_path, _V5_REQUESTED) is False
    assert reads == 2


def test_model_root_observes_missing_then_installed_recognizer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recognizer = b"recognizer"
    tmp_path.mkdir(exist_ok=True)
    for filename in _V5_REQUESTED[:2]:
        (tmp_path / filename).write_bytes(b"bundled")
    monkeypatch.setattr(
        rapidocr_models,
        "RAPIDOCR_DEFAULT_RECOGNIZER_SHA256",
        sha256(recognizer).hexdigest(),
    )

    assert rapidocr_model_root_complete(tmp_path, _V5_REQUESTED) is False
    (tmp_path / _V5_REQUESTED[-1]).write_bytes(recognizer)
    assert rapidocr_model_root_complete(tmp_path, _V5_REQUESTED) is True


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


def test_bundled_alias_plan_seeds_only_v4_detector_and_classifier(
    tmp_path: Path,
) -> None:
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


def test_runtime_resolution_never_downloads_missing_default_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_root = tmp_path / "package"
    shared_root = tmp_path / "shared"
    package_root.mkdir()
    monkeypatch.setattr(ocr_local, "rapidocr_available", lambda: (True, None))
    monkeypatch.setattr(
        ocr_local,
        "rapidocr_model_requirements",
        lambda _language=None: (package_root, _V5_REQUESTED),
    )
    monkeypatch.setattr(
        ocr_local, "rapidocr_shared_model_cache_dir", lambda: shared_root
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("inference must not provision model weights")

    monkeypatch.setattr(ocr_local, "_provision_default_rapidocr_recognizer", forbidden)

    with pytest.raises(ocr_local.RecipeInvocationHalt) as caught:
        ocr_local._rapidocr_model_root_dir()

    assert caught.value.code == "local_artifact_unavailable"


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
