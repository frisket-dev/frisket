from __future__ import annotations
from contextlib import closing

import json
import sqlite3
from typing import Any

import pytest

from frisket.contracts.action import Receipt, ReceiptRecord, ReceiptIO
from frisket.engine.store import Project
from frisket.engine.store.import_blobs import ImportBlobPlan, PreparedImportBlobPlan
from frisket.engine.store.import_sessions import (
    ImportSessionConflict,
    ImportSessionStore,
)
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.streaming_import import (
    DuplicateStreamingPublication,
    StreamingSheetWriter,
)
from frisket.engine.store.transactions import write_transaction


COLUMNS = [
    {"name": "ordinal", "type": "integer"},
    {"name": "subject", "type": "text", "format": "plain_text"},
    {"name": "attachment", "type": "file"},
]


def _start(
    project: Project,
    *,
    name: str = "Evidence",
    idempotency_key: str | None = None,
    params_hash: str | None = None,
    action_id: str | None = None,
    receipt_id: str | None = None,
) -> StreamingSheetWriter:
    return StreamingSheetWriter.start(
        project,
        sheet_name=name,
        columns=COLUMNS,
        project_id="streaming-project",
        action_kind="import.csv",
        idempotency_key=idempotency_key or f"streaming:{name}",
        params_hash=params_hash or f"sha256:caller-{name}",
        action_id=action_id or f"act:caller-{name}",
        receipt_id=receipt_id or f"receipt:caller-{name}",
        source_ref={"kind": "file", "logical_path": "evidence.csv"},
    )


def _counts(project: Project) -> dict[str, int]:
    return {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("sheets", "columns", "rows", "cells", "ops", "receipts")
    }


def _records(start: int, stop: int) -> list[dict[str, Any]]:
    return [
        {"ordinal": ordinal, "subject": f"message {ordinal}", "attachment": None}
        for ordinal in range(start, stop)
    ]


def _start_session(project: Project, *, authority: str = "claim:one"):
    return StreamingSheetWriter.start_session(
        project,
        session_id="import:one",
        writer_authority=authority,
        sheet_name="Visible import",
        columns=COLUMNS,
        project_id="streaming-project",
        action_kind="import.files",
        idempotency_key="inventory:one",
        params_hash="sha256:inventory-one",
        action_id="act:inventory-one",
        receipt_id="receipt:inventory-one",
        source_ref={"kind": "file_inventory", "inventory_id": "inventory:one"},
    )


def test_resumable_session_reopens_without_duplicate_rows(make_project) -> None:
    project = make_project("resume")
    writer = _start_session(project)
    assert project.sheets()[0]["id"] == writer.sheet_id
    writer.append_page(_records(0, 2), expected_cursor=0, next_cursor=2)
    writer.abort()

    resumed = StreamingSheetWriter.resume_session(
        project,
        session_id="import:one",
        writer_authority="claim:two",
        expected_cursor=2,
    )
    resumed.append_page(_records(2, 4), expected_cursor=2, next_cursor=4)
    publication = resumed.finalize_session(expected_cursor=4, enumeration_complete=True)

    assert publication.row_count == 4
    session = ImportSessionStore(project).get("import:one")
    assert session is not None and session.state == "completed"
    assert session.cursor == 4 and session.committed_rows == 4
    assert project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] == 1
    producer = project.db.execute("SELECT id,op_id FROM base_cell_producers").fetchone()
    assert producer is not None and producer["op_id"] == publication.op_id
    receipt = ReceiptStore(project).find_by_id(publication.receipt_id)
    assert receipt is not None and receipt.status == "completed"
    assert receipt.parsed().op_ids == [publication.op_id]
    assert (
        project.db.execute("SELECT COUNT(DISTINCT producer_id) FROM cells").fetchone()[
            0
        ]
        == 1
    )


