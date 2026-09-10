from __future__ import annotations

import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pypdf import PdfReader, PdfWriter, Transformation
from pypdf._codecs.core_font_metrics import CORE_FONT_METRICS

__all__ = [
    "COMPOSITOR_VERSION",
    "PageComposeResult",
    "UnencodableTextError",
    "compose_searchable_pdf",
]

# Bump when the compositor's output for identical inputs changes, so callers can
# fold it into a cache key.
COMPOSITOR_VERSION = 1

# Base-14 glyphless font used for the invisible layer (Latin/Western).
_FONT_NAME = "Helvetica"
_HELVETICA_WIDTHS = CORE_FONT_METRICS[_FONT_NAME].character_widths
_DEFAULT_GLYPH_WIDTH = _HELVETICA_WIDTHS.get("default", 500)

# PDF text render mode 3 == invisible (neither fill nor stroke).
_RENDER_MODE_INVISIBLE = 3


class UnencodableTextError(ValueError):
    """Block text cannot be represented by the v1 Latin/Western base font.

    Raised only under ``on_unencodable="error"``. The default ``"skip"`` contract
    drops the offending block from the text layer and flags the page instead, so a
    CJK/RTL block never becomes silent mojibake (spec SS5.3, SS12).
    """


@dataclass(frozen=True)
class PageComposeResult:
    """Per-page outcome of compositing (feeds the recipe's degraded-page list).

    ``degraded`` is True whenever recognized text failed to reach the layer
    (skipped blocks) or the whole page overlay errored; an empty page (nothing to
    place) is NOT degraded — it is a faithful "nothing to search here" page.
    """

    index: int
    block_count: int  # input blocks carrying non-empty text
    painted_count: int  # blocks actually written into the layer
    skipped_count: int  # placeable blocks dropped (degenerate bbox / unencodable)
    degraded: bool
    reason: str | None  # "overlay_error" | "unencodable_text" | "degenerate_bbox"


def _page_blocks(page: object) -> list[Mapping]:
    """Accept a page as either a bare block list or the ops/ocr.py page mapping
    ``{"text": ..., "blocks": [...]}`` so the recipe can pass ``pages`` straight."""
    if isinstance(page, Mapping):
        blocks = page.get("blocks", [])
    else:
        blocks = page
    if not blocks:
        return []
    return [b for b in blocks if isinstance(b, Mapping)]


def _encode_pdf_text(text: str) -> str | None:
    """Escape a string for a PDF literal in WinAnsi/Latin-1. Returns None if any
    character is not representable in the v1 base font (the CJK/RTL contract)."""
    out: list[str] = []
    for ch in text:
        if ch in "\r\n\t":
            out.append(" ")
            continue
        if ord(ch) > 0xFF:
            return None
        if ch in "()\\":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _helvetica_width(text: str, font_size: float) -> float:
    """Natural advance width (pt) of ``text`` at ``font_size`` in unscaled
    Helvetica, from pypdf's bundled core-14 metrics (widths are per mille em)."""
    total = 0
    for ch in text:
        total += _HELVETICA_WIDTHS.get(ch, _DEFAULT_GLYPH_WIDTH)
    return total / 1000.0 * font_size


def _bbox_extent(bbox: object) -> tuple[float, float, float, float] | None:
    """Reduce a 4-corner pixel polygon to an axis-aligned (x0, y_top, x1, y_bot)
    in pixel space. Returns None for missing/degenerate geometry."""
    if not isinstance(bbox, (list, tuple)) or len(bbox) < 2:
        return None
    xs: list[float] = []
    ys: list[float] = []
    for pt in bbox:
        if not isinstance(pt, (list, tuple)) or len(pt) < 2:
            return None
        try:
            xs.append(float(pt[0]))
            ys.append(float(pt[1]))
        except (TypeError, ValueError):
            return None
    x0, x1 = min(xs), max(xs)
    y_top, y_bot = min(ys), max(ys)
    if x1 - x0 <= 0 or y_bot - y_top <= 0:
        return None
    return x0, y_top, x1, y_bot


def _build_overlay_content(
    blocks: Sequence[Mapping],
    dpi: int,
    page_h_pt: float,
    on_unencodable: str,
) -> tuple[bytes, int, int, str | None]:
    """Assemble the invisible text content stream for one page.

    Returns ``(content_bytes, painted, skipped, reason)``. ``page_h_pt`` is the
    UPRIGHT (displayed) page height used for the y-flip. Raises
    ``UnencodableTextError`` under ``on_unencodable="error"``.
    """
    scale = 72.0 / dpi
    parts: list[str] = []
    painted = 0
    skipped = 0
    reason: str | None = None
    for block in blocks:
        text = str(block.get("text", "") or "").strip()
        if not text:
            continue
        extent = _bbox_extent(block.get("bbox"))
        if extent is None:
            skipped += 1
            reason = reason or "degenerate_bbox"
            continue
        x0_px, y_top_px, x1_px, y_bot_px = extent
        encoded = _encode_pdf_text(text)
        if encoded is None:
            if on_unencodable == "error":
                raise UnencodableTextError(
                    f"block text is not representable in the v1 base font "
                    f"({_FONT_NAME}/WinAnsi): {text!r}"
                )
            skipped += 1
            reason = "unencodable_text"
            continue
        x0_pt = x0_px * scale
        box_w_pt = (x1_px - x0_px) * scale
        # font size = box height in points; baseline = box bottom, y-flipped.
        font_size = (y_bot_px - y_top_px) * scale
        y_baseline_pt = page_h_pt - (y_bot_px * scale)
        # horizontal scaling (Tz) so the glyph run's advance matches the box width
        # → selection rectangles line up.
        natural = _helvetica_width(encoded, font_size)
        tz = (box_w_pt / natural * 100.0) if natural > 0 else 100.0
        parts.append(
            f"BT\n/F0 {font_size:.3f} Tf\n{_RENDER_MODE_INVISIBLE} Tr\n"
            f"{tz:.3f} Tz\n{x0_pt:.3f} {y_baseline_pt:.3f} Td\n({encoded}) Tj\nET\n"
        )
        painted += 1
    if skipped and reason != "unencodable_text":
        reason = "degenerate_bbox"
    return (
        "".join(parts).encode("latin-1"),
        painted,
        skipped,
        (reason if skipped else None),
    )


