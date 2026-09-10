"""Generic map-op replay + value-hash helpers (de-duplicated).

These functions were COPY-PASTED verbatim into all 8 map-op family modules
(~180 lines x8). The SDK owns them once; `build_replay_fn` parameterizes by the
op's output-ref kind discriminator and action kind.

`output_columns_replay_error` is the ONE replay checker:
its `scope` keyword derives from the op's `accepted_run_statuses` contract, not
from a separate value-hash knob. "expected" scope (deterministic, completed-only
ops) checks the receipt's row set against the caller's freshly computed expected
row set; "receipt" scope (partial-tolerant ops: the model five, ner,
media.extract_metadata) trusts the receipt's own recorded row set instead, so a
partial run's replay is not penalized for the rows it never touched. Both scopes
run every column-identity check and the REPLAY_EDIT_POLICY == "surface"
edit-forgiveness; a receipt written before this change (no `value_hash`) skips
only the value comparison, not the identity/row checks (legacy tolerance).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from frisket.contracts.action import ActionError, Receipt
from frisket.sdk.replay_policy import REPLAY_EDIT_POLICY

_SQLITE_ID_CHUNK_SIZE = 900


def _replay_output_column_row(
    project: Any,
    receipt: Receipt,
    *,
    column_id: int,
    sheet_id: int,
    allow_hidden: bool = False,
) -> Any | None:
    """Look up a replay target output column, tolerating the all-rows-
    failed-no-columns-v1 zero-success hide: a column is still a legitimate
    replay target when it's hidden ONLY because its creating op's run
    produced zero successful rows (``zero_success_columns``) AND that op is
    still 'applied' (not undone since — an undo re-hides the same column for
    an unrelated reason and must still read as stale). Any other hidden
    column — undone, manually hidden, or anything else — is treated exactly
    as before this fix: missing, i.e. stale. Returns None (missing) in the
    truly-absent and genuinely-stale-hidden cases; callers treat None
    exactly as the old ``hidden=0`` filter did. Selects ``*`` (not a fixed
    column list) so every caller — including the hand-written per-op replay
    checks that read extra columns like ``format`` — can pull whatever
    field it needs from the same row."""
    row = project.db.execute(
        "SELECT * FROM columns WHERE id=? AND sheet_id=?",
        (column_id, sheet_id),
    ).fetchone()
    if row is None:
        return None
    if row["hidden"]:
        op_ids = receipt.op_ids
        if not op_ids:
            return None
        placeholders = ",".join("?" for _ in op_ids)
        rows = project.db.execute(
            f"SELECT id, status, undo_info FROM ops WHERE id IN ({placeholders})",
            op_ids,
        ).fetchall()
        zero_success_hidden = False
        for op_row in rows:
            try:
                info = json.loads(op_row["undo_info"] or "{}")
            except (TypeError, ValueError):
                continue
            if column_id in {int(c) for c in info.get("zero_success_columns", [])}:
                zero_success_hidden = True
                break
        if not zero_success_hidden and not (allow_hidden and row["ai_generated"]):
            return None
        by_id = {int(op_row["id"]): str(op_row["status"]) for op_row in rows}
        if not all(by_id.get(op_id) == "applied" for op_id in op_ids):
            return None
    return row


def output_column_value_hash(
    project: Any, *, sheet_id: int, column_id: int, row_ids: list[int]
) -> str:
    return _output_column_value_hash_streaming(
        project,
        sheet_id=sheet_id,
        column_id=column_id,
        row_ids=row_ids,
        apply_edits=True,
    )


def _output_column_value_hash_streaming(
    project: Any,
    *,
    sheet_id: int,
    column_id: int,
    row_ids: list[int],
    apply_edits: bool,
    page_size: int = 128,
) -> str:
    """Hash the canonical row array without materializing the full column.

    The emitted bytes are exactly ``json.dumps(ordered, sort_keys=True,
    separators=(",", ":"), allow_nan=False)`` from the original implementation,
    but only one bounded page of values and one encoded row exist at a time.
    This matters for metadata details columns, where 10,000 valid rows can each
    approach 64 KiB.
    """

    digest = hashlib.sha256()
    digest.update(b"[")
    first = True
    batch_size = max(1, int(page_size))
    for offset in range(0, len(row_ids), batch_size):
        page = [int(row_id) for row_id in row_ids[offset : offset + batch_size]]
        values = project.get_values(
            sheet_id,
            column_id,
            row_ids=page,
            apply_edits=apply_edits,
        )
        for row_id in page:
            if not first:
                digest.update(b",")
            first = False
            encoded = json.dumps(
                {"row_id": row_id, "value": values.get(row_id)},
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            digest.update(encoded)
    digest.update(b"]")
    return "sha256:" + digest.hexdigest()


def output_column_result_value_hash(
    project: Any, *, sheet_id: int, column_id: int, row_ids: list[int]
) -> str:
    """Value hash of the UNDERLYING current-run result values, with the manual
    edit overlay stripped (replay-accept-surface-v1). Byte-identical to
    ``output_column_value_hash`` except it resolves ``apply_edits=False``, so
    under REPLAY_EDIT_POLICY == "surface" a value mismatch that is purely the
    result of human edits (this hash still matches the receipt evidence) is
    surfaced rather than hard-erroring, while structural drift (which moves this
    hash too) stays a real ``stale_replay``."""
    return _output_column_value_hash_streaming(
        project,
        sheet_id=sheet_id,
        column_id=column_id,
        row_ids=row_ids,
        apply_edits=False,
    )


def replay_expected_row_ids(
    project: Any, *, sheet_id: int, requested_row_ids: list[int] | None
) -> list[int]:
    if requested_row_ids is not None:
        return list(requested_row_ids)
    return project.visible_row_ids(sheet_id)


def replay_output_column_identity(
    project: Any,
    receipt: Receipt,
    output: Any,
    *,
    action_kind: str,
    error_code: str = "stale_replay",
    compare: tuple[str, ...] = ("name", "type"),
    allow_hidden: bool = False,
) -> Any | ActionError:
    """Resolve one replay output's live column and check shared identity facts.

    This deliberately stops before run, row-scope, value-hash, blob, and
    action-specific evidence checks. Those policies differ across replay
    consumers. Ref shape and column existence are always checked; callers choose
    whether name and type belong to their replay contract.
    """
    ref = output.ref
    column_id = ref.get("column_id")
    sheet_id = ref.get("sheet_id")
    if not isinstance(column_id, int) or not isinstance(sheet_id, int):
        problem = "receipt has invalid output refs"
        extra: dict[str, Any] = {}
    else:
        row = _replay_output_column_row(
            project,
            receipt,
            column_id=column_id,
            sheet_id=sheet_id,
            allow_hidden=allow_hidden,
        )
        if row is None:
            problem = "output column is missing"
            extra = {"column_id": column_id, "sheet_id": sheet_id}
        else:
            labels = {"name": "was renamed", "type": "type changed"}
            changed = next(
                (field for field in compare if row[field] != ref.get(field)), None
            )
            if changed is None:
                return row
            problem = f"output column {labels[changed]}"
            extra = {
                "column_id": column_id,
                f"expected_{changed}": ref.get(changed),
                f"current_{changed}": row[changed],
            }
    return ActionError(
        code=error_code,
        message=f"{action_kind} replay {problem}",
        action_kind=receipt.action_kind,
        details={"receipt_id": receipt.receipt_id, "output": output.name, **extra},
    )


def output_columns_replay_error(
    project: Any,
    receipt: Receipt,
    *,
    output_kind: str,
    action_kind: str,
    scope: str = "expected",
    expected_row_ids: list[int] | None = None,
    compare_declared_format: bool = False,
    error_code: str = "stale_replay",
    allow_empty_rows: bool = False,
    hidden_output_kind: str | None = None,
) -> ActionError | None:
    """The one replay checker.

    ``scope="expected"`` (deterministic, completed-only ops): the receipt's row
    set must equal the caller-computed ``expected_row_ids`` (today's strict
    behavior). ``scope="receipt"`` (partial-tolerant ops: their
    ``accepted_run_statuses`` includes more than just "completed"): the row and
    value checks run against the ref's OWN recorded ``row_ids`` instead —
    ``expected_row_ids`` is ignored — so a partial run's replay is not
    penalized for rows it never touched. Both scopes run every column-identity
    check (name/type/run/declared-format) and the REPLAY_EDIT_POLICY ==
    "surface" edit-forgiveness identically. ``error_code`` is used for every
    ActionError this function emits; callers whose op has its own
    ``map_error_code`` (receipt-scoped ops) pass it through instead of the
    "stale_replay" default.

    Legacy tolerance: a ref with no (or empty) ``value_hash`` — a receipt
    written before receipts always carried one — skips ONLY the value
    comparison; every identity and row-scope check above still runs. New
    receipts always carry a hash (`sdk/capture.py`), so this tolerance decays
    naturally as old receipts age out.
    """
    if scope not in ("expected", "receipt"):
        raise ValueError(f"output_columns_replay_error: unknown scope {scope!r}")
    seen_output = False
    from frisket.contracts.action import ReceiptIO

    outputs = list(receipt.outputs)
    if hidden_output_kind is not None:
        outputs.extend(
            ReceiptIO(name=item.ref["name"], ref=item.ref)
            for item in receipt.evidence
            if item.ref.get("kind") == hidden_output_kind
        )
    for output in outputs:
        ref = output.ref
        hidden = (
            hidden_output_kind is not None and ref.get("kind") == hidden_output_kind
        )
        if ref.get("kind") != output_kind and not hidden:
            continue
        seen_output = True
        column_id = ref.get("column_id")
        sheet_id = ref.get("sheet_id")
        row = replay_output_column_identity(
            project,
            receipt,
            output,
            action_kind=action_kind,
            error_code=error_code,
            allow_hidden=hidden,
        )
        if isinstance(row, ActionError):
            return row
        # Treat `format` as operation policy only when the ref opts in.
        if (
            compare_declared_format
            and "format" in ref
            and row["format"] != ref.get("format")
        ):
            return ActionError(
                code=error_code,
                message=f"{action_kind} replay output column format changed",
                action_kind=receipt.action_kind,
                details={
                    "receipt_id": receipt.receipt_id,
                    "output": output.name,
                    "column_id": column_id,
                    "expected_format": ref.get("format"),
                    "current_format": row["format"],
                },
            )
        run_id = ref.get("run_id")
        if not isinstance(run_id, int):
            return ActionError(
                code=error_code,
                message=f"{action_kind} replay output column changed runs",
                action_kind=receipt.action_kind,
                details={
                    "receipt_id": receipt.receipt_id,
                    "output": output.name,
                    "column_id": column_id,
                    "expected_run_id": run_id,
                    "current_run_id": row["current_run_id"],
                },
            )
        from frisket.engine.store.result_generations import ResultGenerationStore

        generations = ResultGenerationStore(project)
        generation_managed = generations.is_generation_managed(column_id)
        if generation_managed:
            binding = generations.get_binding(run_id, column_id)
            if binding is None or binding.state != "sealed":
                return ActionError(
                    code=error_code,
                    message=f"{action_kind} replay output column changed runs",
                    action_kind=receipt.action_kind,
                    details={
                        "receipt_id": receipt.receipt_id,
                        "output": output.name,
                        "column_id": column_id,
                        "expected_run_id": run_id,
                        "binding_state": None if binding is None else binding.state,
                        "current_head_run_ids": generations.origin_run_ids(
                            column_id, limit=2
                        ),
                    },
                )
        elif row["current_run_id"] != run_id:
            return ActionError(
                code=error_code,
                message=f"{action_kind} replay output column changed runs",
                action_kind=receipt.action_kind,
                details={
                    "receipt_id": receipt.receipt_id,
                    "output": output.name,
                    "column_id": column_id,
                    "expected_run_id": run_id,
                    "current_run_id": row["current_run_id"],
                },
            )
        row_ids = ref.get("row_ids")
        if not isinstance(row_ids, list) or not all(
            isinstance(row_id, int) for row_id in row_ids
        ):
            return ActionError(
                code=error_code,
                message=f"{action_kind} replay receipt has invalid output rows",
                action_kind=receipt.action_kind,
                details={"receipt_id": receipt.receipt_id, "output": output.name},
            )
        if not row_ids and not allow_empty_rows:
            return ActionError(
                code=error_code,
                message=f"{action_kind} replay receipt has no output rows",
                action_kind=receipt.action_kind,
                details={"receipt_id": receipt.receipt_id, "output": output.name},
            )
        visible_rows: set[int] = set()
        for start in range(0, len(row_ids), _SQLITE_ID_CHUNK_SIZE):
            chunk = row_ids[start : start + _SQLITE_ID_CHUNK_SIZE]
            placeholders = ",".join("?" for _ in chunk)
            visible_rows.update(
                int(row["id"])
                for row in project.db.execute(
                    "SELECT id FROM rows WHERE sheet_id=? AND hidden=0 "
                    f"AND id IN ({placeholders})",
                    [sheet_id, *chunk],
                ).fetchall()
            )
        if visible_rows != set(row_ids):
            return ActionError(
                code=error_code,
                message=f"{action_kind} replay output rows are missing",
                action_kind=receipt.action_kind,
                details={
                    "receipt_id": receipt.receipt_id,
                    "output": output.name,
                    "missing_row_ids": sorted(set(row_ids) - visible_rows),
                },
            )
        if scope == "expected" and set(row_ids) != set(expected_row_ids or ()):
            return ActionError(
                code=error_code,
                message=f"{action_kind} replay output row scope changed",
                action_kind=receipt.action_kind,
                details={
                    "receipt_id": receipt.receipt_id,
                    "output": output.name,
                    "receipt_row_ids": row_ids,
                    "expected_row_ids": expected_row_ids,
                },
            )
        if generation_managed:
            heads = generations.read_cell_heads(column_id, row_ids=row_ids)
            missing_head_row_ids = [
                row_id
                for row_id in row_ids
                if row_id not in heads or heads[row_id].run_id != run_id
            ]
            if missing_head_row_ids:
                return ActionError(
                    code=error_code,
                    message=f"{action_kind} replay output column changed runs",
                    action_kind=receipt.action_kind,
                    details={
                        "receipt_id": receipt.receipt_id,
                        "output": output.name,
                        "column_id": column_id,
                        "expected_run_id": run_id,
                        "changed_row_ids": missing_head_row_ids,
                        "current_head_run_ids": generations.origin_run_ids(
                            column_id,
                            row_ids,
                            limit=2,
                        ),
                    },
                )
        result_rows: set[int] = set()
        for start in range(0, len(row_ids), _SQLITE_ID_CHUNK_SIZE):
            chunk = row_ids[start : start + _SQLITE_ID_CHUNK_SIZE]
            placeholders = ",".join("?" for _ in chunk)
            result_rows.update(
                int(row["row_id"])
                for row in project.db.execute(
                    "SELECT row_id FROM results "
                    f"WHERE run_id=? AND column_id=? AND row_id IN ({placeholders})",
                    [run_id, column_id, *chunk],
                ).fetchall()
            )
        if result_rows != set(row_ids):
            return ActionError(
                code=error_code,
                message=f"{action_kind} replay output result rows are missing",
                action_kind=receipt.action_kind,
                details={
                    "receipt_id": receipt.receipt_id,
                    "output": output.name,
                    "missing_row_ids": sorted(set(row_ids) - result_rows),
                },
            )
        expected_value_hash = ref.get("value_hash")
        if not isinstance(expected_value_hash, str) or not expected_value_hash:
            # Legacy tolerance: a receipt written before receipts always
            # carried a value_hash. Identity/row-scope checks above already
            # ran; skip only the value comparison for this output.
            continue
        actual_value_hash = output_column_value_hash(
            project, sheet_id=sheet_id, column_id=column_id, row_ids=row_ids
        )
        if actual_value_hash != expected_value_hash:
            # REPLAY_EDIT_POLICY == "surface": a value
            # mismatch caused ONLY by human edits since write is surfaced, not a
            # hard error. The underlying current-run RESULT (edit overlay
            # stripped) still hashes to the receipt's evidence iff nothing but
            # edits changed; structural staleness moves the raw hash too and
            # stays a real stale_replay.
            if REPLAY_EDIT_POLICY == "surface":
                result_value_hash = output_column_result_value_hash(
                    project, sheet_id=sheet_id, column_id=column_id, row_ids=row_ids
                )
                if result_value_hash == expected_value_hash:
                    continue
            return ActionError(
                code=error_code,
                message=f"{action_kind} replay output values changed",
                action_kind=receipt.action_kind,
                details={
                    "receipt_id": receipt.receipt_id,
                    "output": output.name,
                    "column_id": column_id,
                    "expected_value_hash": expected_value_hash,
                    "actual_value_hash": actual_value_hash,
                },
            )
    if not seen_output:
        return ActionError(
            code=error_code,
            message=f"{action_kind} replay receipt has no output column ref",
            action_kind=receipt.action_kind,
            details={"receipt_id": receipt.receipt_id},
        )
    return None