@pytest.mark.parametrize("terminal", ["completed", "kept", "removed"])
def test_resumable_session_preserves_reserved_receipt_through_terminalization(
    make_project, terminal: str
) -> None:
    project = make_project(f"reserved-{terminal}")
    reserved = Receipt(
        receipt_id="receipt:inventory-one",
        project_id="streaming-project",
        action_id="act:inventory-one",
        action_kind="import.files",
        idempotency_key="inventory:one",
        params_hash="sha256:inventory-one",
        status="running",
        inputs=[ReceiptIO(name="reservation", ref={"kind": "job_claim"})],
        records=[ReceiptRecord(ref={"kind": "consent", "id": "consent:one"})],
        warnings=["reservation warning"],
    )
    ReceiptStore(project).insert_running(
        reserved, edition_run_context={"edition": "desktop"}
    )

    writer = _start_session(project)
    stored = ReceiptStore(project).find_by_id("receipt:inventory-one")
    assert stored is not None
    adopted = stored.parsed()
    assert adopted.op_ids == [writer._op_id]
    assert adopted.inputs[:-1] == reserved.inputs
    assert adopted.inputs[-1] == ReceiptIO(
        name="source",
        ref={"kind": "file_inventory", "inventory_id": "inventory:one"},
    )
    assert adopted.records == reserved.records
    assert adopted.warnings == reserved.warnings
    writer.abort()
    writer = StreamingSheetWriter.resume_session(
        project,
        session_id="import:one",
        writer_authority="claim:resumed",
        expected_cursor=0,
    )
    writer.set_warnings(["bounded parser warning"])
    writer.append_page(_records(0, 1), expected_cursor=0, next_cursor=1)
    if terminal == "completed":
        writer.finalize_session(expected_cursor=1, enumeration_complete=True)
    else:
        writer.cancel(expected_cursor=1)
        if terminal == "kept":
            writer.finalize_session(expected_cursor=1, keep_cancelled=True)
        else:
            writer.remove_session(expected_cursor=1)

    terminal_receipt = ReceiptStore(project).find_by_id(reserved.receipt_id)
    assert terminal_receipt is not None
    parsed = terminal_receipt.parsed()
    assert reserved.records[0] in parsed.records
    assert any(
        item.ref
        == {
            "kind": "import_source",
            "inventory_id": "inventory:one",
            "source_kind": "file_inventory",
        }
        for item in parsed.records
    )
    assert "bounded parser warning" in parsed.warnings
    assert (
        terminal_receipt.status
        == {
            "completed": "completed",
            "kept": "partial",
            "removed": "cancelled",
        }[terminal]
    )
    assert (
        project.db.execute(
            "SELECT edition_run_context FROM receipts WHERE id=?",
            (reserved.receipt_id,),
        ).fetchone()[0]
        == '{"edition": "desktop"}'
    )


def test_resumable_session_rejects_uncheckpointed_append_rows(make_project) -> None:
    project = make_project("append-guard")
    writer = _start_session(project)
    with pytest.raises(RuntimeError, match="append_page"):
        writer.append_rows(_records(0, 1))
    assert project.db.execute("SELECT COUNT(*) FROM rows").fetchone()[0] == 0


