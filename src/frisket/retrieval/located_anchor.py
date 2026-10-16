"""Resolve an existing search location when it has no cached document vector."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .anchors import ResolvedAnchor, resolve_cell_anchor
from .content import TextChunkRecipe
from .contracts import CellAnchor, ScopedBinding, SourceAnchor
from .encoding import TextEncodingInput
from .query import RetrievalQueryError
from .source_adapter import UnsupportedLexicalSourceError, resolve_source_text
from .sources import input_hash


def _current_binding(project: Any, binding: ScopedBinding) -> bool:
    visible = project.db.execute(
        "SELECT 1 FROM rows r JOIN sheets s ON s.id=r.sheet_id "
        "JOIN columns c ON c.sheet_id=s.id "
        "WHERE r.id=? AND s.id=? AND c.id=? "
        "AND r.hidden=0 AND s.hidden=0 AND c.hidden=0",
        (binding.row_id, binding.sheet_id, binding.column_id),
    ).fetchone()
    if visible is None or not binding.dependencies:
        return False
    return all(
        project.db.execute(
            "SELECT 1 FROM current_cells WHERE row_id=? AND column_id=? "
            "AND retrieval_revision=?",
            (dep.row_id, dep.column_id, dep.retrieval_revision),
        ).fetchone()
        is not None
        for dep in binding.dependencies
    )


def resolve_located_anchor(
    project: Any,
    anchor: SourceAnchor,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> ResolvedAnchor:
    """Encode the exact current location, never a nearby prepared passage.

    Source-less or composite anchors must already have their named-index vector.
    A concrete cell binding provides the authority needed for fresh encoding.
    """
    binding = anchor.binding
    if binding is None or binding.column_id is None:
        raise RetrievalQueryError(
            "source_anchor_not_found",
            "This source is not prepared for similarity search.",
        )
    with project.read_snapshot() as snapshot:
        if not _current_binding(snapshot, binding):
            raise RetrievalQueryError(
                "source_anchor_changed",
                "The selected source has changed. Select it again.",
            )
        try:
            text = resolve_source_text(
                snapshot, source=anchor.source, locator=anchor.locator, binding=binding
            )
        except UnsupportedLexicalSourceError:
            text = None
        else:
            if text is None:
                raise RetrievalQueryError(
                    "source_anchor_changed",
                    "The selected text has changed. Select it again.",
                )
            selected = anchor.locator.text
            if selected is not None:
                if input_hash(text) != selected.content_hash or selected.end > len(
                    text
                ):
                    raise RetrievalQueryError(
                        "source_anchor_changed",
                        "The selected text has changed. Select it again.",
                    )
                text = text[selected.start : selected.end]
            maximum = TextChunkRecipe().max_codepoints
            if not text or len(text) > maximum:
                raise RetrievalQueryError(
                    "source_location_required",
                    f"Select a non-empty passage of at most {maximum} characters "
                    "before searching for similar content.",
                )
            return ResolvedAnchor(anchor, TextEncodingInput(text))
    resolved = resolve_cell_anchor(
        project,
        CellAnchor(
            binding.sheet_id,
            binding.row_id,
            binding.column_id,
            exclude_exact=anchor.exclude_exact,
            locator=anchor.locator,
        ),
        cancelled=cancelled,
    )
    if resolved.anchor.source != anchor.source:
        raise RetrievalQueryError(
            "source_anchor_changed", "The selected source has changed. Select it again."
        )
    return resolved
