"""Native installed tables retain cooperative cancellation and joined cleanup."""

import asyncio
import threading

import pytest

from frisket.authoring.workbench.installed_actions import resolve_installed_action
from frisket.engine.executor.action_inventory import ExecutorDeps
from frisket.engine.executor.actions import run_action_spec
from tests.authoring.test_typed_plugin_table_host import request

pytest_plugins = ("tests.authoring.test_typed_plugin_table_host",)


def _assert_no_publication(project):
    for table in ("sheets", "columns", "rows", "cells", "ops", "receipts"):
        assert project.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["static", "runtime"])
@pytest.mark.parametrize("phase", ["before", "prepare", "close"])
def test_native_table_cancel_never_publishes(installed_table, kind, phase):
    _client, project, project_id, _package, calls = installed_table
    registered, _ = resolve_installed_action(project, f"test.tables.{kind}")
    namespace = registered.definition.run.handler.__globals__
    cancel = threading.Event()
    if phase == "before":
        cancel.set()
    elif phase == "prepare":
        namespace["on_prepare"] = cancel.set
    else:
        namespace["on_close"] = cancel.set
    result = run_action_spec(
        project,
        request(kind, count=501),
        project_id=project_id,
        deps=ExecutorDeps(cancelled=cancel.is_set),
    )
    assert result.status == "cancelled", result
    assert result.errors[0].code == "action_cancelled"
    assert calls == ([] if phase == "before" else [kind])
    if phase == "close":
        assert namespace["closed"] == [True]
    _assert_no_publication(project)


@pytest.mark.parametrize("kind", ["static", "runtime"])
def test_native_handler_cooperative_cancel_settles_without_publication(
    installed_table, kind
):
    _client, project, project_id, _package, calls = installed_table
    registered, _ = resolve_installed_action(project, f"test.tables.{kind}")
    namespace = registered.definition.run.handler.__globals__
    cancel = threading.Event()

    def stop():
        cancel.set()
        raise asyncio.CancelledError()

    namespace["on_prepare"] = stop
    result = run_action_spec(
        project,
        request(kind),
        project_id=project_id,
        deps=ExecutorDeps(cancelled=cancel.is_set),
    )
    assert result.status == "cancelled", result
    assert result.errors[0].code == "action_cancelled"
    assert calls == [kind]
    _assert_no_publication(project)


@pytest.mark.parametrize("kind", ["static", "runtime"])
def test_spontaneous_handler_cancellation_is_not_host_cancel(installed_table, kind):
    _client, project, project_id, _package, calls = installed_table
    registered, _ = resolve_installed_action(project, f"test.tables.{kind}")

    def interrupted():
        raise asyncio.CancelledError()

    registered.definition.run.handler.__globals__["on_prepare"] = interrupted
    with pytest.raises(asyncio.CancelledError):
        run_action_spec(
            project,
            request(kind),
            project_id=project_id,
            deps=ExecutorDeps(cancelled=lambda: False),
        )
    assert calls == [kind]
    _assert_no_publication(project)


def test_true_outer_task_cancel_is_not_cooperative_result(installed_table):
    _client, project, project_id, _package, calls = installed_table
    registered, _ = resolve_installed_action(project, "test.tables.static")
    cancel = threading.Event()

    def interrupted():
        cancel.set()
        asyncio.current_task().cancel()
        raise asyncio.CancelledError()

    registered.definition.run.handler.__globals__["on_prepare"] = interrupted

    async def outer():
        async def invoke():
            run_action_spec(
                project,
                request(),
                project_id=project_id,
                deps=ExecutorDeps(cancelled=cancel.is_set),
            )
            raise AssertionError("outer task cancellation was converted into a result")

        task = asyncio.create_task(invoke())
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()

    asyncio.run(outer())
    assert calls == ["static"]
    _assert_no_publication(project)


def test_private_worker_task_cancel_with_host_flag_propagates(installed_table):
    _client, project, project_id, _package, calls = installed_table
    registered, _ = resolve_installed_action(project, "test.tables.runtime")
    cancel = threading.Event()
    tasks = []

    def interrupted():
        task = asyncio.current_task()
        tasks.append(task)
        cancel.set()
        task.cancel()
        raise asyncio.CancelledError()

    registered.definition.run.handler.__globals__["on_prepare"] = interrupted
    with pytest.raises(asyncio.CancelledError):
        run_action_spec(
            project,
            request("runtime"),
            project_id=project_id,
            deps=ExecutorDeps(cancelled=cancel.is_set),
        )
    assert tasks[0].cancelling()
    assert calls == ["runtime"]
    _assert_no_publication(project)