def test_resumable_page_cursor_and_rows_roll_back_together(
    make_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project("rollback")
    writer = _start_session(project)

    def fail(*_args, **_kwargs):
        raise RuntimeError("publication failed")

    monkeypatch.setattr(
        "frisket.engine.store.streaming_import.publish_import_blobs", fail
    )
    with pytest.raises(RuntimeError, match="publication failed"):
        writer.append_page(
            _records(0, 1),
            expected_cursor=0,
            next_cursor=1,
            blob_plan=PreparedImportBlobPlan(ImportBlobPlan()),
        )
    session = ImportSessionStore(project).get("import:one")
    assert session is not None and session.cursor == 0
    assert session.committed_rows == 0
    assert project.db.execute("SELECT COUNT(*) FROM rows").fetchone()[0] == 0


def test_resumable_session_rejects_stale_writer_and_keeps_cancelled_rows(
    make_project,
) -> None:
    project = make_project("stale")
    stale = _start_session(project)
    stale.append_page(_records(0, 1), expected_cursor=0, next_cursor=1)
    current = StreamingSheetWriter.resume_session(
        project,
        session_id="import:one",
        writer_authority="claim:two",
        expected_cursor=1,
    )
    with pytest.raises(ImportSessionConflict):
        stale.append_page(_records(1, 2), expected_cursor=1, next_cursor=2)

    current.cancel(expected_cursor=1)
    assert project.db.execute("SELECT COUNT(*) FROM rows").fetchone()[0] == 1
    kept = current.finalize_session(expected_cursor=1, keep_cancelled=True)
    assert kept.receipt.status == "partial"
    assert project.sheets()[0]["name"] == "Visible import"
    assert ImportSessionStore(project).get("import:one").state == "kept"


def test_cancelled_session_removal_retains_saved_data_for_restore(
    make_project, monkeypatch
) -> None:
    project = make_project("remove")
    writer = _start_session(project)
    writer.append_page(_records(0, 2), expected_cursor=0, next_cursor=2)
    project.db.executemany(
        "INSERT INTO source_artifacts "
        "(stable_id,artifact_kind,media_type,source_sheet_id) VALUES (?,?,?,?)",
        [
            (
                f"artifact:session:{index}",
                "file",
                "application/pdf",
                writer.sheet_id,
            )
            for index in range(130)
        ],
    )
    project.db.execute(
        "INSERT INTO citations "
        "(stable_id,subject_kind,sheet_id,op_id,receipt_id) VALUES (?,?,?,?,?)",
        (
            "evidence:session",
            "cell_value",
            writer.sheet_id,
            writer._op_id,
            "receipt:inventory-one",
        ),
    )
    project.db.commit()
    # Removal must not materialize every artifact ID into SQL parameters.
    # This proves corpus-sized cancellation works under SQLite's bound limit.
    project.db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 64)
    writer.cancel(expected_cursor=2)
    with monkeypatch.context() as failed:

        def fail_unapply(_operation):
            raise RuntimeError("interrupted removal")

        failed.setattr(project, "_unapply", fail_unapply)
        with pytest.raises(RuntimeError, match="interrupted removal"):
            writer.remove_session(expected_cursor=2)
    assert ImportSessionStore(project).get("import:one").state == "cancelled"
    assert ReceiptStore(project).find_by_id("receipt:inventory-one").status == "running"
    assert project.sheets()[0]["id"] == writer.sheet_id
    writer.remove_session(expected_cursor=2)

    assert project.sheets() == []
    assert project.db.execute("SELECT COUNT(*) FROM rows").fetchone()[0] == 2
    removed_receipt = ReceiptStore(project).find_by_id("receipt:inventory-one")
    assert removed_receipt is not None and removed_receipt.status == "cancelled"
    assert removed_receipt.parsed().op_ids == [writer._op_id]
    assert (
        project.db.execute(
            "SELECT barrier FROM ops WHERE id=?", (writer._op_id,)
        ).fetchone()[0]
        == 0
    )
    assert (
        project.db.execute("SELECT COUNT(*) FROM source_artifacts").fetchone()[0] == 130
    )
    assert project.db.execute("SELECT COUNT(*) FROM citations").fetchone()[0] == 1
    session = ImportSessionStore(project).get("import:one")
    assert session is not None and session.state == "removed"


