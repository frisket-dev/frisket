"""Public reader signatures for immutable staged table sources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from pathlib import Path
from types import TracebackType
from typing import Any, Iterator, Self
import zipfile
from itertools import islice

from .models import (
    RawCell,
    RawRow,
    PrefixRead,
    RowIssue,
    SourceColumn,
    SourceKind,
    TableReadSettings,
    TableRow,
    TableSourceInfo,
)


class TableReadError(ValueError):
    """The immutable source cannot be read with the selected settings."""


class TableRecordCountRequired(TableReadError):
    """A footer cannot be excluded safely until the logical count is known."""


_RETAINED_SKIPPED_ROWS = 16


class TableReader(ABC):
    """One open source claim. Iteration is single-pass and closes explicitly."""

    @property
    @abstractmethod
    def columns(self) -> tuple[SourceColumn, ...]: ...

    @abstractmethod
    def __iter__(self) -> Iterator[TableRow]: ...

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def inspect_source(path: Path) -> TableSourceInfo:
    """Inspect a bounded prefix or workbook metadata without retaining rows."""
    kind = _source_kind(path)
    if kind == "delimited":
        from .delimited import inspect_delimited

        encoding, delimiter, decimal, thousands = inspect_delimited(path)
        return TableSourceInfo(
            kind=kind,
            detected_delimiter=delimiter,
            detected_encoding=encoding,
            detected_decimal_separator=decimal,  # type: ignore[arg-type]
            detected_thousands_separator=thousands,
        )
    from .workbook import inspect_workbook

    return TableSourceInfo(kind=kind, sheets=inspect_workbook(path, kind=kind))


def count_records(path: Path, settings: TableReadSettings) -> int:
    """Count logical parser records with the same parser used for iteration."""
    rows = _open_raw(path, settings)
    try:
        return sum(1 for _ in rows)
    finally:
        rows.close()


def open_table(
    path: Path,
    settings: TableReadSettings,
    *,
    total_record_count: int | None = None,
) -> TableReader:
    """Open one single-pass configured reader over an immutable source path."""
    if settings.skip_rows_end and total_record_count is None:
        raise TableRecordCountRequired(
            "skip_rows_end requires a completed logical-record count"
        )
    rows = _open_raw(path, settings)
    try:
        return _ConfiguredTableReader(
            rows,
            settings=settings,
            total_record_count=total_record_count,
        )
    except BaseException:
        rows.close()
        raise


def read_prefix(
    path: Path,
    settings: TableReadSettings,
    *,
    record_limit: int,
) -> PrefixRead:
    """Read a bounded start preview without counting or claiming a tail."""

    if record_limit < 1:
        raise ValueError("record_limit must be positive")
    rows = _open_raw(path, settings)
    try:
        reader = _ConfiguredTableReader(
            rows, settings=settings, total_record_count=None
        )
        return PrefixRead(
            columns=reader.columns,
            rows=tuple(islice(reader, record_limit)),
            tail_pending=True,
        )
    finally:
        rows.close()


class _RawRows(ABC):
    @abstractmethod
    def __iter__(self) -> Iterator[RawRow]: ...

    @abstractmethod
    def close(self) -> None: ...


class _ConfiguredTableReader(TableReader):
    def __init__(
        self,
        raw: Any,
        *,
        settings: TableReadSettings,
        total_record_count: int | None,
    ) -> None:
        self._raw = raw
        self._settings = settings
        self._total = total_record_count
        self._iterator = iter(raw)
        self._buffer: list[RawRow] = []
        self._closed = False
        self._started = False
        self._header_ordinal: int | None = None
        self._columns = self._read_columns()

    @property
    def columns(self) -> tuple[SourceColumn, ...]:
        return self._columns

    def _read_columns(self) -> tuple[SourceColumn, ...]:
        skipped: deque[RawRow] = deque(maxlen=_RETAINED_SKIPPED_ROWS)
        for _ in range(self._settings.skip_rows_start):
            row = next(self._iterator, None)
            if row is None:
                raise TableReadError("skipped rows leave no table data")
            skipped.append(row)
        self._buffer.extend(skipped)
        if self._settings.first_row_is_header:
            header = next(self._iterator, None)
            if header is None or not header.cells:
                raise TableReadError("table has no column names")
            self._buffer.append(header)
            self._header_ordinal = header.record_ordinal
            return _normalized_columns(header.cells)

        leading_blank: deque[RawRow] = deque(maxlen=_RETAINED_SKIPPED_ROWS)
        while True:
            row = next(self._iterator, None)
            if row is None:
                raise TableReadError("table has no cells")
            if row.cells and any(cell.text != "" for cell in row.cells):
                self._buffer.extend(leading_blank)
                self._buffer.append(row)
                return tuple(
                    SourceColumn(index, f"column {index + 1}", None, "missing")
                    for index in range(len(row.cells))
                )
            leading_blank.append(row)

    def __iter__(self) -> Iterator[TableRow]:
        if self._started:
            raise RuntimeError("table reader is single-pass")
        self._started = True
        observed = 0
        for raw in self._buffer:
            observed = raw.record_ordinal
            yield self._configured(raw)
        self._buffer.clear()
        for raw in self._iterator:
            observed = raw.record_ordinal
            yield self._configured(raw)
        if self._total is not None and observed != self._total:
            raise TableReadError("table record count changed after analysis")

    def _configured(self, row: RawRow) -> TableRow:
        width = len(self._columns)
        cells = row.cells[:width]
        extras = row.cells[width:]
        issues = list(row.issues)
        if extras:
            issues.append(
                RowIssue(
                    "extra_values",
                    f"row has {len(extras)} extra value(s)",
                    tuple(cell.text for cell in extras),
                )
            )
        if len(cells) < width:
            missing = width - len(cells)
            cells = (*cells, *(RawCell("", "blank") for _ in range(missing)))
            issues.append(
                RowIssue(
                    "missing_values",
                    f"row has {missing} missing value(s)",
                )
            )
        role = self._role(row)
        return TableRow(role, row, tuple(cells), tuple(extras), tuple(issues))

    def _role(self, row: RawRow) -> str:
        ordinal = row.record_ordinal
        if ordinal <= self._settings.skip_rows_start:
            return "skipped_start"
        if ordinal == self._header_ordinal:
            return "header"
        if (
            self._total is not None
            and self._settings.skip_rows_end
            and ordinal > self._total - self._settings.skip_rows_end
        ):
            return "skipped_end"
        if not row.cells or all(cell.text == "" for cell in row.cells):
            return "blank"
        return "data"

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._raw.close()


def _normalized_columns(cells: tuple[RawCell, ...]) -> tuple[SourceColumn, ...]:
    used: set[str] = set()
    columns: list[SourceColumn] = []
    for index, cell in enumerate(cells):
        original = cell.text.strip()
        if not original:
            base = f"column {index + 1}"
            candidate = base
            suffix = 1
            while candidate in used:
                suffix += 1
                candidate = f"{base} ({suffix})"
            status = "missing"
            source_name = None
        else:
            base = original
            candidate = base
            suffix = 1
            while candidate in used:
                suffix += 1
                candidate = f"{base} ({suffix})"
            status = "original" if candidate == original else "duplicate"
            source_name = original
        used.add(candidate)
        columns.append(SourceColumn(index, candidate, source_name, status))
    return tuple(columns)


def _open_raw(path: Path, settings: TableReadSettings) -> Any:
    actual = _source_kind(path)
    if actual != settings.kind:
        raise TableReadError(
            f"source contains {actual}, but settings require {settings.kind}"
        )
    if settings.kind == "delimited":
        from .delimited import DelimitedRows

        assert settings.encoding is not None and settings.delimiter is not None
        return DelimitedRows(
            path, encoding=settings.encoding, delimiter=settings.delimiter
        )
    from .workbook import WorkbookRows

    return WorkbookRows(path, kind=settings.kind, worksheet=settings.worksheet)


def _source_kind(path: Path) -> SourceKind:
    try:
        with path.open("rb") as source:
            signature = source.read(8)
    except OSError as exc:
        raise TableReadError(f"table source could not be opened: {exc}") from exc
    if not signature:
        raise TableReadError("empty table source")
    if signature.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "xls"
    if zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                if "mimetype" in names:
                    info = archive.getinfo("mimetype")
                    mime = (
                        archive.read(info).decode("ascii", errors="ignore")
                        if info.file_size <= 256
                        else ""
                    )
                    if mime == "application/vnd.oasis.opendocument.spreadsheet":
                        return "ods"
                if "[Content_Types].xml" in names and "xl/workbook.xml" in names:
                    return "xlsx"
        except (OSError, zipfile.BadZipFile) as exc:
            raise TableReadError(
                f"table archive could not be inspected: {exc}"
            ) from exc
    return "delimited"
