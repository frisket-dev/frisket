from __future__ import annotations

import asyncio
import hashlib
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import frisket.engine.jobs.import_table as table_job
from frisket.actions.system import typed_action_for_request
from frisket.engine.executor.action_jobs import (
    ActionJobEnvelope,
    reserve_typed_action_job,
)
from frisket.engine.jobs import HandlerRegistry, SqliteJobQueue, Worker
from frisket.engine.jobs.import_table import (
    register_import_table_analysis_handler,
    register_import_table_handler,
    table_import_dedupe_key,
)
from frisket.engine.jobs.queue import (
    IMPORT_TABLE_ANALYSIS_KIND,
    IMPORT_TABLE_STREAM_KIND,
)
from frisket.engine.store import Project
from frisket.engine.store.import_intake import (
    ImportIntakeHeader,
    import_intake_dir,
    read_import_header,
)
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
                    {"name": "extra values", "type": "json"},
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
                _bound(
                    rows=[
                        {
                            "id": "one",
                            "amount": 1,
                            "note": "inline",
                            "extra values": None,
                        }
                    ]
                ),
            )


def test_table_analysis_runs_on_the_shared_worker_queue(tmp_path: Path) -> None:
    calls: list[tuple[str, str, int]] = []
    Project.create(tmp_path / f"{PROJECT_ID}.frisket").close()
    with closing(SqliteJobQueue(tmp_path / "queue.db")) as queue:
        registry = HandlerRegistry()
        register_import_table_analysis_handler(
            registry,
            workspace_root=tmp_path,
            run_analysis=lambda _project, project_id, draft_id, revision: calls.append(
                (project_id, draft_id, revision)
            ),
        )
        job_id = queue.enqueue(
            IMPORT_TABLE_ANALYSIS_KIND,
            {
                "project_id": PROJECT_ID,
                "draft_id": IMPORT_REF,
                "settings_revision": 3,
            },
        )

        assert Worker(queue, registry, worker_id="analysis-worker").run_once()
        job = queue.get(job_id)
        assert job is not None
        assert job.status == "done"
        assert job.result == {
            "draft_id": IMPORT_REF,
            "settings_revision": 3,
        }
    assert calls == [(PROJECT_ID, IMPORT_REF, 3)]