def _overlay_pdf(content: bytes, width: float, height: float) -> bytes:
    """A minimal single-page PDF carrying the content stream + the base-14 font,
    hand-authored so nothing but pypdf's public read/merge API is needed."""
    objs = [
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n",
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n",
        (
            "3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 %s %s]"
            "/Contents 4 0 R/Resources<</Font<</F0 5 0 R>>>>>>endobj\n"
            % (_fmt(width), _fmt(height))
        ).encode("latin-1"),
        b"4 0 obj<</Length "
        + str(len(content)).encode("latin-1")
        + b">>stream\n"
        + content
        + b"\nendstream\nendobj\n",
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/"
        + _FONT_NAME.encode("latin-1")
        + b"/Encoding/WinAnsiEncoding>>endobj\n",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for obj in objs:
        offsets.append(len(out))
        out += obj
    xref = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n"
    for off in offsets:
        out += ("%010d 00000 n \n" % off).encode("latin-1")
    out += (
        b"trailer<</Size 6/Root 1 0 R>>\nstartxref\n"
        + str(xref).encode("latin-1")
        + b"\n%%EOF"
    )
    return bytes(out)


def _fmt(value: float) -> str:
    return ("%.4f" % value).rstrip("0").rstrip(".") or "0"


def _page_transform(rotate: int, w_pt: float, h_pt: float) -> Transformation:
    """Map overlay coordinates authored in UPRIGHT/displayed space back into the
    page's unrotated content (MediaBox) space for ``/Rotate`` pages (spec SS4)."""
    rotate %= 360
    if rotate == 90:
        return Transformation().rotate(90).translate(tx=w_pt, ty=0)
    if rotate == 180:
        return Transformation().rotate(180).translate(tx=w_pt, ty=h_pt)
    if rotate == 270:
        return Transformation().rotate(270).translate(tx=0, ty=h_pt)
    return Transformation()


def compose_searchable_pdf(
    source_pdf: bytes,
    pages: Sequence[object],
    *,
    dpi: int,
    on_unencodable: str = "skip",
) -> tuple[bytes, list[PageComposeResult]]:
    """Sandwich an invisible OCR text layer onto ``source_pdf``.

    Args:
        source_pdf: the ORIGINAL PDF bytes the blocks were OCR'd from.
        pages: per-page blocks, one entry per source page (a bare block list, or
            the ``{"text": ..., "blocks": [...]}`` mapping). Extra source pages
            beyond ``len(pages)`` simply receive an empty text layer.
        dpi: the render DPI the block pixel bboxes are in (the pdftoppm ``-r``).
        on_unencodable: ``"skip"`` (default) drops CJK/RTL blocks and flags the
            page; ``"error"`` raises ``UnencodableTextError``.

    Returns:
        ``(pdf_bytes, results)`` where ``results[i]`` describes page ``i``. A
        per-page overlay failure degrades only that page (original page kept,
        no layer); a whole-PDF read/write failure propagates.
    """
    if dpi <= 0:
        raise ValueError(f"dpi must be positive, got {dpi!r}")
    if on_unencodable not in ("skip", "error"):
        raise ValueError(
            f"on_unencodable must be 'skip' or 'error', got {on_unencodable!r}"
        )

    reader = PdfReader(io.BytesIO(source_pdf))
    writer = PdfWriter()
    results: list[PageComposeResult] = []

    for index, page in enumerate(reader.pages):
        blocks = _page_blocks(pages[index]) if index < len(pages) else []
        block_count = sum(1 for b in blocks if str(b.get("text", "") or "").strip())

        mediabox = page.mediabox
        w_pt = float(mediabox.width)
        h_pt = float(mediabox.height)
        rotate = int(page.get("/Rotate", 0) or 0) % 360
        # pdftoppm uprights the raster: displayed dims swap for 90/270.
        disp_w, disp_h = (h_pt, w_pt) if rotate in (90, 270) else (w_pt, h_pt)

        painted = 0
        skipped = 0
        reason: str | None = None
        overlay_error = False

        # attach to the writer first, then merge onto the writer's own page — the
        # only reliable merge target in pypdf >=6 (merging onto an unattached
        # reader page is deprecated for removal in 7.0).
        writer_page = writer.add_page(page)

        if block_count:
            try:
                content, painted, skipped, reason = _build_overlay_content(
                    blocks, dpi, disp_h, on_unencodable
                )
                if painted:
                    overlay_page = PdfReader(
                        io.BytesIO(_overlay_pdf(content, disp_w, disp_h))
                    ).pages[0]
                    writer_page.merge_transformed_page(
                        overlay_page, _page_transform(rotate, w_pt, h_pt)
                    )
            except UnencodableTextError:
                raise
            except Exception:
                # per-page overlay failure: keep the original page, no text layer,
                # flag it, and carry on so one page never sinks the whole PDF.
                overlay_error = True
                painted = 0
                skipped = block_count
                reason = "overlay_error"
        degraded = overlay_error or skipped > 0
        results.append(
            PageComposeResult(
                index=index,
                block_count=block_count,
                painted_count=painted,
                skipped_count=skipped,
                degraded=degraded,
                reason=reason if degraded else None,
            )
        )

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue(), results
