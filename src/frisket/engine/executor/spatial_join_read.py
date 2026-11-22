"""One-snapshot point-in-polygon spatial joins with lazy fanout admission."""

from __future__ import annotations

import heapq
from collections.abc import Iterator, Mapping
from contextlib import closing
from dataclasses import dataclass
from typing import Any
from weakref import WeakKeyDictionary, WeakSet

from pydantic import TypeAdapter, ValidationError

from frisket.actions.join_types import JoinColumnName
from frisket.actions.spatial_geometry import (
    EPSILON,
    PreparedShape,
    parse_geo_point,
    point_in_prepared_shape,
    prepare_shape,
)
from frisket.actions.spatial_join_types import (
    SpatialJoinMatch,
    SpatialJoinMode,
    SpatialJoinSpec,
)
from frisket.actions.types import (
    ColumnRef,
    DynamicOutput,
    DynamicTableResult,
    RowSource,
    SheetRef,
    SheetRows,
    TableColumn,
    TableError,
    TableRow,
)
from frisket.contracts.action import ActionError, canonical_column_type
from frisket.engine.executor.embedding_read import TableReadRefused
from frisket.engine.executor.import_read_guard import require_action_sheet_readable
from frisket.engine.executor.joined_tables_read import (
    JoinRefreshAdmission,
    _digest,
    _sheet,
    validate_joined_tables_source,
)
from frisket.engine.runner.confirmation_context import (
    ParamsScope,
    mint_confirmation_hash,
    scope_confirmation,
)
from frisket.engine.runner.confirmation_echo import refuse_unless_exact_echo
from frisket.engine.store.consumed_source_pages import iter_consumed_source_pages
from frisket.engine.store.consumed_sources import capture_fingerprint

PreparedShapeEntry = tuple[int, PreparedShape, float, float, float, float]
_TOP_FANOUT_LIMIT = 10


@dataclass(frozen=True, slots=True)
class SpatialJoinAdmission:
    """Host invocation context for an admitted spatial join reader."""

    action_kind: str
    request_identity: Mapping[str, Any]
    confirmation: str | None
    refresh_admission: JoinRefreshAdmission | None = None
    schema_only: bool = False


@dataclass(frozen=True, slots=True)
class _ValidatedSpatialSpec:
    point_column: str
    right_sheet: SheetRef
    shape_column: str
    how: SpatialJoinMode
    match: SpatialJoinMatch
    right_columns: list[str] | None
    indicator: bool
    max_output_rows: int


@dataclass(frozen=True, slots=True)
class _SideReadRequest:
    sheet: dict[str, Any]
    columns: Mapping[str, dict[str, Any]]
    col_ids: list[int]
    scope_intent: str = "whole"
    require_complete: bool = False


@dataclass(frozen=True, slots=True)
class _MatchCounts:
    estimated_rows: int
    top_fanout: list[dict[str, int]]
    stats: dict[str, int]


def _spatial_projection(
    left_columns: Mapping[str, dict[str, Any]],
    right_columns: Mapping[str, dict[str, Any]],
    right_picks: list[str] | None,
    indicator: bool,
) -> list[dict[str, Any]]:
    """Build deterministic output column descriptors with ``_right`` suffixing."""
    left_names = list(left_columns.keys())
    right_names = (
        list(right_columns.keys()) if right_picks is None else list(right_picks)
    )
    for col_name in right_names:
        if col_name not in right_columns:
            raise TableError(
                "invalid_input_ref",
                f"Projected right column {col_name!r} is missing",
            )

    natural: set[str] = set(left_names) | set(right_names)
    if indicator:
        natural.add("_merge")
    used: set[str] = {"_merge"} if indicator else set()

    def allocate(base: str, *, generated: bool = False) -> str:
        if base not in used and not (generated and base in natural):
            used.add(base)
            return base
        index = 2
        while f"{base}_{index}" in (used | natural):
            index += 1
        result = f"{base}_{index}"
        used.add(result)
        return result

    descriptors: list[dict[str, Any]] = []
    for col_name in left_names:
        col = left_columns[col_name]
        conflict = indicator and col_name == "_merge"
        base = f"{col_name}_left" if conflict else col_name
        descriptors.append(
            {
                "name": allocate(base, generated=conflict),
                "type": col["type"],
                "role": "left",
                "source_col_id": col["id"],
            }
        )
    for col_name in right_names:
        col = right_columns[col_name]
        conflict = col_name in used or (indicator and col_name == "_merge")
        base = f"{col_name}_right" if conflict else col_name
        descriptors.append(
            {
                "name": allocate(base, generated=conflict),
                "type": col["type"],
                "role": "right",
                "source_col_id": col["id"],
            }
        )
    if indicator:
        descriptors.append({"name": "_merge", "type": "text", "role": "indicator"})
    return descriptors


