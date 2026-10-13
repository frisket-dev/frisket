from __future__ import annotations

import asyncio

from frisket.actions.media import OcrParams, _ocr_outputs, ocr_options
from frisket.contracts.actions.schemas._engines import is_generic_vlm_ocr_engine
from frisket.engine.executor.ocr_read import ocr_token_granularity
from frisket.execution.definitions import build_static_targets
from frisket.execution.resolve_for_action import authored_options
from frisket.execution.targets import CAPABILITY_OCR
from frisket.ops.base import OpContext
from frisket.ops.ocr_engines import OcrEngines


GENERIC_ENGINE = "openai/gpt-6.1-sol"


def _params(engine: str, **options) -> OcrParams:
    return OcrParams.model_validate({"source": "scan", "engine": engine, **options})


def test_generic_vlm_position_option_defaults_on_and_is_identity_material() -> None:
    params = _params(GENERIC_ENGINE)

    assert params.keep_text_position is True
    assert ocr_options(params).normalize(GENERIC_ENGINE)["keep_text_position"] is True
    assert authored_options({"keep_text_position": False}, CAPABILITY_OCR) == {
        "keep_text_position": False
    }


def test_position_option_is_generic_only_and_controls_boxes_output() -> None:
    generic_plain = _params(GENERIC_ENGINE, keep_text_position=False)
    dedicated = _params("rapidocr", keep_text_position=False)

    assert (
        ocr_options(generic_plain).normalize(GENERIC_ENGINE)["keep_text_position"]
        is False
    )
    assert "keep_text_position" not in ocr_options(dedicated).normalize("rapidocr")
    assert _ocr_outputs(generic_plain) == ("text",)
    assert _ocr_outputs(dedicated) == ("text", "blocks")


def test_qualified_dedicated_engine_keeps_fixed_geometry_behavior() -> None:
    from frisket.opendocrouter_catalog import current_catalog

    dedicated = current_catalog().models[0].engine

    assert "/" in dedicated
    assert is_generic_vlm_ocr_engine(dedicated) is False
    assert "keep_text_position" not in ocr_options(
        _params(dedicated, keep_text_position=False)
    ).normalize(dedicated)


def test_generic_vlm_dispatch_receives_actual_position_choice(
    tmp_path, monkeypatch
) -> None:
    page = tmp_path / "page.png"
    page.write_bytes(b"page")
    seen: dict[str, object] = {}

    async def fake_vlm(
        self, model, pages, ctx, usage, language=None, *, keep_text_position=True
    ):
        del self, pages, ctx, usage, language
        seen.update(model=model, keep_text_position=keep_text_position)
        return [{"text": "plain", "blocks": []}]

    monkeypatch.setattr(OcrEngines, "_ocr_vlm", fake_vlm)

    result = asyncio.run(
        OcrEngines().run_engine_on_pages(
            GENERIC_ENGINE,
            [page],
            OpContext(extras={}),
            usage={},
            scratch=tmp_path,
            keep_text_position=False,
        )
    )

    assert result == [{"text": "plain", "blocks": []}]
    assert seen == {"model": GENERIC_ENGINE, "keep_text_position": False}


def test_generic_vlm_declares_geometry_and_line_granularity() -> None:
    remote = next(
        target for target in build_static_targets() if target.id == "remote-api:openai"
    )
    support = next(
        row
        for row in remote.engines
        if row.capability == CAPABILITY_OCR and row.engine == "openai/*"
    )

    assert support.options.geometry is True
    assert ocr_token_granularity(GENERIC_ENGINE) == "line"
