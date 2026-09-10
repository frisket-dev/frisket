from __future__ import annotations

import json
import io
import hashlib
from collections.abc import Iterable, Iterator
from email.message import EmailMessage
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.actions.types import EmailInput
from frisket.engine.executor import table_action
from frisket.engine.store import Project
from frisket.engine.store.streaming_import import StreamingSheetWriter


MESSAGE_COUNT = 10_001
SIX_GIB = 6 * 1024 * 1024 * 1024


def _email_import() -> Any:
    return import_module("frisket.ops.email_import")


def _message(subject: str) -> EmailMessage:
    message = EmailMessage()
    message["Date"] = "Tue, 02 Sep 2026 12:34:56 +0000"
    message["From"] = "Reporter <reporter@example.test>"
    message["To"] = "Editor <editor@example.test>"
    message["Subject"] = subject
    message.set_content(f"Body for {subject}")
    return message


class _SequentialMailbox(Iterable[EmailMessage]):
    """A small injected reader that records lazy, one-pass consumption."""

    def __init__(self, count: int) -> None:
        self.count = count
        self.consumed = 0
        self.closed = False

    def __iter__(self) -> Iterator[EmailMessage]:
        for index in range(self.count):
            self.consumed += 1
            yield _message(f"Message {index:05d}")

    def close(self) -> None:
        self.closed = True


def _source(module: Any, path: Path, logical_path: str) -> Any:
    return module.EmailSource(path=path, logical_path=logical_path, format="mbox")


def _message_value(message: Any, field: str) -> Any:
    if isinstance(message, dict):
        return message[field]
    row = getattr(message, "row", None)
    if isinstance(row, dict) and field in row:
        return row[field]
    return getattr(message, "from_" if field == "from" else field)


def _normalized_row(
    index: int, *, attachment: dict[str, str] | None = None
) -> dict[str, Any]:
    return {
        "date": "Tue, 02 Sep 2026 12:34:56 +0000",
        "from": "Reporter <reporter@example.test>",
        "to": ["Editor <editor@example.test>"],
        "cc": [],
        "subject": f"Message {index:05d}",
        "body": f"Body {index}",
        "html": "",
        "attachments": [] if attachment is None else [attachment],
        "source_file": f"exports/archive.mbox#{index + 1}",
    }


def _action(*, key: str) -> dict[str, Any]:
    return {
        "action_id": "import.email",
        "scope": {"kind": "project"},
        "sheet_name": "Emails",
        "params": {
            "sources": [
                {
                    "source_ref": "fixture:archive",
                    "logical_path": "exports/archive.mbox",
                    "format": "mbox",
                }
            ],
        },
        "idempotency_key": key,
    }


def _write_archive(
    path: Path, count: int, *, attachment: bytes, attachment_index: int
) -> None:
    with path.open("wb") as stream:
        for index in range(count):
            message = _message(f"Message {index:05d}")
            if index == attachment_index:
                message.add_attachment(
                    attachment,
                    maintype="application",
                    subtype="octet-stream",
                    filename="evidence.bin",
                )
            stream.write(b"From reporter@example.test Tue Sep 02 12:34:56 2026\n")
            stream.write(message.as_bytes())
            stream.write(b"\n")


def _deps(stream) -> ExecutorDeps:
    return ExecutorDeps(
        email_sources={
            "fixture:archive": EmailInput(
                logical_path="exports/archive.mbox", format="mbox", stream=stream
            )
        }
    )


def _visible_counts(project: Project) -> dict[str, int]:
    return {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("sheets", "columns", "rows", "cells", "blobs", "ops", "receipts")
    }


def _assert_hidden_stage(project: Project, *, rows: int) -> None:
    assert project.sheets() == []
    assert len(project.sheets(include_hidden=True)) == 1
    assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 1
    assert project.db.execute("SELECT COUNT(*) FROM rows").fetchone()[0] == rows
    assert project.db.execute("SELECT COUNT(*) FROM cells").fetchone()[0] == rows * 9
    assert project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] == 0
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_mbox_iterator_accepts_10001_messages_and_preserves_order(
    tmp_path: Path,
) -> None:
    module = _email_import()
    source_path = tmp_path / "archive.mbox"
    source_path.touch()
    mailbox_reader = _SequentialMailbox(MESSAGE_COUNT)
    opens: list[Path] = []

    def open_mbox(path: str | Path, *args: Any, **kwargs: Any) -> _SequentialMailbox:
        del args, kwargs
        opens.append(Path(path))
        return mailbox_reader

    messages = module.iter_email_messages(
        [_source(module, source_path, "exports/archive.mbox")],
        open_mbox=open_mbox,
        attachment_dir=tmp_path / "attachments",
    )

    assert mailbox_reader.consumed == 0
    first = next(messages)
    assert mailbox_reader.consumed == 1
    remaining = list(messages)

    assert opens == [source_path]
    assert len(remaining) + 1 == MESSAGE_COUNT
    assert _message_value(first, "subject") == "Message 00000"
    assert _message_value(remaining[-1], "subject") == "Message 10000"
    assert _message_value(first, "source_file") == "exports/archive.mbox#1"
    assert _message_value(remaining[-1], "source_file") == "exports/archive.mbox#10001"
    assert mailbox_reader.closed


