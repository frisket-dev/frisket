"""Strict normalization of dots.mocr reading-order JSON."""

from __future__ import annotations

import json
import math
from typing import Any

IMAGE_FACTOR = 28
MIN_PIXELS = 3_136
MAX_PIXELS = 11_289_600

LAYOUT_CATEGORIES = frozenset(
    {
        "Caption",
        "Footnote",
        "Formula",
        "List-item",
        "Other",
        "Page-footer",
        "Page-header",
        "Picture",
        "Section-header",
        "Table",
        "Text",
        "Title",
    }
)


class LayoutOutputError(ValueError):
    """The model did not return its declared layout format."""


def _round_by_factor(number: int, factor: int) -> int:
    return round(number / factor) * factor


def _ceil_by_factor(number: float, factor: int) -> int:
    return math.ceil(number / factor) * factor


def _floor_by_factor(number: float, factor: int) -> int:
    return math.floor(number / factor) * factor


def smart_resize(height: int, width: int) -> tuple[int, int]:
    """Reproduce dots.mocr's factor-28 vision input dimensions."""

    if height < 1 or width < 1:
        raise ValueError("image dimensions must be positive")
    if max(height, width) / min(height, width) > 200:
        raise ValueError("image aspect ratio exceeds dots.mocr's limit")

    resized_height = max(IMAGE_FACTOR, _round_by_factor(height, IMAGE_FACTOR))
    resized_width = max(IMAGE_FACTOR, _round_by_factor(width, IMAGE_FACTOR))
    if resized_height * resized_width > MAX_PIXELS:
        beta = math.sqrt((height * width) / MAX_PIXELS)
        resized_height = max(
            IMAGE_FACTOR, _floor_by_factor(height / beta, IMAGE_FACTOR)
        )
        resized_width = max(IMAGE_FACTOR, _floor_by_factor(width / beta, IMAGE_FACTOR))
    elif resized_height * resized_width < MIN_PIXELS:
        beta = math.sqrt(MIN_PIXELS / (height * width))
        resized_height = _ceil_by_factor(height * beta, IMAGE_FACTOR)
        resized_width = _ceil_by_factor(width * beta, IMAGE_FACTOR)
        if resized_height * resized_width > MAX_PIXELS:
            beta = math.sqrt((resized_height * resized_width) / MAX_PIXELS)
            resized_height = max(
                IMAGE_FACTOR,
                _floor_by_factor(resized_height / beta, IMAGE_FACTOR),
            )
            resized_width = max(
                IMAGE_FACTOR,
                _floor_by_factor(resized_width / beta, IMAGE_FACTOR),
            )
    return resized_height, resized_width


def _json_text(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[0] in {"```", "```json"}:
            return "\n".join(lines[1:-1]).strip()
    return stripped


def fallback_text(output: str) -> str:
    """Keep useful model text when its optional layout structure is malformed."""

    cleaned = _json_text(output)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        return cleaned
    if isinstance(value, list):
        texts = [cell.get("text") for cell in value if isinstance(cell, dict)]
        usable = [
            text.strip() for text in texts if isinstance(text, str) and text.strip()
        ]
        if usable:
            return "\n\n".join(usable)
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"].strip()
    return cleaned


def _coordinate(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LayoutOutputError("layout bbox coordinates must be numbers")
    number = float(value)
    if not math.isfinite(number):
        raise LayoutOutputError("layout bbox coordinates must be finite")
    return number


def parse_page(output: str, *, width: int, height: int) -> dict[str, Any]:
    """Map model-space boxes to Frisket polygons in source-image pixels."""

    try:
        cells = json.loads(_json_text(output))
    except json.JSONDecodeError as exc:
        raise LayoutOutputError("dots.mocr returned malformed JSON") from exc
    if not isinstance(cells, list):
        raise LayoutOutputError("dots.mocr layout output must be a JSON array")

    resized_height, resized_width = smart_resize(height, width)
    scale_x = width / resized_width
    scale_y = height / resized_height
    blocks: list[dict[str, Any]] = []

    for cell in cells:
        if not isinstance(cell, dict):
            raise LayoutOutputError("each layout element must be an object")
        category = cell.get("category")
        if category not in LAYOUT_CATEGORIES:
            raise LayoutOutputError("layout element has an unknown category")
        bbox = cell.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise LayoutOutputError("layout element must have a four-value bbox")
        x1, y1, x2, y2 = (_coordinate(value) for value in bbox)
        if x2 <= x1 or y2 <= y1:
            raise LayoutOutputError("layout element bbox must have positive area")

        text = cell.get("text", "" if category == "Picture" else None)
        if not isinstance(text, str):
            raise LayoutOutputError("layout element text must be a string")

        sx1 = max(0, min(width, int(x1 * scale_x)))
        sy1 = max(0, min(height, int(y1 * scale_y)))
        sx2 = max(0, min(width, int(x2 * scale_x)))
        sy2 = max(0, min(height, int(y2 * scale_y)))
        if sx2 <= sx1 or sy2 <= sy1:
            raise LayoutOutputError("scaled layout bbox must have positive area")

        blocks.append(
            {
                "text": text,
                "bbox": [[sx1, sy1], [sx2, sy1], [sx2, sy2], [sx1, sy2]],
            }
        )

    return {
        "text": "\n\n".join(
            block["text"].strip() for block in blocks if block["text"].strip()
        ),
        "blocks": blocks,
    }
