from __future__ import annotations

import hashlib
import io
import mailbox
import tempfile
import threading
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from typing import Any

import pytest

from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.executor import table_action
from frisket.actions import import_email as email_action
from frisket.actions.types import EmailInput
from frisket.engine.executor.import_blob_stage import AdmittedImportBlobStager
from frisket.engine.store import Project
from frisket.ops import email_import


EMAIL_COLUMNS = [
    "date",
    "from",
    "to",
    "cc",
    "subject",
    "body",
    "html",
    "attachments",
    "source_file",
]


def _message_bytes(
    subject: str,
    *,
    plain: str = "message body",
    html: str | None = None,
    attachment: tuple[bytes, str, str] | None = None,
) -> bytes:
    message = EmailMessage()
    message["Date"] = "Tue, 02 Sep 2026 12:34:56 +0000"
    message["From"] = "Reporter <reporter@example.test>"
    message["To"] = "Editor <editor@example.test>"
    message["Cc"] = "Desk <desk@example.test>"
    message["Subject"] = subject
    message.set_content(plain)
    if html is not None:
        message.add_alternative(html, subtype="html")
    if attachment is not None:
        payload, mime, filename = attachment
        maintype, subtype = mime.split("/", 1)
        message.add_attachment(
            payload,
            maintype=maintype,
            subtype=subtype,
            filename=filename,
        )
    return message.as_bytes()


def _write(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    return path


def _write_mbox(path: Path, messages: list[bytes]) -> Path:
    box = mailbox.mbox(path, create=True)
    try:
        for payload in messages:
            parsed = BytesParser(policy=default).parsebytes(payload)
            box.add(mailbox.mboxMessage(parsed))
        box.flush()
    finally:
        box.close()
    return path


def _import_email_action(
    sources: list[tuple[Path, str, str]],
    *,
    key: str = "import_email@sha256:stable",
    sheet_name: str = "Emails",
) -> dict[str, Any]:
    return {
        "action_id": "import.email",
        "scope": {"kind": "project"},
        "sheet_name": sheet_name,
        "params": {
            "sources": [
                {
                    "source_ref": f"fixture:{index}",
                    "logical_path": logical_path,
                    "format": format,
                }
                for index, (_path, logical_path, format) in enumerate(sources)
            ],
        },
        "idempotency_key": key,
    }


def _trusted_deps(sources: list[tuple[Path, str, str]]) -> ExecutorDeps:
    return ExecutorDeps(
        email_sources={
            f"fixture:{index}": EmailInput(
                logical_path=logical_path,
                format=format,
                stream=io.BytesIO(path.read_bytes()),
            )
            for index, (path, logical_path, format) in enumerate(sources)
        }
    )


def _columns(project: Project, sheet_id: int) -> dict[str, Any]:
    return {
        row["name"]: row
        for row in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=? ORDER BY position",
            (sheet_id,),
        ).fetchall()
    }


def _values(project: Project, sheet_id: int, column: Any) -> list[Any]:
    return list(project.get_values(sheet_id, int(column["id"])).values())


def _visible_counts(project: Project) -> dict[str, int]:
    return {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("sheets", "columns", "rows", "cells", "blobs", "ops", "receipts")
    }


def test_import_email_uses_explicit_names_refuses_collision_and_replays(tmp_path):
    source = _write(tmp_path / "mail.eml", _message_bytes("Message"))
    sources = [(source, "mail.eml", "eml")]
    project = Project.create(tmp_path / "email.frisket", name="Email")
    try:
        results = [
            run_action_spec(
                project,
                _import_email_action(
                    sources,
                    key=f"email-{index}",
                    sheet_name="Emails" if index == 0 else "Emails-2",
                ),
                project_id="p",
                deps=_trusted_deps(sources),
            )
            for index in range(2)
        ]
        assert [result.status for result in results] == ["completed", "completed"]
        assert [
            row["name"]
            for row in project.db.execute("SELECT name FROM sheets ORDER BY id")
        ] == ["Emails", "Emails-2"]
        before = _visible_counts(project)
        collision = run_action_spec(
            project,
            _import_email_action(sources, key="email-collision"),
            project_id="p",
            deps=_trusted_deps(sources),
        )
        assert collision.status == "failed"
        assert collision.errors[0].code == "duplicate_sheet_name"
        assert _visible_counts(project) == before
        source.unlink()
        replay = run_action_spec(
            project,
            _import_email_action(sources, key="email-1", sheet_name="Emails-2"),
            project_id="p",
        )
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == results[1].receipt_id
        assert replay.outputs[0].name == results[1].outputs[0].name == "Emails-2"
        assert replay.outputs[0].sheet_id == results[1].outputs[0].sheet_id
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 2
    finally:
        project.close()


