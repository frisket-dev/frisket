"""``replay_strict`` is only a no-spend proof when a cache is actually attached.

The strict-replay exemption exists because a strict-replay cache MISS raises
``CacheMiss`` before egress, so an LLM row in that posture can neither spend
provider money nor book a hosted meter.  That proof is carried by
``ModelRouter._complete_transport``, and it is guarded on ``self.cache is not
None``: with ``cache=None`` BOTH replay branches are skipped entirely and the
call falls straight through to ``_call_with_retry`` and the live adapter.

So the mode string alone proves nothing.  ``frisket.operability.diagnostics.
replay_mode_report`` has always read both axes (mode AND cache-attached), but
the money-path authorities read the mode only, and a cacheless strict-replay
router therefore looked free to all of them at once:

- the paidness predicate (``row_effect_spends_or_meters``) — no reservation,
  so a crash after the provider returns buys the row again on retry;
- validation's confirmation gate — a paid run launched with no consent echo;
- validation's provider spend cap — an at-or-over-cap key billed anyway;
- MapRunner's row-effect fence — no checkpoint around the live call;
- the reduce family's checkpoint gate — the unfenced group loop.

Every test here pairs the cacheless case (money moves, so the gate must bind)
with the cached case (the genuine proof, so the exemption must survive).
"""

from __future__ import annotations

import asyncio
import copy

import pytest

from frisket.ai.llm import LLMResponse, ModelRouter
from frisket.ai.llm.cache import ResponseCache
from frisket.ai.llm.types import LLMRequest
from frisket.engine.runner import CostGate, MapRunner, ProviderSpendCapExceeded
from frisket.engine.runner.network_policy import row_effect_spends_or_meters
from frisket.engine.store import Project
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.execution.provider import (
    ExecutionCompositionContext,
    open_execution_composition,
)
from helpers import write_claimless_test_model_calls
from frisket.team.security.secrets import encrypt_secret, key_hint
from typed_model_fixtures import model_plan, prepare_model_run, run_with_output_claim

CAP_MICRO = 25_000_000
REMOTE_MODEL = "anthropic/claude-haiku-4-5"


class _LiveAdapter:
    """Stands in for a real provider adapter: counts what reached the wire."""

    api_key = "sk-ant-test"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request, _client):  # noqa: ANN001
        self.calls += 1
        return LLMResponse(
            content='{"relevance": 7}',
            data={"relevance": 7},
            tokens_in=10,
            tokens_out=5,
            cost=0.5,
            model=request.model,
        )


def _strict_router(*, cache: ResponseCache | None) -> tuple[ModelRouter, _LiveAdapter]:
    router = ModelRouter(
        keys={"anthropic": "sk-ant-test"},
        cache=cache,
        cache_mode="replay_strict",
        use_env_keys=False,
    )
    adapter = _LiveAdapter()
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    return router, adapter


def _spec(sheet_id: int, model: str = REMOTE_MODEL) -> dict:
    return {
        "action_kind": "map.classify",
        "model": model,
        "sheet_id": sheet_id,
        "input_columns": ["text"],
        "context": "Test rows.",
        "fields": [
            {"name": "relevance", "type": "score", "description": "0-10 relevance"}
        ],
    }


def _agent_spec(sheet_id: int) -> dict:
    """The composite case: an LLM recipe whose loop ALSO calls live tools.
    Its canonical action kind carries the ``external:web_search`` catalog tag;
    the private implementation name is never an authorization input."""
    return {
        "action_kind": "research.answer",
        "model": REMOTE_MODEL,
        "sheet_id": sheet_id,
        "input_columns": ["text"],
        "question": {"text": "Who runs {{text}}?"},
    }


def _project(tmp_path, name: str, *, cap_micro: int | None = CAP_MICRO) -> Project:
    project = Project.create(tmp_path / f"{name}.frisket", name=name)
    project.set_provider_key(
        provider="anthropic",
        encrypted=encrypt_secret("sk-ant-test"),
        hint=key_hint("sk-ant-test"),
        spend_cap_micro=cap_micro,
    )
    return project


