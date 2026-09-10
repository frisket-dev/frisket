"""Committed streamed imports return their receipt without a second database read."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pytest

from frisket.engine.executor import run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.streaming_import import StreamingSheetWriter
from tests.engine.test_import_csv_typed_boundaries import _action, _counts


def test_postcommit_receipt_read_failure_cannot_report_uncommitted_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "source.csv"
    path.write_text("value\n1\n2\n", encoding="utf-8")
    request = _action(path)
    publications = []
    publish = StreamingSheetWriter.publish
    find_by_id = ReceiptStore.find_by_id

    def capture_publication(writer, **kwargs):
        publication = publish(writer, **kwargs)
        publications.append(publication)
        return publication

    def fail_postcommit_read(store, receipt_id):
        if any(item.receipt_id == receipt_id for item in publications):
            raise sqlite3.OperationalError("injected post-commit receipt read failure")
        return find_by_id(store, receipt_id)

    monkeypatch.setattr(StreamingSheetWriter, "publish", capture_publication)
    monkeypatch.setattr(ReceiptStore, "find_by_id", fail_postcommit_read)

    with closing(Project.create(tmp_path / "import.frisket", name="Import")) as project:
        result = run_action_spec(project, request, project_id="p")
        assert result.status == "completed", result.errors
        assert result.errors == []
        assert len(publications) == 1
        publication = publications[0]
        assert result.receipt_id == publication.receipt_id
        assert result.op_ids == [publication.op_id]
        assert result.outputs[0].sheet_id == publication.sheet_id
        assert project.row_count(publication.sheet_id) == 2
        assert (
            project.db.execute(
                "SELECT hidden FROM sheets WHERE id=?", (publication.sheet_id,)
            ).fetchone()[0]
            == 0
        )
        stored = project.db.execute(
            "SELECT status,body FROM receipts WHERE id=?", (publication.receipt_id,)
        ).fetchone()
        assert stored["status"] == "completed"
        assert publication.receipt.model_dump(mode="json") == json.loads(stored["body"])
        before = _counts(project)

        # Replay uses its existing idempotency lookup, never republishes, and
        # does not need the original source or the failed read-by-id operation.
        path.unlink()
        replay = run_action_spec(project, request, project_id="p")
        assert replay == result
        assert _counts(project) == before
        assert len(publications) == 1