def _assert_committed_staging(
    project: Project,
    *,
    sheet_id: int,
    ordinals: list[int],
    expected_cell_count: int,
) -> None:
    sheet = project.db.execute(
        "SELECT id, hidden FROM sheets WHERE id=?", (sheet_id,)
    ).fetchone()
    assert sheet is not None
    assert sheet["hidden"] == 1
    assert project.sheets() == []
    columns = {row["name"]: row for row in project.columns(sheet_id)}
    assert list(columns) == ["ordinal", "subject", "attachment"]
    assert columns["ordinal"]["type"] == "integer"
    assert columns["subject"]["format"] == "plain_text"
    assert columns["attachment"]["type"] == "file"
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM cells "
            "WHERE row_id IN (SELECT id FROM rows WHERE sheet_id=?)",
            (sheet_id,),
        ).fetchone()[0]
        == expected_cell_count
    )
    # Unbound staging payloads are durable but not current values yet.
    assert set(
        project.get_values(sheet_id, int(columns["ordinal"]["id"])).values()
    ) == {None}
    raw = project.db.execute(
        "SELECT value FROM cells WHERE column_id=? ORDER BY row_id",
        (int(columns["ordinal"]["id"]),),
    ).fetchall()
    assert [row[0] for row in raw] == ordinals
    subjects = [
        row[0]
        for row in project.db.execute(
            "SELECT value FROM cells WHERE column_id=? ORDER BY row_id",
            (int(columns["subject"]["id"]),),
        )
    ]
    assert subjects[0] in {"first", "message 0"}
    assert subjects[-1] in {"second", "fourth", f"message {ordinals[-1]}"}


def _contains_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def test_staging_producer_links_admitted_receipt_before_publish(make_project) -> None:
    project = make_project("receipt-linked")
    writer = _start(project, receipt_id="receipt:admitted")

    producer = project.db.execute(
        "SELECT stage_id,op_id FROM base_cell_producers"
    ).fetchone()
    assert producer is not None
    assert producer["op_id"] is None
    assert "receipt:admitted" in producer["stage_id"]
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0

    writer.abort()


def test_committed_batches_stay_hidden_until_atomic_publish(client) -> None:
    created = client.post("/api/projects", json={"name": "Streaming project"})
    assert created.status_code == 200, created.text
    project_id = created.json()["id"]
    project = client.app.state.workspace.get(project_id)

    payload = b"canonical attachment bytes"
    digest = project.add_blob(
        payload,
        filename="evidence.txt",
        mime="text/plain",
    )
    attachment = {
        "blob": digest,
        "mime": "text/plain",
        "filename": "evidence.txt",
    }
    writer = _start(project)
    writer.append_rows(
        [
            {"ordinal": 1, "subject": "first", "attachment": attachment},
            {"ordinal": 2, "subject": "second", "attachment": None},
        ]
    )

    # A separately opened project sees the first durable batch in raw storage,
    # while every normal list/read surface still treats its sheet as absent.
    with closing(Project(project.path)) as first_observer:
        assert first_observer.db.execute("SELECT COUNT(*) FROM rows").fetchone()[0] == 2
        _assert_committed_staging(
            first_observer,
            sheet_id=writer.sheet_id,
            ordinals=[1, 2],
            expected_cell_count=5,
        )
        blob = first_observer.db.execute(
            "SELECT hash, filename, mime, size FROM blobs WHERE hash=?", (digest,)
        ).fetchone()
        assert blob is not None
        assert dict(blob) == {
            "hash": digest,
            "filename": "evidence.txt",
            "mime": "text/plain",
            "size": len(payload),
        }

    writer.append_rows(
        [
            {"ordinal": 3, "subject": "third", "attachment": None},
            {"ordinal": 4, "subject": "fourth", "attachment": None},
        ]
    )

    # Appends are durable bounded transactions, not one import-sized transaction.
    with closing(Project(project.path)) as observer:
        staged = observer.db.execute(
            "SELECT id, hidden FROM sheets ORDER BY id"
        ).fetchall()
        assert len(staged) == 1
        assert staged[0]["hidden"] == 1
        assert observer.db.execute("SELECT COUNT(*) FROM rows").fetchone()[0] == 4
        _assert_committed_staging(
            observer,
            sheet_id=writer.sheet_id,
            ordinals=[1, 2, 3, 4],
            expected_cell_count=9,
        )
        attachment_column = next(
            column
            for column in observer.columns(writer.sheet_id)
            if column["name"] == "attachment"
        )
        assert list(
            observer.get_values(writer.sheet_id, int(attachment_column["id"])).values()
        ) == [None, None, None, None]

    listed = client.get(f"/api/projects/{project_id}/sheets")
    assert listed.status_code == 200, listed.text
    assert listed.json() == []
    direct = client.get(f"/api/projects/{project_id}/sheets/{writer.sheet_id}/data")
    assert direct.status_code == 404, direct.text
    first_row_id = int(
        project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position LIMIT 1",
            (writer.sheet_id,),
        ).fetchone()["id"]
    )
    subject_column_id = int(
        next(
            column
            for column in project.columns(writer.sheet_id)
            if column["name"] == "subject"
        )["id"]
    )
    stats = client.get(
        f"/api/projects/{project_id}/sheets/{writer.sheet_id}"
        f"/columns/{subject_column_id}/stats"
    )
    assert stats.status_code == 404, stats.text
    locate = client.get(
        f"/api/projects/{project_id}/sheets/{writer.sheet_id}"
        f"/rows/{first_row_id}/locate"
    )
    assert locate.status_code == 404, locate.text
    patch = client.patch(
        f"/api/projects/{project_id}/sheets/{writer.sheet_id}",
        json={"title_column_id": subject_column_id},
    )
    assert patch.status_code == 404, patch.text
    assert project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] == 0
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0

    published = writer.publish()
    assert published.sheet_id == writer.sheet_id
    assert published.sheet_name == "Evidence"
    assert published.row_count == 4

    visible = project.sheets()
    assert [(row["id"], row["name"]) for row in visible] == [
        (writer.sheet_id, "Evidence")
    ]
    rows = project.db.execute(
        "SELECT id, position FROM rows WHERE sheet_id=? ORDER BY position",
        (writer.sheet_id,),
    ).fetchall()
    assert [row["position"] for row in rows] == [1, 2, 3, 4]
    columns = {row["name"]: row for row in project.columns(writer.sheet_id)}
    assert columns["subject"]["format"] == "plain_text"
    assert list(
        project.get_values(writer.sheet_id, int(columns["ordinal"]["id"])).values()
    ) == [1, 2, 3, 4]
    assert (
        list(
            project.get_values(
                writer.sheet_id, int(columns["attachment"]["id"])
            ).values()
        )[0]
        == attachment
    )
    assert project.read_blob(digest) == payload


