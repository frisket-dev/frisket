from __future__ import annotations

import hashlib
import io
import math

import pytest
from PIL import Image

from frisket.ai.vision.region_locator import (
    ImageAsset,
    LocateRegionsRequest,
    RegionLocatorProfile,
    decode_region_response,
    prepare_image_asset,
    profile_for_engine,
    region_request_identity,
)


@pytest.mark.parametrize(
    ("engine_ref", "bbox_field", "raw_bbox"),
    [
        (
            "openrouter/z-ai/glm-5.3-flash",
            "box_2d",
            [100, 200, 500, 800],
        ),
        (
            "openrouter/moonshotai/kimi-k3",
            "bbox_normalized_xyxy",
            [0.2, 0.1, 0.8, 0.5],
        ),
        (
            "openrouter/xiaomi/mimo-v2.5",
            "bbox_normalized_yxyx",
            [0.1, 0.2, 0.5, 0.8],
        ),
        (
            "openrouter/qwen/qwen3-vl-32b-instruct",
            "bbox_2d",
            [200, 100, 800, 500],
        ),
    ],
)
def test_profiles_decode_to_one_canonical_region(
    engine_ref: str,
    bbox_field: str,
    raw_bbox: list[float],
) -> None:
    profile = profile_for_engine(engine_ref)
    assert profile is not None

    result = decode_region_response(
        profile,
        {
            "matches": [
                {
                    "match": "blue rectangle",
                    bbox_field: raw_bbox,
                    "details": {"sentiment": "neutral"},
                }
            ]
        },
        image_width=1_000,
        image_height=600,
        engine_ref=engine_ref,
    )

    assert result.issues == ()
    assert len(result.regions) == 1
    region = result.regions[0]
    assert region.match == "blue rectangle"
    assert region.details == {"sentiment": "neutral"}
    assert region.bbox == {
        "space": "page_normalized",
        "x0": 0.2,
        "y0": 0.1,
        "x1": 0.8,
        "y1": 0.5,
        "raw": {
            "space": profile.coordinate_space,
            "x0": 200.0 if profile.coordinate_space != "normalized" else 0.2,
            "y0": 100.0 if profile.coordinate_space != "normalized" else 0.1,
            "x1": 800.0 if profile.coordinate_space != "normalized" else 0.8,
            "y1": 500.0 if profile.coordinate_space != "normalized" else 0.5,
        },
    }
    assert region.raw_bbox == tuple(raw_bbox)
    assert region.provenance.engine_ref == engine_ref
    assert region.provenance.profile_id == profile.profile_id
    assert region.provenance.grounding_method == "vision_model_region"


def test_pixel_profile_uses_image_dimensions() -> None:
    profile = RegionLocatorProfile(
        profile_id="pixel-xyxy-test-v1",
        model_patterns=("openrouter/example/pixel-grounder",),
        bbox_field="bbox_pixels",
        coordinate_order="xyxy",
        coordinate_space="pixel",
        prompt_version="pixel-test-v1",
        reasoning_policy="disabled",
    )

    result = decode_region_response(
        profile,
        {
            "matches": [
                {
                    "match": "flag",
                    "bbox_pixels": [80, 70, 240, 170],
                    "details": {},
                }
            ]
        },
        image_width=1_000,
        image_height=600,
        engine_ref="openrouter/example/pixel-grounder",
    )

    assert result.issues == ()
    assert result.regions[0].bbox["x0"] == 0.08
    assert result.regions[0].bbox["y0"] == pytest.approx(70 / 600, abs=1e-6)
    assert result.regions[0].bbox["x1"] == 0.24
    assert result.regions[0].bbox["y1"] == pytest.approx(170 / 600, abs=1e-6)


@pytest.mark.parametrize(
    ("engine_ref", "bbox_field", "coordinate_order", "coordinate_space"),
    [
        (
            "anthropic/claude-sonnet-5",
            "bbox_pixels_xyxy",
            "xyxy",
            "pixel",
        ),
        ("openai/gpt-5", "bbox_pixels_xyxy", "xyxy", "pixel"),
        ("openai/gpt-5-mini", "bbox_pixels_xyxy", "xyxy", "pixel"),
        ("openai/gpt-5.6-luna", "bbox_pixels_xyxy", "xyxy", "pixel"),
        ("openai/gpt-5.6-terra", "bbox_pixels_xyxy", "xyxy", "pixel"),
        ("openai/gpt-5.6-sol", "bbox_pixels_xyxy", "xyxy", "pixel"),
        ("gemini/gemini-3.6-flash", "box_2d", "yxyx", "normalized_1000"),
    ],
)
def test_direct_provider_profiles_are_explicit(
    engine_ref: str,
    bbox_field: str,
    coordinate_order: str,
    coordinate_space: str,
) -> None:
    profile = profile_for_engine(engine_ref)
    assert profile is not None
    assert profile.bbox_field == bbox_field
    assert profile.coordinate_order == coordinate_order
    assert profile.coordinate_space == coordinate_space
    assert profile.reasoning_policy is None


def test_prepare_image_asset_bounds_and_hashes_inference_copy() -> None:
    original = io.BytesIO()
    Image.new("RGB", (2_000, 1_000), color="white").save(original, format="JPEG")

    asset = prepare_image_asset(
        original.getvalue(), source_media_type="image/jpeg", max_edge=1_024
    )

    assert asset.media_type == "image/png"
    assert (asset.width, asset.height) == (1_024, 512)
    assert (asset.display_width, asset.display_height) == (2_000, 1_000)
    assert asset.sha256 == "sha256:" + hashlib.sha256(asset.data).hexdigest()


