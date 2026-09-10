"""Crash-window regressions for paid row effects in ``MapRunner``.

A provider response and the result/fact transaction are separated by Python
instructions.  A process can die in that gap.  Resume must replay durable
returned data, never buy the row again, and the provider fact must retain the
attempt that authorized the original egress.
"""

from __future__ import annotations

import frisket.sdk.ops.transcribe_engines as transcribe_engines

import asyncio
import wave

import pytest

from frisket.ai.llm import LLMError, LLMResponse, ModelRouter
from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig
from frisket.ai.llm.adapters import OpenAICompatAdapter
from frisket.ai.llm.pricing import cost_of
from frisket.engine.runner import CostGate, MapRunner
from frisket.engine.runner.validation import (
    ProviderSpendCapExceeded,
    assert_provider_spend_cap,
)
from frisket.engine.store import Project
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.result_generations import GenerationSealedError
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.execution.runtime_binding import ExecutionRouteVerificationFailed
from frisket.ops.base import Recipe
from frisket.team.security.secrets import encrypt_secret, key_hint
from typed_model_fixtures import model_plan, run_with_exact_confirmation


class _ReturnedProviderCall:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request, _client):  # noqa: ANN001
        self.calls += 1
        return LLMResponse(
            content='{"relevance": 7}',
            data={"relevance": 7},
            tokens_in=10,
            tokens_out=5,
            cost=0.0001,
            model=request.model,
        )


