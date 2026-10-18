from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from frisket.engine.store.import_intake import (
    ImportIntakeHeader,
    read_import_header,
    request_import_cancel,
)
from frisket.engine.store.table_import_intake import (
    TableImportColumn,
    TableImportDedup,
    TableImportIssue,
    TableImportPlan,
    TableImportScan,
    TableImportSource,
    advance_table_progress,
    append_table_dedup_keys,
    finish_table_intake,
    freeze_table_plan,
    initialize_table_intake,
    mark_table_committed,
    read_table_settings,
    record_table_issues,
    replace_table_dedup_keys,
    replace_table_settings,
    table_import_status,
    table_dedup_key,
    table_dedup_revision,
    table_issue_groups,
    table_key_exists,
    write_table_scan,
)
from frisket.table_import import TableReadSettings


def _header() -> ImportIntakeHeader:
    return ImportIntakeHeader(
        project_id="project-one",
        storage_identity="storage-one",
        envelope={},
    )


def _setup(tmp_path: Path) -> tuple[Path, TableReadSettings]:
    directory = tmp_path / ("import-" + "a" * 32)
    directory.mkdir(parents=True)
    data = b"id,amount\na,1.20\nb,N/A\n"
    (directory / "source").write_bytes(data)
    settings = TableReadSettings(kind="delimited", delimiter=",", encoding="utf-8")
    initialize_table_intake(
        directory,
        _header(),
        source=TableImportSource(
            filename="rows.csv",
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            kind="delimited",
        ),
        settings=settings,
    )
    return directory, settings


def test_table_intake_freezes_revisioned_plan_and_replays_commit(
    tmp_path: Path,
) -> None:
    directory, settings = _setup(tmp_path)
    header = read_import_header(directory)
    assert header.intake_kind == "table"
    assert header.provider_state == {
        "schema_version": "frisket.table_import_provider.v1"
    }

    settings_hash = read_table_settings(directory)[1]
    write_table_scan(
        directory,
        TableImportScan(0, settings_hash, 2, 2, 2, True, problem_count=1),
        expected_settings_revision=0,
    )
    plan = TableImportPlan(
        settings_revision=0,
        settings=settings,
        sheet_name="Rows",
        destination_sheet_id=None,
        columns=(
            TableImportColumn(0, "id", "id", "text"),
            TableImportColumn(1, "amount", "amount", "number"),
        ),
    )
    frozen = freeze_table_plan(directory, plan)
    assert frozen.exact_rows == 2
    envelope = {"schema_version": "test.action", "receipt_id": "receipt-one"}
    assert (
        mark_table_committed(
            directory,
            expected_plan_hash=frozen.plan_hash,
            envelope=envelope,
            destination_sheet_id=None,
        ).plan_hash
        == frozen.plan_hash
    )
    assert (
        mark_table_committed(
            directory,
            expected_plan_hash=frozen.plan_hash,
            envelope=envelope,
            destination_sheet_id=None,
        ).plan_hash
        == frozen.plan_hash
    )
    assert read_import_header(directory).envelope == envelope
    with pytest.raises(ValueError, match="action changed"):
        mark_table_committed(
            directory,
            expected_plan_hash=frozen.plan_hash,
            envelope={"schema_version": "changed"},
            destination_sheet_id=None,
        )

    advance_table_progress(directory, expected_cursor=0, source_cursor=1)
    advance_table_progress(directory, expected_cursor=1, source_cursor=2, exact_rows=2)
    finish_table_intake(directory, exact_rows=2)
    status = table_import_status(directory)
    assert (status.phase, status.source_cursor, status.exact_rows) == (
        "terminal",
        2,
        2,
    )