def test_commit_table_binds_bootstraps_and_enqueues_exact_frozen_plan(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / f"{PROJECT_ID}.frisket"
    data = b"id\none\n"
    queued: list[tuple[str, str, str]] = []
    with closing(Project.create(project_path)) as project:
        directory = import_intake_dir(project.path, IMPORT_REF)
        directory.mkdir(parents=True)
        (directory / "source").write_bytes(data)
        settings = TableReadSettings(kind="delimited", delimiter=",", encoding="utf-8")
        initialize_table_intake(
            directory,
            ImportIntakeHeader(PROJECT_ID, project.storage_identity, {}, max_rows=4),
            source=TableImportSource(
                filename="rows.csv",
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
                sheet_name="Imported rows",
                destination_sheet_id=None,
                columns=(TableImportColumn(0, "id", "id", "text"),),
                extra_values_target_name="extra values",
            ),
        )
        settings_hash = read_table_settings(directory)[1]
        write_table_scan(
            directory,
            TableImportScan(0, settings_hash, 1, 1, 1, True),
            expected_settings_revision=0,
        )
        workspace = SimpleNamespace(get=lambda _project_id: project)
        service = ImportSessionService(
            workspace,
            enqueue_table=lambda project_id, ref, plan_hash: queued.append(
                (project_id, ref, plan_hash)
            ),
        )

        with pytest.raises(ValueError, match="row limit"):
            asyncio.run(service.commit_table(PROJECT_ID, frozen, max_rows=0))
        assert ImportSessionStore(project).get(IMPORT_REF) is None

        status = asyncio.run(service.commit_table(PROJECT_ID, frozen, max_rows=5))

        assert status.state == "running"
        assert queued == [(PROJECT_ID, IMPORT_REF, frozen.plan_hash)]
        header = read_import_header(directory)
        envelope = ActionJobEnvelope.from_json(header.envelope)
        assert envelope.action["params"]["columns"][-1] == {
            "name": "extra values",
            "type": "json",
        }
        assert header.max_rows == 4
        session = ImportSessionStore(project).get(IMPORT_REF)
        assert session is not None
        assert session.receipt_id == envelope.receipt_id
        source_input = next(
            item
            for item in ReceiptStore(project).parsed_by_id(envelope.receipt_id).inputs
            if item.name == "source"
        )
        assert source_input.ref["reviewed_plan"]["columns"][0]["target_name"] == "id"

        # Bootstrap deliberately pauses until the ordinary worker owns the
        # writer.  While that job is queued, the public status must stay live
        # so clients keep polling and never ask for the committed source again.
        StreamingSheetWriter._from_session(project, session).pause(expected_cursor=0)
        with closing(SqliteJobQueue(tmp_path / "queue.db")) as queue:
            job_id = queue.enqueue(
                IMPORT_TABLE_STREAM_KIND,
                {
                    "project_id": PROJECT_ID,
                    "import_ref": IMPORT_REF,
                    "plan_hash": frozen.plan_hash,
                    "dedupe_key": table_import_dedupe_key(
                        PROJECT_ID, IMPORT_REF, frozen.plan_hash
                    ),
                },
            )
            projected_workspace = SimpleNamespace(
                queue=queue,
                queue_storage_org_id=None,
                get=lambda _project_id: project,
            )
            projected_service = ImportSessionService(projected_workspace)
            queued_status = projected_service.status(PROJECT_ID, IMPORT_REF)
            assert queued_status.state == "running"
            assert queued_status.sealed is True
            assert queued_status.committed_rows == 0

            claimed = queue.claim("retrying-worker")
            assert claimed is not None and claimed.id == job_id
            assert queue.fail(job_id, "retrying-worker", "retry once", retry=True)
            assert projected_service.status(PROJECT_ID, IMPORT_REF).state == "running"

            registry = HandlerRegistry()
            register_import_table_handler(
                registry, workspace_root=tmp_path, queue=queue
            )
            assert Worker(queue, registry, worker_id="status-worker").run_once()
            job = queue.get(job_id)
            assert job is not None and job.status == "done", job.error

            completed_status = projected_service.status(PROJECT_ID, IMPORT_REF)
            assert completed_status.state == "completed"
            assert completed_status.sealed is True
            assert completed_status.committed_rows == 1


def test_table_import_list_keeps_live_first_and_completed_by_recency(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / f"{PROJECT_ID}.frisket"
    data = b"id\none\n"
    with closing(Project.create(project_path)) as project:
        service = ImportSessionService(
            SimpleNamespace(get=lambda _project_id: project),
            enqueue_table=lambda *_args: None,
        )

        def commit(ref: str) -> None:
            directory = import_intake_dir(project.path, ref)
            directory.mkdir(parents=True)
            (directory / "source").write_bytes(data)
            settings = TableReadSettings(
                kind="delimited", delimiter=",", encoding="utf-8"
            )
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
                TableImportScan(0, settings_hash, 1, 1, 1, True),
                expected_settings_revision=0,
            )
            frozen = freeze_table_plan(
                directory,
                TableImportPlan(
                    settings_revision=0,
                    settings=settings,
                    sheet_name=f"Imported {ref[-1]}",
                    destination_sheet_id=None,
                    columns=(TableImportColumn(0, "id", "id", "text"),),
                ),
            )
            asyncio.run(service.commit_table(PROJECT_ID, frozen))

        older = "import-" + "1" * 32
        newer = "import-" + "2" * 32
        live = "import-" + "9" * 32
        for ref in (older, newer, live):
            commit(ref)
        live_session = ImportSessionStore(project).get(live)
        assert live_session is not None
        StreamingSheetWriter._from_session(project, live_session).pause(
            expected_cursor=0
        )
        project.db.execute(
            "UPDATE import_sessions SET state='completed',updated_at=? WHERE id=?",
            ("2026-01-01 00:00:00", older),
        )
        project.db.execute(
            "UPDATE import_sessions SET state='completed',updated_at=? WHERE id=?",
            ("2026-01-02 00:00:00", newer),
        )
        project.db.commit()

        listed = service.list(PROJECT_ID).sessions

        assert [status.import_ref for status in listed] == [live, newer, older]
        assert [status.state for status in listed] == [
            "paused",
            "completed",
            "completed",
        ]


def test_table_worker_streams_real_source_and_publishes_durable_errors(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / f"{PROJECT_ID}.frisket"
    data = b'id,amount,note\na,1,first\nb,N/A,"line one\nline two",overflow\nb,2,last\n'
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
                extra_values_target_name="extra values",
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
                {"name": "extra values", "type": "json"},
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
            assert listed["result_summary"]["problems"] == 2
            assert {item["code"] for item in listed["import_errors"]} == {
                "conversion_failed",
                "extra_values",
            }

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
        assert list(values["extra values"].values()) == [None, ["overflow"], None]
        assert (
            ReceiptStore(project).parsed_by_id(envelope.receipt_id).status
            == "completed"
        )

    status = table_import_status(directory)
    assert (status.phase, status.source_cursor, status.problem_count) == (
        "terminal",
        3,
        2,
    )
    group = next(
        item
        for item in table_issue_groups(directory, through=status.source_cursor)
        if item["code"] == "conversion_failed"
    )
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


def test_full_duplicate_page_advances_processed_cursor(tmp_path: Path) -> None:
    project_path = tmp_path / f"{PROJECT_ID}.frisket"
    data = b"id\n" + b"existing\n" * 256
    with closing(Project.create(project_path)) as project:
        sheet_id = project.add_sheet("Existing")
        column_id = project.add_column(sheet_id, "id", "text")
        project.add_rows(sheet_id, [{"id": "existing"}], {"id": column_id})
        directory = import_intake_dir(project.path, IMPORT_REF)
        directory.mkdir(parents=True)
        (directory / "source").write_bytes(data)
        settings = TableReadSettings(kind="delimited", delimiter=",", encoding="utf-8")
        initialize_table_intake(
            directory,
            ImportIntakeHeader(PROJECT_ID, project.storage_identity, {}),
            source=TableImportSource(
                filename="duplicates.csv",
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                kind="delimited",
            ),
            settings=settings,
        )
        key = table_dedup_key("existing")
        assert key is not None
        dedup_revision = replace_table_dedup_keys(directory, [key], expected_revision=0)
        frozen = freeze_table_plan(
            directory,
            TableImportPlan(
                settings_revision=0,
                settings=settings,
                sheet_name="Existing",
                destination_sheet_id=sheet_id,
                columns=(TableImportColumn(0, "id", "id", "text", column_id),),
                dedup=TableImportDedup(0, column_id, dedup_revision),
            ),
        )
        workspace = SimpleNamespace(get=lambda _project_id: project)
        asyncio.run(ImportSessionService(workspace).commit_table(PROJECT_ID, frozen))

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
        assert Worker(queue, registry, worker_id="duplicate-worker").run_once()
        assert queue.get(job_id).status == "done"

    with closing(Project(project_path)) as project:
        status = ImportSessionService(
            SimpleNamespace(get=lambda _project_id: project)
        ).status(PROJECT_ID, IMPORT_REF)
        assert status.processed_rows == 256
        assert status.committed_rows == 0
        assert status.skipped_duplicates == 256


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


def test_terminal_replay_finishes_intake_after_publication_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_path = tmp_path / f"{PROJECT_ID}.frisket"
    data = b"id\none\n"
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
        frozen = freeze_table_plan(
            directory,
            TableImportPlan(
                settings_revision=0,
                settings=settings,
                sheet_name="Imported rows",
                destination_sheet_id=None,
                columns=(TableImportColumn(0, "id", "id", "text"),),
            ),
        )
        asyncio.run(
            ImportSessionService(
                SimpleNamespace(get=lambda _project_id: project)
            ).commit_table(PROJECT_ID, frozen)
        )

    real_finish = table_job.finish_table_intake
    failed_once = False

    def fail_after_publication(*args, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("simulated crash after sheet publication")
        return real_finish(*args, **kwargs)

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
        monkeypatch.setattr(table_job, "finish_table_intake", fail_after_publication)
        assert Worker(queue, registry, worker_id="first-worker").run_once()
        assert queue.get(job_id).status == "queued"
        with closing(Project(project_path)) as project:
            session = ImportSessionStore(project).get(IMPORT_REF)
            assert session is not None and session.state == "completed"
            visible = ImportSessionService(
                SimpleNamespace(queue=queue, get=lambda _project_id: project)
            ).status(PROJECT_ID, IMPORT_REF)
            assert visible.error is not None
            assert visible.error.startswith("table_import_failed:")
        assert table_import_status(directory).phase == "committed"
        assert (directory / "source").is_file()

        monkeypatch.setattr(table_job, "finish_table_intake", real_finish)
        now[0] += timedelta(minutes=1)
        assert Worker(queue, registry, worker_id="recovery-worker").run_once()
        assert queue.get(job_id).status == "done"

    assert table_import_status(directory).phase == "terminal"
    assert not (directory / "source").exists()


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
                filename="stop.xlsx",
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                kind="xlsx",
                retain_for_workbook_reuse=True,
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
        assert (directory / "source").is_file()

        [listed] = ImportSessionService(workspace).list(PROJECT_ID).sessions
        assert listed.state == "kept"
        assert (listed.committed_rows, listed.processed_rows) == (1, 1)
        assert listed.can_rerun_workbook is True
