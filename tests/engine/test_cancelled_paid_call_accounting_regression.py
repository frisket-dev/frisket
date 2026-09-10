"""Regression proof: cancellation must not erase a provider call that returned.

The result cell may remain resumable when cancellation wins the post-execute
fence, but the already-incurred provider spend is a ledger fact, not a cell.
"""

from __future__ import annotations

import asyncio

from frisket.ai.llm import LLMResponse
from frisket.engine.jobs.runs import project_scoped_router
from frisket.engine.runner import CostGate, MapRunner
from frisket.engine.store import Project
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.team.security.secrets import encrypt_secret, key_hint
from typed_model_fixtures import model_plan, run_with_output_claim


class _ReturnedPaidCall:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, req, client):  # noqa: ANN001
        self.calls += 1
        await asyncio.sleep(0)
        return LLMResponse(
            content='{"relevance": 7}',
            data={"relevance": 7},
            tokens_in=10,
            tokens_out=5,
            cost=0.0001,
            model=req.model,
        )


def test_cancel_after_paid_call_returns_keeps_usage_cost_and_project_spend(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    project = Project.create(tmp_path / "cancelled-paid-call.frisket")
    try:
        project.set_provider_key(
            provider="anthropic",
            encrypted=encrypt_secret("sk-ant-test"),
            hint=key_hint("sk-ant-test"),
            spend_cap_micro=1_000_000,
        )
        sheet = project.add_sheet("data")
        cols = {"text": project.add_column(sheet, "text")}
        project.add_rows(sheet, [{"text": "one billable row"}], cols)
        spec = {
            "action_kind": "map.classify",
            "model": "anthropic/claude-haiku-4-5",
            "sheet_id": sheet,
            "input_columns": ["text"],
            "context": "Test rows.",
            "fields": [
                {
                    "name": "relevance",
                    "type": "score",
                    "description": "0-10 relevance",
                }
            ],
        }

        adapter = _ReturnedPaidCall()
        router = project_scoped_router(project, None)
        router._adapters["anthropic"] = adapter  # noqa: SLF001
        runner = MapRunner(
            project,
            router,
            concurrency=1,
            authority=UnroutedOnlyAuthority(project),
        )
        # False before dispatch; true only after the provider has returned.
        runner.should_cancel = lambda run_id: adapter.calls >= 1

        # A bare confirmed flag on the typed program is challenged, never
        # admitted: the runner must quote first.
        plan = model_plan(spec)
        runner.allow_action_lifecycle_only_recipes = True
        try:
            asyncio.run(
                runner.run(
                    {**spec, **plan.spec_dict()}, program=plan.program, confirmed=True
                )
            )
        except CostGate as challenge:
            assert challenge.promise_set_hash is not None
            confirmed_spec = {
                **spec,
                "consented_promise_set_hash": challenge.promise_set_hash,
            }
        else:  # pragma: no cover - exact confirmation is load-bearing policy
            raise AssertionError("a bare confirmation boolean unexpectedly admitted")
        progress = asyncio.run(
            run_with_output_claim(
                runner,
                confirmed_spec,
                confirmed=True,
            )
        )
        row = project.db.execute(
            "SELECT cost_actual FROM runs WHERE id=?", (progress.run_id,)
        ).fetchone()
        persisted_calls = project.db.execute(
            "SELECT COUNT(*) FROM model_calls WHERE run_id=?", (progress.run_id,)
        ).fetchone()[0]

        assert progress.cancelled is True
        assert adapter.calls == 1
        assert {
            "provider_calls": persisted_calls,
            "run_cost": row["cost_actual"],
            "project_spend_micro": project.provider_spend_state(
                "anthropic"
            ).spent_micro,
        } == {
            "provider_calls": 1,
            "run_cost": 0.0001,
            "project_spend_micro": 100,
        }
    finally:
        project.close()
