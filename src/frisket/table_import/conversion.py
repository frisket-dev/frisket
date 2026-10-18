"""Post-read conversion API; implementations never mutate source text."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import math
import re

from .models import (
    CellConversion,
    ConversionIssue,
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
        if target_type == "integer":
            decimal = _parse_decimal(
                original,
                decimal_separator=decimal_separator,
                thousands_separator=thousands_separator,
            )
            if decimal != decimal.to_integral_value():
                raise ValueError("not a whole number")
            value = int(decimal)
            if not -(2**63) <= value <= 2**63 - 1:
                raise ValueError("integer is outside the supported range")
            display = str(value)
        elif target_type == "number":
            decimal = _parse_decimal(
                original,
                decimal_separator=decimal_separator,
                thousands_separator=thousands_separator,
            )
            value = float(decimal)
            if not math.isfinite(value):
                raise ValueError("number is outside the supported range")
            display = _display_decimal(decimal)
        elif target_type == "boolean":
            value = _parse_boolean(original)
            display = "Yes" if value else "No"
        elif target_type == "date":
            parsed = _parse_datetime(original, date_order=date_order)
            value = parsed.date().isoformat()
            display = value
        elif target_type == "datetime":
            parsed = _parse_datetime(original, date_order=date_order)
            value = parsed.isoformat()
            display = value.replace("T", " ")
        else:  # pragma: no cover - TargetType keeps this closed for typed callers.
            raise ValueError(f"unsupported target type: {target_type}")
    except (InvalidOperation, OverflowError, ValueError):
        issue = ConversionIssue(
            "conversion_failed",
            f"{original} could not be converted to {target_type}",
            original,
        )
        return CellConversion(original, None, "", "invalid", issue)

    outcome = "unchanged" if display == original else "changed"
    return CellConversion(original, value, display, outcome)


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
    value = Decimal(text)
    if not value.is_finite():
        raise ValueError("non-finite number")
    if negative:
        value = -value
    if percent:
        value /= Decimal(100)
    return value


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
        pass
    try:
        parsed_date = date.fromisoformat(text)
        return datetime.combine(parsed_date, datetime.min.time())
    except ValueError:
        pass
    formats = (
        ("%m/%d/%Y", "%m/%d/%y")
        if date_order == "month_first"
        else ("%d/%m/%Y", "%d/%m/%y")
    )
    for date_format in (*formats, "%Y/%m/%d", "%d-%b-%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, date_format)
        except ValueError:
            continue
    raise ValueError("invalid date")