def _seed_rows(project: Project) -> int:
    sheet_id = project.add_sheet("data")
    cols = {"text": project.add_column(sheet_id, "text")}
    project.add_rows(sheet_id, [{"text": "one billable row"}], cols)
    return sheet_id


def _spend_to(project: Project, micro: int) -> None:
    """Move accrued spend to an exact figure through the REAL accrual path."""
    from frisket.engine.store.runs import RunResultStore

    sheet_id = project.add_sheet("accrual")
    cols = {"text": project.add_column(sheet_id, "text")}
    project.add_rows(sheet_id, [{"text": "hello there"}], cols)
    op_id = project.append_op("map", {"recipe": "accrual_seed"}, label="accrual")
    store = RunResultStore(project)
    run_id = store.start_run(op_id, sheet_id, "test.accrual_seed")
    write_claimless_test_model_calls(
        project,
        run_id,
        [
            {
                "row_id": 1,
                "column_id": 1,
                "model_calls": [
                    {
                        "fact_version": "frisket.model-call-fact.v1",
                        "capability": "llm.complete",
                        "engine": f"anthropic/{REMOTE_MODEL}",
                        "provider": "anthropic",
                        "provider_kind": "chat_api",
                        "credential_source": "project_key",
                        "provider_reported_cost_usd": micro / 1_000_000,
                        "provider_cost_usd": micro / 1_000_000,
                        "cost_source": "pricing_data",
                        "units": {"tokens_in": 100, "tokens_out": 50},
                    }
                ],
            }
        ],
    )
    assert project.provider_spend_state("anthropic").spent_micro == micro


def _prepare_after_exact_confirmation(runner: MapRunner, spec: dict):
    try:
        return prepare_model_run(runner, spec, confirmed=False)
    except CostGate as gate:
        assert gate.promise_set_hash is not None
        retry = copy.deepcopy(spec)
        retry["consented_promise_set_hash"] = gate.promise_set_hash
        return prepare_model_run(runner, retry, confirmed=True)


# ---------------------------------------------------------------------------
# Ground truth: what the router actually does.


def test_cacheless_strict_replay_reaches_the_live_adapter() -> None:
    """THE fact every gate below is judged against.

    ``_complete_transport``'s strict-replay branch is guarded on ``self.cache
    is not None``.  With no cache the guard is false, the ``CacheMiss`` raise
    is never reached, and the request goes to the adapter and is billed.
    """
    router, adapter = _strict_router(cache=None)
    response = asyncio.run(
        router.complete(
            LLMRequest(model=REMOTE_MODEL, messages=[{"role": "user", "content": "hi"}])
        )
    )
    assert adapter.calls == 1, "a cacheless replay_strict call reached the provider"
    assert response.cost == 0.5


def test_strict_replay_with_a_cache_raises_instead_of_calling(tmp_path) -> None:
    """The genuine proof: cache attached, miss raises before any egress."""
    from frisket.ai.llm.cache import CacheMiss

    router, adapter = _strict_router(cache=ResponseCache(tmp_path / "cache.db"))
    with pytest.raises(CacheMiss):
        asyncio.run(
            router.complete(
                LLMRequest(
                    model=REMOTE_MODEL, messages=[{"role": "user", "content": "hi"}]
                )
            )
        )
    assert adapter.calls == 0


# ---------------------------------------------------------------------------
# Authority 1: the paidness predicate.


def test_predicate_calls_cacheless_strict_replay_paid() -> None:
    """A row whose effect can reach a billed provider spends, whatever the
    mode string says.  Restore the mode-only ``live_calls_possible`` branch
    and this goes red — the exact state in which MapRunner installs no
    reservation and a post-response crash re-buys the row."""
    router, _ = _strict_router(cache=None)
    assert (
        row_effect_spends_or_meters(
            model_plan(_spec(1)).program, model_plan(_spec(1)).spec_dict(), router
        )
        is True
    )


