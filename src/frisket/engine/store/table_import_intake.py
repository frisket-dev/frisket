"""Durable, bounded state for reviewed streaming table imports.

The original staged source is the only large artifact.  Preview scans and the
execution worker share the immutable source plus these small revisioned facts;
neither materializes a second row corpus.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence

from frisket.engine.store.import_intake import (
    ImportIntakeHeader,
    read_import_header,
    write_import_header,
)
from frisket.table_import import DateOrder, SourceKind, TableReadSettings, TargetType


TABLE_PROVIDER_SCHEMA = "frisket.table_import_provider.v1"
TABLE_STATE_SCHEMA = "frisket.table_import_state.v1"
TABLE_SCAN_SCHEMA = "frisket.table_import_scan.v1"
TABLE_PLAN_SCHEMA = "frisket.table_import_plan.v1"
_PHASES = {"draft", "frozen", "committed", "terminal"}
_SOURCE_KINDS = {"delimited", "xlsx", "xls", "ods"}
_TARGET_TYPES = {"text", "integer", "number", "date", "datetime", "boolean"}
_DATE_ORDERS = {"month_first", "day_first"}


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _hash(value: object) -> str:
    return "sha256:" + hashlib.sha256(_json_bytes(value)).hexdigest()


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}-{os.getpid()}-{os.urandom(4).hex()}")
    try:
        with temporary.open("wb") as sink:
            sink.write(_json_bytes(value))
            sink.flush()
            os.fsync(sink.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain an object")
    return value


@dataclass(frozen=True)
class TableImportSource:
    filename: str
    size: int
    sha256: str
    kind: SourceKind
    retain_for_workbook_reuse: bool = False

    def __post_init__(self) -> None:
        if not self.filename or Path(self.filename).name != self.filename:
            raise ValueError("table source filename must be a basename")
        if self.size < 0:
            raise ValueError("table source size cannot be negative")
        if self.kind not in _SOURCE_KINDS:
            raise ValueError("unsupported table source kind")
        digest = self.sha256.removeprefix("sha256:")
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("table source requires a lowercase sha256 digest")

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> TableImportSource:
        return cls(
            filename=str(value["filename"]),
            size=int(value["size"]),
            sha256=str(value["sha256"]),
            kind=str(value["kind"]),  # type: ignore[arg-type]
            retain_for_workbook_reuse=bool(
                value.get("retain_for_workbook_reuse", False)
            ),
        )


@dataclass(frozen=True)
class TableImportScan:
    settings_revision: int
    settings_hash: str
    scanned_rows: int
    estimated_rows: int | None
    exact_rows: int | None
    complete: bool
    problem_count: int = 0

    def __post_init__(self) -> None:
        counts = (self.settings_revision, self.scanned_rows, self.problem_count)
        if any(isinstance(item, bool) or item < 0 for item in counts):
            raise ValueError("table scan counts must be nonnegative integers")
        for value in (self.estimated_rows, self.exact_rows):
            if value is not None and (isinstance(value, bool) or value < 0):
                raise ValueError("table scan totals must be nonnegative")
        if self.complete and self.exact_rows is None:
            raise ValueError("a complete table scan requires an exact row count")
        if not self.settings_hash:
            raise ValueError("table scan settings hash is required")

    def to_json(self) -> dict[str, Any]:
        return {"schema_version": TABLE_SCAN_SCHEMA, **asdict(self)}

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> TableImportScan:
        if value.get("schema_version") != TABLE_SCAN_SCHEMA:
            raise ValueError("unsupported table scan schema")
        return cls(
            settings_revision=int(value["settings_revision"]),
            settings_hash=str(value["settings_hash"]),
            scanned_rows=int(value["scanned_rows"]),
            estimated_rows=(
                None
                if value.get("estimated_rows") is None
                else int(value["estimated_rows"])
            ),
            exact_rows=(
                None if value.get("exact_rows") is None else int(value["exact_rows"])
            ),
            complete=bool(value["complete"]),
            problem_count=int(value.get("problem_count", 0)),
        )


@dataclass(frozen=True)
class TableImportColumn:
    source_index: int
    source_name: str
    target_name: str
    target_type: TargetType
    target_column_id: int | None = None
    format: str | None = None
    date_order: DateOrder | None = None

    def __post_init__(self) -> None:
        if self.source_index < 0 or not self.source_name or not self.target_name:
            raise ValueError(
                "table import columns require stable source and target names"
            )
        if self.target_type not in _TARGET_TYPES:
            raise ValueError("unsupported frozen table target type")
        if self.target_column_id is not None and self.target_column_id < 1:
            raise ValueError("target column ids must be positive")
        if self.target_type not in {"date", "datetime"} and self.date_order is not None:
            raise ValueError("date order is valid only for temporal columns")
        if self.date_order is not None and self.date_order not in _DATE_ORDERS:
            raise ValueError("unsupported table date order")

    @property
    def output_type(self) -> str:
        """Return the physical project column type for this semantic target."""

        # Datetime is a logical project type over TEXT storage. The conversion
        # keeps the reviewed strict ISO value and never invents a timezone.
        return self.target_type

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> TableImportColumn:
        return cls(
            source_index=int(value["source_index"]),
            source_name=str(value["source_name"]),
            target_name=str(value["target_name"]),
            target_type=str(value["target_type"]),  # type: ignore[arg-type]
            target_column_id=(
                None
                if value.get("target_column_id") is None
                else int(value["target_column_id"])
            ),
            format=None if value.get("format") is None else str(value["format"]),
            date_order=(
                None if value.get("date_order") is None else str(value["date_order"])
            ),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class TableImportDedup:
    source_index: int
    target_column_id: int
    snapshot_revision: int

    def __post_init__(self) -> None:
        if (
            self.source_index < 0
            or self.target_column_id < 1
            or self.snapshot_revision < 0
        ):
            raise ValueError("invalid table dedup authority")


@dataclass(frozen=True)
class TableImportPlan:
    settings_revision: int
    settings: TableReadSettings
    sheet_name: str
    destination_sheet_id: int | None
    columns: tuple[TableImportColumn, ...]
    extra_values_target_name: str | None = None
    dedup: TableImportDedup | None = None

    def __post_init__(self) -> None:
        if self.settings_revision < 0 or not self.sheet_name or not self.columns:
            raise ValueError("frozen table plan is incomplete")
        indices = [column.source_index for column in self.columns]
        targets = [column.target_name for column in self.columns]
        if len(indices) != len(set(indices)) or len(targets) != len(set(targets)):
            raise ValueError("frozen table mapping must be one-to-one")
        if self.destination_sheet_id is not None and self.destination_sheet_id < 1:
            raise ValueError("destination sheet id must be positive")
        if self.dedup is not None and self.destination_sheet_id is None:
            raise ValueError("dedup requires an existing destination sheet")
        if self.dedup is not None and self.dedup.source_index not in indices:
            raise ValueError("dedup key must name an imported source column")

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": TABLE_PLAN_SCHEMA,
            "settings_revision": self.settings_revision,
            "settings": asdict(self.settings),
            "sheet_name": self.sheet_name,
            "destination_sheet_id": self.destination_sheet_id,
            "columns": [column.to_json() for column in self.columns],
            "extra_values_target_name": self.extra_values_target_name,
            "dedup": None if self.dedup is None else asdict(self.dedup),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> TableImportPlan:
        if value.get("schema_version") != TABLE_PLAN_SCHEMA:
            raise ValueError("unsupported table import plan schema")
        dedup = value.get("dedup")
        return cls(
            settings_revision=int(value["settings_revision"]),
            settings=TableReadSettings(**dict(value["settings"])),
            sheet_name=str(value["sheet_name"]),
            destination_sheet_id=(
                None
                if value.get("destination_sheet_id") is None
                else int(value["destination_sheet_id"])
            ),
            columns=tuple(
                TableImportColumn.from_json(column) for column in value["columns"]
            ),
            extra_values_target_name=(
                None
                if value.get("extra_values_target_name") is None
                else str(value["extra_values_target_name"])
            ),
            dedup=(
                None
                if dedup is None
                else TableImportDedup(
                    source_index=int(dedup["source_index"]),
                    target_column_id=int(dedup["target_column_id"]),
                    snapshot_revision=int(dedup["snapshot_revision"]),
                )
            ),
        )


@dataclass(frozen=True)
class _TableState:
    phase: Literal["draft", "frozen", "committed", "terminal"]
    settings_revision: int
    source_cursor: int = 0
    source_byte_offset: int | None = None
    exact_rows: int | None = None
    plan_hash: str | None = None
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"schema_version": TABLE_STATE_SCHEMA, **asdict(self)}

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> _TableState:
        if value.get("schema_version") != TABLE_STATE_SCHEMA:
            raise ValueError("unsupported table import state schema")
        phase = str(value["phase"])
        if phase not in _PHASES:
            raise ValueError("invalid table import phase")
        return cls(
            phase=phase,  # type: ignore[arg-type]
            settings_revision=int(value["settings_revision"]),
            source_cursor=int(value.get("source_cursor", 0)),
            source_byte_offset=(
                None
                if value.get("source_byte_offset") is None
                else int(value["source_byte_offset"])
            ),
            exact_rows=(
                None if value.get("exact_rows") is None else int(value["exact_rows"])
            ),
            plan_hash=(
                None if value.get("plan_hash") is None else str(value["plan_hash"])
            ),
            error=None if value.get("error") is None else str(value["error"]),
        )


@dataclass(frozen=True)
class FrozenTableImport:
    import_ref: str
    intake_dir: Path
    source: TableImportSource
    settings_revision: int
    settings_hash: str
    plan_hash: str
    plan: TableImportPlan
    estimated_rows: int | None
    exact_rows: int | None
    source_record_count: int | None


@dataclass(frozen=True)
class TableImportStatus:
    phase: str
    settings_revision: int
    scanned_rows: int
    estimated_rows: int | None
    exact_rows: int | None
    source_cursor: int
    problem_count: int
    scan_complete: bool
    plan_hash: str | None
    error: str | None


def _source_path(directory: Path) -> Path:
    return directory / "source"


def _state_path(directory: Path) -> Path:
    return directory / "table-state.json"


def _source_info_path(directory: Path) -> Path:
    return directory / "table-source.json"


def _scan_path(directory: Path) -> Path:
    return directory / "table-scan.json"


def _plan_path(directory: Path) -> Path:
    return directory / "table-plan.json"


def initialize_table_intake(
    directory: Path,
    header: ImportIntakeHeader,
    *,
    source: TableImportSource,
    settings: TableReadSettings,
) -> None:
    if header.envelope or header.destination_sheet_id is not None:
        raise ValueError("table draft cannot reserve its action before review")
    path = _source_path(directory)
    if not path.is_file() or path.stat().st_size != source.size:
        raise ValueError("staged table source does not match its admitted size")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != source.sha256.removeprefix("sha256:"):
        raise ValueError("staged table source does not match its admitted digest")
    settings_payload = asdict(settings)
    _write_json(_source_info_path(directory), source.to_json())
    _write_json(
        _state_path(directory),
        _TableState(phase="draft", settings_revision=0).to_json(),
    )
    _write_json(
        directory / "table-settings.json",
        {"revision": 0, "settings": settings_payload, "hash": _hash(settings_payload)},
    )
    write_import_header(
        directory,
        replace(
            header,
            intake_kind="table",
            provider_state={"schema_version": TABLE_PROVIDER_SCHEMA},
        ),
    )
    _open_table_db(directory).close()


def read_table_source(directory: Path) -> TableImportSource:
    return TableImportSource.from_json(_read_json(_source_info_path(directory)))


def read_table_settings(directory: Path) -> tuple[int, str, TableReadSettings]:
    value = _read_json(directory / "table-settings.json")
    settings = TableReadSettings(**dict(value["settings"]))
    observed_hash = _hash(asdict(settings))
    if observed_hash != value.get("hash"):
        raise ValueError("table settings hash changed")
    return int(value["revision"]), observed_hash, settings


def replace_table_settings(
    directory: Path,
    settings: TableReadSettings,
    *,
    expected_revision: int,
) -> int:
    state = _TableState.from_json(_read_json(_state_path(directory)))
    revision, _old_hash, _old = read_table_settings(directory)
    if state.phase != "draft" or revision != expected_revision:
        raise ValueError("table settings revision is stale")
    next_revision = revision + 1
    payload = asdict(settings)
    _write_json(
        directory / "table-settings.json",
        {"revision": next_revision, "settings": payload, "hash": _hash(payload)},
    )
    _write_json(
        _state_path(directory),
        replace(state, settings_revision=next_revision).to_json(),
    )
    _scan_path(directory).unlink(missing_ok=True)
    return next_revision


def write_table_scan(
    directory: Path,
    scan: TableImportScan,
    *,
    expected_settings_revision: int,
) -> None:
    revision, settings_hash, _settings = read_table_settings(directory)
    state = _TableState.from_json(_read_json(_state_path(directory)))
    if (
        state.phase not in {"draft", "frozen", "committed"}
        or revision != expected_settings_revision
        or scan.settings_revision != revision
        or scan.settings_hash != settings_hash
    ):
        raise ValueError("table scan revision is stale")
    current = read_table_scan(directory, required=False)
    if current is not None and current.settings_revision == revision:
        if scan.scanned_rows < current.scanned_rows:
            raise ValueError("table scan progress cannot move backwards")
        if current.complete and scan != current:
            raise ValueError("completed table scan is immutable")
    _write_json(_scan_path(directory), scan.to_json())


def read_table_scan(
    directory: Path, *, required: bool = True
) -> TableImportScan | None:
    try:
        return TableImportScan.from_json(_read_json(_scan_path(directory)))
    except FileNotFoundError:
        if required:
            raise
        return None


def freeze_table_plan(
    directory: Path,
    plan: TableImportPlan,
    *,
    require_exact_count: bool = False,
) -> FrozenTableImport:
    state = _TableState.from_json(_read_json(_state_path(directory)))
    revision, settings_hash, settings = read_table_settings(directory)
    if state.phase not in {"draft", "frozen"}:
        raise ValueError("table intake can no longer change its plan")
    if plan.settings_revision != revision or plan.settings != settings:
        raise ValueError("table plan settings are stale")
    scan = read_table_scan(directory, required=False)
    if scan is not None and scan.settings_revision != revision:
        raise ValueError("table plan scan is stale")
    if require_exact_count and (scan is None or not scan.complete):
        raise ValueError("table plan requires a complete row count")
    if plan.dedup is not None and plan.dedup.snapshot_revision != table_dedup_revision(
        directory
    ):
        raise ValueError("table dedup snapshot is stale")
    payload = plan.to_json()
    plan_hash = _hash(payload)
    if state.plan_hash is not None and state.plan_hash != plan_hash:
        raise ValueError("table plan was already frozen differently")
    _write_json(_plan_path(directory), payload)
    _write_json(
        _state_path(directory),
        replace(state, phase="frozen", plan_hash=plan_hash).to_json(),
    )
    return frozen_table_import(directory)


def frozen_table_import(directory: Path) -> FrozenTableImport:
    header = read_import_header(directory)
    state = _TableState.from_json(_read_json(_state_path(directory)))
    if header.intake_kind != "table" or state.plan_hash is None:
        raise ValueError("table intake is not frozen")
    plan = TableImportPlan.from_json(_read_json(_plan_path(directory)))
    if _hash(plan.to_json()) != state.plan_hash:
        raise ValueError("frozen table plan hash changed")
    revision, settings_hash, settings = read_table_settings(directory)
    if revision != plan.settings_revision or settings != plan.settings:
        raise ValueError("frozen table settings changed")
    if plan.dedup is not None and plan.dedup.snapshot_revision != table_dedup_revision(
        directory
    ):
        raise ValueError("frozen table dedup snapshot changed")
    scan = read_table_scan(directory, required=False)
    return FrozenTableImport(
        import_ref=directory.name,
        intake_dir=directory,
        source=read_table_source(directory),
        settings_revision=revision,
        settings_hash=settings_hash,
        plan_hash=state.plan_hash,
        plan=plan,
        estimated_rows=None if scan is None else scan.estimated_rows,
        exact_rows=(
            state.exact_rows
            if state.exact_rows is not None
            else (None if scan is None else scan.exact_rows)
        ),
        source_record_count=(
            scan.scanned_rows if scan is not None and scan.complete else None
        ),
    )


def mark_table_committed(
    directory: Path,
    *,
    expected_plan_hash: str,
    envelope: Mapping[str, Any],
    destination_sheet_id: int | None,
) -> FrozenTableImport:
    frozen = frozen_table_import(directory)
    state = _TableState.from_json(_read_json(_state_path(directory)))
    header = read_import_header(directory)
    final_envelope = dict(envelope)
    if not final_envelope:
        raise ValueError("table commit requires a reserved action envelope")
    if frozen.plan_hash != expected_plan_hash:
        raise ValueError("table plan hash is stale")
    if state.phase not in {"frozen", "committed"}:
        raise ValueError("table intake cannot be committed")
    if frozen.plan.destination_sheet_id != destination_sheet_id:
        raise ValueError("table commit destination changed")
    if header.envelope and header.envelope != final_envelope:
        raise ValueError("table commit action changed")
    if (
        header.destination_sheet_id is not None
        and header.destination_sheet_id != destination_sheet_id
    ):
        raise ValueError("table commit destination changed")
    if state.phase == "frozen":
        # Write authority first. A crash leaves a frozen intake whose exact
        # replay can finish; execution never sees committed state without it.
        write_import_header(
            directory,
            replace(
                header,
                envelope=final_envelope,
                destination_sheet_id=destination_sheet_id,
            ),
        )
        _write_json(_state_path(directory), replace(state, phase="committed").to_json())
    elif (
        header.envelope != final_envelope
        or header.destination_sheet_id != destination_sheet_id
    ):
        raise ValueError("table commit replay changed")
    return frozen


def advance_table_progress(
    directory: Path,
    *,
    expected_cursor: int,
    source_cursor: int,
    source_byte_offset: int | None = None,
    exact_rows: int | None = None,
) -> None:
    state = _TableState.from_json(_read_json(_state_path(directory)))
    if state.phase != "committed" or state.source_cursor != expected_cursor:
        raise ValueError("table execution cursor is stale")
    if source_cursor < expected_cursor:
        raise ValueError("table execution cursor cannot move backwards")
    _write_json(
        _state_path(directory),
        replace(
            state,
            source_cursor=source_cursor,
            source_byte_offset=source_byte_offset,
            exact_rows=exact_rows if exact_rows is not None else state.exact_rows,
        ).to_json(),
    )


def finish_table_intake(
    directory: Path, *, exact_rows: int | None, error: str | None = None
) -> None:
    state = _TableState.from_json(_read_json(_state_path(directory)))
    if state.phase not in {"committed", "terminal"}:
        raise ValueError("table intake is not executing")
    final_exact = state.exact_rows if exact_rows is None else exact_rows
    if state.phase == "terminal" and (state.exact_rows, state.error) != (
        final_exact,
        error,
    ):
        raise ValueError("terminal table result changed")
    _write_json(
        _state_path(directory),
        replace(state, phase="terminal", exact_rows=final_exact, error=error).to_json(),
    )


def table_import_status(directory: Path) -> TableImportStatus:
    state = _TableState.from_json(_read_json(_state_path(directory)))
    scan = read_table_scan(directory, required=False)
    return TableImportStatus(
        phase=state.phase,
        settings_revision=state.settings_revision,
        scanned_rows=0 if scan is None else scan.scanned_rows,
        estimated_rows=None if scan is None else scan.estimated_rows,
        exact_rows=(
            state.exact_rows
            if state.exact_rows is not None
            else (None if scan is None else scan.exact_rows)
        ),
        source_cursor=state.source_cursor,
        problem_count=table_problem_count(directory, through=state.source_cursor),
        scan_complete=False if scan is None else scan.complete,
        plan_hash=state.plan_hash,
        error=state.error,
    )


def _open_table_db(directory: Path) -> sqlite3.Connection:
    db = sqlite3.connect(directory / "table.db")
    db.row_factory = sqlite3.Row
    db.executescript(
        "CREATE TABLE IF NOT EXISTS dedup_keys (value TEXT PRIMARY KEY);"
        "CREATE TABLE IF NOT EXISTS dedup_meta ("
        "singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
        "revision INTEGER NOT NULL);"
        "INSERT OR IGNORE INTO dedup_meta(singleton,revision) VALUES(1,0);"
        "CREATE TABLE IF NOT EXISTS issues ("
        "source_ordinal INTEGER NOT NULL,line_start INTEGER,line_end INTEGER,"
        "column_name TEXT NOT NULL DEFAULT '',code TEXT NOT NULL,message TEXT NOT NULL,"
        "original TEXT NOT NULL DEFAULT '',"
        "PRIMARY KEY(source_ordinal,column_name,code,original));"
        "CREATE INDEX IF NOT EXISTS ix_table_issues_group "
        "ON issues(message,source_ordinal);"
    )
    return db


def table_dedup_revision(directory: Path) -> int:
    with _open_table_db(directory) as db:
        row = db.execute("SELECT revision FROM dedup_meta WHERE singleton=1").fetchone()
    return int(row[0])


def table_dedup_key(value: object) -> str | None:
    """Encode one typed destination/source value for exact snapshot matching."""

    if value is None or value == "":
        return None
    if isinstance(value, bool):
        kind = "boolean"
    elif isinstance(value, int):
        kind = "integer"
    elif isinstance(value, float):
        kind = "number"
    elif isinstance(value, str):
        kind = "text"
    else:
        raise ValueError("table dedup values must be scalar project values")
    return json.dumps(
        [kind, value],
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def append_table_dedup_keys(directory: Path, values: Sequence[str]) -> int:
    clean = [value for value in values if isinstance(value, str) and value != ""]
    with _open_table_db(directory) as db:
        state = _TableState.from_json(_read_json(_state_path(directory)))
        if state.phase != "draft":
            raise ValueError("table dedup snapshot is frozen")
        before = db.total_changes
        db.executemany(
            "INSERT OR IGNORE INTO dedup_keys(value) VALUES(?)",
            ((value,) for value in clean),
        )
        added = db.total_changes - before
        db.execute("UPDATE dedup_meta SET revision=revision+1 WHERE singleton=1")
        return added


def replace_table_dedup_keys(
    directory: Path,
    values: Iterable[str],
    *,
    expected_revision: int,
) -> int:
    """Atomically replace the final existing-destination key snapshot."""

    state = _TableState.from_json(_read_json(_state_path(directory)))
    if state.phase != "draft":
        raise ValueError("table dedup snapshot is frozen")
    with _open_table_db(directory) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute(
                "SELECT revision FROM dedup_meta WHERE singleton=1"
            ).fetchone()
            if int(row[0]) != expected_revision:
                raise ValueError("table dedup snapshot revision is stale")
            db.execute("DELETE FROM dedup_keys")

            def clean_values() -> Iterable[tuple[str]]:
                for value in values:
                    if not isinstance(value, str):
                        raise ValueError("table dedup keys must be text")
                    if value != "":
                        yield (value,)

            db.executemany(
                "INSERT OR IGNORE INTO dedup_keys(value) VALUES(?)", clean_values()
            )
            next_revision = expected_revision + 1
            db.execute(
                "UPDATE dedup_meta SET revision=? WHERE singleton=1",
                (next_revision,),
            )
            db.commit()
            return next_revision
        except BaseException:
            db.rollback()
            raise


def table_key_exists(directory: Path, value: str) -> bool:
    if value == "":
        return False
    with _open_table_db(directory) as db:
        return (
            db.execute("SELECT 1 FROM dedup_keys WHERE value=?", (value,)).fetchone()
            is not None
        )


class TableDedupSnapshot:
    """One read-only connection for exact key lookups during a worker claim."""

    def __init__(self, directory: Path) -> None:
        self._db = _open_table_db(directory)

    def contains(self, value: str) -> bool:
        if value == "":
            return False
        return (
            self._db.execute(
                "SELECT 1 FROM dedup_keys WHERE value=?", (value,)
            ).fetchone()
            is not None
        )

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> TableDedupSnapshot:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


@dataclass(frozen=True)
class TableImportIssue:
    source_ordinal: int
    line_start: int | None
    line_end: int | None
    column_name: str
    code: str
    message: str
    original: str


def record_table_issues(directory: Path, issues: Sequence[TableImportIssue]) -> int:
    if not issues:
        return 0
    with _open_table_db(directory) as db:
        before = db.total_changes
        db.executemany(
            "INSERT OR IGNORE INTO issues "
            "(source_ordinal,line_start,line_end,column_name,code,message,original) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                (
                    issue.source_ordinal,
                    issue.line_start,
                    issue.line_end,
                    issue.column_name,
                    issue.code,
                    issue.message,
                    issue.original[:4096],
                )
                for issue in issues
            ),
        )
        return db.total_changes - before


def table_problem_count(directory: Path, *, through: int | None = None) -> int:
    with _open_table_db(directory) as db:
        if through is None:
            row = db.execute("SELECT COUNT(*) FROM issues").fetchone()
        else:
            row = db.execute(
                "SELECT COUNT(*) FROM issues WHERE source_ordinal<=?", (through,)
            ).fetchone()
        return int(row[0])


def table_issue_groups(
    directory: Path, *, through: int, max_groups: int = 25, max_examples: int = 5
) -> list[dict[str, Any]]:
    with _open_table_db(directory) as db:
        groups = db.execute(
            "SELECT message,code,COUNT(*) AS count FROM issues "
            "WHERE source_ordinal<=? GROUP BY message,code "
            "ORDER BY count DESC,message LIMIT ?",
            (through, max_groups),
        ).fetchall()
        result = []
        for group in groups:
            examples = db.execute(
                "SELECT source_ordinal,line_start,line_end,column_name,original "
                "FROM issues WHERE source_ordinal<=? AND message=? AND code=? "
                "ORDER BY source_ordinal LIMIT ?",
                (through, group["message"], group["code"], max_examples),
            ).fetchall()
            result.append(
                {
                    "message": str(group["message"]),
                    "code": str(group["code"]),
                    "count": int(group["count"]),
                    "examples": [dict(example) for example in examples],
                }
            )
        return result


def compact_table_intake(directory: Path) -> None:
    source = read_table_source(directory)
    if not source.retain_for_workbook_reuse:
        _source_path(directory).unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        (directory / f"table.db{suffix}").unlink(missing_ok=True)


__all__ = [
    "FrozenTableImport",
    "TableImportColumn",
    "TableImportDedup",
    "TableDedupSnapshot",
    "TableImportIssue",
    "TableImportPlan",
    "TableImportScan",
    "TableImportSource",
    "TableImportStatus",
    "advance_table_progress",
    "append_table_dedup_keys",
    "compact_table_intake",
    "finish_table_intake",
    "freeze_table_plan",
    "frozen_table_import",
    "initialize_table_intake",
    "mark_table_committed",
    "read_table_scan",
    "read_table_settings",
    "read_table_source",
    "record_table_issues",
    "replace_table_dedup_keys",
    "replace_table_settings",
    "table_import_status",
    "table_dedup_key",
    "table_dedup_revision",
    "table_issue_groups",
    "table_key_exists",
    "table_problem_count",
    "write_table_scan",
]
