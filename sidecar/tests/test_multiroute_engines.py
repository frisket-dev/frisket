"""A document model serves both HTTP routes from one resident load."""

from concurrent.futures import ThreadPoolExecutor
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from frisket_models.app import create_app
from frisket_models.engines import Engine, Registry, ResidentState
from stub_helpers import AUTH, TOKEN


def views(loader):
    state = ResidentState()
    return [
        Engine("document-model", route, [], loader, state=state, adapter_method=method)
        for route, method in (("/ocr", "ocr"), ("/to-markdown", "to_markdown"))
    ]


def test_http_routes_share_one_model_and_report_both_capabilities():
    loads = []

    def load():
        loads.append(True)
        return SimpleNamespace(
            ocr=lambda images: [{"text": "Hello", "blocks": []} for _ in images],
            to_markdown=lambda name, data: {"markdown": "# Hello", "ocr_used": [True]},
        )

    registry = Registry(views(load))
    with TestClient(create_app(token=TOKEN, registry=registry)) as client:
        for route, output in (("/ocr", "pages"), ("/to-markdown", "documents")):
            result = client.post(
                route,
                headers=AUTH,
                data={"engine": "document-model"},
                files={"files": ("source.png", b"test-bytes", "image/png")},
            )
            assert result.status_code == 200, result.text
            assert len(result.json()[output]) == 1
        entries = client.get("/capabilities", headers=AUTH).json()["engines"]
        assert {e["route"] for e in entries} == {"/ocr", "/to-markdown"}
        assert all(e["loaded"] and e["available"] for e in entries)
        assert loads == [True]
        assert (
            client.post(
                "/ocr",
                headers=AUTH,
                data={"engine": "unknown"},
                files={"files": ("page.png", b"x", "image/png")},
            ).status_code
            == 400
        )


def test_concurrent_route_loads_share_state():
    loads = []

    def load():
        loads.append(True)
        return SimpleNamespace(ocr=lambda: "ocr", to_markdown=lambda: "markdown")

    engines = views(load)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda e: e.get()(), engines * 10))
    assert results == ["ocr", "markdown"] * 10
    assert len(loads) == 1


def test_load_error_is_shared_between_routes():
    loads = []

    def fail():
        loads.append(True)
        raise RuntimeError("not enough memory")

    engines = views(fail)
    for engine in engines:
        with pytest.raises(RuntimeError, match="not enough memory"):
            engine.get()
        assert engine.describe()["available"] is False
    assert len(loads) == 1


def test_registry_requires_route_for_ambiguous_name():
    engines = views(lambda: None)
    registry = Registry(engines)
    assert registry.get("document-model", route="/ocr") is engines[0]
    with pytest.raises(KeyError):
        registry.get("document-model")
    with pytest.raises(KeyError):
        registry.get("document-model", route="/ner")


def test_lighton_registry_has_both_sizes_and_shared_route_state(monkeypatch):
    from frisket_models import engines as engine_module
    from frisket_models.engines import default_registry, EXTRAS

    registry = default_registry()
    for size in ("0.8b", "4b"):
        name = f"lightonocr-3-{size}"
        ocr = registry.get(name, route="/ocr")
        markdown = registry.get(name, route="/to-markdown")
        assert ocr.state is markdown.state
        assert ocr.models == markdown.models
        assert ocr.revision == markdown.revision
        assert "torchvision" in ocr.modules
        assert EXTRAS[name] == "ocr-lighton"
        assert ocr.loaded is False
    with pytest.raises(KeyError):
        registry.get("lightonocr-3-1b", route="/ocr")

    monkeypatch.setattr(
        engine_module.importlib.util,
        "find_spec",
        lambda module: None if module == "torchvision" else SimpleNamespace(),
    )
    assert registry.get("lightonocr-3-0.8b", route="/ocr").installed is False


def test_registry_build_does_not_import_lighton_markdown_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from frisket_models.engines import default_registry

    monkeypatch.delitem(sys.modules, "frisket_models.lightonocr", raising=False)
    monkeypatch.delitem(sys.modules, "frisket_models.markdown_plain", raising=False)
    monkeypatch.setitem(sys.modules, "markdown_it", None)

    registry = default_registry()

    assert registry.get("lightonocr-3-0.8b", route="/ocr").loaded is False