def test_predicate_keeps_the_exemption_when_a_cache_is_attached(tmp_path) -> None:
    router, _ = _strict_router(cache=ResponseCache(tmp_path / "cache.db"))
    assert (
        row_effect_spends_or_meters(
            model_plan(_spec(1)).program, model_plan(_spec(1)).spec_dict(), router
        )
        is False
    )


def test_predicate_exemption_is_scoped_to_the_model_call(tmp_path) -> None:
    """The exemption is a fact about the MODEL invocation, so it only applies
    where the model call IS the effect.  A non-LLM remote effect is unaffected
    by any cache mode."""
    router, _ = _strict_router(cache=ResponseCache(tmp_path / "cache.db"))
    local = _spec(1, model="ollama/@test-local/llama3")
    plan = model_plan(local)
    # A local model is free on its own merits, not via the replay exemption.
    assert row_effect_spends_or_meters(plan.program, plan.spec_dict(), router) is False


def test_research_answer_stays_paid_when_only_its_model_call_replays(tmp_path) -> None:
    """The exemption must not cover a recipe's non-model subeffects.

    ``research.answer`` is an LLM recipe whose agent loop calls live web
    search and page fetch tools.  Replaying the MODEL response from a strict
    cache says nothing about those calls — they still leave the machine, still
    cost money, and still need a checkpoint. Its canonical server-owned action
    kind declares ``external:web_search``, so the model-scoped exemption keeps
    it paid without consulting the private recipe name.

    Exempt the whole recipe on the mode string and this goes red.
    """
    plan = model_plan(_agent_spec(1))
    for cache in (None, ResponseCache(tmp_path / "cache.db")):
        router, _ = _strict_router(cache=cache)
        assert (
            row_effect_spends_or_meters(plan.program, plan.spec_dict(), router) is True
        )


# ---------------------------------------------------------------------------
# Authority 2: validation's confirmation gate.


def test_cacheless_strict_replay_requires_confirmation(tmp_path, monkeypatch) -> None:
    """Consent is not misleading when confirming really does buy the call."""
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    project = _project(tmp_path, "confirm", cap_micro=None)
    try:
        sheet_id = _seed_rows(project)
        router, _ = _strict_router(cache=None)
        runner = MapRunner(project, router, authority=UnroutedOnlyAuthority(project))
        with pytest.raises(CostGate):
            prepare_model_run(runner, _spec(sheet_id), confirmed=False)
    finally:
        project.close()


def test_cached_strict_replay_still_skips_confirmation(tmp_path) -> None:
    """Confirming a cache-only posture cannot make a miss call the provider,
    so the prompt stays suppressed — the exemption's original point."""
    project = _project(tmp_path, "confirm-cached", cap_micro=None)
    try:
        sheet_id = _seed_rows(project)
        router, _ = _strict_router(cache=ResponseCache(tmp_path / "cache.db"))
        runner = MapRunner(project, router, authority=UnroutedOnlyAuthority(project))
        prepare_model_run(runner, _spec(sheet_id), confirmed=False)  # does not raise
    finally:
        project.close()


def test_cached_strict_replay_still_confirms_a_composite_recipe(
    tmp_path, monkeypatch
) -> None:
    """The confirmation exemption is scoped to the MODEL subeffect too.

    ``agent`` replays its model response from the strict cache, but its loop
    still makes LIVE search/fetch tool calls — the very thing
    ``confirmation_estimate`` flags via the server-owned catalog tag
    ``external:web_search``.  Validation used to promote the model-only proof
    to a whole-recipe one and suppress that estimate entirely, so this exact
    posture returned a prepared run with NO remote/cost consent while
    MapRunner's row-effect fence (correctly) still treated the row as paid.
    """
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    project = _project(tmp_path, "confirm-agent", cap_micro=None)
    try:
        sheet_id = _seed_rows(project)
        router, _ = _strict_router(cache=ResponseCache(tmp_path / "cache.db"))
        runner = MapRunner(project, router, authority=UnroutedOnlyAuthority(project))
        with pytest.raises(CostGate) as excinfo:
            prepare_model_run(runner, _agent_spec(sheet_id), confirmed=False)
        details = excinfo.value.estimate_details or {}
        assert details.get("requires_confirmation") is True
        assert details.get("remote_capability") == "external:http_fetch"
    finally:
        project.close()


