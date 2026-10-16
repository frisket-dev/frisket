"""Durable, bounded preparation of resolved retrieval sources.

Dirty-scope maintenance publishes lexical state and a compact producer cursor
before acknowledging source work.  The existing ``search.index`` worker then
drains those cursors.  Source text is never copied into the preparation row: each
quantum resolves an exact current binding again and conditional publication
rejects a source revision superseded by an edit.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from frisket.ai.embeddings.facade import EmbeddingFacade, embedding_input_hash
from frisket.engine.pdf_render import (
    PdfPageOutOfRange,
    PdfRenderCancelled,
    PdfRenderError,
    render_pdf_pages,
)
from frisket.engine.sandbox.media_sync import run_media_sync
from frisket.engine.store.blob_backend import BlobIntegrityError, BlobNotFoundError
from frisket.retrieval.content import (
    IMAGE_BYTES_RECIPE_ID,
    ImageSurface,
    PreparedDocumentSurface,
    ProducedPart,
    TextChunkRecipe,
    TextCursor,
    TextSurface,
    TranscriptChunkRecipe,
    TranscriptCursor,
    TranscriptSurface,
    produce_prepared_document,
    produce_image,
    produce_text,
    produce_transcript,
)
from frisket.retrieval.contracts import (
    IndexedPart,
    PageRange,
    SourceLocator,
    TextRange,
    TimeRange,
)
from frisket.retrieval.encoding import (
    EMBEDDINGGEMMA2_MAX_ENCODED_IMAGE_BYTES,
    EmbeddingGemma2Space,
    EncodingError,
    EncodingInput,
    EncodingPurpose,
)
from frisket.retrieval.native import pack_vector
from frisket.retrieval.preparation_policy import (
    DEFAULT_PREPARATION_POLICY,
    DEFAULT_TEXT_RECIPE,
    DEFAULT_TRANSCRIPT_RECIPE as DEFAULT_TRANSCRIPT_RECIPE,
    PREPARATION_POLICY_META_KEY,
    PreparationPolicy,
    parse_preparation_policy,
    preparation_policy_json,
    read_preparation_policy,
    write_preparation_policy,
)
from frisket.retrieval.media import (
    AudioWindow,
    FixedClipRecipe,
    LocatedFrame,
    LocatedVideoClip,
    MediaProductionError,
    PeriodicFrameRecipe,
    ShotClipRecipe,
    _borrow_materialized_media,
    produce_media,
)
from frisket.retrieval.source_adapter import (
    ResolvedCellBlobSource,
    ResolvedCellOcrTextSource,
    ResolvedCellTextSource,
    ResolvedCellTranscriptSource,
    resolve_cell_ocr_text_source,
    resolve_cell_blob_source,
    resolve_cell_text_sources,
    resolve_cell_transcript_source,
)
from frisket.retrieval.sources import (
    cell_source_binding_id,
    identity_digest,
    input_hash,
    representation_id,
)
from frisket.retrieval.store import (
    BindingDependency,
    CachedRepresentation,
    LexicalInput,
    PartRepresentation,
    PreparationWork,
    RetrievalStore,
    SourcePublication,
    StoredBinding,
    StoredPart,
)


LEXICAL_RECIPE_ID = "frisket.lexical.whole-source.v1"
LEXICAL_CHANNEL = "lexical"
TEXT_CHANNEL = "text"
TRANSCRIPT_CHANNEL = "transcript_text"
OCR_TEXT_CHANNEL = "ocr_text"
DEFAULT_TEXT_SPACE = EmbeddingGemma2Space()
IMAGE_CHANNEL = "image"
PDF_PAGE_CHANNEL = "pdf_page"
VIDEO_CHANNEL = "video"
AUDIO_CHANNEL = "audio"
UNSUPPORTED_MEDIA_CHANNEL = "media"
PDF_PAGE_RECIPE_ID = "pdf-page-png-v1:dpi=144:max-edge=1600"
UNSUPPORTED_MEDIA_RECIPE_ID = "unsupported-media-v1"
ENCODER_UNAVAILABLE_ERROR = "encoder_unavailable"
POLICY_DISABLED_ERROR = "policy_disabled"
UNSUPPORTED_MEDIA_ERROR = "unsupported_media"
DISABLED_TEXT_RECIPE_ID = "policy-disabled-text-v1"
DISABLED_TRANSCRIPT_RECIPE_ID = "policy-disabled-transcript-v1"
DISABLED_IMAGE_RECIPE_ID = "policy-disabled-image-v1"
DISABLED_PDF_RECIPE_ID = "policy-disabled-pdf-pages-v1"
DISABLED_VIDEO_RECIPE_ID = "policy-disabled-video-v1"
DISABLED_AUDIO_RECIPE_ID = "policy-disabled-audio-v1"


class PreparationEncoder(Protocol):
    """Run-owned local encoder injected by the worker composition root."""

    def available(self) -> bool: ...

    def encode(
        self,
        inputs: Sequence[EncodingInput],
        *,
        purpose: EncodingPurpose,
        space: EmbeddingGemma2Space,
        cancelled: Callable[[], bool] | None = None,
    ) -> list[tuple[float, ...]]: ...


@dataclass(frozen=True, slots=True)
class PreparationProgress:
    processed_parts: int
    pending: bool
    complete: bool


def _check_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise asyncio.CancelledError


def _encoded_version(value: int | str) -> tuple[str, str]:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value), "integer"
    return str(value), "text"


ResolvedTextProducerSource = (
    ResolvedCellTextSource | ResolvedCellTranscriptSource | ResolvedCellOcrTextSource
)
ResolvedProducerSource = ResolvedTextProducerSource | ResolvedCellBlobSource


def _binding(
    resolved: ResolvedProducerSource,
) -> StoredBinding:
    pinned = resolved.binding
    return StoredBinding(
        binding_id=cell_source_binding_id(
            sheet_id=resolved.sheet_id,
            row_id=pinned.row_id,
            column_id=pinned.column_id,
            source_id=resolved.source.source_id,
        ),
        sheet_id=resolved.sheet_id,
        row_id=pinned.row_id,
        column_id=pinned.column_id,
        dependencies=(
            BindingDependency(
                row_id=pinned.row_id,
                column_id=pinned.column_id,
                retrieval_revision=pinned.retrieval_revision,
            ),
        ),
    )


def _whole_locator(resolved: ResolvedTextProducerSource) -> SourceLocator:
    surface = resolved.surface
    if isinstance(surface, TranscriptSurface):
        return SourceLocator(
            kind="transcript",
            time=TimeRange(
                surface.segments[0].start_ms / 1000.0,
                surface.segments[-1].end_ms / 1000.0,
            ),
            source_span_ids=tuple(item.source_span_id for item in surface.segments),
        )
    text = surface if isinstance(surface, TextSurface) else surface.text
    pages = None
    if isinstance(surface, PreparedDocumentSurface):
        pages = PageRange(surface.pages[0].page_number, surface.pages[-1].page_number)
    return SourceLocator(
        kind="whole_source",
        text=TextRange(
            text_surface_id=text.text_surface_id,
            content_hash=text.content_hash,
            start=0,
            end=len(text.text),
        ),
        pages=pages,
        source_span_ids=text.source_span_ids,
    )


def publish_lexical_source(
    store: RetrievalStore, resolved: ResolvedTextProducerSource
) -> None:
    """Durably publish one full logical text and its current binding."""

    store.register_preparation(
        resolved.source,
        (_binding(resolved),),
        recipe_id=LEXICAL_RECIPE_ID,
        channel=LEXICAL_CHANNEL,
        space_id=None,
    )
    state = store.preparation_state(
        resolved.source.source_id,
        recipe_id=LEXICAL_RECIPE_ID,
        channel=LEXICAL_CHANNEL,
    )
    if state is not None and state.status == "ready":
        return
    published = store.publish_source(
        SourcePublication(
            source_revision=resolved.source,
            recipe_id=LEXICAL_RECIPE_ID,
            channel=LEXICAL_CHANNEL,
            parts=(),
            bindings=(),
            lexical=LexicalInput(resolved.lexical_text, _whole_locator(resolved)),
            space_id=None,
        )
    )
    if not published:
        raise RuntimeError("lexical preparation was superseded during dispatch")


def queue_text_preparation(
    store: RetrievalStore,
    resolved: ResolvedCellTextSource,
    *,
    recipe: TextChunkRecipe = DEFAULT_TEXT_RECIPE,
    space: EmbeddingGemma2Space = DEFAULT_TEXT_SPACE,
) -> None:
    """Register a scoped semantic target without copying its source surface."""

    store.register_preparation(
        resolved.source,
        (_binding(resolved),),
        recipe_id=recipe.recipe_id,
        channel=TEXT_CHANNEL,
        space_id=space.identity,
        replace_channel_recipes=True,
    )


def _queue_evidence_preparation(
    store: RetrievalStore,
    resolved: ResolvedCellTranscriptSource | ResolvedCellOcrTextSource,
    *,
    recipe: TranscriptChunkRecipe | TextChunkRecipe,
    channel: str,
    space: EmbeddingGemma2Space,
) -> None:
    store.register_preparation(
        resolved.surface.source,
        (_binding(resolved),),
        recipe_id=recipe.recipe_id,
        channel=channel,
        space_id=space.identity,
        replace_channel_recipes=True,
    )


def dispatch_resolved_evidence_source(
    store: RetrievalStore,
    resolved: ResolvedCellTranscriptSource | ResolvedCellOcrTextSource,
    *,
    space: EmbeddingGemma2Space = DEFAULT_TEXT_SPACE,
    policy: PreparationPolicy = DEFAULT_PREPARATION_POLICY,
) -> None:
    if isinstance(resolved, ResolvedCellTranscriptSource):
        recipe: TranscriptChunkRecipe | TextChunkRecipe | None = (
            policy.transcript_recipe
        )
        recipe_id = (
            DISABLED_TRANSCRIPT_RECIPE_ID if recipe is None else recipe.recipe_id
        )
        channel = TRANSCRIPT_CHANNEL
    else:
        recipe = policy.text_recipe
        recipe_id = DISABLED_TEXT_RECIPE_ID if recipe is None else recipe.recipe_id
        channel = OCR_TEXT_CHANNEL
    store.register_preparation(
        resolved.surface.source,
        (_binding(resolved),),
        recipe_id=recipe_id,
        channel=channel,
        space_id=space.identity,
        replace_channel_recipes=True,
    )
    publish_lexical_source(store, resolved)
    if recipe is None:
        store.mark_preparation_unavailable(
            resolved.surface.source,
            recipe_id=recipe_id,
            channel=channel,
            space_id=space.identity,
            error_code=POLICY_DISABLED_ERROR,
            reason="this retrieval producer is disabled by project policy",
        )


def dispatch_resolved_blob_source(
    store: RetrievalStore,
    resolved: ResolvedCellBlobSource,
    *,
    space: EmbeddingGemma2Space = DEFAULT_TEXT_SPACE,
    policy: PreparationPolicy = DEFAULT_PREPARATION_POLICY,
) -> None:
    """Register one exact blob for each policy-selected native channel."""

    mime = resolved.mime_type.lower().split(";", 1)[0].strip()
    targets: list[tuple[str, str, str | None]]
    if mime.startswith("image/"):
        recipe_id = (
            IMAGE_BYTES_RECIPE_ID if policy.image_enabled else DISABLED_IMAGE_RECIPE_ID
        )
        targets = [(recipe_id, IMAGE_CHANNEL, None)]
    elif mime == "application/pdf":
        recipe_id = (
            PDF_PAGE_RECIPE_ID if policy.pdf_pages_enabled else DISABLED_PDF_RECIPE_ID
        )
        targets = [(recipe_id, PDF_PAGE_CHANNEL, None)]
    elif mime.startswith("video/"):
        video_recipe_id = (
            DISABLED_VIDEO_RECIPE_ID
            if policy.video_recipe is None
            else policy.video_recipe.recipe_id
        )
        targets = [(video_recipe_id, VIDEO_CHANNEL, None)]
        if policy.audio_recipe is not None:
            targets.append((policy.audio_recipe.recipe_id, AUDIO_CHANNEL, None))
        elif (
            store.db.execute(
                "SELECT 1 FROM preparation WHERE source_id=? AND channel=? LIMIT 1",
                (resolved.source.source_id, AUDIO_CHANNEL),
            ).fetchone()
            is not None
        ):
            # Replacing with the disabled recipe removes prior audio parts and
            # prevents a policy change from leaving soundtrack hits searchable.
            targets.append((DISABLED_AUDIO_RECIPE_ID, AUDIO_CHANNEL, None))
    elif mime.startswith("audio/"):
        recipe_id = (
            DISABLED_AUDIO_RECIPE_ID
            if policy.audio_recipe is None
            else policy.audio_recipe.recipe_id
        )
        targets = [(recipe_id, AUDIO_CHANNEL, None)]
    else:
        targets = [
            (
                UNSUPPORTED_MEDIA_RECIPE_ID,
                UNSUPPORTED_MEDIA_CHANNEL,
                UNSUPPORTED_MEDIA_ERROR,
            )
        ]
    for recipe_id, channel, unavailable_code in targets:
        store.register_preparation(
            resolved.source,
            (_binding(resolved),),
            recipe_id=recipe_id,
            channel=channel,
            space_id=space.identity,
            replace_channel_recipes=True,
        )
        state = store.preparation_state(
            resolved.source.source_id,
            recipe_id=recipe_id,
            channel=channel,
        )
        if (
            state is not None
            and state.status == "failed"
            and state.error_code == "blob_integrity_failed"
        ):
            # A later authoritative source event may observe repaired
            # content-addressed storage. Retry only that exact failed target;
            # the worker itself never loops on corrupt bytes.
            store.begin_preparation(
                resolved.source,
                recipe_id=recipe_id,
                channel=channel,
                space_id=space.identity,
            )
        policy_disabled = recipe_id.startswith("policy-disabled-")
        if unavailable_code is not None or policy_disabled:
            error_code = unavailable_code or (
                POLICY_DISABLED_ERROR if policy_disabled else None
            )
            assert error_code is not None
            store.mark_preparation_unavailable(
                resolved.source,
                recipe_id=recipe_id,
                channel=channel,
                space_id=space.identity,
                error_code=error_code,
                reason=(
                    f"unsupported local media type: {resolved.mime_type}"
                    if unavailable_code == UNSUPPORTED_MEDIA_ERROR
                    else "this retrieval producer is disabled by project policy"
                    if policy_disabled
                    else "this retrieval source is unavailable"
                ),
            )


def dispatch_resolved_text_source(
    store: RetrievalStore,
    resolved: ResolvedCellTextSource,
    *,
    recipe: TextChunkRecipe | None = DEFAULT_TEXT_RECIPE,
    space: EmbeddingGemma2Space = DEFAULT_TEXT_SPACE,
) -> None:
    recipe_id = DISABLED_TEXT_RECIPE_ID if recipe is None else recipe.recipe_id
    if recipe is None:
        store.register_preparation(
            resolved.source,
            (_binding(resolved),),
            recipe_id=recipe_id,
            channel=TEXT_CHANNEL,
            space_id=space.identity,
            replace_channel_recipes=True,
        )
    else:
        queue_text_preparation(store, resolved, recipe=recipe, space=space)
    publish_lexical_source(store, resolved)
    if recipe is None:
        store.mark_preparation_unavailable(
            resolved.source,
            recipe_id=recipe_id,
            channel=TEXT_CHANNEL,
            space_id=space.identity,
            error_code=POLICY_DISABLED_ERROR,
            reason="this retrieval producer is disabled by project policy",
        )


def retire_cell_binding(
    store: RetrievalStore, *, sheet_id: int, row_id: int, column_id: int
) -> None:
    store.remove_cell_bindings(
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=column_id,
    )


def _cursor_json(cursor: TextCursor | None) -> str | None:
    if cursor is None:
        return None
    return json.dumps(
        {
            "kind": "text",
            "source_identity": cursor.source_identity,
            "recipe_id": cursor.recipe_id,
            "next_offset": cursor.next_offset,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _text_cursor(value: str | None) -> TextCursor | None:
    if value is None:
        return None
    payload = json.loads(value)
    if not isinstance(payload, dict) or payload.get("kind") != "text":
        raise ValueError("invalid text preparation cursor")
    return TextCursor(
        source_identity=str(payload["source_identity"]),
        recipe_id=str(payload["recipe_id"]),
        next_offset=int(payload["next_offset"]),
    )


def _transcript_cursor_json(cursor: TranscriptCursor | None) -> str | None:
    if cursor is None:
        return None
    return json.dumps(
        {
            "kind": "transcript",
            "source_identity": cursor.source_identity,
            "recipe_id": cursor.recipe_id,
            "next_segment": cursor.next_segment,
            "next_offset": cursor.next_offset,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _transcript_cursor(value: str | None) -> TranscriptCursor | None:
    if value is None:
        return None
    payload = json.loads(value)
    if not isinstance(payload, dict) or payload.get("kind") != "transcript":
        raise ValueError("invalid transcript preparation cursor")
    return TranscriptCursor(
        source_identity=str(payload["source_identity"]),
        recipe_id=str(payload["recipe_id"]),
        next_segment=int(payload["next_segment"]),
        next_offset=int(payload["next_offset"]),
    )


def _resolve_work_source(
    project,
    store: RetrievalStore,
    work: PreparationWork,
) -> ResolvedProducerSource | None:
    target = work.source_revision
    encoded, value_type = _encoded_version(target.version_value)
    bindings = store.db.execute(
        "SELECT binding.sheet_id,dependency.row_id,dependency.column_id,"
        "dependency.retrieval_revision FROM bindings binding "
        "JOIN binding_dependencies dependency "
        "ON dependency.binding_key=binding.binding_key "
        "JOIN sources source ON source.source_key=binding.source_key "
        "WHERE source.source_id=? AND source.version_kind=? "
        "AND source.version_value=? AND source.version_type=? "
        "ORDER BY binding.binding_id,dependency.column_id,dependency.row_id",
        (
            target.source_id,
            target.version_kind,
            encoded,
            value_type,
        ),
    )
    with project.read_snapshot() as snapshot:
        for binding in bindings:
            row_id = int(binding["row_id"])
            column_id = int(binding["column_id"])
            sheet_id = int(binding["sheet_id"])
            current = snapshot.db.execute(
                "SELECT retrieval_revision FROM current_cells "
                "WHERE column_id=? AND row_id=?",
                (column_id, row_id),
            ).fetchone()
            if current is None or int(current["retrieval_revision"]) != int(
                binding["retrieval_revision"]
            ):
                continue
            if target.source_kind == "transcript":
                resolved = resolve_cell_transcript_source(
                    snapshot,
                    sheet_id=sheet_id,
                    row_id=row_id,
                    column_id=column_id,
                )
            elif target.source_kind == "ocr_text":
                resolved = resolve_cell_ocr_text_source(
                    snapshot,
                    sheet_id=sheet_id,
                    row_id=row_id,
                    column_id=column_id,
                )
            elif target.source_kind == "blob":
                resolved = resolve_cell_blob_source(
                    snapshot,
                    sheet_id=sheet_id,
                    row_id=row_id,
                    column_id=column_id,
                )
            else:
                resolved = resolve_cell_text_sources(
                    snapshot,
                    sheet_id=sheet_id,
                    column_id=column_id,
                    row_ids=[row_id],
                ).get(row_id)
            if (
                resolved is not None
                and resolved.binding.retrieval_revision
                == int(binding["retrieval_revision"])
                and resolved.source == target
            ):
                return resolved
    return None


def _produced_batch(
    resolved: ResolvedCellTextSource,
    recipe: TextChunkRecipe,
    *,
    max_parts: int,
    cursor: TextCursor | None,
    cancelled: Callable[[], bool] | None,
):
    if isinstance(resolved.surface, PreparedDocumentSurface):
        return produce_prepared_document(
            resolved.surface,
            recipe,
            max_parts=max_parts,
            cursor=cursor,
            cancelled=cancelled,
        )
    return produce_text(
        resolved.surface,
        recipe,
        max_parts=max_parts,
        cursor=cursor,
        cancelled=cancelled,
    )


def _publication(
    resolved: ResolvedProducerSource,
    *,
    recipe_id: str,
    channel: str = TEXT_CHANNEL,
    items: tuple[ProducedPart, ...],
    vectors: Sequence[tuple[float, ...]],
    space: EmbeddingGemma2Space,
    complete: bool,
) -> SourcePublication:
    representations: list[CachedRepresentation] = []
    attachments: list[PartRepresentation] = []
    represented: set[str] = set()
    for item, vector in zip(items, vectors, strict=True):
        hashed = embedding_input_hash(
            item.input,
            purpose=EncodingPurpose.DOCUMENT,
        )
        identity = representation_id(space=space.identity, input_hash=hashed)
        if identity not in represented:
            represented.add(identity)
            representations.append(
                CachedRepresentation(
                    representation_id=identity,
                    space_id=space.identity,
                    input_hash=hashed,
                    vector=pack_vector(vector, dimensions=space.dimensions),
                    dimension=space.dimensions,
                )
            )
        attachments.append(
            PartRepresentation(
                part_id=item.part.part_id,
                representation_id=identity,
                channel=channel,
            )
        )
    return SourcePublication(
        source_revision=resolved.source,
        recipe_id=recipe_id,
        channel=channel,
        parts=tuple(
            StoredPart(
                part_id=item.part.part_id,
                kind=item.part.part_kind,
                locator=item.part.locator,
                group_key=resolved.source.source_id,
            )
            for item in items
        ),
        bindings=(),
        representations=tuple(representations),
        attachments=tuple(attachments),
        complete=complete,
        space_id=space.identity,
    )


def _remaining_progress(
    store: RetrievalStore,
    *,
    channel: str,
    space_id: str,
    processed_parts: int = 0,
) -> PreparationProgress:
    pending = store.next_preparation(channel=channel, space_id=space_id) is not None
    return PreparationProgress(processed_parts, pending, not pending)


def _record_encoding_error(
    store: RetrievalStore,
    work: PreparationWork,
    error: EncodingError,
) -> None:
    if error.code == "engine_busy":
        # Shared runtime contention is job-level and transient. The existing
        # worker retry/backoff resumes this exact preparing target.
        raise error
    if error.code == "model_unavailable":
        store.mark_preparation_unavailable(
            work.source_revision,
            recipe_id=work.recipe_id,
            channel=work.channel,
            space_id=work.space_id,
            reason=str(error),
        )
        return
    store.fail_preparation(
        work.source_revision,
        recipe_id=work.recipe_id,
        channel=work.channel,
        space_id=work.space_id,
        error_code=error.code,
        error_message=str(error),
    )


def process_text_preparation_batch(
    project,
    *,
    encoder: PreparationEncoder | None,
    recipe: TextChunkRecipe = DEFAULT_TEXT_RECIPE,
    space: EmbeddingGemma2Space = DEFAULT_TEXT_SPACE,
    max_parts: int = 32,
    cancelled: Callable[[], bool] | None = None,
) -> PreparationProgress:
    """Process one durable local-only text batch from the existing worklist."""

    if max_parts < 1:
        raise ValueError("max_parts must be positive")
    _check_cancelled(cancelled)
    with RetrievalStore(project).open() as store:
        work = store.next_preparation(channel=TEXT_CHANNEL, space_id=space.identity)
        if work is None:
            return PreparationProgress(0, False, True)
        resolved = _resolve_work_source(project, store, work)
        if resolved is None:
            store.fail_preparation(
                work.source_revision,
                recipe_id=work.recipe_id,
                channel=work.channel,
                space_id=work.space_id,
                error_code="stale_source",
                error_message="no current binding still resolves this source revision",
            )
            pending = (
                store.next_preparation(channel=TEXT_CHANNEL, space_id=space.identity)
                is not None
            )
            return PreparationProgress(0, pending, not pending)
        if encoder is None or not encoder.available():
            store.mark_preparation_unavailable(
                resolved.source,
                recipe_id=work.recipe_id,
                channel=work.channel,
                space_id=work.space_id,
                reason="the configured local retrieval encoder is unavailable",
            )
            pending = (
                store.next_preparation(channel=TEXT_CHANNEL, space_id=space.identity)
                is not None
            )
            return PreparationProgress(0, pending, not pending)
        if work.recipe_id != recipe.recipe_id:
            # A policy edit can land after dirty dispatch but before this
            # bounded channel pass. Leave the new target for the next claim,
            # whose composition root reads the new policy.
            return PreparationProgress(0, True, False)

        store.begin_preparation(
            resolved.source,
            recipe_id=work.recipe_id,
            channel=TEXT_CHANNEL,
            space_id=space.identity,
        )
        batch = _produced_batch(
            resolved,
            recipe,
            max_parts=max_parts,
            cursor=_text_cursor(work.cursor_json),
            cancelled=cancelled,
        )
        _check_cancelled(cancelled)
        try:
            encoded = EmbeddingFacade(store).encode(
                [item.input for item in batch.items],
                encoder=lambda inputs, *, purpose, space: encoder.encode(
                    inputs,
                    purpose=purpose,
                    space=space,
                    cancelled=cancelled,
                ),
                purpose=EncodingPurpose.DOCUMENT,
                space=space,
            )
        except EncodingError as exc:
            _record_encoding_error(store, work, exc)
            return _remaining_progress(
                store, channel=TEXT_CHANNEL, space_id=space.identity
            )
        if len(encoded) != len(batch.items):
            raise ValueError("encoder returned the wrong vector count")
        vectors = [tuple(vector) for vector in encoded]
        complete = batch.continuation is None
        published = store.publish_source(
            _publication(
                resolved,
                recipe_id=recipe.recipe_id,
                items=batch.items,
                vectors=vectors,
                space=space,
                complete=complete,
            )
        )
        if not published:
            raise RuntimeError("text preparation was superseded during publication")
        if not complete:
            cursor_json = _cursor_json(batch.continuation)
            if cursor_json is None or not store.advance_preparation(
                resolved.source,
                recipe_id=recipe.recipe_id,
                channel=TEXT_CHANNEL,
                space_id=space.identity,
                cursor_json=cursor_json,
            ):
                raise RuntimeError(
                    "text preparation was superseded while saving its cursor"
                )
        pending = (
            store.next_preparation(channel=TEXT_CHANNEL, space_id=space.identity)
            is not None
        )
        return PreparationProgress(len(batch.items), pending, not pending)


def process_evidence_preparation_batch(
    project,
    *,
    channel: str,
    encoder: PreparationEncoder | None,
    space: EmbeddingGemma2Space = DEFAULT_TEXT_SPACE,
    max_parts: int = 32,
    cancelled: Callable[[], bool] | None = None,
    policy: PreparationPolicy = DEFAULT_PREPARATION_POLICY,
) -> PreparationProgress:
    """Process one bounded transcript or OCR-text batch."""

    if channel not in {TRANSCRIPT_CHANNEL, OCR_TEXT_CHANNEL}:
        raise ValueError("evidence channel must be transcript_text or ocr_text")
    if max_parts < 1:
        raise ValueError("max_parts must be positive")
    _check_cancelled(cancelled)
    with RetrievalStore(project).open() as store:
        work = store.next_preparation(channel=channel, space_id=space.identity)
        if work is None:
            return PreparationProgress(0, False, True)
        resolved = _resolve_work_source(project, store, work)
        expected = (
            ResolvedCellTranscriptSource
            if channel == TRANSCRIPT_CHANNEL
            else ResolvedCellOcrTextSource
        )
        if not isinstance(resolved, expected):
            store.fail_preparation(
                work.source_revision,
                recipe_id=work.recipe_id,
                channel=work.channel,
                space_id=work.space_id,
                error_code="stale_source",
                error_message="no current binding still resolves this evidence source",
            )
            pending = (
                store.next_preparation(channel=channel, space_id=space.identity)
                is not None
            )
            return PreparationProgress(0, pending, not pending)
        if encoder is None or not encoder.available():
            store.mark_preparation_unavailable(
                resolved.source,
                recipe_id=work.recipe_id,
                channel=work.channel,
                space_id=work.space_id,
                reason="the configured local retrieval encoder is unavailable",
            )
            pending = (
                store.next_preparation(channel=channel, space_id=space.identity)
                is not None
            )
            return PreparationProgress(0, pending, not pending)

        recipe: TextChunkRecipe | TranscriptChunkRecipe
        if isinstance(resolved, ResolvedCellTranscriptSource):
            recipe = policy.transcript_recipe
            if recipe is None or work.recipe_id != recipe.recipe_id:
                return PreparationProgress(0, True, False)
            batch = produce_transcript(
                resolved.surface,
                recipe,
                max_parts=max_parts,
                cursor=_transcript_cursor(work.cursor_json),
                cancelled=cancelled,
            )
        else:
            recipe = policy.text_recipe
            if recipe is None or work.recipe_id != recipe.recipe_id:
                return PreparationProgress(0, True, False)
            batch = produce_text(
                resolved.surface,
                recipe,
                max_parts=max_parts,
                cursor=_text_cursor(work.cursor_json),
                cancelled=cancelled,
            )
        store.begin_preparation(
            resolved.source,
            recipe_id=work.recipe_id,
            channel=channel,
            space_id=space.identity,
        )
        try:
            encoded = EmbeddingFacade(store).encode(
                [item.input for item in batch.items],
                encoder=lambda inputs, *, purpose, space: encoder.encode(
                    inputs,
                    purpose=purpose,
                    space=space,
                    cancelled=cancelled,
                ),
                purpose=EncodingPurpose.DOCUMENT,
                space=space,
            )
        except EncodingError as exc:
            _record_encoding_error(store, work, exc)
            return _remaining_progress(store, channel=channel, space_id=space.identity)
        vectors = [tuple(vector) for vector in encoded]
        if len(vectors) != len(batch.items):
            raise ValueError("encoder returned the wrong vector count")
        complete = batch.continuation is None
        if not store.publish_source(
            _publication(
                resolved,
                recipe_id=recipe.recipe_id,
                channel=channel,
                items=batch.items,
                vectors=vectors,
                space=space,
                complete=complete,
            )
        ):
            raise RuntimeError("evidence preparation was superseded during publication")
        if not complete:
            if isinstance(batch.continuation, TranscriptCursor):
                cursor_json = _transcript_cursor_json(batch.continuation)
            else:
                cursor_json = _cursor_json(batch.continuation)
            if cursor_json is None or not store.advance_preparation(
                resolved.source,
                recipe_id=work.recipe_id,
                channel=channel,
                space_id=space.identity,
                cursor_json=cursor_json,
            ):
                raise RuntimeError(
                    "evidence preparation was superseded while saving its cursor"
                )
        pending = (
            store.next_preparation(channel=channel, space_id=space.identity) is not None
        )
        return PreparationProgress(len(batch.items), pending, not pending)


def _pdf_cursor_payload(
    resolved: ResolvedCellBlobSource,
    *,
    next_page: int,
    page_count: int,
) -> str:
    return json.dumps(
        {
            "kind": "pdf_pages",
            "source_id": resolved.source.source_id,
            "recipe_id": PDF_PAGE_RECIPE_ID,
            "next_page": next_page,
            "page_count": page_count,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _pdf_cursor(
    value: str | None, resolved: ResolvedCellBlobSource
) -> tuple[int, int] | None:
    if value is None:
        return None
    payload = json.loads(value)
    if (
        not isinstance(payload, dict)
        or payload.get("kind") != "pdf_pages"
        or payload.get("recipe_id") != PDF_PAGE_RECIPE_ID
        or payload.get("source_id") != resolved.source.source_id
    ):
        raise ValueError("invalid PDF preparation cursor")
    next_page = int(payload["next_page"])
    page_count = int(payload["page_count"])
    if next_page < 1 or page_count < next_page:
        raise ValueError("invalid PDF preparation cursor range")
    return next_page, page_count


def _blob_image_batch(
    project,
    resolved: ResolvedCellBlobSource,
    *,
    channel: str,
    cursor_json: str | None,
    max_parts: int,
    cancelled: Callable[[], bool] | None,
) -> tuple[tuple[ProducedPart, ...], str | None]:
    with project.materialize_blob(resolved.blob_hash) as materialized:
        path = Path(materialized)
        if channel == IMAGE_CHANNEL:
            if cursor_json is not None:
                raise ValueError("image preparation does not have a continuation")
            if path.stat().st_size > EMBEDDINGGEMMA2_MAX_ENCODED_IMAGE_BYTES:
                raise ValueError("encoded image exceeds the supported 64 MiB limit")
            data = path.read_bytes()
            batch = produce_image(
                ImageSurface(resolved.source, data, input_hash(data)),
                cancelled=cancelled,
            )
            return batch.items, None

        cursor = _pdf_cursor(cursor_json, resolved)
        authored_page = resolved.page_number
        with tempfile.TemporaryDirectory(
            prefix="frisket-retrieval-pdf-preparation-"
        ) as directory:
            scratch = Path(directory)
            if authored_page is not None:
                if cursor is not None:
                    raise ValueError("authored PDF page cannot have a continuation")
                selected_pages: list[int] | None = [authored_page]
                page_limit = None
            elif cursor is None:
                selected_pages = None
                page_limit = max_parts
            else:
                start, page_count = cursor
                selected_pages = list(
                    range(start, min(start + max_parts, page_count + 1))
                )
                page_limit = None
            rendered = run_media_sync(
                lambda should_cancel: render_pdf_pages(
                    path,
                    scratch,
                    dpi=144,
                    pages=selected_pages,
                    page_limit=page_limit,
                    max_edge=1600,
                    should_cancel=should_cancel,
                ),
                cancelled=cancelled,
            )
            items: list[ProducedPart] = []
            for page, image_path in rendered.pages:
                _check_cancelled(cancelled)
                if image_path.stat().st_size > EMBEDDINGGEMMA2_MAX_ENCODED_IMAGE_BYTES:
                    raise ValueError(
                        f"rendered PDF page {page} exceeds the 64 MiB image limit"
                    )
                data = image_path.read_bytes()
                items.extend(
                    produce_image(
                        ImageSurface(
                            resolved.source,
                            data,
                            input_hash(data),
                            pages=PageRange(page, page),
                        ),
                        recipe_id=PDF_PAGE_RECIPE_ID,
                        cancelled=cancelled,
                    ).items
                )
            if authored_page is not None or not rendered.pages:
                continuation = None
            else:
                next_page = rendered.pages[-1][0] + 1
                continuation = (
                    None
                    if next_page > rendered.page_count
                    else _pdf_cursor_payload(
                        resolved,
                        next_page=next_page,
                        page_count=rendered.page_count,
                    )
                )
            return tuple(items), continuation


def process_blob_image_preparation_batch(
    project,
    *,
    channel: str,
    encoder: PreparationEncoder | None,
    space: EmbeddingGemma2Space = DEFAULT_TEXT_SPACE,
    max_parts: int = 8,
    cancelled: Callable[[], bool] | None = None,
    policy: PreparationPolicy = DEFAULT_PREPARATION_POLICY,
) -> PreparationProgress:
    """Prepare one bounded image or rendered-PDF-page batch."""

    if channel not in {IMAGE_CHANNEL, PDF_PAGE_CHANNEL}:
        raise ValueError("blob image channel must be image or pdf_page")
    if max_parts < 1:
        raise ValueError("max_parts must be positive")
    _check_cancelled(cancelled)
    with RetrievalStore(project).open() as store:
        work = store.next_preparation(channel=channel, space_id=space.identity)
        if work is None:
            return PreparationProgress(0, False, True)
        resolved = _resolve_work_source(project, store, work)
        if not isinstance(resolved, ResolvedCellBlobSource):
            store.fail_preparation(
                work.source_revision,
                recipe_id=work.recipe_id,
                channel=channel,
                space_id=work.space_id,
                error_code="stale_source",
                error_message="no current binding still resolves this blob source",
            )
            pending = (
                store.next_preparation(channel=channel, space_id=space.identity)
                is not None
            )
            return PreparationProgress(0, pending, not pending)
        if encoder is None or not encoder.available():
            store.mark_preparation_unavailable(
                resolved.source,
                recipe_id=work.recipe_id,
                channel=channel,
                space_id=space.identity,
                reason="the configured local retrieval encoder is unavailable",
            )
            pending = (
                store.next_preparation(channel=channel, space_id=space.identity)
                is not None
            )
            return PreparationProgress(0, pending, not pending)
        expected_recipe = (
            IMAGE_BYTES_RECIPE_ID if channel == IMAGE_CHANNEL else PDF_PAGE_RECIPE_ID
        )
        enabled = (
            policy.image_enabled
            if channel == IMAGE_CHANNEL
            else policy.pdf_pages_enabled
        )
        if not enabled or work.recipe_id != expected_recipe:
            return PreparationProgress(0, True, False)
        store.begin_preparation(
            resolved.source,
            recipe_id=work.recipe_id,
            channel=channel,
            space_id=space.identity,
        )
        try:
            items, continuation = _blob_image_batch(
                project,
                resolved,
                channel=channel,
                cursor_json=work.cursor_json,
                max_parts=max_parts,
                cancelled=cancelled,
            )
        except PdfRenderCancelled as exc:
            raise asyncio.CancelledError from exc
        except (
            BlobIntegrityError,
            BlobNotFoundError,
            PdfPageOutOfRange,
            PdfRenderError,
            ValueError,
        ) as exc:
            error_code = (
                "missing_blob"
                if isinstance(exc, BlobNotFoundError)
                else "blob_integrity_failed"
                if isinstance(exc, BlobIntegrityError)
                else "pdf_page_out_of_range"
                if isinstance(exc, PdfPageOutOfRange)
                else "pdf_render_failed"
                if isinstance(exc, PdfRenderError)
                else "invalid_blob_input"
            )
            store.fail_preparation(
                resolved.source,
                recipe_id=work.recipe_id,
                channel=channel,
                space_id=space.identity,
                error_code=error_code,
                error_message=str(exc),
            )
            pending = (
                store.next_preparation(channel=channel, space_id=space.identity)
                is not None
            )
            return PreparationProgress(0, pending, not pending)
        try:
            encoded = EmbeddingFacade(store).encode(
                [item.input for item in items],
                encoder=lambda inputs, *, purpose, space: encoder.encode(
                    inputs,
                    purpose=purpose,
                    space=space,
                    cancelled=cancelled,
                ),
                purpose=EncodingPurpose.DOCUMENT,
                space=space,
            )
        except EncodingError as exc:
            _record_encoding_error(store, work, exc)
            return _remaining_progress(store, channel=channel, space_id=space.identity)
        vectors = [tuple(vector) for vector in encoded]
        if len(vectors) != len(items):
            raise ValueError("encoder returned the wrong vector count")
        if not store.publish_source(
            _publication(
                resolved,
                recipe_id=work.recipe_id,
                channel=channel,
                items=items,
                vectors=vectors,
                space=space,
                complete=continuation is None,
            )
        ):
            raise RuntimeError("blob preparation was superseded during publication")
        if continuation is not None and not store.advance_preparation(
            resolved.source,
            recipe_id=work.recipe_id,
            channel=channel,
            space_id=space.identity,
            cursor_json=continuation,
        ):
            raise RuntimeError(
                "blob preparation was superseded while saving its cursor"
            )
        pending = (
            store.next_preparation(channel=channel, space_id=space.identity) is not None
        )
        return PreparationProgress(len(items), pending, not pending)


def _media_cursor(
    value: str | None, resolved: ResolvedCellBlobSource, recipe_id: str
) -> tuple[int, float | None]:
    if value is None:
        return 0, None
    payload = json.loads(value)
    if (
        not isinstance(payload, dict)
        or payload.get("kind") != "media"
        or payload.get("source_id") != resolved.source.source_id
        or payload.get("recipe_id") != recipe_id
    ):
        raise ValueError("invalid media preparation cursor")
    if "next_seconds" in payload:
        seconds = float(payload["next_seconds"])
        if not seconds >= 0:
            raise ValueError("invalid media preparation cursor time")
        return 0, seconds
    index = int(payload["next_index"])
    if index < 1:
        raise ValueError("invalid media preparation cursor index")
    return index, None


def _media_cursor_json(
    resolved: ResolvedCellBlobSource,
    recipe_id: str,
    *,
    next_index: int | None = None,
    next_seconds: float | None = None,
) -> str:
    if (next_index is None) == (next_seconds is None):
        raise ValueError("media cursor requires exactly one continuation position")
    position = (
        {"next_index": next_index}
        if next_index is not None
        else {"next_seconds": next_seconds}
    )
    return json.dumps(
        {
            "kind": "media",
            "source_id": resolved.source.source_id,
            "recipe_id": recipe_id,
            **position,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _located_media_part(
    resolved: ResolvedCellBlobSource,
    recipe_id: str,
    item: LocatedFrame | LocatedVideoClip | AudioWindow,
) -> ProducedPart:
    if isinstance(item, LocatedFrame):
        part_kind = "video_frame"
        start_seconds = end_seconds = item.point.seconds
    elif isinstance(item, LocatedVideoClip):
        part_kind = "video_clip"
        start_seconds, end_seconds = item.start_seconds, item.end_seconds
    else:
        part_kind = "audio_window"
        start_seconds, end_seconds = item.start_seconds, item.end_seconds
    locator = SourceLocator(
        kind=part_kind,
        time=TimeRange(start_seconds, end_seconds),
    )
    encoded_hash = embedding_input_hash(item.input, purpose=EncodingPurpose.DOCUMENT)
    part_id = "part:" + identity_digest(
        "media_part",
        {
            "source": resolved.source.source_id,
            "version": resolved.source.version_value,
            "recipe": recipe_id,
            "kind": part_kind,
            "start": start_seconds,
            "end": end_seconds,
            "input_hash": encoded_hash,
        },
    )
    return ProducedPart(
        IndexedPart(part_id, resolved.source, recipe_id, part_kind, locator), item.input
    )


def process_timed_media_preparation_batch(
    project,
    *,
    channel: str,
    encoder: PreparationEncoder | None,
    space: EmbeddingGemma2Space = DEFAULT_TEXT_SPACE,
    max_parts: int = 8,
    cancelled: Callable[[], bool] | None = None,
    policy: PreparationPolicy = DEFAULT_PREPARATION_POLICY,
) -> PreparationProgress:
    """Prepare one resumable fixed-video or raw-audio window batch."""

    if channel not in {VIDEO_CHANNEL, AUDIO_CHANNEL}:
        raise ValueError("timed media channel must be video or audio")
    if max_parts < 1:
        raise ValueError("max_parts must be positive")
    _check_cancelled(cancelled)
    with RetrievalStore(project).open() as store:
        work = store.next_preparation(channel=channel, space_id=space.identity)
        if work is None:
            return PreparationProgress(0, False, True)
        resolved = _resolve_work_source(project, store, work)
        if not isinstance(resolved, ResolvedCellBlobSource):
            store.fail_preparation(
                work.source_revision,
                recipe_id=work.recipe_id,
                channel=channel,
                space_id=work.space_id,
                error_code="stale_source",
                error_message="no current binding still resolves this media source",
            )
            pending = (
                store.next_preparation(channel=channel, space_id=space.identity)
                is not None
            )
            return PreparationProgress(0, pending, not pending)
        if encoder is None or not encoder.available():
            store.mark_preparation_unavailable(
                resolved.source,
                recipe_id=work.recipe_id,
                channel=channel,
                space_id=space.identity,
                reason="the configured local retrieval encoder is unavailable",
            )
            pending = (
                store.next_preparation(channel=channel, space_id=space.identity)
                is not None
            )
            return PreparationProgress(0, pending, not pending)
        base_recipe = (
            policy.video_recipe if channel == VIDEO_CHANNEL else policy.audio_recipe
        )
        if base_recipe is None or work.recipe_id != base_recipe.recipe_id:
            return PreparationProgress(0, True, False)
        start_index, start_seconds = _media_cursor(
            work.cursor_json, resolved, base_recipe.recipe_id
        )
        if isinstance(base_recipe, PeriodicFrameRecipe):
            recipe = replace(base_recipe, max_frames=max_parts)
        elif isinstance(base_recipe, FixedClipRecipe | ShotClipRecipe):
            recipe = replace(base_recipe, max_clips=max_parts)
        else:
            recipe = replace(base_recipe, max_windows=max_parts)
        store.begin_preparation(
            resolved.source,
            recipe_id=work.recipe_id,
            channel=channel,
            space_id=space.identity,
        )
        try:
            with _borrow_materialized_media(project, resolved.blob_hash) as source:
                produced = run_media_sync(
                    lambda should_cancel: produce_media(
                        source,
                        recipe,
                        cancelled=should_cancel,
                        start_index=start_index,
                        start_seconds=start_seconds,
                    ),
                    cancelled=cancelled,
                )
        except MediaProductionError as exc:
            store.fail_preparation(
                resolved.source,
                recipe_id=work.recipe_id,
                channel=channel,
                space_id=space.identity,
                error_code=exc.code,
                error_message=str(exc),
            )
            pending = (
                store.next_preparation(channel=channel, space_id=space.identity)
                is not None
            )
            return PreparationProgress(0, pending, not pending)
        except (BlobIntegrityError, BlobNotFoundError) as exc:
            store.fail_preparation(
                resolved.source,
                recipe_id=work.recipe_id,
                channel=channel,
                space_id=space.identity,
                error_code=(
                    "missing_blob"
                    if isinstance(exc, BlobNotFoundError)
                    else "blob_integrity_failed"
                ),
                error_message=str(exc),
            )
            pending = (
                store.next_preparation(channel=channel, space_id=space.identity)
                is not None
            )
            return PreparationProgress(0, pending, not pending)
        if produced.coverage.truncation_reason == "missing_samples":
            store.fail_preparation(
                resolved.source,
                recipe_id=work.recipe_id,
                channel=channel,
                space_id=space.identity,
                error_code="missing_samples",
                error_message="media decoder could not cover every planned window",
            )
            pending = (
                store.next_preparation(channel=channel, space_id=space.identity)
                is not None
            )
            return PreparationProgress(0, pending, not pending)
        items = tuple(
            _located_media_part(resolved, work.recipe_id, item)
            for item in produced.items
            if isinstance(item, LocatedFrame | LocatedVideoClip | AudioWindow)
        )
        try:
            encoded = EmbeddingFacade(store).encode(
                [item.input for item in items],
                encoder=lambda inputs, *, purpose, space: encoder.encode(
                    inputs,
                    purpose=purpose,
                    space=space,
                    cancelled=cancelled,
                ),
                purpose=EncodingPurpose.DOCUMENT,
                space=space,
            )
        except EncodingError as exc:
            _record_encoding_error(store, work, exc)
            return _remaining_progress(store, channel=channel, space_id=space.identity)
        vectors = [tuple(vector) for vector in encoded]
        if len(vectors) != len(items):
            raise ValueError("encoder returned the wrong vector count")
        complete = produced.coverage.complete
        if not store.publish_source(
            _publication(
                resolved,
                recipe_id=work.recipe_id,
                channel=channel,
                items=items,
                vectors=vectors,
                space=space,
                complete=complete,
            )
        ):
            raise RuntimeError("media preparation was superseded during publication")
        if not complete:
            if isinstance(base_recipe, ShotClipRecipe):
                if not produced.coverage.indexed_spans:
                    raise RuntimeError("shot preparation made no timeline progress")
                cursor_json = _media_cursor_json(
                    resolved,
                    work.recipe_id,
                    next_seconds=produced.coverage.indexed_spans[-1][1],
                )
            else:
                cursor_json = _media_cursor_json(
                    resolved,
                    work.recipe_id,
                    next_index=start_index + max_parts,
                )
            if not store.advance_preparation(
                resolved.source,
                recipe_id=work.recipe_id,
                channel=channel,
                space_id=space.identity,
                cursor_json=cursor_json,
            ):
                raise RuntimeError(
                    "media preparation was superseded while saving its cursor"
                )
        pending = (
            store.next_preparation(channel=channel, space_id=space.identity) is not None
        )
        return PreparationProgress(len(items), pending, not pending)


def preparation_work_marker(project_path: str | Path) -> str | None:
    """Return a scheduling marker without creating or modifying the sidecar."""

    path = Path(project_path) / "project.retrieval.db"
    if not path.is_file():
        return None
    db = None
    try:
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0)
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='preparation'"
        ).fetchone()
        if exists is None:
            return None
        row = db.execute(
            "SELECT source_id,recipe_id,channel,space_id,target_version_kind,"
            "target_version_value,target_version_type,cursor_json "
            "FROM preparation WHERE status IN ('pending','preparing') "
            "ORDER BY CASE status WHEN 'preparing' THEN 0 ELSE 1 END,"
            "source_id,recipe_id,channel LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        cursor = "start" if row[7] is None else identity_digest("cursor", row[7])
        return ":".join(
            (
                str(row[0]),
                str(row[1]),
                str(row[2]),
                str(row[3]),
                str(row[4]),
                str(row[5]),
                str(row[6]),
                cursor,
            )
        )
    except sqlite3.DatabaseError:
        return None
    finally:
        if db is not None:
            db.close()


__all__ = [
    "AUDIO_CHANNEL",
    "DEFAULT_PREPARATION_POLICY",
    "DEFAULT_TEXT_RECIPE",
    "DEFAULT_TEXT_SPACE",
    "IMAGE_CHANNEL",
    "LEXICAL_CHANNEL",
    "LEXICAL_RECIPE_ID",
    "OCR_TEXT_CHANNEL",
    "PDF_PAGE_CHANNEL",
    "PREPARATION_POLICY_META_KEY",
    "PreparationEncoder",
    "PreparationPolicy",
    "PreparationProgress",
    "TEXT_CHANNEL",
    "TRANSCRIPT_CHANNEL",
    "UNSUPPORTED_MEDIA_CHANNEL",
    "VIDEO_CHANNEL",
    "dispatch_resolved_blob_source",
    "dispatch_resolved_evidence_source",
    "dispatch_resolved_text_source",
    "parse_preparation_policy",
    "preparation_work_marker",
    "preparation_policy_json",
    "process_blob_image_preparation_batch",
    "process_evidence_preparation_batch",
    "process_text_preparation_batch",
    "process_timed_media_preparation_batch",
    "publish_lexical_source",
    "queue_text_preparation",
    "read_preparation_policy",
    "retire_cell_binding",
    "write_preparation_policy",
]
