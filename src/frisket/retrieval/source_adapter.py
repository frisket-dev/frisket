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
    TranscriptSegment,
    TranscriptSurface,
)
from frisket.retrieval.contracts import (
    BindingRevision,
    PageRange,
    ScopedBinding,
    SourceLocator,
    SourceRevision,
)
from frisket.search_hydration import searchable_text


# The logical cell's producer, not its name or JSON shape, identifies OCR boxes.
_OCR_METADATA_PREDICATE = (
    "EXISTS(SELECT 1 FROM run_output_generations generation "
    "JOIN runs run ON run.id=generation.run_id "
    "WHERE generation.run_id=logical.origin_run_id "
    "AND generation.column_id=logical.column_id "
    "AND run.action_kind='media.ocr' "
    "AND generation.output_role=json_extract(run.params,'$.output_names.blocks'))"
)


def resolve_ocr_metadata_cells(
    project: Any, *, sheet_id: int, row_ids: Sequence[int]
) -> frozenset[tuple[int, int]]:
    """Identify generated geometry for implicit content reads; keep explicit access."""
    if not row_ids:
        return frozenset()
    rows = project.db.execute(
        "SELECT logical.row_id,logical.column_id FROM current_cell_values logical "
        "JOIN rows r ON r.id=logical.row_id WHERE r.sheet_id=? "
        "AND logical.row_id IN (SELECT value FROM json_each(?)) AND "
        + _OCR_METADATA_PREDICATE,
        (sheet_id, json.dumps(list(row_ids))),
    )
    return frozenset((int(row[0]), int(row[1])) for row in rows)


TEXT_SOURCE_COLUMN_TYPES = ("text", "category", "json", "link")
BLOB_SOURCE_COLUMN_TYPES = ("file", "image", "audio", "video")
EVIDENCE_SOURCE_COLUMN_TYPES = ("timestamped_transcript",)
RETRIEVAL_SOURCE_COLUMN_TYPES = (
    *TEXT_SOURCE_COLUMN_TYPES,
    *BLOB_SOURCE_COLUMN_TYPES,
    *EVIDENCE_SOURCE_COLUMN_TYPES,
)


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
        from frisket.retrieval.sources import (
            blob_page_source_revision,
            blob_source_revision,
        )

        _positive_id(self.sheet_id, "sheet_id")
        if not isinstance(self.binding, BindingRevision):
            raise TypeError("binding must be a BindingRevision")
        if not isinstance(self.source, SourceRevision):
            raise TypeError("source must be a SourceRevision")
        if not isinstance(self.blob_hash, str) or not self.blob_hash:
            raise ValueError("blob_hash must not be empty")
        expected_source = (
            blob_source_revision(blob_hash=self.blob_hash)
            if self.page_number is None
            else blob_page_source_revision(
                blob_hash=self.blob_hash, page_number=self.page_number
            )
        )
        if self.source != expected_source:
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

    from frisket.retrieval.sources import (
        blob_page_source_revision,
        blob_source_revision,
    )

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
        (selected_column, selected_row, selected_sheet, *BLOB_SOURCE_COLUMN_TYPES),
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
    source = (
        blob_source_revision(blob_hash=blob_hash)
        if page_number is None
        else blob_page_source_revision(blob_hash=blob_hash, page_number=page_number)
    )
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


@dataclass(frozen=True, slots=True)
class ResolvedCellTranscriptSource:
    sheet_id: int
    binding: BindingRevision
    lexical_text: str
    surface: TranscriptSurface

    @property
    def source(self) -> SourceRevision:
        return self.surface.source


@dataclass(frozen=True, slots=True)
class ResolvedCellOcrTextSource:
    sheet_id: int
    binding: BindingRevision
    lexical_text: str
    surface: TextSurface

    @property
    def source(self) -> SourceRevision:
        return self.surface.source


def _binding_revision(
    project: Any, *, row_id: int, column_id: int
) -> BindingRevision | None:
    row = project.db.execute(
        "SELECT retrieval_revision FROM current_cells WHERE row_id=? AND column_id=?",
        (row_id, column_id),
    ).fetchone()
    if row is None:
        return None
    return BindingRevision(row_id, column_id, int(row["retrieval_revision"]))


def _cell_uses_prepared_text(project: Any, *, row_id: int, column_id: int) -> bool:
    """Return whether the logical cell already owns a prepared text surface.

    OCR evidence attached to such a cell supplies grounding for that prepared
    surface. It is not a second independently searchable copy of the text.
    """

    row = project.db.execute(
        "SELECT prepared_ref_id FROM current_cell_values "
        "WHERE row_id=? AND column_id=?",
        (row_id, column_id),
    ).fetchone()
    return row is not None and row["prepared_ref_id"] is not None


