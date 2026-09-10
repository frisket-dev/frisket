"""Circuit-break repeated failures within one run.

Without a circuit breaker, a large map where EVERY row fails with the same
missing-key error still attempts every row, writing thousands of identical
error cells.
A systemic failure (bad key, dead provider, malformed spec) looks like an
unbroken failure STREAK from row one; burning the rest of the run adds
nothing but noise (and, for priced models, cost).

The MapRunner contract is:
- A consecutive-failure streak >= the halt threshold (default 10, a
  constructor knob; None disables) stops the run through the EXISTING
  cooperative-cancel path: queued rows drop, in-flight rows drain, partial
  results persist, status lands 'cancelled' — i.e. resumable via the same
  backfill that resumes a hand-cancelled run. No new terminal status.
- Successes RESET the streak: a run with interleaved failures (flaky rows,
  not a systemic fault) never halts.
- The halt is OBSERVABLE: RunProgress.halted_reason says how many
  consecutive failures tripped it, and the run row's params carry the same
  reason (the UI/receipt layer can surface "halted — fix and re-run").

Fake-model idiom follows tests/test_all_rows_failed_no_columns.py: a real
ModelRouter with a stub adapter injected into ``_adapters``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.ai.llm.types import LLMError
from frisket.engine.runner import CostGate, MapRunner
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from typed_model_fixtures import prepare_model_run, run_with_output_claim

MODEL = "anthropic/claude-haiku-4-5"


def _classify_spec(sheet_id: int) -> dict[str, Any]:
    return {
        "action_kind": "map.classify",
        "model": MODEL,
        "sheet_id": sheet_id,
        "input_columns": ["text"],
        "context": "Test rows.",
        "fields": [
            {"name": "relevance", "type": "score", "description": "0-10 relevance"}
        ],
    }


class _ScriptedAdapter:
    """Fails or succeeds per call according to ``script`` (True = fail)."""

    def __init__(self, script):
        self.script = script
        self.calls = 0

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        del client
        index = self.calls
        self.calls += 1
        if self.script(index):
            raise LLMError(f"model exploded on call {index}", status=500)
        return LLMResponse(
            content=json.dumps({"relevance": 5}),
            data={"relevance": 5},
            tokens_in=10,
            tokens_out=5,
            cost=0.0,
            model=req.model,
        )


def _runner(
    tmp_path, texts: list[str], script, **runner_kwargs
) -> tuple[Project, MapRunner, _ScriptedAdapter, int]:
    project = Project.create(tmp_path / "p.frisket", name="p")
    sheet = project.add_sheet("data")
    cols = {"text": project.add_column(sheet, "text")}
    project.add_rows(sheet, [{"text": t} for t in texts], cols)
    router = ModelRouter(
        keys={"anthropic": "k"},
        cache=None,
        cache_mode="off",
        use_env_keys=False,
        max_retries=1,
    )
    adapter = _ScriptedAdapter(script)
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    runner = MapRunner(
        project,
        router,
        concurrency=1,
        authority=UnroutedOnlyAuthority(project),
        **runner_kwargs,
    )
    return project, runner, adapter, sheet


def _run_confirmed(runner: MapRunner, spec: dict[str, Any]):
    """Exercise the exact 402 handshake before the circuit-breaker run.

    The canonical classify spec is bound to its explicit typed program by the
    fixtures on both the quote and the confirmed dispatch."""
    with pytest.raises(CostGate) as quote:
        prepare_model_run(runner, spec)
    assert quote.value.promise_set_hash
    confirmed_spec = {
        **spec,
        "consented_promise_set_hash": quote.value.promise_set_hash,
    }
    return asyncio.run(run_with_output_claim(runner, confirmed_spec, confirmed=True))


def test_ten_consecutive_failures_halt_the_run_resumably(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    texts = [f"row {i}" for i in range(50)]
    project, runner, adapter, sheet = _runner(tmp_path, texts, lambda i: True)

    progress = _run_confirmed(runner, _classify_spec(sheet))

    # halted well short of the full 50 — not one error cell per row
    assert progress.failed >= 10
    assert progress.completed < 50
    assert progress.cancelled is True
    assert progress.halted_reason is not None
    assert "10 consecutive" in progress.halted_reason

    run_row = RunResultStore(project).get_run(progress.run_id)
    assert run_row is not None
    assert run_row["status"] == "cancelled"  # the resumable terminal state
    params = json.loads(run_row["params"] or "{}")
    assert "10 consecutive" in params.get("halted_reason", "")


def test_interleaved_failures_never_trip_the_breaker(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    texts = [f"row {i}" for i in range(30)]
    project, runner, adapter, sheet = _runner(tmp_path, texts, lambda i: i % 2 == 0)

    progress = _run_confirmed(runner, _classify_spec(sheet))

    assert progress.cancelled is False
    assert progress.halted_reason is None
    assert progress.completed == 30
    assert progress.failed == 15


class _ByRowAdapter:
    """Outcome keyed by the ROW CONTENT (deterministic under concurrency —
    call order is not dispatch order): rows named in ``slow_success_rows``
    succeed after a sleep; every other row fails instantly."""

    def __init__(self, slow_success_rows: set[int], delay: float = 0.15):
        self.slow_success_rows = slow_success_rows
        self.delay = delay

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        del client
        import re

        match = re.search(r"row (\d+)", json.dumps(req.messages))
        row = int(match.group(1)) if match else -1
        if row in self.slow_success_rows:
            await asyncio.sleep(self.delay)
            return LLMResponse(
                content=json.dumps({"relevance": 5}),
                data={"relevance": 5},
                tokens_in=10,
                tokens_out=5,
                cost=0.0,
                model=req.model,
            )
        raise LLMError(f"model exploded on row {row}", status=500)


def test_streak_is_source_order_not_completion_order(tmp_path, monkeypatch) -> None:
    """A concurrency regression: under concurrency, a burst of
    FAST failures must not trip the breaker while EARLIER slow successes are
    still in flight. Source order here: row 0 success, rows 1-5 fail, row 6
    success, rows 7-13 fail — max source-order streak is 7, below the
    threshold of 10. Completion order would see 12 consecutive fast failures
    (both successes still sleeping) and trip; source-order evaluation stalls
    at each in-flight success and never does."""
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    texts = [f"row {i}" for i in range(14)]
    project = Project.create(tmp_path / "p.frisket", name="p")
    sheet = project.add_sheet("data")
    cols = {"text": project.add_column(sheet, "text")}
    project.add_rows(sheet, [{"text": t} for t in texts], cols)
    router = ModelRouter(
        keys={"anthropic": "k"},
        cache=None,
        cache_mode="off",
        use_env_keys=False,
        max_retries=1,
    )
    router._adapters["anthropic"] = _ByRowAdapter({0, 6})  # noqa: SLF001
    runner = MapRunner(
        project,
        router,
        concurrency=6,
        halt_after_consecutive_failures=10,
        authority=UnroutedOnlyAuthority(project),
    )

    progress = _run_confirmed(runner, _classify_spec(sheet))

    assert progress.cancelled is False
    assert progress.halted_reason is None
    assert progress.completed == 14
    assert progress.failed == 12


def test_threshold_none_disables_the_breaker(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    texts = [f"row {i}" for i in range(30)]
    project, runner, adapter, sheet = _runner(
        tmp_path, texts, lambda i: True, halt_after_consecutive_failures=None
    )

    progress = _run_confirmed(runner, _classify_spec(sheet))

    assert progress.cancelled is False
    assert progress.completed == 30
    assert progress.failed == 30
