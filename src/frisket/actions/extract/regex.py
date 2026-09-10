from __future__ import annotations

import math
from functools import lru_cache
from typing import Any, ClassVar, Self

import regex as safe_regex
from pydantic import Field, field_validator, model_validator

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.types import ActionParams, ColumnRef, DynamicOutput, Row, RowResult


DEFAULT_REGEX_TIMEOUT_SECONDS = 0.25
MAX_REGEX_TIMEOUT_SECONDS = 2.0


@lru_cache(maxsize=128)
def _compiled(pattern: str) -> Any:
    try:
        return safe_regex.compile(pattern)
    except safe_regex.error as error:
        raise ValueError("invalid regex pattern") from error


class RegexSource(ColumnRef[str]):
    accepted_column_types: ClassVar[tuple[str, ...]] = (
        "text",
        "timestamped_transcript",
        "category",
    )


class RegexExtractParams(ActionParams):
    input_columns: list[RegexSource] = Field(min_length=1)
    pattern: str = Field(min_length=1)
    all_matches: bool = False
    group: int | str | None = None
    timeout_seconds: float = Field(
        default=DEFAULT_REGEX_TIMEOUT_SECONDS,
        gt=0,
        le=MAX_REGEX_TIMEOUT_SECONDS,
    )

    @field_validator("input_columns")
    @classmethod
    def _unique_columns(cls, columns: list[RegexSource]) -> list[RegexSource]:
        if len({column.name for column in columns}) != len(columns):
            raise ValueError("input columns must be unique")
        return columns

    @field_validator("pattern")
    @classmethod
    def _pattern(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("invalid regex pattern")
        _compiled(value)
        return value

    @field_validator("group", mode="before")
    @classmethod
    def _group_shape(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError("invalid regex group")
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("invalid regex group")
            if value.isdecimal():
                return int(value)
        return value

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def _finite_timeout(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("invalid regex timeout")
        if not math.isfinite(value):
            raise ValueError("invalid regex timeout")
        return value

    @model_validator(mode="after")
    def _valid_group(self) -> Self:
        compiled = _compiled(self.pattern)
        if isinstance(self.group, int) and not 0 <= self.group <= compiled.groups:
            raise ValueError("invalid regex group")
        if isinstance(self.group, str) and self.group not in compiled.groupindex:
            raise ValueError("invalid regex group")
        return self


def _regex_outputs(params: RegexExtractParams) -> dict[str, Any]:
    compiled = _compiled(params.pattern)
    if params.group is None and not params.all_matches and compiled.groups >= 2:
        return {
            f"extracted_{index}": str | None for index in range(1, compiled.groups + 1)
        }
    return {"extracted": list[str | None] if params.all_matches else str | None}


def regex_extract(
    params: RegexExtractParams,
    row: Row,
) -> RowResult[DynamicOutput]:
    source_values = (column.read(row) for column in params.input_columns)
    text = " ".join(str(value) for value in source_values if value is not None)
    compiled = _compiled(params.pattern)
    try:
        if params.all_matches:
            group = (
                params.group if params.group is not None else int(compiled.groups > 0)
            )
            output = [
                match.group(group)
                for match in compiled.finditer(text, timeout=params.timeout_seconds)
            ]
            return RowResult(output=DynamicOutput({"extracted": output}))
        match = compiled.search(text, timeout=params.timeout_seconds)
    except TimeoutError as error:
        raise RuntimeError(
            f"regex timed out after {params.timeout_seconds:.3f}s"
        ) from error

    if params.group is None and compiled.groups >= 2:
        values = {
            f"extracted_{index}": match.group(index) if match else None
            for index in range(1, compiled.groups + 1)
        }
    else:
        group = params.group if params.group is not None else int(compiled.groups > 0)
        values = {"extracted": match.group(group) if match else None}
    return RowResult(output=DynamicOutput(values))


REGEX_EXTRACT = action(
    examples=(RegexExtractParams(input_columns=["source"], pattern=r"\b\d{4}\b"),),
    name="regex_extract",
    title="Extract text with regex",
    description="Extract a regex match from one or more text columns.",
    category=ActionCategory.EXTRACT,
    run=map_rows(regex_extract, dynamic_outputs=_regex_outputs),
)
