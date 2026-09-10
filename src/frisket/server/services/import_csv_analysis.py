"""CSV decoding, scanning, and column analysis for import workflows."""

from __future__ import annotations

import codecs
import csv
import io
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from frisket.server.services.import_csv_errors import ImportCsvRouteError
from frisket.server.services.import_inference import infer_column_type, sniff_markdown


@dataclass(frozen=True)
class CsvUploadAnalysis:
    source_encoding: str
    delimiter: str
    decimal_separator: str
    fieldnames: list[str]
    sample_records: list[dict[str, str | None]]
    row_count: int
    columns: list[dict[str, Any]]


@dataclass(frozen=True)
class CsvFileScan:
    source: BinaryIO
    logical_path: str
    source_encoding: str
    delimiter: str
    decimal_separator: str
    fieldnames: list[str]
    row_count: int
    columns: list[dict[str, Any]]


def _detect_csv_delimiter(raw: str) -> str:
    """Choose among the common delimited-text conventions.

    A delimiter present in the header is strong evidence.  Row-shape
    consistency breaks ties; a genuinely one-column or ambiguous file keeps
    the historical comma default.
    """
    best_delimiter = ","
    best_score: tuple[float, int] | None = None
    for delimiter in (",", ";", "\t"):
        try:
            reader = csv.reader(io.StringIO(raw), delimiter=delimiter)
            widths: list[int] = []
            for row in reader:
                if not row or not any(value.strip() for value in row):
                    continue
                widths.append(len(row))
                if len(widths) >= 51:
                    break
        except csv.Error:
            continue
        if not widths or widths[0] <= 1:
            continue
        matching_rows = sum(width == widths[0] for width in widths)
        score = (matching_rows / len(widths), widths[0])
        if best_score is None or score > best_score:
            best_delimiter = delimiter
            best_score = score
    return best_delimiter


# A byte-order mark is the file declaring its own encoding, so it is read as
# a declaration rather than guessed at.  Excel's "Unicode Text" export is
# UTF-16, whose NUL bytes are perfectly legal UTF-8 -- without this the file
# decodes "successfully" into interleaved NULs.  Longest marks first: the
# UTF-32LE mark starts with the UTF-16LE one.
_BOM_ENCODINGS: tuple[tuple[bytes, str], ...] = (
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)

# Tried in order when the upload does not declare an encoding.  Both rungs
# decode strictly: a codec either accepts every byte or is rejected, so no
# character is ever replaced.  cp1252 is what Excel on Windows and most
# municipal data portals emit; it is also the last rung, which keeps the
# refusal reachable rather than letting a never-failing codec (latin-1)
# turn every file into plausible-looking mojibake.
_ENCODING_LADDER: tuple[str, ...] = ("utf-8-sig", "cp1252")

# The browser offers this exact finite set. Keeping the HTTP boundary finite
# prevents Python's transform codecs (for example base64_codec) from reaching
# ``bytes.decode`` and turning a user error into a 500. UTF-8 is decoded with
# ``utf-8-sig`` so an explicit choice strips an optional BOM just like auto
# detection does.
_SUPPORTED_ENCODINGS: dict[str, str] = {
    "utf-8": "utf-8-sig",
    "cp1252": "cp1252",
    "iso-8859-1": "iso-8859-1",
    "iso-8859-2": "iso-8859-2",
    "shift_jis": "shift_jis",
    "mac_roman": "mac_roman",
    "utf-16": "utf-16",
    "utf-32": "utf-32",
}


def _decode_upload(raw_bytes: bytes, *, declared: str | None) -> tuple[str, str]:
    """Return the CSV text and the codec that produced it.

    Decoding is strict everywhere.  The uploaded bytes are the user's data:
    they are staged unchanged and this only decides which codec the executor
    is told to read them back with.
    """
    if declared is not None:
        resolved = _SUPPORTED_ENCODINGS.get(declared)
        if resolved is None:
            supported = ", ".join(_SUPPORTED_ENCODINGS)
            raise ImportCsvRouteError(
                400,
                f"unsupported CSV encoding {declared!r}; choose one of: {supported}",
            )
        candidates: tuple[str, ...] = (resolved,)
    else:
        candidates = next(
            (
                (encoding,)
                for mark, encoding in _BOM_ENCODINGS
                if raw_bytes.startswith(mark)
            ),
            _ENCODING_LADDER,
        )
    for candidate in candidates:
        try:
            return raw_bytes.decode(candidate), candidate
        except UnicodeDecodeError:
            continue
    tried = ", ".join(candidates)
    raise ImportCsvRouteError(
        400,
        f"CSV file is not valid {tried}. Nothing was changed. Re-upload it with "
        "?encoding=<name> using one of the supported character encodings, or "
        "re-save it as UTF-8.",
    )


