"""One-shot, offline Indra OCR worker for already-rendered page images."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any


def _original_box(box: Any, *, rotation: int, width: int, height: int) -> list[float]:
    """Map Indra's upright-page rectangle back to the input image."""

    if (
        not isinstance(box, (list, tuple))
        or len(box) != 4
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in box
        )
    ):
        raise ValueError("invalid Indra OCR line box")
    x0, y0, x1, y1 = (float(value) for value in box)
    if x1 < x0 or y1 < y0:
        raise ValueError("invalid Indra OCR line box")
    turn = rotation % 360
    if turn == 0:
        mapped = [x0, y0, x1, y1]
    elif turn == 90:
        mapped = [y0, height - x1, y1, height - x0]
    elif turn == 180:
        mapped = [width - x1, height - y1, width - x0, height - y0]
    elif turn == 270:
        mapped = [width - y1, x0, width - y0, x1]
    else:
        raise ValueError("invalid Indra OCR page rotation")
    return [
        round(max(0.0, min(float(limit), value)), 1)
        for value, limit in zip(mapped, (width, height, width, height), strict=True)
    ]


def _polygon(box: list[float]) -> list[list[float]]:
    x0, y0, x1, y1 = box
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def _page_result(page: Any, *, width: int, height: int) -> dict[str, Any]:
    if getattr(page, "source", None) == "error":
        raise RuntimeError("Indra OCR could not read a page")
    rotation = getattr(page, "rotation", 0)
    if isinstance(rotation, bool) or not isinstance(rotation, int):
        raise ValueError("invalid Indra OCR page rotation")
    blocks: list[dict[str, Any]] = []
    for line in getattr(page, "lines", ()):
        text = str(getattr(line, "text", "")).strip()
        if not text:
            continue
        block: dict[str, Any] = {
            "text": text,
            "bbox": _polygon(
                _original_box(
                    getattr(line, "box", None),
                    rotation=rotation,
                    width=width,
                    height=height,
                )
            ),
        }
        score = getattr(line, "conf", None)
        if (
            isinstance(score, (int, float))
            and not isinstance(score, bool)
            and math.isfinite(score)
            and 0 <= score <= 1
        ):
            block["score"] = round(float(score), 4)
        blocks.append(block)
    return {
        "text": "\n".join(block["text"] for block in blocks),
        "blocks": blocks,
    }


def main() -> None:
    payload = json.load(sys.stdin)
    output = Path(payload["out"])
    try:
        import cv2
        import indra_ocr

        converter = indra_ocr.load(
            path=payload["model_dir"],
            threads=payload["threads"],
            mode="ocr",
        )
        pages = []
        for raw_path in payload["paths"]:
            image = cv2.imread(str(raw_path), cv2.IMREAD_GRAYSCALE)
            if image is None or len(image.shape) != 2:
                raise ValueError("Indra OCR input is not a readable page image")
            converted = list(converter.convert(raw_path))
            if len(converted) != 1 or converted[0][0] != 0:
                raise RuntimeError("Indra OCR returned an invalid page result")
            pages.append(
                _page_result(
                    converted[0][1],
                    width=int(image.shape[1]),
                    height=int(image.shape[0]),
                )
            )
    except Exception:  # noqa: BLE001 - worker returns one content-free failure
        _write(output, {"error": "Indra OCR could not process the page images"})
        return
    _write(output, {"pages": pages})


def _write(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()