def _ordered_unique_ids(ids: list[int]) -> list[int]:
    seen: set[int] = set()
    ordered: list[int] = []
    for col_id in ids:
        if col_id not in seen:
            seen.add(col_id)
            ordered.append(col_id)
    return ordered


def _require_complete_refs(refs: Mapping[int, Mapping[int, Mapping[str, Any]]]) -> None:
    for column_id, column_refs in refs.items():
        for row_id, ref in column_refs.items():
            if (
                ref.get("outcome") not in (None, "completed")
                or ref.get("validity") == "invalid"
            ):
                raise TableError(
                    "invalid_input_ref",
                    "Spatial join source is unavailable; refresh cannot infer that previous matches disappeared",
                    details={
                        "row_id": row_id,
                        "column_id": column_id,
                        "reason": "upstream_unavailable",
                    },
                )


def _read_side_values(
    project: Any,
    request: _SideReadRequest,
) -> tuple[dict[str, Any], dict[str, Any], dict[int, dict[int, Any]]]:
    by_id = {int(column["id"]): column for column in request.columns.values()}
    captures: list[dict[str, Any]] = []
    values_by_col: dict[int, dict[int, Any]] = {
        col_id: {} for col_id in request.col_ids
    }
    pages = iter_consumed_source_pages(
        project,
        request.sheet["sheet_id"],
        request.col_ids,
        row_ids=request.sheet["row_ids"]
        if request.scope_intent == "selected"
        else None,
        captures=captures,
    )
    with closing(pages):
        for page, values, refs in pages:
            if request.require_complete:
                _require_complete_refs(refs)
            for col_id in request.col_ids:
                col_values = values.get(col_id, {})
                dest = values_by_col[col_id]
                for row_id in page:
                    dest[row_id] = col_values.get(row_id)
    capture = captures[0]
    facts: dict[str, Any] = {
        **request.sheet,
        "columns": [by_id[column_id] for column_id in request.col_ids],
    }
    facts["snapshot_hash"] = _digest([facts, capture_fingerprint(capture)])
    return facts, capture, values_by_col


def _validate_spec(spec: SpatialJoinSpec, left_sheet_id: int) -> _ValidatedSpatialSpec:
    raw_point = (
        spec.point_column.name
        if isinstance(spec.point_column, ColumnRef)
        else spec.point_column
    )
    try:
        point_col = TypeAdapter(JoinColumnName).validate_python(raw_point)
        right = SheetRef.model_validate(spec.right_sheet)
        shape_col = TypeAdapter(JoinColumnName).validate_python(spec.shape_column)
        how: SpatialJoinMode = TypeAdapter(SpatialJoinMode).validate_python(spec.how)
        match: SpatialJoinMatch = TypeAdapter(SpatialJoinMatch).validate_python(
            spec.match
        )
        right_cols: list[str] | None = (
            None
            if spec.right_columns is None
            else TypeAdapter(list[JoinColumnName]).validate_python(spec.right_columns)
        )
    except ValidationError as error:
        raise TableError("invalid_params", "Invalid spatial join arguments") from error

    if (
        right_cols == []
        or (right_cols is not None and len(set(right_cols)) != len(right_cols))
        or type(spec.indicator) is not bool
        or type(spec.max_output_rows) is not int
        or spec.max_output_rows <= 0
    ):
        raise TableError("invalid_params", "Invalid spatial join options")
    if right.sheet_id == left_sheet_id:
        raise TableError("invalid_input_ref", "Choose two different sheets")
    return _ValidatedSpatialSpec(
        point_column=point_col,
        right_sheet=right,
        shape_column=shape_col,
        how=how,
        match=match,
        right_columns=right_cols,
        indicator=spec.indicator,
        max_output_rows=spec.max_output_rows,
    )


def _require_typed_column(
    columns: Mapping[str, dict[str, Any]],
    name: str,
    expected_type: str,
    label: str,
) -> dict[str, Any]:
    col = columns.get(name)
    if col is None:
        raise TableError("invalid_input_ref", f"{label} {name!r} is missing")
    if canonical_column_type(col["type"]) != expected_type:
        raise TableError(
            "invalid_input_ref",
            f"{label} {name!r} must be type {expected_type}",
        )
    return col