def _analyze_upload(raw_bytes: bytes, *, encoding: str | None) -> CsvUploadAnalysis:
    """Decode and parse once for both preview and the eventual write."""

    raw, source_encoding = _decode_upload(raw_bytes, declared=encoding)
    delimiter = _detect_csv_delimiter(raw)
    decimal_separator = "," if delimiter == ";" else "."
    reader = csv.DictReader(io.StringIO(raw), delimiter=delimiter)
    sample_records: list[dict[str, str | None]] = []
    row_count = 0
    for record in reader:
        row_count += 1
        if len(sample_records) < 50:
            sample_records.append(record)
    if row_count == 0:
        raise ImportCsvRouteError(400, "empty CSV")

    fieldnames = list(reader.fieldnames or sample_records[0].keys())
    columns: list[dict[str, Any]] = []
    for col_name in fieldnames:
        samples = [record.get(col_name) for record in sample_records]
        column_type = infer_column_type(
            col_name,
            samples,
            decimal_separator=decimal_separator,
        )
        column: dict[str, Any] = {"name": col_name, "type": column_type}
        if column_type == "text" and sniff_markdown(samples):
            column["format"] = "markdown"
        columns.append(column)

    return CsvUploadAnalysis(
        source_encoding=source_encoding,
        delimiter=delimiter,
        decimal_separator=decimal_separator,
        fieldnames=fieldnames,
        sample_records=sample_records,
        row_count=row_count,
        columns=columns,
    )


_PATH_SAMPLE_BYTES = 64 * 1024
_PATH_READ_BYTES = 1024 * 1024


def inspect_csv_header(path: Path) -> tuple[list[str], str, str, str]:
    """Inspect only a bounded prefix for deterministic bulk planning."""

    with path.open("rb") as source:
        return inspect_csv_header_stream(source)


def inspect_csv_header_stream(
    source: BinaryIO,
) -> tuple[list[str], str, str, str]:
    source.seek(0)
    prefix = source.read(_PATH_SAMPLE_BYTES)
    if not prefix:
        raise ImportCsvRouteError(400, "empty CSV")
    declared = next(
        (encoding for mark, encoding in _BOM_ENCODINGS if prefix.startswith(mark)),
        None,
    )
    candidates = (declared,) if declared else _ENCODING_LADDER
    text = None
    source_encoding = ""
    for candidate in candidates:
        try:
            decoder = codecs.getincrementaldecoder(candidate)(errors="strict")
            text = decoder.decode(prefix, final=False)
            source_encoding = candidate
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ImportCsvRouteError(400, "CSV header could not be decoded")
    delimiter = _detect_csv_delimiter(text)
    try:
        header = next(csv.reader(io.StringIO(text), delimiter=delimiter, strict=True))
    except (StopIteration, csv.Error) as exc:
        raise ImportCsvRouteError(400, "CSV header could not be parsed") from exc
    fieldnames = _strict_header(header)
    return (
        fieldnames,
        source_encoding,
        delimiter,
        "," if delimiter == ";" else ".",
    )


def scan_csv_file(
    path: Path,
    *,
    logical_path: str,
    encoding: str | None = None,
    force_text_columns: bool = False,
) -> CsvFileScan:
    """Validate an entire CSV and infer types with bounded working memory."""

    source = path.open("rb")
    try:
        return scan_csv_stream(
            source,
            logical_path=logical_path,
            encoding=encoding,
            force_text_columns=force_text_columns,
        )
    except Exception:
        source.close()
        raise


def scan_csv_stream(
    source: BinaryIO,
    *,
    logical_path: str,
    encoding: str | None = None,
    force_text_columns: bool = False,
) -> CsvFileScan:
    source_encoding = _resolve_stream_encoding(source, declared=encoding)
    sample = _read_text_prefix(source, source_encoding)
    delimiter = _detect_csv_delimiter(sample)
    decimal_separator = "," if delimiter == ";" else "."
    fieldnames: list[str] | None = None
    inferred: list[str | None] = []
    samples: list[list[str]] = []
    row_count = 0
    try:
        with _text_view(source, source_encoding) as text_source:
            reader = csv.reader(text_source, delimiter=delimiter, strict=True)
            raw_header = next(reader, None)
            if raw_header is None:
                raise ImportCsvRouteError(400, "empty CSV")
            fieldnames = _strict_header(raw_header)
            inferred = [None] * len(fieldnames)
            samples = [[] for _ in fieldnames]
            for line_number, row in enumerate(reader, start=2):
                if len(row) != len(fieldnames):
                    raise ImportCsvRouteError(
                        400,
                        f"CSV row {line_number} has {len(row)} values; "
                        f"expected {len(fieldnames)}",
                    )
                row_count += 1
                if force_text_columns:
                    continue
                for index, value in enumerate(row):
                    if len(samples[index]) < 50:
                        samples[index].append(value)
                    detected = infer_column_type(
                        fieldnames[index],
                        [value],
                        decimal_separator=decimal_separator,
                    )
                    inferred[index] = _widen_csv_type(inferred[index], detected, value)
    except ImportCsvRouteError:
        raise
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise ImportCsvRouteError(400, f"CSV file could not be parsed: {exc}") from exc
    if row_count == 0:
        raise ImportCsvRouteError(400, "empty CSV")
    assert fieldnames is not None
    columns = []
    for index, name in enumerate(fieldnames):
        column = {
            "name": name,
            "type": "text" if force_text_columns else (inferred[index] or "text"),
        }
        if column["type"] == "text" and sniff_markdown(samples[index]):
            column["format"] = "markdown"
        columns.append(column)
    return CsvFileScan(
        source=source,
        logical_path=logical_path,
        source_encoding=source_encoding,
        delimiter=delimiter,
        decimal_separator=decimal_separator,
        fieldnames=fieldnames,
        row_count=row_count,
        columns=columns,
    )


