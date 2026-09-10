"""Shared CSV/XLSX lexical conversion into registered column values."""

from __future__ import annotations

import json
from typing import Any

from frisket.contracts.actions.schemas._base import canonical_column_type
from frisket.contracts.actions.schemas.imports import normalize_import_value
from frisket.csv_numbers import normalize_csv_number


def parse_csv_value(raw: str, type_name: str, *, decimal_separator: str = ".") -> Any:
    canonical = canonical_column_type(type_name)
    text = raw.strip()
    value: Any = raw
    if raw == "":
        value = None
    elif canonical == "integer":
        value = int(normalize_csv_number(text, decimal_separator=decimal_separator))
    elif canonical == "number":
        value = float(normalize_csv_number(text, decimal_separator=decimal_separator))
    elif canonical == "boolean":
        lowered = text.lower()
        if lowered in {"true", "1", "yes"}:
            value = True
        elif lowered in {"false", "0", "no"}:
            value = False
        else:
            raise ValueError("invalid boolean")
    elif canonical == "json":
        value = json.loads(raw)
    return normalize_import_value(canonical, value)
