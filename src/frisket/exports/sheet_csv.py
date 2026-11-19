"""Shared visible-sheet CSV export renderer."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from frisket.exports.plan import (
    AttachmentPath,
    SheetExportPlan,
    iter_export_row_batches,
    plan_receipt_payload,
)
from frisket.engine.store import Project


# Compatibility name for callers outside this streaming path.  Local/Solo CSV
# exports intentionally have no total-row ceiling; hosted composition injects
# one through SheetExportLimits.
MAX_FILTERED_SHEET_CSV_EXPORT_ROWS: int | None = None


@dataclass(frozen=True)
class SheetExportLimits:
    """Deployment-owned export policy.  ``None`` means no total cap."""

    max_rows: int | None = None


# Leading characters that spreadsheet software may interpret as a formula when a
# downloaded CSV is opened.
FORMULA_TRIGGER_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _escape_csv_formula(text: str) -> str:
    if text and text[0] in FORMULA_TRIGGER_PREFIXES:
        return "'" + text
    return text


def csv_display(value: Any, *, formula_policy: str = "escape") -> str:
    """Render a typed cell value as a CSV display string.

    ``formula_policy='escape'`` (default) prefixes string cells whose text would
    be treated as a formula with a leading apostrophe; ``'raw'`` preserves bytes.
    Only string values are escaped — a native numeric/boolean cell (e.g. the
    integer ``-1``) is data, not a formula, so it is never apostrophe-prefixed.
    """
    text = csv_cell(value)
    if formula_policy == "escape" and isinstance(value, str):
        return _escape_csv_formula(text)
    return text


def iter_export_csv_rows(
    project: Project,
    plan: SheetExportPlan,
    *,
    formula_policy: str = "escape",
    batch_size: int = 1000,
) -> Iterator[list[str]]:
    """Stream CSV rows (header first) from a shared export plan."""
    yield [
        csv_display(name, formula_policy=formula_policy) for name in plan.column_names
    ]
    for batch in iter_export_row_batches(project, plan, batch_size=batch_size):
        for row in batch.rows:
            yield [csv_display(value, formula_policy=formula_policy) for value in row]


def render_export_csv(
    project: Project,
    plan: SheetExportPlan,
    *,
    formula_policy: str = "escape",
    batch_size: int = 1000,
) -> tuple[dict[str, Any], str]:
    """Render the full CSV for a plan and return (receipt payload, text)."""
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(
        [csv_display(name, formula_policy=formula_policy) for name in plan.column_names]
    )
    exported_row_ids: list[int] = []
    for batch in iter_export_row_batches(project, plan, batch_size=batch_size):
        exported_row_ids.extend(batch.row_ids)
        writer.writerows(
            [csv_display(value, formula_policy=formula_policy) for value in row]
            for row in batch.rows
        )
    return plan_receipt_payload(plan, exported_row_ids), out.getvalue()


def iter_export_csv_bytes(
    project: Project,
    plan: SheetExportPlan,
    *,
    formula_policy: str = "escape",
    batch_size: int = 1000,
    max_chunk_bytes: int = 64 * 1024,
    bom: bool = True,
    on_batch: Callable[[list[int]], None] | None = None,
    attachment_path: AttachmentPath | None = None,
) -> Iterator[bytes]:
    """Incrementally encode a CSV without collecting the rendered corpus.

    Encoded records larger than the preferred chunk size are byte-sliced. CSV
    consumers see the exact same byte stream after concatenation while network
    and disk consumers never receive an oversized buffer.
    """
    pending = bytearray(b"\xef\xbb\xbf" if bom else b"")

    def encoded(row: list[str]) -> bytes:
        out = io.StringIO(newline="")
        csv.writer(out).writerow(row)
        return out.getvalue().encode("utf-8")

    rows: Iterator[list[str]]
    header = [
        csv_display(name, formula_policy=formula_policy) for name in plan.column_names
    ]

    def data_rows() -> Iterator[list[str]]:
        yield header
        for batch in iter_export_row_batches(
            project, plan, batch_size=batch_size, attachment_path=attachment_path
        ):
            if on_batch is not None:
                on_batch(batch.row_ids)
            for row in batch.rows:
                yield [
                    csv_display(value, formula_policy=formula_policy) for value in row
                ]

    rows = data_rows()
    for row in rows:
        item = encoded(row)
        if pending and len(pending) + len(item) > max_chunk_bytes:
            yield bytes(pending)
            pending.clear()
        if len(item) > max_chunk_bytes:
            for start in range(0, len(item), max_chunk_bytes):
                yield item[start : start + max_chunk_bytes]
        else:
            pending.extend(item)
    if pending:
        yield bytes(pending)