def _publish_with_rows(
    project: Project, *, name: str, count: int
) -> tuple[Any, Any, Receipt]:
    writer = _start(project, name=name)
    for start in range(0, count, 2):
        writer.append_rows(_records(start, min(start + 2, count)))
    result = writer.publish()
    op = project.db.execute("SELECT * FROM ops WHERE id=?", (result.op_id,)).fetchone()
    receipt = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert op is not None
    assert receipt is not None
    return result, op, Receipt.model_validate(json.loads(receipt["body"]))


def test_publish_metadata_is_compact_instead_of_enumerating_rows(
    make_project,
) -> None:
    project = make_project("compact")
    small_result, small_op, small_receipt = _publish_with_rows(
        project, name="Small", count=2
    )
    large_result, large_op, large_receipt = _publish_with_rows(
        project, name="Large", count=80
    )

    for result, op, receipt, name, count in (
        (small_result, small_op, small_receipt, "Small", 2),
        (large_result, large_op, large_receipt, "Large", 80),
    ):
        spec = json.loads(op["spec"])
        undo = json.loads(op["undo_info"])
        receipt_document = receipt.model_dump(mode="json")
        assert op["kind"] == "import.csv"
        assert op["barrier"] == 0
        assert spec["schema_version"] == "frisket.action.v2"
        assert spec["kind"] == "import.csv"
        assert spec["params"]["sheet_name"] == name
        assert spec["params"]["row_count"] == count
        assert spec["params"]["source"] == {
            "kind": "file",
            "logical_path": "evidence.csv",
        }
        assert undo == {"created_sheets": [result.sheet_id]}
        assert not _contains_key(spec, "row_ids")
        assert not _contains_key(undo, "row_ids")
        assert not _contains_key(receipt_document, "row_ids")

        assert receipt.project_id == "streaming-project"
        assert receipt.action_kind == "import.csv"
        assert receipt.idempotency_key == f"streaming:{name}"
        assert receipt.params_hash == f"sha256:caller-{name}"
        assert receipt.action_id == f"act:caller-{name}"
        assert receipt.status == "completed"
        assert receipt.op_ids == [result.op_id]
        source_input = next(item for item in receipt.inputs if item.name == "source")
        assert source_input.ref == {
            "kind": "file",
            "logical_path": "evidence.csv",
        }
        sheet_output = next(
            item
            for item in receipt.outputs
            if item.ref.get("kind") == "materialized_sheet"
        )
        assert sheet_output.name == name
        assert sheet_output.ref == {
            "kind": "materialized_sheet",
            "sheet_id": result.sheet_id,
            "op_id": result.op_id,
            "row_count": count,
            "columns": {
                column["name"]: column["id"]
                for column in project.columns(result.sheet_id)
            },
            "reads": [],
        }
        rows_output = next(
            item for item in receipt.outputs if item.ref.get("kind") == "source_rows"
        )
        assert rows_output.name == "rows"
        assert rows_output.ref == {
            "kind": "source_rows",
            "sheet_id": result.sheet_id,
            "row_count": count,
            "op_id": result.op_id,
        }

    small_spec = str(small_op["spec"])
    large_spec = str(large_op["spec"])
    small_body = json.dumps(small_receipt.model_dump(mode="json"), sort_keys=True)
    large_body = json.dumps(large_receipt.model_dump(mode="json"), sort_keys=True)
    assert len(large_spec) - len(small_spec) < 128
    assert len(large_body) - len(small_body) < 128
    assert len(large_spec) < 2_048
    assert len(large_body) < 4_096

    assert [row["name"] for row in project.sheets()] == ["Small", "Large"]
    assert project.row_count(large_result.sheet_id) == 80
    assert project.undo() == large_result.op_id
    assert [row["name"] for row in project.sheets()] == ["Small"]
    assert project.redo() == large_result.op_id
    assert [row["name"] for row in project.sheets()] == ["Small", "Large"]
    assert project.row_count(large_result.sheet_id) == 80


