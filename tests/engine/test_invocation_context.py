from __future__ import annotations

import asyncio
import threading
from dataclasses import replace

import pytest
from pydantic import BaseModel

from frisket.actions.core import (
    ActionCategory,
    RegisteredAction,
    action,
    map_batch,
    map_rows,
)
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    ColumnRef,
    DynamicOutput,
    InvocationContext,
    MediaMetadataReader,
    Row,
    RowResult,
    Rows,
    SheetRows,
)
from frisket.actions.classify_types import Classifier
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.extract import RegexExtractParams
from frisket.ai.llm import ModelRouter
from frisket.engine.executor.invocation_context import HostInvocationContext
from frisket.engine.executor.map_rows_action import (
    build_typed_map_rows_plan,
    run_typed_map_rows_action,
)
from frisket.engine.runner import MapRunner
from frisket.engine.store import Project
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.ops.base import OpContext
from frisket.plugins.sdk import Plugin
from frisket.sdk import InvocationContext as ExportedContext
from runner_test_helpers import run_with_output_claim


class Params(ActionParams):
    source: ColumnRef[str]


class Output(BaseModel):
    value: str


def _bound(sheet, handler, *, batch=False):
    registered = RegisteredAction(
        "map.context_test",
        action(
            name="context_test",
            title="Context test",
            description="Exercise invocation controls.",
            category=ActionCategory.TEXT,
            run=(map_batch if batch else map_rows)(handler),
        ),
    )
    request = ActionRequest(
        action_id=registered.action_id,
        scope=SheetRows(sheet_id=sheet),
        params={"source": "source"},
        idempotency_key="context-test",
    )
    return BoundTypedActionRequest.bind(registered, request)


@pytest.fixture
def project(tmp_path):
    project = Project.create(tmp_path / "context.frisket")
    try:
        sheet = project.add_sheet("Input")
        column = project.add_column(sheet, "source")
        project.add_rows(sheet, [{"source": "one"}], {"source": column})
        yield project, sheet
    finally:
        project.close()


