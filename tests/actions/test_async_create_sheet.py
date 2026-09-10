"""Async table producers use the existing owner-thread materialization lifecycle."""

import asyncio
import io
import threading
from contextlib import closing
from contextvars import ContextVar
from pathlib import Path

import pytest
from pydantic import BaseModel

from frisket.actions.core import ActionCategory, RegisteredAction, action, create_sheet
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    DynamicOutput,
    DynamicTableResult,
    ImportBlobStager,
    LocalFileReader,
    PdfDocument,
    PdfPageRenderer,
    TableColumn,
    TableError,
    TableResult,
    TableRow,
)
from frisket.engine.executor import ExecutorDeps
from frisket.engine.executor.table_action import run_typed_create_sheet_action
from frisket.engine.executor.table_preview import preview_table
from frisket.engine.store import Project


class Params(ActionParams):
    text: str = "hello"


class Output(BaseModel):
    text: str


def bound(handler):
    registered = RegisteredAction(
        "test.async_table",
        action(
            name="async_table",
            title="Table",
            description="Table",
            category=ActionCategory.CONVERT,
            run=create_sheet(handler),
        ),
    )
    return BoundTypedActionRequest.bind(
        registered,
        ActionRequest(
            action_id=registered.action_id,
            scope={"kind": "project"},
            params={},
            sheet_name="Output",
            idempotency_key="async-table",
        ),
    )


@pytest.mark.parametrize("async_rows", [False, True])
def test_fast_table_rows_do_not_poll_database_per_row(
    tmp_path, monkeypatch, async_rows
):
    from frisket.engine.executor import table_producer

    monkeypatch.setattr(table_producer, "monotonic", lambda: 0.0)
    checks = []

    async def asynchronous_rows():
        for index in range(1000):
            yield TableRow(output=Output(text=str(index)))

    def produce(params: Params) -> TableResult[Output]:
        return TableResult(
            rows=asynchronous_rows()
            if async_rows
            else (TableRow(output=Output(text=str(index))) for index in range(1000))
        )

    with closing(Project.create(tmp_path / "project")) as project:

        def cancelled():
            project.db.execute("SELECT 1").fetchone()
            checks.append(True)
            return False

        result = run_typed_create_sheet_action(
            project, "p", bound(produce), deps=ExecutorDeps(cancelled=cancelled)
        )
        assert result.status == "completed", result.errors
        assert 2 <= len(checks) <= 4
        assert project.db.execute("SELECT COUNT(*) FROM rows").fetchone()[0] == 1000


@pytest.mark.parametrize("async_rows", [False, True])
@pytest.mark.parametrize("cancel_after", [123, 1000])
def test_polled_table_cancellation_aborts_before_publication(
    tmp_path, monkeypatch, async_rows, cancel_after
):
    from frisket.engine.executor import table_producer

    produced, closed = [], []
    # Mid-stream cancellation advances the clock; end-of-stream cancellation
    # must still be seen when the entire stream fits inside one poll interval.
    monkeypatch.setattr(
        table_producer,
        "monotonic",
        lambda: len(produced) / 1000 if cancel_after == 123 else 0.0,
    )

    def rows():
        try:
            for index in range(1000):
                produced.append(index)
                yield TableRow(output=Output(text=str(index)))
        finally:
            closed.append(True)

    async def asynchronous_rows():
        with closing(rows()) as values:
            for value in values:
                yield value

    def produce(params: Params) -> TableResult[Output]:
        return TableResult(rows=asynchronous_rows() if async_rows else rows())

    with closing(Project.create(tmp_path / "project")) as project:
        result = run_typed_create_sheet_action(
            project,
            "p",
            bound(produce),
            deps=ExecutorDeps(cancelled=lambda: len(produced) >= cancel_after),
        )
        assert result.status == "cancelled", result.errors
        assert result.errors[0].code == "action_cancelled"
        assert closed == [True]
        assert cancel_after <= len(produced) <= (175 if cancel_after == 123 else 1000)
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0
        assert project.db.execute("SELECT COUNT(*) FROM rows").fetchone()[0] == 0


def test_async_table_nested_loop_refusal_is_typed_and_closes_coroutine():
    import inspect

    from frisket.engine.executor.table_producer import TableProducer

    async def produce():
        return None

    async def nested():
        value = produce()
        with closing(TableProducer()) as producer:
            with pytest.raises(TableError) as caught:
                producer.resolve(value)
            assert caught.value.code == "async_table_requires_worker"
            assert inspect.getcoroutinestate(value) == inspect.CORO_CLOSED

    asyncio.run(nested())


