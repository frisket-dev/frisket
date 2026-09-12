"""RapidOCR 3.8.1 bundled-model compatibility tests."""

from __future__ import annotations

from hashlib import sha256

import pytest

from frisket.engine._workers.rapidocr_models import rapidocr_bundled_model_aliases
from frisket.ops import ocr_engines_local as ocr_local
from frisket.ops.base import RecipeInvocationHalt


_REQUESTED = (
    "ch_PP-OCRv4_det_mobile.onnx",
    "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    "ch_PP-OCRv4_rec_mobile.onnx",
)


def test_rapidocr_381_alias_plan_has_only_the_verified_bundled_trio(tmp_path):
    aliases = rapidocr_bundled_model_aliases(tmp_path, _REQUESTED)

    assert aliases == (
        (
            tmp_path / "ch_PP-OCRv4_det_infer.onnx",
            _REQUESTED[0],
            "d2a7720d45a54257208b1e13e36a8479894cb74155a5efe29462512d42f49da9",
        ),
        (
            tmp_path / "ch_ppocr_mobile_v2.0_cls_infer.onnx",
            _REQUESTED[1],
            "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
        ),
        (
            tmp_path / "ch_PP-OCRv4_rec_infer.onnx",
            _REQUESTED[2],
            "48fc40f24f6d2a207a2b1091d3437eb3cc3eb6b676dc3ef9c37384005483683b",
        ),
    )
    assert rapidocr_bundled_model_aliases(tmp_path, ("unknown.onnx",)) is None


def test_model_root_seeds_verified_bundled_aliases_into_shared_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    package = tmp_path / "readonly-package"
    cache = tmp_path / "cache"
    package.mkdir()
    source_files = []
    aliases = []
    for number, target in enumerate(_REQUESTED):
        source = package / f"bundled-{number}.onnx"
        content = f"model-{number}".encode()
        source.write_bytes(content)
        source_files.append((source, content))
        aliases.append((source, target, sha256(content).hexdigest()))
    cache.mkdir()
    (cache / _REQUESTED[0]).write_bytes(b"stale cache bytes")

    monkeypatch.setattr(ocr_local, "rapidocr_available", lambda: (True, None))
    monkeypatch.setattr(
        ocr_local, "rapidocr_model_requirements", lambda language: (package, _REQUESTED)
    )
    monkeypatch.setattr(ocr_local, "rapidocr_shared_model_cache_dir", lambda: cache)
    monkeypatch.setattr(
        ocr_local, "rapidocr_bundled_model_aliases", lambda *_: tuple(aliases)
    )

    assert ocr_local._rapidocr_model_root_dir() == str(cache)
    assert [(cache / target).read_bytes() for target in _REQUESTED] == [
        content for _source, content in source_files
    ]
    assert [source.read_bytes() for source, _content in source_files] == [
        content for _source, content in source_files
    ]


def test_model_root_refuses_unverified_bundled_aliases(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    package = tmp_path / "package"
    cache = tmp_path / "cache"
    package.mkdir()
    source = package / "bundled.onnx"
    source.write_bytes(b"different")
    monkeypatch.setattr(ocr_local, "rapidocr_available", lambda: (True, None))
    monkeypatch.setattr(
        ocr_local, "rapidocr_model_requirements", lambda language: (package, _REQUESTED)
    )
    monkeypatch.setattr(ocr_local, "rapidocr_shared_model_cache_dir", lambda: cache)
    monkeypatch.setattr(
        ocr_local,
        "rapidocr_bundled_model_aliases",
        lambda *_: ((source, _REQUESTED[0], sha256(b"expected").hexdigest()),),
    )

    with pytest.raises(RecipeInvocationHalt, match="offline model"):
        ocr_local._rapidocr_model_root_dir()
    assert not cache.exists()