def test_context_is_exported_and_has_no_invocation_data_or_effects():
    assert ExportedContext is InvocationContext
    context = HostInvocationContext(None)
    context.check_cancelled()
    assert not hasattr(context, "__dict__")
    for name in ("project", "row", "run_id", "preview", "extras", "credentials"):
        assert not hasattr(context, name)


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("stop", [None, "durable", "internal"])
def test_context_normal_run_and_cancellation_use_shared_lifecycle(project, batch, stop):
    store, sheet = project
    event = threading.Event()
    observed_progress = []

    def request_stop():
        if stop == "durable":
            event.set()
        elif stop == "internal":
            # The host's on_progress observer may stop work independently of
            # durable operator intent, just like the failure circuit breaker.
            observed_progress[-1].cancel()

    def row(params: Params, row: Row, context: InvocationContext) -> RowResult[Output]:
        request_stop()
        context.check_cancelled()
        return RowResult(output=Output(value=params.source.read(row)))

    def rows(
        params: Params, rows: Rows, context: InvocationContext
    ) -> dict[int, RowResult[Output]]:
        request_stop()
        context.check_cancelled()
        return {
            key: RowResult(output=Output(value=params.source.read(value)))
            for key, value in rows.items()
        }

    bound = _bound(sheet, rows if batch else row, batch=batch)

    def factory(project, router):
        return MapRunner(
            project,
            router or ModelRouter(cache=None, cache_mode="off"),
            authority=UnroutedOnlyAuthority(project),
            should_cancel=lambda _run_id: event.is_set(),
            on_progress=observed_progress.append,
        )

    result = run_typed_map_rows_action(store, "p", bound, None, factory)
    assert result.status == ("cancelled" if stop else "completed"), result
    assert observed_progress[-1].cancel_requested is (stop == "durable")
    assert store.db.execute("SELECT status FROM runs").fetchone()[0] == result.status
    assert (
        store.db.execute("SELECT status FROM receipts").fetchone()[0] == result.status
    )
    assert store.db.execute("SELECT count(*) FROM results").fetchone()[0] == (
        0 if stop else 1
    )
    assert (
        store.db.execute(
            "SELECT count(*) FROM output_column_claims WHERE status='active'"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize("batch", [False, True])
def test_context_preview_cancellation_is_not_a_row_failure(project, batch):
    store, sheet = project
    event = threading.Event()

    def row(params: Params, row: Row, context: InvocationContext) -> RowResult[Output]:
        event.set()
        context.check_cancelled()
        raise AssertionError("cancelled handler continued")

    def rows(
        params: Params, rows: Rows, context: InvocationContext
    ) -> dict[int, RowResult[Output]]:
        event.set()
        context.check_cancelled()
        raise AssertionError("cancelled handler continued")

    plan = build_typed_map_rows_plan(
        store, _bound(sheet, rows if batch else row, batch=batch)
    )
    runner = MapRunner(
        store,
        ModelRouter(cache=None, cache_mode="off"),
        authority=UnroutedOnlyAuthority(store),
    )
    spec = plan.spec_dict()
    spec["row_ids"] = store.visible_row_ids(sheet)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner.preview(spec, program=plan.program, cancel_event=event))
    for table in ("runs", "results", "receipts"):
        assert store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_context_keeps_injection_order_but_adds_no_capability_grants(project):
    store, sheet = project

    def with_context(
        params: Params,
        row: Row,
        media: MediaMetadataReader,
        context: InvocationContext,
        classifier: Classifier,
    ) -> RowResult[Output]:
        assert media is first
        assert classifier is second
        context.check_cancelled()
        return RowResult(output=Output(value="ordered"))

    def without_context(
        params: Params, row: Row, media: MediaMetadataReader, classifier: Classifier
    ) -> RowResult[Output]:
        return RowResult(output=Output(value="unused"))

    bound = _bound(sheet, with_context)
    terminal = bound.action.definition.run
    assert terminal.injections == (MediaMetadataReader, InvocationContext, Classifier)
    assert terminal.capabilities == (MediaMetadataReader, Classifier)
    plain = _bound(sheet, without_context)
    for key in ("required_capabilities", "side_effects", "writes_project"):
        assert bound.action.catalog_entry()[key] == plain.action.catalog_entry()[key]
    plan = build_typed_map_rows_plan(store, bound)
    first, second = object(), object()
    arguments = plan.program._handler_arguments(
        [first, second], OpContext(project=store)
    )
    assert terminal.handler(bound.params, Row({}), *arguments).output.value == "ordered"


def test_contexts_do_not_share_cancellation_state():
    stopped, running = threading.Event(), threading.Event()
    first = HostInvocationContext(stopped.is_set)
    second = HostInvocationContext(running.is_set)
    stopped.set()
    with pytest.raises(asyncio.CancelledError):
        first.check_cancelled()
    second.check_cancelled()


@pytest.mark.parametrize("batch", [False, True])
def test_spontaneous_child_cancellation_without_host_stop_still_escapes(project, batch):
    store, sheet = project
    observed_progress = []

    def row(params: Params, row: Row, context: InvocationContext) -> RowResult[Output]:
        context.check_cancelled()
        assert not observed_progress[-1].cancelled
        raise asyncio.CancelledError("unsolicited child cancellation")

    def rows(
        params: Params, rows: Rows, context: InvocationContext
    ) -> dict[int, RowResult[Output]]:
        context.check_cancelled()
        assert not observed_progress[-1].cancelled
        raise asyncio.CancelledError("unsolicited child cancellation")

    plan = build_typed_map_rows_plan(
        store, _bound(sheet, rows if batch else row, batch=batch)
    )
    runner = MapRunner(
        store,
        ModelRouter(cache=None, cache_mode="off"),
        authority=UnroutedOnlyAuthority(store),
        should_cancel=lambda _run: False,
        on_progress=observed_progress.append,
    )
    runner.allow_action_lifecycle_only_recipes = True
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            run_with_output_claim(runner, plan.spec_dict(), program=plan.program)
        )
    assert not observed_progress[-1].cancel_requested
    assert store.db.execute("SELECT status FROM runs").fetchone()[0] == "cancelled"
    assert store.db.execute("SELECT count(*) FROM results").fetchone()[0] == 0


