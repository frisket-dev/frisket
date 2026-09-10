"""One mint for provider cost: ``runs.cost_actual`` is a projection.

``model_calls.provider_cost_usd`` (per call, honest NULL unknowns) and
``runs.cost_actual`` (per run) used to compute the same fact twice, with two
known divergences: cached calls were zeroed in one computation but not the
other, and ``embeddings.py`` OVERWROTE the run figure where every other
writer accumulated.  The run figure is now reprojected from the durable call
facts inside ``RunResultStore.write_model_calls`` — the only place model_calls
change — so each of those divergences is pinned here as impossible, not
patched.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from frisket.ai.models.metadata import (
    PROVIDER_COST_MAX_USD,
    PROVIDER_COST_TOTAL_MAX_USD,
    model_calls_cost_actual,
    provider_cost_total,
    provider_cost_value,
)
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from helpers import run_writer_authority_fixture


def _fact(call_id: str, cost: float | None, **overrides) -> dict:
    fact = {
        "id": call_id,
        "fact_version": "frisket.model-call-fact.v1",
        "capability": "classify",
        "engine": "fixture",
        "provider": "fixture",
        "provider_kind": "test",
        "model_ids": ["fixture-model"],
        "credential_source": "platform_key",
        "provider_cost_usd": cost,
    }
    fact.update(overrides)
    return fact


def _seed(tmp_path: Path) -> tuple[Project, RunResultStore, int, int, int]:
    project = Project.create(tmp_path / "cost.frisket", name="Cost")
    sheet_id = project.add_sheet("Rows")
    column_id = project.add_column(sheet_id, "out", type="text", ai_generated=True)
    project.add_rows(sheet_id, [{"out": None}, {"out": None}], {"out": column_id})
    row_ids = [
        int(row["id"])
        for row in project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (sheet_id,)
        )
    ]
    op_id = project.append_op("map", {"action_kind": "map.classify"}, label="cost rows")
    store = RunResultStore(project)
    run_id = store.start_run(op_id, sheet_id, "map.classify", total_rows=len(row_ids))
    return project, store, run_id, row_ids[0], column_id


def _run_cost(project: Project, run_id: int):
    return project.db.execute(
        "SELECT cost_actual FROM runs WHERE id=?", (run_id,)
    ).fetchone()["cost_actual"]


def _writer(project: Project, run_id: int, column_id: int) -> dict:
    return run_writer_authority_fixture(
        project,
        run_id,
        output_column_ids={column_id},
    ).kwargs()


def test_cached_call_never_accrues_even_when_the_batch_cost_disagrees(
    tmp_path: Path,
) -> None:
    """Former divergence 1: cached calls zeroed in one computation, not the
    other.  A cache fact may retain historical price provenance in
    ``provider_cost_usd``; the run figure must follow the facts' cache
    exclusion even when the batch's per-row ``cost`` field claims otherwise."""
    project, store, run_id, row_id, column_id = _seed(tmp_path)
    store.write_results(
        run_id,
        [
            {
                "row_id": row_id,
                "column_id": column_id,
                "value": "x",
                # Deliberately divergent batch figure: the old accrual summed
                # this field and would have charged the cached call's
                # historical price to the run.
                "cost": 0.06,
                "model_calls": [
                    _fact("live-1", 0.01),
                    _fact(
                        "cached-1",
                        0.05,
                        credential_source="cache",
                        cache={"hit": True},
                    ),
                ],
            }
        ],
        **_writer(project, run_id, column_id),
    )
    assert _run_cost(project, run_id) == pytest.approx(0.01)
    project.close()


def test_batch_cost_without_a_fact_never_lands(tmp_path: Path) -> None:
    """The second computation is dead: a per-row ``cost`` with no durable
    fact behind it cannot move the run figure."""
    project, store, run_id, row_id, column_id = _seed(tmp_path)
    store.write_results(
        run_id,
        [{"row_id": row_id, "column_id": column_id, "value": "x", "cost": 5.0}],
        **_writer(project, run_id, column_id),
    )
    assert _run_cost(project, run_id) == 0.0
    project.close()


def test_multi_fact_run_accumulates_instead_of_overwriting(tmp_path: Path) -> None:
    """Former divergence 2: ``embeddings.py`` overwrote ``cost_actual`` with
    each batch's single fact, so a multi-batch refresh kept only the LAST
    batch's cost.  Two sequential fact writes (the embeddings shape:
    ``write_model_calls`` once per provider batch) must project the sum, and
    the overwrite lever itself is gone."""
    project, store, run_id, row_id, column_id = _seed(tmp_path)
    for call_id, cost in (("batch-1", 0.002), ("batch-2", 0.003)):
        store.write_model_calls(
            run_id,
            [
                {
                    "row_id": row_id,
                    "column_id": column_id,
                    "model_calls": [_fact(call_id, cost)],
                }
            ],
            **_writer(project, run_id, column_id),
        )
    project.db.commit()
    assert _run_cost(project, run_id) == pytest.approx(0.005)
    # The blind-overwrite entry point must not come back.
    assert not hasattr(RunResultStore, "set_cost_actual")
    project.close()


