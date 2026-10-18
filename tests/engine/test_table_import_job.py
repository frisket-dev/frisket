from __future__ import annotations

import hashlib
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from frisket.actions.system import typed_action_for_request
from frisket.engine.executor.action_jobs import (
    ActionJobEnvelope,
    reserve_typed_action_job,
)
from frisket.engine.jobs import HandlerRegistry, SqliteJobQueue, Worker
from frisket.engine.jobs.import_table import register_import_table_handler
from frisket.engine.jobs.queue import IMPORT_TABLE_STREAM_KIND
from frisket.engine.store import Project
from frisket.engine.store.import_intake import ImportIntakeHeader, import_intake_dir
from frisket.engine.store.import_sessions import ImportSessionStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.streaming_import import StreamingSheetWriter
from frisket.engine.store.table_import_intake import (
    TableImportColumn,
    TableImportDedup,
    TableImportPlan,
    TableImportScan,
    TableImportSource,
    freeze_table_plan,
    initialize_table_intake,
    mark_table_committed,
    read_table_settings,
    replace_table_dedup_keys,
    table_dedup_key,
    table_import_status,
    table_issue_groups,
    write_table_scan,
)
from frisket.table_import import TableReadSettings
from frisket.server.services.action_runs import ActionRunService
from frisket.server.services.import_sessions import ImportSessionService


PROJECT_ID = "table-project"
IMPORT_REF = "import-" + "7" * 32


def _bound(*, rows: list[dict[str, object]]):
    return typed_action_for_request(
        {
            "action_id": "import.rows",
            "scope": {"kind": "project"},
            "sheet_name": "Imported rows",
            "params": {
                "columns": [
                    {
                        "name": "id",
                        "source_name": "id",
                        "type": "text",
                    },
                    {
                        "name": "amount",
                        "source_name": "amount",
                        "type": "number",
                    },
                    {
                        "name": "note",
                        "source_name": "note",
                        "type": "text",
                    },
                ],
                "rows": rows,
                "source": {
                    "kind": "file",
                    "label": "rows.csv",
                    "fingerprint": "sha256:source",
                    "importer": "table",
                },
            },
            "idempotency_key": IMPORT_REF,
        }
    )


def _single_column_bound():
    return typed_action_for_request(
        {
            "action_id": "import.rows",
            "scope": {"kind": "project"},
            "sheet_name": "Restart rows",
            "params": {
                "columns": [{"name": "id", "source_name": "id", "type": "text"}],
                "rows": [],
            },
            "idempotency_key": IMPORT_REF,
        }
    )


def test_empty_import_rows_can_reserve_for_streaming_but_inline_rows_cannot(
    tmp_path: Path,
) -> None:
    with closing(Project.create(tmp_path / "reserve.frisket")) as project:
        envelope = reserve_typed_action_job(project, PROJECT_ID, _bound(rows=[]))
        assert isinstance(envelope, ActionJobEnvelope)
        with pytest.raises(TypeError, match="supported callable"):
            reserve_typed_action_job(
                project,
                PROJECT_ID,
                _bound(rows=[{"id": "one", "amount": 1, "note": "inline"}]),
            )