def test_circuit_breaker_stops_an_inflight_context_and_settles_receipt(project):
    store, sheet = project
    column = next(
        column["id"] for column in store.columns(sheet) if column["name"] == "source"
    )
    store.add_rows(sheet, [{"source": "two"}], {"source": column})
    second_started, breaker_stopped = asyncio.Event(), asyncio.Event()
    observed_progress = []

    async def row(
        params: Params, row: Row, context: InvocationContext
    ) -> RowResult[Output]:
        if params.source.read(row) == "one":
            await asyncio.wait_for(second_started.wait(), timeout=2)
            raise ValueError("first row fails and trips the circuit breaker")
        second_started.set()
        await asyncio.wait_for(breaker_stopped.wait(), timeout=2)
        context.check_cancelled()
        raise AssertionError("stopped sibling continued")

    def observe(progress):
        observed_progress.append(progress)
        if progress.failure_streak == 1 and progress.cancelled:
            breaker_stopped.set()

    def factory(project, router):
        return MapRunner(
            project,
            router or ModelRouter(cache=None, cache_mode="off"),
            authority=UnroutedOnlyAuthority(project),
            concurrency=2,
            halt_after_consecutive_failures=1,
            should_cancel=lambda _run: False,
            on_progress=observe,
        )

    result = run_typed_map_rows_action(store, "p", _bound(sheet, row), None, factory)
    assert second_started.is_set() and breaker_stopped.is_set()
    assert result.status == "cancelled", result
    assert observed_progress[-1].failure_streak == 1
    assert not observed_progress[-1].cancel_requested
    assert store.db.execute("SELECT status FROM runs").fetchone()[0] == "cancelled"
    assert store.db.execute("SELECT status FROM receipts").fetchone()[0] == "cancelled"
    assert store.db.execute("SELECT count(*) FROM results").fetchone()[0] == 1
    assert store.db.execute("SELECT error FROM results").fetchone()[0]
    assert (
        store.db.execute(
            "SELECT count(*) FROM output_column_claims WHERE status='active'"
        ).fetchone()[0]
        == 0
    )


def test_duplicate_context_rejected_and_plugins_share_native_context():
    def duplicate(
        params: Params, row: Row, first: InvocationContext, second: InvocationContext
    ) -> RowResult[Output]:
        raise AssertionError("must not execute")

    with pytest.raises(TypeError, match="must not be repeated"):
        map_rows(duplicate)

    def handler(
        params: Params, row: Row, context: InvocationContext
    ) -> RowResult[Output]:
        raise AssertionError("must not execute")

    bound = _bound(1, handler)
    registered = Plugin(id="test-plugin", actions=(bound.action.definition,)).actions[0]
    assert registered.definition is bound.action.definition
    assert registered.definition.run.injections == (InvocationContext,)


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("durable_intent", [False, True])
def test_true_outer_cancellation_still_escapes_with_concurrent_intent(
    project, batch, durable_intent
):
    store, sheet = project
    intent = threading.Event()

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def wait(context):
            try:
                context.check_cancelled()
                started.set()
                await release.wait()
            finally:
                if durable_intent:
                    # Latch the host's durable flag while teardown is joined.
                    # This must not replace the outer task's cancellation.
                    context.check_cancelled()

        async def row(
            params: Params, row: Row, context: InvocationContext
        ) -> RowResult[Output]:
            await wait(context)
            raise AssertionError("cancelled handler continued")

        async def rows(
            params: Params, rows: Rows, context: InvocationContext
        ) -> dict[int, RowResult[Output]]:
            await wait(context)
            raise AssertionError("cancelled handler continued")

        plan = build_typed_map_rows_plan(
            store, _bound(sheet, rows if batch else row, batch=batch)
        )
        runner = MapRunner(
            store,
            ModelRouter(cache=None, cache_mode="off"),
            authority=UnroutedOnlyAuthority(store),
            should_cancel=lambda _run: intent.is_set(),
        )
        runner.allow_action_lifecycle_only_recipes = True
        task = asyncio.create_task(
            run_with_output_claim(runner, plan.spec_dict(), program=plan.program)
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        if durable_intent:
            intent.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert store.db.execute("SELECT status FROM runs").fetchone()[0] == "cancelled"
    assert store.db.execute("SELECT count(*) FROM results").fetchone()[0] == 0


@pytest.mark.parametrize("batch", [False, True])
def test_queued_context_cancellation_finalizes_receipt_attempt_and_claims(
    tmp_path, monkeypatch, batch
):
    from frisket.engine.jobs.worker import Worker
    from frisket.engine.store.runs import RunResultStore
    from frisket.execution.attempt import TERMINAL_ATTEMPT_STATES
    from tests.engine.test_typed_project_run_queue import _client

    with _client(tmp_path) as client:
        project_id = client.post("/api/projects", json={"name": "Context"}).json()["id"]
        workspace = client.app.state.workspace
        project = workspace.get(project_id)
        sheet = project.add_sheet("Source")
        column = project.add_column(sheet, "source")
        project.add_rows(sheet, [{"source": "one"}], {"source": column})
        observed = []

        def stop(context):
            assert RunResultStore(project).request_cancel(queued["run_id"])
            observed.append(True)
            context.check_cancelled()
            raise AssertionError("cancelled handler continued")

        def row(
            params: RegexExtractParams, row: Row, context: InvocationContext
        ) -> RowResult[DynamicOutput]:
            stop(context)

        def rows(
            params: RegexExtractParams, rows: Rows, context: InvocationContext
        ) -> dict[int, RowResult[DynamicOutput]]:
            stop(context)

        original = ACTION_REGISTRY.get("map.regex_extract")
        registered = replace(
            original,
            definition=replace(
                original.definition,
                run=(map_batch if batch else map_rows)(
                    rows if batch else row,
                    dynamic_outputs=original.definition.run.dynamic_outputs,
                ),
            ),
        )
        monkeypatch.setattr(
            ACTION_REGISTRY,
            "_actions",
            {**ACTION_REGISTRY._actions, original.action_id: registered},
        )
        body = {
            "action_id": "map.regex_extract",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"input_columns": ["source"], "pattern": "one"},
            "idempotency_key": "context-queue",
        }
        response = client.post(f"/api/projects/{project_id}/actions/v1/run", json=body)
        assert response.status_code == 200, response.text
        queued = response.json()
        assert queued["status"] == "queued", queued
        assert Worker(
            workspace.queue, workspace.registry, worker_id="context"
        ).run_once()
        job = workspace.queue.get(queued["job_id"])
        assert observed == [True]
        assert job.status == "done", job
        assert job.result["status"] == "cancelled", job.result
        assert job.result["action_result"]["status"] == "cancelled", job.result
        assert (
            project.db.execute(
                "SELECT status FROM receipts WHERE id=?", (queued["receipt_id"],)
            ).fetchone()[0]
            == "cancelled"
        )
        assert (
            project.db.execute(
                "SELECT status FROM runs WHERE id=?", (queued["run_id"],)
            ).fetchone()[0]
            == "cancelled"
        )
        assert (
            project.db.execute(
                "SELECT count(*) FROM output_column_claims WHERE status='active'"
            ).fetchone()[0]
            == 0
        )
        states = project.db.execute(
            "SELECT state FROM execution_attempts WHERE run_id=?", (queued["run_id"],)
        ).fetchall()
        assert states and all(state[0] in TERMINAL_ATTEMPT_STATES for state in states)
        assert project.db.execute("SELECT count(*) FROM results").fetchone()[0] == 0


