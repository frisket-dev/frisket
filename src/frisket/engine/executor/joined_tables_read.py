"""One-snapshot exact joins with schema inspection before lazy fanout admission."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from pydantic import TypeAdapter, ValidationError

from frisket.actions.join_types import JoinColumnPick, JoinKeyPair, JoinMode
from frisket.actions.types import (
    DynamicOutput,
    DynamicTableResult,
    RowSource,
    SheetRef,
    SheetRows,
    TableColumn,
    TableError,
    TableRow,
)
from frisket.contracts.action import ActionError
from frisket.engine.executor.embedding_read import TableReadRefused
from frisket.engine.executor.joined_table import (
    _pair_mode,
    _row_key,
    _side_read_ids,
    _top_fanout_keys,
    _widen_type,
    iter_join_records,
    join_counts,
)
from frisket.engine.runner.confirmation_context import (
    ParamsScope,
    mint_confirmation_hash,
    scope_confirmation,
)
from frisket.engine.runner.confirmation_echo import refuse_unless_exact_echo


@dataclass(frozen=True)
class JoinRefreshAdmission:
    """Current host invocation, never authorization read from a saved join."""

    action_kind: str
    request_hash: str
    sheet_id: int
    parent_op_id: int
    confirmation: str | None


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _sheet(project, sheet_id, requested=None):
    sheet = project.db.execute(
        "SELECT id, name FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
    ).fetchone()
    if sheet is None:
        raise TableError("invalid_input_ref", "Join source sheet is missing or hidden")
    rows = project.visible_row_ids(sheet_id, requested)
    if requested is not None and set(rows) != set(requested):
        raise TableError(
            "invalid_input_ref", "Selected rows must belong to the left sheet"
        )
    columns = {
        str(column["name"]): {
            "id": int(column["id"]),
            "name": str(column["name"]),
            "type": str(column["type"]),
        }
        for column in project.columns(sheet_id)
    }
    return {"sheet_id": sheet_id, "name": str(sheet["name"]), "row_ids": rows}, columns


def _projection(keys, picks, left, right, indicator):
    """One logical-name authority, including explicit ordered/repeated picks."""
    descriptors = [
        {
            "name": pair.left_column,
            "type": _widen_type(
                left[pair.left_column]["type"], right[pair.right_column]["type"]
            ),
            "role": "key",
            "left_col_id": left[pair.left_column]["id"],
            "right_col_id": right[pair.right_column]["id"],
        }
        for pair in keys
    ]
    key_names = {column["name"] for column in descriptors}
    if picks is None:
        key_sets = {
            "left": {pair.left_column for pair in keys},
            "right": {pair.right_column for pair in keys},
        }
        picks = [
            JoinColumnPick(side=side, column=name)
            for side, columns in (("left", left), ("right", right))
            for name in columns
            if name not in key_sets[side]
        ]
    sides_by_name = {}
    for pick in picks:
        sides_by_name.setdefault(pick.column, set()).add(pick.side)
    natural = key_names | {pick.column for pick in picks}
    if indicator:
        natural.add("_merge")
    # Keep the indicator's stable logical key even if a source is named _merge.
    used = {"_merge"} if indicator else set()

    def name(base, *, generated=False):
        if base not in used and not (generated and base in natural):
            used.add(base)
            return base
        index = 2
        while f"{base}_{index}" in used | natural:
            index += 1
        result = f"{base}_{index}"
        used.add(result)
        return result

    for descriptor in descriptors:
        descriptor["name"] = name(descriptor["name"])
    for pick in picks:
        column = (left if pick.side == "left" else right).get(pick.column)
        if column is None:
            raise TableError("invalid_input_ref", "A projected join column is missing")
        conflict = (
            len(sides_by_name[pick.column]) > 1
            or pick.column in key_names
            or (indicator and pick.column == "_merge")
        )
        base = f"{pick.column}_{pick.side}" if conflict else pick.column
        descriptors.append(
            {
                "name": name(base, generated=conflict),
                "type": column["type"],
                "role": pick.side,
                "source_col_id": column["id"],
            }
        )
    if indicator:
        descriptors.append({"name": "_merge", "type": "text", "role": "indicator"})
    return descriptors


def _read_side(project, sheet, columns, ids):
    by_id = {column["id"]: column for column in columns.values()}
    values = {
        column_id: project.get_values(
            sheet["sheet_id"], column_id, row_ids=sheet["row_ids"]
        )
        for column_id in ids
    }
    facts = {**sheet, "columns": [by_id[column_id] for column_id in ids]}
    facts["snapshot_hash"] = _digest([facts, values])
    return facts, values


def validate_joined_tables_source(project, fact, *, code="stale_replay"):
    """Verify both recorded dependencies, even when no output row used either."""
    try:
        for side in ("left", "right"):
            recorded = fact[side]
            sheet, columns = _sheet(
                project,
                fact[f"{side}_sheet_id"],
                fact["left_requested_row_ids"] if side == "left" else None,
            )
            current, _ = _read_side(
                project,
                sheet,
                columns,
                [column["id"] for column in recorded["columns"]],
            )
            if "column_roster" in recorded:
                current["column_roster"] = list(columns.values())
            if current != recorded:
                raise ValueError("join source changed")
    except (KeyError, TypeError, ValueError, TableError) as error:
        raise TableError(code, "Join source rows, values or columns changed") from error


class AdmittedJoinedTablesReader:
    def __init__(
        self,
        project,
        *,
        scope,
        action_kind,
        request_identity,
        confirmation,
        refresh_admission=None,
    ):
        if not isinstance(scope, SheetRows):
            raise TableError(
                "invalid_input_ref", "Join requires a left sheet_rows scope"
            )
        self.project = project
        self.scope = scope
        self.action_kind = (
            refresh_admission.action_kind
            if refresh_admission is not None
            else action_kind
        )
        self.request_identity = (
            request_identity
            if refresh_admission is None
            else {
                "request_hash": refresh_admission.request_hash,
                "sheet_id": refresh_admission.sheet_id,
                "parent_op_id": refresh_admission.parent_op_id,
                "join": request_identity,
            }
        )
        self.confirmation = (
            confirmation
            if refresh_admission is None
            else refresh_admission.confirmation
        )
        self.parent_sheet_id = scope.sheet_id
        self.sources = set()
        self.source_roles = {}
        self.facts = []
        self._plans = []

    def read(
        self,
        right,
        *,
        join_keys,
        how="inner",
        columns=None,
        indicator=False,
        max_output_rows=1_000_000,
    ):
        # Capability arguments are independently admitted, not recovered by
        # inspecting a particular action's Params object.
        try:
            right = SheetRef.model_validate(right)
            keys = TypeAdapter(list[JoinKeyPair]).validate_python(join_keys)
            picks = (
                None
                if columns is None
                else TypeAdapter(list[JoinColumnPick]).validate_python(columns)
            )
            how = TypeAdapter(JoinMode).validate_python(how)
        except ValidationError as error:
            raise TableError("invalid_params", "Invalid join arguments") from error
        if (
            not 1 <= len(keys) <= 8
            or picks == []
            or type(indicator) is not bool
            or type(max_output_rows) is not int
            or max_output_rows <= 0
        ):
            raise TableError("invalid_params", "Invalid join options")
        if right.sheet_id == self.scope.sheet_id:
            raise TableError("invalid_input_ref", "Choose two different sheets")
        if len({(key.left_column, key.right_column) for key in keys}) != len(keys):
            raise TableError("invalid_input_ref", "Join key pairs must be distinct")
        requested = None if self.scope.row_ids is None else list(self.scope.row_ids)
        with self.project.read_snapshot() as project:
            left_sheet, left_columns = _sheet(project, self.scope.sheet_id, requested)
            right_sheet, right_columns = _sheet(project, right.sheet_id)
            specs, warnings = [], []
            for pair in keys:
                left = left_columns.get(pair.left_column)
                other = right_columns.get(pair.right_column)
                if left is None or other is None:
                    raise TableError(
                        "invalid_input_ref", "A join key column is missing"
                    )
                mode = _pair_mode(left["type"], other["type"])
                if mode == "cross":
                    warnings.append(
                        f"cross-type key pair {left['name']}({left['type']}) vs {other['name']}({other['type']}) compared as text"
                    )
                specs.append(
                    {
                        "left_col_id": left["id"],
                        "right_col_id": other["id"],
                        "mode": mode,
                    }
                )
            output = _projection(keys, picks, left_columns, right_columns, indicator)
            left_fact, left_vals = _read_side(
                project, left_sheet, left_columns, _side_read_ids(specs, output, "left")
            )
            right_fact, right_vals = _read_side(
                project,
                right_sheet,
                right_columns,
                _side_read_ids(specs, output, "right"),
            )
            if picks is None:
                # An implicit projection reads the column roster as well as cells:
                # newly added columns change its schema. Explicit picks do not.
                left_fact["column_roster"] = list(left_columns.values())
                right_fact["column_roster"] = list(right_columns.values())
        left_keys = [
            (row, _row_key(row, left_vals, specs, "left"))
            for row in left_sheet["row_ids"]
        ]
        right_keys = [
            (row, _row_key(row, right_vals, specs, "right"))
            for row in right_sheet["row_ids"]
        ]
        right_index, left_key_set, stats, count = join_counts(
            left_keys, right_keys, how
        )
        fact = {
            "kind": "joined_tables_source",
            "left_sheet_id": self.scope.sheet_id,
            "right_sheet_id": right.sheet_id,
            "left_requested_row_ids": requested,
            "left": left_fact,
            "right": right_fact,
            "columns": output,
            "join_keys": [key.model_dump(mode="json") for key in keys],
            "how": how,
            "indicator": indicator,
            "max_output_rows": max_output_rows,
            "stats": stats,
        }
        self.facts.append(fact)
        tokens = {
            side: {
                row: RowSource(sheet_id=sheet["sheet_id"], row_id=row)
                for row in sheet["row_ids"]
            }
            for side, sheet in (("left", left_sheet), ("right", right_sheet))
        }
        for side, rows in tokens.items():
            self.sources.update(rows.values())
            self.source_roles.update((token, f"join_{side}") for token in rows.values())
        self._plans.append(
            (tokens, dict(left_keys), dict(right_keys), left_key_set, right_index, how)
        )

        def rows():
            details = {
                "estimated_rows": count,
                "max_output_rows": max_output_rows,
                "top_fanout_keys": _top_fanout_keys(
                    left_key_set & right_index.keys(), left_keys, right_index
                ),
            }
            confirmation_hash = mint_confirmation_hash(
                scope_confirmation(
                    family_kind=self.action_kind,
                    scope=ParamsScope(params=self.request_identity),
                    bindings={**details, "input_digest": _digest(fact)},
                )
            )
            if count > max_output_rows:
                refusal = refuse_unless_exact_echo(
                    confirmed=self.confirmation is not None,
                    echoed_hash=self.confirmation,
                    expected_hash=confirmation_hash,
                    refuse=lambda: ActionError(
                        code="join_fanout_requires_confirmation",
                        message="Join projected rows exceed max_output_rows; resubmit with the current confirmation token",
                        action_kind=self.action_kind,
                        field="confirmation",
                        needs_confirmation=True,
                        details={**details, "promise_set_hash": confirmation_hash},
                    ),
                )
                if refusal is not None:
                    raise TableReadRefused(refusal)
            for record in iter_join_records(
                how=how,
                output_columns=output,
                left_keys=left_keys,
                right_keys=right_keys,
                right_index=right_index,
                left_key_set=left_key_set,
                left_vals=left_vals,
                right_vals=right_vals,
            ):
                left = tokens["left"].get(record["left_row_id"])
                other = tokens["right"].get(record["right_row_id"])
                yield TableRow(
                    output=DynamicOutput(record["values"]),
                    sources=tuple(
                        token for token in (left, other) if token is not None
                    ),
                    parent=left,
                )

        return DynamicTableResult(
            schema=[TableColumn(column["name"], column["type"]) for column in output],
            rows=rows(),
            warnings=warnings,
        )

    def validate_lineage(self, sources, parent):
        for tokens, left_keys, right_keys, left_set, right_index, how in self._plans:
            left = next(
                (
                    token
                    for token in sources
                    if tokens["left"].get(token.row_id) is token
                ),
                None,
            )
            right = next(
                (
                    token
                    for token in sources
                    if tokens["right"].get(token.row_id) is token
                ),
                None,
            )
            if parent is not left or set(sources) != {
                token for token in (left, right) if token is not None
            }:
                continue
            if left is not None and right is not None:
                key = left_keys[left.row_id]
                if key is not None and key == right_keys[right.row_id]:
                    return
            elif (
                left is not None
                and how in ("left", "outer")
                and left_keys[left.row_id] not in right_index
            ):
                return
            elif (
                right is not None
                and how in ("right", "outer")
                and right_keys[right.row_id] not in left_set
            ):
                return
        raise TableError(
            "invalid_input_ref",
            "Join rows must retain their admitted matching contributors",
        )

    def revalidate(self):
        for fact in self.facts:
            validate_joined_tables_source(self.project, fact, code="stale_input")