def test_sparse_mbox_over_six_gib_has_no_source_wide_size_refusal(
    tmp_path: Path,
) -> None:
    module = _email_import()
    source_path = tmp_path / "large-archive.mbox"
    with source_path.open("wb") as stream:
        stream.truncate(SIX_GIB + 1)
    mailbox_reader = _SequentialMailbox(1)
    opened = False

    def open_mbox(path: str | Path, *args: Any, **kwargs: Any) -> _SequentialMailbox:
        nonlocal opened
        del args, kwargs
        assert Path(path) == source_path
        opened = True
        return mailbox_reader

    messages = module.iter_email_messages(
        [_source(module, source_path, "exports/large-archive.mbox")],
        open_mbox=open_mbox,
        attachment_dir=tmp_path / "attachments",
    )

    assert [_message_value(item, "subject") for item in messages] == ["Message 00000"]
    assert opened
    assert source_path.stat().st_size > SIX_GIB


def test_message_batches_are_bounded_and_ordered() -> None:
    module = _email_import()
    produced = 0

    def rows() -> Iterator[dict[str, Any]]:
        nonlocal produced
        for index in range(5):
            produced += 1
            yield _normalized_row(index)

    batches = module.iter_email_batches(rows(), batch_size=2)
    assert produced == 0
    first = next(batches)
    assert produced == 2
    assert [row["subject"] for row in first] == ["Message 00000", "Message 00001"]
    second = next(batches)
    assert produced == 4
    assert [row["subject"] for row in second] == ["Message 00002", "Message 00003"]
    last = next(batches)
    assert produced == 5
    assert [row["subject"] for row in last] == ["Message 00004"]
    with pytest.raises(StopIteration):
        next(batches)


def test_large_email_import_stages_bounded_batches_then_publishes_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "archive.mbox"
    attachment_bytes = b"exact attachment bytes\x00\xff"
    _write_archive(
        source_path,
        MESSAGE_COUNT,
        attachment=attachment_bytes,
        attachment_index=MESSAGE_COUNT - 1,
    )
    project = Project.create(tmp_path / "large.frisket", name="Large email import")
    checkpoints: list[int] = []

    original_append = StreamingSheetWriter.append_rows

    def append_rows(self, rows):
        assert 0 < len(rows) <= 2
        row_ids = original_append(self, rows)
        if self.row_count in {2, MESSAGE_COUNT}:
            _assert_hidden_stage(project, rows=self.row_count)
            assert project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 0
            checkpoints.append(self.row_count)
        return row_ids

    monkeypatch.setattr(table_action, "_TABLE_BATCH_SIZE", 2)
    monkeypatch.setattr(StreamingSheetWriter, "append_rows", append_rows)
    try:
        with source_path.open("rb") as stream:
            result = run_action_spec(
                project,
                _action(key="import_email@sha256:large"),
                project_id="project-large-email",
                deps=_deps(stream),
            )
            assert not stream.closed

        assert result.status == "completed", result.errors
        assert checkpoints == [2, MESSAGE_COUNT]
        sheet = project.db.execute(
            "SELECT id FROM sheets WHERE name='Emails'"
        ).fetchone()
        assert sheet is not None
        sheet_id = int(sheet["id"])
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM rows WHERE sheet_id=?", (sheet_id,)
            ).fetchone()[0]
            == MESSAGE_COUNT
        )
        subject_column = project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='subject'", (sheet_id,)
        ).fetchone()
        assert subject_column is not None
        subjects = list(
            project.get_values(sheet_id, int(subject_column["id"])).values()
        )
        assert subjects[0] == "Message 00000"
        assert subjects[-1] == "Message 10000"

        blob = project.db.execute(
            "SELECT hash, filename, mime, size FROM blobs WHERE filename='evidence.bin'"
        ).fetchone()
        assert blob is not None
        assert blob["size"] == len(attachment_bytes)
        with project.blob_store.materialize(blob["hash"]) as stored:
            assert stored.read_bytes() == attachment_bytes

        op = project.db.execute("SELECT undo_info FROM ops").fetchone()
        receipt = project.db.execute("SELECT body FROM receipts").fetchone()
        assert op is not None and receipt is not None
        assert len(op["undo_info"]) < 8_192
        assert len(receipt["body"]) < 32_768
        assert "Message 00000" not in op["undo_info"]
        assert "Message 10000" not in receipt["body"]
        assert json.loads(receipt["body"])["status"] == "completed"
    finally:
        project.close()