def test_import_email_publishes_one_sorted_sheet_and_canonical_attachment(
    tmp_path: Path,
) -> None:
    attachment = b"%PDF-1.4\ncanonical invoice bytes\n"
    last = _write(
        tmp_path / "last.eml",
        _message_bytes(
            "Last",
            plain="last plain",
            html="<p>last html</p>",
            attachment=(attachment, "application/pdf", "invoice.pdf"),
        ),
    )
    mbox_path = _write_mbox(
        tmp_path / "mailbox.mbox",
        [
            _message_bytes("Mbox one", plain="one"),
            _message_bytes("Mbox two", plain="two"),
        ],
    )
    first = _write(
        tmp_path / "first.eml",
        _message_bytes("First", plain="first"),
    )
    bad = _write(tmp_path / "bad.eml", b"\x00\xffnot an email")
    action = _import_email_action(
        [
            (last, "z/last.eml", "eml"),
            (mbox_path, "m/mailbox.mbox", "mbox"),
            (first, "a/first.eml", "eml"),
            (bad, "bad/bad.eml", "eml"),
        ]
    )
    project = Project.create(tmp_path / "email.frisket", name="Email import")
    try:
        result = run_action_spec(
            project,
            action,
            project_id="project-email",
            deps=_trusted_deps(
                [
                    (last, "z/last.eml", "eml"),
                    (mbox_path, "m/mailbox.mbox", "mbox"),
                    (first, "a/first.eml", "eml"),
                    (bad, "bad/bad.eml", "eml"),
                ]
            ),
        )

        assert result.status == "completed", result.errors
        assert len(result.op_ids) == 1
        assert len(result.warnings) == 1
        assert "bad/bad.eml" in result.warnings[0]

        sheets = project.db.execute(
            "SELECT * FROM sheets WHERE name='Emails' ORDER BY id"
        ).fetchall()
        assert len(sheets) == 1
        sheet_id = int(sheets[0]["id"])
        columns = _columns(project, sheet_id)
        assert list(columns) == EMAIL_COLUMNS
        assert columns["body"]["type"] == "text"
        assert columns["body"]["format"] == "plain_text"
        assert columns["html"]["type"] == "text"
        assert columns["html"]["format"] == "plain_text"
        assert _values(project, sheet_id, columns["subject"]) == [
            "First",
            "Mbox one",
            "Mbox two",
            "Last",
        ]
        assert _values(project, sheet_id, columns["source_file"]) == [
            "a/first.eml",
            "m/mailbox.mbox#1",
            "m/mailbox.mbox#2",
            "z/last.eml",
        ]

        attachment_cells = _values(project, sheet_id, columns["attachments"])
        assert attachment_cells[:3] == [[], [], []]
        digest = hashlib.sha256(attachment).hexdigest()
        assert attachment_cells[3] == [
            {
                "blob": digest,
                "mime": "application/pdf",
                "filename": "invoice.pdf",
            }
        ]
        blob = project.db.execute(
            "SELECT hash, filename, mime, size FROM blobs WHERE hash=?",
            (digest,),
        ).fetchone()
        assert dict(blob) == {
            "hash": digest,
            "filename": "invoice.pdf",
            "mime": "application/pdf",
            "size": len(attachment),
        }
        with project.blob_store.materialize(digest) as stored:
            assert stored.read_bytes() == attachment
        assert _visible_counts(project) == {
            "sheets": 1,
            "columns": 9,
            "rows": 4,
            "cells": 36,
            "blobs": 1,
            "ops": 1,
            "receipts": 1,
        }
    finally:
        project.close()


def test_import_email_all_invalid_fails_without_an_empty_sheet(tmp_path: Path) -> None:
    bad = _write(tmp_path / "bad.eml", b"\x00\xffnot an email")
    project = Project.create(tmp_path / "invalid.frisket", name="Invalid email")
    try:
        result = run_action_spec(
            project,
            _import_email_action([(bad, "bad.eml", "eml")]),
            project_id="project-invalid-email",
            deps=_trusted_deps([(bad, "bad.eml", "eml")]),
        )

        assert result.status == "failed"
        assert result.errors[0].code == "email_parse_failed"
        assert _visible_counts(project) == {
            "sheets": 0,
            "columns": 0,
            "rows": 0,
            "cells": 0,
            "blobs": 0,
            "ops": 0,
            "receipts": 0,
        }
    finally:
        project.close()