def _compile_shapes(
    row_ids: list[int],
    shape_values: Mapping[int, Any],
) -> tuple[list[PreparedShapeEntry], int]:
    prepared_shapes: list[PreparedShapeEntry] = []
    invalid_rows = 0
    for right_row_id in row_ids:
        prepared = prepare_shape(shape_values.get(right_row_id))
        if prepared is None:
            invalid_rows += 1
            continue
        min_lon, min_lat, max_lon, max_lat = prepared.bbox
        prepared_shapes.append(
            (
                int(right_row_id),
                prepared,
                min_lon - EPSILON,
                min_lat - EPSILON,
                max_lon + EPSILON,
                max_lat + EPSILON,
            )
        )
    return prepared_shapes, invalid_rows


def _iter_point_matches(
    lon: float,
    lat: float,
    prepared_shapes: list[PreparedShapeEntry],
    match: SpatialJoinMatch,
) -> Iterator[int]:
    for right_row_id, prepared, min_x, min_y, max_x, max_y in prepared_shapes:
        if lon < min_x or lon > max_x or lat < min_y or lat > max_y:
            continue
        if point_in_prepared_shape(lon, lat, prepared):
            yield right_row_id
            if match == "first":
                return


def _count_one_point(
    lon: float,
    lat: float,
    prepared_shapes: list[PreparedShapeEntry],
    match: SpatialJoinMatch,
) -> int:
    hits = 0
    for _ in _iter_point_matches(lon, lat, prepared_shapes, match):
        hits += 1
    return hits


def _record_top_fanout(
    heap: list[tuple[int, int]], left_row_id: int, match_len: int
) -> None:
    if match_len <= 1:
        return
    entry = (match_len, -left_row_id)
    if len(heap) < _TOP_FANOUT_LIMIT:
        heapq.heappush(heap, entry)
    elif entry > heap[0]:
        heapq.heapreplace(heap, entry)


def _count_matches(
    left_row_ids: list[int],
    point_values: Mapping[int, Any],
    prepared_shapes: list[PreparedShapeEntry],
    *,
    validated: _ValidatedSpatialSpec,
    invalid_right_rows: int,
) -> _MatchCounts:
    """O(1)-memory counting pass computing exact cardinality and top-10 fanout."""
    top_heap: list[tuple[int, int]] = []
    both_count = 0
    left_only_count = 0
    null_left_rows = 0
    matched_left_rows = 0
    max_fanout = 0

    for left_row_id_raw in left_row_ids:
        left_row_id = int(left_row_id_raw)
        point = parse_geo_point(point_values.get(left_row_id))
        if point is None:
            null_left_rows += 1
            hits = 0
        else:
            hits = _count_one_point(
                point[0], point[1], prepared_shapes, validated.match
            )
        if hits > 0:
            matched_left_rows += 1
            both_count += hits
            max_fanout = max(max_fanout, hits)
            _record_top_fanout(top_heap, left_row_id, hits)
        elif validated.how == "left":
            left_only_count += 1

    top_fanout = [
        {"left_row_id": -neg_rid, "matches": count}
        for count, neg_rid in sorted(top_heap, key=lambda item: (-item[0], -item[1]))
    ]
    stats = {
        "both": both_count,
        "left_only": left_only_count,
        "right_only": 0,
        "matched_pairs": both_count,
        "matched_left_rows": matched_left_rows,
        "max_fanout": max_fanout,
        "null_left_rows": null_left_rows,
        "invalid_right_rows": invalid_right_rows,
    }
    return _MatchCounts(
        estimated_rows=both_count + left_only_count,
        top_fanout=top_fanout,
        stats=stats,
    )


def _stream_matched_pairs(
    left_row_ids: list[int],
    point_values: Mapping[int, Any],
    prepared_shapes: list[PreparedShapeEntry],
    validated: _ValidatedSpatialSpec,
) -> Iterator[tuple[int, int | None, str]]:
    """Lazily yield matched ``(left_id, right_id, merge_label)`` pairs."""
    for left_row_id_raw in left_row_ids:
        left_row_id = int(left_row_id_raw)
        point = parse_geo_point(point_values.get(left_row_id))
        matched_any = False
        if point is not None:
            for right_row_id in _iter_point_matches(
                point[0], point[1], prepared_shapes, validated.match
            ):
                matched_any = True
                yield (left_row_id, right_row_id, "both")
        if not matched_any and validated.how == "left":
            yield (left_row_id, None, "left_only")


