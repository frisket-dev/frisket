"""Bounded producers for immutable text, prepared-document, transcript, and image sources.

Authority resolution stays outside this module.  Callers pass an immutable
``SourceRevision`` and the exact source surface selected for that revision.
The producer only divides that surface into located encoder inputs; it never
chooses a current cell, artifact, OCR result, or transcript generation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal, TypeAlias

from frisket.retrieval.contracts import (
    ChannelCoverage,
    IndexedPart,
    PageRange,
    SourceLocator,
    SourceRevision,
    TextRange,
    TimeRange,
)
from frisket.retrieval.encoding import (
    EncodingInput,
    ImageEncodingInput,
    TextEncodingInput,
)


CancelCheck: TypeAlias = Callable[[], bool]
IMAGE_BYTES_RECIPE_ID = "image-bytes-v1"


def _nonblank(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _positive_integer(value: object, name: str) -> int:
    return _integer(value, name, minimum=1)


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _verify_content_hash(value: bytes, content_hash: object, name: str) -> str:
    checked = _nonblank(content_hash, name)
    if checked != _sha256(value):
        raise ValueError(f"{name} does not match the resolved content")
    return checked


def _source_payload(source: SourceRevision) -> dict[str, object]:
    return {
        "source_id": source.source_id,
        "source_kind": source.source_kind,
        "version_kind": source.version_kind,
        "version_value": source.version_value,
        "content_hash": source.content_hash,
    }


def _identity_token(*values: object) -> str:
    encoded = json.dumps(
        values,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_identity(source: SourceRevision, *surface_values: object) -> str:
    return _identity_token(_source_payload(source), *surface_values)


def _check_source(source: object) -> SourceRevision:
    if not isinstance(source, SourceRevision):
        raise TypeError("source must be a SourceRevision")
    return source


def _check_cancelled(cancelled: CancelCheck | None) -> None:
    if cancelled is not None and cancelled():
        raise asyncio.CancelledError


def _check_max_parts(max_parts: object) -> int:
    return _positive_integer(max_parts, "max_parts")


def _span_ids(values: object) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise TypeError("source_span_ids must be a tuple")
    checked = tuple(_nonblank(value, "source_span_id") for value in values)
    if len(set(checked)) != len(checked):
        raise ValueError("source_span_ids must not contain duplicates")
    return checked


@dataclass(frozen=True, slots=True)
class TextSurface:
    """One exact, content-addressed Unicode text surface."""

    source: SourceRevision
    text_surface_id: str
    content_hash: str
    text: str
    source_span_ids: tuple[str, ...] = ()
    _identity: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        source = _check_source(self.source)
        surface_id = _nonblank(self.text_surface_id, "text_surface_id")
        if not isinstance(self.text, str):
            raise TypeError("text must be a string")
        content_hash = _verify_content_hash(
            self.text.encode("utf-8"), self.content_hash, "content_hash"
        )
        spans = _span_ids(self.source_span_ids)
        object.__setattr__(self, "source_span_ids", spans)
        object.__setattr__(
            self,
            "_identity",
            _source_identity(source, surface_id, content_hash)
            if not spans
            else _source_identity(source, surface_id, content_hash, spans),
        )


@dataclass(frozen=True, slots=True)
class PreparedPageExtent:
    """An immutable prepared page's exact range in its resolved text surface."""

    page_number: int
    version_id: int
    content_hash: str
    start: int
    end: int

    def __post_init__(self) -> None:
        _positive_integer(self.page_number, "page_number")
        _positive_integer(self.version_id, "version_id")
        _nonblank(self.content_hash, "content_hash")
        start = _integer(self.start, "start")
        end = _integer(self.end, "end")
        if end < start:
            raise ValueError("prepared page end must not be before its start")


