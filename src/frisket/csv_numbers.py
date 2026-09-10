"""Locale-aware numeric normalization for delimited-text imports."""

from __future__ import annotations


def normalize_csv_number(text: str, *, decimal_separator: str = ".") -> str:
    """Return a Python-parseable number using the declared CSV convention.

    CSV files that use semicolons commonly use commas for decimals and dots
    for digit grouping.  Comma-delimited files use the inverse convention.
    Non-breaking spaces are also accepted as digit-group separators.
    """
    normalized = text.strip().replace("\u00a0", "").replace("\u202f", "")
    if decimal_separator == ",":
        return normalized.replace(".", "").replace(",", ".")
    return normalized.replace(",", "")