def test_table_worker_streams_real_source_and_publishes_durable_errors(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / f"{PROJECT_ID}.frisket"
    data = b'id,amount,note\na,1,first\nb,N/A,"line one\nline two"\nb,2,last\n'
    with closing(Project.create(project_path)) as project:
        directory = import_intake_dir(project.path, IMPORT_REF)
        directory.mkdir(parents=True)
        (directory / "source").write_bytes(data)
        settings = TableReadSettings(kind="delimited", delimiter=",", encoding="utf-8")
        initialize_table_intake(
            directory,
            ImportIntakeHeader(PROJECT_ID, project.storage_identity, {}),
            source=TableImportSource(
                filename="rows.csv",
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                kind="delimited",
            ),
            settings=settings,
        )
        settings_hash = read_table_settings(directory)[1]
        write_table_scan(
            directory,
            TableImportScan(0, settings_hash, 3, 3, 3, True),
            expected_settings_revision=0,
        )
        frozen = freeze_table_plan(
            directory,
            TableImportPlan(
                settings_revision=0,
                settings=settings,
                sheet_name="Imported rows",
                destination_sheet_id=None,
                columns=(
                    TableImportColumn(0, "id", "id", "text"),
                    TableImportColumn(1, "amount", "amount", "number"),
                    TableImportColumn(2, "note", "note", "text"),
                ),
            ),
        )
        envelope = reserve_typed_action_job(project, PROJECT_ID, _bound(rows=[]))
        assert isinstance(envelope, ActionJobEnvelope)
        mark_table_committed(
            directory,
            expected_plan_hash=frozen.plan_hash,
            envelope=envelope.to_json(),
            destination_sheet_id=None,
        )
        receipt = ReceiptStore(project).parsed_by_id(envelope.receipt_id)
        ReceiptStore(project).update_body_status(
            receipt.model_copy(update={"status": "running"}),
            require_status="queued",
        )
        StreamingSheetWriter.start_session(
            project,
            session_id=IMPORT_REF,
            writer_authority="bootstrap",
            sheet_name="Imported rows",
            columns=[
                {"name": "id", "type": "text"},
                {"name": "amount", "type": "number"},
                {"name": "note", "type": "text"},
            ],
            project_id=PROJECT_ID,
            action_kind="import.rows",
            idempotency_key=envelope.idempotency_key,
            params_hash=envelope.params_hash,
            action_id=envelope.action_id,
            receipt_id=envelope.receipt_id,
            source_ref={
                "kind": "file",
                "label": "rows.csv",
                "fingerprint": hashlib.sha256(data).hexdigest(),
                "importer": "table",
            },
        ).abort()

    with closing(SqliteJobQueue(tmp_path / "queue.db")) as queue:
        registry = HandlerRegistry()
        register_import_table_handler(registry, workspace_root=tmp_path, queue=queue)
        job_id = queue.enqueue(
            IMPORT_TABLE_STREAM_KIND,
            {
                "project_id": PROJECT_ID,
                "import_ref": IMPORT_REF,
                "plan_hash": frozen.plan_hash,
                "dedupe_key": f"table:{IMPORT_REF}",
            },
        )
        assert Worker(queue, registry, worker_id="table-worker").run_once()
        job = queue.get(job_id)
        assert job is not None and job.status == "done", job.error
        with closing(Project(project_path)) as listed_project:
            workspace = SimpleNamespace(
                queue=queue,
                queue_storage_org_id=None,
                get=lambda _project_id: listed_project,
            )
            [listed] = ActionRunService(workspace).list_jobs(PROJECT_ID)["jobs"]
            assert listed["payload_ref"] == {
                "kind": "import",
                "import_ref": IMPORT_REF,
            }
            assert listed["result_summary"]["problems"] == 1
            assert listed["import_errors"][0]["code"] == "conversion_failed"

    with closing(Project(project_path)) as project:
        session = ImportSessionStore(project).get(IMPORT_REF)
        assert session is not None and session.state == "completed"
        assert (session.cursor, session.committed_rows) == (3, 3)
        assert project.row_count(session.sheet_id) == 3
        values = {
            str(column["name"]): project.get_values(session.sheet_id, int(column["id"]))
            for column in project.columns(session.sheet_id)
        }
        assert list(values["id"].values()) == ["a", "b", "b"]
        assert list(values["amount"].values()) == [1.0, None, 2.0]
        assert list(values["note"].values()) == [
            "first",
            "line one\nline two",
            "last",
        ]
        assert (
            ReceiptStore(project).parsed_by_id(envelope.receipt_id).status
            == "completed"
        )

    status = table_import_status(directory)
    assert (status.phase, status.source_cursor, status.problem_count) == (
        "terminal",
        3,
        1,
    )
    [group] = table_issue_groups(directory, through=status.source_cursor)
    assert (group["code"], group["count"]) == ("conversion_failed", 1)
    assert group["examples"][0]["original"] == "N/A"
    assert not (directory / "source").exists()


def test_existing_sheet_dedup_skips_only_snapshot_and_one_undo_keeps_old_rows(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / f"{PROJECT_ID}.frisket"
    data = b"id,value\nexisting,skip\n,blank\nnew,first\nnew,second\n"
    with closing(Project.create(project_path)) as project:
        sheet_id = project.add_sheet("Existing")
        id_column = project.add_column(sheet_id, "id", "text")
        value_column = project.add_column(sheet_id, "value", "text")
        old_row = project.add_rows(
            sheet_id,
            [{"id": "existing", "value": "before"}],
            {"id": id_column, "value": value_column},
        )[0]
        directory = import_intake_dir(project.path, IMPORT_REF)
        directory.mkdir(parents=True)
        (directory / "source").write_bytes(data)
        settings = TableReadSettings(kind="delimited", delimiter=",", encoding="utf-8")
        initialize_table_intake(
            directory,
            ImportIntakeHeader(PROJECT_ID, project.storage_identity, {}),
            source=TableImportSource(
                filename="append.csv",
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                kind="delimited",
            ),
            settings=settings,
        )
        key = table_dedup_key("existing")
        assert key is not None
        snapshot_revision = replace_table_dedup_keys(
            directory, [key], expected_revision=0
        )
        plan = TableImportPlan(
            settings_revision=0,
            settings=settings,
            sheet_name="Existing",
            destination_sheet_id=sheet_id,
            columns=(
                TableImportColumn(0, "id", "id", "text", id_column),
                TableImportColumn(1, "value", "value", "text", value_column),
            ),
            dedup=TableImportDedup(0, id_column, snapshot_revision),
        )
        frozen = freeze_table_plan(directory, plan)
        bound = typed_action_for_request(
            {
                "action_id": "import.append_rows",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {
                    "columns": [
                        {"name": "id", "source_name": "id", "type": "text"},
                        {
                            "name": "value",
                            "source_name": "value",
                            "type": "text",
                        },
                    ],
                    "rows": [],
                },
                "idempotency_key": IMPORT_REF,
            }
        )
        envelope = reserve_typed_action_job(project, PROJECT_ID, bound)
        assert isinstance(envelope, ActionJobEnvelope)
        mark_table_committed(
            directory,
            expected_plan_hash=frozen.plan_hash,
            envelope=envelope.to_json(),
            destination_sheet_id=sheet_id,
        )
        receipt = ReceiptStore(project).parsed_by_id(envelope.receipt_id)
        ReceiptStore(project).update_body_status(
            receipt.model_copy(update={"status": "running"}),
            require_status="queued",
        )
        StreamingSheetWriter.start_append_session(
            project,
            session_id=IMPORT_REF,
            writer_authority="bootstrap",
            sheet_id=sheet_id,
            columns=[
                {"name": "id", "type": "text"},
                {"name": "value", "type": "text"},
            ],
            project_id=PROJECT_ID,
            action_kind="import.append_rows",
            idempotency_key=envelope.idempotency_key,
            params_hash=envelope.params_hash,
            action_id=envelope.action_id,
            receipt_id=envelope.receipt_id,
            source_ref={"kind": "file", "label": "append.csv", "importer": "table"},
        ).abort()

    with closing(SqliteJobQueue(tmp_path / "queue.db")) as queue:
        registry = HandlerRegistry()
        register_import_table_handler(registry, workspace_root=tmp_path, queue=queue)
        job_id = queue.enqueue(
            IMPORT_TABLE_STREAM_KIND,
            {
                "project_id": PROJECT_ID,
                "import_ref": IMPORT_REF,
                "plan_hash": frozen.plan_hash,
            },
        )
        assert Worker(queue, registry, worker_id="append-worker").run_once()
        assert queue.get(job_id).status == "done"

    with closing(Project(project_path)) as project:
        session = ImportSessionStore(project).get(IMPORT_REF)
        assert session is not None
        assert (session.cursor, session.committed_rows) == (4, 3)
        rows = project.visible_row_ids(sheet_id)
        assert rows[0] == old_row
        assert list(project.get_values(sheet_id, id_column, rows).values()) == [
            "existing",
            None,
            "new",
            "new",
        ]
        assert project.undo() == session.op_id
        assert project.visible_row_ids(sheet_id) == [old_row]


def test_worker_reopens_source_at_durable_cursor_after_process_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_path = tmp_path / f"{PROJECT_ID}.frisket"
    data = ("id\n" + "".join(f"row-{index}\n" for index in range(300))).encode()
    with closing(Project.create(project_path)) as project:
        directory = import_intake_dir(project.path, IMPORT_REF)
        directory.mkdir(parents=True)
        (directory / "source").write_bytes(data)
        settings = TableReadSettings(kind="delimited", delimiter=",", encoding="utf-8")
        initialize_table_intake(
            directory,
            ImportIntakeHeader(PROJECT_ID, project.storage_identity, {}),
            source=TableImportSource(
                filename="restart.csv",
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                kind="delimited",
            ),
            settings=settings,
        )
        frozen = freeze_table_plan(
            directory,
            TableImportPlan(
                settings_revision=0,
                settings=settings,
                sheet_name="Restart rows",
                destination_sheet_id=None,
                columns=(TableImportColumn(0, "id", "id", "text"),),
            ),
        )
        envelope = reserve_typed_action_job(project, PROJECT_ID, _single_column_bound())
        assert isinstance(envelope, ActionJobEnvelope)
        mark_table_committed(
            directory,
            expected_plan_hash=frozen.plan_hash,
            envelope=envelope.to_json(),
            destination_sheet_id=None,
        )
        receipt = ReceiptStore(project).parsed_by_id(envelope.receipt_id)
        ReceiptStore(project).update_body_status(
            receipt.model_copy(update={"status": "running"}),
            require_status="queued",
        )
        StreamingSheetWriter.start_session(
            project,
            session_id=IMPORT_REF,
            writer_authority="bootstrap",
            sheet_name="Restart rows",
            columns=[{"name": "id", "type": "text"}],
            project_id=PROJECT_ID,
            action_kind="import.rows",
            idempotency_key=envelope.idempotency_key,
            params_hash=envelope.params_hash,
            action_id=envelope.action_id,
            receipt_id=envelope.receipt_id,
            source_ref={"kind": "file", "label": "restart.csv"},
        ).abort()

    original_append = StreamingSheetWriter.append_page
    interrupted = False

    def append_then_fail(self, records, **kwargs):
        nonlocal interrupted
        row_ids = original_append(self, records, **kwargs)
        if not interrupted:
            interrupted = True
            raise RuntimeError("simulated process exit after durable page commit")
        return row_ids

    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    with closing(SqliteJobQueue(tmp_path / "queue.db", clock=lambda: now[0])) as queue:
        registry = HandlerRegistry()
        register_import_table_handler(registry, workspace_root=tmp_path, queue=queue)
        job_id = queue.enqueue(
            IMPORT_TABLE_STREAM_KIND,
            {
                "project_id": PROJECT_ID,
                "import_ref": IMPORT_REF,
                "plan_hash": frozen.plan_hash,
            },
        )
        monkeypatch.setattr(StreamingSheetWriter, "append_page", append_then_fail)
        assert Worker(queue, registry, worker_id="first-worker").run_once()
        assert queue.get(job_id).status == "queued"
        with closing(Project(project_path)) as project:
            session = ImportSessionStore(project).get(IMPORT_REF)
            assert session is not None
            assert (session.state, session.cursor, session.committed_rows) == (
                "paused",
                256,
                256,
            )

        monkeypatch.setattr(StreamingSheetWriter, "append_page", original_append)
        now[0] += timedelta(minutes=1)
        assert Worker(queue, registry, worker_id="recovery-worker").run_once()
        job = queue.get(job_id)
        assert job is not None and job.status == "done", job.error

    with closing(Project(project_path)) as project:
        session = ImportSessionStore(project).get(IMPORT_REF)
        assert session is not None
        assert (session.state, session.cursor, session.committed_rows) == (
            "completed",
            300,
            300,
        )
        ids = list(
            project.get_values(
                session.sheet_id,
                int(project.columns(session.sheet_id)[0]["id"]),
            ).values()
        )
        assert ids == [f"row-{index}" for index in range(300)]


def test_stop_keeps_committed_table_rows_and_finishes_intake(tmp_path: Path) -> None:
    project_path = tmp_path / f"{PROJECT_ID}.frisket"
    data = b"id\none\ntwo\n"
    with closing(Project.create(project_path)) as project:
        directory = import_intake_dir(project.path, IMPORT_REF)
        directory.mkdir(parents=True)
        (directory / "source").write_bytes(data)
        settings = TableReadSettings(kind="delimited", delimiter=",", encoding="utf-8")
        initialize_table_intake(
            directory,
            ImportIntakeHeader(PROJECT_ID, project.storage_identity, {}),
            source=TableImportSource(
                filename="stop.csv",
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                kind="delimited",
            ),
            settings=settings,
        )
        frozen = freeze_table_plan(
            directory,
            TableImportPlan(
                settings_revision=0,
                settings=settings,
                sheet_name="Restart rows",
                destination_sheet_id=None,
                columns=(TableImportColumn(0, "id", "id", "text"),),
            ),
        )
        envelope = reserve_typed_action_job(project, PROJECT_ID, _single_column_bound())
        assert isinstance(envelope, ActionJobEnvelope)
        mark_table_committed(
            directory,
            expected_plan_hash=frozen.plan_hash,
            envelope=envelope.to_json(),
            destination_sheet_id=None,
        )
        receipt = ReceiptStore(project).parsed_by_id(envelope.receipt_id)
        ReceiptStore(project).update_body_status(
            receipt.model_copy(update={"status": "running"}),
            require_status="queued",
        )
        writer = StreamingSheetWriter.start_session(
            project,
            session_id=IMPORT_REF,
            writer_authority="bootstrap",
            sheet_name="Restart rows",
            columns=[{"name": "id", "type": "text"}],
            project_id=PROJECT_ID,
            action_kind="import.rows",
            idempotency_key=envelope.idempotency_key,
            params_hash=envelope.params_hash,
            action_id=envelope.action_id,
            receipt_id=envelope.receipt_id,
            source_ref={"kind": "file", "label": "stop.csv"},
        )
        writer.append_page([{"id": "one"}], expected_cursor=0, next_cursor=1)
        workspace = SimpleNamespace(
            get=lambda _project_id: project,
            queue=None,
            queue_storage_org_id=None,
            root=tmp_path,
        )
        status = ImportSessionService(workspace).cancel(PROJECT_ID, IMPORT_REF)
        assert (status.state, status.committed_rows, status.processed_rows) == (
            "kept",
            1,
            1,
        )
        session = ImportSessionStore(project).get(IMPORT_REF)
        assert session is not None and session.state == "kept"
        assert project.row_count(session.sheet_id) == 1
        assert table_import_status(directory).phase == "terminal"
        assert not (directory / "source").exists()
