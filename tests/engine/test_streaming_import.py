from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.contracts.action import Receipt
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.streaming_import import (
    DuplicateStreamingPublication,
    StreamingSheetWriter,
)
from frisket.server.app import create_app


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
    assert (
        list(project.get_values(sheet_id, int(columns["ordinal"]["id"])).values())
        == ordinals
    )
    subjects = list(
        project.get_values(sheet_id, int(columns["subject"]["id"])).values()
    )
    assert subjects[0] in {"first", "message 0"}
    assert subjects[-1] in {"second", "fourth", f"message {ordinals[-1]}"}


def _contains_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def test_staging_producer_links_admitted_receipt_before_publish(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "receipt-linked.frisket", name="receipt-linked")
    try:
        writer = _start(project, receipt_id="receipt:admitted")

        producer = project.db.execute(
            "SELECT stage_id,op_id FROM base_cell_producers"
        ).fetchone()
        assert producer is not None
        assert producer["op_id"] is None
        assert "receipt:admitted" in producer["stage_id"]
        assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0

        writer.abort()
    finally:
        project.close()


def test_committed_batches_stay_hidden_until_atomic_publish(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
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
    first_observer = Project(project.path)
    try:
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
    finally:
        first_observer.close()

    writer.append_rows(
        [
            {"ordinal": 3, "subject": "third", "attachment": None},
            {"ordinal": 4, "subject": "fourth", "attachment": None},
        ]
    )

    # Appends are durable bounded transactions, not one import-sized transaction.
    observer = Project(project.path)
    try:
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
        ) == [attachment, None, None, None]
    finally:
        observer.close()

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
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "compact.frisket", name="compact")
    try:
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
            source_input = next(
                item for item in receipt.inputs if item.name == "source"
            )
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
                item
                for item in receipt.outputs
                if item.ref.get("kind") == "source_rows"
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
    finally:
        project.close()


def test_abort_removes_all_committed_partial_sheet_state(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "abort.frisket", name="abort")
    try:
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
    finally:
        project.close()


def test_context_failure_aborts_after_multiple_committed_batches(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "failure.frisket", name="failure")
    try:
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
    finally:
        project.close()


def test_receipt_insert_failure_cannot_partially_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(create_app(tmp_path / "publish-failure-workspace"))
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

    observer = Project(project.path)
    try:
        assert observer.sheets() == []
        staged = observer.db.execute(
            "SELECT hidden FROM sheets WHERE id=?", (writer.sheet_id,)
        ).fetchone()
        assert staged is None or staged["hidden"] == 1
        assert observer.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] == 0
        assert observer.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
    finally:
        observer.close()

    listed = client.get(f"/api/projects/{project_id}/sheets")
    assert listed.status_code == 200, listed.text
    assert listed.json() == []
    direct = client.get(f"/api/projects/{project_id}/sheets/{writer.sheet_id}/data")
    assert direct.status_code == 404, direct.text


def test_publish_refuses_a_late_name_collision_without_renaming(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "collision.frisket", name="collision")
    try:
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
    finally:
        project.close()


def test_duplicate_publication_is_typed_and_cleans_staging(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "duplicate.frisket", name="duplicate")
    try:
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
    finally:
        project.close()
