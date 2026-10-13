from __future__ import annotations

import base64
import io
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from frisket.ai.llm.structured import StructuredCompleter
from frisket.ops.base import OpContext
from frisket.ops.ocr_engines_hosted import ocr_coordinate_profile, ocr_vlm


def _page(tmp_path, *, size: tuple[int, int] = (200, 100)):
    path = tmp_path / "page.png"
    Image.new("RGB", size, color="white").save(path, format="PNG")
    return path


def _usage() -> dict[str, Any]:
    return {"calls": 0, "in": 0, "out": 0, "cost": 0.0, "duration_ms": 0}


def _stub_completion(
    monkeypatch: pytest.MonkeyPatch,
    data: dict[str, Any],
) -> tuple[list[Any], Any]:
    requests: list[Any] = []
    wire_call = SimpleNamespace(
        tokens_in=12,
        tokens_out=8,
        cost=0.002,
        duration_ms=9,
        cached=False,
    )

    async def complete(self, request, *, recipe_version="1", trace=None):
        del self, recipe_version, trace
        requests.append(request)
        return SimpleNamespace(data=data, response=wire_call, wire_calls=[wire_call])

    monkeypatch.setattr(StructuredCompleter, "complete", complete)
    return requests, wire_call


@pytest.mark.parametrize(
    ("model", "profile_id"),
    [
        (
            "openrouter/anthropic/claude-sonnet-5",
            "claude-sonnet-5-xyxy-pixel-v1",
        ),
        ("openrouter/openai/gpt-6-luna", "openai-gpt6-ocr-xyxy-pixel-v1"),
        ("openrouter/openai/gpt-6.1-sol", "openai-gpt6-ocr-xyxy-pixel-v1"),
    ],
)
def test_positioned_ocr_selects_ocr_only_native_profiles(
    model: str, profile_id: str
) -> None:
    profile = ocr_coordinate_profile(model)

    assert profile.profile_id == profile_id
    assert profile.bbox_field == "bbox_pixels_xyxy"


@pytest.mark.asyncio
async def test_positioned_ocr_uses_generic_contract_and_preserves_line_order(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests, _ = _stub_completion(
        monkeypatch,
        {
            "lines": [
                {"text": "right column", "bbox_xyxy_1000": [500, 400, 900, 600]},
                {"text": "left column", "bbox_xyxy_1000": [100, 100, 400, 300]},
            ]
        },
    )
    usage = _usage()

    result = await ocr_vlm(
        "mock/vision",
        [_page(tmp_path)],
        OpContext(http=None, extras={"router": object()}),
        usage,
        recipe_version="test-v1",
    )

    assert result == [
        {
            "text": "right column\nleft column",
            "blocks": [
                {
                    "text": "right column",
                    "bbox": [
                        [100.0, 40.0],
                        [180.0, 40.0],
                        [180.0, 60.0],
                        [100.0, 60.0],
                    ],
                },
                {
                    "text": "left column",
                    "bbox": [
                        [20.0, 10.0],
                        [80.0, 10.0],
                        [80.0, 30.0],
                        [20.0, 30.0],
                    ],
                },
            ],
        }
    ]
    request = requests[0]
    assert request.model == "mock/vision"
    assert (
        "bbox_xyxy_1000" in request.schema["properties"]["lines"]["items"]["properties"]
    )
    assert "0 to 1000" in request.messages[-1]["content"][-1]["text"]
    assert usage["calls"] == usage["input_pages"] == 1


@pytest.mark.asyncio
async def test_positioned_ocr_uses_known_yxyx_profile(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests, _ = _stub_completion(
        monkeypatch,
        {"lines": [{"text": "Gemini line", "box_2d": [100, 200, 500, 800]}]},
    )

    result = await ocr_vlm(
        "gemini/gemini-3.6-flash",
        [_page(tmp_path, size=(1_000, 600))],
        OpContext(http=None, extras={"router": object()}),
        _usage(),
        recipe_version="test-v1",
    )

    assert result[0]["blocks"][0]["bbox"] == [
        [200.0, 60.0],
        [800.0, 60.0],
        [800.0, 300.0],
        [200.0, 300.0],
    ]
    assert "box_2d" in requests[0].schema["properties"]["lines"]["items"]["properties"]


@pytest.mark.asyncio
async def test_text_only_ocr_retains_existing_request_and_result_shape(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests, _ = _stub_completion(monkeypatch, {"text": "plain transcription"})

    result = await ocr_vlm(
        "mock/vision",
        [_page(tmp_path)],
        OpContext(http=None, extras={"router": object()}),
        _usage(),
        language="Welsh",
        recipe_version="test-v1",
        keep_text_position=False,
    )

    assert result == [{"text": "plain transcription", "blocks": []}]
    assert requests[0].schema["required"] == ["text"]
    assert "lines" not in requests[0].schema["properties"]
    assert "The text is in Welsh." in requests[0].messages[-1]["content"][-1]["text"]


@pytest.mark.asyncio
async def test_invalid_positioned_box_fails_after_preserving_wire_accounting(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, wire_call = _stub_completion(
        monkeypatch,
        {"lines": [{"text": "bad box", "bbox_xyxy_1000": [500, 100, 400, 300]}]},
    )
    usage = _usage()

    with pytest.raises(
        ValueError, match="malformed, out of range, or degenerate"
    ) as exc:
        await ocr_vlm(
            "mock/vision",
            [_page(tmp_path)],
            OpContext(http=None, extras={"router": object()}),
            usage,
            recipe_version="test-v1",
        )

    assert usage["calls"] == usage["input_pages"] == 1
    assert usage["cost"] == 0.002
    assert exc.value.wire_calls == [wire_call]


@pytest.mark.asyncio
async def test_minimax_positioned_resize_maps_back_to_original_page(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests, _ = _stub_completion(
        monkeypatch,
        {"lines": [{"text": "wide page", "bbox_xyxy_1000": [100, 250, 900, 750]}]},
    )

    result = await ocr_vlm(
        "openrouter/minimax/minimax-m3",
        [_page(tmp_path, size=(4_000, 400))],
        OpContext(http=None, extras={"router": object()}),
        _usage(),
        recipe_version="test-v1",
    )

    assert result[0]["blocks"][0]["bbox"] == [
        [400.0, 100.0],
        [3600.0, 100.0],
        [3600.0, 300.0],
        [400.0, 300.0],
    ]
    request = requests[0]
    assert request.model == "openrouter/minimax/minimax-m3"
    assert request.temperature == 1.0
    assert request.params == {"top_p": 0.95}
    assert request.reasoning_policy == "disabled"
    assert [part["type"] for part in request.messages[-1]["content"]] == [
        "text",
        "image",
    ]
    image_part = request.messages[-1]["content"][1]
    with Image.open(io.BytesIO(base64.b64decode(image_part["data"]))) as image:
        assert max(image.size) == 3_584