def resolve_cell_transcript_source(
    project: Any, *, sheet_id: int, row_id: int, column_id: int
) -> ResolvedCellTranscriptSource | None:
    """Resolve one exact current timestamped transcript producer surface."""

    from frisket.engine.executor.temporal_transcripts import (
        resolve_timestamped_transcript,
    )
    from frisket.retrieval.sources import transcript_source_revision

    resolved = resolve_timestamped_transcript(
        project, sheet_id=sheet_id, row_id=row_id, column_id=column_id
    )
    binding = _binding_revision(project, row_id=row_id, column_id=column_id)
    if resolved is None or binding is None:
        return None
    source = transcript_source_revision(
        evidence_link_stable_id=resolved.evidence_link_stable_id,
        snapshot_hash=resolved.snapshot_hash,
        text=resolved.transcript_value,
    )
    segments = tuple(
        TranscriptSegment(
            source_span_id=str(span["stable_id"]),
            start_ms=int(span["start_ms"]),
            end_ms=int(span["end_ms"]),
            text=str(span["quote"]),
            segment_index=index,
        )
        for index, span in enumerate(resolved.spans)
    )
    if not segments:
        return None
    return ResolvedCellTranscriptSource(
        sheet_id=sheet_id,
        binding=binding,
        lexical_text=resolved.transcript_value,
        surface=TranscriptSurface(source, segments),
    )


