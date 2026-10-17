from __future__ import annotations

import asyncio
import hashlib
import time
import tracemalloc

import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from frisket.engine.store import Project
from frisket.engine.store.current_value_windows import (
    current_text_hash,
    current_text_metadata,
    read_current_value_window,
)
from frisket.engine.store.evidence import (
    record_evidence_link,
    record_source_artifact,
    record_source_span,
)
from frisket.engine.store.prepared_content import (
    PreparedContentStore,
    PreparedPageDraft,
)
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.value_codec import PreparedContentRef
from frisket.server.app import create_app
from frisket.server.services.project_qa_sources import (
    SCAN_SECONDS,
    read_prepared_passages,
    read_source_text,
)
from helpers import write_claimed_test_results


def _source(tmp_path, text, authority):
    project = Project.create(tmp_path / "source.frisket", name="Source")
    sheet = project.add_sheet("documents")
    column = project.add_column(sheet, "body", type="text")
    [row] = project.add_rows(
        sheet, [{"body": text if authority == "source" else "input"}], {"body": column}
    )
    if authority == "result":
        op = project.append_op("map.extract")
        run = RunResultStore(project).start_run(op, sheet, "map.extract", row_ids=[row])
        write_claimed_test_results(
            project,
            run,
            [{"row_id": row, "column_id": column, "value": text, "outcome": "ok"}],
        )
    elif authority == "edit":
        project.apply_edits([{"row_id": row, "column_id": column, "value": text}])
    return project, (sheet, row, column)


@pytest.mark.parametrize("authority", ["source", "result", "edit"])
def test_twenty_mib_tail_anchor_and_digest_finish_within_ask_budget(
    tmp_path, authority, record_property
):
    text = "a" * (20 * 1024 * 1024) + "😀TAIL-NEEDLE"
    project, cell = _source(tmp_path, text, authority)
    try:
        tracemalloc.start()
        started = time.monotonic()
        opened = read_source_text(project, cell, anchor="TAIL-NEEDLE", limit=50)
        elapsed = time.monotonic() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert "TAIL-NEEDLE" in opened["text"]
        assert elapsed < SCAN_SECONDS
        assert peak < 2 * 1024 * 1024
        record_property("anchor_seconds", elapsed)
        record_property("anchor_python_peak", peak)
        with project.read_snapshot() as snapshot:
            metadata = current_text_metadata(
                snapshot.db, sheet_id=cell[0], row_id=cell[1], column_id=cell[2]
            )
            tracemalloc.start()
            started = time.monotonic()
            digest = current_text_hash(
                snapshot.db,
                sheet_id=cell[0],
                row_id=cell[1],
                column_id=cell[2],
                metadata=metadata,
            )
            elapsed = time.monotonic() - started
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        assert digest == "sha256:" + hashlib.sha256(text.encode()).hexdigest()
        assert elapsed < SCAN_SECONDS
        assert peak < 2 * 1024 * 1024
        record_property("digest_seconds", elapsed)
        record_property("digest_python_peak", peak)
    finally:
        tracemalloc.stop()
        project.close()


def test_twenty_mib_nul_metadata_and_windows_stay_bounded_and_revision_fenced(
    tmp_path, record_property
):
    text = "leading\0" + "a" * (20 * 1024 * 1024) + "😀TAIL-NEEDLE"
    project, cell = _source(tmp_path, text, "source")
    try:
        tracemalloc.start()
        started = time.monotonic()
        first = read_source_text(project, cell, start=len(text) - 20, limit=20)
        elapsed = time.monotonic() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert first["length"] == len(text)
        assert first["text"] == text[-20:]
        assert elapsed < SCAN_SECONDS
        assert peak < 2 * 1024 * 1024
        record_property("nul_read_seconds", elapsed)
        record_property("nul_python_peak", peak)
        anchored = read_source_text(project, cell, anchor="TAIL-NEEDLE", limit=25)
        assert "TAIL-NEEDLE" in anchored["text"]
        metadata = current_text_metadata(
            project.db, sheet_id=cell[0], row_id=cell[1], column_id=cell[2]
        )
        before = metadata.retrieval_revision
        digest = current_text_hash(
            project.db,
            sheet_id=cell[0],
            row_id=cell[1],
            column_id=cell[2],
            metadata=metadata,
        )
        assert digest == "sha256:" + hashlib.sha256(text.encode()).hexdigest()
        # The same coordinate acquires a new revision and must not reuse old sizes/digest.
        project.apply_edits(
            [{"row_id": cell[1], "column_id": cell[2], "value": "new\0😀"}]
        )
        window = read_current_value_window(
            project.db, sheet_id=cell[0], row_id=cell[1], column_id=cell[2]
        )
        assert window.retrieval_revision > before
        assert window.total_characters == 5
        assert window.text == "new\0😀"
    finally:
        tracemalloc.stop()
        project.close()


