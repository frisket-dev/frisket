"""Read-scope grants proven from action receipts and current project state."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from frisket.contracts.action import ActionOutput, Receipt
from frisket.engine.executor import action_receipts
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore


UnavailableReason = Literal[
    "receipt_not_found",
    "receipt_invalid",
    "receipt_not_completed",
    "output_not_supported",
    "output_not_current",
]


@dataclass(frozen=True)
class OutputReadGrant:
    """One private addition to a Project Ask turn's frozen read scope.

    ``None`` means the whole axis. A materialized-sheet grant therefore has
    both axes set to ``None``; a generated-column grant names exact columns
    and rows.
    """

    receipt_id: str
    sheet_id: int
    column_ids: frozenset[int] | None
    row_ids: frozenset[int] | None


@dataclass(frozen=True)
class UnavailableOutput:
    """A receipt or output that cannot safely expand the current read scope."""

    receipt_id: str
    output_name: str | None
    reason: UnavailableReason


@dataclass(frozen=True)
class OutputGrantResolution:
    grants: tuple[OutputReadGrant, ...]
    unavailable: tuple[UnavailableOutput, ...]


def _positive_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _exact_row_ids(output: ActionOutput) -> tuple[int, ...] | None:
    raw_rows = output.ref.get("row_ids")
    if not isinstance(raw_rows, list) or raw_rows != output.row_ids:
        return None
    rows = tuple(raw_rows)
    if not rows or any(_positive_int(row_id) is None for row_id in rows):
        return None
    if len(rows) != len(set(rows)):
        return None
    return rows


def _column_coordinates(
    receipt: Receipt, output: ActionOutput
) -> tuple[int, int, int, int, tuple[int, ...]] | None:
    ref_kind = output.ref.get("kind")
    if (
        output.kind != "column"
        or not isinstance(ref_kind, str)
        or not (ref_kind == "map_result_column" or ref_kind.endswith("_output_column"))
    ):
        return None
    sheet_id = _positive_int(output.ref.get("sheet_id"))
    column_id = _positive_int(output.ref.get("column_id"))
    run_id = _positive_int(output.ref.get("run_id"))
    op_id = _positive_int(output.ref.get("op_id"))
    row_ids = _exact_row_ids(output)
    if None in (sheet_id, column_id, run_id, op_id) or row_ids is None:
        return None
    assert sheet_id is not None
    assert column_id is not None
    assert run_id is not None
    assert op_id is not None
    if receipt.run_id != run_id or op_id not in receipt.op_ids:
        return None
    return sheet_id, column_id, run_id, op_id, row_ids


def _column_is_current(
    project: Project,
    *,
    sheet_id: int,
    column_id: int,
    run_id: int,
    op_id: int,
    row_ids: tuple[int, ...],
) -> bool:
    owner = project.db.execute(
        "SELECT 1 FROM runs run "
        "JOIN ops op ON op.id=run.op_id "
        "JOIN sheets sheet ON sheet.id=run.sheet_id "
        "JOIN columns column_def ON column_def.id=? "
        "WHERE run.id=? AND run.op_id=? AND run.sheet_id=? "
        "AND run.status='completed' AND op.status='applied' "
        "AND sheet.hidden=0 AND column_def.sheet_id=run.sheet_id "
        "AND column_def.hidden=0",
        (column_id, run_id, op_id, sheet_id),
    ).fetchone()
    if owner is None:
        return False

    requested_rows = json.dumps(row_ids, separators=(",", ":"))
    stale = project.db.execute(
        "SELECT EXISTS("
        "SELECT 1 FROM json_each(?) requested "
        "LEFT JOIN rows row_def ON row_def.id=requested.value "
        "LEFT JOIN current_cells cell ON cell.row_id=requested.value "
        "AND cell.column_id=? "
        "WHERE row_def.id IS NULL OR row_def.sheet_id!=? OR row_def.hidden!=0 "
        "OR cell.row_id IS NULL OR cell.origin_kind!='run_result' "
        "OR cell.origin_run_id!=? OR cell.origin_op_id!=?"
        ")",
        (requested_rows, column_id, sheet_id, run_id, op_id),
    ).fetchone()
    return not bool(stale[0])


def _materialized_sheet_coordinates(
    receipt: Receipt, output: ActionOutput
) -> tuple[int, int] | None:
    if output.kind != "sheet" or output.ref.get("kind") == "source_sheet":
        return None
    sheet_id = _positive_int(output.ref.get("sheet_id"))
    op_id = _positive_int(output.ref.get("op_id"))
    if sheet_id is None or op_id is None or op_id not in receipt.op_ids:
        return None
    return sheet_id, op_id


def _materialized_sheet_is_current(
    project: Project, *, sheet_id: int, op_id: int
) -> bool:
    return (
        project.db.execute(
            "SELECT 1 FROM sheets sheet JOIN ops op ON op.id=sheet.parent_op_id "
            "WHERE sheet.id=? AND sheet.parent_op_id=? AND sheet.hidden=0 "
            "AND op.status='applied'",
            (sheet_id, op_id),
        ).fetchone()
        is not None
    )


def _unavailable(
    receipt_id: str, output_name: str | None, reason: UnavailableReason
) -> UnavailableOutput:
    return UnavailableOutput(
        receipt_id=receipt_id,
        output_name=output_name,
        reason=reason,
    )


def derive_output_read_grants(
    project: Project, receipt_ids: Iterable[str]
) -> OutputGrantResolution:
    """Resolve operation-owned receipt IDs into narrow, current read grants.

    The caller owns the operation-to-receipt association. This boundary trusts
    only the supplied IDs, then independently proves each persisted output
    against the current project projection before returning a private grant.
    """

    grants: list[OutputReadGrant] = []
    unavailable: list[UnavailableOutput] = []
    store = ReceiptStore(project)
    seen_receipts: set[str] = set()
    for supplied_id in receipt_ids:
        receipt_id = str(supplied_id)
        if receipt_id in seen_receipts:
            continue
        seen_receipts.add(receipt_id)
        try:
            receipt = store.parsed_by_id(receipt_id)
        except (TypeError, ValueError):
            unavailable.append(_unavailable(receipt_id, None, "receipt_invalid"))
            continue
        if receipt is None:
            unavailable.append(_unavailable(receipt_id, None, "receipt_not_found"))
            continue
        if receipt.status != "completed":
            unavailable.append(_unavailable(receipt_id, None, "receipt_not_completed"))
            continue
        try:
            result = action_receipts._result_from_receipt(receipt)  # noqa: SLF001
        except (TypeError, ValueError):
            unavailable.append(_unavailable(receipt_id, None, "receipt_invalid"))
            continue

        receipt_grants: list[OutputReadGrant] = []
        receipt_unavailable: list[tuple[ActionOutput, UnavailableOutput]] = []
        for output in result.outputs:
            sheet_coordinates = _materialized_sheet_coordinates(receipt, output)
            if sheet_coordinates is not None:
                sheet_id, op_id = sheet_coordinates
                if _materialized_sheet_is_current(
                    project, sheet_id=sheet_id, op_id=op_id
                ):
                    receipt_grants.append(
                        OutputReadGrant(
                            receipt_id=receipt_id,
                            sheet_id=sheet_id,
                            column_ids=None,
                            row_ids=None,
                        )
                    )
                else:
                    receipt_unavailable.append(
                        (
                            output,
                            _unavailable(receipt_id, output.name, "output_not_current"),
                        )
                    )
                continue

            column_coordinates = _column_coordinates(receipt, output)
            if column_coordinates is None:
                receipt_unavailable.append(
                    (
                        output,
                        _unavailable(receipt_id, output.name, "output_not_supported"),
                    )
                )
                continue
            sheet_id, column_id, run_id, op_id, row_ids = column_coordinates
            if not _column_is_current(
                project,
                sheet_id=sheet_id,
                column_id=column_id,
                run_id=run_id,
                op_id=op_id,
                row_ids=row_ids,
            ):
                receipt_unavailable.append(
                    (
                        output,
                        _unavailable(receipt_id, output.name, "output_not_current"),
                    )
                )
                continue
            receipt_grants.append(
                OutputReadGrant(
                    receipt_id=receipt_id,
                    sheet_id=sheet_id,
                    column_ids=frozenset({column_id}),
                    row_ids=frozenset(row_ids),
                )
            )

        whole_sheets = {
            grant.sheet_id
            for grant in receipt_grants
            if grant.column_ids is None and grant.row_ids is None
        }
        grants.extend(receipt_grants)
        unavailable.extend(
            entry
            for output, entry in receipt_unavailable
            if output.sheet_id not in whole_sheets
        )

    return OutputGrantResolution(
        grants=tuple(dict.fromkeys(grants)),
        unavailable=tuple(unavailable),
    )
