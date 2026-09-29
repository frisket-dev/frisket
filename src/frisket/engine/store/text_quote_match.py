"""Exact source-text quote matching with whitespace-equivalent ranges."""

from __future__ import annotations


def _normalized_with_ranges(text: str) -> tuple[str, list[tuple[int, int]]]:
    normalized: list[str] = []
    ranges: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        if text[index].isspace():
            end = index + 1
            while end < len(text) and text[end].isspace():
                end += 1
            normalized.append(" ")
            ranges.append((index, end))
            index = end
            continue
        normalized.append(text[index])
        ranges.append((index, index + 1))
        index += 1
    return "".join(normalized), ranges


def quote_ranges(source: str, quote: str) -> list[tuple[int, int]]:
    """Return every exact or whitespace-equivalent half-open source range.

    Offsets index Python Unicode codepoints in the original frozen ``source``.
    Each maximal whitespace run compares as one ordinary space, while the
    returned range still covers the complete original run.
    """

    if not source or not quote:
        return []
    normalized_source, source_ranges = _normalized_with_ranges(source)
    normalized_quote, _ = _normalized_with_ranges(quote)
    if not normalized_quote:
        return []

    matches: list[tuple[int, int]] = []
    start = 0
    while True:
        found = normalized_source.find(normalized_quote, start)
        if found < 0:
            return matches
        last = found + len(normalized_quote) - 1
        matches.append((source_ranges[found][0], source_ranges[last][1]))
        start = found + 1