def test_replayed_facts_do_not_double_count(tmp_path: Path) -> None:
    """A crash-replayed batch re-inserts the same call ids; the projection
    recomputes the same value instead of accruing a second time."""
    project, store, run_id, row_id, column_id = _seed(tmp_path)
    batch = [
        {
            "row_id": row_id,
            "column_id": column_id,
            "value": "x",
            "cost": 0.01,
            "model_calls": [_fact("replayed-1", 0.01)],
        }
    ]
    store.write_results(run_id, batch, **_writer(project, run_id, column_id))
    store.write_results(run_id, batch, **_writer(project, run_id, column_id))
    store.write_returned_call_accounting(
        run_id,
        batch,
        **_writer(project, run_id, column_id),
    )
    assert _run_cost(project, run_id) == pytest.approx(0.01)
    project.close()


def test_unknown_live_cost_projects_null_and_recovers_on_reconciliation(
    tmp_path: Path,
) -> None:
    """A run with one live unknown-cost call must present NULL, never a
    confident figure built from the calls we could price.  When the provider
    later reports the meter for the SAME call id, the projection recomputes
    and the run figure becomes exact again — an increment-only rollup could
    never leave NULL."""
    project, store, run_id, row_id, column_id = _seed(tmp_path)
    store.write_model_calls(
        run_id,
        [
            {
                "row_id": row_id,
                "column_id": column_id,
                "model_calls": [
                    _fact("known-1", 0.01),
                    _fact("pending-1", None),
                ],
            }
        ],
        **_writer(project, run_id, column_id),
    )
    project.db.commit()
    assert _run_cost(project, run_id) is None

    # Terminal polling enriches the accepted fact with its meter.
    store.write_model_calls(
        run_id,
        [
            {
                "row_id": row_id,
                "column_id": column_id,
                "model_calls": [_fact("pending-1", 0.02)],
            }
        ],
        **_writer(project, run_id, column_id),
    )
    project.db.commit()
    assert _run_cost(project, run_id) == pytest.approx(0.03)
    project.close()


def test_projection_matches_the_receipt_derivation(tmp_path: Path) -> None:
    """Parity fence: the SQL projection and the receipts' Python derivation
    (``model_calls_cost_actual``) are two spellings of one rule; they must
    answer identically over the same durable facts."""
    project, store, run_id, row_id, column_id = _seed(tmp_path)
    store.write_model_calls(
        run_id,
        [
            {
                "row_id": row_id,
                "column_id": column_id,
                "model_calls": [
                    _fact("p-1", 0.0001),
                    _fact("p-2", 0.0),
                    _fact("p-3", 0.02, credential_source="cache"),
                    _fact("p-4", 0.0399),
                ],
            }
        ],
        **_writer(project, run_id, column_id),
    )
    project.db.commit()
    derived = model_calls_cost_actual(store.model_calls(run_id))
    assert _run_cost(project, run_id) == pytest.approx(derived)
    assert derived == pytest.approx(0.04)

    # And the unknown case agrees on unknown, not on a number.
    store.write_model_calls(
        run_id,
        [
            {
                "row_id": row_id,
                "column_id": column_id,
                "model_calls": [_fact("p-5", None)],
            }
        ],
        **_writer(project, run_id, column_id),
    )
    project.db.commit()
    assert model_calls_cost_actual(store.model_calls(run_id)) is None
    assert _run_cost(project, run_id) is None
    project.close()


def test_the_cost_domain_refuses_values_it_will_not_treat_as_money(
    tmp_path: Path,
) -> None:
    """One domain, applied where costs are WRITTEN.

    ``ai/models/metadata.provider_cost_value`` decides what a provider cost
    means, and the writers refuse a STATED value it cannot value rather than
    storing it for four downstream surfaces to reinterpret independently. NULL
    stays legal: it is the honest unknown, not a bad value.
    """
    project, store, run_id, row_id, column_id = _seed(tmp_path)

    def _write(call_id: str, cost: float) -> None:
        store.write_model_calls(
            run_id,
            [
                {
                    "row_id": row_id,
                    "column_id": column_id,
                    "model_calls": [_fact(call_id, cost)],
                }
            ],
            **_writer(project, run_id, column_id),
        )

    for call_id, bad in (
        ("neg", -0.01),
        ("absurd", 1.1e308),
        ("inf", float("inf")),
    ):
        with pytest.raises(ValueError, match="provider_cost_usd"):
            _write(call_id, bad)
        project.db.rollback()
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM model_calls WHERE id=?", (call_id,)
            ).fetchone()[0]
            == 0
        )

    _write("honest-unknown", None)
    project.db.commit()
    assert _run_cost(project, run_id) is None
    project.close()