def test_many_eml_sources_keep_compact_identity_and_full_replay_authority(
    tmp_path: Path,
) -> None:
    sources = [
        {
            "source_ref": f"fixture:{index}",
            "logical_path": f"archive/{'long-folder-' * 60}/{index:03d}.eml",
            "format": "eml",
        }
        for index in reversed(range(40))
    ]
    inputs = {
        source["source_ref"]: EmailInput(
            logical_path=source["logical_path"],
            format="eml",
            stream=io.BytesIO(_message(source["source_ref"]).as_bytes()),
        )
        for source in sources
    }
    action = _action(key="import_email@sha256:many-eml")
    action["params"]["sources"] = sources
    project = Project.create(tmp_path / "many.frisket", name="Many email sources")
    try:
        first = run_action_spec(
            project,
            action,
            project_id="many-email",
            deps=ExecutorDeps(email_sources=inputs),
        )
        assert first.status == "completed", first.errors
        sheet_id = first.outputs[0].sheet_id
        columns = first.outputs[0].ref["columns"]
        assert project.row_count(sheet_id) == 40
        assert list(project.get_values(sheet_id, columns["subject"]).values()) == [
            f"fixture:{index}" for index in range(40)
        ]
        assert list(project.get_values(sheet_id, columns["source_file"]).values()) == [
            source["logical_path"] for source in reversed(sources)
        ]
        assert (
            list(project.get_values(sheet_id, columns["attachments"]).values())
            == [[]] * 40
        )
        assert all(not item.stream.closed for item in inputs.values())

        op_raw = project.db.execute("SELECT spec FROM ops").fetchone()[0]
        receipt_raw = project.db.execute("SELECT body FROM receipts").fetchone()[0]
        op, receipt = json.loads(op_raw), json.loads(receipt_raw)
        assert "sources" not in op["params"]
        assert op["import_row_count"] == 40
        assert op["params_hash"] == receipt["params_hash"]
        assert op["params_hash"]
        admission = {
            "kind": "email_source_admission",
            "source_count": 40,
            "descriptors_sha256": hashlib.sha256(
                json.dumps(sources, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
        assert op["reads"] == [admission]
        assert [
            item["ref"]
            for item in receipt["inputs"]
            if item["ref"]["kind"] == "email_source_admission"
        ] == [admission]
        assert len(op_raw) < 4096
        assert len(receipt_raw) < 16_384
        assert all(
            source["logical_path"] not in op_raw + receipt_raw for source in sources
        )

        before = _visible_counts(project)
        for item in inputs.values():
            item.stream.close()
        replay = run_action_spec(project, action, project_id="many-email")
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert replay.op_ids == first.op_ids
        changed = {
            **action,
            "params": {"sources": [dict(source) for source in sources]},
        }
        changed["params"]["sources"][0]["logical_path"] += ".changed"
        conflict = run_action_spec(project, changed, project_id="many-email")
        assert conflict.status == "failed"
        assert conflict.errors[0].code == "idempotency_conflict"
        assert _visible_counts(project) == before
    finally:
        for item in inputs.values():
            item.stream.close()
        project.close()


def test_email_import_failure_after_staged_batches_publishes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "archive.mbox"
    _write_archive(
        source_path, 6, attachment=b"staged before failure", attachment_index=0
    )
    project = Project.create(tmp_path / "failed.frisket", name="Failed email import")
    reached_late_failure = False
    empty = {
        table: 0
        for table in ("sheets", "columns", "rows", "cells", "blobs", "ops", "receipts")
    }

    checkpoints = []
    original_append = StreamingSheetWriter.append_rows

    def append_rows(self, rows):
        assert len(rows) == 2
        row_ids = original_append(self, rows)
        _assert_hidden_stage(project, rows=self.row_count)
        checkpoints.append(self.row_count)
        return row_ids

    class FailingStream(io.BytesIO):
        def readline(self, size=-1):
            nonlocal reached_late_failure
            if project.db.execute("SELECT COUNT(*) FROM rows").fetchone()[0] >= 4:
                reached_late_failure = True
                raise OSError("simulated source read failure")
            return super().readline(size)

    stream = FailingStream(source_path.read_bytes())
    monkeypatch.setattr(table_action, "_TABLE_BATCH_SIZE", 2)
    monkeypatch.setattr(StreamingSheetWriter, "append_rows", append_rows)
    try:
        result = run_action_spec(
            project,
            _action(key="import_email@sha256:failed-stream"),
            project_id="project-failed-email",
            deps=_deps(stream),
        )

        assert result.status == "failed"
        assert reached_late_failure
        assert checkpoints == [2, 4]
        assert not stream.closed
        assert _visible_counts(project) == empty
    finally:
        stream.close()
        project.close()
