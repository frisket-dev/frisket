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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from frisket.ai.embeddings.facade import embedding_input_hash
from frisket.retrieval.content import (
    PreparedDocumentSurface,
    ProducedPart,
    TextChunkRecipe,
    TextCursor,
    TextSurface,
    produce_prepared_document,
    produce_text,
)
from frisket.retrieval.contracts import PageRange, SourceLocator, TextRange
from frisket.retrieval.encoding import (
    EmbeddingGemma2Space,
    EncodingError,
    EncodingInput,
    EncodingPurpose,
)
from frisket.retrieval.native import pack_vector
from frisket.retrieval.source_adapter import (
    ResolvedCellTextSource,
    resolve_cell_text_sources,
)
from frisket.retrieval.sources import (
    identity_digest,
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
DEFAULT_TEXT_RECIPE = TextChunkRecipe()
DEFAULT_TEXT_SPACE = EmbeddingGemma2Space()


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


def cell_binding_id(*, sheet_id: int, row_id: int, column_id: int) -> str:
    return identity_digest(
        "cell_binding",
        {"sheet_id": sheet_id, "row_id": row_id, "column_id": column_id},
    )


def _binding(resolved: ResolvedCellTextSource) -> StoredBinding:
    pinned = resolved.binding
    return StoredBinding(
        binding_id=cell_binding_id(
            sheet_id=resolved.sheet_id,
            row_id=pinned.row_id,
            column_id=pinned.column_id,
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


def _whole_locator(resolved: ResolvedCellTextSource) -> SourceLocator:
    surface = resolved.surface
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
    )


def publish_lexical_source(
    store: RetrievalStore, resolved: ResolvedCellTextSource
) -> None:
    """Durably publish one full logical text and its current binding."""

    store.begin_preparation(
        resolved.source,
        recipe_id=LEXICAL_RECIPE_ID,
        channel=LEXICAL_CHANNEL,
    )
    published = store.publish_source(
        SourcePublication(
            source_revision=resolved.source,
            recipe_id=LEXICAL_RECIPE_ID,
            channel=LEXICAL_CHANNEL,
            parts=(),
            bindings=(_binding(resolved),),
            lexical=LexicalInput(resolved.lexical_text, _whole_locator(resolved)),
        )
    )
    if not published:
        raise RuntimeError("lexical preparation was superseded during dispatch")


def queue_text_preparation(
    store: RetrievalStore,
    resolved: ResolvedCellTextSource,
    *,
    recipe: TextChunkRecipe = DEFAULT_TEXT_RECIPE,
) -> None:
    """Register a scoped semantic target without copying its source surface."""

    store.register_preparation(
        resolved.source,
        (_binding(resolved),),
        recipe_id=recipe.recipe_id,
        channel=TEXT_CHANNEL,
    )


def dispatch_resolved_text_source(
    store: RetrievalStore,
    resolved: ResolvedCellTextSource,
    *,
    recipe: TextChunkRecipe = DEFAULT_TEXT_RECIPE,
    semantic_available: bool = True,
) -> None:
    queue_text_preparation(store, resolved, recipe=recipe)
    publish_lexical_source(store, resolved)
    if not semantic_available:
        store.mark_preparation_unavailable(
            resolved.source,
            recipe_id=recipe.recipe_id,
            channel=TEXT_CHANNEL,
            reason="the configured local retrieval encoder is unavailable",
        )


def retire_cell_binding(
    store: RetrievalStore, *, sheet_id: int, row_id: int, column_id: int
) -> None:
    store.remove_bindings(
        [cell_binding_id(sheet_id=sheet_id, row_id=row_id, column_id=column_id)]
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


def _resolve_work_source(
    project,
    store: RetrievalStore,
    work: PreparationWork,
) -> ResolvedCellTextSource | None:
    target = work.source_revision
    encoded, value_type = _encoded_version(target.version_value)
    bindings = store.db.execute(
        "SELECT binding.sheet_id,dependency.row_id,dependency.column_id,"
        "dependency.retrieval_revision FROM bindings binding "
        "JOIN binding_dependencies dependency "
        "ON dependency.binding_id=binding.binding_id "
        "WHERE binding.source_id=? AND binding.version_kind=? "
        "AND binding.version_value=? AND binding.version_type=? "
        "ORDER BY binding.binding_id,dependency.column_id,dependency.row_id",
        (
            target.source_id,
            target.version_kind,
            encoded,
            value_type,
        ),
    ).fetchall()
    with project.read_snapshot() as snapshot:
        for binding in bindings:
            row_id = int(binding["row_id"])
            column_id = int(binding["column_id"])
            candidates = resolve_cell_text_sources(
                snapshot,
                sheet_id=int(binding["sheet_id"]),
                column_id=column_id,
                row_ids=[row_id],
            )
            resolved = candidates.get(row_id)
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
    resolved: ResolvedCellTextSource,
    *,
    recipe: TextChunkRecipe,
    items: tuple[ProducedPart, ...],
    vectors: Sequence[tuple[float, ...]],
    space: EmbeddingGemma2Space,
    complete: bool,
) -> SourcePublication:
    representations: list[CachedRepresentation] = []
    attachments: list[PartRepresentation] = []
    for item, vector in zip(items, vectors, strict=True):
        hashed = embedding_input_hash(
            item.input,
            purpose=EncodingPurpose.DOCUMENT,
        )
        identity = representation_id(space=space.identity, input_hash=hashed)
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
                channel=TEXT_CHANNEL,
            )
        )
    return SourcePublication(
        source_revision=resolved.source,
        recipe_id=recipe.recipe_id,
        channel=TEXT_CHANNEL,
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
        work = store.next_preparation(channel=TEXT_CHANNEL)
        if work is None:
            return PreparationProgress(0, False, True)
        resolved = _resolve_work_source(project, store, work)
        if resolved is None:
            store.fail_preparation(
                work.source_revision,
                recipe_id=work.recipe_id,
                channel=work.channel,
                error_code="stale_source",
                error_message="no current binding still resolves this source revision",
            )
            pending = store.next_preparation(channel=TEXT_CHANNEL) is not None
            return PreparationProgress(0, pending, not pending)
        if encoder is None or not encoder.available():
            store.mark_preparation_unavailable(
                resolved.source,
                recipe_id=work.recipe_id,
                channel=work.channel,
                reason="the configured local retrieval encoder is unavailable",
            )
            pending = store.next_preparation(channel=TEXT_CHANNEL) is not None
            return PreparationProgress(0, pending, not pending)

        store.begin_preparation(
            resolved.source,
            recipe_id=recipe.recipe_id,
            channel=TEXT_CHANNEL,
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
            encoded = encoder.encode(
                [item.input for item in batch.items],
                purpose=EncodingPurpose.DOCUMENT,
                space=space,
                cancelled=cancelled,
            )
        except EncodingError as exc:
            if exc.code != "model_unavailable":
                raise
            store.mark_preparation_unavailable(
                resolved.source,
                recipe_id=recipe.recipe_id,
                channel=TEXT_CHANNEL,
                reason=str(exc),
            )
            pending = store.next_preparation(channel=TEXT_CHANNEL) is not None
            return PreparationProgress(0, pending, not pending)
        if len(encoded) != len(batch.items):
            raise ValueError("encoder returned the wrong vector count")
        vectors = [tuple(vector) for vector in encoded]
        complete = batch.continuation is None
        published = store.publish_source(
            _publication(
                resolved,
                recipe=recipe,
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
                cursor_json=cursor_json,
            ):
                raise RuntimeError(
                    "text preparation was superseded while saving its cursor"
                )
        pending = store.next_preparation(channel=TEXT_CHANNEL) is not None
        return PreparationProgress(len(batch.items), pending, not pending)


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
            "SELECT source_id,recipe_id,channel,target_version_kind,"
            "target_version_value,target_version_type,cursor_json "
            "FROM preparation WHERE status IN ('pending','preparing') "
            "ORDER BY CASE status WHEN 'preparing' THEN 0 ELSE 1 END,"
            "source_id,recipe_id,channel LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        cursor = "start" if row[6] is None else identity_digest("cursor", row[6])
        return ":".join(
            (
                str(row[0]),
                str(row[1]),
                str(row[2]),
                str(row[3]),
                str(row[4]),
                str(row[5]),
                cursor,
            )
        )
    except sqlite3.DatabaseError:
        return None
    finally:
        if db is not None:
            db.close()


__all__ = [
    "DEFAULT_TEXT_RECIPE",
    "DEFAULT_TEXT_SPACE",
    "LEXICAL_CHANNEL",
    "LEXICAL_RECIPE_ID",
    "PreparationEncoder",
    "PreparationProgress",
    "TEXT_CHANNEL",
    "cell_binding_id",
    "dispatch_resolved_text_source",
    "preparation_work_marker",
    "process_text_preparation_batch",
    "publish_lexical_source",
    "queue_text_preparation",
    "retire_cell_binding",
]