@dataclass(frozen=True, slots=True)
class PreparedDocumentSurface:
    """Existing prepared text plus its authoritative page-version extents."""

    text: TextSurface
    pages: tuple[PreparedPageExtent, ...]
    _identity: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.text, TextSurface):
            raise TypeError("text must be a TextSurface")
        if not isinstance(self.pages, tuple) or not self.pages:
            raise ValueError("pages must be a non-empty tuple")
        if not all(isinstance(page, PreparedPageExtent) for page in self.pages):
            raise TypeError("pages must contain PreparedPageExtent values")

        prior_page = 0
        prior_end = 0
        identity_pages: list[tuple[object, ...]] = []
        for page in self.pages:
            if page.page_number <= prior_page:
                raise ValueError("prepared pages must be ordered without duplicates")
            if page.start < prior_end or page.end > len(self.text.text):
                raise ValueError("prepared page extents must be ordered within text")
            _verify_content_hash(
                self.text.text[page.start : page.end].encode("utf-8"),
                page.content_hash,
                "page content_hash",
            )
            if self.text.text[prior_end : page.start].strip():
                raise ValueError(
                    "text outside prepared page extents must be whitespace"
                )
            prior_page = page.page_number
            prior_end = page.end
            identity_pages.append(
                (
                    page.page_number,
                    page.version_id,
                    page.content_hash,
                    page.start,
                    page.end,
                )
            )
        if self.text.text[prior_end:].strip():
            raise ValueError("text outside prepared page extents must be whitespace")
        object.__setattr__(
            self,
            "_identity",
            _identity_token(self.text._identity, identity_pages),
        )


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    """One authority-bound transcript source unit on the original timeline."""

    source_span_id: str
    start_ms: int
    end_ms: int
    text: str
    segment_index: int

    def __post_init__(self) -> None:
        _nonblank(self.source_span_id, "source_span_id")
        start = _integer(self.start_ms, "start_ms")
        end = _integer(self.end_ms, "end_ms")
        if end < start:
            raise ValueError("transcript segment end must not be before its start")
        if not isinstance(self.text, str):
            raise TypeError("transcript segment text must be a string")
        _integer(self.segment_index, "segment_index")


@dataclass(frozen=True, slots=True)
class TranscriptSurface:
    """An ordered immutable transcript snapshot already bound to its source."""

    source: SourceRevision
    segments: tuple[TranscriptSegment, ...]
    _identity: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        source = _check_source(self.source)
        if not isinstance(self.segments, tuple):
            raise TypeError("segments must be a tuple")
        if not all(isinstance(item, TranscriptSegment) for item in self.segments):
            raise TypeError("segments must contain TranscriptSegment values")
        prior_index = -1
        prior_start = -1
        identities: list[tuple[object, ...]] = []
        for segment in self.segments:
            if segment.segment_index <= prior_index:
                raise ValueError("transcript segment indexes must be strictly ordered")
            if segment.start_ms < prior_start:
                raise ValueError("transcript segment times must be ordered")
            prior_index = segment.segment_index
            prior_start = segment.start_ms
            identities.append(
                (
                    segment.source_span_id,
                    segment.start_ms,
                    segment.end_ms,
                    segment.text,
                    segment.segment_index,
                )
            )
        object.__setattr__(
            self,
            "_identity",
            _source_identity(source, identities),
        )


@dataclass(frozen=True, slots=True)
class ImageSurface:
    """Verified encoded image bytes selected by the authority layer."""

    source: SourceRevision
    image_bytes: bytes
    content_hash: str
    source_span_ids: tuple[str, ...] = ()
    pages: PageRange | None = None
    _identity: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        source = _check_source(self.source)
        if not isinstance(self.image_bytes, bytes):
            raise TypeError("image_bytes must be bytes")
        content_hash = _verify_content_hash(
            self.image_bytes, self.content_hash, "content_hash"
        )
        spans = _span_ids(self.source_span_ids)
        if self.pages is not None and not isinstance(self.pages, PageRange):
            raise TypeError("pages must be a PageRange or None")
        ImageEncodingInput(self.image_bytes)
        object.__setattr__(self, "source_span_ids", spans)
        object.__setattr__(
            self,
            "_identity",
            _source_identity(source, content_hash, spans)
            if self.pages is None
            else _source_identity(
                source,
                content_hash,
                spans,
                (self.pages.start, self.pages.end),
            ),
        )


@dataclass(frozen=True, slots=True)
class TextChunkRecipe:
    max_codepoints: int = 2048
    overlap_codepoints: int = 256
    strategy: Literal["text_codepoint_windows"] = "text_codepoint_windows"

    def __post_init__(self) -> None:
        maximum = _positive_integer(self.max_codepoints, "max_codepoints")
        overlap = _integer(self.overlap_codepoints, "overlap_codepoints")
        if overlap >= maximum:
            raise ValueError("overlap_codepoints must be less than max_codepoints")

    @property
    def recipe_id(self) -> str:
        return (
            "text-codepoint-windows-v1:"
            f"max={self.max_codepoints}:overlap={self.overlap_codepoints}"
        )


