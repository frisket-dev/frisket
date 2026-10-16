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
from frisket.retrieval.contracts import SourceRevision
from frisket.search_hydration import searchable_text


_SEARCHABLE_COLUMN_TYPES = ("text", "category", "json", "link")


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _positive_id(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class CellBindingRevision:
    """The live project cell revision required by one contextual binding."""

    row_id: int
    column_id: int
    retrieval_revision: int

    def __post_init__(self) -> None:
        _positive_id(self.row_id, "row_id")
        _positive_id(self.column_id, "column_id")
        _positive_id(self.retrieval_revision, "retrieval_revision")


TextProducerSurface = TextSurface | PreparedDocumentSurface


@dataclass(frozen=True, slots=True)
class ResolvedCellTextSource:
    """One exact logical text value plus immutable producer provenance."""

    sheet_id: int
    binding: CellBindingRevision
    lexical_text: str
    surface: TextProducerSurface

    def __post_init__(self) -> None:
        _positive_id(self.sheet_id, "sheet_id")
        if not isinstance(self.binding, CellBindingRevision):
            raise TypeError("binding must be a CellBindingRevision")
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
            binding=CellBindingRevision(
                row_id=row_id,
                column_id=selected_column,
                retrieval_revision=retrieval_revision,
            ),
            lexical_text=text,
            surface=surface,
        )
    return resolved


__all__ = [
    "CellBindingRevision",
    "ResolvedCellTextSource",
    "TextProducerSurface",
    "resolve_cell_text_sources",
]