def _materialize_row_values(
    output: list[dict[str, Any]],
    left_values: Mapping[int, Mapping[int, Any]],
    right_values: Mapping[int, Mapping[int, Any]],
    pair: tuple[int, int | None, str],
) -> dict[str, Any]:
    left_id, right_id, merge_label = pair
    row_values: dict[str, Any] = {}
    for column in output:
        role = column["role"]
        if role == "indicator":
            row_values[column["name"]] = merge_label
        elif role == "left":
            row_values[column["name"]] = left_values[int(column["source_col_id"])].get(
                left_id
            )
        else:
            row_values[column["name"]] = (
                None
                if right_id is None
                else right_values[int(column["source_col_id"])].get(right_id)
            )
    return row_values


def validate_spatial_join_source(
    project: Any, fact: Mapping[str, Any], *, code: str = "stale_replay"
) -> None:
    """Verify both recorded dependencies, even when no output row used either."""
    validate_joined_tables_source(project, fact, code=code)


class AdmittedSpatialJoinReader:
    def __init__(
        self,
        project: Any,
        *,
        scope: Any,
        admission: SpatialJoinAdmission,
    ) -> None:
        if not isinstance(scope, SheetRows):
            raise TableError(
                "invalid_input_ref", "Spatial join requires a left sheet_rows scope"
            )
        self.project = project
        self.scope = scope
        refresh = admission.refresh_admission
        self.action_kind = (
            refresh.action_kind if refresh is not None else admission.action_kind
        )
        self.request_identity: dict[str, Any] = (
            dict(admission.request_identity)
            if refresh is None
            else {
                "request_hash": refresh.request_hash,
                "sheet_id": refresh.sheet_id,
                "parent_change_id": refresh.parent_change_id,
                "join": dict(admission.request_identity),
            }
        )
        self.confirmation = (
            admission.confirmation if refresh is None else refresh.confirmation
        )
        self.require_complete = refresh is not None
        self.schema_only = admission.schema_only
        self.parent_sheet_id = scope.sheet_id
        self.sources: WeakSet[RowSource] = WeakSet()
        self.source_roles: WeakKeyDictionary[RowSource, str] = WeakKeyDictionary()
        self._pair_links: WeakKeyDictionary[RowSource, RowSource | None] = (
            WeakKeyDictionary()
        )
        self.facts: list[dict[str, Any]] = []
        self.consumed_captures: list[dict[str, Any]] = []

    def _mint_pair_tokens(
        self, right_sheet_id: int, left_id: int, right_id: int | None
    ) -> tuple[RowSource, RowSource | None]:
        left_token = RowSource(sheet_id=self.scope.sheet_id, row_id=left_id)
        self.sources.add(left_token)
        self.source_roles[left_token] = "join_left"
        right_token: RowSource | None = None
        if right_id is not None:
            right_token = RowSource(sheet_id=right_sheet_id, row_id=right_id)
            self.sources.add(right_token)
            self.source_roles[right_token] = "join_right"
        self._pair_links[left_token] = right_token
        return left_token, right_token

    def _enforce_fanout_cap(
        self,
        fact: Mapping[str, Any],
        counts: _MatchCounts,
        max_output_rows: int,
    ) -> None:
        details = {
            "estimated_rows": counts.estimated_rows,
            "max_output_rows": max_output_rows,
            "top_fanout_points": counts.top_fanout,
        }
        confirmation_hash = mint_confirmation_hash(
            scope_confirmation(
                family_kind=self.action_kind,
                scope=ParamsScope(params=self.request_identity),
                bindings={**details, "input_digest": _digest(fact)},
            )
        )
        if counts.estimated_rows <= max_output_rows:
            return
        refusal = refuse_unless_exact_echo(
            confirmed=self.confirmation is not None,
            echoed_hash=self.confirmation,
            expected_hash=confirmation_hash,
            refuse=lambda: ActionError(
                code="join_fanout_requires_confirmation",
                message=(
                    "Spatial join projected rows exceed max_output_rows; "
                    "resubmit with the current confirmation token"
                ),
                action_kind=self.action_kind,
                field="confirmation",
                needs_confirmation=True,
                details={**details, "promise_set_hash": confirmation_hash},
            ),
        )
        if refusal is not None:
            raise TableReadRefused(refusal)

    def read(self, spec: SpatialJoinSpec) -> DynamicTableResult:
        validated = _validate_spec(spec, self.scope.sheet_id)
        require_action_sheet_readable(self.project, validated.right_sheet.sheet_id)
        requested = None if self.scope.row_ids is None else list(self.scope.row_ids)

        with self.project.read_snapshot() as project:
            left_sheet, left_cols = _sheet(project, self.scope.sheet_id, requested)
            right_sheet, right_cols = _sheet(project, validated.right_sheet.sheet_id)
            point_meta = _require_typed_column(
                left_cols, validated.point_column, "geo_point", "Point column"
            )
            shape_meta = _require_typed_column(
                right_cols, validated.shape_column, "geo_shape", "Shape column"
            )
            output = _spatial_projection(
                left_cols, right_cols, validated.right_columns, validated.indicator
            )
            if self.schema_only:
                return DynamicTableResult(
                    schema=[
                        TableColumn(column["name"], column["type"]) for column in output
                    ],
                    rows=(),
                )
            left_ids = _ordered_unique_ids(
                [int(point_meta["id"])]
                + [int(c["source_col_id"]) for c in output if c["role"] == "left"]
            )
            right_ids = _ordered_unique_ids(
                [int(shape_meta["id"])]
                + [int(c["source_col_id"]) for c in output if c["role"] == "right"]
            )
            left_fact, left_capture, left_values = _read_side_values(
                project,
                _SideReadRequest(
                    sheet=left_sheet,
                    columns=left_cols,
                    col_ids=left_ids,
                    scope_intent="selected" if requested is not None else "whole",
                    require_complete=self.require_complete,
                ),
            )
            right_fact, right_capture, right_values = _read_side_values(
                project,
                _SideReadRequest(
                    sheet=right_sheet,
                    columns=right_cols,
                    col_ids=right_ids,
                    require_complete=self.require_complete,
                ),
            )
            left_fact["column_roster"] = list(left_cols.values())
            if validated.right_columns is None:
                right_fact["column_roster"] = list(right_cols.values())

        prepared_shapes, invalid_right = _compile_shapes(
            right_sheet["row_ids"], right_values[int(shape_meta["id"])]
        )
        point_values = left_values[int(point_meta["id"])]
        counts = _count_matches(
            left_sheet["row_ids"],
            point_values,
            prepared_shapes,
            validated=validated,
            invalid_right_rows=invalid_right,
        )
        fact: dict[str, Any] = {
            "kind": "spatial_join_source",
            "left_sheet_id": self.scope.sheet_id,
            "right_sheet_id": validated.right_sheet.sheet_id,
            "left_requested_row_ids": requested,
            "left": left_fact,
            "right": right_fact,
            "columns": output,
            "point_column": validated.point_column,
            "shape_column": validated.shape_column,
            "how": validated.how,
            "match": validated.match,
            "indicator": validated.indicator,
            "max_output_rows": validated.max_output_rows,
            "stats": counts.stats,
        }
        self.facts.append(fact)
        self.consumed_captures.extend((left_capture, right_capture))

        def rows() -> Iterator[TableRow[DynamicOutput]]:
            self._enforce_fanout_cap(fact, counts, validated.max_output_rows)
            right_sheet_id = validated.right_sheet.sheet_id
            for pair in _stream_matched_pairs(
                left_sheet["row_ids"], point_values, prepared_shapes, validated
            ):
                left_token, right_token = self._mint_pair_tokens(
                    right_sheet_id, pair[0], pair[1]
                )
                yield TableRow(
                    output=DynamicOutput(
                        _materialize_row_values(output, left_values, right_values, pair)
                    ),
                    sources=tuple(
                        item for item in (left_token, right_token) if item is not None
                    ),
                    parent=left_token,
                )

        return DynamicTableResult(
            schema=[TableColumn(column["name"], column["type"]) for column in output],
            rows=rows(),
        )

    def close(self) -> None:
        return None

    def validate_lineage(
        self, sources: tuple[RowSource, ...], parent: RowSource | None
    ) -> None:
        if parent is not None and parent in self._pair_links:
            expected_right = self._pair_links[parent]
            expected_sources = (
                {parent} if expected_right is None else {parent, expected_right}
            )
            if (
                len(sources) == len(expected_sources)
                and set(sources) == expected_sources
            ):
                return
        raise TableError(
            "invalid_input_ref",
            "Spatial join rows must retain their admitted matching contributors",
        )

    def revalidate(self) -> None:
        for fact in self.facts:
            validate_spatial_join_source(self.project, fact, code="stale_input")