@dataclass(frozen=True, slots=True)
class TranscriptChunkRecipe:
    max_codepoints: int = 2048
    overlap_segments: int = 1
    strategy: Literal["transcript_segment_windows"] = "transcript_segment_windows"

    def __post_init__(self) -> None:
        _positive_integer(self.max_codepoints, "max_codepoints")
        _integer(self.overlap_segments, "overlap_segments")

    @property
    def recipe_id(self) -> str:
        return (
            "transcript-segment-windows-v1:"
            f"max={self.max_codepoints}:overlap={self.overlap_segments}"
        )


@dataclass(frozen=True, slots=True)
class TextCursor:
    source_identity: str
    recipe_id: str
    next_offset: int


@dataclass(frozen=True, slots=True)
class TranscriptCursor:
    source_identity: str
    recipe_id: str
    next_segment: int
    next_offset: int = 0


ProductionCursor: TypeAlias = TextCursor | TranscriptCursor


@dataclass(frozen=True, slots=True)
class ProducedPart:
    part: IndexedPart
    input: EncodingInput


@dataclass(frozen=True, slots=True)
class ProducedBatch:
    items: tuple[ProducedPart, ...]
    coverage: ChannelCoverage
    continuation: ProductionCursor | None = None


def _locator_payload(locator: SourceLocator) -> dict[str, object]:
    text = locator.text
    pages = locator.pages
    time = locator.time
    return {
        "kind": locator.kind,
        "text": (
            None
            if text is None
            else {
                "text_surface_id": text.text_surface_id,
                "content_hash": text.content_hash,
                "start": text.start,
                "end": text.end,
                "offset_unit": text.offset_unit,
            }
        ),
        "pages": (None if pages is None else {"start": pages.start, "end": pages.end}),
        "time": (
            None
            if time is None
            else {
                "start_seconds": time.start_seconds,
                "end_seconds": time.end_seconds,
            }
        ),
        "source_span_ids": locator.source_span_ids,
    }


def _part(
    *,
    source: SourceRevision,
    recipe_id: str,
    part_kind: str,
    locator: SourceLocator,
    input: EncodingInput,
    identity_salt: object | None = None,
) -> ProducedPart:
    if isinstance(input, TextEncodingInput):
        input_hash = _sha256(input.text.encode("utf-8"))
    elif isinstance(input, ImageEncodingInput):
        input_hash = _sha256(input.image_bytes)
    else:  # This producer currently emits text and image inputs only.
        raise TypeError("unsupported content encoding input")
    identity = _identity_token(
        _source_payload(source),
        recipe_id,
        part_kind,
        _locator_payload(locator),
        input_hash,
        identity_salt,
    )
    return ProducedPart(
        part=IndexedPart(
            part_id=f"part:{identity}",
            source=source,
            producer_recipe_id=recipe_id,
            part_kind=part_kind,
            locator=locator,
        ),
        input=input,
    )


def _coverage(
    *, channel: str, recipe_id: str, count: int, continuation: object | None
) -> ChannelCoverage:
    return ChannelCoverage(
        channel=channel,
        state="pending" if continuation is not None else "ready",
        recipe_id=recipe_id,
        prepared_count=count,
        reason="continuation" if continuation is not None else None,
    )


def _text_window(
    text: str, start: int, maximum: int, overlap: int
) -> tuple[int, int | None]:
    hard_end = min(start + maximum, len(text))
    end = _preferred_text_end(text, start, hard_end, maximum, overlap)
    if end == len(text):
        return end, None
    return end, end - overlap