def test_two_hundred_prepared_pages_keep_verified_ask_quotes_under_budget(
    tmp_path, monkeypatch, record_property
):
    project = Project.create(tmp_path / "prepared.frisket", name="Prepared")
    try:
        sheet = project.add_sheet("documents")
        column = project.add_column(sheet, "body", type="text")
        artifact = record_source_artifact(
            project,
            artifact_kind="document",
            media_type="application/pdf",
            page_count=200,
        )
        op = project.append_op("media.ocr")
        pages = [
            PreparedPageDraft(
                index, "x" * 40_000 + ("😀TAIL-NEEDLE" if index == 200 else "")
            )
            for index in range(1, 201)
        ]
        digest = hashlib.sha256()
        for index, page in enumerate(pages):
            if index:
                digest.update(b"\n\n")
            digest.update(page.text.encode())
        expected_hash = "sha256:" + digest.hexdigest()
        project.db.execute("BEGIN IMMEDIATE")
        ref = PreparedContentStore(project).stage_reference(
            source_artifact_id=artifact["id"], producing_op_id=op, pages=pages
        )
        project.db.commit()
        [row] = project.add_rows(
            sheet, [{"body": PreparedContentRef(ref.ref_id)}], {"body": column}
        )
        cell = (sheet, row, column)
        span = record_source_span(
            project,
            artifact_id=artifact["id"],
            span_kind="page",
            page_start=200,
            page_end=200,
            quote="TAIL-NEEDLE",
            text_layer_hash=expected_hash,
        )
        record_evidence_link(
            project,
            subject_kind="cell_value",
            subject_ref={
                "kind": "source_cell",
                "op_id": None,
                "row_id": row,
                "column_id": column,
                "run_id": None,
                "prepared_ref_id": ref.ref_id,
            },
            spans=[{"span_id": span["id"]}],
            sheet_id=sheet,
            row_id=row,
            column_id=column,
        )
        tracemalloc.start()
        started = time.monotonic()
        opened = read_source_text(project, cell, anchor="TAIL-NEEDLE", limit=50)
        passages = read_prepared_passages(
            project,
            cell,
            expected_version=opened["version"],
            start=opened["range"]["start"],
            end=opened["range"]["end"],
            limit=50,
            span_id=span["stable_id"],
        )
        elapsed = time.monotonic() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert "TAIL-NEEDLE" in opened["text"]
        assert [passage["text"] for passage in passages] == ["TAIL-NEEDLE"]
        assert elapsed < SCAN_SECONDS
        assert peak < 2 * 1024 * 1024
        record_property("prepared_ask_seconds", elapsed)
        record_property("prepared_python_peak", peak)
        import frisket.engine.store.current_value_windows as windows

        monkeypatch.setattr(
            windows,
            "iter_current_value",
            lambda *args, **kwargs: pytest.fail("unchanged digest should be cached"),
        )
        again = read_prepared_passages(
            project,
            cell,
            expected_version=opened["version"],
            start=0,
            end=50,
            limit=50,
            span_id=span["stable_id"],
        )
        assert again == passages
    finally:
        tracemalloc.stop()
        project.close()


@pytest.mark.parametrize("failure", ["start", "body", "disconnect"])
def test_cell_download_releases_snapshot_on_disconnect_even_before_first_yield(
    tmp_path, monkeypatch, failure
):
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "download"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet = project.add_sheet("documents")
    column = project.add_column(sheet, "body")
    [row] = project.add_rows(sheet, [{"body": "x" * (1024 * 1024)}], {"body": column})
    revision = project.db.execute(
        "SELECT retrieval_revision FROM current_cells WHERE row_id=? AND column_id=?",
        (row, column),
    ).fetchone()[0]
    snapshots = []
    original = project.read_snapshot

    def capture():
        snapshot = original()
        snapshots.append(snapshot)
        return snapshot

    monkeypatch.setattr(project, "read_snapshot", capture)
    scope = {
        "type": "http",
        "asgi": {
            "version": "3.0",
            "spec_version": "2.0" if failure == "disconnect" else "2.4",
        },
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/api/projects/{pid}/cells/{row}/{column}/download",
        "raw_path": b"",
        "query_string": f"revision={revision}".encode(),
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 80),
    }

    async def request():
        started = asyncio.Event()
        sent_bodies = 0

        async def send(message):
            nonlocal sent_bodies
            if message["type"] == "http.response.start":
                started.set()
                if failure == "start":
                    raise OSError("client closed before first chunk")
            elif message["type"] == "http.response.body":
                sent_bodies += 1
                if failure == "body":
                    raise OSError("client closed after one chunk")

        async def receive():
            await started.wait()
            return {"type": "http.disconnect"}

        try:
            await client.app(scope, receive, send)
        except (ClientDisconnect, OSError):
            assert failure != "disconnect"
        assert started.is_set()
        if failure == "start":
            assert sent_bodies == 0
        elif failure == "body":
            assert sent_bodies == 1

    asyncio.run(request())
    assert snapshots
    assert all(snapshot._connection is None for snapshot in snapshots)
