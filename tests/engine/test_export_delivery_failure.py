from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore


class _CommitFailingConnection:
    """Commit-only proxy; these tests assume executor commits via connection.commit()."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.fail_commit = False
        self.fail_commit_at: int | None = None
        self.commit_count = 0
        self.before_failed_commit = None

    def commit(self) -> None:
        self.commit_count += 1
        if self.fail_commit or self.commit_count == self.fail_commit_at:
            if self.before_failed_commit is not None:
                self.before_failed_commit()
            raise RuntimeError("injected commit failure after export delivery")
        self._conn.commit()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def _wrap_project_db_commit(monkeypatch, project: Project) -> _CommitFailingConnection:
    original_db = Project.db
    wrapper = _CommitFailingConnection(project.db)

    def db(self: Project) -> sqlite3.Connection | _CommitFailingConnection:
        if self is project:
            return wrapper
        return original_db.fget(self)

    monkeypatch.setattr(Project, "db", property(db))
    return wrapper


def _receipt_count(project: Project) -> int:
    return int(project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0])


def _work_log_action(destination: Path) -> dict[str, Any]:
    return {
        "action_id": "export.work_log",
        "scope": {"kind": "project"},
        "params": {
            "destination": {"kind": "local_file", "path": str(destination)},
            "include_receipts": True,
        },
        "idempotency_key": "rollback/export_work_log@sha256:v1",
    }


def _sheet_csv_action(*, sheet_id: int, destination: Path) -> dict[str, Any]:
    return {
        "action_id": "export.sheet_csv",
        "scope": {"kind": "project"},
        "params": {
            "sheet_id": sheet_id,
            "destination": {"kind": "local_file", "path": str(destination)},
        },
        "idempotency_key": "rollback/export_sheet_csv@sha256:v1",
    }


def _dataset_action(kind: str, *, sheet_id: int, destination: Path) -> dict[str, Any]:
    format_ = kind.removeprefix("export.sheet_")
    return {
        "action_id": kind,
        "scope": {"kind": "project"},
        "params": {
            "sheet_id": sheet_id,
            "destination": {"kind": "local_file", "path": str(destination)},
        },
        "idempotency_key": f"partial-write/{format_}@sha256:v1",
    }


@pytest.mark.parametrize(
    "kind",
    [
        "export.work_log",
        "export.sheet_csv",
        "export.sheet_jsonl",
        "export.sheet_parquet",
    ],
)
@pytest.mark.parametrize("path", ["   ", " artifact.out "])
def test_local_export_rejects_nonsemantic_path_before_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    path: str,
) -> None:
    from frisket.engine.executor import actions as executor_actions

    project = Project.create(tmp_path / "invalid-path.frisket", name="Invalid path")
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    monkeypatch.chdir(export_dir)
    try:
        sheet_id = project.add_sheet("Rows")
        params: dict[str, Any] = {"destination": {"kind": "local_file", "path": path}}
        if kind != "export.work_log":
            params["sheet_id"] = sheet_id

        result = executor_actions.run_action_spec(
            project,
            {
                "action_id": kind,
                "scope": {"kind": "project"},
                "params": params,
                "idempotency_key": f"invalid-path/{kind}@sha256:v1",
            },
            project_id="project-export-invalid-path",
        )

        assert result.status == "failed"
        assert result.errors[0].code == "invalid_action_request"
        assert list(export_dir.iterdir()) == []
    finally:
        project.close()


@pytest.mark.parametrize(
    ("kind", "suffix"),
    [
        ("export.work_log", "md"),
        ("export.sheet_jsonl", "jsonl"),
        ("export.sheet_parquet", "parquet"),
    ],
)
def test_buffered_export_restores_destination_when_commit_fails_after_delivery(
    tmp_path: Path,
    monkeypatch,
    kind: str,
    suffix: str,
) -> None:
    from frisket.engine.executor import actions as executor_actions

    project = Project.create(tmp_path / f"{suffix}-rollback.frisket", name="Rollback")
    try:
        sheet_id = project.add_sheet("Stories")
        name_column_id = project.add_column(sheet_id, "name", "text")
        project.add_rows(sheet_id, [{"name": "Ada"}], {"name": name_column_id})
        destination = tmp_path / f"artifact.{suffix}"
        previous = b"previous artifact\n"
        destination.write_bytes(previous)
        wrapper = _wrap_project_db_commit(monkeypatch, project)
        # Reservation commits first; the next commit follows artifact promotion.
        wrapper.fail_commit_at = 2
        promoted = []
        wrapper.before_failed_commit = lambda: promoted.append(destination.read_bytes())
        action = (
            _work_log_action(destination)
            if kind == "export.work_log"
            else _dataset_action(kind, sheet_id=sheet_id, destination=destination)
        )
        result = executor_actions.run_action_spec(
            project,
            action,
            project_id="project-export-rollback",
        )
        assert result.status == "failed"
        assert result.errors[0].code == "project_write_failed"
        assert wrapper.commit_count == 3
        assert len(promoted) == 1 and promoted[0] != previous
        assert destination.read_bytes() == previous
        assert not list(tmp_path.glob(f".{destination.name}.*"))
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        assert receipt.status == "failed"
        assert not receipt.outputs and not receipt.evidence and not receipt.exports
    finally:
        project.close()


def test_export_work_log_removes_new_destination_when_commit_fails_after_delivery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from frisket.engine.executor import actions as executor_actions

    project = Project.create(
        tmp_path / "work-log-new-rollback.frisket", name="Rollback"
    )
    destination = tmp_path / "work-log-new.md"
    wrapper = _wrap_project_db_commit(monkeypatch, project)
    before_receipts = _receipt_count(project)
    wrapper.fail_commit_at = 2
    promoted = []
    wrapper.before_failed_commit = lambda: promoted.append(destination.read_bytes())
    try:
        result = executor_actions.run_action_spec(
            project,
            _work_log_action(destination),
            project_id="project-export-rollback",
        )
        assert result.status == "failed"
        assert result.errors[0].code == "project_write_failed"
        assert not destination.exists()
        assert not list(tmp_path.glob(".work-log-new.md.*"))
        assert wrapper.commit_count == 3
        assert promoted
        assert _receipt_count(project) == before_receipts + 1
    finally:
        project.close()


def test_export_sheet_csv_restores_destination_when_commit_fails_after_delivery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from frisket.engine.executor import actions as executor_actions

    project = Project.create(tmp_path / "sheet-csv-rollback.frisket", name="Rollback")
    try:
        sheet_id = project.add_sheet("Stories")
        name_column_id = project.add_column(sheet_id, "name", "text")
        project.add_rows(
            sheet_id,
            [{"name": "Ada"}],
            {"name": name_column_id},
        )
        destination = tmp_path / "stories.csv"
        destination.write_text("previous csv\n", encoding="utf-8")
        wrapper = _wrap_project_db_commit(monkeypatch, project)
        wrapper.fail_commit_at = 2
        promoted = []
        wrapper.before_failed_commit = lambda: promoted.append(destination.read_bytes())
        result = executor_actions.run_action_spec(
            project,
            _sheet_csv_action(sheet_id=sheet_id, destination=destination),
            project_id="project-export-rollback",
        )
        assert result.status == "failed"
        assert result.errors[0].code == "project_write_failed"
        assert destination.read_text(encoding="utf-8") == "previous csv\n"
        assert not list(tmp_path.glob(".stories.csv.*"))
        assert wrapper.commit_count == 3
        assert len(promoted) == 1 and promoted[0] != b"previous csv\n"
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        assert receipt.status == "failed"
        assert not receipt.outputs and not receipt.evidence and not receipt.exports
    finally:
        project.close()


@pytest.mark.parametrize(
    ("kind", "suffix"),
    [
        ("export.work_log", "md"),
        ("export.sheet_jsonl", "jsonl"),
        ("export.sheet_parquet", "parquet"),
    ],
)
def test_buffered_export_removes_partial_staging_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    suffix: str,
) -> None:
    """A failed buffered write must not strand its partially written tmp file."""
    from frisket.engine.executor import actions as executor_actions

    project = Project.create(tmp_path / f"partial-{suffix}.frisket", name="Partial")
    try:
        sheet_id = project.add_sheet("Rows")
        column_id = project.add_column(sheet_id, "name", "text")
        project.add_rows(sheet_id, [{"name": "Ada"}], {"name": column_id})
        destination = tmp_path / f"artifact.{suffix}"
        destination.write_bytes(b"previous artifact")
        before_receipts = _receipt_count(project)
        original_write_bytes = Path.write_bytes

        def partial_then_fail(path: Path, data: bytes) -> int:
            if path.name.startswith(f".{destination.name}."):
                original_write_bytes(path, data[: max(1, len(data) // 2)])
                raise OSError("injected partial staging write")
            return original_write_bytes(path, data)

        monkeypatch.setattr(Path, "write_bytes", partial_then_fail)
        action = (
            _work_log_action(destination)
            if kind == "export.work_log"
            else _dataset_action(kind, sheet_id=sheet_id, destination=destination)
        )

        result = executor_actions.run_action_spec(
            project, action, project_id="project-export-partial-write"
        )

        assert result.status == "failed"
        assert result.errors[0].code == "invalid_export_destination"
        assert destination.read_bytes() == b"previous artifact"
        assert not list(tmp_path.glob(f".{destination.name}.*.tmp"))
        assert _receipt_count(project) == before_receipts + 1
        receipt_id = project.db.execute("SELECT id FROM receipts").fetchone()[0]
        receipt = ReceiptStore(project).parsed_by_id(receipt_id)
        assert receipt.status == "failed"
        assert receipt.errors[0].code == "invalid_export_destination"
        assert not receipt.outputs and not receipt.exports
    finally:
        project.close()


@pytest.mark.parametrize(
    ("kind", "render_name", "suffix"),
    [
        ("export.work_log", "build_work_log_payload", "md"),
        ("export.sheet_csv", "iter_export_csv_bytes", "csv"),
        ("export.sheet_jsonl", "render_jsonl", "jsonl"),
        ("export.sheet_parquet", "render_parquet", "parquet"),
    ],
)
def test_export_renders_once_before_transaction_and_not_on_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    render_name: str,
    suffix: str,
) -> None:
    from frisket.engine.executor import actions as executor_actions
    from frisket.engine.executor.action_families import exports as export_action

    project = Project.create(tmp_path / f"once-{suffix}.frisket", name="Once")
    try:
        sheet_id = project.add_sheet("Rows")
        column_id = project.add_column(sheet_id, "name", "text")
        project.add_rows(sheet_id, [{"name": "Ada"}], {"name": column_id})
        destination = tmp_path / f"once.{suffix}"
        original = getattr(export_action, render_name)
        calls = 0
        capability_calls = 0

        def observed(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            assert project.db.in_transaction is False
            return original(*args, **kwargs)

        monkeypatch.setattr(export_action, render_name, observed)
        capability_type, capability_method = {
            "export.work_log": (
                export_action.WorkLogExportCapability,
                "write_work_log",
            ),
            "export.sheet_csv": (
                export_action.SheetCsvExportCapability,
                "write_sheet_csv",
            ),
            "export.sheet_jsonl": (
                export_action.SheetJsonlExportCapability,
                "write_sheet_jsonl",
            ),
            "export.sheet_parquet": (
                export_action.SheetParquetExportCapability,
                "write_sheet_parquet",
            ),
        }[kind]
        original_capability = getattr(capability_type, capability_method)

        def observed_capability(*args: Any, **kwargs: Any) -> Any:
            nonlocal capability_calls
            capability_calls += 1
            assert project.db.in_transaction is False
            return original_capability(*args, **kwargs)

        monkeypatch.setattr(capability_type, capability_method, observed_capability)
        if kind == "export.work_log":
            action = _work_log_action(destination)
        elif kind == "export.sheet_csv":
            action = _sheet_csv_action(sheet_id=sheet_id, destination=destination)
        else:
            action = _dataset_action(kind, sheet_id=sheet_id, destination=destination)

        first = executor_actions.run_action_spec(
            project, action, project_id="project-export-render-once"
        )
        replay = executor_actions.run_action_spec(
            project, action, project_id="project-export-render-once"
        )

        assert first.status == "completed", first.errors
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert calls == 1
        assert capability_calls == 1
    finally:
        project.close()


@pytest.mark.parametrize("destination_kind", ["local_dir", "project_file"])
@pytest.mark.parametrize("existing", [False, True])
def test_column_tables_commit_failure_rolls_back_delivered_artifact_metadata(
    tmp_path, monkeypatch, destination_kind, existing
):
    from frisket.engine.executor import run_action_spec
    from frisket.engine.executor.action_families import exports_column_tables as exports
    from tests.engine.test_export_column_tables_executor import (
        _export_action,
        _seed_scalar_list_sheet,
    )

    project = Project.create(tmp_path / "project")
    try:
        sheet_id, column_id = _seed_scalar_list_sheet(project)
        directory = tmp_path / "exports"
        directory.mkdir()
        destination = directory / "tags.zip"
        target = (
            {"kind": "local_dir", "path": str(directory)}
            if destination_kind == "local_dir"
            else {"kind": "project_file", "prefix": "exports/tables"}
        )
        previous_blob = None
        if existing:
            if destination_kind == "local_dir":
                destination.write_bytes(b"previous package")
            else:
                first = run_action_spec(
                    project,
                    _export_action(
                        sheet_id=sheet_id,
                        column_id=column_id,
                        destination=target,
                        key="previous-package",
                    ),
                    project_id="p",
                )
                assert first.status == "completed", first.errors
                previous_blob = first.outputs[0].ref["blob_hash"]
        before_receipts = _receipt_count(project)
        before_blobs = [tuple(row) for row in project.db.execute("SELECT * FROM blobs")]
        wrapper = _wrap_project_db_commit(monkeypatch, project)
        delivered = []
        if destination_kind == "local_dir":
            original = exports._deliver_export_artifact

            def deliver(*args, **kwargs):
                value = original(*args, **kwargs)
                assert destination.read_bytes().startswith(b"PK")
                delivered.append(True)
                wrapper.fail_commit = True
                return value

            monkeypatch.setattr(exports, "_deliver_export_artifact", deliver)
        else:
            original = exports._store_export_column_tables_project_blob

            def store(*args, **kwargs):
                value = original(*args, **kwargs)
                assert (
                    project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
                    == len(before_blobs) + 1
                )
                delivered.append(True)
                wrapper.fail_commit = True
                return value

            monkeypatch.setattr(
                exports, "_store_export_column_tables_project_blob", store
            )
        result = run_action_spec(
            project,
            _export_action(
                sheet_id=sheet_id,
                column_id=column_id,
                destination=target,
                key="failed-package",
                name_template="new.csv",
            ),
            project_id="p",
        )
        assert delivered == [True]
        assert result.status == "failed"
        assert result.errors[0].code == "project_write_failed"
        assert _receipt_count(project) == before_receipts
        assert [
            tuple(row) for row in project.db.execute("SELECT * FROM blobs")
        ] == before_blobs
        if destination_kind == "local_dir":
            assert destination.exists() is existing
            if existing:
                assert destination.read_bytes() == b"previous package"
            assert not list(directory.glob(".tags.zip.*"))
        elif previous_blob is not None:
            assert project.read_blob(previous_blob).startswith(b"PK")
        # Content-addressed bytes may survive rollback; shared blobs are not deleted.
    finally:
        project.close()


def test_column_tables_partial_zip_staging_is_removed(tmp_path, monkeypatch):
    from frisket.engine.executor import run_action_spec
    from tests.engine.test_export_column_tables_executor import (
        _export_action,
        _seed_scalar_list_sheet,
    )

    project = Project.create(tmp_path / "project")
    try:
        sheet_id, column_id = _seed_scalar_list_sheet(project)
        directory = tmp_path / "exports"
        directory.mkdir()
        destination = directory / "tags.zip"
        destination.write_bytes(b"previous package")
        write_bytes = Path.write_bytes
        staged = []

        def partial_write(path, data):
            if path.name.startswith(".tags.zip.") and path.suffix == ".tmp":
                write_bytes(path, data[: max(1, len(data) // 2)])
                staged.append(path)
                raise OSError("partial ZIP staging failure")
            return write_bytes(path, data)

        monkeypatch.setattr(Path, "write_bytes", partial_write)
        before = _receipt_count(project)
        result = run_action_spec(
            project,
            _export_action(
                sheet_id=sheet_id,
                column_id=column_id,
                destination={"kind": "local_dir", "path": str(directory)},
                key="partial-package",
            ),
            project_id="p",
        )
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_export_destination"
        assert len(staged) == 1
        assert not staged[0].exists()
        assert destination.read_bytes() == b"previous package"
        assert _receipt_count(project) == before
    finally:
        project.close()


def test_column_tables_finalize_failure_preserves_committed_success(
    tmp_path, monkeypatch
):
    from frisket.engine.executor import run_action_spec
    from frisket.engine.executor.action_families import exports_column_tables as exports
    from tests.engine.test_export_column_tables_executor import (
        _export_action,
        _seed_scalar_list_sheet,
    )

    project = Project.create(tmp_path / "project")
    try:
        sheet_id, column_id = _seed_scalar_list_sheet(project)
        directory = tmp_path / "exports"
        directory.mkdir()
        destination = directory / "tags.zip"
        destination.write_bytes(b"previous package")
        original = exports._finalize_export_column_tables
        finalized = []

        def fail_finalization(resolved):
            original(resolved)
            finalized.append(True)
            raise OSError("post-commit finalization failure")

        monkeypatch.setattr(
            exports, "_finalize_export_column_tables", fail_finalization
        )
        request = _export_action(
            sheet_id=sheet_id,
            column_id=column_id,
            destination={"kind": "local_dir", "path": str(directory)},
            key="committed-package",
        )
        result = run_action_spec(project, request, project_id="p")
        assert result.status == "completed", result.errors
        assert finalized == [True]
        assert destination.read_bytes().startswith(b"PK")
        assert _receipt_count(project) == 1
        replay = run_action_spec(project, request, project_id="p")
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == result.receipt_id
        assert finalized == [True]
    finally:
        project.close()
