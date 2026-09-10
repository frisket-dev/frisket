from __future__ import annotations

from typing import Any


def _bbox_dimension(value: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        try:
            dimension = float(value[key])
        except (KeyError, TypeError, ValueError):
            continue
        if dimension > 0:
            return dimension
    return None


def _is_polygon(value: Any) -> bool:
    """A pixel polygon is a list of >= 2 ``[x, y]`` numeric pairs."""
    if not isinstance(value, list) or len(value) < 2:
        return False
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            return False
        try:
            float(point[0])
            float(point[1])
        except (TypeError, ValueError):
            return False
    return True


def normalize_bbox(
    value: Any,
    *,
    frame: str = "page",
    width: float | None = None,
    height: float | None = None,
) -> dict[str, Any] | None:
    """Coerce one bbox of any input genus into the canonical grounding shape.

    Returns ``{space, x0, y0, x1, y1, raw}`` with coordinates in 0-1 space,
    top-left origin, ``x0 < x1`` and ``y0 < y1``, or ``None`` when the input is
    unusable (missing pixel dimensions, degenerate box, unrecognized space).

    ``frame`` selects the reference frame the caller knows the box lives in:
    ``"page"`` -> ``space = "page_normalized"`` (PDF/document/image page, OCR
    blocks, map.extract regions); ``"frame"`` -> ``space = "frame_normalized"``
    (video frame regions, extract_faces). ``width``/``height`` supply the pixel
    reference dimensions for polygon and ``{x, y, w, h}`` inputs (and as a
    fallback for pixel-space dicts that do not carry their own dimensions).
    """

    frame_label = f"{frame}_normalized"

    # OCR pixel polygon -> enclosing axis-aligned box.
    if _is_polygon(value):
        xs = [float(point[0]) for point in value]
        ys = [float(point[1]) for point in value]
        box: dict[str, Any] = {
            "space": "pixel",
            "x0": min(xs),
            "y0": min(ys),
            "x1": max(xs),
            "y1": max(ys),
        }
        if width:
            box["page_width"] = width
        if height:
            box["page_height"] = height
        return _finalize(box, frame_label, raw=value)

    # map.extract sometimes wraps its single region dict in a list.
    if isinstance(value, list) and value:
        value = value[0]
    if not isinstance(value, dict):
        return None

    if all(key in value for key in ("x0", "y0", "x1", "y1")):
        return _finalize(
            value,
            frame_label,
            raw=dict(value),
            extra_width=width,
            extra_height=height,
        )

    # extract_faces pixel {x, y, w, h}.
    if all(key in value for key in ("x", "y", "w", "h")):
        try:
            x = float(value["x"])
            y = float(value["y"])
            w = float(value["w"])
            h = float(value["h"])
        except (TypeError, ValueError):
            return None
        box = {"space": "pixel", "x0": x, "y0": y, "x1": x + w, "y1": y + h}
        page_width = _bbox_dimension(value, "page_width", "image_width") or width
        page_height = _bbox_dimension(value, "page_height", "image_height") or height
        if page_width:
            box["page_width"] = page_width
        if page_height:
            box["page_height"] = page_height
        return _finalize(box, frame_label, raw=dict(value))

    return None


def _finalize(
    value: dict[str, Any],
    frame_label: str,
    *,
    raw: Any,
    extra_width: float | None = None,
    extra_height: float | None = None,
) -> dict[str, Any] | None:
    try:
        x0 = float(value["x0"])
        y0 = float(value["y0"])
        x1 = float(value["x1"])
        y1 = float(value["y1"])
    except (KeyError, TypeError, ValueError):
        return None

    space = str(value.get("space") or "page_normalized").strip().lower()
    max_coord = max(x0, y0, x1, y1)
    if space in {"page_normalized", "frame_normalized", "normalized", "relative"}:
        scale_x = scale_y = 1.0
    elif space in {"percent", "percentage", "page_percent"}:
        scale_x = scale_y = 100.0
    elif space in {
        "page_1000",
        "provider_1000",
        "normalized_1000",
        "thousandths",
        "bbox_1000",
    }:
        scale_x = scale_y = 1000.0
    elif space in {"pixel", "pixels", "page_pixels", "image_pixels"}:
        width = _bbox_dimension(value, "page_width", "width", "image_width")
        height = _bbox_dimension(value, "page_height", "height", "image_height")
        if width is None:
            width = extra_width
        if height is None:
            height = extra_height
        if not width or not height:
            return None
        scale_x = float(width)
        scale_y = float(height)
    elif max_coord <= 1:
        scale_x = scale_y = 1.0
    elif max_coord <= 100:
        scale_x = scale_y = 100.0
    elif max_coord <= 1000:
        scale_x = scale_y = 1000.0
    else:
        return None

    if scale_x <= 0 or scale_y <= 0:
        return None
    nx0 = round(x0 / scale_x, 6)
    ny0 = round(y0 / scale_y, 6)
    nx1 = round(x1 / scale_x, 6)
    ny1 = round(y1 / scale_y, 6)
    if not (0 <= nx0 < nx1 <= 1 and 0 <= ny0 < ny1 <= 1):
        return None
    return {
        "space": frame_label,
        "x0": nx0,
        "y0": ny0,
        "x1": nx1,
        "y1": ny1,
        "raw": raw,
    }
