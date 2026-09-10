from __future__ import annotations

import datetime as dt
import re
from typing import Any, ClassVar

from pydantic import BaseModel, field_validator

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.types import (
    ActionParams,
    ColumnRef,
    Outcome,
    Row,
    RowResult,
    Template,
)


class TemplateParams(ActionParams):
    template: Template[str]


class TemplateOutput(BaseModel):
    rendered: str


def render_template(
    params: TemplateParams,
    row: Row,
) -> RowResult[TemplateOutput]:
    return RowResult(
        output=TemplateOutput(rendered=params.template.render(row)),
    )


TEMPLATE = action(
    examples=(TemplateParams(template=Template[str](text="{{source}}")),),
    name="template",
    title="Template",
    description="Build text from values in each row.",
    category=ActionCategory.TEXT,
    run=map_rows(render_template),
)


_FORMAT_VALIDATION_PROBE = dt.datetime(
    2004,
    11,
    22,
    13,
    14,
    15,
    123456,
    tzinfo=dt.UTC,
)
_TIME_TEXT = re.compile(
    r"(?:\b\d{1,2}:\d{2}\b|\b(?:noon|midnight)\b|"
    r"\b\d{1,2}\s*(?:a\.?m\.?|p\.?m\.?)\b)",
    re.IGNORECASE,
)
_TIME_FORMAT_DIRECTIVES = ("%H", "%I", "%M", "%S", "%f", "%X", "%p", "%z", "%Z")


def _valid_strftime_format(value: str) -> bool:
    try:
        rendered = _FORMAT_VALIDATION_PROBE.strftime(value)
        dt.datetime.strptime(rendered, value)
    except (TypeError, ValueError):
        return False
    return True


class CleanDateSource(ColumnRef[Any]):
    accepted_column_types: ClassVar[tuple[str, ...]] = (
        "text",
        "category",
        "date",
        "number",
        "integer",
    )


class CleanDatesParams(ActionParams):
    source: CleanDateSource
    format: str | None = None

    @field_validator("format")
    @classmethod
    def _validate_format(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if not _valid_strftime_format(value):
            raise ValueError("invalid strftime format")
        return value


class CleanDatesOutput(BaseModel):
    cleaned: Outcome[dt.date | dt.datetime | None]


def _normalized_date(
    parsed: dt.date | dt.datetime,
    *,
    preserve_time: bool = False,
) -> dt.date | dt.datetime:
    if isinstance(parsed, dt.datetime):
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=dt.UTC)
        else:
            parsed = parsed.astimezone(dt.UTC)
        if not preserve_time and (
            parsed.hour,
            parsed.minute,
            parsed.second,
            parsed.microsecond,
        ) == (0, 0, 0, 0):
            return parsed.date()
    return parsed


def _parse_iso(text: str) -> dt.date | dt.datetime | None:
    for parse in (dt.date.fromisoformat, dt.datetime.fromisoformat):
        try:
            parsed = parse(text)
            return _normalized_date(
                parsed,
                preserve_time=isinstance(parsed, dt.datetime),
            )
        except ValueError:
            continue
    return None


def parse_date(text: str, *, format: str | None = None) -> dt.date | dt.datetime | None:
    text = str(text).strip()
    if not text:
        return None
    if format:
        try:
            return _normalized_date(
                dt.datetime.strptime(text, format),
                preserve_time=any(part in format for part in _TIME_FORMAT_DIRECTIVES),
            )
        except (TypeError, ValueError):
            return None

    parsed_iso = _parse_iso(text)
    if parsed_iso is not None:
        return parsed_iso

    import dateparser

    parsed = dateparser.parse(
        text,
        settings={
            "STRICT_PARSING": True,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "TIMEZONE": "UTC",
        },
    )
    if parsed is None:
        return None
    return _normalized_date(parsed, preserve_time=_TIME_TEXT.search(text) is not None)


def clean_date(
    params: CleanDatesParams,
    row: Row,
) -> RowResult[CleanDatesOutput]:
    raw = params.source.read(row)
    if raw is None or raw == "":
        cleaned = Outcome.ok(None)
    else:
        text = str(raw).strip()
        parsed = parse_date(text, format=params.format)
        if parsed is not None:
            cleaned = Outcome.ok(parsed, confidence=1.0)
        else:
            reason = (
                f"did not match the format {params.format!r}"
                if params.format
                else "as a date"
            )
            cleaned = Outcome.ok(
                None,
                confidence=0.0,
                justification=(
                    f"could not parse {text!r} {reason} "
                    "(original kept in the source column)"
                ),
            )
    return RowResult(output=CleanDatesOutput(cleaned=cleaned))


CLEAN_DATES = action(
    examples=(CleanDatesParams(source="source", format="%d/%m/%Y"),),
    name="clean_dates",
    title="Parse strict dates",
    description="Parse messy date strings into strict ISO dates.",
    category=ActionCategory.CLEANUP,
    run=map_rows(clean_date),
)
