"""One-shot, offline Indra OCR worker for already-rendered page images."""

from __future__ import annotations

import json
import math
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from .pdf_render_worker import POST_SUCCESS_FAILURE_LIMIT, is_environment_failure
from frisket.engine.sandbox.fence import SandboxEnforcementUnavailable
from frisket.engine.sandbox.shim import SandboxTeardownError


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
    progress_stream = sys.stdout

    def report(completed, total):
        progress_stream.write(
            json.dumps(
                {
                    "kind": "ocr_pages",
                    "completed": completed,
                    "total": total,
                }
            )
            + "\n"
        )
        progress_stream.flush()

    try:
        import cv2
        import indra_ocr

        # Library diagnostics never share the page-progress stream.
        with redirect_stdout(sys.stderr):
            converter = indra_ocr.load(
                path=payload["model_dir"],
                threads=payload["threads"],
                mode="ocr",
            )
            if "pdf" in payload:
                pages = _pdf_pages(converter, payload, report)
            else:
                pages = []
                for raw_path in payload["paths"]:
                    image = cv2.imread(str(raw_path), cv2.IMREAD_GRAYSCALE)
                    if image is None or len(image.shape) != 2:
                        raise ValueError("Indra OCR input is not a readable page image")
                    pages.append(
                        _recognize_page(
                            converter,
                            raw_path,
                            width=int(image.shape[1]),
                            height=int(image.shape[0]),
                        )
                    )
        _write(output, {"pages": pages})
    except Exception as exc:  # noqa: BLE001 - worker returns one content-free failure
        if is_environment_failure(exc) or isinstance(
            exc, (SandboxTeardownError, SandboxEnforcementUnavailable)
        ):
            # Fatal storage failures cannot rely on writing the result file.
            progress_stream.write(json.dumps({"error": "environment_failed"}) + "\n")
            progress_stream.flush()
        else:
            _write(output, {"error": "Indra OCR could not process the page images"})
        return


def _recognize_page(converter, path, *, width, height):
    converted = list(converter.convert(str(path)))
    if len(converted) != 1 or converted[0][0] != 0:
        raise RuntimeError("Indra OCR returned an invalid page result")
    return _page_result(
        converted[0][1],
        width=width,
        height=height,
    )


def _pdf_pages(converter, payload, report):
    import pypdfium2 as pdfium

    from .pdf_render_worker import render_page

    document = pdfium.PdfDocument(payload["pdf"])
    pages = []
    has_success = False
    consecutive_failures = 0
    raster = Path(payload["out"]).parent / "indra-page.png"
    try:
        total = len(document)
        for number in range(1, total + 1):
            stage = "rendering"
            frame = {}
            try:
                width, height = render_page(
                    document,
                    number,
                    raster,
                    dpi=payload["dpi"],
                )
                frame = {"width": width, "height": height}
                stage = "recognition"
                page = _recognize_page(converter, raster, width=width, height=height)
                has_success = True
                consecutive_failures = 0
            except (SandboxTeardownError, SandboxEnforcementUnavailable):
                raise
            except Exception as exc:  # noqa: BLE001 - native page errors have varied types
                raster_memory = stage == "rendering" and isinstance(exc, MemoryError)
                if is_environment_failure(exc) and not raster_memory:
                    raise
                consecutive_failures += 1
                if not has_success and consecutive_failures >= 5:
                    raise RuntimeError(
                        "Indra OCR stopped after 5 consecutive page failures"
                    ) from None
                page = {
                    "text": "",
                    "blocks": [],
                    "error": {"code": "ocr_page_failed", "stage": stage},
                }
            finally:
                raster.unlink(missing_ok=True)
            pages.append({**page, **frame})
            report(number, total)
            if has_success and consecutive_failures >= POST_SUCCESS_FAILURE_LIMIT:
                pages.extend(
                    {
                        "text": "",
                        "blocks": [],
                        "error": {
                            "code": "ocr_page_skipped",
                            "stage": "not attempted after OCR stopped",
                        },
                    }
                    for _ in range(number, total)
                )
                break
    finally:
        document.close()
        raster.unlink(missing_ok=True)
    return pages


def _write(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()
