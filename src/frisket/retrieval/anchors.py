"""Resolve selected current sources before similarity encoding.

The anchor is the user's chosen source, independently of which candidates are
already indexed. This boundary does not substitute a nearby indexed passage.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frisket.engine.executor.action_inventory import BoundLocalFile
from frisket.engine.pdf_render import (
    PdfRenderCancelled,
    PdfRenderError,
    render_pdf_pages,
)
from frisket.engine.sandbox.media_sync import run_media_sync
from frisket.engine.store.blob_backend import BlobStoreError

from .content import PreparedDocumentSurface, TextChunkRecipe
from .contracts import (
    CellAnchor,
    PageRange,
    ScopedBinding,
    SourceAnchor,
    SourceLocator,
    TextRange,
    TimeRange,
)
from .encoding import (
    EMBEDDINGGEMMA2_MAX_ENCODED_IMAGE_BYTES,
    EncodingInput,
    ImageEncodingInput,
    TextEncodingInput,
)
from .media import MediaProductionError, decode_audio_interval, decode_video_frame
from .query import RetrievalQueryError
from .source_adapter import (
    ResolvedCellBlobSource,
    resolve_cell_blob_source,
    resolve_cell_text_sources,
)
from .sources import cell_source_binding_id


CancelCheck = Callable[[], bool]
_MAX_TEXT_ANCHOR_CODEPOINTS = TextChunkRecipe().max_codepoints


@dataclass(frozen=True, slots=True)
class ResolvedAnchor:
    anchor: SourceAnchor
    input: EncodingInput | None = None
    space_id: str | None = None


@dataclass(frozen=True, slots=True)
class _BlobAnchor:
    resolved: ResolvedCellBlobSource
    selected: CellAnchor


def _binding(selected: CellAnchor, dependency, *, source_id: str) -> ScopedBinding:
    return ScopedBinding(
        binding_id=cell_source_binding_id(
            sheet_id=selected.sheet_id,
            row_id=selected.row_id,
            column_id=selected.column_id,
            source_id=source_id,
        ),
        sheet_id=selected.sheet_id,
        row_id=selected.row_id,
        column_id=selected.column_id,
        dependencies=(dependency,),
    )


def _text_anchor(project: Any, selected: CellAnchor) -> ResolvedAnchor | None:
    with project.read_snapshot() as snapshot:
        resolved = resolve_cell_text_sources(
            snapshot,
            sheet_id=selected.sheet_id,
            column_id=selected.column_id,
            row_ids=[selected.row_id],
        ).get(selected.row_id)
    if resolved is None:
        return None
    surface = resolved.surface
    text = surface.text if isinstance(surface, PreparedDocumentSurface) else surface
    locator = selected.locator
    if locator is None:
        if len(text.text) > _MAX_TEXT_ANCHOR_CODEPOINTS:
            raise RetrievalQueryError(
                "source_location_required",
                "Select a passage of at most "
                f"{_MAX_TEXT_ANCHOR_CODEPOINTS} characters before searching for "
                "similar content.",
            )
        locator = SourceLocator(
            kind="text",
            text=TextRange(text.text_surface_id, text.content_hash, 0, len(text.text)),
        )
    if locator.text is None or (
        locator.text.text_surface_id != text.text_surface_id
        or locator.text.content_hash != text.content_hash
        or locator.text.end > len(text.text)
    ):
        raise RetrievalQueryError(
            "source_anchor_changed", "The selected text has changed. Select it again."
        )
    value = text.text[locator.text.start : locator.text.end]
    if not value or len(value) > _MAX_TEXT_ANCHOR_CODEPOINTS:
        raise RetrievalQueryError(
            "invalid_anchor",
            "Text similarity requires a non-empty passage of at most "
            f"{_MAX_TEXT_ANCHOR_CODEPOINTS} characters.",
        )
    return ResolvedAnchor(
        SourceAnchor(
            resolved.source,
            locator,
            _binding(selected, resolved.binding, source_id=resolved.source.source_id),
            selected.exclude_exact,
        ),
        TextEncodingInput(value),
    )


def _resolve_authority(
    project: Any, selected: CellAnchor, named_index_id: str | None
) -> ResolvedAnchor | _BlobAnchor:
    if named_index_id is not None:
        from frisket.ai.embeddings.unified_index import (
            NamedIndexAnchorError,
            resolve_named_cell_anchor,
        )

        if selected.locator is not None:
            raise RetrievalQueryError(
                "invalid_anchor", "Choose a row for similarity within this index."
            )
        try:
            resolved = resolve_named_cell_anchor(
                project,
                index_id=named_index_id,
                sheet_id=selected.sheet_id,
                row_id=selected.row_id,
                column_id=selected.column_id,
            )
        except NamedIndexAnchorError as exc:
            raise RetrievalQueryError(exc.code, str(exc)) from exc
        return ResolvedAnchor(resolved.anchor, space_id=resolved.space_id)

    text = _text_anchor(project, selected)
    if text is not None:
        return text
    with project.read_snapshot() as snapshot:
        blob = resolve_cell_blob_source(
            snapshot,
            sheet_id=selected.sheet_id,
            row_id=selected.row_id,
            column_id=selected.column_id,
        )
    if blob is None:
        raise RetrievalQueryError(
            "source_anchor_unavailable",
            "The selected source is not available for similarity search.",
        )
    return _BlobAnchor(blob, selected)


def _single_pdf_page(anchor: _BlobAnchor) -> int:
    locator = anchor.selected.locator
    authored = anchor.resolved.page_number
    if locator is None:
        if authored is None:
            raise RetrievalQueryError(
                "source_location_required",
                "Choose a PDF page before searching for similar content.",
            )
        return authored
    if (
        locator.pages is None
        or locator.pages.start != locator.pages.end
        or locator.text is not None
        or locator.time is not None
        or locator.source_span_ids
    ):
        raise RetrievalQueryError(
            "invalid_anchor", "PDF similarity requires one exact selected page."
        )
    page = locator.pages.start
    if authored is not None and authored != page:
        raise RetrievalQueryError(
            "source_anchor_changed",
            "The selected PDF page has changed. Select it again.",
        )
    return page


def _video_time(anchor: _BlobAnchor) -> float:
    locator = anchor.selected.locator
    if locator is None or locator.time is None:
        raise RetrievalQueryError(
            "source_location_required",
            "Choose a video time before searching for similar content.",
        )
    if (
        locator.time.duration_seconds != 0
        or locator.text is not None
        or locator.pages is not None
        or locator.source_span_ids
    ):
        raise RetrievalQueryError(
            "invalid_anchor", "Video similarity requires one exact selected time."
        )
    return locator.time.start_seconds


def _audio_time(anchor: _BlobAnchor) -> TimeRange:
    locator = anchor.selected.locator
    if locator is None or locator.time is None:
        raise RetrievalQueryError(
            "source_location_required",
            "Choose an audio interval before searching for similar content.",
        )
    if (
        not 0 < locator.time.duration_seconds <= 30
        or locator.text is not None
        or locator.pages is not None
        or locator.source_span_ids
    ):
        raise RetrievalQueryError(
            "invalid_anchor",
            "Audio similarity requires an interval of at most 30 seconds.",
        )
    return locator.time


def _whole_image_locator(anchor: _BlobAnchor) -> SourceLocator:
    locator = anchor.selected.locator
    if locator is not None and (
        locator.text is not None
        or locator.pages is not None
        or locator.time is not None
        or locator.source_span_ids
    ):
        raise RetrievalQueryError(
            "invalid_anchor", "Image similarity requires the whole selected image."
        )
    return SourceLocator(kind="image")


def _read_image(path: Path) -> ImageEncodingInput:
    try:
        size = path.stat().st_size
        if size < 1 or size > EMBEDDINGGEMMA2_MAX_ENCODED_IMAGE_BYTES:
            raise RetrievalQueryError(
                "source_anchor_unavailable",
                "The selected image exceeds the supported encoded size.",
            )
        value = path.read_bytes()
    except OSError as exc:
        raise RetrievalQueryError(
            "source_anchor_unavailable", "The selected image bytes are unavailable."
        ) from exc
    return ImageEncodingInput(value)


async def _decode_blob_anchor(
    anchor: _BlobAnchor,
    path: Path,
    *,
    cancelled: CancelCheck | None,
) -> ResolvedAnchor:
    if cancelled is not None and cancelled():
        raise asyncio.CancelledError
    resolved, selected = anchor.resolved, anchor.selected
    mime = resolved.mime_type.lower().split(";", 1)[0].strip()
    binding = _binding(selected, resolved.binding, source_id=resolved.source.source_id)
    try:
        if mime.startswith("image/"):
            locator = _whole_image_locator(anchor)
            value: EncodingInput = _read_image(path)
        elif mime == "application/pdf":
            page = _single_pdf_page(anchor)
            with tempfile.TemporaryDirectory(
                prefix="frisket-retrieval-pdf-anchor-"
            ) as directory:
                rendered = await render_pdf_pages(
                    path,
                    Path(directory),
                    dpi=144,
                    pages=[page],
                    max_edge=1600,
                    should_cancel=cancelled,
                )
                if len(rendered.pages) != 1 or rendered.pages[0][0] != page:
                    raise PdfRenderError("PDF renderer returned the wrong page")
                value = _read_image(rendered.pages[0][1])
            locator = SourceLocator(kind="pdf_page", pages=PageRange(page, page))
        elif mime.startswith("video/"):
            at_seconds = _video_time(anchor)
            with path.open("rb") as stream:
                frame = await decode_video_frame(
                    BoundLocalFile(
                        stream=stream, sha256=f"sha256:{resolved.blob_hash}"
                    ),
                    at_seconds,
                    cancelled=cancelled,
                )
            value = frame.input
            locator = SourceLocator(
                kind="video_frame",
                time=TimeRange(frame.point.seconds, frame.point.seconds),
            )
        elif mime.startswith("audio/"):
            requested = _audio_time(anchor)
            with path.open("rb") as stream:
                window = await decode_audio_interval(
                    BoundLocalFile(
                        stream=stream, sha256=f"sha256:{resolved.blob_hash}"
                    ),
                    requested.start_seconds,
                    requested.end_seconds,
                    cancelled=cancelled,
                )
            value = window.input
            locator = SourceLocator(
                kind="audio_window",
                time=TimeRange(window.start_seconds, window.end_seconds),
            )
        else:
            raise RetrievalQueryError(
                "source_anchor_unavailable",
                "The selected file type cannot be encoded for similarity search.",
            )
    except PdfRenderCancelled as exc:
        raise asyncio.CancelledError from exc
    except (MediaProductionError, PdfRenderError) as exc:
        code = getattr(exc, "code", "source_anchor_unavailable")
        raise RetrievalQueryError(code, str(exc)) from exc
    return ResolvedAnchor(
        SourceAnchor(
            resolved.source,
            locator,
            binding,
            selected.exclude_exact,
        ),
        value,
    )


async def resolve_cell_anchor_async(
    project: Any,
    selected: CellAnchor,
    *,
    named_index_id: str | None = None,
    cancelled: CancelCheck | None = None,
) -> ResolvedAnchor:
    """Resolve and decode one exact selected current source."""

    authority = _resolve_authority(project, selected, named_index_id)
    if isinstance(authority, ResolvedAnchor):
        return authority
    try:
        with project.materialize_blob(authority.resolved.blob_hash) as path:
            return await _decode_blob_anchor(authority, Path(path), cancelled=cancelled)
    except BlobStoreError as exc:
        raise RetrievalQueryError(
            "source_anchor_unavailable",
            "The selected source bytes are unavailable.",
        ) from exc


def resolve_cell_anchor(
    project: Any,
    selected: CellAnchor,
    *,
    named_index_id: str | None = None,
    cancelled: CancelCheck | None = None,
) -> ResolvedAnchor:
    """Synchronous resolver for search services and non-async callers."""

    authority = _resolve_authority(project, selected, named_index_id)
    if isinstance(authority, ResolvedAnchor):
        return authority
    try:
        with project.materialize_blob(authority.resolved.blob_hash) as path:
            return run_media_sync(
                lambda should_cancel: _decode_blob_anchor(
                    authority, Path(path), cancelled=should_cancel
                ),
                cancelled=cancelled,
            )
    except BlobStoreError as exc:
        raise RetrievalQueryError(
            "source_anchor_unavailable",
            "The selected source bytes are unavailable.",
        ) from exc


__all__ = ["ResolvedAnchor", "resolve_cell_anchor", "resolve_cell_anchor_async"]