def test_abort_removes_all_committed_partial_sheet_state(make_project) -> None:
    project = make_project("abort")
    writer = _start(project)
    writer.append_rows(_records(0, 2))
    writer.append_rows(_records(2, 4))
    assert project.db.execute("SELECT COUNT(*) FROM rows").fetchone()[0] == 4

    writer.abort()

    assert _counts(project) == {
        "sheets": 0,
        "columns": 0,
        "rows": 0,
        "cells": 0,
        "ops": 0,
        "receipts": 0,
    }


def test_context_failure_aborts_after_multiple_committed_batches(
    make_project,
) -> None:
    project = make_project("failure")
    with pytest.raises(RuntimeError, match="parser failed"):
        with _start(project) as writer:
            writer.append_rows(_records(0, 2))
            writer.append_rows(_records(2, 4))
            raise RuntimeError("parser failed")

    assert _counts(project) == {
        "sheets": 0,
        "columns": 0,
        "rows": 0,
        "cells": 0,
        "ops": 0,
        "receipts": 0,
    }


def test_receipt_insert_failure_cannot_partially_publish(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = client.post("/api/projects", json={"name": "Failed publication"})
    assert created.status_code == 200, created.text
    project_id = created.json()["id"]
    project = client.app.state.workspace.get(project_id)
    writer = _start(project)
    writer.append_rows(_records(0, 2))
    writer.append_rows(_records(2, 4))

    def fail_receipt_insert(
        self: ReceiptStore, receipt: Receipt, **kwargs: Any
    ) -> None:
        del self, receipt, kwargs
        raise sqlite3.OperationalError("injected receipt insert failure")

    monkeypatch.setattr(ReceiptStore, "insert_completed", fail_receipt_insert)

    with pytest.raises(
        sqlite3.OperationalError, match="injected receipt insert failure"
    ):
        writer.publish()

    assert project.db.in_transaction is False
    assert project.sheets() == []
    assert project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] == 0
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0

    with closing(Project(project.path)) as observer:
        assert observer.sheets() == []
        staged = observer.db.execute(
            "SELECT hidden FROM sheets WHERE id=?", (writer.sheet_id,)
        ).fetchone()
        assert staged is None or staged["hidden"] == 1
        assert observer.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] == 0
        assert observer.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0

    listed = client.get(f"/api/projects/{project_id}/sheets")
    assert listed.status_code == 200, listed.text
    assert listed.json() == []
    direct = client.get(f"/api/projects/{project_id}/sheets/{writer.sheet_id}/data")
    assert direct.status_code == 404, direct.text


