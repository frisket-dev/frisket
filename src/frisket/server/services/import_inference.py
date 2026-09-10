"""Column type and Markdown inference shared by tabular imports."""

from __future__ import annotations

import re
from typing import Any, Sequence

from frisket.csv_numbers import normalize_csv_number


_MD_STRONG = (
    re.compile(r"(?m)^#{1,6} +\S"),
    re.compile(r"(?m)^```"),
)
_MD_WEAK = (
    re.compile(r"(?m)^ {0,3}[-*+] +\S"),
    re.compile(r"(?m)^ {0,3}\d+[.)] +\S"),
    re.compile(r"\*\*[^*\n]+\*\*|__[^_\n]+__"),
    re.compile(
        r"(?<![*\w])\*[^*\s][^*\n]*\*(?![*\w])"
        r"|(?<![_\w])_[^_\s][^_\n]*_(?![_\w])"
    ),
    re.compile(r"`[^`\n]+`"),
    re.compile(r"!?\[[^\]\n]+\]\([^)\s]+\)"),
    re.compile(r"(?m)^ {0,3}> +\S"),
)


def _looks_markdown(text: str) -> bool:
    if any(pattern.search(text) for pattern in _MD_STRONG):
        return True
    return sum(1 for pattern in _MD_WEAK if pattern.search(text)) >= 2


def sniff_markdown(samples: Sequence[Any]) -> bool:
    """Return whether nearly all non-empty sampled values are Markdown."""

    values = [str(sample) for sample in samples if sample not in (None, "")]
    if not values:
        return False
    return sum(1 for value in values if _looks_markdown(value)) / len(values) >= 0.8


# Preserve only numeric spellings that round-trip without silently changing an
# identifier (for example, leading-zero ZIP codes or large integer IDs).
_PLAIN_NUMERAL = re.compile(r"^-?(?:(?:0|[1-9][0-9]*)(?:\.[0-9]+)?|\.[0-9]+)$")
_MAX_EXACT_INTEGER = 2**53 - 1


def infer_column_type(
    name: str,
    samples: Sequence[Any],
    *,
    decimal_separator: str = ".",
) -> str:
    """Infer the safe default type for one imported column."""

    values = [sample for sample in samples if sample not in (None, "")]
    if not values:
        return "text"
    lower_name = name.lower()
    if any(key in lower_name for key in ("image", "photo", "thumbnail")) and all(
        str(value).startswith("http") for value in values
    ):
        return "image"
    if "video" in lower_name and all(str(value).startswith("http") for value in values):
        return "video"
    if any(key in lower_name for key in ("audio", "mp3", "podcast")) and all(
        str(value).startswith("http") for value in values
    ):
        return "audio"
    if all(str(value).startswith(("http://", "https://")) for value in values):
        return "link"

    normalized = [
        normalize_csv_number(str(value), decimal_separator=decimal_separator)
        for value in values
    ]
    if not all(_PLAIN_NUMERAL.fullmatch(value) for value in normalized):
        return "text"
    if any("." in value for value in normalized):
        return "number"
    if any(abs(int(value)) > _MAX_EXACT_INTEGER for value in normalized):
        return "text"
    return "integer"
