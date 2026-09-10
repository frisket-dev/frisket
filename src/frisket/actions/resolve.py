"""Typed, deterministic whole-column value resolution actions."""

from __future__ import annotations

import json
import math
import re
import statistics
from collections import Counter
from typing import Any, ClassVar, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    field_validator,
    model_validator,
)

from frisket.actions.core import ActionCategory, RowScope, action, column_transform
from frisket.actions.types import (
    ActionParams,
    ColumnRef,
    ColumnTransformContext,
    RowResult,
    Rows,
)
from frisket.preview.replace_rules import (
    MAX_REPLACE_RULES,
    compile_replace_rules,
    evaluate_replace_rules,
)

MAX_SUBSTITUTE_MAPPING_ENTRIES = 5_000
MAX_COMBINE_GROUPS = 1_000
MAX_COMBINE_MEMBERS_TOTAL = 20_000

_TEXT_COLUMN_TYPES = ("text", "category", "link")
_NUMERIC_COLUMN_TYPES = frozenset({"number", "integer"})


class ResolveTextColumn(ColumnRef[str]):
    accepted_column_types: ClassVar[tuple[str, ...]] = _TEXT_COLUMN_TYPES


def _clean_target(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("target must not be blank")
    return cleaned


def _cell_surface(raw: Any) -> str | None:
    if raw is None:
        return None
    surface = raw if isinstance(raw, str) else str(raw)
    return surface if surface.strip() else None


class ResolvedTextOutput(BaseModel):
    cleaned: str | None


class SubstituteParams(ActionParams):
    source: ResolveTextColumn
    mapping: dict[StrictStr, StrictStr | None]
    unmatched: Literal["keep", "null"] = "keep"

    @field_validator("mapping")
    @classmethod
    def _valid_mapping(cls, value: dict[str, str | None]) -> dict[str, str | None]:
        if not value:
            raise ValueError("mapping must not be empty")
        if len(value) > MAX_SUBSTITUTE_MAPPING_ENTRIES:
            raise ValueError(
                f"mapping accepts at most {MAX_SUBSTITUTE_MAPPING_ENTRIES} entries"
            )
        if any(not key.strip() for key in value):
            raise ValueError("mapping keys must not be blank")
        return {key: _clean_target(target) for key, target in value.items()}


def substitute_values(
    params: SubstituteParams,
    rows: Rows,
) -> dict[int, RowResult[ResolvedTextOutput]]:
    result: dict[int, RowResult[ResolvedTextOutput]] = {}
    for row_id, row in rows.items():
        surface = _cell_surface(params.source.read(row))
        if surface is None:
            cleaned = None
        elif surface in params.mapping:
            cleaned = params.mapping[surface]
        elif params.unmatched == "null":
            cleaned = None
        else:
            cleaned = surface
        result[row_id] = RowResult(output=ResolvedTextOutput(cleaned=cleaned))
    return result


SUBSTITUTE = action(
    examples=(SubstituteParams(source="source", mapping={"NY": "New York"}),),
    name="substitute",
    title="Substitute values",
    description=(
        "Rewrite exact source values through an explicit mapping while preserving "
        "the source column."
    ),
    category=ActionCategory.CLEANUP,
    row_scope=RowScope.ALL_ROWS,
    run=column_transform(substitute_values),
)


class ReplaceRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    match: Literal["contains", "exact", "regex"]
    pattern: StrictStr
    target: StrictStr | None = None
    case_sensitive: bool = False

    @field_validator("pattern")
    @classmethod
    def _valid_pattern(cls, value: str) -> str:
        if not value:
            raise ValueError("pattern must not be empty")
        return value

    @field_validator("target")
    @classmethod
    def _valid_target(cls, value: str | None) -> str | None:
        return _clean_target(value)

    @model_validator(mode="after")
    def _valid_regex(self) -> Self:
        if self.match == "regex":
            try:
                re.compile(self.pattern)
            except re.error as error:
                raise ValueError("invalid regex pattern") from error
        return self


class ReplaceParams(ActionParams):
    source: ResolveTextColumn
    rules: list[ReplaceRule] = Field(min_length=1, max_length=MAX_REPLACE_RULES)
    unmatched: Literal["keep", "null"] = "keep"


def replace_values(
    params: ReplaceParams,
    rows: Rows,
) -> dict[int, RowResult[ResolvedTextOutput]]:
    compiled = compile_replace_rules(params.rules)
    return {
        row_id: RowResult(
            output=ResolvedTextOutput(
                cleaned=evaluate_replace_rules(
                    params.source.read(row), compiled, params.unmatched
                )[1]
            )
        )
        for row_id, row in rows.items()
    }


REPLACE = action(
    examples=(
        ReplaceParams(
            source="source",
            rules=[ReplaceRule(match="exact", pattern="N/A", target=None)],
        ),
    ),
    name="replace",
    title="Replace by rules",
    description=(
        "Apply ordered contains, exact, or regex rules to whole source values; "
        "the first match wins."
    ),
    category=ActionCategory.CLEANUP,
    row_scope=RowScope.ALL_ROWS,
    run=column_transform(replace_values),
)


class CombineGroup(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    canonical: StrictStr
    members: list[StrictStr] = Field(min_length=1)

    @field_validator("canonical")
    @classmethod
    def _valid_canonical(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("canonical value must not be blank")
        return cleaned

    @field_validator("members")
    @classmethod
    def _valid_members(cls, value: list[str]) -> list[str]:
        if any(not member.strip() for member in value):
            raise ValueError("group members must not be blank")
        if len(value) != len(set(value)):
            raise ValueError("group members must be unique")
        return value


class CombineParams(ActionParams):
    source: ResolveTextColumn
    groups: list[CombineGroup] = Field(min_length=1, max_length=MAX_COMBINE_GROUPS)
    unmatched: Literal["keep", "null", "value"] = "keep"
    unmatched_value: StrictStr | None = None

    @field_validator("unmatched_value")
    @classmethod
    def _valid_unmatched_value(cls, value: str | None) -> str | None:
        return _clean_target(value)

    @model_validator(mode="after")
    def _valid_groups(self) -> Self:
        if (self.unmatched == "value") != (self.unmatched_value is not None):
            raise ValueError(
                "unmatched_value is required only when unmatched is 'value'"
            )
        members = [member for group in self.groups for member in group.members]
        if len(members) > MAX_COMBINE_MEMBERS_TOTAL:
            raise ValueError(
                f"groups accept at most {MAX_COMBINE_MEMBERS_TOTAL} members"
            )
        if len(members) != len(set(members)):
            raise ValueError("members must be unique across groups")
        return self


def combine_values(
    params: CombineParams,
    rows: Rows,
) -> dict[int, RowResult[ResolvedTextOutput]]:
    canonical_by_member = {
        member: group.canonical for group in params.groups for member in group.members
    }
    result: dict[int, RowResult[ResolvedTextOutput]] = {}
    for row_id, row in rows.items():
        surface = _cell_surface(params.source.read(row))
        if surface is None:
            cleaned = None
        elif surface in canonical_by_member:
            cleaned = canonical_by_member[surface]
        elif params.unmatched == "null":
            cleaned = None
        elif params.unmatched == "value":
            cleaned = params.unmatched_value
        else:
            cleaned = surface
        result[row_id] = RowResult(output=ResolvedTextOutput(cleaned=cleaned))
    return result


COMBINE = action(
    examples=(
        CombineParams(
            source="source",
            groups=[CombineGroup(canonical="New York", members=["NY", "N.Y."])],
        ),
    ),
    name="combine",
    title="Combine values",
    description=(
        "Rewrite explicitly grouped source values to their canonical value while "
        "preserving the source column."
    ),
    category=ActionCategory.CLEANUP,
    row_scope=RowScope.ALL_ROWS,
    run=column_transform(combine_values),
)


class FillMissingParams(ActionParams):
    source: ColumnRef[Any]
    method: Literal["down", "up", "value", "mean", "median", "mode"] = "down"
    fill_value: StrictStr | None = Field(
        default=None,
    )
    treat_blank_as_missing: bool = True

    @field_validator("fill_value")
    @classmethod
    def _valid_fill_value(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("fill value must not be blank")
        return value

    @model_validator(mode="after")
    def _valid_method(self) -> Self:
        if (self.method == "value") != (self.fill_value is not None):
            raise ValueError("fill_value is required only for the value method")
        return self


def _is_missing(value: Any, treat_blank_as_missing: bool) -> bool:
    return value is None or (
        treat_blank_as_missing and isinstance(value, str) and not value.strip()
    )


def _parse_numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
    elif isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_number_literal(text: str) -> int | float | None:
    try:
        return int(text.strip())
    except ValueError:
        pass
    try:
        parsed = float(text.strip())
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _mode_value(values: list[Any]) -> Any | None:
    counts: Counter[str] = Counter()
    first: dict[str, tuple[int, Any]] = {}
    for index, value in enumerate(values):
        key = json.dumps(value, sort_keys=True, default=str)
        counts[key] += 1
        first.setdefault(key, (index, value))
    if not counts:
        return None
    key = max(counts, key=lambda item: (counts[item], -first[item][0]))
    return first[key][1]


def _fill_values(
    params: FillMissingParams,
    rows: Rows,
    *,
    source_type: str | None = None,
) -> dict[int, Any]:
    row_ids = list(rows)
    source_values = {row_id: params.source.read(rows[row_id]) for row_id in row_ids}
    missing = {
        row_id: _is_missing(source_values[row_id], params.treat_blank_as_missing)
        for row_id in row_ids
    }
    non_missing = [source_values[row_id] for row_id in row_ids if not missing[row_id]]
    fills: dict[int, Any] = {}

    if params.method == "down":
        carry: Any = None
        for row_id in row_ids:
            if missing[row_id]:
                fills[row_id] = carry
            else:
                carry = source_values[row_id]
    elif params.method == "up":
        carry = None
        for row_id in reversed(row_ids):
            if missing[row_id]:
                fills[row_id] = carry
            else:
                carry = source_values[row_id]
    elif params.method == "value":
        fill: Any = params.fill_value
        if source_type in _NUMERIC_COLUMN_TYPES and params.fill_value is not None:
            parsed = _parse_number_literal(params.fill_value)
            if parsed is not None:
                fill = parsed
        fills = {row_id: fill for row_id in row_ids if missing[row_id]}
    elif params.method in {"mean", "median"}:
        parsed = [
            number
            for value in non_missing
            if (number := _parse_numeric(value)) is not None
        ]
        aggregate: int | float | None = None
        if parsed:
            value = (
                statistics.fmean(parsed)
                if params.method == "mean"
                else float(statistics.median(parsed))
            )
            aggregate = (
                int(value)
                if value.is_integer() and all(item.is_integer() for item in parsed)
                else value
            )
        fills = {row_id: aggregate for row_id in row_ids if missing[row_id]}
    else:
        mode = _mode_value(non_missing)
        fills = {row_id: mode for row_id in row_ids if missing[row_id]}

    return {
        row_id: fills.get(row_id) if missing[row_id] else source_values[row_id]
        for row_id in row_ids
    }


class FilledOutput(BaseModel):
    cleaned: Any


def fill_missing_output_type(
    params: FillMissingParams,
    context: ColumnTransformContext,
    results: dict[int, RowResult[FilledOutput]],
) -> str:
    del results
    if (
        params.method in {"mean", "median"}
        and context.source_type not in _NUMERIC_COLUMN_TYPES
    ):
        raise ValueError(
            f"method {params.method!r} requires a number or integer column"
        )
    return context.source_type


def validate_fill_missing_source(
    params: FillMissingParams, context: ColumnTransformContext
) -> None:
    if (
        params.method in {"mean", "median"}
        and context.source_type not in _NUMERIC_COLUMN_TYPES
    ):
        raise ValueError(
            f"method {params.method!r} requires a number or integer column"
        )


def fill_missing_values(
    params: FillMissingParams,
    rows: Rows,
    context: ColumnTransformContext,
) -> dict[int, RowResult[FilledOutput]]:
    source_type = context.source_type
    # Resolve output types here too so direct handler calls cannot bypass the
    # numeric-method gate used during project planning.
    validate_fill_missing_source(params, context)
    values = _fill_values(params, rows, source_type=source_type)
    return {
        row_id: RowResult(output=FilledOutput(cleaned=value))
        for row_id, value in values.items()
    }


FILL_MISSING = action(
    examples=(FillMissingParams(source="source", method="down"),),
    name="fill_missing",
    title="Fill missing values",
    description=(
        "Fill null or blank source cells from neighboring rows, a fixed value, "
        "or a column aggregate while preserving the source column."
    ),
    category=ActionCategory.CLEANUP,
    row_scope=RowScope.ALL_ROWS,
    run=column_transform(
        fill_missing_values,
        output_type=fill_missing_output_type,
        preflight=validate_fill_missing_source,
    ),
)
