"""One-snapshot exact joins with schema inspection before lazy fanout admission."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from contextlib import nullcontext, closing
from dataclasses import dataclass
from typing import Any, Literal
from weakref import WeakSet, WeakKeyDictionary
from .table_read_stage import JoinReadStage

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
from frisket.engine.executor.import_read_guard import require_action_sheet_readable
from frisket.engine.executor.joined_table import (
    _pair_mode,
    _side_read_ids,
    _widen_type,
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
    parent_change_id: int
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
        sides: tuple[tuple[Literal["left", "right"], Mapping[str, Any]], ...] = (
            ("left", left),
            ("right", right),
        )
        picks = [
            JoinColumnPick(side=side, column=name)
            for side, columns in sides
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


def _read_side(
    project,
    sheet,
    columns,
    ids,
    *,
    scope_intent="whole",
    stage=None,
    side=None,
    specs=(),
    require_complete=False,
):
    from frisket.engine.store.consumed_source_pages import iter_consumed_source_pages
    from frisket.engine.store.consumed_sources import capture_fingerprint

    by_id = {column["id"]: column for column in columns.values()}
    captures = []
    pages = iter_consumed_source_pages(
        project,
        sheet["sheet_id"],
        ids,
        row_ids=sheet["row_ids"] if scope_intent == "selected" else None,
        captures=captures,
    )
    with closing(pages):
        for page, values, refs in pages:
            if require_complete:
                for column, column_refs in refs.items():
                    for row, ref in column_refs.items():
                        if (
                            ref.get("outcome") not in (None, "completed")
                            or ref.get("validity") == "invalid"
                        ):
                            raise TableError(
                                "invalid_input_ref",
                                "Join source is unavailable; refresh cannot infer that previous matches disappeared",
                                details={
                                    "row_id": row,
                                    "column_id": column,
                                    "reason": "upstream_unavailable",
                                },
                            )
            if stage is not None:
                for row in page:
                    stage.add(side, row, values, specs)
    capture = captures[0]
    facts = {**sheet, "columns": [by_id[column_id] for column_id in ids]}
    facts["snapshot_hash"] = _digest([facts, capture_fingerprint(capture)])
    return facts, capture


def validate_joined_tables_source(project, fact, *, code="stale_replay"):
    snapshot = (
        nullcontext(project) if project.db.in_transaction else project.read_snapshot()
    )
    with snapshot as captured:
        return _validate_joined_tables_source(captured, fact, code=code)


def _validate_joined_tables_source(project, fact, *, code):
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
                scope_intent="selected"
                if side == "left" and fact["left_requested_row_ids"] is not None
                else "whole",
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
                "parent_change_id": refresh_admission.parent_change_id,
                "join": request_identity,
            }
        )
        self.confirmation = (
            confirmation
            if refresh_admission is None
            else refresh_admission.confirmation
        )
        self.require_complete = refresh_admission is not None
        self.parent_sheet_id = scope.sheet_id
        self.sources = WeakSet()
        self.source_roles = WeakKeyDictionary()
        self._tokens = WeakKeyDictionary()
        self._stages = []
        self.facts = []
        self.consumed_captures = []
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
        require_action_sheet_readable(self.project, right.sheet_id)
        if len({(key.left_column, key.right_column) for key in keys}) != len(keys):
            raise TableError("invalid_input_ref", "Join key pairs must be distinct")
        requested = None if self.scope.row_ids is None else list(self.scope.row_ids)
        stage = JoinReadStage()
        self._stages.append(stage)
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
            left_fact, left_capture = _read_side(
                project,
                left_sheet,
                left_columns,
                _side_read_ids(specs, output, "left"),
                scope_intent="selected" if requested is not None else "whole",
                stage=stage,
                side="left",
                specs=specs,
                require_complete=self.require_complete,
            )
            right_fact, right_capture = _read_side(
                project,
                right_sheet,
                right_columns,
                _side_read_ids(specs, output, "right"),
                stage=stage,
                side="right",
                specs=specs,
                require_complete=self.require_complete,
            )
            if picks is None:
                # An implicit projection reads the column roster as well as cells:
                # newly added columns change its schema. Explicit picks do not.
                left_fact["column_roster"] = list(left_columns.values())
                right_fact["column_roster"] = list(right_columns.values())
        stage.seal()
        stats, count = stage.stats(how)
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
        self.consumed_captures.extend((left_capture, right_capture))
        self._plans.append((stage, how))

        def token(side, row_id):
            if row_id is None:
                return None
            source = RowSource(
                sheet_id=self.scope.sheet_id if side == "left" else right.sheet_id,
                row_id=row_id,
            )
            self.sources.add(source)
            self.source_roles[source] = f"join_{side}"
            self._tokens[source] = (stage, side)
            return source

        def rows():
            details = {
                "estimated_rows": count,
                "max_output_rows": max_output_rows,
                "top_fanout_keys": stage.top_keys(),
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
            for left_id, right_id, values in stage.records(how, output):
                left = token("left", left_id)
                other = token("right", right_id)
                yield TableRow(
                    output=DynamicOutput(values),
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

    def close(self):
        for stage in self._stages:
            stage.close()
        self._stages.clear()

    def validate_lineage(self, sources, parent):
        for stage, how in self._plans:
            left = next(
                (
                    token
                    for token in sources
                    if self._tokens.get(token) == (stage, "left")
                ),
                None,
            )
            right = next(
                (
                    token
                    for token in sources
                    if self._tokens.get(token) == (stage, "right")
                ),
                None,
            )
            if parent is not left or set(sources) != {
                token for token in (left, right) if token is not None
            }:
                continue
            if stage.valid_pair(
                left.row_id if left else None, right.row_id if right else None, how
            ):
                return
        raise TableError(
            "invalid_input_ref",
            "Join rows must retain their admitted matching contributors",
        )

    def revalidate(self):
        for fact in self.facts:
            validate_joined_tables_source(self.project, fact, code="stale_input")