def _spec(sheet_id: int) -> dict:
    return {
        "action_kind": "map.classify",
        "model": "anthropic/claude-haiku-4-5",
        "sheet_id": sheet_id,
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


def _bound(spec: dict) -> tuple[dict, Recipe]:
    """Bind the canonical runner spec to its explicit typed classify program."""

    plan = model_plan(spec)
    return {**spec, **plan.spec_dict()}, plan.program


def _runner(project: Project, router: ModelRouter) -> MapRunner:
    """A direct runner carrying the same explicit-program authority an action
    lifecycle grants; the typed program is passed on every prepare/run."""

    return MapRunner(
        project,
        router,
        concurrency=1,
        authority=UnroutedOnlyAuthority(project),
        allow_action_lifecycle_only_recipes=True,
    )


def _confirmed_spec(runner: MapRunner, spec: dict, program: Recipe) -> dict:
    with pytest.raises(CostGate) as challenge:
        runner.prepare_run(spec, program=program, confirmed=False)
    assert challenge.value.promise_set_hash is not None
    return {
        **spec,
        "consented_promise_set_hash": challenge.value.promise_set_hash,
    }


def _age_silent_attempt(project: Project, attempt_id: str) -> None:
    """Model the recovery detector observing an old attempt and expired lease."""

    project.db.execute(
        "UPDATE execution_attempts SET created_at="
        "datetime('now', '-7 hours') WHERE id=?",
        (attempt_id,),
    )
    project.db.execute(
        "UPDATE output_column_claims SET "
        "lease_expires_at=datetime('now', '-1 second') "
        "WHERE run_id=(SELECT run_id FROM execution_attempts WHERE id=?) "
        "AND status='active'",
        (attempt_id,),
    )
    project.db.commit()


def _prepare_claimed_run(
    runner: MapRunner,
    spec: dict,
    program: Recipe,
    *,
    confirmed: bool = True,
) -> tuple[int, str]:
    """Give a raw runner test the same durable output authority as an action."""

    progress = runner.prepare_run(spec, program=program, confirmed=confirmed)
    fields = [dict(field) for field in program.output_fields(spec)]
    token = f"output-claim:test:{progress.run_id}"
    claims, conflict = OutputColumnClaimStore(runner.project).acquire(
        sheet_id=int(spec["sheet_id"]),
        output_names=[str(field["name"]) for field in fields],
        action_kind=str(spec["action_kind"]),
        run_id=progress.run_id,
        claim_token=token,
        lease_seconds=6 * 60 * 60,
    )
    assert conflict is None
    assert len(claims) == len(fields)
    OutputColumnClaimStore(runner.project).bind_to_run(
        claim_token=token,
        run_id=progress.run_id,
        expected_output_names=[str(field["name"]) for field in fields],
    )
    return progress.run_id, token


def test_resume_replays_returned_row_without_buying_it_again(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    project = Project.create(tmp_path / "returned-row.frisket")
    try:
        sheet = project.add_sheet("data")
        columns = {"text": project.add_column(sheet, "text")}
        project.add_rows(sheet, [{"text": "one billable row"}], columns)
        spec, program = _bound(_spec(sheet))

        provider = _ReturnedProviderCall()
        router = ModelRouter(keys={"anthropic": "test"}, cache_mode="off")
        router._adapters["anthropic"] = provider  # noqa: SLF001
        first = _runner(project, router)
        confirmed = _confirmed_spec(first, spec, program)
        run_id, claim_token = _prepare_claimed_run(first, confirmed, program)

        class InjectedProcessDeath(RuntimeError):
            pass

        def die_before_result_commit(*_args, **_kwargs) -> None:
            raise InjectedProcessDeath("after provider return")

        # This is the exact vulnerable boundary: the adapter has returned,
        # but the normal result + provider-fact transaction has not begun.
        first.run_store.write_results = die_before_result_commit  # type: ignore[method-assign]
        with pytest.raises(InjectedProcessDeath, match="after provider return"):
            asyncio.run(
                first.run(
                    confirmed,
                    program=program,
                    confirmed=True,
                    resume_run_id=run_id,
                    claim_token=claim_token,
                )
            )

        first_attempt_id = str(
            project.db.execute(
                "SELECT id FROM execution_attempts WHERE run_id=? ORDER BY seq",
                (run_id,),
            ).fetchone()[0]
        )
        assert provider.calls == 1
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            == 0
        )
        checkpoint = project.db.execute(
            "SELECT state, authorized_attempt_id, payload AS response "
            "FROM effect_checkpoints WHERE family='row_effect' "
            "AND group_key=CAST(? AS TEXT)",
            (run_id,),
        ).fetchone()
        assert checkpoint is not None
        assert checkpoint["state"] == "returned"
        assert checkpoint["authorized_attempt_id"] == first_attempt_id
        assert checkpoint["response"]

        # Model the queue's bounded orphan recovery after both the attempt and
        # its last metered heartbeat have been silent past the cutoff.
        _age_silent_attempt(project, first_attempt_id)

        resumed = asyncio.run(
            _runner(project, router).run(
                confirmed,
                program=program,
                confirmed=True,
                resume_run_id=run_id,
                claim_token=claim_token,
            )
        )

        assert resumed.done is True
        assert resumed.completed == 1
        assert provider.calls == 1
        call = project.db.execute(
            "SELECT attempt_id, provider_cost_usd FROM model_calls WHERE run_id=?",
            (run_id,),
        ).fetchone()
        assert call is not None
        assert call["attempt_id"] == first_attempt_id
        assert call["provider_cost_usd"] == pytest.approx(0.0001)
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM execution_attempts WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            == 2
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM effect_checkpoints "
                "WHERE family='row_effect' AND group_key=CAST(? AS TEXT)",
                (run_id,),
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_returned_checkpoint_accounts_spend_before_result_commit(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    """The provider invoice becomes cap truth before the result crash window."""

    project = Project.create(tmp_path / "returned-row-cap.frisket")
    try:
        project.set_provider_key(
            provider="anthropic",
            encrypted=encrypt_secret("sk-ant-test"),
            hint=key_hint("sk-ant-test"),
            spend_cap_micro=50,
        )
        sheet = project.add_sheet("data")
        columns = {"text": project.add_column(sheet, "text")}
        project.add_rows(sheet, [{"text": "cross the cap"}], columns)
        provider = _ReturnedProviderCall()
        router = ModelRouter(
            keys={"anthropic": "sk-ant-test"},
            key_sources={"anthropic": "project_key"},
            cache_mode="off",
        )
        router._adapters["anthropic"] = provider  # noqa: SLF001
        runner = _runner(project, router)
        spec, program = _bound(_spec(sheet))
        confirmed = _confirmed_spec(runner, spec, program)
        run_id, claim_token = _prepare_claimed_run(runner, confirmed, program)

        def die_before_result_commit(*_args, **_kwargs) -> None:
            raise RuntimeError("after accounted provider return")

        runner.run_store.write_results = die_before_result_commit  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="after accounted provider return"):
            asyncio.run(
                runner.run(
                    confirmed,
                    program=program,
                    confirmed=True,
                    resume_run_id=run_id,
                    claim_token=claim_token,
                )
            )

        # The exact provider fact and spend must already be durable even though
        # no result cell committed. Otherwise another launch can buy work while
        # this returned checkpoint remains invisible to the cap.
        assert provider.calls == 1
        assert project.provider_spend_state("anthropic").spent_micro == 100
        assert project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 1
        with pytest.raises(ProviderSpendCapExceeded):
            assert_provider_spend_cap(project, "anthropic")
    finally:
        project.close()


class _RepairThenReturn:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request, _client):  # noqa: ANN001
        self.calls += 1
        data = {} if self.calls == 1 else {"relevance": 8}
        return LLMResponse(
            content="{}" if not data else '{"relevance": 8}',
            data=data,
            tokens_in=10,
            tokens_out=5,
            cost=0.0001,
            model=request.model,
        )