def test_cached_strict_replay_backfill_confirms_a_composite_recipe(
    tmp_path, monkeypatch
) -> None:
    """Same correction on the backfill branch: a resume that EXTENDS the run
    to new rows re-gates them, and the model-only proof must not exempt the
    external subeffect there either."""
    from frisket.engine.runner import validation
    from frisket.engine.store.runs import RunResultStore
    from frisket.execution.pricing_policy import default_pricing_policy

    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    project = _project(tmp_path, "confirm-agent-backfill", cap_micro=None)
    try:
        sheet_id = _seed_rows(project)
        cols = {c["name"]: c["id"] for c in project.columns(sheet_id)}
        project.add_rows(sheet_id, [{"text": "a newly visible row"}], cols)
        router, _ = _strict_router(cache=ResponseCache(tmp_path / "cache.db"))
        store = RunResultStore(project)
        op_id = project.append_op(
            "map", {"action_kind": "research.answer"}, label="agent"
        )
        run_id = store.start_run(op_id, sheet_id, "research.answer", row_ids=[1])

        spec = {**_agent_spec(sheet_id), "row_ids": [2]}
        plan = model_plan(spec, project)
        with pytest.raises(CostGate) as excinfo:
            validation.validate_spec(
                project,
                router,
                store,
                plan.spec_dict(),
                program=plan.program,
                confirmed=False,
                resume_run_id=run_id,
                pricing_policy=default_pricing_policy(),
                composition=open_execution_composition(
                    project, router, ExecutionCompositionContext.direct()
                ),
            )
        details = excinfo.value.estimate_details or {}
        assert details.get("requires_confirmation") is True
        assert details.get("remote_capability") == "external:http_fetch"
    finally:
        project.close()


# ---------------------------------------------------------------------------
# Authority 3: validation's provider spend cap.


def test_cacheless_strict_replay_refuses_on_the_cap(tmp_path) -> None:
    """$26 spent against a $25 cap, and this posture really does bill.  The
    cap must answer.  Restore the mode-only ``live_provider_effect`` and the
    run launches and buys past the journalist's ceiling."""
    project = _project(tmp_path, "cap")
    try:
        _spend_to(project, 26_000_000)
        sheet_id = _seed_rows(project)
        router, _ = _strict_router(cache=None)
        runner = MapRunner(project, router, authority=UnroutedOnlyAuthority(project))
        with pytest.raises(ProviderSpendCapExceeded):
            _prepare_after_exact_confirmation(runner, _spec(sheet_id))
    finally:
        project.close()


def test_cached_strict_replay_keeps_its_cap_exemption(tmp_path) -> None:
    project = _project(tmp_path, "cap-cached")
    try:
        _spend_to(project, 26_000_000)
        sheet_id = _seed_rows(project)
        router, _ = _strict_router(cache=ResponseCache(tmp_path / "cache.db"))
        runner = MapRunner(project, router, authority=UnroutedOnlyAuthority(project))
        _prepare_after_exact_confirmation(runner, _spec(sheet_id))  # does not raise
    finally:
        project.close()


# ---------------------------------------------------------------------------
# Authority 4: MapRunner's row-effect fence.