def resolve_cell_ocr_text_source(
    project: Any, *, sheet_id: int, row_id: int, column_id: int
) -> ResolvedCellOcrTextSource | None:
    """Resolve one unambiguous exact current OCR text producer surface."""

    if _cell_uses_prepared_text(project, row_id=row_id, column_id=column_id):
        return None

    from frisket.engine.store.ocr_word_stream import resolve_current_ocr_evidence
    from frisket.retrieval.sources import identity_digest, ocr_source_revision

    candidates = resolve_current_ocr_evidence(
        project, sheet_id=sheet_id, row_id=row_id, column_id=column_id
    )
    binding = _binding_revision(project, row_id=row_id, column_id=column_id)
    if len(candidates) != 1 or binding is None:
        return None
    resolved = candidates[0]
    source = ocr_source_revision(
        evidence_link_stable_id=resolved.evidence_link_stable_id,
        snapshot_hash=resolved.snapshot_hash,
        text=resolved.text_value,
    )
    span_ids = tuple(str(span["stable_id"]) for span in resolved.spans)
    if not span_ids:
        return None
    return ResolvedCellOcrTextSource(
        sheet_id=sheet_id,
        binding=binding,
        lexical_text=resolved.text_value,
        surface=TextSurface(
            source=source,
            text_surface_id=identity_digest(
                "ocr_text_surface",
                {
                    "evidence_link": resolved.evidence_link_stable_id,
                    "snapshot": resolved.snapshot_hash,
                },
            ),
            content_hash=_sha256_text(resolved.text_value),
            text=resolved.text_value,
            source_span_ids=span_ids,
        ),
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


def _prepared_surface(
    prepared: Any,
    *,
    source_span_ids: tuple[str, ...] = (),
    grounding_snapshot_hash: str | None = None,
) -> PreparedDocumentSurface:
    source_id = f"prepared-ref:{prepared.ref_id}"
    pins = [
        (pin.page_number, pin.version_id, pin.content_hash) for pin in prepared.pins
    ]
    version_payload = json.dumps(
        {
            "pins": pins,
            "grounding_snapshot_hash": grounding_snapshot_hash,
            "source_span_ids": source_span_ids,
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    version_hash = _sha256_bytes(version_payload)
    content_hash = _sha256_text(prepared.text)
    source = SourceRevision(
        source_id=source_id,
        source_kind="prepared_text",
        version_kind="prepared_surface",
        version_value=version_hash,
        content_hash=content_hash,
    )
    text = TextSurface(
        source=source,
        text_surface_id=source_id,
        content_hash=content_hash,
        text=prepared.text,
        source_span_ids=source_span_ids,
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


def _prepared_ocr_grounding(
    project: Any,
    *,
    sheet_id: int,
    row_id: int,
    column_id: int,
    text: str,
    source_artifact_id: int | None,
) -> tuple[tuple[str, ...], str | None]:
    """Return grounding owned by one exact current OCR text derivative."""

    from frisket.engine.store.ocr_word_stream import resolve_current_ocr_evidence

    candidates = resolve_current_ocr_evidence(
        project,
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=column_id,
    )
    if (
        len(candidates) != 1
        or candidates[0].text_value != text
        or candidates[0].artifact_id != source_artifact_id
    ):
        return (), None
    selected = candidates[0]
    return (
        tuple(str(span["stable_id"]) for span in selected.spans),
        selected.snapshot_hash,
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
        (selected_column, selected_sheet, *TEXT_SOURCE_COLUMN_TYPES),
    ).fetchone()
    if descriptor is None:
        return {}

    placeholders = ",".join("?" for _ in selected_rows)
    rows = project.db.execute(
        "SELECT current.row_id,current.retrieval_revision,logical.prepared_ref_id,"
        f"{_OCR_METADATA_PREDICATE} AS noncontent_metadata "
        "FROM current_cells current "
        "JOIN current_cell_values logical ON logical.column_id=current.column_id "
        "AND logical.row_id=current.row_id "
        f"WHERE current.column_id=? AND current.row_id IN ({placeholders})",
        (selected_column, *selected_rows),
    ).fetchall()
    metadata = {
        int(row["row_id"]): (
            int(row["retrieval_revision"]),
            row["prepared_ref_id"],
            bool(row["noncontent_metadata"]),
        )
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
        for _revision, ref_id, _noncontent in metadata.values()
        if isinstance(ref_id, int) and not isinstance(ref_id, bool)
    }
    prepared_by_id = PreparedContentStore(project).resolve_many(sorted(prepared_ids))

    resolved: dict[int, ResolvedCellTextSource] = {}
    for row_id in selected_rows:
        if row_id not in metadata:
            continue
        retrieval_revision, metadata_ref_id, noncontent_metadata = metadata[row_id]
        if noncontent_metadata:
            continue
        text = searchable_text(values.get(row_id))
        if text is None:
            continue
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
            source_span_ids, grounding_snapshot_hash = _prepared_ocr_grounding(
                project,
                sheet_id=selected_sheet,
                row_id=row_id,
                column_id=selected_column,
                text=text,
                source_artifact_id=prepared.source_artifact_id,
            )
            surface = _prepared_surface(
                prepared,
                source_span_ids=source_span_ids,
                grounding_snapshot_hash=grounding_snapshot_hash,
            )
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


def resolve_source_text_pages(
    project: Any,
    *,
    source: SourceRevision,
    locator: SourceLocator,
    binding: ScopedBinding,
) -> PageRange | None:
    """Resolve the prepared pages intersecting an exact text location."""

    if (
        source.source_kind != "prepared_text"
        or binding.column_id is None
        or locator.text is None
    ):
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
        or not isinstance(resolved.surface, PreparedDocumentSurface)
    ):
        return None
    text_range = locator.text
    surface = resolved.surface.text
    if (
        text_range.text_surface_id != surface.text_surface_id
        or text_range.content_hash != surface.content_hash
        or text_range.end > len(surface.text)
    ):
        return None
    pages = tuple(
        page.page_number
        for page in resolved.surface.pages
        if page.start < text_range.end and text_range.start < page.end
    )
    if not pages:
        return None
    return PageRange(pages[0], pages[-1])


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
    if source != expected_source or not _is_ordered_span_subset(
        locator.source_span_ids, span_ids
    ):
        return None
    return resolved.transcript_value


def _resolve_ocr_lexical_text(
    project: Any,
    *,
    source: SourceRevision,
    locator: SourceLocator,
    binding: ScopedBinding,
) -> str | None:
    if (
        binding.column_id is None
        or not locator.source_span_ids
        or _cell_uses_prepared_text(
            project, row_id=binding.row_id, column_id=binding.column_id
        )
    ):
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
        if source == expected_source and _is_ordered_span_subset(
            locator.source_span_ids, span_ids
        ):
            matches.append(resolved.text_value)
    return matches[0] if len(matches) == 1 else None


def _is_ordered_span_subset(requested: tuple[str, ...], owned: tuple[str, ...]) -> bool:
    """Accept exact producer subsets without inventing evidence membership."""

    if not requested or len(set(requested)) != len(requested):
        return False
    positions = {stable_id: index for index, stable_id in enumerate(owned)}
    try:
        requested_positions = tuple(positions[stable_id] for stable_id in requested)
    except KeyError:
        return False
    return all(
        current < following
        for current, following in zip(requested_positions, requested_positions[1:])
    )


def resolve_source_text(
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
    "ResolvedCellOcrTextSource",
    "ResolvedCellTextSource",
    "ResolvedCellTranscriptSource",
    "TextProducerSurface",
    "resolve_cell_blob_source",
    "resolve_cell_ocr_text_source",
    "resolve_cell_text_sources",
    "resolve_cell_transcript_source",
    "resolve_ocr_metadata_cells",
    "resolve_source_text",
    "resolve_source_text_pages",
    "UnsupportedLexicalSourceError",
]
