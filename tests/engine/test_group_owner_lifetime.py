"""A live grouped invocation owns its wait, cancellation and existing leases."""

import asyncio
from contextlib import closing

import pytest

from executor_harness import run_action_with_confirmation
from frisket.ai.llm import LLMError
from frisket.actions.system import typed_action_for_request
from frisket.engine.executor import group_summary_action, group_summary_publication
from frisket.engine.executor.sheet_refresh_action import run_typed_sheet_refresh_action
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from test_reduce_effect_checkpoint import (
    _action,
    _CountingSummaryAdapter,
    _project_key_router,
    _run,
    project as _project_fixture,
)
from test_group_summary_refresh import (
    _seed,
    _reduce_action,
    _bound_request,
    _summary_router,
)


project = _project_fixture


def test_cancel_during_lease_cleanup_retains_returned_purchase(project):
    class CancelAfterResponse(_CountingSummaryAdapter):
        async def complete(self, req, client, **kwargs):
            response = await super().complete(req, client, **kwargs)
            asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
            return response

    adapter = CancelAfterResponse()
    with pytest.raises(asyncio.CancelledError):
        _run(
            project,
            _action(project._sheet_id, key="cancel-lease-cleanup"),
            _project_key_router(adapter),
        )
    assert len(adapter.requests) == 1
    run_id = project.db.execute("SELECT MAX(id) FROM runs").fetchone()[0]
    calls = RunResultStore(project).model_calls(run_id)
    assert len(calls) == 1
    assert calls[0]["provider_cost_usd"] == pytest.approx(0.006)
    assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0
    assert (
        project.db.execute("SELECT state FROM effect_checkpoints").fetchone()[0]
        == "returned"
    )


def test_external_task_cancel_in_retry_backoff_retains_usage_and_stops_next_group(
    project, monkeypatch
):
    class CancelDuringBackoff(_CountingSummaryAdapter):
        async def complete(self, req, client, **kwargs):
            response = await super().complete(req, client, **kwargs)
            if len(self.requests) == 1:
                asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
                error = LLMError("known malformed response", retryable=True)
                error.wire_calls = [response]
                raise error
            return response

    adapter = CancelDuringBackoff()
    router = _project_key_router(adapter)
    original_run = asyncio.run

    def checked_run(coroutine):
        async def joined():
            try:
                return await coroutine
            finally:
                assert not [
                    task
                    for task in asyncio.all_tasks()
                    if task is not asyncio.current_task()
                    and task.get_name().startswith("group-provider-lease")
                ]

        return original_run(joined())

    monkeypatch.setattr(group_summary_action.asyncio, "run", checked_run)
    with pytest.raises(asyncio.CancelledError):
        _run(project, _action(project._sheet_id, key="actual-task-cancel"), router)
    assert len(adapter.requests) == 1
    run_id = project.db.execute("SELECT MAX(id) FROM runs").fetchone()[0]
    calls = RunResultStore(project).model_calls(run_id)
    assert len(calls) == 1 and calls[0]["provider_cost_usd"] == pytest.approx(0.006)
    assert RunResultStore(project).get_run(run_id)["cancel_requested_at"] is None
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM results WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        == 0
    )
    assert (
        project.db.execute("SELECT state FROM effect_checkpoints").fetchone()[0]
        == "returned"
    )


def test_refresh_renews_expiring_sheet_and_output_leases_while_provider_waits(
    tmp_path, make_project, monkeypatch
):
    monkeypatch.setattr(
        group_summary_publication, "_PROVIDER_LEASE_RENEW_SECONDS", 0.001, raising=False
    )
    project = make_project("slow-refresh")
    seeded = _seed(project, tmp_path)
    initial_router, _ = _summary_router()
    initial = group_summary_action.run_typed_group_summary_action(
        project,
        "p",
        _bound_request(_reduce_action(seeded["sheet_id"])),
        initial_router,
    )
    assert initial.status == "completed", initial.errors
    sheet = initial.outputs[0].sheet_id
    router, adapter = _summary_router()
    competing_router, competing_adapter = _summary_router()
    complete = adapter.complete
    competing = []
    first = True

    async def slow_provider(request, client):
        nonlocal first
        if first:
            first = False
            run_id = project.db.execute("SELECT MAX(id) FROM runs").fetchone()[0]
            # Advance the existing lease timestamps past expiry, then give
            # the owning renewal one tick while this actual request waits.
            with project.transaction_scope():
                project.db.execute(
                    "UPDATE sheets SET refresh_lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                    (sheet,),
                )
                project.db.execute(
                    "UPDATE generated_column_claims SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE run_id=? AND status='active'",
                    (run_id,),
                )
            await asyncio.sleep(0.02)

            def other_writer():
                with closing(Project(project.path)) as other:
                    action = {
                        "action_id": "sheet.refresh",
                        "scope": {"kind": "project"},
                        "params": {"sheet_id": sheet},
                        "idempotency_key": "competing-slow-refresh",
                    }
                    return run_action_with_confirmation(
                        other, action, project_id="p", router=competing_router
                    )

            competing.append(await asyncio.to_thread(other_writer))
        return await complete(request, client)

    adapter.complete = slow_provider
    request = typed_action_for_request(
        {
            "action_id": "sheet.refresh",
            "scope": {"kind": "project"},
            "params": {"sheet_id": sheet},
            "idempotency_key": "owning-slow-refresh",
        }
    )
    result = run_typed_sheet_refresh_action(project, "p", request, router)
    assert competing[0].status == "failed", competing[0].model_dump()
    assert competing[0].errors[0].code == "sheet_refresh_busy", competing[
        0
    ].model_dump()
    assert competing_adapter.requests == []
    assert result.status == "completed", result.errors
    assert len(adapter.requests) == 2
    assert (
        project.db.execute(
            "SELECT refresh_claim_token FROM sheets WHERE id=?", (sheet,)
        ).fetchone()[0]
        is None
    )


def test_renewal_failure_keeps_returned_purchase_for_exact_recovery(
    project, monkeypatch
):
    from frisket.engine.executor.group_summary_publication import GroupPublication

    monkeypatch.setattr(
        group_summary_publication, "_PROVIDER_LEASE_RENEW_SECONDS", 0.001
    )
    renew = GroupPublication._renew_provider_lease
    renewals = 0

    def fail_one_renewal(self):
        nonlocal renewals
        renewals += 1
        if renewals == 2:
            raise RuntimeError("injected lease write failure")
        return renew(self)

    monkeypatch.setattr(GroupPublication, "_renew_provider_lease", fail_one_renewal)

    class Slow(_CountingSummaryAdapter):
        async def complete(self, request, client, **kwargs):
            await asyncio.sleep(0.01)
            return await super().complete(request, client, **kwargs)

    adapter = Slow()
    router = _project_key_router(adapter)
    action = _action(project._sheet_id, key="renewal-failure")
    failed = _run(project, action, router)
    assert failed.status == "failed" and failed.errors[0].code == "project_write_failed"
    assert len(adapter.requests) == 1
    run_id = project.db.execute("SELECT MAX(id) FROM runs").fetchone()[0]
    assert len(RunResultStore(project).model_calls(run_id)) == 1
    assert (
        project.db.execute("SELECT state FROM effect_checkpoints").fetchone()[0]
        == "returned"
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM results WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        == 0
    )
    recovered = _run(project, action, router)
    assert recovered.status == "completed", recovered.errors
    assert len(adapter.requests) == 2
    assert len(RunResultStore(project).model_calls(run_id)) == 2
    assert RunResultStore(project).get_run(run_id)["cost_actual"] == pytest.approx(
        0.012
    )
