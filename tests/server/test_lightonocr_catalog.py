"""LightOnOCR choices follow the sidecar's route-specific availability."""

import pytest

from frisket.contracts.actions.schemas._engines import ocr_engine_has_geometry
from frisket.server.action_catalog_hints import project_action_catalog_launcher_hints
from frisket.execution.definitions import build_static_targets


@pytest.mark.parametrize("available", [True, False])
def test_both_sizes_surface_for_ocr_compare_and_markdown(available):
    names = ("lightonocr-3-0.8b", "lightonocr-3-4b")
    capabilities = {
        "configured": True,
        "available": True,
        "engines": [
            {
                "name": name,
                "route": route,
                "available": available,
                "models": [name],
                "error": None if available else "Install ocr-lighton",
            }
            for name in names
            for route in ("/ocr", "/to-markdown")
        ],
    }
    hints = project_action_catalog_launcher_hints(capabilities)
    for action in ("media.ocr", "media.to_markdown"):
        choices = {e["id"]: e for e in hints[action]["engines"]}
        for name in names:
            assert choices[name]["available"] is available
            assert choices[name]["billable"] is False
            assert choices[name]["tier"] == "sidecar"
            assert choices[name]["models"] == [name]
            assert ocr_engine_has_geometry(name)
        assert "lightonocr-3-1b" not in choices
    gateway = next(t for t in build_static_targets() if t.id == "models-gateway")
    for name in names:
        assert {
            (e.capability, e.transport) for e in gateway.engines if e.engine == name
        } == {("ocr", "sidecar.ocr"), ("document.convert", "sidecar.convert")}


def test_missing_gateway_keeps_lighton_unavailable():
    hints = project_action_catalog_launcher_hints({"configured": False, "engines": []})
    for action in ("media.ocr", "media.to_markdown"):
        for entry in hints[action]["engines"]:
            if entry["id"].startswith("lightonocr-"):
                assert entry["available"] is False
                assert entry["error"]


@pytest.mark.parametrize("engine", ["lightonocr-3-0.8b", "lightonocr-3-4b"])
def test_region_highlights_do_not_enable_searchable_pdf(engine):
    from frisket.actions.media_options import OcrOptions
    from frisket.contracts.actions.schemas._engines import (
        ocr_engine_supports_searchable_pdf,
    )

    assert ocr_engine_has_geometry(engine)
    assert not ocr_engine_supports_searchable_pdf(engine)
    assert OcrOptions().normalize(engine)["searchable_pdf"] is False
    with pytest.raises(ValueError, match="searchable_pdf_unsupported_engine"):
        OcrOptions(searchable_pdf=True).normalize(engine)
    target = next(t for t in build_static_targets() if t.id == "models-gateway")
    support = next(
        e for e in target.engines if e.engine == engine and e.capability == "ocr"
    )
    assert support.options.geometry is True
    assert support.options.searchable_pdf is False


def test_existing_ocr_pdf_composition_remains_available():
    from frisket.actions.media_options import OcrOptions
    from frisket.contracts.actions.schemas._engines import (
        ocr_engine_supports_searchable_pdf,
    )

    assert ocr_engine_supports_searchable_pdf("rapidocr")
    assert (
        OcrOptions(searchable_pdf=True).normalize("rapidocr")["searchable_pdf"] is True
    )
