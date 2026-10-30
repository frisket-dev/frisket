"""Pure expected-answer matching, shared with the workbench implementation.

Only null and the empty string mean an unchecked expectation; whitespace is a
value. Sets accept arrays or JSON-encoded arrays, never comma-separated text.
Dates accept ISO dates, English month names, and US month/day/year slashes;
two-digit slash years denote 2000–2099.
"""

import json
import math
import re
from datetime import date

_NUMBER = re.compile(
    r"^([+-]?)(?:\$)?((?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?|\.[0-9]+)(?:[eE]([+-]?[0-9]+))?$"
)
_MONTHS = {
    name: index
    for index, names in enumerate(
        (
            "jan january",
            "feb february",
            "mar march",
            "apr april",
            "may",
            "jun june",
            "jul july",
            "aug august",
            "sep sept september",
            "oct october",
            "nov november",
            "dec december",
        ),
        1,
    )
    for name in names.split()
}


# ECMAScript trim whitespace, so browser and backend agree on non-ASCII text.
_WHITESPACE = "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"


def _trim(value):
    return value.strip(_WHITESPACE)


def _canonical(value):
    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        return ("number", _number(value))
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, list):
        return ("array", tuple(_canonical(v) for v in value))
    if isinstance(value, dict):
        return ("object", tuple((k, _canonical(v)) for k, v in sorted(value.items())))
    return ("unsupported",)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    match = _NUMBER.fullmatch(_trim(str(value)))
    if not match:
        return None
    sign, digits, exponent = match.groups()
    digits = digits.replace(",", "")
    whole, _, fraction = digits.partition(".")
    coefficient = (whole + fraction).lstrip("0")
    if not coefficient:
        return ("", "0", 0)
    power = int(exponent or 0) - len(fraction)
    trailing = len(coefficient) - len(coefficient.rstrip("0"))
    return ("-" if sign == "-" else "", coefficient.rstrip("0"), power + trailing)


def _date(value):
    if not isinstance(value, str):
        return None
    value = _trim(value)
    iso = re.fullmatch(r"([0-9]{4})-([0-9]{2})-([0-9]{2})", value)
    slash = re.fullmatch(r"([0-9]{1,2})/([0-9]{1,2})/([0-9]{2}|[0-9]{4})", value)
    space = f"[{re.escape(_WHITESPACE)}]"
    month = re.fullmatch(
        rf"([A-Za-z]+){space}+([0-9]{{1,2}}),?{space}+([0-9]{{4}})", value
    )
    try:
        if iso:
            return date(*(int(x) for x in iso.groups()))
        if slash:
            m, d, y = slash.groups()
            return date(int(y) + (2000 if len(y) == 2 else 0), int(m), int(d))
        if month and month.group(1).lower() in _MONTHS:
            return date(
                int(month.group(3)),
                _MONTHS[month.group(1).lower()],
                int(month.group(2)),
            )
    except ValueError:
        pass
    return None


def _set(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    if not isinstance(value, list):
        return None
    return {_canonical(item) for item in value}


def match_answer(rule, expected, answer, *, state="value", hand_verdict=None):
    """Return match, miss, unchecked, unscored, or not_run for one answer."""
    if state in ("missing", "pending", "cancelled"):
        return "not_run"
    if expected is None or expected == "":
        return "unchecked"
    if state == "error":
        return "miss"
    if rule == "hand":
        return hand_verdict if hand_verdict in ("match", "miss") else "unscored"
    if expected == "[empty]":
        empty = (
            answer is None
            or (isinstance(answer, str) and not _trim(answer))
            or answer == []
        )
        return "match" if empty else "miss"
    if rule == "exact":
        matches = _canonical(expected) == _canonical(answer)
    elif rule == "ignore_case":
        matches = (
            isinstance(expected, str)
            and isinstance(answer, str)
            and _trim(expected).lower() == _trim(answer).lower()
        )
    elif rule in ("number", "date", "set"):
        parse = {"number": _number, "date": _date, "set": _set}[rule]
        left, right = parse(expected), parse(answer)
        matches = left is not None and right is not None and left == right
    else:
        raise ValueError(f"Unknown match rule: {rule}")
    return "match" if matches else "miss"
