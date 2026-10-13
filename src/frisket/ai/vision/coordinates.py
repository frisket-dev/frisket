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
