"""Native SQLite encoding for cell authority values.

The kind column is part of the value: SQLite's storage class alone cannot
distinguish booleans from integers, explicit nulls from absent payloads, or
exact out-of-range integers from ordinary text.  New writes use
``encode_stored_value``.  The schema migration additionally uses
``migrate_legacy_json_value`` so malformed historical JSON remains byte-for-
byte recoverable instead of being discarded or coerced.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, TypeAlias


SQLiteValue: TypeAlias = str | int | float | None

VALUE_KINDS = frozenset(
    {
        "null",
        "text",
        "integer",
        "real",
        "boolean",
        "json",
        "bigint",
        "legacy_invalid",
    }
)

_INTEGER_MIN = -(2**63)
_INTEGER_MAX = 2**63 - 1
_CANONICAL_INTEGER = re.compile(r"-?(?:0|[1-9]\d*)\Z")


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"non-JSON numeric constant: {value}")


def _encode_complex(value: Any) -> str:
    return json.dumps(value, allow_nan=False)


def encode_stored_value(value: Any) -> tuple[str, SQLiteValue]:
    """Encode one decoded cell value using a native SQLite storage class."""

    if value is None:
        return "null", None
    if isinstance(value, bool):
        return "boolean", int(value)
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            # sqlite3 cannot bind lone surrogates as TEXT. The existing
            # legacy_invalid read path also carries bindable escaped JSON for
            # historical strings with this representation and decodes them
            # back to their original string value.
            return "legacy_invalid", _encode_complex(value)
        return "text", value
    if isinstance(value, int):
        if _INTEGER_MIN <= value <= _INTEGER_MAX:
            return "integer", value
        return "bigint", str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("cell numbers must be finite")
        return "real", value
    # Match the former json.dumps boundary, including tuple -> JSON array.
    encoded = _encode_complex(value)
    decoded = json.loads(encoded, parse_constant=_reject_non_json_constant)
    if not isinstance(decoded, (list, dict)):
        raise TypeError(f"unsupported cell value type: {type(value).__name__}")
    return "json", encoded


def migrate_legacy_json_value(encoded: str | None) -> tuple[str, SQLiteValue]:
    """Convert one historical JSON payload without losing malformed text.

    Valid arrays and objects retain their original serialized bytes.  Valid
    scalars move to native SQLite storage.  SQL NULL historically decoded as
    an empty value and becomes an explicit null authority row; callers that
    use SQL NULL for an absent/error payload must handle that case before
    invoking this helper.
    """

    if encoded is None:
        return "null", None
    try:
        decoded = json.loads(encoded, parse_constant=_reject_non_json_constant)
        # Reject overflow-to-infinity and non-finite values nested in containers.
        json.dumps(decoded, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return "legacy_invalid", encoded
    if isinstance(decoded, (list, dict)):
        return "json", encoded
    if isinstance(decoded, str):
        try:
            decoded.encode("utf-8")
        except UnicodeEncodeError:
            # sqlite3 binds Python strings as UTF-8. Preserve the escaped JSON
            # bytes when a historical scalar contains a lone surrogate rather
            # than failing the whole project migration.
            return "legacy_invalid", encoded
    return encode_stored_value(decoded)


def decode_stored_value(
    value_kind: str | None,
    stored_value: SQLiteValue,
    *,
    tolerate_errors: bool = False,
) -> Any:
    """Decode one authority payload, preserving the old tolerant-read option."""

    try:
        if value_kind is None:
            return None
        if value_kind == "null":
            if stored_value is not None:
                raise ValueError("null cell payload has a non-null value")
            return None
        if value_kind == "text":
            if not isinstance(stored_value, str):
                raise ValueError("text cell payload is not SQLite text")
            return stored_value
        if value_kind == "integer":
            if type(stored_value) is not int:
                raise ValueError("integer cell payload is not SQLite integer")
            return stored_value
        if value_kind == "real":
            if not isinstance(stored_value, float) or not math.isfinite(stored_value):
                raise ValueError("real cell payload is not a finite SQLite real")
            return stored_value
        if value_kind == "boolean":
            if type(stored_value) is not int or stored_value not in (0, 1):
                raise ValueError("boolean cell payload is not 0 or 1")
            return bool(stored_value)
        if value_kind == "json":
            if not isinstance(stored_value, str):
                raise ValueError("JSON cell payload is not SQLite text")
            decoded = json.loads(stored_value, parse_constant=_reject_non_json_constant)
            if not isinstance(decoded, (list, dict)):
                raise ValueError("JSON cell payload is not an array or object")
            return decoded
        if value_kind == "bigint":
            if (
                not isinstance(stored_value, str)
                or _CANONICAL_INTEGER.fullmatch(stored_value) is None
            ):
                raise ValueError("bigint cell payload is not a canonical integer")
            value = int(stored_value)
            if _INTEGER_MIN <= value <= _INTEGER_MAX:
                raise ValueError("bigint cell payload fits SQLite integer storage")
            return value
        if value_kind == "legacy_invalid":
            if not isinstance(stored_value, str):
                raise ValueError("legacy invalid payload is not SQLite text")
            # Replay the historical strict decode failure rather than presenting
            # malformed serialization as a user-authored text value.
            return json.loads(stored_value, parse_constant=_reject_non_json_constant)
        raise ValueError(f"unknown stored value kind: {value_kind!r}")
    except (TypeError, ValueError, OverflowError, RecursionError):
        if tolerate_errors:
            return None
        raise