@pytest.mark.parametrize("async_rows", [False, True])
def test_table_rows_recheck_cancellation_at_entry_even_after_fresh_poll(
    monkeypatch, async_rows
):
    from frisket.engine.executor import table_producer

    monkeypatch.setattr(table_producer, "monotonic", lambda: 0.0)
    cancelled = False

    class SyncRows:
        def __iter__(self):
            pytest.fail("cancelled stream must not be opened")

    class AsyncRows:
        def __aiter__(self):
            pytest.fail("cancelled stream must not be opened")

    with closing(table_producer.TableProducer(cancelled=lambda: cancelled)) as producer:
        producer.check_cancelled()
        cancelled = True
        with pytest.raises(TableError) as caught:
            next(producer.rows(AsyncRows() if async_rows else SyncRows()))
        assert caught.value.code == "action_cancelled"


@pytest.mark.parametrize("async_producer", [False, True])
@pytest.mark.parametrize("async_rows", [False, True])
def test_async_table_uses_one_loop_owner_thread_and_replays(
    tmp_path, async_producer, async_rows
):
    owner = threading.get_ident()
    loops, calls, closed = [], [], []
    with closing(Project.create(tmp_path / "project")) as project:

        def observed():
            assert threading.get_ident() == owner
            assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] <= 1
            assert not project.db.in_transaction
            loops.append(asyncio.get_running_loop())

        async def rows():
            try:
                for index in range(502):
                    observed()
                    await asyncio.sleep(0)
                    yield TableRow(output=Output(text=str(index)))
            finally:
                observed()
                await asyncio.sleep(0)
                closed.append(True)

        def produce(params: Params) -> TableResult[Output]:
            calls.append(True)
            return TableResult(
                rows=rows()
                if async_rows
                else [TableRow(output=Output(text=params.text))]
            )

        async def async_produce(params: Params) -> TableResult[Output]:
            observed()
            await asyncio.sleep(0)
            return produce(params)

        request = bound(async_produce if async_producer else produce)
        result = run_typed_create_sheet_action(project, "p", request)
        assert result.status == "completed", result.errors
        assert calls == [True]
        assert closed == ([True] if async_rows else [])
        assert len(set(loops)) == int(async_producer or async_rows)
        assert all(loop.is_closed() for loop in loops)
        assert run_typed_create_sheet_action(project, "p", request) == result
        assert calls == [True]


def test_dynamic_async_schema_is_available_without_consuming_extra_preview_row(
    tmp_path,
):
    events = []

    async def produce(params: Params) -> DynamicTableResult:
        await asyncio.sleep(0)

        async def rows():
            try:
                for index in range(25):
                    events.append(index)
                    yield TableRow(output=DynamicOutput({"word": str(index)}))
            finally:
                await asyncio.sleep(0)
                events.append("closed")

        return DynamicTableResult(schema=[TableColumn("word", "text")], rows=rows())

    with closing(Project.create(tmp_path / "project")) as project:
        before = tuple(project.db.iterdump())
        result = preview_table(
            project,
            "p",
            bound(produce),
            deps=ExecutorDeps(),
            progress=lambda *_: None,
            cancelled=lambda: False,
        )
        try:
            assert len(result.rows) == 20 and result.total is None
            assert events == [*range(20), "closed"]
            assert result.columns[0].name == "word"
            assert tuple(project.db.iterdump()) == before
        finally:
            result.close()


@pytest.mark.parametrize("waiting", ["producer", "rows"])
def test_cancellation_while_awaiting_closes_before_reader_release(
    tmp_path, monkeypatch, waiting
):
    from frisket.engine.executor.local_file_read import AdmittedLocalFileReader

    events = []
    owner = threading.get_ident()
    original_close = AdmittedLocalFileReader.close

    def close(reader):
        events.append("reader_closed")
        original_close(reader)

    monkeypatch.setattr(AdmittedLocalFileReader, "close", close)

    async def wait():
        try:
            events.append("waiting")
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            events.append("await_closed")

    async def produce(params: Params, reader: LocalFileReader) -> TableResult[Output]:
        if waiting == "producer":
            await wait()

        async def rows():
            await wait()
            yield TableRow(output=Output(text=params.text))

        return TableResult(rows=rows())

    with closing(Project.create(tmp_path / "project")) as project:

        def cancelled():
            assert threading.get_ident() == owner
            project.db.execute("SELECT 1").fetchone()
            return "waiting" in events

        result = run_typed_create_sheet_action(
            project, "p", bound(produce), deps=ExecutorDeps(cancelled=cancelled)
        )
        assert result.status == "cancelled"
        assert result.errors[0].code == "action_cancelled"
        assert events == ["waiting", "await_closed", "reader_closed"]
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0


