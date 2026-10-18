"""Post-read conversion API; implementations never mutate source text."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
import math
import re
from typing import NamedTuple

from .models import (
    CellConversion,
    ConversionIssue,
    ConversionOutcome,
    DateOrder,
    RawCell,
    RowIssue,
    SourceColumn,
    TargetType,
)


def convert_cell(
    cell: RawCell,
    target_type: TargetType,
    *,
    decimal_separator: str = ".",
    thousands_separator: str | None = ",",
    date_order: DateOrder = "month_first",
) -> CellConversion:
    original = cell.text
    if target_type == "omit":
        return CellConversion(original, None, "", "blank")
    if original == "" or cell.kind == "blank":
        return CellConversion(original, None, "", "blank")
    if target_type == "text":
        return CellConversion(original, original, original, "unchanged")

    try:
        if target_type in {"integer", "number"}:
            parsed_number = _parse_decimal_details(
                original,
                decimal_separator=decimal_separator,
                thousands_separator=thousands_separator,
            )
            value, display = _numeric_value(parsed_number.value, target_type)
            outcome = _numeric_semantic_outcome(
                parsed_number,
                value=value,
                target_type=target_type,
            )
        elif target_type == "boolean":
            value = _parse_boolean(original)
            display = "Yes" if value else "No"
            outcome = "unchanged"
        elif target_type in {"date", "datetime"}:
            parsed = _parse_datetime(original, date_order=date_order)
            value, display = _temporal_value(parsed, target_type)
            outcome = _temporal_semantic_outcome(cell, target_type)
        else:  # pragma: no cover - TargetType keeps this closed for typed callers.
            raise ValueError(f"unsupported target type: {target_type}")
    except (InvalidOperation, OverflowError, ValueError):
        issue = ConversionIssue(
            "conversion_failed",
            f"{original} could not be converted to {target_type}",
            original,
        )
        return CellConversion(original, None, "", "invalid", issue)

    return CellConversion(original, value, display, outcome)


class _ConversionOutcomes(NamedTuple):
    text: ConversionOutcome
    integer: ConversionOutcome
    number: ConversionOutcome
    boolean: ConversionOutcome
    month_first_date: ConversionOutcome
    month_first_datetime: ConversionOutcome
    day_first_date: ConversionOutcome
    day_first_datetime: ConversionOutcome


def _conversion_outcomes(
    cell: RawCell,
    *,
    decimal_separator: str,
    thousands_separator: str | None,
) -> _ConversionOutcomes:
    """Classify all inference targets without allocating converted values."""

    original = cell.text
    if original == "" or cell.kind == "blank":
        return _ConversionOutcomes(*("blank" for _ in range(8)))

    try:
        _parse_boolean(original)
    except ValueError:
        boolean_outcome: ConversionOutcome = "invalid"
    else:
        boolean_outcome = "unchanged"

    try:
        parsed_number = _parse_decimal_details(
            original,
            decimal_separator=decimal_separator,
            thousands_separator=thousands_separator,
        )
    except (InvalidOperation, OverflowError, ValueError):
        integer_outcome: ConversionOutcome = "invalid"
        number_outcome: ConversionOutcome = "invalid"
    else:
        integer_outcome = _numeric_outcome(parsed_number, "integer")
        number_outcome = _numeric_outcome(parsed_number, "number")

    parsed_by_order = _parse_datetime_orders(original)
    temporal_outcomes: list[ConversionOutcome] = []
    for order in ("month_first", "day_first"):
        parsed = parsed_by_order.get(order)
        if parsed is None:
            temporal_outcomes.extend(("invalid", "invalid"))
        else:
            for target in ("date", "datetime"):
                temporal_outcomes.append(_temporal_semantic_outcome(cell, target))
    return _ConversionOutcomes(
        "unchanged",
        integer_outcome,
        number_outcome,
        boolean_outcome,
        *temporal_outcomes,
    )


class _ParsedNumber(NamedTuple):
    value: Decimal
    leading_zero: bool
    long_integer: bool


def _numeric_outcome(
    parsed_number: _ParsedNumber, target_type: TargetType
) -> ConversionOutcome:
    try:
        value, _display = _numeric_value(parsed_number.value, target_type)
    except (OverflowError, ValueError):
        return "invalid"
    return _numeric_semantic_outcome(
        parsed_number,
        value=value,
        target_type=target_type,
    )


def _numeric_semantic_outcome(
    parsed_number: _ParsedNumber,
    *,
    value: int | float,
    target_type: TargetType,
) -> ConversionOutcome:
    if parsed_number.leading_zero or parsed_number.long_integer:
        return "changed"
    if target_type == "number" and Decimal(str(value)) != parsed_number.value:
        return "changed"
    return "unchanged"


def _temporal_semantic_outcome(
    cell: RawCell, target_type: TargetType
) -> ConversionOutcome:
    has_explicit_time = cell.kind == "datetime" or (
        cell.kind != "date" and _EXPLICIT_TIME.search(cell.text.strip()) is not None
    )
    if target_type == "date":
        return "changed" if has_explicit_time else "unchanged"
    return "unchanged" if has_explicit_time else "changed"


def _numeric_value(
    decimal: Decimal, target_type: TargetType
) -> tuple[int | float, str]:
    if target_type == "integer":
        if decimal != decimal.to_integral_value():
            raise ValueError("not a whole number")
        value = int(decimal)
        if not -(2**63) <= value <= 2**63 - 1:
            raise ValueError("integer is outside the supported range")
        return value, str(value)
    value = float(decimal)
    if not math.isfinite(value):
        raise ValueError("number is outside the supported range")
    return value, _display_decimal(decimal)


def _temporal_value(parsed: datetime, target_type: TargetType) -> tuple[str, str]:
    if target_type == "date":
        value = parsed.date().isoformat()
        return value, value
    value = parsed.isoformat()
    return value, value.replace("T", " ")


def convert_row(
    cells: Sequence[RawCell],
    columns: Sequence[SourceColumn],
    target_types: Mapping[int, TargetType],
    *,
    decimal_separator: str = ".",
    thousands_separator: str | None = ",",
    date_orders: Mapping[int, DateOrder] | None = None,
) -> tuple[dict[str, str | int | float | bool | None], tuple[RowIssue, ...]]:
    if len(cells) != len(columns):
        raise ValueError("row width does not match source columns")
    orders = date_orders or {}
    values: dict[str, str | int | float | bool | None] = {}
    issues: list[RowIssue] = []
    for column, cell in zip(columns, cells, strict=True):
        target = target_types.get(column.index, "text")
        if target == "omit":
            continue
        converted = convert_cell(
            cell,
            target,
            decimal_separator=decimal_separator,
            thousands_separator=thousands_separator,
            date_order=orders.get(column.index, "month_first"),
        )
        values[column.name] = converted.value
        if converted.issue is not None:
            issues.append(
                RowIssue(
                    "unreadable_cell",
                    f"{column.name}: {converted.issue.message}",
                    (converted.original,),
                )
            )
    return values, tuple(issues)


_CURRENCY = re.compile(r"^[\s$£€¥]+|[\s$£€¥]+$")


def _parse_decimal(
    raw: str, *, decimal_separator: str, thousands_separator: str | None
) -> Decimal:
    return _parse_decimal_details(
        raw,
        decimal_separator=decimal_separator,
        thousands_separator=thousands_separator,
    ).value


def _parse_decimal_details(
    raw: str, *, decimal_separator: str, thousands_separator: str | None
) -> _ParsedNumber:
    text = raw.strip().replace("\u00a0", "").replace("\u202f", "")
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
    percent = text.endswith("%")
    if percent:
        text = text[:-1].strip()
    text = _CURRENCY.sub("", text)
    if thousands_separator and thousands_separator in text:
        text = _remove_grouping(
            text,
            decimal_separator=decimal_separator,
            thousands_separator=thousands_separator,
        )
    if decimal_separator != ".":
        text = text.replace(decimal_separator, ".")
    if not text:
        raise ValueError("empty number")
    unsigned_mantissa = text.lower().partition("e")[0].lstrip("+-")
    integer_digits, decimal_mark, _fraction = unsigned_mantissa.partition(".")
    leading_zero = len(integer_digits) > 1 and integer_digits.startswith("0")
    long_integer = (
        not decimal_mark and integer_digits.isdigit() and len(integer_digits) > 15
    )
    value = Decimal(text)
    if not value.is_finite():
        raise ValueError("non-finite number")
    if negative:
        value = -value
    if percent:
        value /= Decimal(100)
    return _ParsedNumber(value, leading_zero, long_integer)


def _remove_grouping(
    text: str, *, decimal_separator: str, thousands_separator: str
) -> str:
    mantissa, marker, exponent = text.lower().partition("e")
    if marker and (not exponent or not exponent.lstrip("+-").isdigit()):
        raise ValueError("invalid exponent")
    sign = mantissa[:1] if mantissa[:1] in {"+", "-"} else ""
    unsigned = mantissa[1:] if sign else mantissa
    integer, decimal_mark, fraction = unsigned.partition(decimal_separator)
    if decimal_mark and decimal_separator in fraction:
        raise ValueError("multiple decimal separators")
    groups = integer.split(thousands_separator)
    if (
        len(groups) < 2
        or not 1 <= len(groups[0]) <= 3
        or not groups[0].isdigit()
        or any(len(group) != 3 or not group.isdigit() for group in groups[1:])
        or thousands_separator in fraction
    ):
        raise ValueError("invalid thousands grouping")
    normalized = sign + "".join(groups)
    if decimal_mark:
        if not fraction or not fraction.isdigit():
            raise ValueError("invalid decimal fraction")
        normalized += decimal_separator + fraction
    if marker:
        normalized += marker + exponent
    return normalized


def _display_decimal(value: Decimal) -> str:
    text = format(value, "f")
    sign = ""
    if text.startswith("-"):
        sign, text = "-", text[1:]
    integer, dot, fraction = text.partition(".")
    grouped = f"{int(integer or '0'):,}"
    return f"{sign}{grouped}{dot}{fraction}"


def _parse_boolean(raw: str) -> bool:
    value = raw.strip().casefold()
    if value in {"y", "yes", "true", "1"}:
        return True
    if value in {"n", "no", "false", "0"}:
        return False
    raise ValueError("invalid boolean")


def _parse_datetime(raw: str, *, date_order: DateOrder) -> datetime:
    text = raw.strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return _parse_ordered_datetime(text, date_order=date_order)


def _parse_datetime_orders(raw: str) -> dict[DateOrder, datetime]:
    text = raw.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None:
        return {"month_first": parsed, "day_first": parsed}
    result: dict[DateOrder, datetime] = {}
    for order in ("month_first", "day_first"):
        try:
            result[order] = _parse_ordered_datetime(text, date_order=order)
        except ValueError:
            continue
    return result


_TIME_SUFFIX = r"(?:\s+\d{1,2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?)?"
_EXPLICIT_TIME = re.compile(r"(?:T|\s)\d{1,2}:\d{2}\b")
_SLASH_DATE = re.compile(rf"^\d{{1,4}}/\d{{1,2}}/\d{{1,4}}{_TIME_SUFFIX}$")
_DAY_MONTH_NAME = re.compile(rf"^\d{{1,2}}-[A-Za-z]{{3,9}}-\d{{2,4}}{_TIME_SUFFIX}$")
_MONTH_NAME_DATE = re.compile(
    rf"^[A-Za-z]{{3,9}}\s+\d{{1,2}},\s*\d{{2,4}}{_TIME_SUFFIX}$"
)


def _parse_ordered_datetime(text: str, *, date_order: DateOrder) -> datetime:
    date_formats = (
        ("%m/%d/%Y", "%m/%d/%y")
        if date_order == "month_first"
        else ("%d/%m/%Y", "%d/%m/%y")
    )
    if _SLASH_DATE.fullmatch(text):
        formats = (*date_formats, "%Y/%m/%d")
    elif _DAY_MONTH_NAME.fullmatch(text):
        formats = ("%d-%b-%Y",)
    elif _MONTH_NAME_DATE.fullmatch(text):
        formats = ("%b %d, %Y",)
    else:
        raise ValueError("invalid date")
    for date_format in formats:
        for time_format in ("", " %H:%M", " %H:%M:%S", " %H:%M:%S.%f"):
            try:
                return datetime.strptime(text, date_format + time_format)
            except ValueError:
                continue
    raise ValueError("invalid date")
