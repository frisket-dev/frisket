"""Returned purchases outlive publication authority closed during the request."""

from frisket.execution.purchases import LOCAL_PURCHASE_POLICY

import asyncio
from contextlib import closing

import pytest

import test_execution_lifecycle_recovery as lifecycle_recovery

from frisket.ai.llm import ModelRouter
from frisket.engine.store.runs import RunResultStore
from tests.engine.test_map_row_effect_checkpoint import (
    _ReturnedProviderCall,
    _bound,
    _confirmed_spec,
    _prepare_claimed_run,
    _runner,
    _spec,
)


work = lifecycle_recovery.work


def test_undo_during_provider_call_retains_usage_and_response_without_publication(
    make_project, monkeypatch
):
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    project = make_project("undo-return")
    sheet = project.add_sheet("data")
    source = project.add_column(sheet, "text")
    rows = project.add_rows(sheet, [{"text": "one"}, {"text": "two"}], {"text": source})
    spec, program = _bound(_spec(sheet))
    provider = _ReturnedProviderCall()
    router = ModelRouter(
        model_call_policy=LOCAL_PURCHASE_POLICY,
        keys={"anthropic": "test"},
        use_env_keys=False,
        cache_mode="off",
    )
    router._adapters["anthropic"] = provider
    runner = _runner(project, router)
    confirmed = _confirmed_spec(runner, spec, program)
    run_id, token = _prepare_claimed_run(runner, confirmed, program)
    complete = provider.complete

    async def undo_during_call(request, client):
        response = await complete(request, client)
        assert project.undo() is not None
        from frisket.engine.store.run_lifecycle import require_terminal_writer
        from frisket.execution.attempt import StaleAttemptWriter

        writer = runner.run_store.get_run(run_id)["current_attempt_id"]
        for attempt_id, claim, status in (
            ("other-attempt", token, "cancelled"),
            (writer, "other-claim", "cancelled"),
            (writer, token, "completed"),
        ):
            with pytest.raises(StaleAttemptWriter):
                require_terminal_writer(
                    project,
                    run_id=run_id,
                    writer_attempt_id=attempt_id,
                    claim_token=claim,
                    terminal_status=status,
                )
        return response

    monkeypatch.setattr(provider, "complete", undo_during_call)
    try:
        progress = asyncio.run(
            runner.run(
                confirmed,
                program=program,
                confirmed=True,
                resume_run_id=run_id,
                claim_token=token,
            )
        )
        assert progress.cancelled
        assert provider.calls == 1
        run = runner.run_store.get_run(run_id)
        assert run["status"] == "cancelled"
        assert run["cost_actual"] > 0
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM model_calls WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            == 1
        )
        checkpoint = runner.run_store.row_effect_checkpoint(run_id, rows[0])
        assert checkpoint["state"] == "returned"
        assert checkpoint["accounting_persisted"] is True
        assert checkpoint["response"]
        from frisket.engine.store.effect_checkpoints import EffectCheckpointStore

        # Re-observing known accounting after finalization is free and does
        # not require reopening its released output claim.
        def unexpected_charge(_checkpoint):
            pytest.fail("already accounted response must not accrue twice")

        assert (
            EffectCheckpointStore(project.db).account_returned(
                checkpoint["id"],
                family="row_effect",
                group_key=str(run_id),
                unit_key=str(rows[0]),
                action_kind=spec["action_kind"],
                identity=checkpoint["identity"],
                payload=checkpoint["response"],
                accrue=unexpected_charge,
                run_id=run_id,
                writer_attempt_id=checkpoint["authorized_attempt_id"],
                claim_token=token,
            )
            == 0.0
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            == 0
        )
        assert tuple(
            project.db.execute(
                "SELECT status,publication_closed FROM ops WHERE id=?",
                (run["op_id"],),
            ).fetchone()
        ) == ("undone", 1)
        assert {column["name"] for column in project.columns(sheet)} == {"text"}
    finally:
        asyncio.run(router.aclose())


def test_cloud_child_return_survives_undo_and_can_retire_after_accounting(tmp_path):
    from tests.engine.test_result_generation_store import (
        _seed_project,
        _start_claimed_run,
        _declare,
    )
    from test_cloud_llm_checkpoint import _payer, _request, _response
    from frisket.cloud_execution import CloudEstimateAuthority
    from frisket.cloud_llm_checkpoint import CloudLLMCheckpointCoordinator
    from frisket.engine.store.effect_checkpoints import EffectCheckpointStore
    from frisket.engine.store.result_generations import ResultGenerationStore

    project, sheet, column, rows = _seed_project(tmp_path, row_count=1)
    with closing(project):
        run = _start_claimed_run(
            project,
            sheet_id=sheet,
            output_column_id=column,
            row_ids=rows,
            label="cloud-return",
        )
        _declare(ResultGenerationStore(project), run, column, write_mode="create")

        class Transport:
            async def execute_llm(self, attempt, request):
                assert project.undo() == run.op_id
                return _response(execution_id="cloud-purchase")

        coordinator = CloudLLMCheckpointCoordinator(
            project,
            Transport(),
            payer=_payer(),
            estimate_authority=CloudEstimateAuthority(100),
            group_key="map:cloud-purchase",
            action_kind="map.extract",
            run_id=run.run_id,
            writer_attempt_id=run.authority.writer_attempt_id,
            claim_token=run.authority.claim_token,
        )
        _, response = asyncio.run(coordinator.execute(_request()))
        assert response.execution_id == "cloud-purchase"
        checkpoint = coordinator.returned_checkpoints[0]
        assert (
            EffectCheckpointStore(project.db).get(checkpoint.checkpoint_id)["state"]
            == "returned"
        )
        RunResultStore(project).write_returned_call_accounting(
            run.run_id,
            [
                {"row_id": rows[0], "column_id": column, **entry}
                for entry in coordinator.returned_accounting
            ],
            writer_attempt_id=run.authority.writer_attempt_id,
            claim_token=run.authority.claim_token,
        )
        coordinator.retire_returned()
        assert EffectCheckpointStore(project.db).get(checkpoint.checkpoint_id) is None
        assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0


def test_typed_action_finalizes_cancelled_receipt_after_undo(work, monkeypatch):
    project, _, _, _, calls, adapter, run = work
    complete = adapter.complete
    undone = []

    async def undo_during_call(request, client):
        response = await complete(request, client)
        if not undone:
            undone.append(project.undo())
        return response

    monkeypatch.setattr(adapter, "complete", undo_during_call)
    result = run(
        "map.ask",
        {
            "source": ["source"],
            "model": "anthropic/claude-haiku-4-5",
            "question": "describe",
        },
        output_names={"answer": "Answer"},
    )
    assert result.status == "cancelled", result
    assert undone[0] is not None
    assert calls
    stored = project.db.execute(
        "SELECT status FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert stored[0] == "cancelled"
    assert project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == len(
        calls
    )
    assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0