def test_late_async_stream_failure_aborts_hidden_batches(tmp_path):
    closed = []

    async def produce(params: Params) -> TableResult[Output]:
        async def rows():
            try:
                for index in range(502):
                    yield TableRow(output=Output(text=str(index)))
                raise ValueError("late failure")
            finally:
                await asyncio.sleep(0)
                closed.append(True)

        return TableResult(rows=rows())

    with closing(Project.create(tmp_path / "project")) as project:
        result = run_typed_create_sheet_action(project, "p", bound(produce))
        assert result.status == "failed"
        assert closed == [True]
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0


def test_unsolicited_child_cancelled_error_still_propagates(tmp_path):
    async def produce(params: Params) -> TableResult[Output]:
        raise asyncio.CancelledError()

    with closing(Project.create(tmp_path / "project")) as project:
        with pytest.raises(asyncio.CancelledError):
            run_typed_create_sheet_action(project, "p", bound(produce))


def test_cancellation_at_producer_return_still_closes_owned_rows(tmp_path):
    events = []

    class Rows:
        def __aiter__(self):
            pytest.fail("cancelled result must not open rows")

        async def aclose(self):
            await asyncio.sleep(0)
            events.append("closed")

    async def produce(params: Params) -> TableResult[Output]:
        events.append("returned")
        return TableResult(rows=Rows())

    with closing(Project.create(tmp_path / "project")) as project:
        result = run_typed_create_sheet_action(
            project,
            "p",
            bound(produce),
            deps=ExecutorDeps(cancelled=lambda: "returned" in events),
        )
        assert result.status == "cancelled"
        assert events == ["returned", "closed"]


def test_async_generator_context_survives_pulls_and_preview_close(tmp_path):
    context = ContextVar("table-test", default="outside")
    tasks, closed = [], []

    async def produce(params: Params) -> TableResult[Output]:
        tasks.append(asyncio.current_task())

        async def rows():
            token = context.set("inside")
            try:
                for index in range(25):
                    tasks.append(asyncio.current_task())
                    assert context.get() == "inside"
                    yield TableRow(output=Output(text=str(index)))
            finally:
                tasks.append(asyncio.current_task())
                context.reset(token)
                closed.append(True)

        return TableResult(rows=rows())

    with closing(Project.create(tmp_path / "project")) as project:
        result = preview_table(
            project,
            "p",
            bound(produce),
            deps=ExecutorDeps(),
            progress=lambda *_: None,
            cancelled=lambda: False,
        )
        result.close()
    assert closed == [True]
    assert len(set(tasks)) == 1
    assert context.get() == "outside"


@pytest.mark.parametrize("cancel", [False, True])
def test_async_pdf_capability_bridges_only_renderer_and_joins_before_cleanup(
    tmp_path, monkeypatch, cancel
):
    from frisket.engine.executor import pdf_page_read
    from frisket.engine.executor.import_blob_stage import AdmittedImportBlobStager
    from frisket.engine.sandbox.shim import SandboxResult

    owner = threading.get_ident()
    entered, stopped = threading.Event(), threading.Event()
    paths = []
    stage = AdmittedImportBlobStager.stage

    def owned_stage(self, *args, **kwargs):
        assert threading.get_ident() == owner
        return stage(self, *args, **kwargs)

    async def render(command, *, should_cancel, **kwargs):
        assert threading.get_ident() != owner
        paths.append(Path(command[-1]).parent)
        entered.set()
        if cancel:
            while not should_cancel():
                await asyncio.sleep(0.01)
        else:
            Path(f"{command[-1]}-1.png").write_bytes(b"image")
        stopped.set()
        return SandboxResult(0, "", "", cancelled=cancel)

    monkeypatch.setattr(AdmittedImportBlobStager, "stage", owned_stage)
    monkeypatch.setattr(pdf_page_read.shutil, "which", lambda _: "/tools/pdftoppm")
    monkeypatch.setattr(pdf_page_read, "run_sandboxed", render)

    async def produce(
        params: Params, blobs: ImportBlobStager, pdfs: PdfPageRenderer
    ) -> DynamicTableResult:
        document = blobs.stage(
            io.BytesIO(b"pdf"),
            filename="input.pdf",
            mime="application/pdf",
            role=PdfDocument(),
        )
        images = pdfs.render(document, dpi=150)
        return DynamicTableResult(
            schema=[TableColumn("image", "image")],
            rows=[TableRow(output=DynamicOutput({"image": images[1]}))],
        )

    with closing(Project.create(tmp_path / "project")) as project:
        before = tuple(project.db.iterdump())

        def cancelled():
            assert threading.get_ident() == owner
            project.db.execute("SELECT 1").fetchone()
            return cancel and entered.is_set()

        def preview():
            return preview_table(
                project,
                "p",
                bound(produce),
                deps=ExecutorDeps(),
                progress=lambda *_: None,
                cancelled=cancelled,
            )

        if cancel:
            with pytest.raises(TableError, match="cancelled"):
                preview()
        else:
            result = preview()
            try:
                assert len(result.rows) == len(result.artifacts) == 1
            finally:
                result.close()
        assert stopped.is_set()
        assert all(not path.exists() for path in paths)
        assert tuple(project.db.iterdump()) == before