def test_publish_refuses_a_late_name_collision_without_renaming(make_project) -> None:
    project = make_project("collision")
    writer = _start(project)
    writer.append_rows(_records(0, 2))
    colliding_sheet_id = project.add_sheet("Evidence")

    with pytest.raises(ValueError, match="already exists"):
        writer.publish()
    writer.abort()

    assert [(row["id"], row["name"]) for row in project.sheets()] == [
        (colliding_sheet_id, "Evidence"),
    ]
    assert project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] == 0
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
    assert project.db.execute("SELECT COUNT(*) FROM rows").fetchone()[0] == 0
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM sheets WHERE id=?", (writer.sheet_id,)
        ).fetchone()[0]
        == 0
    )


def test_duplicate_publication_is_typed_and_cleans_staging(make_project) -> None:
    project = make_project("duplicate")
    first = _start(project, name="First", idempotency_key="same-request")
    first.append_rows(_records(0, 2))
    published = first.publish()

    duplicate = _start(
        project,
        name="Second",
        idempotency_key="same-request",
        params_hash="sha256:caller-second",
        action_id="act:caller-second",
        receipt_id="receipt:caller-second",
    )
    duplicate.append_rows(_records(2, 4))
    with pytest.raises(DuplicateStreamingPublication) as caught:
        duplicate.publish()

    assert caught.value.existing_receipt_id == published.receipt_id
    assert caught.value.idempotency_key == "same-request"
    assert caught.value.params_hash == "sha256:caller-First"
    assert [row["name"] for row in project.sheets()] == ["First"]
    assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 1
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1


def test_completion_requires_enumeration_and_preserves_atomic_state(make_project):
    project = make_project("completion")
    writer = _start_session(project)
    writer.append_page(_records(0, 1), expected_cursor=0, next_cursor=1)
    with pytest.raises(ImportSessionConflict, match="enumeration"):
        writer.finalize_session(expected_cursor=1)
    session = ImportSessionStore(project).get("import:one")
    assert session.state == "active"
    assert ReceiptStore(project).find_by_id(session.receipt_id).status == "running"
    op = project.db.execute(
        "SELECT barrier,publication_closed FROM ops WHERE id=?", (session.op_id,)
    ).fetchone()
    assert tuple(op) == (1, 0)
    writer.finalize_session(expected_cursor=1, enumeration_complete=True)
    assert ImportSessionStore(project).get(session.id).state == "completed"
    assert tuple(
        project.db.execute(
            "SELECT barrier,publication_closed FROM ops WHERE id=?",
            (session.op_id,),
        ).fetchone()
    ) == (0, 1)
    with pytest.raises(ImportSessionConflict):
        ImportSessionStore(project).replace_writer(
            session.id, "another-worker", expected_cursor=1
        )


def test_cancel_requires_explicit_resolution_and_fences_old_writer(make_project):
    project = make_project("cancel")
    writer = _start_session(project)
    writer.append_page(_records(0, 1), expected_cursor=0, next_cursor=1)
    writer.pause(expected_cursor=1)
    writer.cancel(expected_cursor=1)
    session = ImportSessionStore(project).get("import:one")
    assert session.state == "cancelled"
    assert session.committed_rows == 1
    assert ReceiptStore(project).find_by_id(session.receipt_id).status == "running"
    with pytest.raises(ImportSessionConflict):
        writer.append_page(_records(1, 2), expected_cursor=1, next_cursor=2)
    with pytest.raises(ImportSessionConflict):
        writer.finalize_session(expected_cursor=1, enumeration_complete=True)
    writer.finalize_session(expected_cursor=1, keep_cancelled=True)
    assert ImportSessionStore(project).get(session.id).state == "kept"