def test_prepare_image_asset_rejects_animated_sources() -> None:
    animated = io.BytesIO()
    first = Image.new("RGB", (20, 10), color="white")
    second = Image.new("RGB", (20, 10), color="blue")
    first.save(
        animated,
        format="GIF",
        save_all=True,
        append_images=[second],
        duration=100,
        loop=0,
    )

    with pytest.raises(ValueError, match="animated images"):
        prepare_image_asset(animated.getvalue(), source_media_type="image/gif")


def test_prepare_image_asset_reuses_pillow_decoded_pixel_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = io.BytesIO()
    Image.new("RGB", (20, 10), color="white").save(encoded, format="PNG")
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 150)

    with pytest.warns(Image.DecompressionBombWarning):
        with pytest.raises(ValueError, match="decoded-pixel budget"):
            prepare_image_asset(encoded.getvalue(), source_media_type="image/png")


def test_region_request_identity_binds_prompt_schema_and_coordinate_profile() -> None:
    request = LocateRegionsRequest(
        engine_ref="gemini/gemini-3.6-flash",
        image=ImageAsset(
            data=b"png bytes",
            media_type="image/png",
            width=1_000,
            height=600,
            sha256="sha256:fixture",
        ),
        query="find every blue rectangle",
        detail_schema={"type": "object", "properties": {}},
    )

    identity = region_request_identity(request)

    assert identity["profile"] == {
        "profile_id": "gemini-3.6-flash-yxyx-1000-v1",
        "bbox_field": "box_2d",
        "coordinate_order": "yxyx",
        "coordinate_space": "normalized_1000",
        "prompt_version": "region-locate-gemini36-v1",
        "reasoning_policy": None,
        "supports_detail_schema": True,
    }
    assert "box_2d" in identity["prompt"]
    assert identity["response_schema"]["properties"]["matches"]["items"]["properties"][
        "box_2d"
    ]


@pytest.mark.parametrize(
    "raw_bbox",
    [
        [0.1, 0.2, 0.3],
        [0.1, 0.2, 0.3, 0.4, 0.5],
        [0.1, 0.2, 0.1, 0.4],
        [0.1, 0.2, 1.1, 0.4],
        [0.1, math.nan, 0.3, 0.4],
        [0.1, math.inf, 0.3, 0.4],
        ["0.1", 0.2, 0.3, 0.4],
    ],
)
def test_invalid_boxes_are_rejected_without_repair(raw_bbox: list[object]) -> None:
    engine_ref = "openrouter/moonshotai/kimi-k3"
    profile = profile_for_engine(engine_ref)
    assert profile is not None

    result = decode_region_response(
        profile,
        {
            "matches": [
                {
                    "match": "cat",
                    "bbox_normalized_xyxy": raw_bbox,
                    "details": {},
                }
            ]
        },
        image_width=640,
        image_height=480,
        engine_ref=engine_ref,
    )

    assert result.regions == ()
    assert [issue.code for issue in result.issues] == ["invalid_bbox"]


def test_empty_matches_is_successful_and_exact_boxes_are_deduplicated() -> None:
    engine_ref = "openrouter/qwen/qwen3-vl-8b-instruct"
    profile = profile_for_engine(engine_ref)
    assert profile is not None

    empty = decode_region_response(
        profile,
        {"matches": []},
        image_width=1_000,
        image_height=600,
        engine_ref=engine_ref,
    )
    assert empty.regions == ()
    assert empty.issues == ()

    item = {
        "match": "cat",
        "bbox_2d": [100, 200, 300, 400],
        "details": {"color": "black"},
    }
    duplicate = decode_region_response(
        profile,
        {"matches": [item, dict(item)]},
        image_width=1_000,
        image_height=600,
        engine_ref=engine_ref,
    )
    assert len(duplicate.regions) == 1
    assert duplicate.issues == ()


def test_regions_are_sorted_in_canonical_page_order() -> None:
    engine_ref = "openrouter/qwen/qwen3-vl-8b-instruct"
    profile = profile_for_engine(engine_ref)
    assert profile is not None

    result = decode_region_response(
        profile,
        {
            "matches": [
                {
                    "match": "bottom",
                    "bbox_2d": [100, 700, 300, 900],
                    "details": {},
                },
                {
                    "match": "top",
                    "bbox_2d": [200, 100, 400, 300],
                    "details": {},
                },
            ]
        },
        image_width=1_000,
        image_height=600,
        engine_ref=engine_ref,
    )

    assert [region.match for region in result.regions] == ["top", "bottom"]


def test_profile_selection_fails_closed_and_declares_reasoning_policy() -> None:
    assert profile_for_engine("openrouter/minimax/minimax-m3") is None
    assert profile_for_engine("openrouter/unknown/model") is None
    assert profile_for_engine("openrouter/qwen/qwen3-vl-unqualified-instruct") is None

    glm = profile_for_engine("openrouter/z-ai/glm-5.3-flash")
    kimi = profile_for_engine("openrouter/moonshotai/kimi-k3")
    assert glm is not None and kimi is not None
    assert glm.reasoning_policy == "low_exclude"
    assert kimi.reasoning_policy == "disabled"


def test_request_contract_is_action_neutral() -> None:
    request = LocateRegionsRequest(
        engine_ref="openrouter/qwen/qwen3-vl-32b-instruct",
        image=ImageAsset(
            data=b"png bytes",
            media_type="image/png",
            width=1_000,
            height=600,
            sha256="sha256:fixture",
        ),
        query="find every blue rectangle",
        detail_schema={
            "type": "object",
            "properties": {"position": {"type": "string"}},
        },
    )

    assert request.query == "find every blue rectangle"
    assert request.image.width == 1_000
    assert request.detail_schema is not None