def test_sql_and_python_share_one_upper_bound(tmp_path: Path) -> None:
    """The two derivations used to pick their own limits.

    SQL treated anything above 1e308 as unknown; the Python derivation
    happily returned the number; ``capture_facts`` then rendered it 0.0. A
    legacy row above the bound (the writers refuse to create one now) must
    read the same way on both sides, so the SQL spelling binds the SAME
    constant the Python rule uses.
    """
    project, store, run_id, row_id, column_id = _seed(tmp_path)
    store.write_model_calls(
        run_id,
        [
            {
                "row_id": row_id,
                "column_id": column_id,
                "model_calls": [_fact("in-bounds", 0.02)],
            }
        ],
        **_writer(project, run_id, column_id),
    )
    project.db.commit()
    assert _run_cost(project, run_id) == pytest.approx(0.02)

    # A pre-domain row, inserted the way a legacy writer could have.
    project.db.execute(
        "INSERT INTO model_calls (id, fact_version, run_id, capability, engine, "
        "provider, provider_kind, credential_source, provider_cost_usd) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "legacy-absurd",
            "frisket.model-call-fact.v1",
            run_id,
            "classify",
            "fixture",
            "fixture",
            "test",
            "platform_key",
            PROVIDER_COST_MAX_USD * 1.1,
        ),
    )
    store._project_cost_actual(run_id)  # noqa: SLF001
    project.db.commit()
    assert _run_cost(project, run_id) is None
    assert model_calls_cost_actual(store.model_calls(run_id)) is None
    project.close()


def _facts_over_the_total_bound() -> list[float]:
    """The smallest set of individually in-domain costs whose total is not."""
    count = int(PROVIDER_COST_TOTAL_MAX_USD // PROVIDER_COST_MAX_USD) + 1
    return [PROVIDER_COST_MAX_USD] * count


def test_the_domain_bounds_the_sum_and_not_only_each_fact() -> None:
    """A per-fact maximum cannot prove an unbounded sum finite.

    The bound was 1e308 per fact with a comment claiming a sum therefore
    could not reach ``inf``. Two in-domain facts of 1e308 summed to ``inf``:
    SQL answered NULL, the Python derivation answered ``inf``, and the spend
    accrual raised OverflowError converting ``inf`` to micro-dollars. The
    domain now owns the ADDITION, not just the value.
    """
    assert provider_cost_total([1.5, 2.25]) == pytest.approx(3.75)
    # Unknown propagates rather than contributing zero.
    assert provider_cost_total([1.0, None]) is None
    # And an absurd TOTAL of individually in-domain facts is unknown, never
    # a number and never inf.
    assert provider_cost_total(_facts_over_the_total_bound()) is None
    # The per-fact bound is small enough that the aggregate bound is reached
    # long before a float runs out of headroom, so no reachable batch can
    # produce inf on either side.
    assert math.isfinite(PROVIDER_COST_TOTAL_MAX_USD + PROVIDER_COST_MAX_USD)
    assert PROVIDER_COST_MAX_USD < PROVIDER_COST_TOTAL_MAX_USD


def test_sql_and_python_agree_on_an_absurd_total(tmp_path: Path) -> None:
    """Parity for the AGGREGATE clause specifically.

    Both costs are individually acceptable, so the per-fact clause cannot
    reach this case; only bounded addition can, and both spellings must
    answer unknown together.
    """
    project, store, run_id, row_id, column_id = _seed(tmp_path)
    costs = _facts_over_the_total_bound()
    assert all(provider_cost_value(cost) is not None for cost in costs)
    project.db.executemany(
        "INSERT INTO model_calls (id, fact_version, run_id, capability, "
        "engine, provider, provider_kind, credential_source, "
        "provider_cost_usd) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                f"in-domain-fact-{index}",
                "frisket.model-call-fact.v1",
                run_id,
                "classify",
                "fixture",
                "fixture",
                "test",
                "platform_key",
                cost,
            )
            for index, cost in enumerate(costs)
        ],
    )
    store._project_cost_actual(run_id)  # noqa: SLF001
    project.db.commit()
    assert _run_cost(project, run_id) is None
    assert model_calls_cost_actual(store.model_calls(run_id)) is None
    project.close()


def test_two_absurd_costs_cannot_reach_inf_through_the_writer(
    tmp_path: Path,
) -> None:
    """Regression coverage for the reproduced failure.

    Two facts of 1e308 used to be written happily, return a delta of ``inf``
    to the progress display, leave ``runs.cost_actual`` NULL in SQL and
    ``inf`` in Python. A cost that large is a corrupt meter reading, so it
    refuses at the write, naming the knob — and nothing lands.
    """
    project, store, run_id, row_id, column_id = _seed(tmp_path)
    with pytest.raises(ValueError, match="provider_cost_usd"):
        store.write_model_calls(
            run_id,
            [
                {
                    "row_id": row_id,
                    "column_id": column_id,
                    "model_calls": [_fact("huge-1", 1e308), _fact("huge-2", 1e308)],
                }
            ],
            **_writer(project, run_id, column_id),
        )
    project.db.commit()
    assert project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
    assert _run_cost(project, run_id) == pytest.approx(0.0)
    assert model_calls_cost_actual(store.model_calls(run_id)) == pytest.approx(0.0)
    project.close()
