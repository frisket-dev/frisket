"""``model_calls.duration_ms``: per-call runtime, measured once, stored once.

Run-level wall time already lives on ``runs.started_at``/``finished_at``. What
was missing was the per-call grain — which of a run's provider calls was slow —
and the tempting way to get it (a wall-clock read at the fact-assembly site in
each recipe) would have measured rendering, media materialization and schema
repair as if the provider had spent that time.

So the number comes from the one place that sees both ends of the wire call:
the clock ``ModelRouter._call_with_retry`` was ALREADY running for its health
stats and trace. These pin the whole wire — router stamp, fact carry, durable
column — so cutting any link goes red.

No test here reads a clock. The store tests assert exact literals that never
touched one; the router test asserts only what a monotonic measurement can
guarantee without racing (present, non-negative, integral), which is the same
bound ``test_parakeet_session.py`` and ``test_rapidocr_recipe_scope.py`` use
for their perf_counter-derived fields.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from frisket.ai.llm.cache import ResponseCache, request_key
from frisket.ai.llm.router import ModelRouter
from frisket.ai.llm.types import LLMRequest, LLMResponse
from frisket.ai.models.metadata import (
    DURATION_MS_MAX,
    ModelCallMeta,
    duration_ms_value,
)
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from helpers import run_writer_authority_fixture, write_claimed_test_results


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "duration.frisket", name="duration")
    yield p
    p.close()


def _row_and_column(p: Project) -> tuple[int, int, int]:
    sheet = p.add_sheet("data")
    col = p.add_column(sheet, "text")
    p.add_rows(sheet, [{"text": "hello"}], {"text": col})
    out_col = p.add_column(sheet, "answer", ai_generated=True)
    row_id = p.db.execute("SELECT id FROM rows").fetchone()["id"]
    return sheet, row_id, out_col


def _fact(**overrides: Any) -> dict[str, Any]:
    fact = ModelCallMeta.provider_call(
        capability="llm.complete",
        engine="mock/model-a",
        provider="mock",
        provider_kind="chat_api",
        model_ids=["model-a"],
        credential_source="platform_key",
        provider_reported_cost_usd=0.001,
        provider_cost_usd=0.001,
        cost_source="pricing_data",
        units={"tokens_in": 10, "tokens_out": 2},
        warnings=[],
        duration_ms=1234,
    ).as_dict()
    fact["id"] = "call-duration-1"
    fact.update(overrides)
    return fact


def _stored_duration(project: Project, call_id: str) -> Any:
    row = project.db.execute(
        "SELECT duration_ms FROM model_calls WHERE id=?", (call_id,)
    ).fetchone()
    assert row is not None, f"no model_calls row for {call_id!r}"
    return row["duration_ms"]


# --------------------------------------------------------------------------
# The typed fact carries it, and the store makes it durable.
# --------------------------------------------------------------------------


def test_model_call_meta_emits_duration_on_as_dict() -> None:
    assert _fact()["duration_ms"] == 1234


def test_cache_hit_facts_cannot_claim_a_duration() -> None:
    """A cache hit made no provider call. ``cache_hit`` takes no
    ``duration_ms`` argument at all, so the honest NULL is structural rather
    than a rule someone has to remember."""
    hit = ModelCallMeta.cache_hit(
        capability="llm.complete",
        engine="mock/model-a",
        provider="mock",
        provider_kind="chat_api",
    ).as_dict()
    assert hit["duration_ms"] is None


def test_scoped_writer_persists_duration_ms(project) -> None:
    sheet, row_id, out_col = _row_and_column(project)
    op = project.append_op("map")
    store = RunResultStore(project)
    run = store.start_run(op, sheet, "test.complete", total_rows=1)
    write_claimed_test_results(
        project,
        run,
        [
            {
                "row_id": row_id,
                "column_id": out_col,
                "value": "hi",
                "cost": 0.001,
                "model_calls": [_fact()],
            }
        ],
    )
    assert _stored_duration(project, "call-duration-1") == 1234


def test_scoped_writer_stores_null_when_the_bracket_is_absent(project) -> None:
    """The unbracketed transports (sidecar, local worker, the not-yet-wired
    provider paths) must store NULL, never a fabricated zero — a zero would
    read as an instantaneous provider call."""
    sheet, row_id, out_col = _row_and_column(project)
    op = project.append_op("map")
    store = RunResultStore(project)
    run = store.start_run(op, sheet, "test.complete", total_rows=1)
    fact = _fact()
    del fact["duration_ms"]
    write_claimed_test_results(
        project,
        run,
        [
            {
                "row_id": row_id,
                "column_id": out_col,
                "value": "hi",
                "cost": 0.001,
                "model_calls": [fact],
            }
        ],
    )
    assert _stored_duration(project, "call-duration-1") is None


def test_unscoped_writer_persists_duration_ms(project) -> None:
    """Direct whole-project effects write through the second writer; a column
    added to one insert list and not the other is exactly the drift this
    catches."""
    RunResultStore(project).write_unscoped_model_calls(
        [_fact(id="call-unscoped-1")], row_id=None, column_id=None
    )
    assert _stored_duration(project, "call-unscoped-1") == 1234


# --------------------------------------------------------------------------
# Reconciliation: an accepted-but-unmetered fact's NULL is enrichable once.
# --------------------------------------------------------------------------


def _accepted_fact(**overrides: Any) -> dict[str, Any]:
    """An acceptance-boundary fact: unknown cost, unmeasured duration —
    exactly the shape Datalab's accepted-submission fact has before its
    first poll (ops/integrations/datalab.py's
    ``_accepted_submission_accounting``)."""
    fact = ModelCallMeta.provider_call(
        capability="document.convert",
        engine="datalab",
        provider="datalab",
        provider_kind="platform_api",
        model_ids=["datalab"],
        credential_source="platform_key",
        provider_reported_cost_usd=None,
        provider_cost_usd=None,
        cost_source="unknown",
        units={"requests": 1},
        warnings=[],
        duration_ms=None,
    ).as_dict()
    fact["id"] = "call-reconcile-1"
    fact.update(overrides)
    return fact


def test_reconcile_enriches_the_accepted_facts_null_duration(project) -> None:
    """The Datalab bracket: accepted with duration_ms NULL, later completed
    with the poll's bracketed elapsed time carried under the SAME call id —
    the terminal-enrichment path (COALESCE), not a second mint."""
    sheet, row_id, out_col = _row_and_column(project)
    op = project.append_op("map")
    store = RunResultStore(project)
    run = store.start_run(op, sheet, "test.complete", total_rows=1)
    authority = run_writer_authority_fixture(
        project,
        run,
        output_column_ids={out_col},
    )

    accepted = _accepted_fact()
    store.write_returned_call_accounting(
        run,
        [{"row_id": row_id, "column_id": out_col, "model_calls": [accepted]}],
        **authority.kwargs(),
    )
    assert _stored_duration(project, "call-reconcile-1") is None

    completed = _accepted_fact(
        provider_reported_cost_usd=0.01,
        provider_cost_usd=0.01,
        cost_source="provider_reported",
        duration_ms=1500,
    )
    store.write_returned_call_accounting(
        run,
        [{"row_id": row_id, "column_id": out_col, "model_calls": [completed]}],
        **authority.kwargs(),
    )
    assert _stored_duration(project, "call-reconcile-1") == 1500


def test_reconcile_never_overwrites_an_already_enriched_duration(project) -> None:
    """COALESCE, not an unconditional SET: once a duration lands, a later
    reconcile call (cost still unmetered) may not clobber it with a fresh
    reading."""
    sheet, row_id, out_col = _row_and_column(project)
    op = project.append_op("map")
    store = RunResultStore(project)
    run = store.start_run(op, sheet, "test.complete", total_rows=1)
    authority = run_writer_authority_fixture(
        project,
        run,
        output_column_ids={out_col},
    )

    accepted = _accepted_fact(id="call-reconcile-2")
    store.write_returned_call_accounting(
        run,
        [{"row_id": row_id, "column_id": out_col, "model_calls": [accepted]}],
        **authority.kwargs(),
    )

    first_enrichment = _accepted_fact(id="call-reconcile-2", duration_ms=1000)
    store.write_returned_call_accounting(
        run,
        [{"row_id": row_id, "column_id": out_col, "model_calls": [first_enrichment]}],
        **authority.kwargs(),
    )
    assert _stored_duration(project, "call-reconcile-2") == 1000

    second_enrichment = _accepted_fact(id="call-reconcile-2", duration_ms=9000)
    store.write_returned_call_accounting(
        run,
        [{"row_id": row_id, "column_id": out_col, "model_calls": [second_enrichment]}],
        **authority.kwargs(),
    )
    assert _stored_duration(project, "call-reconcile-2") == 1000


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, None),
        (1500, 1500),
        (12.9, 12),
        (0, 0),
        (-5, None),
        ("slow", None),
        (True, None),
        (float("inf"), None),
        # Past what a SQLite INTEGER holds. sqlite3 answers these with
        # OverflowError, not a constraint failure, so an unbounded domain
        # would have taken the whole INSERT down with it.
        (DURATION_MS_MAX, None),
        (DURATION_MS_MAX + 1, None),
        (10**30, None),
        ("1e100", None),
        (1e19, None),
        # ...and float() itself refuses an int this big ("too large to convert
        # to float"), which is why OverflowError is caught around the cast too.
        (10**400, None),
        # The bound is not off-by-a-magnitude: ordinary large values survive.
        (2**62, 2**62),
    ],
)
def test_duration_domain_never_refuses_a_paid_fact(raw, expected) -> None:
    """A duration is descriptive, so an unusable value degrades to "not
    measured" instead of raising — unlike ``validate_provider_cost``, which
    refuses, because a wrong COST is a money defect. Refusing here would
    discard a fact for a provider that was already paid."""
    assert duration_ms_value(raw) == expected


def test_no_duration_input_can_overflow_the_insert(project) -> None:
    """The fence, exercised against a REAL INSERT rather than the domain alone.

    ``float(DURATION_MS_MAX)`` rounds UP to ``2**63``, so a value the column
    could legitimately hold came back out of range; checking the bound after
    the narrowing would have let exactly that case through. Every input here
    must reach the column as NULL, and none may raise.
    """
    sheet, row_id, out_col = _row_and_column(project)
    op = project.append_op("map")
    store = RunResultStore(project)
    hostile = ["1e100", 10**30, DURATION_MS_MAX, DURATION_MS_MAX + 1, 1e19, 10**400]
    for index, raw in enumerate(hostile):
        run = store.start_run(op, sheet, "test.complete", total_rows=1)
        call_id = f"call-overflow-{index}"
        write_claimed_test_results(
            project,
            run,
            [
                {
                    "row_id": row_id,
                    "column_id": out_col,
                    "value": "hi",
                    "cost": 0.001,
                    "model_calls": [_fact(id=call_id, duration_ms=raw)],
                }
            ],
        )
        assert _stored_duration(project, call_id) is None, raw
        # The unscoped writer shares the domain but not the insert list.
        unscoped_id = f"call-overflow-unscoped-{index}"
        store.write_unscoped_model_calls(
            [_fact(id=unscoped_id, duration_ms=raw)], row_id=None, column_id=None
        )
        assert _stored_duration(project, unscoped_id) is None, raw


def test_garbage_duration_stores_null_rather_than_refusing_the_write(project) -> None:
    sheet, row_id, out_col = _row_and_column(project)
    op = project.append_op("map")
    store = RunResultStore(project)
    run = store.start_run(op, sheet, "test.complete", total_rows=1)
    write_claimed_test_results(
        project,
        run,
        [
            {
                "row_id": row_id,
                "column_id": out_col,
                "value": "hi",
                "cost": 0.001,
                "model_calls": [_fact(duration_ms="ages")],
            }
        ],
    )
    assert _stored_duration(project, "call-duration-1") is None


# --------------------------------------------------------------------------
# The router is the one site that measures it.
# --------------------------------------------------------------------------


@dataclass
class _ScriptedAdapter:
    """A scripted wire inside a REAL ModelRouter, so retry/cache/chaos are the
    actual router code (the technique tests/ai/test_structured_completer.py
    uses); only the transport is fake."""

    base_url: str = "http://mock"
    seen: list[Any] = field(default_factory=list)

    async def complete(self, req: Any, client: Any) -> LLMResponse:
        self.seen.append(req)
        return LLMResponse(
            content="ok",
            data=None,
            tokens_in=10,
            tokens_out=2,
            cost=0.001,
            model=req.model,
        )


def _request() -> LLMRequest:
    return LLMRequest(model="mock/model-a", messages=[{"role": "user", "content": "x"}])


def test_router_stamps_the_attempt_it_already_measured() -> None:
    router = ModelRouter(keys={}, max_retries=0)
    router._adapters["mock"] = _ScriptedAdapter()
    trace: list[dict] = []
    resp = asyncio.run(router._complete_transport(_request(), trace=trace))
    assert resp.duration_ms is not None
    assert isinstance(resp.duration_ms, int)
    assert resp.duration_ms >= 0
    # ONE measurement, not two: the fact and the trace line beside it are the
    # same number, so a receipt can never contradict the trace.
    attempts = [event for event in trace if event.get("event") == "attempt"]
    assert [event["latency_ms"] for event in attempts] == [resp.duration_ms]


def test_row_execution_carries_the_router_measurement_onto_the_fact() -> None:
    """The middle link: the LLM row path must read ``wire.duration_ms`` rather
    than timing itself. This assembly point also covers rendering, schema
    repair and throttle decay, none of which the provider spent time on."""
    from frisket.engine.runner.row_execution import _wire_accounting_meta

    live = LLMResponse(
        content="ok",
        data=None,
        tokens_in=10,
        tokens_out=2,
        cost=0.001,
        model="mock/model-a",
        provider="mock",
        credential_source="platform_key",
        duration_ms=321,
    )
    cached = LLMResponse(
        content="ok",
        data=None,
        tokens_in=10,
        tokens_out=2,
        cost=0.0,
        model="mock/model-a",
        provider="mock",
        cached=True,
        duration_ms=999,
    )
    meta = _wire_accounting_meta("mock/model-a", [live, cached])
    durations = [call["duration_ms"] for call in meta["model_calls"]]
    # The live call reports what the router measured; the cache hit reports
    # nothing, even though the replayed body still carried a stale number.
    assert durations == [321, None]


def test_replayed_response_reports_no_duration(tmp_path) -> None:
    """A cassette holds whatever the original live call took. Replaying it made
    no wire call, so re-reporting that number would be a fabricated fact — the
    same reason the router restamps ``credential_source`` on a hit."""
    cache = ResponseCache(tmp_path / "cache.db")
    req = _request()
    key = request_key(req, "1")
    cache.put(
        key,
        LLMResponse(
            content="ok",
            data=None,
            tokens_in=10,
            tokens_out=2,
            cost=0.001,
            model=req.model,
            duration_ms=987,
        ),
    )
    # The cassette really did carry the stale number, or this proves nothing.
    assert (
        json.loads(
            cache.db.execute(
                "SELECT response FROM llm_cache WHERE key=?", (key,)
            ).fetchone()[0]
        )["duration_ms"]
        == 987
    )

    router = ModelRouter(keys={}, max_retries=0, cache=cache, cache_mode="replay")
    router._adapters["mock"] = _ScriptedAdapter()
    resp = asyncio.run(router._complete_transport(req, recipe_version="1"))
    assert resp.cached is True
    assert resp.duration_ms is None