def test_returned_checkpoint_replays_every_structured_wire_ordinal(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    """A corrective retry is one row effect containing two paid wire facts."""

    project = Project.create(tmp_path / "returned-repair.frisket")
    try:
        sheet = project.add_sheet("data")
        columns = {"text": project.add_column(sheet, "text")}
        project.add_rows(sheet, [{"text": "repair me"}], columns)
        spec, program = _bound(_spec(sheet))
        provider = _RepairThenReturn()
        router = ModelRouter(keys={"anthropic": "test"}, cache_mode="off")
        router._adapters["anthropic"] = provider  # noqa: SLF001
        first = _runner(project, router)
        confirmed = _confirmed_spec(first, spec, program)
        run_id, claim_token = _prepare_claimed_run(first, confirmed, program)

        def die_before_result_commit(*_args, **_kwargs) -> None:
            raise RuntimeError("after repaired provider return")

        first.run_store.write_results = die_before_result_commit  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="after repaired provider return"):
            asyncio.run(
                first.run(
                    confirmed,
                    program=program,
                    confirmed=True,
                    resume_run_id=run_id,
                    claim_token=claim_token,
                )
            )
        first_attempt_id = str(
            project.db.execute(
                "SELECT id FROM execution_attempts WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        )
        assert provider.calls == 2
        _age_silent_attempt(project, first_attempt_id)

        resumed = asyncio.run(
            _runner(project, router).run(
                confirmed,
                program=program,
                confirmed=True,
                resume_run_id=run_id,
                claim_token=claim_token,
            )
        )

        assert resumed.completed == 1
        assert provider.calls == 2
        calls = project.db.execute(
            "SELECT attempt_id FROM model_calls WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()
        assert len(calls) == 2
        assert {call["attempt_id"] for call in calls} == {first_attempt_id}
    finally:
        project.close()


def test_identical_prompts_keep_distinct_row_effect_identities(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    project = Project.create(tmp_path / "identical-rows.frisket")
    try:
        sheet = project.add_sheet("data")
        columns = {"text": project.add_column(sheet, "text")}
        project.add_rows(
            sheet,
            [{"text": "same prompt"}, {"text": "same prompt"}],
            columns,
        )
        spec, program = _bound(_spec(sheet))
        provider = _ReturnedProviderCall()
        router = ModelRouter(keys={"anthropic": "test"}, cache_mode="off")
        router._adapters["anthropic"] = provider  # noqa: SLF001
        first = _runner(project, router)
        confirmed = _confirmed_spec(first, spec, program)
        run_id, claim_token = _prepare_claimed_run(first, confirmed, program)

        def die_before_first_result(*_args, **_kwargs) -> None:
            raise RuntimeError("first identical row returned")

        first.run_store.write_results = die_before_first_result  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="first identical row returned"):
            asyncio.run(
                first.run(
                    confirmed,
                    program=program,
                    confirmed=True,
                    resume_run_id=run_id,
                    claim_token=claim_token,
                )
            )
        first_attempt_id = str(
            project.db.execute(
                "SELECT id FROM execution_attempts WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        )
        assert provider.calls == 1
        _age_silent_attempt(project, first_attempt_id)

        resumed = asyncio.run(
            _runner(project, router).run(
                confirmed,
                program=program,
                confirmed=True,
                resume_run_id=run_id,
                claim_token=claim_token,
            )
        )

        # One replay plus one genuinely different row effect: the prompt text
        # is identical, but provider/cache/request hashes are not row identity.
        assert resumed.completed == 2
        assert provider.calls == 2
        assert (
            project.db.execute(
                "SELECT COUNT(DISTINCT row_id) FROM model_calls WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
            == 2
        )
    finally:
        project.close()


class SimulatedProcessKill(BaseException):
    pass


class _KilledInFlightCall:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request, _client):  # noqa: ANN001
        self.calls += 1
        if self.calls == 1:
            raise SimulatedProcessKill("provider outcome unknown")
        return LLMResponse(
            content='{"relevance": 9}',
            data={"relevance": 9},
            tokens_in=10,
            tokens_out=5,
            cost=0.0001,
            model=request.model,
        )


class _AcceptedThenResponseLost:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request, _client):  # noqa: ANN001
        self.calls += 1
        if self.calls == 1:
            raise LLMError(
                "provider accepted work but its response was lost",
                status=502,
                retryable=True,
            )
        return LLMResponse(
            content='{"relevance": 9}',
            data={"relevance": 9},
            tokens_in=10,
            tokens_out=5,
            cost=0.0001,
            model=request.model,
        )


def test_router_ambiguous_failure_keeps_row_effect_reserved_for_reconciliation(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    project = Project.create(tmp_path / "ambiguous-router-row.frisket")
    try:
        sheet = project.add_sheet("data")
        columns = {"text": project.add_column(sheet, "text")}
        project.add_rows(sheet, [{"text": "unknown accepted outcome"}], columns)
        provider = _AcceptedThenResponseLost()
        router = ModelRouter(
            keys={"anthropic": "test"},
            cache_mode="off",
            max_retries=3,
        )
        router._adapters["anthropic"] = provider  # noqa: SLF001
        runner = _runner(project, router)
        spec, program = _bound(_spec(sheet))
        confirmed = _confirmed_spec(runner, spec, program)
        run_id, claim_token = _prepare_claimed_run(runner, confirmed, program)

        halted = asyncio.run(
            runner.run(
                confirmed,
                program=program,
                confirmed=True,
                resume_run_id=run_id,
                claim_token=claim_token,
            )
        )

        assert halted.halted_code == "external_effect_reconciliation_required"
        assert halted.completed == 0
        assert provider.calls == 1
        checkpoint = project.db.execute(
            "SELECT state FROM effect_checkpoints WHERE family='row_effect' "
            "AND group_key=CAST(? AS TEXT)",
            (run_id,),
        ).fetchone()
        assert checkpoint is not None
        assert checkpoint["state"] == "reserved"
        assert project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
    finally:
        project.close()


def test_ambiguous_reserved_effect_refuses_resume_instead_of_recalling(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    project = Project.create(tmp_path / "ambiguous-row.frisket")
    try:
        sheet = project.add_sheet("data")
        columns = {"text": project.add_column(sheet, "text")}
        project.add_rows(sheet, [{"text": "unknown outcome"}], columns)
        spec, program = _bound(_spec(sheet))
        provider = _KilledInFlightCall()
        router = ModelRouter(keys={"anthropic": "test"}, cache_mode="off")
        router._adapters["anthropic"] = provider  # noqa: SLF001
        first = _runner(project, router)
        confirmed = _confirmed_spec(first, spec, program)
        run_id, claim_token = _prepare_claimed_run(first, confirmed, program)
        with pytest.raises(SimulatedProcessKill, match="outcome unknown"):
            asyncio.run(
                first.run(
                    confirmed,
                    program=program,
                    confirmed=True,
                    resume_run_id=run_id,
                    claim_token=claim_token,
                )
            )
        first_attempt_id = str(
            project.db.execute(
                "SELECT id FROM execution_attempts WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        )
        checkpoint = project.db.execute(
            "SELECT state FROM effect_checkpoints WHERE family='row_effect' "
            "AND group_key=CAST(? AS TEXT)",
            (run_id,),
        ).fetchone()
        assert checkpoint is not None and checkpoint["state"] == "reserved"
        project.db.execute(
            "UPDATE execution_attempts SET created_at="
            "datetime('now', '-7 hours') WHERE id=?",
            (first_attempt_id,),
        )
        project.db.commit()

        with pytest.raises(ExecutionRouteVerificationFailed) as refusal:
            asyncio.run(
                _runner(project, router).run(
                    confirmed,
                    program=program,
                    confirmed=True,
                    resume_run_id=run_id,
                    claim_token=claim_token,
                )
            )

        assert refusal.value.code == "stale_head"
        assert "ONE dispatch at a time" in str(refusal.value)
        assert provider.calls == 1
        assert (
            project.db.execute(
                "SELECT state FROM effect_checkpoints WHERE family='row_effect' "
                "AND group_key=CAST(? AS TEXT)",
                (run_id,),
            ).fetchone()[0]
            == "reserved"
        )
        run = project.db.execute(
            "SELECT status, current_attempt_id FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        assert tuple(run) == ("running", first_attempt_id)
        assert (
            project.db.execute(
                "SELECT state FROM execution_attempts WHERE id=?",
                (first_attempt_id,),
            ).fetchone()[0]
            == "dispatching"
        )
        assert (
            project.db.execute(
                "SELECT status FROM output_column_claims WHERE claim_token=?",
                (claim_token,),
            ).fetchone()[0]
            == "active"
        )
    finally:
        project.close()


def test_cancelled_return_requires_fresh_generation_without_changing_old_accounting(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    project = Project.create(tmp_path / "cancelled-return.frisket")
    try:
        sheet = project.add_sheet("data")
        columns = {"text": project.add_column(sheet, "text")}
        project.add_rows(sheet, [{"text": "cancel after return"}], columns)
        spec, program = _bound(_spec(sheet))
        provider = _ReturnedProviderCall()
        router = ModelRouter(keys={"anthropic": "test"}, cache_mode="off")
        router._adapters["anthropic"] = provider  # noqa: SLF001
        first = _runner(project, router)
        confirmed = _confirmed_spec(first, spec, program)
        run_id, claim_token = _prepare_claimed_run(first, confirmed, program)
        first.should_cancel = lambda _run_id: provider.calls >= 1
        cancelled = asyncio.run(
            first.run(
                confirmed,
                program=program,
                confirmed=True,
                resume_run_id=run_id,
                claim_token=claim_token,
            )
        )
        OutputColumnClaimStore(project).release(
            claim_token=claim_token, status="cancelled"
        )
        original_attempt = str(
            project.db.execute(
                "SELECT id FROM execution_attempts WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        )
        assert cancelled.cancelled is True
        assert provider.calls == 1
        checkpoint = project.db.execute(
            "SELECT state, accounting_persisted FROM effect_checkpoints "
            "WHERE family='row_effect' AND group_key=CAST(? AS TEXT)",
            (run_id,),
        ).fetchone()
        assert checkpoint is not None
        assert dict(checkpoint) == {"state": "returned", "accounting_persisted": 1}

        with pytest.raises(GenerationSealedError, match="fresh explicitly scoped"):
            asyncio.run(
                _runner(project, router).run(
                    confirmed,
                    program=program,
                    confirmed=True,
                    resume_run_id=run_id,
                    claim_token=claim_token,
                )
            )

        assert provider.calls == 1
        run = project.db.execute(
            "SELECT cost_actual FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        calls = project.db.execute(
            "SELECT attempt_id FROM model_calls WHERE run_id=?", (run_id,)
        ).fetchall()
        assert run["cost_actual"] == pytest.approx(0.0001)
        assert len(calls) == 1
        assert calls[0]["attempt_id"] == original_attempt
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM effect_checkpoints "
                "WHERE family='row_effect' AND group_key=CAST(? AS TEXT)",
                (run_id,),
            ).fetchone()[0]
            == 1
        )

        row_id = int(
            project.db.execute(
                "SELECT id FROM rows WHERE sheet_id=?", (sheet,)
            ).fetchone()[0]
        )
        recovery = asyncio.run(
            run_with_exact_confirmation(
                _runner(project, router),
                {**spec, "row_ids": [row_id], "replace_existing": True},
            )
        )
        assert recovery.run_id != run_id
        assert recovery.completed == 1
        assert provider.calls == 2
    finally:
        project.close()


def test_row_refresh_cannot_erase_unreconciled_external_effect(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    """Physical row replacement must not cascade-delete money-path evidence."""

    project = Project.create(tmp_path / "refreshed-return.frisket")
    try:
        sheet = project.add_sheet("data")
        columns = {"text": project.add_column(sheet, "text")}
        project.add_rows(sheet, [{"text": "soon replaced"}], columns)
        row_id = int(project.db.execute("SELECT id FROM rows").fetchone()[0])
        spec, program = _bound(_spec(sheet))
        provider = _ReturnedProviderCall()
        router = ModelRouter(keys={"anthropic": "test"}, cache_mode="off")
        router._adapters["anthropic"] = provider  # noqa: SLF001
        runner = _runner(project, router)
        confirmed = _confirmed_spec(runner, spec, program)
        run_id, claim_token = _prepare_claimed_run(runner, confirmed, program)

        def die_before_result_commit(*_args, **_kwargs) -> None:
            raise RuntimeError("returned before refresh")

        runner.run_store.write_results = die_before_result_commit  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="returned before refresh"):
            asyncio.run(
                runner.run(
                    confirmed,
                    program=program,
                    confirmed=True,
                    resume_run_id=run_id,
                    claim_token=claim_token,
                )
            )
        assert provider.calls == 1
        assert (
            project.db.execute(
                "SELECT state FROM effect_checkpoints WHERE family='row_effect' "
                "AND group_key=CAST(? AS TEXT) AND unit_key=CAST(? AS TEXT)",
                (run_id, row_id),
            ).fetchone()[0]
            == "returned"
        )

        # sheet_refresh replaces physical source rows with this same DELETE.
        project.db.execute("DELETE FROM rows WHERE sheet_id=?", (sheet,))
        project.db.commit()

        checkpoint = project.db.execute(
            "SELECT state, payload AS response FROM effect_checkpoints "
            "WHERE family='row_effect' AND group_key=CAST(? AS TEXT) "
            "AND unit_key=CAST(? AS TEXT)",
            (run_id, row_id),
        ).fetchone()
        assert checkpoint is not None
        assert checkpoint["state"] == "returned"
        assert checkpoint["response"]
    finally:
        project.close()


def test_free_local_llm_row_checkpoint_is_consumed(tmp_path) -> None:
    """A local LLM adapter still follows classify's metered row-effect
    contract: reserve once before the call, then atomically consume the
    checkpoint when its $0 result lands so no stranded reservation remains."""
    project = Project.create(tmp_path / "free-local-row.frisket")
    try:
        sheet = project.add_sheet("data")
        columns = {"text": project.add_column(sheet, "text")}
        project.add_rows(sheet, [{"text": "one free local row"}], columns)
        spec, program = _bound(
            {
                **_spec(sheet),
                # Deliberately absent from pricing_data.json: canonical local
                # identity, not a model-name exception, proves the known zero.
                "model": "ollama/@test-local/frisket-free-local-test",
            }
        )

        class _FreeLocalCall(OpenAICompatAdapter):
            def __init__(self) -> None:
                super().__init__("test", "http://127.0.0.1:11434/v1")
                self.calls = 0

            async def complete(  # noqa: ANN001
                self, request, _client, *, cost_model=None
            ):
                self.calls += 1
                return LLMResponse(
                    content='{"relevance": 3}',
                    data={"relevance": 3},
                    tokens_in=10,
                    tokens_out=5,
                    cost=cost_of(cost_model or request.model, 10, 5),
                    model=request.model,
                )

        provider = _FreeLocalCall()
        router = ModelRouter(
            cache_mode="off",
            use_env_keys=False,
            local_endpoints=(
                LocalModelEndpointConfig(
                    endpoint_id="test-local",
                    display_name="Test local",
                    origin="http://127.0.0.1:11434",
                    source="local_file",
                ),
            ),
        )
        router._adapters["ollama"]._adapters["test-local"] = provider  # type: ignore[attr-defined]  # noqa: SLF001
        runner = _runner(project, router)
        run_id, claim_token = _prepare_claimed_run(
            runner, spec, program, confirmed=False
        )

        reserve_calls = 0
        durable_reserve = runner.run_store.reserve_row_effect_checkpoint

        def counting_reserve(*args, **kwargs):  # noqa: ANN002, ANN003
            nonlocal reserve_calls
            reserve_calls += 1
            return durable_reserve(*args, **kwargs)

        runner.run_store.reserve_row_effect_checkpoint = counting_reserve  # type: ignore[method-assign]
        progress = asyncio.run(
            runner.run(
                spec,
                program=program,
                confirmed=False,
                resume_run_id=run_id,
                claim_token=claim_token,
            )
        )

        assert progress.done is True
        assert progress.completed == 1
        assert progress.failed == 0
        assert provider.calls == 1
        run_id = progress.run_id
        run = project.db.execute(
            "SELECT cost_actual FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        assert run["cost_actual"] == 0.0
        call = project.db.execute(
            "SELECT provider_cost_usd, cost_source FROM model_calls WHERE run_id=?",
            (run_id,),
        ).fetchone()
        assert call["provider_cost_usd"] == 0.0
        assert call["cost_source"] == "free_local"

        from frisket.server.provenance_payloads import provenance_manifest_payload

        report = provenance_manifest_payload(project, "free-local-project")
        assert report["has_unknown_costs"] is False
        assert report["unknown_cost_runs"] == 0
        assert report["runs"][0]["cost"] == 0.0
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            == 1
        )
        assert reserve_calls == 1
        # Publication consumes the returned checkpoint in the same transaction
        # that writes the result; the durable fence does not linger afterward.
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM effect_checkpoints WHERE family='row_effect'"
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def _wav(path) -> str:
    """PCM16 mono 16 kHz half-second of silence."""

    sr = 16000
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00" * (sr // 2))
    return str(path)


def test_local_halt_discards_reservation_so_the_run_stays_recoverable(
    tmp_path, monkeypatch
) -> None:
    """A metered-but-local row (no remote capability resolves) halting
    mid-session provably reached no provider; the meter fact books only at
    checkpoint completion. Its reservation must be discarded so a fresh
    scoped generation can buy the remaining row."""

    from frisket.ops.base import RecipeInvocationHalt
    from frisket.engine.executor import run_action_spec
    from frisket.engine.store.media_blobs import (
        media_cell,
        owned_media_metadata_document,
    )
    from pathlib import Path

    project = Project.create(tmp_path / "local-halt.frisket")
    try:
        sheet = project.add_sheet("data")
        columns = {"audio": project.add_column(sheet, "audio", type="audio")}
        project.add_rows(
            sheet,
            [
                {
                    "audio": media_cell(
                        project.add_blob(
                            Path(_wav(tmp_path / f"row-{index}.wav")).read_bytes(),
                            filename=f"row-{index}.wav",
                            mime="audio/wav",
                            metadata=owned_media_metadata_document(
                                probe={"duration_seconds": 0.5, "kind": "audio"}
                            ),
                        ),
                        mime="audio/wav",
                        filename=f"row-{index}.wav",
                    )
                }
                for index in range(2)
            ],
            columns,
        )
        action = {
            "action_id": "media.transcribe",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"source": "audio", "engine": "faster_whisper"},
            "output_names": {"text": "transcript"},
            "idempotency_key": "local-halt",
        }
        calls = 0
        halt = True

        async def halt_then_recover(self, path, spec, *, should_cancel=None):  # noqa: ANN001
            nonlocal calls
            del self, path, spec
            calls += 1
            if halt and calls == 2:
                raise RecipeInvocationHalt(
                    "local_session_failed", "session exited during row work"
                )
            return {
                "text": f"transcript {calls}",
                "segments": [{"start": 0.0, "end": 0.5, "text": f"transcript {calls}"}],
                "language": "en",
                "duration": 0.5,
            }

        monkeypatch.setattr(
            transcribe_engines.FasterWhisperAdapter, "transcribe", halt_then_recover
        )
        halted = run_action_spec(project, action, project_id="local-halt")
        run_id = halted.run_id
        assert halted.status == "failed", halted
        assert any(error.code == "local_session_failed" for error in halted.errors)
        assert calls == 2
        # The halted row provably bought nothing; no reservation may survive
        # to make the run permanently irrecoverable.
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM effect_checkpoints "
                "WHERE family='row_effect' AND group_key=CAST(? AS TEXT)",
                (run_id,),
            ).fetchone()[0]
            == 0
        )

        halt = False
        completed_rows = {
            int(row[0])
            for row in project.db.execute(
                "SELECT DISTINCT row_id FROM results WHERE run_id=? AND outcome='ok'",
                (run_id,),
            ).fetchall()
        }
        pending_rows = [
            int(row[0])
            for row in project.db.execute(
                "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (sheet,)
            ).fetchall()
            if int(row[0]) not in completed_rows
        ]
        assert len(completed_rows) == 1
        resumed = run_action_spec(
            project,
            {
                **action,
                "scope": {**action["scope"], "row_ids": pending_rows},
                "replace_existing": True,
                "idempotency_key": "local-recover",
            },
            project_id="local-halt",
        )

        assert resumed.status == "completed", resumed
        assert resumed.run_id != run_id
        assert not resumed.errors
        assert calls == 3
        transcript = next(
            c for c in project.columns(sheet) if c["name"] == "transcript"
        )
        assert sorted(project.get_values(sheet, transcript["id"]).values()) == [
            "transcript 1",
            "transcript 3",
        ]
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM effect_checkpoints "
                "WHERE family='row_effect' AND group_key=CAST(? AS TEXT)",
                (run_id,),
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()