def test_import_email_never_opens_an_opaque_ref_without_trusted_ingress(
    tmp_path: Path,
) -> None:
    sensitive = _write(tmp_path / "host-secret.eml", _message_bytes("Do not read"))
    action = {
        "action_id": "import.email",
        "scope": {"kind": "project"},
        "sheet_name": "Emails",
        "params": {
            "sources": [
                {
                    "source_ref": str(sensitive),
                    "logical_path": "claimed.eml",
                    "format": "eml",
                }
            ],
        },
        "idempotency_key": "import_email@sha256:opaque-ref",
    }
    project = Project.create(tmp_path / "opaque.frisket", name="Opaque email")
    try:
        result = run_action_spec(project, action, project_id="project-opaque-email")
        assert result.status == "failed"
        assert result.errors[0].code == "email_sources_unavailable"
        assert sensitive.read_bytes() == _message_bytes("Do not read")
        assert _visible_counts(project) == {
            "sheets": 0,
            "columns": 0,
            "rows": 0,
            "cells": 0,
            "blobs": 0,
            "ops": 0,
            "receipts": 0,
        }
    finally:
        project.close()


def test_import_email_replays_before_opening_a_missing_staged_source(
    tmp_path: Path,
) -> None:
    source = _write(tmp_path / "message.eml", _message_bytes("Recorded"))
    action = _import_email_action([(source, "message.eml", "eml")])
    project = Project.create(tmp_path / "replay.frisket", name="Email replay")
    try:
        first = run_action_spec(
            project,
            action,
            project_id="project-email-replay",
            deps=_trusted_deps([(source, "message.eml", "eml")]),
        )
        assert first.status == "completed", first.errors
        source.unlink()

        replay = run_action_spec(project, action, project_id="project-email-replay")
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert replay.action.action_id == first.action.action_id
    finally:
        project.close()


def test_import_email_storage_failure_rolls_back_all_visible_database_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _write(
        tmp_path / "first.eml",
        _message_bytes(
            "First",
            attachment=(b"first attachment", "text/plain", "first.txt"),
        ),
    )
    second = _write(
        tmp_path / "second.eml",
        _message_bytes(
            "Second",
            attachment=(b"second attachment", "text/plain", "second.txt"),
        ),
    )
    project = Project.create(tmp_path / "rollback.frisket", name="Atomic email")
    original_put_path = project.blob_store.put_path
    calls = 0

    def fail_second_put_path(
        path: str | Path,
        *,
        expected_digest: str | None = None,
    ) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated attachment storage failure")
        return original_put_path(path, expected_digest=expected_digest)

    monkeypatch.setattr(project.blob_store, "put_path", fail_second_put_path)
    try:
        result = run_action_spec(
            project,
            _import_email_action(
                [
                    (first, "first.eml", "eml"),
                    (second, "second.eml", "eml"),
                ],
                key="import_email@sha256:storage-failure",
            ),
            project_id="project-atomic-email",
            deps=_trusted_deps(
                [(first, "first.eml", "eml"), (second, "second.eml", "eml")]
            ),
        )

        assert calls == 2
        assert result.status == "failed"
        assert result.errors[0].code == "project_write_failed"
        # Blob metadata rolls back atomically. Canonical bytes may be shared
        # with a winner and are left for reference-aware object cleanup.
        assert _visible_counts(project) == {
            "sheets": 0,
            "columns": 0,
            "rows": 0,
            "cells": 0,
            "blobs": 0,
            "ops": 0,
            "receipts": 0,
        }
        assert (
            project.db.execute("SELECT COUNT(*) FROM sheets WHERE hidden=1").fetchone()[
                0
            ]
            == 0
        )
    finally:
        project.close()