def test_cacheless_strict_replay_reserves_a_row_effect_checkpoint(
    tmp_path, monkeypatch
) -> None:
    """The crash-window fence.  Without a reservation, a process death between
    the provider's return and the result transaction leaves nothing durable,
    and the resume buys the row a second time."""
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    project = _project(tmp_path, "fence", cap_micro=None)
    try:
        sheet_id = _seed_rows(project)
        router, adapter = _strict_router(cache=None)
        runner = MapRunner(
            project,
            router,
            concurrency=1,
            authority=UnroutedOnlyAuthority(project),
        )
        spec = _spec(sheet_id)
        try:
            prepare_model_run(runner, spec, confirmed=False)
        except CostGate as gate:
            spec = {**spec, "consented_promise_set_hash": gate.promise_set_hash}

        # A completed row-effect checkpoint is CONSUMED (deleted) in the result
        # transaction, so counting surviving rows proves nothing about a
        # successful run. Count the reserve — the first durable write, and the
        # thing whose absence lets a post-response crash re-buy the row.
        reserve_calls = 0
        durable_reserve = runner.run_store.reserve_row_effect_checkpoint

        def counting_reserve(*args, **kwargs):  # noqa: ANN002, ANN003
            nonlocal reserve_calls
            reserve_calls += 1
            return durable_reserve(*args, **kwargs)

        runner.run_store.reserve_row_effect_checkpoint = counting_reserve  # type: ignore[method-assign]
        progress = asyncio.run(run_with_output_claim(runner, spec, confirmed=True))
        assert progress.failed == 0, "the run should complete against the fake adapter"
        assert adapter.calls == 1, "the provider really was called"
        assert reserve_calls == 1, "the billed row ran under a durable reservation"
    finally:
        project.close()


def test_cached_strict_replay_writes_no_checkpoint(tmp_path) -> None:
    """A genuinely cache-only run is $0 stakes: zero durable checkpoint
    writes, per Lane 2.1."""
    project = _project(tmp_path, "fence-cached", cap_micro=None)
    try:
        sheet_id = _seed_rows(project)
        cache = ResponseCache(tmp_path / "cache.db")
        router, adapter = _strict_router(cache=cache)
        runner = MapRunner(
            project,
            router,
            concurrency=1,
            authority=UnroutedOnlyAuthority(project),
        )
        reserve_calls = 0
        durable_reserve = runner.run_store.reserve_row_effect_checkpoint

        def counting_reserve(*args, **kwargs):  # noqa: ANN002, ANN003
            nonlocal reserve_calls
            reserve_calls += 1
            return durable_reserve(*args, **kwargs)

        runner.run_store.reserve_row_effect_checkpoint = counting_reserve  # type: ignore[method-assign]
        # Every row misses -> CacheMiss -> the row fails, spending nothing.
        asyncio.run(
            run_with_output_claim(
                runner,
                _spec(sheet_id),
                confirmed=True,
            )
        )
        assert adapter.calls == 0
        assert reserve_calls == 0, "a $0 cache-only run writes no checkpoint"
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM effect_checkpoints WHERE family='row_effect'"
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


# ---------------------------------------------------------------------------
# Authority 5: the reduce family's checkpoint gate.


def test_reduce_family_checkpoints_cacheless_strict_replay(tmp_path) -> None:
    from frisket.engine.executor.group_summary_runtime import (
        _reduce_group_summary_effect_checkpoints_active,
    )
    from frisket.engine.executor.group_summary_plan import GroupSummaryPlan

    params = GroupSummaryPlan(
        action_kind="reduce.group_summary",
        request={},
        sheet_id=1,
        input_columns=["text"],
        group_by="topic",
        model=REMOTE_MODEL,
        instruction="Summarize each topic.",
        target_sheet_name="summaries",
        group_column_name="group",
        row_count_column_name="rows",
        summary_column_name="summary",
        row_ids=None,
    )
    cacheless, _ = _strict_router(cache=None)
    cached, _ = _strict_router(cache=ResponseCache(tmp_path / "cache.db"))
    assert _reduce_group_summary_effect_checkpoints_active(params, cacheless) is True
    assert _reduce_group_summary_effect_checkpoints_active(params, cached) is False
