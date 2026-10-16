"""Resolve current project cells into immutable retrieval producer surfaces.

This module is the authority boundary between mutable project state and the
pure content producers.  Every result is read from one caller-owned snapshot
and carries the exact current-cell revision needed to publish a binding.  It
does not choose a newer prepared set, evidence artifact, or external source.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from frisket.engine.store.prepared_content import PreparedContentStore
from frisket.retrieval.content import (
    PreparedDocumentSurface,
    PreparedPageExtent,
    TextSurface,
)
from frisket.retrieval.contracts import (
    BindingRevision,
    ScopedBinding,
    SourceLocator,
    SourceRevision,
)
from frisket.search_hydration import searchable_text


_SEARCHABLE_COLUMN_TYPES = ("text", "category", "json", "link")
_BLOB_COLUMN_TYPES = ("file", "image", "audio", "video")


class UnsupportedLexicalSourceError(ValueError):
    """The source kind has no authoritative text resolver."""


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _positive_id(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


TextProducerSurface = TextSurface | PreparedDocumentSurface


@dataclass(frozen=True, slots=True)
class ResolvedCellTextSource:
    """One exact logical text value plus immutable producer provenance."""

    sheet_id: int
    binding: BindingRevision
    lexical_text: str
    surface: TextProducerSurface

    def __post_init__(self) -> None:
        _positive_id(self.sheet_id, "sheet_id")
        if not isinstance(self.binding, BindingRevision):
            raise TypeError("binding must be a BindingRevision")
        if not isinstance(self.lexical_text, str) or not self.lexical_text.strip():
            raise ValueError("lexical_text must be nonblank")
        if not isinstance(self.surface, TextSurface | PreparedDocumentSurface):
            raise TypeError("surface must be a text producer surface")
        surface_text = (
            self.surface.text.text
            if isinstance(self.surface, PreparedDocumentSurface)
            else self.surface.text
        )
        if surface_text != self.lexical_text:
            raise ValueError("producer surface must contain the lexical text")

    @property
    def source(self) -> SourceRevision:
        if isinstance(self.surface, PreparedDocumentSurface):
            return self.surface.text.source
        return self.surface.source


@dataclass(frozen=True, slots=True)
class ResolvedCellBlobSource:
    """One exact current blob cell plus its immutable byte identity."""

    sheet_id: int
    binding: BindingRevision
    source: SourceRevision
    blob_hash: str
    mime_type: str
    page_number: int | None = None

    def __post_init__(self) -> None:
        from frisket.retrieval.sources import blob_source_revision

        _positive_id(self.sheet_id, "sheet_id")
        if not isinstance(self.binding, BindingRevision):
            raise TypeError("binding must be a BindingRevision")
        if not isinstance(self.source, SourceRevision):
            raise TypeError("source must be a SourceRevision")
        if not isinstance(self.blob_hash, str) or not self.blob_hash:
            raise ValueError("blob_hash must not be empty")
        if self.source != blob_source_revision(blob_hash=self.blob_hash):
            raise ValueError("source must identify blob_hash bytes")
        if not isinstance(self.mime_type, str) or not self.mime_type.strip():
            raise ValueError("mime_type must not be empty")
        if self.page_number is not None:
            _positive_id(self.page_number, "page_number")


def resolve_cell_blob_source(
    project: Any,
    *,
    sheet_id: int,
    row_id: int,
    column_id: int,
) -> ResolvedCellBlobSource | None:
    """Resolve one visible current media cell to its canonical blob metadata.

    The result describes authority only.  Consumers materialize and verify the
    bytes for their own call lifetime before decoding or encoding them.
    """

    from frisket.retrieval.sources import blob_source_revision

    selected_sheet = _positive_id(sheet_id, "sheet_id")
    selected_row = _positive_id(row_id, "row_id")
    selected_column = _positive_id(column_id, "column_id")
    current = project.db.execute(
        "SELECT current.retrieval_revision,c.type AS column_type "
        "FROM current_cells current "
        "JOIN columns c ON c.id=current.column_id "
        "JOIN sheets s ON s.id=c.sheet_id "
        "WHERE current.column_id=? AND current.row_id=? "
        "AND c.sheet_id=? AND c.hidden=0 AND s.hidden=0 "
        "AND c.type IN (?,?,?,?)",
        (selected_column, selected_row, selected_sheet, *_BLOB_COLUMN_TYPES),
    ).fetchone()
    if current is None:
        return None
    value = project.get_values(
        selected_sheet,
        selected_column,
        row_ids=[selected_row],
    ).get(selected_row)
    if not isinstance(value, dict):
        return None
    blob_hash = value.get("blob")
    if not isinstance(blob_hash, str):
        return None
    blob = project.db.execute(
        "SELECT hash,mime FROM blobs WHERE hash=?",
        (blob_hash,),
    ).fetchone()
    if blob is None or not isinstance(blob["mime"], str) or not blob["mime"].strip():
        return None
    page_number: int | None = None
    selected_page = value.get("page")
    if selected_page is not None:
        if (
            str(current["column_type"]) != "file"
            or type(selected_page) is not int
            or selected_page <= 0
        ):
            return None
        page_number = selected_page
    source = blob_source_revision(blob_hash=blob_hash)
    return ResolvedCellBlobSource(
        sheet_id=selected_sheet,
        binding=BindingRevision(
            row_id=selected_row,
            column_id=selected_column,
            retrieval_revision=int(current["retrieval_revision"]),
        ),
        source=source,
        blob_hash=blob_hash,
        mime_type=str(blob["mime"]),
        page_number=page_number,
    )


def _plain_surface(
    *, row_id: int, column_id: int, retrieval_revision: int, text: str
) -> TextSurface:
    source_id = f"cell:{row_id}:{column_id}"
    content_hash = _sha256_text(text)
    source = SourceRevision(
        source_id=source_id,
        source_kind="text",
        version_kind="retrieval_revision",
        version_value=retrieval_revision,
        content_hash=content_hash,
    )
    return TextSurface(
        source=source,
        text_surface_id=source_id,
        content_hash=content_hash,
        text=text,
    )


def _prepared_surface(prepared: Any) -> PreparedDocumentSurface:
    source_id = f"prepared-ref:{prepared.ref_id}"
    pins = [
        (pin.page_number, pin.version_id, pin.content_hash) for pin in prepared.pins
    ]
    pin_payload = json.dumps(
        pins,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    pin_hash = _sha256_bytes(pin_payload)
    content_hash = _sha256_text(prepared.text)
    source = SourceRevision(
        source_id=source_id,
        source_kind="prepared_text",
        version_kind="prepared_page_versions",
        version_value=pin_hash,
        content_hash=content_hash,
    )
    text = TextSurface(
        source=source,
        text_surface_id=source_id,
        content_hash=content_hash,
        text=prepared.text,
    )
    return PreparedDocumentSurface(
        text=text,
        pages=tuple(
            PreparedPageExtent(
                page_number=pin.page_number,
                version_id=pin.version_id,
                content_hash=pin.content_hash,
                start=pin.start,
                end=pin.end,
            )
            for pin in prepared.pins
        ),
    )


def resolve_cell_text_sources(
    project: Any,
    *,
    sheet_id: int,
    column_id: int,
    row_ids: Sequence[int],
) -> dict[int, ResolvedCellTextSource]:
    """Resolve a bounded set of current searchable cells from one snapshot.

    The caller controls batching and the lifetime of ``project``.  Passing a
    ``ProjectReadSnapshot`` pins values, binding revisions, and prepared
    selectors to the same source snapshot.
    """

    selected_sheet = _positive_id(sheet_id, "sheet_id")
    selected_column = _positive_id(column_id, "column_id")
    selected_rows = list(dict.fromkeys(_positive_id(row, "row_id") for row in row_ids))
    if not selected_rows:
        return {}

    descriptor = project.db.execute(
        "SELECT 1 FROM columns c JOIN sheets s ON s.id=c.sheet_id "
        "WHERE c.id=? AND c.sheet_id=? AND c.hidden=0 AND s.hidden=0 "
        "AND c.type IN (?,?,?,?)",
        (selected_column, selected_sheet, *_SEARCHABLE_COLUMN_TYPES),
    ).fetchone()
    if descriptor is None:
        return {}

    placeholders = ",".join("?" for _ in selected_rows)
    rows = project.db.execute(
        "SELECT current.row_id,current.retrieval_revision,logical.prepared_ref_id "
        "FROM current_cells current "
        "JOIN current_cell_values logical ON logical.column_id=current.column_id "
        "AND logical.row_id=current.row_id "
        f"WHERE current.column_id=? AND current.row_id IN ({placeholders})",
        (selected_column, *selected_rows),
    ).fetchall()
    metadata = {
        int(row["row_id"]): (int(row["retrieval_revision"]), row["prepared_ref_id"])
        for row in rows
    }
    if not metadata:
        return {}

    values = project.get_values(
        selected_sheet,
        selected_column,
        row_ids=[row for row in selected_rows if row in metadata],
    )
    prepared_ids = {
        int(ref_id)
        for _revision, ref_id in metadata.values()
        if isinstance(ref_id, int) and not isinstance(ref_id, bool)
    }
    prepared_by_id = PreparedContentStore(project).resolve_many(sorted(prepared_ids))

    resolved: dict[int, ResolvedCellTextSource] = {}
    for row_id in selected_rows:
        if row_id not in metadata:
            continue
        text = searchable_text(values.get(row_id))
        if text is None:
            continue
        retrieval_revision, metadata_ref_id = metadata[row_id]
        ref_id = metadata_ref_id
        if ref_id is None:
            surface: TextProducerSurface = _plain_surface(
                row_id=row_id,
                column_id=selected_column,
                retrieval_revision=retrieval_revision,
                text=text,
            )
        else:
            if not isinstance(ref_id, int) or isinstance(ref_id, bool):
                raise ValueError("prepared_ref_id must be an integer")
            prepared = prepared_by_id.get(ref_id)
            if prepared is None or prepared.text != text:
                raise ValueError(
                    "prepared selector does not match the logical cell text"
                )
            surface = _prepared_surface(prepared)
        resolved[row_id] = ResolvedCellTextSource(
            sheet_id=selected_sheet,
            binding=BindingRevision(
                row_id=row_id,
                column_id=selected_column,
                retrieval_revision=retrieval_revision,
            ),
            lexical_text=text,
            surface=surface,
        )
    return resolved


def _resolved_surface_text(source: ResolvedCellTextSource) -> TextSurface:
    if isinstance(source.surface, PreparedDocumentSurface):
        return source.surface.text
    return source.surface


def _resolve_cell_lexical_text(
    project: Any,
    *,
    source: SourceRevision,
    locator: SourceLocator,
    binding: ScopedBinding,
) -> str | None:
    if binding.column_id is None:
        return None
    resolved = resolve_cell_text_sources(
        project,
        sheet_id=binding.sheet_id,
        column_id=binding.column_id,
        row_ids=[binding.row_id],
    ).get(binding.row_id)
    if (
        resolved is None
        or resolved.source != source
        or resolved.binding not in binding.dependencies
    ):
        return None
    text_range = locator.text
    if text_range is not None:
        surface = _resolved_surface_text(resolved)
        if (
            text_range.text_surface_id != surface.text_surface_id
            or text_range.content_hash != surface.content_hash
            or text_range.end > len(surface.text)
        ):
            return None
    return resolved.lexical_text


def _resolve_transcript_lexical_text(
    project: Any,
    *,
    source: SourceRevision,
    locator: SourceLocator,
    binding: ScopedBinding,
) -> str | None:
    if binding.column_id is None or not locator.source_span_ids:
        return None
    from frisket.engine.executor.temporal_transcripts import (
        resolve_timestamped_transcript,
    )
    from frisket.retrieval.sources import transcript_source_revision

    resolved = resolve_timestamped_transcript(
        project,
        sheet_id=binding.sheet_id,
        row_id=binding.row_id,
        column_id=binding.column_id,
    )
    if resolved is None:
        return None
    expected_source = transcript_source_revision(
        evidence_link_stable_id=resolved.evidence_link_stable_id,
        snapshot_hash=resolved.snapshot_hash,
        text=resolved.transcript_value,
    )
    span_ids = tuple(str(span["stable_id"]) for span in resolved.spans)
    if source != expected_source or locator.source_span_ids != span_ids:
        return None
    return resolved.transcript_value


def _resolve_ocr_lexical_text(
    project: Any,
    *,
    source: SourceRevision,
    locator: SourceLocator,
    binding: ScopedBinding,
) -> str | None:
    if binding.column_id is None or not locator.source_span_ids:
        return None
    from frisket.engine.store.ocr_word_stream import resolve_current_ocr_evidence
    from frisket.retrieval.sources import ocr_source_revision

    candidates = resolve_current_ocr_evidence(
        project,
        sheet_id=binding.sheet_id,
        row_id=binding.row_id,
        column_id=binding.column_id,
    )
    matches: list[str] = []
    for resolved in candidates:
        expected_source = ocr_source_revision(
            evidence_link_stable_id=resolved.evidence_link_stable_id,
            snapshot_hash=resolved.snapshot_hash,
            text=resolved.text_value,
        )
        span_ids = tuple(str(span["stable_id"]) for span in resolved.spans)
        if source == expected_source and locator.source_span_ids == span_ids:
            matches.append(resolved.text_value)
    return matches[0] if len(matches) == 1 else None


def resolve_lexical_source_text(
    project: Any,
    *,
    source: SourceRevision,
    locator: SourceLocator,
    binding: ScopedBinding,
) -> str | None:
    """Resolve one exact lexical surface from the query's pinned snapshot.

    Authority selection lives here so query execution does not grow source-kind
    conditionals or choose a newer artifact.  A known source whose exact
    revision no longer resolves returns ``None``.  A source kind without a
    resolver raises explicitly instead of silently pretending search coverage.
    """

    if source.source_kind in {"text", "prepared_text"}:
        return _resolve_cell_lexical_text(
            project, source=source, locator=locator, binding=binding
        )
    if source.source_kind == "transcript":
        return _resolve_transcript_lexical_text(
            project, source=source, locator=locator, binding=binding
        )
    if source.source_kind == "ocr_text":
        return _resolve_ocr_lexical_text(
            project, source=source, locator=locator, binding=binding
        )
    raise UnsupportedLexicalSourceError(
        f"lexical hydration does not support source kind {source.source_kind!r}"
    )


__all__ = [
    "ResolvedCellBlobSource",
    "ResolvedCellTextSource",
    "TextProducerSurface",
    "resolve_cell_blob_source",
    "resolve_cell_text_sources",
    "resolve_lexical_source_text",
    "UnsupportedLexicalSourceError",
]