def test_table_scan_and_settings_reject_stale_writers(tmp_path: Path) -> None:
    directory, settings = _setup(tmp_path)
    from frisket.engine.store.table_import_intake import read_table_settings

    settings_hash = read_table_settings(directory)[1]
    write_table_scan(
        directory,
        TableImportScan(0, settings_hash, 10, 20, None, False),
        expected_settings_revision=0,
    )
    changed = TableReadSettings(
        kind="delimited",
        delimiter=";",
        encoding="utf-8",
        decimal_separator=",",
        thousands_separator=".",
    )
    assert replace_table_settings(directory, changed, expected_revision=0) == 1
    with pytest.raises(ValueError, match="stale"):
        write_table_scan(
            directory,
            TableImportScan(0, settings_hash, 20, 20, 20, True),
            expected_settings_revision=0,
        )
    with pytest.raises(ValueError, match="stale"):
        freeze_table_plan(
            directory,
            TableImportPlan(
                settings_revision=0,
                settings=settings,
                sheet_name="Rows",
                destination_sheet_id=None,
                columns=(TableImportColumn(0, "id", "id", "text"),),
            ),
        )


def test_dedup_snapshot_is_exact_text_blank_exempt_and_issues_are_bounded(
    tmp_path: Path,
) -> None:
    directory, _settings = _setup(tmp_path)
    assert append_table_dedup_keys(directory, ["A", " a ", "", "A"]) == 2
    assert table_key_exists(directory, "A")
    assert table_key_exists(directory, " a ")
    assert not table_key_exists(directory, "a")
    assert not table_key_exists(directory, "")

    issue = TableImportIssue(
        source_ordinal=2,
        line_start=3,
        line_end=3,
        column_name="amount",
        code="invalid_number",
        message="Value was left blank",
        original="N/A" * 2000,
    )
    assert record_table_issues(directory, [issue, issue]) == 1
    assert table_import_status(directory).problem_count == 0
    [group] = table_issue_groups(directory, through=2)
    assert group["count"] == 1
    assert group["examples"][0]["source_ordinal"] == 2
    assert len(group["examples"][0]["original"]) == 4096
    assert table_dedup_key(None) is None
    assert table_dedup_key("") is None
    assert table_dedup_key(True) != table_dedup_key(1)
    assert table_dedup_key(1) != table_dedup_key("1")


def test_final_dedup_snapshot_replaces_stale_preview_keys(tmp_path: Path) -> None:
    directory, settings = _setup(tmp_path)
    assert append_table_dedup_keys(directory, ["stale"]) == 1
    revision = replace_table_dedup_keys(
        directory, (value for value in ["current", "", "current"]), expected_revision=1
    )
    assert revision == 2
    assert table_dedup_revision(directory) == 2
    assert not table_key_exists(directory, "stale")
    assert table_key_exists(directory, "current")
    frozen = freeze_table_plan(
        directory,
        TableImportPlan(
            settings_revision=0,
            settings=settings,
            sheet_name="Rows",
            destination_sheet_id=9,
            columns=(TableImportColumn(0, "id", "id", "text"),),
            dedup=TableImportDedup(0, 12, revision),
        ),
    )
    assert frozen.plan.dedup is not None
    with pytest.raises(ValueError, match="frozen"):
        replace_table_dedup_keys(directory, ["changed"], expected_revision=revision)


def test_table_intake_validates_source_authority_and_preserves_provider_state(
    tmp_path: Path,
) -> None:
    directory = tmp_path / ("import-" + "b" * 32)
    directory.mkdir()
    (directory / "source").write_bytes(b"changed")
    with pytest.raises(ValueError, match="digest"):
        initialize_table_intake(
            directory,
            _header(),
            source=TableImportSource(
                filename="rows.csv",
                size=7,
                sha256=hashlib.sha256(b"claimed").hexdigest(),
                kind="delimited",
            ),
            settings=TableReadSettings(
                kind="delimited", delimiter=",", encoding="utf-8"
            ),
        )

    directory, _settings = _setup(tmp_path / "valid")
    cancelled = request_import_cancel(directory)
    assert cancelled.intake_kind == "table"
    assert cancelled.provider_state == {
        "schema_version": "frisket.table_import_provider.v1"
    }


def test_datetime_semantics_publish_to_the_logical_datetime_type() -> None:
    column = TableImportColumn(0, "created", "created", "datetime")
    assert column.target_type == "datetime"
    assert column.output_type == "datetime"
    assert TableImportColumn(0, "day", "day", "date").output_type == "date"
