"""One-shot positioned native PDF text extraction, imported after fencing."""

from __future__ import annotations

import json
import math
from pathlib import Path
import sys


def _normalized_box(bounds, media, rotation):
    left, bottom, right, top = media
    width, height = right - left, top - bottom
    points = []
    for raw_x in (bounds[0], bounds[2]):
        for raw_y in (bounds[1], bounds[3]):
            x, y = (raw_x - left) / width, (raw_y - bottom) / height
            if rotation == 0:
                point = (x, 1 - y)
            elif rotation == 90:
                point = (y, x)
            elif rotation == 180:
                point = (1 - x, y)
            else:
                point = (1 - y, 1 - x)
            points.append(point)
    xs, ys = zip(*points)
    box = [max(0, min(xs)), max(0, min(ys)), min(1, max(xs)), min(1, max(ys))]
    if not all(math.isfinite(value) for value in box):
        raise ValueError("invalid_geometry")
    if box[0] >= box[2] or box[1] >= box[3]:
        return None  # Wholly outside the displayed MediaBox, or a nonprinting glyph.
    return dict(zip(("x0", "y0", "x1", "y1"), box))


def _page_tokens(page, textpage, count):
    import pypdfium2.raw as raw

    media, rotation = page.get_mediabox(), page.get_rotation()
    tokens = []
    characters, boxes = [], []

    def flush():
        if characters:
            tokens.append(
                {
                    "text": "".join(characters),
                    "box": {
                        "x0": min(box["x0"] for box in boxes),
                        "y0": min(box["y0"] for box in boxes),
                        "x1": max(box["x1"] for box in boxes),
                        "y1": max(box["y1"] for box in boxes),
                    },
                    "granularity": "word",
                }
            )
        characters.clear()
        boxes.clear()

    # GetUnicode shares the character index space with GetCharBox; indexing a
    # get_text_range() string instead misaligns inserted/excluded characters.
    for index in range(count):
        codepoint = raw.FPDFText_GetUnicode(textpage, index)
        character = chr(codepoint) if codepoint else ""
        if not character or character.isspace():
            flush()
            continue
        box = _normalized_box(textpage.get_charbox(index), media, rotation)
        if box is None:
            flush()
            continue
        characters.append(character)
        boxes.append(box)
    flush()
    return tokens


def _extract(request):
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(request["source"])
    try:
        page_count = len(document)
        mode = request.get("mode", "whole")
        first_page = request.get("start_page", request.get("page", 1))
        if mode != "whole" and first_page > page_count:
            raise ValueError("page_out_of_range")
        if mode == "sample":
            import pypdfium2.raw as raw

            page = document[first_page - 1]
            try:
                textpage = page.get_textpage()
                try:
                    count = textpage.count_chars()
                    # GetUnicode preserves non-BMP text and avoids building
                    # boxes or asking PDFium for the whole page's text buffer.
                    prefix = []
                    for index in range(min(count, request["max_chars"])):
                        codepoint = raw.FPDFText_GetUnicode(textpage, index)
                        if codepoint:
                            prefix.append(chr(codepoint))
                    return {
                        "ok": True,
                        "text": "".join(prefix),
                        "page": first_page,
                        "page_count": page_count,
                        "truncated": count > request["max_chars"],
                    }
                finally:
                    textpage.close()
            finally:
                page.close()
        if mode == "whole" and page_count > request["max_pages"]:
            raise ValueError("document_limit_exceeded")
        pages, characters = [], 0
        last_page = min(page_count, first_page - 1 + request["max_pages"])
        for index in range(first_page - 1, last_page):
            page = document[index]
            try:
                # Same visible page as the PDF renderer, including intrinsic
                # rotation and nonzero MediaBox origin, without CropBox clipping.
                page.set_cropbox(*page.get_mediabox())
                width, height = page.get_size()
                textpage = page.get_textpage()
                try:
                    count = textpage.count_chars()
                    if characters + count > request["max_chars"]:
                        if mode == "batch" and pages:
                            break
                        raise ValueError(
                            "page_limit_exceeded"
                            if mode == "batch"
                            else "document_limit_exceeded"
                        )
                    characters += count
                    tokens = _page_tokens(page, textpage, count)
                finally:
                    textpage.close()
                pages.append(
                    {
                        "page": index + 1,
                        "width": width,
                        "height": height,
                        "tokens": tokens,
                    }
                )
            finally:
                page.close()
        response = {"ok": True, "pages": pages}
        if mode == "batch":
            next_page = pages[-1]["page"] + 1
            response.update(
                page_count=page_count,
                next_page=next_page if next_page <= page_count else None,
            )
        return response
    finally:
        document.close()


def main():
    try:
        request = json.load(sys.stdin)
        mode = request.get("mode", "whole") if isinstance(request, dict) else None
        fields = {"source", "max_chars"}
        if mode == "whole":
            fields.add("max_pages")
        elif mode == "batch":
            fields.update(("mode", "start_page", "max_pages"))
        elif mode == "sample":
            fields.update(("mode", "page"))
        else:
            raise ValueError("invalid_request")
        if (
            not isinstance(request, dict)
            or set(request) != fields
            or not isinstance(request["source"], str)
            or not Path(request["source"]).is_absolute()
            or any(
                type(request[name]) is not int or request[name] < 1
                for name in fields - {"source", "mode"}
            )
        ):
            raise ValueError("invalid_request")
        response = _extract(request)
    except Exception as exc:
        # Never return filenames, document contents, or native parser errors.
        code = (
            str(exc)
            if isinstance(exc, ValueError)
            and str(exc)
            in {"document_limit_exceeded", "page_limit_exceeded", "page_out_of_range"}
            else "text_extraction_failed"
        )
        response = {"ok": False, "error": code}
    print(json.dumps(response, separators=(",", ":"), allow_nan=False))


if __name__ == "__main__":
    main()