def test_cancelled_append_remove_preserves_other_changes_and_restores_saved_rows(
    make_project,
):
    from frisket.engine.store.op_log import step_operation

    project = make_project("append-remove")
    sheet = project.add_sheet("Existing")
    column = project.add_column(sheet, "value", "text")
    original = project.add_rows(sheet, [{"value": "original"}], {"value": column})
    writer = StreamingSheetWriter.start_append_session(
        project,
        session_id="append:one",
        writer_authority="worker:one",
        sheet_id=sheet,
        columns=[{"name": "value", "type": "text"}],
        project_id="project",
        action_kind="import.append_rows",
        idempotency_key="append:one",
        params_hash="hash:one",
        action_id="action:one",
        receipt_id="receipt:one",
        source_ref={"kind": "file", "label": "rows.csv"},
    )
    added = writer.append_page(
        [{"value": "imported"}], expected_cursor=0, next_cursor=1
    )
    stale_writer = StreamingSheetWriter._from_session(
        project, ImportSessionStore(project).get("append:one")
    )
    unrelated = project.add_rows(sheet, [{"value": "independent"}], {"value": column})
    writer.cancel(expected_cursor=1)
    writer.remove_session(expected_cursor=1)
    assert project.visible_row_ids(sheet) == original + unrelated
    assert project.get_values(sheet, column, original + unrelated) == {
        original[0]: "original",
        unrelated[0]: "independent",
    }
    project.db.execute("BEGIN IMMEDIATE")
    step_operation(project, "restore", change_id=writer._op_id)
    project.db.commit()
    assert project.visible_row_ids(sheet) == original + added + unrelated
    assert project.get_values(sheet, column, added) == {added[0]: "imported"}
    assert ImportSessionStore(project).get("append:one").state == "removed"
    with pytest.raises(ImportSessionConflict):
        stale_writer.append_page([{"value": "late"}], expected_cursor=1, next_cursor=2)


@pytest.mark.parametrize("append", [False, True])
def test_session_insert_failure_rolls_back_queued_receipt_start(make_project, append):
    project = make_project("Atomic start")
    sheet_id = project.add_sheet("Existing") if append else None
    receipt = Receipt(
        receipt_id="receipt:inventory-one",
        project_id="streaming-project",
        action_id="act:inventory-one",
        action_kind="import.files",
        status="queued",
        idempotency_key="inventory:one",
        params_hash="sha256:inventory-one",
    )
    with write_transaction(project.db):
        ReceiptStore(project).insert_queued(receipt)
    before = _counts(project)
    project.db.execute(
        "CREATE TEMP TRIGGER fail_session BEFORE INSERT ON import_sessions BEGIN SELECT RAISE(ABORT,'session insert failed'); END"
    )
    project.db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="session insert failed"):
        if append:
            StreamingSheetWriter.start_append_session(
                project,
                session_id="import:one",
                writer_authority="claim:one",
                sheet_id=sheet_id,
                columns=COLUMNS,
                project_id=receipt.project_id,
                action_kind=receipt.action_kind,
                idempotency_key=receipt.idempotency_key,
                params_hash=receipt.params_hash,
                action_id=receipt.action_id,
                receipt_id=receipt.receipt_id,
                source_ref={"kind": "inline"},
            )
        else:
            _start_session(project)
    assert _counts(project) == before
    persisted = ReceiptStore(project).find_by_id(receipt.receipt_id)
    assert persisted.status == "queued" and persisted.parsed().op_ids == []
    assert project.db.execute("SELECT COUNT(*) FROM import_sessions").fetchone()[0] == 0
