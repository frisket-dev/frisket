"""Bounded page signals over existing thumbnails and positioned text."""

from __future__ import annotations

from collections.abc import Sequence
from itertools import islice
import math
import statistics
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray

if TYPE_CHECKING:
    from frisket.actions.document_extraction_types import PositionedToken


_MAX_EDGE = 1_400
_MAX_TOKENS = 5_000
_PageBox = tuple[float, float, float, float, str]


def page_quality_signals(
    *,
    grayscale_thumbnail: NDArray[np.uint8] | None,
    positioned_tokens: Sequence[PositionedToken] | None,
) -> tuple[bool, bool]:
    tokens = positioned_tokens or ()
    boxes = _normalized_boxes(tokens)
    image_shape = None
    if isinstance(grayscale_thumbnail, np.ndarray) and grayscale_thumbnail.ndim == 2:
        image_shape = (
            int(grayscale_thumbnail.shape[0]),
            int(grayscale_thumbnail.shape[1]),
        )
    vertical_words = _predominantly_vertical(boxes, image_shape=image_shape)
    boxes_truncated = len(tokens) > _MAX_TOKENS
    unexplained_ink = bool(
        grayscale_thumbnail is not None
        and not boxes_truncated
        and _has_unexplained_ink(grayscale_thumbnail, boxes)
    )
    return vertical_words, unexplained_ink


def _normalized_boxes(tokens: Sequence[PositionedToken]) -> list[_PageBox]:
    boxes: list[_PageBox] = []
    for token in islice(tokens, _MAX_TOKENS):
        box = token.box
        values = (float(box.x0), float(box.y0), float(box.x1), float(box.y1))
        if not all(math.isfinite(value) for value in values):
            continue
        x0, y0, x1, y1 = values
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            continue
        boxes.append((x0, y0, x1, y1, str(token.text)))
    return boxes


def _predominantly_vertical(
    boxes: list[_PageBox], *, image_shape: tuple[int, int] | None
) -> bool:
    words = [box for box in boxes if len(str(box[4]).strip()) >= 2]
    if len(boxes) < 20 or not words:
        return False
    height_scale, width_scale = image_shape or (1, 1)
    vertical_fraction = sum(
        (box[2] - box[0]) * width_scale < 0.8 * (box[3] - box[1]) * height_scale
        for box in words
    ) / len(words)
    advances = [
        (box[2] - box[0])
        * width_scale
        / max(
            1e-9,
            (box[3] - box[1]) * height_scale * len(str(box[4]).strip()),
        )
        for box in words
    ]
    return vertical_fraction > 0.60 and statistics.median(advances) < 0.15


def _has_unexplained_ink(image: NDArray[np.uint8], boxes: list[_PageBox]) -> bool:
    if not isinstance(image, np.ndarray) or image.ndim != 2 or image.size == 0:
        raise ValueError("grayscale_thumbnail must be a non-empty 2D numpy array")
    if image.dtype != np.uint8:
        raise ValueError("grayscale_thumbnail must use uint8 pixels")

    import cv2

    scale = min(1.0, _MAX_EDGE / max(image.shape))
    if scale < 1.0:
        image = cv2.resize(
            image,
            None,
            fx=scale,
            fy=scale,
            interpolation=cv2.INTER_AREA,
        )
    image_height, image_width = image.shape
    pixel_boxes = [
        (
            x0 * image_width,
            y0 * image_height,
            x1 * image_width,
            y1 * image_height,
        )
        for x0, y0, x1, y1, _text in boxes
    ]
    heights = [y1 - y0 for _x0, y0, _x1, y1 in pixel_boxes]
    median_height = (
        float(statistics.median(heights)) if heights else max(6.0, image_height * 0.012)
    )

    _threshold, ink = cv2.threshold(
        image, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )
    component_count, labels, stats, _centroids = cv2.connectedComponentsWithStats(
        ink, 8
    )
    min_area = max(3, round(0.018 * median_height * median_height))
    max_area = max(min_area + 1, round(40.0 * median_height * median_height))
    eligible_labels = np.zeros(component_count, dtype=np.uint8)
    for label in range(1, component_count):
        _x, _y, width, height, area = (int(value) for value in stats[label])
        if area < min_area or area > max_area:
            continue
        if height < 0.12 * median_height or height > 4.0 * median_height:
            continue
        if width > 24.0 * median_height:
            continue
        if width > 8.0 * median_height and height < 0.35 * median_height:
            continue
        if height > 8.0 * median_height and width < 0.35 * median_height:
            continue
        eligible_labels[label] = 1

    eligible = eligible_labels[labels]
    box_mask = np.zeros_like(eligible)
    padding = max(2, round(0.45 * median_height))
    for x0, y0, x1, y1 in pixel_boxes:
        left = max(0, round(x0) - padding)
        top = max(0, round(y0) - padding)
        right = min(image_width, round(x1) + padding + 1)
        bottom = min(image_height, round(y1) + padding + 1)
        box_mask[top:bottom, left:right] = 1

    eligible_pixels = int(eligible.sum())
    if not eligible_pixels:
        return False
    explained_ink = int((eligible & box_mask).sum()) / eligible_pixels
    raw_ink_fraction = float(np.count_nonzero(ink)) / ink.size
    eligible_ink_fraction = eligible_pixels / ink.size
    return (
        explained_ink < 0.93
        and eligible_ink_fraction >= 0.002
        and raw_ink_fraction <= 0.25
    )
