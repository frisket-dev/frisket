from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from frisket_models import engines


def _clear_worker_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(engines.PP_OCRV6_WORKER_URL_ENV, raising=False)
    monkeypatch.delenv(engines.PP_OCRV6_WORKER_TOKEN_ENV, raising=False)


def test_pp_ocrv6_uses_official_medium_defaults_and_normalizes_geometry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_worker_config(monkeypatch)
    monkeypatch.delenv("FRISKET_PADDLE_DEVICE", raising=False)
    calls: dict[str, object] = {}

    class PaddleOCR:
        def __init__(self, **options: object) -> None:
            calls["options"] = options

        def predict(self, image: object) -> list[dict]:
            calls["image"] = image
            return [
                {
                    "rec_texts": [" First line ", "", "Second line"],
                    "rec_polys": [
                        [[1.9, 2.1], [20, 2], [20, 8], [1, 8]],
                        [[0, 0], [0, 0], [0, 0], [0, 0]],
                        [[3, 12], [30, 12], [30, 19], [3, 19]],
                    ],
                    "rec_scores": [0.98765, 0.5, 0.81234],
                }
            ]

    class Image:
        @staticmethod
        def open(stream: object) -> object:
            calls["bytes"] = stream.read()
            return SimpleNamespace(convert=lambda mode: ("converted", mode))

    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(PaddleOCR=PaddleOCR))
    monkeypatch.setitem(sys.modules, "PIL", SimpleNamespace(Image=Image))
    monkeypatch.setitem(
        sys.modules,
        "numpy",
        SimpleNamespace(array=lambda value: ("array", value)),
    )

    pages = engines.load_pp_ocrv6()([b"png"])

    assert calls["options"] == {
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
        "enable_mkldnn": False,
    }
    assert calls["bytes"] == b"png"
    assert pages == [
        {
            "text": "First line\nSecond line",
            "blocks": [
                {
                    "text": "First line",
                    "bbox": [[1, 2], [20, 2], [20, 8], [1, 8]],
                    "score": 0.9877,
                },
                {
                    "text": "Second line",
                    "bbox": [[3, 12], [30, 12], [30, 19], [3, 19]],
                    "score": 0.8123,
                },
            ],
        }
    ]


def test_pp_ocrv6_uses_explicit_cuda_device(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_worker_config(monkeypatch)
    monkeypatch.setenv("FRISKET_PADDLE_DEVICE", "gpu:0")
    constructors: list[dict[str, object]] = []

    class PaddleOCR:
        def __init__(self, **options: object) -> None:
            constructors.append(options)

    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(PaddleOCR=PaddleOCR))
    monkeypatch.setitem(
        sys.modules,
        "paddle",
        SimpleNamespace(is_compiled_with_cuda=lambda: True),
    )

    engines.load_pp_ocrv6()

    assert constructors == [
        {
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
            "enable_mkldnn": False,
            "device": "gpu:0",
        }
    ]


def test_pp_ocrv6_proxy_forwards_engine_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"pages": [{"text": "quick", "blocks": []}]}

    def post(url: str, **kwargs: object) -> Response:
        request.update(url=url, **kwargs)
        return Response()

    monkeypatch.setenv(
        engines.PP_OCRV6_WORKER_URL_ENV,
        "https://pp-ocrv6.example/",
    )
    monkeypatch.setenv(engines.PP_OCRV6_WORKER_TOKEN_ENV, "edge-token")
    monkeypatch.setattr("httpx.post", post)

    pages = engines.load_pp_ocrv6()([b"png"])

    assert pages == [{"text": "quick", "blocks": []}]
    assert request["url"] == "https://pp-ocrv6.example/ocr"
    assert request["data"] == {"engine": "pp-ocrv6"}
    assert request["headers"] == {"Authorization": "Bearer edge-token"}