def test_email_temp_cleanup_failure_aborts_hidden_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(
        tmp_path / "cleanup.eml",
        _message_bytes(
            "Cleanup",
            attachment=(b"cleanup attachment", "text/plain", "cleanup.txt"),
        ),
    )
    real_temporary_directory = tempfile.TemporaryDirectory
    cleanup_observed = []

    class _FailingTemporaryDirectory:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._inner = real_temporary_directory(*args, **kwargs)

        def __enter__(self) -> str:
            return self._inner.__enter__()

        def __exit__(self, *args: Any) -> bool:
            counts = _visible_counts(project)
            assert counts["rows"] == 1
            assert counts["blobs"] == counts["ops"] == counts["receipts"] == 0
            cleanup_observed.append(True)
            self._inner.cleanup()
            raise OSError("simulated attachment scratch cleanup failure")

    monkeypatch.setattr(email_action, "TemporaryDirectory", _FailingTemporaryDirectory)
    monkeypatch.setattr(table_action, "_TABLE_BATCH_SIZE", 1)
    project = Project.create(tmp_path / "cleanup.frisket", name="Cleanup failure")
    try:
        result = run_action_spec(
            project,
            _import_email_action([(source, "cleanup.eml", "eml")]),
            project_id="project-email-cleanup",
            deps=_trusted_deps([(source, "cleanup.eml", "eml")]),
        )
        assert result.status == "failed"
        assert cleanup_observed == [True]
        assert result.errors[0].code == "project_write_failed"
        counts = _visible_counts(project)
        assert counts["sheets"] == counts["columns"] == counts["rows"] == 0
        assert counts["cells"] == counts["ops"] == counts["receipts"] == 0
        assert counts["blobs"] == 0
        assert (
            project.db.execute("SELECT COUNT(*) FROM sheets WHERE hidden=1").fetchone()[
                0
            ]
            == 0
        )
    finally:
        project.close()


def test_email_attachment_scratch_is_empty_before_the_next_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_mbox(
        tmp_path / "source.mbox",
        [
            _message_bytes(
                "first", attachment=(b"fixture attachment", "text/plain", "fixture.txt")
            ),
            _message_bytes("second"),
        ],
    )
    original = email_import.iter_email_messages
    checked = []

    def messages(sources, *, attachment_dir, warnings):
        parsed = original(sources, attachment_dir=attachment_dir, warnings=warnings)
        try:
            for message in parsed:
                yield message
                assert not any(attachment_dir.iterdir())
                checked.append(message.subject)
        finally:
            parsed.close()

    monkeypatch.setattr(email_import, "iter_email_messages", messages)

    project = Project.create(tmp_path / "scratch.frisket", name="Scratch batches")
    try:
        result = run_action_spec(
            project,
            _import_email_action([(source, "source.mbox", "mbox")]),
            project_id="project-email-scratch",
            deps=_trusted_deps([(source, "source.mbox", "mbox")]),
        )
        assert result.status == "completed", result.errors
        assert checked == ["first", "second"]
        assert source.exists()
    finally:
        project.close()


def test_concurrent_same_key_same_attachment_keeps_winner_blob_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = b"shared attachment"
    source = _write(
        tmp_path / "shared.eml",
        _message_bytes("Shared", attachment=(attachment, "text/plain", "shared.txt")),
    )
    action = _import_email_action(
        [(source, "shared.eml", "eml")], key="import_email@sha256:concurrent"
    )
    start = threading.Barrier(2)
    original_stage = AdmittedImportBlobStager.stage

    def concurrent_stage(self, *args, **kwargs):
        staged = original_stage(self, *args, **kwargs)
        start.wait(timeout=10)
        return staged

    monkeypatch.setattr(AdmittedImportBlobStager, "stage", concurrent_stage)

    project = Project.create(tmp_path / "concurrent.frisket", name="Concurrent")
    results: list[Any] = []
    failures: list[BaseException] = []

    def run() -> None:
        try:
            results.append(
                run_action_spec(
                    project,
                    action,
                    project_id="project-email-concurrent",
                    deps=_trusted_deps([(source, "shared.eml", "eml")]),
                )
            )
        except BaseException as exc:  # pragma: no cover - assertion below reports it
            failures.append(exc)

    # realtime: Barrier-rendezvoused concurrency is under test; joins are liveness bounds.
    threads = [threading.Thread(target=run) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert not any(thread.is_alive() for thread in threads)
        assert not failures
        assert len(results) == 2
        assert all(result.status == "completed" for result in results)
        assert {result.receipt_id for result in results} == {results[0].receipt_id}

        sheet = project.db.execute("SELECT id FROM sheets WHERE hidden=0").fetchone()
        assert sheet is not None
        column = project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='attachments'",
            (sheet["id"],),
        ).fetchone()
        assert column is not None
        assert _values(project, int(sheet["id"]), column) == [
            [
                {
                    "blob": hashlib.sha256(attachment).hexdigest(),
                    "mime": "text/plain",
                    "filename": "shared.txt",
                }
            ]
        ]
        assert project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 1
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 1
        assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
        with project.materialize_blob(hashlib.sha256(attachment).hexdigest()) as stored:
            assert stored.read_bytes() == attachment
    finally:
        project.close()