def widen_csv_scans(scans: list[CsvFileScan]) -> list[dict[str, Any]]:
    if not scans:
        raise ImportCsvRouteError(400, "CSV output has no files")
    first = scans[0].fieldnames
    expected = set(first)
    for scan in scans[1:]:
        if set(scan.fieldnames) != expected:
            raise ImportCsvRouteError(400, "CSV headers are not compatible")
    by_scan = [
        {column["name"]: column["type"] for column in scan.columns} for scan in scans
    ]
    columns: list[dict[str, Any]] = []
    for name in first:
        current: str | None = None
        for types in by_scan:
            current = _merge_column_types(current, str(types[name]))
        column: dict[str, Any] = {"name": name, "type": current or "text"}
        if column["type"] == "text" and any(
            next(item for item in scan.columns if item["name"] == name).get("format")
            == "markdown"
            for scan in scans
        ):
            column["format"] = "markdown"
        columns.append(column)
    return columns


def _strict_header(header: list[str]) -> list[str]:
    names = [str(value).strip() for value in header]
    if not names or any(not name for name in names):
        raise ImportCsvRouteError(400, "CSV headers must be non-empty")
    if len(names) != len(set(names)):
        raise ImportCsvRouteError(400, "CSV headers must be unique")
    return names


def _resolve_path_encoding(path: Path, *, declared: str | None) -> str:
    with path.open("rb") as source:
        return _resolve_stream_encoding(source, declared=declared)


def _resolve_stream_encoding(source: BinaryIO, *, declared: str | None) -> str:
    if declared is not None:
        resolved = _SUPPORTED_ENCODINGS.get(declared)
        if resolved is None:
            supported = ", ".join(_SUPPORTED_ENCODINGS)
            raise ImportCsvRouteError(
                400,
                f"unsupported CSV encoding {declared!r}; choose one of: {supported}",
            )
        candidates = (resolved,)
    else:
        source.seek(0)
        prefix = source.read(4)
        candidates = next(
            (
                (encoding,)
                for mark, encoding in _BOM_ENCODINGS
                if prefix.startswith(mark)
            ),
            _ENCODING_LADDER,
        )
    for candidate in candidates:
        decoder = codecs.getincrementaldecoder(candidate)(errors="strict")
        try:
            source.seek(0)
            while chunk := source.read(_PATH_READ_BYTES):
                decoder.decode(chunk)
            decoder.decode(b"", final=True)
            return candidate
        except UnicodeDecodeError:
            continue
    tried = ", ".join(candidates)
    raise ImportCsvRouteError(
        400,
        f"CSV file is not valid {tried}. Nothing was changed. Re-upload it with "
        "?encoding=<name> using one of the supported character encodings, or "
        "re-save it as UTF-8.",
    )


def _widen_csv_type(current: str | None, detected: str, value: str) -> str | None:
    if value == "":
        return current
    return _merge_column_types(current, detected)


def _merge_column_types(current: str | None, incoming: str) -> str:
    if current is None:
        return incoming
    if current == incoming:
        return current
    if {current, incoming} <= {"integer", "number"}:
        return "number"
    return "text"


@contextmanager
def _text_view(source: BinaryIO, encoding: str):
    source.seek(0)
    wrapper = io.TextIOWrapper(source, encoding=encoding, newline="")
    try:
        yield wrapper
    finally:
        wrapper.detach()


def _read_text_prefix(source: BinaryIO, encoding: str) -> str:
    source.seek(0)
    prefix = source.read(_PATH_SAMPLE_BYTES)
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    return decoder.decode(prefix, final=False)
