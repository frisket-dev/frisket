"""Upload envelopes are bounded before route parsing consumes their bodies."""

import asyncio
import io
import threading

import httpx
import pytest
from fastapi import FastAPI, Request
from pypdf import PdfWriter
from starlette.types import Message

from frisket.server.app import create_app
from frisket.server.import_admission import ImportAdmissionLease
from frisket.server.import_bulk_request_limit import BulkImportRequestLimitMiddleware
from frisket.server.services import import_drafts as import_drafts_service
from frisket.server.services import import_urls as import_urls_service
from frisket.server.services.pdf_packet_splits import PdfPacketSplitService


def _single_page_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    data = io.BytesIO()
    writer.write(data)
    return data.getvalue()


def test_import_admission_lease_releases_when_background_start_fails() -> None:
    releases = 0

    class Permit:
        def release(self) -> None:
            nonlocal releases
            releases += 1

    lease = ImportAdmissionLease(Permit())

    def fail_start(_on_finished):
        raise RuntimeError("registry is closed")

    with pytest.raises(RuntimeError, match="registry is closed"):
        lease.start_background(fail_start)
    lease.release_request()

    assert lease.transferred is True
    assert releases == 1


@pytest.mark.parametrize(
    "suffix",
    [
        "xlsx",
        "xlsx/preview",
        "pdf",
        "files",
        "followthemoney",
        "drafts/paste",
        "csv/update/preview",
        "xlsx/update/preview",
    ],
)
def test_import_envelope_refuses_declared_oversize_before_reading(suffix: str) -> None:
    async def invoke() -> None:
        messages: list[Message] = []

        async def downstream(scope, receive, send) -> None:
            pytest.fail("oversized import reached request parsing")

        async def receive() -> Message:
            pytest.fail("declared oversized body was consumed")

        async def send(message: Message) -> None:
            messages.append(message)

        await BulkImportRequestLimitMiddleware(downstream, max_request_bytes=8)(
            {
                "type": "http",
                "method": "POST",
                "path": f"/api/projects/test/import/{suffix}",
                "headers": [(b"content-length", b"9")],
            },
            receive,
            send,
        )
        assert messages[0]["status"] == 413

    asyncio.run(invoke())


def test_import_envelope_counts_chunks_without_content_length() -> None:
    app = FastAPI()

    @app.post("/api/projects/test/import/drafts/paste")
    async def parse(request: Request):
        return await request.json()

    async def invoke() -> None:
        messages: list[Message] = []
        chunks = iter([b'{"raw":', b'"too much text"}', b"   "])
        reads = 0

        async def receive() -> Message:
            nonlocal reads
            reads += 1
            return {
                "type": "http.request",
                "body": next(chunks),
                "more_body": reads < 3,
            }

        async def send(message: Message) -> None:
            messages.append(message)

        await BulkImportRequestLimitMiddleware(app, max_request_bytes=8)(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/projects/test/import/drafts/paste",
                "headers": [(b"content-type", b"application/json")],
                "query_string": b"",
            },
            receive,
            send,
        )
        assert messages[0]["status"] == 413
        assert reads == 2

    asyncio.run(invoke())


def test_cancelled_native_url_import_holds_permit_until_worker_exits(
    tmp_path, monkeypatch
) -> None:
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_exited = threading.Event()
    permit_released = threading.Event()
    acquisitions = 0

    class Permit:
        def release(self) -> None:
            permit_released.set()

    class Admission:
        def try_acquire(self):
            nonlocal acquisitions
            acquisitions += 1
            return Permit()

    def blocked_run_action(*_args, **_kwargs):
        worker_started.set()
        assert release_worker.wait(5), "test did not release the URL import worker"
        worker_exited.set()
        return object()

    monkeypatch.setattr(import_urls_service, "run_action_spec", blocked_run_action)
    app = create_app(tmp_path / "workspace", import_admission=Admission())
    pid = app.state.workspace.create("Cancelled URL import")["id"]

    async def cancel_while_worker_holds_permit() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            task = asyncio.create_task(
                client.post(
                    f"/api/projects/{pid}/import/urls",
                    json={"urls": ["https://cdn.example/item.mp3"]},
                )
            )
            assert await asyncio.to_thread(worker_started.wait, 5)
            task.cancel()
            for _ in range(4):
                await asyncio.sleep(0)
            try:
                assert not permit_released.is_set()
                assert not task.done()
            finally:
                release_worker.set()
                assert await asyncio.to_thread(worker_exited.wait, 5)
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert permit_released.is_set()

    asyncio.run(cancel_while_worker_holds_permit())
    assert acquisitions == 1


