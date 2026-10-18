"""Streaming delimited-text parsing and bounded format inspection."""

from __future__ import annotations

import codecs
import csv
import io
from pathlib import Path
import re
from typing import Iterator

from .models import RawCell, RawRow
from .reader import TableReadError

_SAMPLE_BYTES = 64 * 1024
_DELIMITERS = (",", ";", "\t", "|")


class DelimitedRows:
    def __init__(self, path: Path, *, encoding: str, delimiter: str) -> None:
        self._binary = path.open("rb")
        codec = "utf-8-sig" if encoding == "utf-8" else encoding
        self._text = io.TextIOWrapper(
            self._binary, encoding=codec, errors="strict", newline=""
        )
        self._reader = csv.reader(self._text, delimiter=delimiter, strict=True)
        self._ordinal = 0
        self._previous_line = 0
        self._closed = False

    def __iter__(self) -> Iterator[RawRow]:
        return self

    def __next__(self) -> RawRow:
        try:
            values = next(self._reader)
        except StopIteration:
            raise
        except (csv.Error, UnicodeError) as exc:
            raise TableReadError(
                f"delimited source could not be parsed: {exc}"
            ) from exc
        self._ordinal += 1
        line_end = self._reader.line_num
        row = RawRow(
            record_ordinal=self._ordinal,
            physical_line_start=self._previous_line + 1,
            physical_line_end=line_end,
            cells=tuple(RawCell(value) for value in values),
        )
        self._previous_line = line_end
        return row

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._text.close()


def inspect_delimited(path: Path) -> tuple[str, str, str, str | None]:
    with path.open("rb") as source:
        prefix = source.read(_SAMPLE_BYTES)
    if not prefix:
        raise TableReadError("empty table source")
    encoding = detect_encoding(prefix)
    codec = "utf-8-sig" if encoding == "utf-8" else encoding
    try:
        decoder = codecs.getincrementaldecoder(codec)(errors="strict")
        text = decoder.decode(prefix, final=False)
    except UnicodeDecodeError as exc:
        raise TableReadError(f"table prefix is not valid {encoding}") from exc
    delimiter = detect_delimiter(text)
    decimal, thousands = detect_number_convention(text, delimiter=delimiter)
    return encoding, delimiter, decimal, thousands


def detect_encoding(prefix: bytes) -> str:
    if prefix.startswith(codecs.BOM_UTF8):
        return "utf-8"
    if prefix.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    if prefix.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return "utf-32"
    try:
        prefix.decode("utf-8", errors="strict")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes

        match = from_bytes(prefix).best()
        guessed = "" if match is None or match.encoding is None else match.encoding
    except Exception:  # pragma: no cover - dependency failures use safe fallback.
        guessed = ""
    normalized = guessed.casefold().replace("_", "-")
    if normalized in {"utf-8", "ascii"}:
        return "utf-8"
    if normalized.startswith("utf-16"):
        return "utf-16"
    if normalized in {"mac-roman", "macroman"}:
        return "mac_roman"
    if normalized in {"iso-8859-1", "latin-1", "latin1"}:
        return "iso-8859-1"
    return "cp1252"


def detect_delimiter(text: str) -> str:
    best = ","
    best_score: tuple[float, int, int] | None = None
    for order, delimiter in enumerate(_DELIMITERS):
        try:
            widths = [
                len(row)
                for _, row in zip(
                    range(100), csv.reader(io.StringIO(text), delimiter=delimiter)
                )
                if row and any(value != "" for value in row)
            ]
        except csv.Error:
            continue
        if not widths or max(widths) <= 1:
            continue
        modal = max(set(widths), key=lambda width: (widths.count(width), width))
        score = (widths.count(modal) / len(widths), modal, -order)
        if best_score is None or score > best_score:
            best, best_score = delimiter, score
    return best


_COMMA_DECIMAL = re.compile(r"^[+-]?(?:\d{1,3}(?:\.\d{3})+|\d+),\d+$")
_DOT_DECIMAL = re.compile(r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d+$")


def detect_number_convention(text: str, *, delimiter: str) -> tuple[str, str | None]:
    comma = dot = 0
    try:
        rows = csv.reader(io.StringIO(text), delimiter=delimiter)
        for row_number, row in enumerate(rows):
            if row_number >= 100:
                break
            for value in row:
                sample = value.strip()
                comma += bool(_COMMA_DECIMAL.fullmatch(sample))
                dot += bool(_DOT_DECIMAL.fullmatch(sample))
    except csv.Error:
        pass
    if comma > dot:
        return ",", "."
    return ".", ","