@pytest.mark.parametrize("fatal", [False, True])
def test_cancelled_async_generator_cleanup_failure_is_not_lost(tmp_path, fatal):
    from frisket.engine.sandbox.shim import SandboxTeardownError

    entered, closed = [], []
    failure = (
        SandboxTeardownError("unproved teardown")
        if fatal
        else RuntimeError("cleanup failed")
    )

    async def produce(params: Params) -> TableResult[Output]:
        async def rows():
            try:
                entered.append(True)
                await asyncio.Event().wait()
                yield TableRow(output=Output(text="never"))
            finally:
                closed.append(True)
                raise failure

        return TableResult(rows=rows())

    with closing(Project.create(tmp_path / "project")) as project:

        def run():
            return run_typed_create_sheet_action(
                project,
                "p",
                bound(produce),
                deps=ExecutorDeps(cancelled=lambda: bool(entered)),
            )

        if fatal:
            with pytest.raises(SandboxTeardownError) as caught:
                run()
            assert caught.value is failure
        else:
            result = run()
            assert result.status == "failed"
            assert result.errors[0].code == "project_write_failed"
        assert closed == [True]
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0


@pytest.mark.parametrize("waiting", ["producer", "rows"])
def test_suppressed_cancellation_closes_returned_resource_in_original_context(
    tmp_path, waiting
):
    context = ContextVar("cancelled-table", default="outside")
    entered, closed, tasks = [], [], []

    class Rows:
        def __aiter__(self):
            pytest.fail("cancelled producer must not open rows")

        async def aclose(self):
            assert context.get() == "inside"
            tasks.append(asyncio.current_task())
            context.reset(self.token)
            closed.append(True)

    async def produce(params: Params) -> TableResult[Output]:
        if waiting == "producer":
            rows = Rows()
            rows.token = context.set("inside")
            tasks.append(asyncio.current_task())
            entered.append(True)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return TableResult(rows=rows)

        async def rows():
            token = context.set("inside")
            tasks.append(asyncio.current_task())
            try:
                entered.append(True)
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    yield TableRow(output=Output(text="not published"))
            finally:
                assert context.get() == "inside"
                tasks.append(asyncio.current_task())
                context.reset(token)
                closed.append(True)

        return TableResult(rows=rows())

    with closing(Project.create(tmp_path / "project")) as project:
        result = run_typed_create_sheet_action(
            project,
            "p",
            bound(produce),
            deps=ExecutorDeps(cancelled=lambda: bool(entered)),
        )
        assert result.status == "cancelled"
        assert closed == [True]
        assert len(set(tasks)) == 1
        assert context.get() == "outside"
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0


@pytest.mark.parametrize("fatal", [False, True])
def test_renderer_teardown_failure_wins_over_owner_cancellation(fatal):
    from frisket.engine.sandbox.media_sync import run_media_sync
    from frisket.engine.sandbox.shim import SandboxTeardownError

    stopped = threading.Event()
    failure = SandboxTeardownError("renderer remains alive")
    cancellation = asyncio.CancelledError("owner cancelled")

    async def render(should_cancel):
        while not should_cancel():
            await asyncio.sleep(0)
        stopped.set()
        if fatal:
            raise failure

    def cancelled():
        raise cancellation

    async def invoke():
        run_media_sync(render, cancelled=cancelled)

    with pytest.raises(
        SandboxTeardownError if fatal else asyncio.CancelledError
    ) as caught:
        asyncio.run(invoke())
    assert caught.value is (failure if fatal else cancellation)
    assert stopped.is_set()