def test_cancelled_native_sync_import_holds_permit_until_worker_exits(
    tmp_path, monkeypatch
) -> None:
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_exited = threading.Event()
    permit_released = threading.Event()

    class Permit:
        def release(self) -> None:
            permit_released.set()

    class Admission:
        def try_acquire(self):
            return Permit()

    def blocked_confirm(*_args, **_kwargs):
        worker_started.set()
        assert release_worker.wait(5), "test did not release the paste import worker"
        worker_exited.set()
        return object()

    monkeypatch.setattr(
        import_drafts_service.ImportDraftService, "confirm", blocked_confirm
    )
    app = create_app(tmp_path / "workspace", import_admission=Admission())
    pid = app.state.workspace.create("Cancelled paste import")["id"]

    async def cancel_while_worker_holds_permit() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            task = asyncio.create_task(
                client.post(
                    f"/api/projects/{pid}/import/drafts/paste/confirm",
                    json={
                        "raw": "id\n1\n",
                        "draft_id": "cancelled-draft",
                        "columns": [
                            {"source_name": "id", "name": "id", "type": "text"}
                        ],
                    },
                )
            )
            assert await asyncio.to_thread(worker_started.wait, 5)
            task.cancel()
            for _ in range(4):
                await asyncio.sleep(0)
            try:
                assert not permit_released.is_set()
                assert not task.done()
            finally:
                release_worker.set()
                assert await asyncio.to_thread(worker_exited.wait, 5)
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert permit_released.is_set()

    asyncio.run(cancel_while_worker_holds_permit())


def test_pdf_packet_prepare_holds_native_import_permit_until_worker_exits(
    tmp_path, monkeypatch
) -> None:
    worker_started = threading.Event()
    release_worker = threading.Event()
    permit_released = threading.Event()
    lock = threading.Lock()
    active = 0

    class Permit:
        def __init__(self) -> None:
            self.released = False

        def release(self) -> None:
            nonlocal active
            with lock:
                if self.released:
                    return
                self.released = True
                active -= 1
                permit_released.set()

    class Admission:
        def try_acquire(self):
            nonlocal active
            with lock:
                if active:
                    return None
                active += 1
            return Permit()

    calls = 0

    def blocked_prepare(self, split_id, *, progress, cancelled):
        nonlocal calls
        calls += 1
        if calls == 1:
            worker_started.set()
            assert release_worker.wait(5), "test did not release packet preparation"
        with self._lock:
            session = self._sessions[split_id]
            session.status = "ready"
        progress(1, 1)

    monkeypatch.setattr(PdfPacketSplitService, "_prepare", blocked_prepare)
    app = create_app(tmp_path / "workspace", import_admission=Admission())
    pid = app.state.workspace.create("Packet permit")["id"]
    raw = _single_page_pdf()

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await client.post(
                f"/api/projects/{pid}/import/pdf-packet-splits",
                files={"file": ("first.pdf", raw, "application/pdf")},
            )
            assert first.status_code == 202, first.text
            assert await asyncio.to_thread(worker_started.wait, 5)

            blocked = await client.post(
                f"/api/projects/{pid}/import/pdf-packet-splits",
                files={"file": ("blocked.pdf", raw, "application/pdf")},
            )
            assert blocked.status_code == 429, blocked.text

            release_worker.set()
            assert await asyncio.to_thread(permit_released.wait, 5)
            resumed = await client.post(
                f"/api/projects/{pid}/import/pdf-packet-splits",
                files={"file": ("resumed.pdf", raw, "application/pdf")},
            )
            assert resumed.status_code == 202, resumed.text

    try:
        asyncio.run(exercise())
    finally:
        release_worker.set()
        app.state.pdf_packet_split_service.shutdown()
        app.state.pdf_packet_job_registry.shutdown()
