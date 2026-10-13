"""Declared coordinate dialects and strict image-box conversion.

Vision models use several mutually ambiguous coordinate formats.  Callers must
select a profile before making a request; this module never guesses a format
from the values returned by a model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

from frisket.ai.llm.types import ReasoningPolicy


CoordinateOrder = Literal["xyxy", "yxyx"]
CoordinateSpace = Literal["pixel", "normalized", "normalized_1000"]
ImageDetail = Literal["auto", "low", "high", "original"]


@dataclass(frozen=True)
class CoordinateProfile:
    """One versioned model wire contract for axis-aligned image boxes."""

    profile_id: str
    model_patterns: tuple[str, ...]
    bbox_field: str
    coordinate_order: CoordinateOrder
    coordinate_space: CoordinateSpace
    prompt_version: str
    reasoning_policy: ReasoningPolicy | None
    image_max_edge: int | None = None
    image_patch_size: int | None = None
    image_max_patches: int | None = None
    image_detail: ImageDetail | None = None

    def __post_init__(self) -> None:
        if self.image_max_edge is not None and self.image_max_edge < 1:
            raise ValueError("image_max_edge must be positive")
        if (self.image_patch_size is None) != (self.image_max_patches is None):
            raise ValueError(
                "image_patch_size and image_max_patches must be declared together"
            )
        if self.image_patch_size is not None and self.image_patch_size < 1:
            raise ValueError("image_patch_size must be positive")
        if self.image_max_patches is not None and self.image_max_patches < 1:
            raise ValueError("image_max_patches must be positive")


GENERIC_COORDINATE_PROFILE = CoordinateProfile(
    profile_id="generic-xyxy-1000-v1",
    model_patterns=(),
    bbox_field="bbox_xyxy_1000",
    coordinate_order="xyxy",
    coordinate_space="normalized_1000",
    prompt_version="coordinates-generic-xyxy-1000-v1",
    reasoning_policy=None,
)

GEMINI_COORDINATE_PROFILE = CoordinateProfile(
    profile_id="gemini-family-yxyx-1000-v1",
    model_patterns=("gemini/gemini-*", "openrouter/google/gemini-*"),
    bbox_field="box_2d",
    coordinate_order="yxyx",
    coordinate_space="normalized_1000",
    prompt_version="coordinates-gemini-yxyx-1000-v1",
    reasoning_policy=None,
)

CLAUDE_STANDARD_COORDINATE_PROFILE = CoordinateProfile(
    profile_id="claude-family-xyxy-pixel-standard-v1",
    model_patterns=("anthropic/claude-*", "openrouter/anthropic/claude-*"),
    bbox_field="bbox_pixels_xyxy",
    coordinate_order="xyxy",
    coordinate_space="pixel",
    prompt_version="coordinates-claude-pixel-v1",
    reasoning_policy=None,
    image_max_edge=1_568,
    image_patch_size=28,
    image_max_patches=1_568,
)

CLAUDE_HIGH_RES_COORDINATE_PROFILE = CoordinateProfile(
    profile_id="claude-family-xyxy-pixel-high-v1",
    model_patterns=(
        "anthropic/claude-sonnet-5*",
        "anthropic/claude-fable-5*",
        "anthropic/claude-opus-4.8*",
        "openrouter/anthropic/claude-sonnet-5*",
        "openrouter/anthropic/claude-fable-5*",
        "openrouter/anthropic/claude-opus-4.8*",
    ),
    bbox_field="bbox_pixels_xyxy",
    coordinate_order="xyxy",
    coordinate_space="pixel",
    prompt_version="coordinates-claude-pixel-v1",
    reasoning_policy=None,
    image_max_edge=2_576,
    image_patch_size=28,
    image_max_patches=4_784,
)

OPENAI_GPT5_COORDINATE_PROFILE = CoordinateProfile(
    profile_id="openai-gpt5-xyxy-pixel-high-v1",
    model_patterns=(
        "openai/gpt-5",
        "openai/gpt-5-mini",
        "openai/gpt-5.6-luna",
        "openai/gpt-5.6-terra",
        "openai/gpt-5.6-sol",
        "openrouter/openai/gpt-5",
        "openrouter/openai/gpt-5-mini",
        "openrouter/openai/gpt-5.6-luna",
        "openrouter/openai/gpt-5.6-terra",
        "openrouter/openai/gpt-5.6-sol",
    ),
    bbox_field="bbox_pixels_xyxy",
    coordinate_order="xyxy",
    coordinate_space="pixel",
    prompt_version="coordinates-openai-gpt5-pixel-high-v1",
    reasoning_policy=None,
    image_max_edge=2_048,
    image_patch_size=32,
    image_max_patches=2_500,
    image_detail="high",
)

OPENAI_GPT6_COORDINATE_PROFILE = CoordinateProfile(
    profile_id="openai-gpt6-xyxy-pixel-high-v1",
    model_patterns=(
        "openai/gpt-6-luna",
        "openai/gpt-6.1-sol",
        "openrouter/openai/gpt-6-luna",
        "openrouter/openai/gpt-6.1-sol",
    ),
    bbox_field="bbox_pixels_xyxy",
    coordinate_order="xyxy",
    coordinate_space="pixel",
    prompt_version="coordinates-openai-gpt6-pixel-high-v1",
    reasoning_policy=None,
    image_max_edge=2_048,
    image_patch_size=32,
    image_max_patches=2_500,
    image_detail="high",
)

_EXACT_COORDINATE_PROFILES: tuple[CoordinateProfile, ...] = (
    OPENAI_GPT5_COORDINATE_PROFILE,
    OPENAI_GPT6_COORDINATE_PROFILE,
    CoordinateProfile(
        profile_id="glm-5.3-flash-yxyx-1000-v1",
        model_patterns=("openrouter/z-ai/glm-5.3-flash",),
        bbox_field="box_2d",
        coordinate_order="yxyx",
        coordinate_space="normalized_1000",
        prompt_version="coordinates-glm53-yxyx-1000-v1",
        reasoning_policy="low_exclude",
    ),
    CoordinateProfile(
        profile_id="kimi-k3-xyxy-normalized-v1",
        model_patterns=("openrouter/moonshotai/kimi-k3",),
        bbox_field="bbox_normalized_xyxy",
        coordinate_order="xyxy",
        coordinate_space="normalized",
        prompt_version="coordinates-kimi-k3-xyxy-normalized-v1",
        reasoning_policy="disabled",
    ),
    CoordinateProfile(
        profile_id="mimo-v2.5-yxyx-normalized-v1",
        model_patterns=("openrouter/xiaomi/mimo-v2.5",),
        bbox_field="bbox_normalized_yxyx",
        coordinate_order="yxyx",
        coordinate_space="normalized",
        prompt_version="coordinates-mimo-v25-yxyx-normalized-v1",
        reasoning_policy="disabled",
    ),
    CoordinateProfile(
        profile_id="qwen3-vl-xyxy-1000-v1",
        model_patterns=(
            "openrouter/qwen/qwen3-vl-8b-instruct",
            "openrouter/qwen/qwen3-vl-32b-instruct",
        ),
        bbox_field="bbox_2d",
        coordinate_order="xyxy",
        coordinate_space="normalized_1000",
        prompt_version="coordinates-qwen3vl-xyxy-1000-v1",
        reasoning_policy="disabled",
    ),
)


def coordinate_profile_for_model(model_ref: str) -> CoordinateProfile | None:
    """Select documented family facts or an exact checked-in override.

    OpenRouter is a transport prefix. Its next path component identifies the
    upstream model vendor; parsing those components avoids guessing a family
    from a model substring.
    """

    normalized = model_ref.strip().lower()
    parts = normalized.split("/")
    if len(parts) < 2:
        return None
    if parts[0] == "openrouter":
        if len(parts) < 3:
            return None
        provider, model_name = parts[1], "/".join(parts[2:])
    else:
        provider, model_name = parts[0], "/".join(parts[1:])

    if provider in {"gemini", "google"} and model_name.startswith("gemini-"):
        return GEMINI_COORDINATE_PROFILE
    if provider == "anthropic" and model_name.startswith("claude-"):
        high_resolution_prefixes = (
            "claude-sonnet-5",
            "claude-fable-5",
            "claude-opus-4.8",
            "claude-opus-4-8",
        )
        if model_name.startswith(high_resolution_prefixes):
            return CLAUDE_HIGH_RES_COORDINATE_PROFILE
        return CLAUDE_STANDARD_COORDINATE_PROFILE
    for profile in _EXACT_COORDINATE_PROFILES:
        if normalized in profile.model_patterns:
            return profile
    return None


@dataclass(frozen=True)
class DecodedBoundingBox:
    """One validated box in declared, normalized, and output-pixel frames."""

    raw_values: tuple[float, float, float, float]
    raw_xyxy: tuple[float, float, float, float]
    normalized_xyxy: tuple[float, float, float, float]

    def page_normalized(self, *, raw_space: CoordinateSpace) -> dict[str, Any]:
        x0, y0, x1, y1 = self.normalized_xyxy
        raw_x0, raw_y0, raw_x1, raw_y1 = self.raw_xyxy
        return {
            "space": "page_normalized",
            "x0": round(x0, 6),
            "y0": round(y0, 6),
            "x1": round(x1, 6),
            "y1": round(y1, 6),
            "raw": {
                "space": raw_space,
                "x0": raw_x0,
                "y0": raw_y0,
                "x1": raw_x1,
                "y1": raw_y1,
            },
        }

    def pixel_quad(self, *, output_width: int, output_height: int) -> list[list[float]]:
        """Scale into a caller-declared output frame without changing shape."""

        if output_width < 1 or output_height < 1:
            raise ValueError("output image dimensions must be positive")
        x0, y0, x1, y1 = self.normalized_xyxy
        px0 = x0 * output_width
        py0 = y0 * output_height
        px1 = x1 * output_width
        py1 = y1 * output_height
        return [[px0, py0], [px1, py0], [px1, py1], [px0, py1]]


def inference_image_size(
    profile: CoordinateProfile,
    *,
    width: int,
    height: int,
) -> tuple[int, int]:
    """Return the largest aspect-preserving size inside provider limits.

    Patch-based providers measure the padded inference tensor rather than the
    unpadded image. The binary search matches that discrete contract exactly,
    so a caller can resize before upload and interpret pixel coordinates in the
    dimensions actually sent to the model.
    """

    if width < 1 or height < 1:
        raise ValueError("image dimensions must be positive")
    max_edge = profile.image_max_edge
    patch_size = profile.image_patch_size
    max_patches = profile.image_max_patches
    if max_edge is None and max_patches is None:
        return width, height

    def padded(value: int) -> int:
        if patch_size is None:
            return value
        return math.ceil(value / patch_size) * patch_size

    def fits(candidate_width: int, candidate_height: int) -> bool:
        padded_width = padded(candidate_width)
        padded_height = padded(candidate_height)
        if max_edge is not None and max(padded_width, padded_height) > max_edge:
            return False
        if max_patches is not None:
            assert patch_size is not None
            patch_count = math.ceil(candidate_width / patch_size) * math.ceil(
                candidate_height / patch_size
            )
            if patch_count > max_patches:
                return False
        return True

    if fits(width, height):
        return width, height
    if height > width:
        resized_height, resized_width = inference_image_size(
            profile,
            width=height,
            height=width,
        )
        return resized_width, resized_height

    aspect_ratio = width / height
    lower, upper = 1, width
    while lower + 1 < upper:
        candidate_width = (lower + upper) // 2
        candidate_height = max(round(candidate_width / aspect_ratio), 1)
        if fits(candidate_width, candidate_height):
            lower = candidate_width
        else:
            upper = candidate_width
    return lower, max(round(lower / aspect_ratio), 1)


def coordinate_instruction(profile: CoordinateProfile) -> str:
    """Describe the selected wire dialect unambiguously in a model prompt."""

    order = (
        "[x_min, y_min, x_max, y_max]"
        if profile.coordinate_order == "xyxy"
        else "[y_min, x_min, y_max, x_max]"
    )
    if profile.coordinate_space == "normalized":
        space = "numbers from 0 to 1 normalized to image width and height"
    elif profile.coordinate_space == "normalized_1000":
        space = "integers from 0 to 1000 normalized to image width (x) and height (y)"
    else:
        space = "pixel coordinates in the supplied image"
    return f'Put each box in "{profile.bbox_field}" as {order}, using {space}.'


def decode_bounding_box(
    profile: CoordinateProfile,
    raw_bbox: Any,
    *,
    image_width: int,
    image_height: int,
) -> DecodedBoundingBox | None:
    """Decode only the supplied profile, rejecting malformed or unsafe boxes."""

    if image_width < 1 or image_height < 1:
        raise ValueError("inference image dimensions must be positive")
    if not isinstance(raw_bbox, (list, tuple)) or len(raw_bbox) != 4:
        return None
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        for value in raw_bbox
    ):
        return None
    values = tuple(float(value) for value in raw_bbox)
    if not all(math.isfinite(value) for value in values):
        return None
    if profile.coordinate_order == "xyxy":
        x0, y0, x1, y1 = values
    else:
        y0, x0, y1, x1 = values

    if profile.coordinate_space == "normalized":
        scale_x = scale_y = 1.0
    elif profile.coordinate_space == "normalized_1000":
        scale_x = scale_y = 1000.0
    else:
        scale_x = float(image_width)
        scale_y = float(image_height)
    if not (0 <= x0 < x1 <= scale_x and 0 <= y0 < y1 <= scale_y):
        return None
    return DecodedBoundingBox(
        raw_values=values,
        raw_xyxy=(x0, y0, x1, y1),
        normalized_xyxy=(x0 / scale_x, y0 / scale_y, x1 / scale_x, y1 / scale_y),
    )