def _preferred_text_end(
    text: str, start: int, hard_end: int, maximum: int, overlap: int
) -> int:
    """Prefer a nearby structural boundary without exceeding the hard limit."""

    if hard_end == len(text):
        return hard_end
    lower = max(start + maximum // 2, start + overlap + 1)
    if lower >= hard_end:
        return hard_end

    paragraph = text.rfind("\n\n", lower, hard_end)
    if paragraph >= 0:
        return paragraph + 2
    newline = text.rfind("\n", lower, hard_end)
    if newline >= 0:
        return newline + 1
    for position in range(hard_end - 1, lower - 1, -1):
        if text[position] in ".!?" and (
            position + 1 == len(text) or text[position + 1].isspace()
        ):
            return position + 1
    for position in range(hard_end - 1, lower - 1, -1):
        if text[position].isspace():
            return position + 1
    return hard_end


def _page_range(pages: Sequence[PreparedPageExtent], start: int, end: int) -> PageRange:
    touched = [
        page.page_number for page in pages if page.start < end and start < page.end
    ]
    if not touched:
        raise ValueError("nonblank prepared text must intersect an authoritative page")
    return PageRange(start=touched[0], end=touched[-1])


def _text_batch(
    surface: TextSurface,
    recipe: TextChunkRecipe,
    *,
    max_parts: int,
    cursor: TextCursor | None,
    cancelled: CancelCheck | None,
    part_kind: str,
    pages: Sequence[PreparedPageExtent] | None,
    identity: str,
) -> ProducedBatch:
    limit = _check_max_parts(max_parts)
    if not isinstance(recipe, TextChunkRecipe):
        raise TypeError("recipe must be a TextChunkRecipe")
    offset = 0
    if cursor is not None:
        if not isinstance(cursor, TextCursor):
            raise TypeError("cursor must be a TextCursor")
        if cursor.source_identity != identity or cursor.recipe_id != recipe.recipe_id:
            raise ValueError("cursor does not belong to this source and recipe")
        offset = _integer(cursor.next_offset, "cursor next_offset")
        if offset >= len(surface.text):
            raise ValueError("cursor next_offset must be inside the text surface")

    items: list[ProducedPart] = []
    next_offset: int | None = offset if offset < len(surface.text) else None
    while next_offset is not None and len(items) < limit:
        _check_cancelled(cancelled)
        start = next_offset
        end, next_offset = _text_window(
            surface.text,
            start,
            recipe.max_codepoints,
            recipe.overlap_codepoints,
        )
        value = surface.text[start:end]
        if not value.strip():
            continue
        page_range = None if pages is None else _page_range(pages, start, end)
        locator = SourceLocator(
            kind=part_kind,
            text=TextRange(
                text_surface_id=surface.text_surface_id,
                content_hash=surface.content_hash,
                start=start,
                end=end,
            ),
            pages=page_range,
            source_span_ids=surface.source_span_ids,
        )
        items.append(
            _part(
                source=surface.source,
                recipe_id=recipe.recipe_id,
                part_kind=part_kind,
                locator=locator,
                input=TextEncodingInput(value),
            )
        )

    continuation = (
        None
        if next_offset is None
        else TextCursor(
            source_identity=identity,
            recipe_id=recipe.recipe_id,
            next_offset=next_offset,
        )
    )
    return ProducedBatch(
        items=tuple(items),
        coverage=_coverage(
            channel="text",
            recipe_id=recipe.recipe_id,
            count=len(items),
            continuation=continuation,
        ),
        continuation=continuation,
    )


def produce_text(
    surface: TextSurface,
    recipe: TextChunkRecipe,
    *,
    max_parts: int,
    cursor: TextCursor | None = None,
    cancelled: CancelCheck | None = None,
) -> ProducedBatch:
    """Produce one bounded batch of exactly located text windows."""

    if not isinstance(surface, TextSurface):
        raise TypeError("surface must be a TextSurface")
    return _text_batch(
        surface,
        recipe,
        max_parts=max_parts,
        cursor=cursor,
        cancelled=cancelled,
        part_kind="text_chunk",
        pages=None,
        identity=_identity_token(surface._identity, "text_chunk"),
    )


def produce_prepared_document(
    surface: PreparedDocumentSurface,
    recipe: TextChunkRecipe,
    *,
    max_parts: int,
    cursor: TextCursor | None = None,
    cancelled: CancelCheck | None = None,
) -> ProducedBatch:
    """Chunk an existing prepared surface and preserve its page memberships."""

    if not isinstance(surface, PreparedDocumentSurface):
        raise TypeError("surface must be a PreparedDocumentSurface")
    return _text_batch(
        surface.text,
        recipe,
        max_parts=max_parts,
        cursor=cursor,
        cancelled=cancelled,
        part_kind="prepared_text_chunk",
        pages=surface.pages,
        identity=_identity_token(surface._identity, "prepared_text_chunk"),
    )


def _transcript_item(
    surface: TranscriptSurface,
    recipe: TranscriptChunkRecipe,
    segments: Sequence[TranscriptSegment],
    text: str,
    identity_salt: object,
) -> ProducedPart:
    start_ms = min(segment.start_ms for segment in segments)
    end_ms = max(segment.end_ms for segment in segments)
    locator = SourceLocator(
        kind="transcript_chunk",
        time=TimeRange(start_ms / 1000.0, end_ms / 1000.0),
        source_span_ids=tuple(segment.source_span_id for segment in segments),
    )
    return _part(
        source=surface.source,
        recipe_id=recipe.recipe_id,
        part_kind="transcript_chunk",
        locator=locator,
        input=TextEncodingInput(text),
        identity_salt=identity_salt,
    )


def produce_transcript(
    surface: TranscriptSurface,
    recipe: TranscriptChunkRecipe,
    *,
    max_parts: int,
    cursor: TranscriptCursor | None = None,
    cancelled: CancelCheck | None = None,
) -> ProducedBatch:
    """Pack whole transcript source units, splitting only oversized unit text.

    A split oversized unit retains the whole authoritative unit time and source
    span.  It therefore has source-unit location precision; no word timing or
    character-to-time mapping is invented.
    """

    if not isinstance(surface, TranscriptSurface):
        raise TypeError("surface must be a TranscriptSurface")
    if not isinstance(recipe, TranscriptChunkRecipe):
        raise TypeError("recipe must be a TranscriptChunkRecipe")
    limit = _check_max_parts(max_parts)
    index = 0
    offset = 0
    identity = _identity_token(surface._identity, "transcript_chunk")
    if cursor is not None:
        if not isinstance(cursor, TranscriptCursor):
            raise TypeError("cursor must be a TranscriptCursor")
        if cursor.source_identity != identity or cursor.recipe_id != recipe.recipe_id:
            raise ValueError("cursor does not belong to this source and recipe")
        index = _integer(cursor.next_segment, "cursor next_segment")
        offset = _integer(cursor.next_offset, "cursor next_offset")
        if index >= len(surface.segments):
            raise ValueError("cursor next_segment must be inside the transcript")
        if offset >= len(surface.segments[index].text) and offset != 0:
            raise ValueError("cursor next_offset must be inside its segment")

    items: list[ProducedPart] = []
    while index < len(surface.segments) and len(items) < limit:
        _check_cancelled(cancelled)
        segment = surface.segments[index]

        if offset or len(segment.text) > recipe.max_codepoints:
            fragment_start = offset
            end = min(offset + recipe.max_codepoints, len(segment.text))
            value = segment.text[offset:end]
            if end == len(segment.text):
                index += 1
                offset = 0
            else:
                offset = end
            if value.strip():
                items.append(
                    _transcript_item(
                        surface,
                        recipe,
                        (segment,),
                        value,
                        (
                            "segment_fragment",
                            segment.segment_index,
                            fragment_start,
                            end,
                        ),
                    )
                )
            continue

        selected: list[TranscriptSegment] = []
        size = 0
        scan = index
        while scan < len(surface.segments):
            candidate = surface.segments[scan]
            if not candidate.text.strip():
                scan += 1
                continue
            if len(candidate.text) > recipe.max_codepoints:
                break
            addition = len(candidate.text) + (1 if selected else 0)
            if selected and size + addition > recipe.max_codepoints:
                break
            selected.append(candidate)
            size += addition
            scan += 1

        if not selected:
            # Blank units may run to the end; an oversized nonblank unit is
            # handled by the split branch on the next iteration.
            index = scan
            continue

        value = "\n".join(item.text for item in selected)
        items.append(
            _transcript_item(
                surface,
                recipe,
                selected,
                value,
                ("segment_group", tuple(item.segment_index for item in selected)),
            )
        )
        if scan == len(surface.segments):
            index = scan
        else:
            index = max(index + 1, scan - recipe.overlap_segments)

    continuation = (
        None
        if index >= len(surface.segments)
        else TranscriptCursor(
            source_identity=identity,
            recipe_id=recipe.recipe_id,
            next_segment=index,
            next_offset=offset,
        )
    )
    return ProducedBatch(
        items=tuple(items),
        coverage=_coverage(
            channel="transcript",
            recipe_id=recipe.recipe_id,
            count=len(items),
            continuation=continuation,
        ),
        continuation=continuation,
    )


def produce_image(
    surface: ImageSurface,
    *,
    recipe_id: str = IMAGE_BYTES_RECIPE_ID,
    cancelled: CancelCheck | None = None,
) -> ProducedBatch:
    """Produce the selected encoded image bytes without decoding or rewriting."""

    if not isinstance(surface, ImageSurface):
        raise TypeError("surface must be an ImageSurface")
    _nonblank(recipe_id, "recipe_id")
    _check_cancelled(cancelled)
    input = ImageEncodingInput(surface.image_bytes)
    part_kind = "pdf_page_image" if surface.pages is not None else "image"
    item = _part(
        source=surface.source,
        recipe_id=recipe_id,
        part_kind=part_kind,
        locator=SourceLocator(
            kind=part_kind,
            pages=surface.pages,
            source_span_ids=surface.source_span_ids,
        ),
        input=input,
    )
    return ProducedBatch(
        items=(item,),
        coverage=_coverage(
            channel="image",
            recipe_id=recipe_id,
            count=1,
            continuation=None,
        ),
    )