def test_preview_job_context_stop_is_cancelled_without_publication(
    tmp_path, monkeypatch
):
    from tests.engine.test_typed_project_run_queue import _client
    from tests.deterministic_time import controlled_time

    entered = threading.Event()
    with _client(tmp_path) as client:
        project_id = client.post(
            "/api/projects", json={"name": "Preview context"}
        ).json()["id"]
        project = client.app.state.workspace.get(project_id)
        sheet = project.add_sheet("Source")
        column = project.add_column(sheet, "source")
        project.add_rows(sheet, [{"source": "one"}], {"source": column})

        def row(
            params: RegexExtractParams, row: Row, context: InvocationContext
        ) -> RowResult[DynamicOutput]:
            entered.set()
            for _ in range(200):
                context.check_cancelled()
                # Yield without touching project state from the HTTP thread.
                threading.Event().wait(0.01)
            raise AssertionError("preview did not receive cancellation")

        original = ACTION_REGISTRY.get("map.regex_extract")
        registered = replace(
            original,
            definition=replace(
                original.definition,
                run=map_rows(
                    row, dynamic_outputs=original.definition.run.dynamic_outputs
                ),
            ),
        )
        monkeypatch.setattr(
            ACTION_REGISTRY,
            "_actions",
            {**ACTION_REGISTRY._actions, original.action_id: registered},
        )
        body = {
            "action_id": "map.regex_extract",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"input_columns": ["source"], "pattern": "one"},
            "idempotency_key": "context-preview",
        }
        base = f"/api/projects/{project_id}/actions/v1/preview"
        response = client.post(base, json=body)
        assert response.status_code == 202, response.text
        assert entered.wait(timeout=2)
        job_url = f"{base}/{response.json()['preview_id']}"
        assert client.delete(job_url).status_code == 204
        with controlled_time(timeout=2) as clock:
            clock.wait_until(
                lambda: client.get(job_url).json()["status"] != "running",
                message="preview cancellation did not finish",
            )
        assert client.get(job_url).json()["status"] == "cancelled"
        for table in ("runs", "results", "receipts"):
            assert (
                project.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
            )
