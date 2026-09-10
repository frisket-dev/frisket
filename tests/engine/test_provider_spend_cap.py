"""The spend cap, made real.

Before this suite, `project_provider_keys.spend_cap_micro` and `spent_micro`
were a shipped lie: settable in Settings, rendered in two tables, incremented
by nothing and read by nothing. A journalist could set a $25 cap, spend all
month, and read "$0.00 spent" the whole time.

Two wires make it true, and each has a test here that goes red if the wire is
cut:

1. ACCRUAL — ``RunResultStore.write_model_calls`` charges every newly recorded
   provider call to the project key that paid for it. The attribution rule is
   ``credential_source == "project_key"``, which is exact rather than
   heuristic: the routers set that token only for providers taken from
   ``Project.provider_model_keys()``, so the provider string on the fact IS
   the primary key of the row to charge.
2. THE LAUNCH CHECK — ``validate_spec`` refuses to start a run whose provider
   key is already at or past its cap, OR whose cap cannot be enforced because
   the key has made calls of undeterminable price. Both name the knob.

There is deliberately NO mid-run halt and no reconsent for this external
provider-key budget: a run that crosses the cap while executing finishes, and
the provider's actual invoice appears in the accrued total. Only the launch
check refuses. This is distinct from Frisket's platform-retail 402, whose "up
to" amount caps the customer debit while Frisket absorbs metered variance.
"""

from __future__ import annotations

import copy
import json
import sqlite3

import pytest

from frisket.ai.llm import ModelRouter
from frisket.ai.llm.cache import ResponseCache
from frisket.engine.runner import (
    CostGate,
    MapRunner,
    ProviderSpendCapExceeded,
    ProviderSpendCapUnenforceable,
)
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.team.security.secrets import encrypt_secret, key_hint
from helpers import run_writer_authority_fixture, write_claimless_test_model_calls
from typed_model_fixtures import prepare_model_run

CAP_USD = 25.0
CAP_MICRO = 25_000_000


def _project(tmp_path, *, cap_micro: int | None = CAP_MICRO) -> Project:
    project = Project.create(tmp_path / "cap.frisket", name="Cap")
    project.set_provider_key(
        provider="anthropic",
        encrypted=encrypt_secret("sk-ant-test"),
        hint=key_hint("sk-ant-test"),
        spend_cap_micro=cap_micro,
    )
    return project


def _run_id(project: Project) -> int:
    sheet_id = project.add_sheet("accrual")
    cols = {"text": project.add_column(sheet_id, "text")}
    project.add_rows(sheet_id, [{"text": "hello there"}], cols)
    op_id = project.append_op("map", {"recipe": "accrual_seed"}, label="accrual")
    # A recipe name distinct from the launch tests' "classify" so those can
    # assert that a REFUSED launch created no run row of its own.
    return RunResultStore(project).start_run(op_id, sheet_id, "test.accrual_seed")


def _call(
    *,
    provider: str = "anthropic",
    credential_source: str = "project_key",
    cost_usd: float | None,
    call_id: str | None = None,
) -> dict:
    call: dict = {
        "fact_version": "frisket.model-call-fact.v1",
        "capability": "llm.complete",
        "engine": f"{provider}/test-model",
        "provider": provider,
        "provider_kind": "chat_api",
        "credential_source": credential_source,
        "provider_reported_cost_usd": cost_usd,
        "provider_cost_usd": cost_usd,
        "cost_source": "pricing_data" if cost_usd is not None else "unknown",
        "units": {"tokens_in": 100, "tokens_out": 50},
    }
    if call_id is not None:
        call["id"] = call_id
    return call


def _record(project: Project, run_id: int, calls: list[dict], *, row_id: int = 1):
    write_claimless_test_model_calls(
        project,
        run_id,
        [{"row_id": row_id, "column_id": 1, "model_calls": calls}],
    )


def _spend(project: Project, provider: str = "anthropic"):
    return project.provider_spend_state(provider)


def _run_cost(project: Project, run_id: int):
    return project.db.execute(
        "SELECT cost_actual FROM runs WHERE id=?", (run_id,)
    ).fetchone()["cost_actual"]


# ---------------------------------------------------------------------------
# Wire 1: accrual


def test_spend_accrues_and_accumulates_across_batches(tmp_path) -> None:
    """The headline defect: spend a real amount, read a real number.

    Cut the accrual call in write_model_calls and this goes red with the
    original bug's exact symptom — 0 after real spending.
    """
    project = _project(tmp_path)
    try:
        run_id = _run_id(project)
        assert _spend(project).spent_micro == 0

        _record(project, run_id, [_call(cost_usd=1.50)])
        assert _spend(project).spent_micro == 1_500_000

        _record(project, run_id, [_call(cost_usd=2.25)], row_id=2)
        assert _spend(project).spent_micro == 3_750_000

        # and it reaches the surface the journalist actually reads
        row = project.provider_key_catalog_rows()["anthropic"]
        assert row["spent_micro"] == 3_750_000
        assert row["unmetered_calls"] == 0
    finally:
        project.close()


def test_replayed_batch_does_not_charge_twice(tmp_path) -> None:
    """model_calls ignores an id conflict, so a crash-replayed batch writes no
    new facts. Accrual must drop exactly the same rows or a retry inflates
    the total."""
    project = _project(tmp_path)
    try:
        run_id = _run_id(project)
        batch = [_call(cost_usd=4.0, call_id="fixed-call-id")]

        _record(project, run_id, batch)
        _record(project, run_id, batch)
        _record(project, run_id, batch)

        assert _spend(project).spent_micro == 4_000_000
        assert project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 1
    finally:
        project.close()


def test_duplicate_call_ids_in_one_batch_accrue_once(tmp_path) -> None:
    """Deduplication and spend accrual must agree within one write batch.

    ``model_calls.id`` is a primary key, so two copies of one provider fact in
    the same batch persist as one call. Charging both candidates would make
    the cap ledger disagree with the provider-call ledger even though neither
    replay existed before this transaction began.
    """
    project = _project(tmp_path)
    try:
        run_id = _run_id(project)
        call = _call(cost_usd=4.0, call_id="same-batch-call-id")

        _record(project, run_id, [call, dict(call)])

        assert project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 1
        assert _spend(project).spent_micro == 4_000_000
    finally:
        project.close()


def test_a_fact_the_schema_rejects_accrues_nothing(tmp_path) -> None:
    """Replay dedup ignores ONE conflict; it must not swallow every constraint.

    The writer used to insert with a blanket ``INSERT OR IGNORE``, so a NOT
    NULL violation on a brand-new call id disappeared as quietly as a replay.
    The pre-insert id probe had not seen that id, so accrual treated it as
    newly recorded spend and charged the key $1, while ``_project_cost_actual``
    reprojected the run from facts that did not include it and minted $0. Cap
    said $1, run said $0, and no receipt fact existed to reconcile them.

    Accrual and the projection now read the rows the database ACCEPTED, and a
    non-id constraint failure raises instead of vanishing.
    """
    project = _project(tmp_path)
    try:
        run_id = _run_id(project)
        _record(project, run_id, [_call(cost_usd=2.0, call_id="durable")])
        project.db.commit()
        before = _spend(project)
        assert before.spent_micro == 2_000_000
        assert _run_cost(project, run_id) == pytest.approx(2.0)

        broken = _call(cost_usd=1.0, call_id="never-durable")
        broken["fact_version"] = None  # a NOT NULL column

        with pytest.raises(sqlite3.IntegrityError, match="fact_version"):
            _record(project, run_id, [broken], row_id=2)

        assert _spend(project) == before
        project.db.rollback()
        assert _spend(project) == before
        assert _run_cost(project, run_id) == pytest.approx(2.0)
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM model_calls WHERE id=?", ("never-durable",)
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_a_partial_batch_never_goes_durable_without_its_accrual(tmp_path) -> None:
    """The inverse of the defect above, opened by the fix for it.

    ``ON CONFLICT(id) DO NOTHING ... RETURNING id`` cannot run under
    ``executemany``, so the writer inserts ONE STATEMENT PER ROW. When row 2
    violates a non-id constraint, row 1 is already inserted and the raise
    happens before ``_accrue_project_key_spend`` and ``_project_cost_actual``
    ever run. ``write_results`` had its own savepoint, but
    ``write_returned_call_accounting`` — the production path after a
    cancellation race — called the writer directly and committed on success
    only: the caller's next commit published a paid fact that charged the key
    nothing and moved the run cost not at all. Cap said $2, facts said $3.
    """
    project = _project(tmp_path)
    try:
        run_id = _run_id(project)
        _record(project, run_id, [_call(cost_usd=2.0, call_id="durable")])
        project.db.commit()
        before = _spend(project)
        assert before.spent_micro == 2_000_000

        broken = _call(cost_usd=5.0, call_id="second-refused")
        broken["fact_version"] = None  # a NOT NULL column
        authority = run_writer_authority_fixture(
            project,
            run_id,
            claimless_direct_effect=True,
        )
        with pytest.raises(sqlite3.IntegrityError, match="fact_version"):
            RunResultStore(project).write_returned_call_accounting(
                run_id,
                [
                    {
                        "row_id": 1,
                        "column_id": 1,
                        "model_calls": [
                            _call(cost_usd=1.0, call_id="first-valid"),
                            broken,
                        ],
                    }
                ],
                **authority.kwargs(),
            )

        # Whatever anyone commits next, the batch left nothing behind.
        project.db.commit()
        assert _spend(project) == before
        assert _run_cost(project, run_id) == pytest.approx(2.0)
        durable = {
            row["id"]
            for row in project.db.execute("SELECT id FROM model_calls").fetchall()
        }
        assert durable == {"durable"}
    finally:
        project.close()


def test_a_partial_unscoped_batch_leaves_nothing_for_its_caller_to_commit(
    tmp_path,
) -> None:
    """Same row-at-a-time exposure in the unscoped twin, on the path where the
    caller owns the transaction (``commit=False``, the effect-checkpoint
    writers): the writer's own rollback never fires there, so the batch needs
    a savepoint of its own."""
    project = _project(tmp_path)
    try:
        store = RunResultStore(project)
        broken = _call(cost_usd=5.0, call_id="unscoped-second-refused")
        broken["fact_version"] = None
        with pytest.raises(sqlite3.IntegrityError, match="fact_version"):
            store.write_unscoped_model_calls(
                [_call(cost_usd=1.0, call_id="unscoped-first-valid"), broken],
                row_id=None,
                column_id=None,
                commit=False,
            )
        project.db.commit()
        assert _spend(project).spent_micro == 0
        assert project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
    finally:
        project.close()


def test_unscoped_writer_also_accrues_only_what_landed(tmp_path) -> None:
    """The unscoped twin has no run to reproject, so a silently dropped fact
    would leave an accrual with nothing anywhere to explain it."""
    project = _project(tmp_path)
    try:
        store = RunResultStore(project)
        store.write_unscoped_model_calls(
            [_call(cost_usd=2.0, call_id="unscoped-durable")],
            row_id=None,
            column_id=None,
        )
        before = _spend(project)
        assert before.spent_micro == 2_000_000

        broken = _call(cost_usd=1.0, call_id="unscoped-never-durable")
        broken["fact_version"] = None

        with pytest.raises(sqlite3.IntegrityError, match="fact_version"):
            store.write_unscoped_model_calls([broken], row_id=None, column_id=None)

        assert _spend(project) == before
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM model_calls WHERE id=?",
                ("unscoped-never-durable",),
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_unpriced_call_is_counted_not_silently_zero(tmp_path) -> None:
    """An unpriced model DID spend money; we just cannot say how much.

    Folding it into spent_micro as 0 is the `cost_actual REAL NOT NULL
    DEFAULT 0` defect — a real BYOK call rendering identically to a free
    local run. It gets its own counter instead.
    """
    project = _project(tmp_path)
    try:
        run_id = _run_id(project)
        _record(project, run_id, [_call(cost_usd=None)])

        spend = _spend(project)
        assert spend.spent_micro == 0
        assert spend.unmetered_calls == 1

        # a priced call alongside it accrues normally; the two stay distinct
        _record(project, run_id, [_call(cost_usd=3.0)], row_id=2)
        spend = _spend(project)
        assert spend.spent_micro == 3_000_000
        assert spend.unmetered_calls == 1
    finally:
        project.close()


def test_accepted_unknown_call_reconciles_into_spend_once(tmp_path) -> None:
    """A job accepted before its final meter moves, rather than duplicates.

    Acceptance first makes the cap unenforceable with one unmetered request.
    Terminal polling enriches that same fact id, replacing the uncertainty
    with spend exactly once while retaining the request evidence.
    """
    project = _project(tmp_path)
    try:
        run_id = _run_id(project)
        call_id = "accepted-provider-job"
        accepted = _call(cost_usd=None, call_id=call_id)
        accepted["units"] = {"requests": 1}
        _record(project, run_id, [accepted])

        spend = _spend(project)
        assert spend.spent_micro == 0
        assert spend.unmetered_calls == 1

        completed = _call(cost_usd=2.75, call_id=call_id)
        completed["units"] = {"requests": 1, "pages": 3}
        _record(project, run_id, [completed])
        _record(project, run_id, [completed])  # terminal replay is free

        spend = _spend(project)
        assert spend.spent_micro == 2_750_000
        assert spend.unmetered_calls == 0
        [fact] = RunResultStore(project).model_calls(run_id)
        assert fact["provider_cost_usd"] == pytest.approx(2.75)
        assert fact["cost_source"] == "pricing_data"
        assert json.loads(fact["units"]) == {"requests": 1, "pages": 3}
    finally:
        project.close()


def test_negative_terminal_cost_cannot_credit_the_cap(tmp_path) -> None:
    """Reconciliation may only ADD information, never refund the key.

    Terminal polling used to accept whatever number the provider handed back.
    A ``-0.50`` reached the ledger by the enrichment route: it was written onto
    the fact, SUBTRACTED from ``spent_micro`` and allowed to decrement
    ``unmetered_calls``, so an exhausted cap reopened by fifty cents. Both cost
    derivations then read a negative cost as unknown, so the run figure stayed
    NULL and nothing on any surface could show where the credit came from.
    Insertion already refused negative spend; this route has the same domain
    rule now, applied before anything is written.
    """
    project = _project(tmp_path)
    try:
        run_id = _run_id(project)
        _record(project, run_id, [_call(cost_usd=5.0, call_id="already-paid")])
        accepted = _call(cost_usd=None, call_id="accepted-provider-job")
        accepted["units"] = {"requests": 1}
        _record(project, run_id, [accepted], row_id=2)
        project.db.commit()
        before = _spend(project)
        assert (before.spent_micro, before.unmetered_calls) == (5_000_000, 1)

        refund = _call(cost_usd=-0.50, call_id="accepted-provider-job")
        refund["units"] = {"requests": 1}
        with pytest.raises(ValueError, match="provider_cost_usd"):
            _record(project, run_id, [refund], row_id=2)

        # Atomic refusal: nothing was written before the refusal, so neither
        # counter moved even before the caller's rollback.
        after = _spend(project)
        assert (after.spent_micro, after.unmetered_calls) == (5_000_000, 1)
        project.db.rollback()
        assert _spend(project) == before
        fact = project.db.execute(
            "SELECT provider_cost_usd, cost_source FROM model_calls WHERE id=?",
            ("accepted-provider-job",),
        ).fetchone()
        assert fact["provider_cost_usd"] is None
        assert _run_cost(project, run_id) is None
    finally:
        project.close()


@pytest.mark.parametrize(
    "credential_source", ["org_byok", "platform_key", "cache", "local", "none"]
)
def test_only_project_key_spend_accrues(tmp_path, credential_source: str) -> None:
    """Spend on someone else's credential is not this key's spend. A cache
    hit in particular costs the provider nothing."""
    project = _project(tmp_path)
    try:
        run_id = _run_id(project)
        _record(
            project,
            run_id,
            [_call(cost_usd=9.99, credential_source=credential_source)],
        )
        spend = _spend(project)
        assert spend.spent_micro == 0
        assert spend.unmetered_calls == 0
    finally:
        project.close()


def test_spend_on_an_unconfigured_provider_accrues_nowhere(tmp_path) -> None:
    """No configured key means no cap of ours to enforce and no row to
    charge — it must not land on some other provider's row."""
    project = _project(tmp_path)
    try:
        run_id = _run_id(project)
        _record(project, run_id, [_call(provider="openai", cost_usd=5.0)])
        assert _spend(project).spent_micro == 0
        assert _spend(project, "openai") is None
    finally:
        project.close()


def test_sub_micro_calls_accumulate_within_a_batch(tmp_path) -> None:
    """Rounding once per provider per batch, not per call, so a run of tiny
    calls does not truncate every one of them to zero."""
    project = _project(tmp_path)
    try:
        run_id = _run_id(project)
        # 10 calls at $0.0000004 each = $0.000004 => 4 micro-dollars
        _record(project, run_id, [_call(cost_usd=0.0000004) for _ in range(10)])
        assert _spend(project).spent_micro == 4
    finally:
        project.close()


def test_a_new_key_value_starts_a_fresh_spend_period(tmp_path) -> None:
    """Rotating the credential means the old accrual was spent on a
    different key at the provider, and the provider-side bill starts over
    with it. This is the recovery path both cap refusals name."""
    project = _project(tmp_path)
    try:
        run_id = _run_id(project)
        _record(project, run_id, [_call(cost_usd=5.0), _call(cost_usd=None)])
        assert _spend(project).spent_micro == 5_000_000
        assert _spend(project).unmetered_calls == 1

        project.set_provider_key(
            provider="anthropic",
            encrypted=encrypt_secret("sk-ant-rotated"),
            hint=key_hint("sk-ant-rotated"),
            spend_cap_micro=CAP_MICRO,
        )
        spend = _spend(project)
        assert spend.spent_micro == 0
        assert spend.unmetered_calls == 0
    finally:
        project.close()


def test_editing_the_cap_does_not_forgive_accrued_spend(tmp_path) -> None:
    """The 2026-07-26 live defect: `set_provider_key` zeroed both counters on
    EVERY save, so the settings form that edits a cap also forgave every
    dollar accrued against it — and the freshly lowered cap then had nothing
    to refuse. Observed as "$0.00 spent" on a key that had just billed.

    A cap that any edit resets is not a cap. Restore the unconditional
    `spent_micro=0` and this goes red.
    """
    project = _project(tmp_path)
    try:
        run_id = _run_id(project)
        _record(project, run_id, [_call(cost_usd=5.0), _call(cost_usd=None)])
        assert _spend(project).spent_micro == 5_000_000
        assert _spend(project).unmetered_calls == 1

        # the journalist TIGHTENS the cap, re-entering the same key
        project.set_provider_key(
            provider="anthropic",
            encrypted=encrypt_secret("sk-ant-test"),
            hint=key_hint("sk-ant-test"),
            spend_cap_micro=1_000_000,  # $25 -> $1
        )

        spend = _spend(project)
        assert spend.spent_micro == 5_000_000
        assert spend.unmetered_calls == 1
        assert spend.cap_micro == 1_000_000
        assert spend.over_cap  # and the tightened cap now bites

        runner, spec = _runner_and_spec(project)
        with pytest.raises(ProviderSpendCapExceeded):
            _prepare_after_exact_confirmation(runner, spec)
    finally:
        project.close()


def test_ciphertext_alone_never_decides_the_reset(tmp_path) -> None:
    """Encryption is randomized, so the same key encrypts differently every
    save. Comparing ciphertext would reset on every save — the original bug
    with an extra step — so the comparison is on plaintext."""
    project = _project(tmp_path)
    try:
        run_id = _run_id(project)
        _record(project, run_id, [_call(cost_usd=5.0)])
        first = project.db.execute(
            "SELECT encrypted FROM project_provider_keys WHERE provider='anthropic'"
        ).fetchone()["encrypted"]
        second = encrypt_secret("sk-ant-test")
        assert first != second, "ciphertext is expected to differ per encryption"

        project.set_provider_key(
            provider="anthropic",
            encrypted=second,
            hint=key_hint("sk-ant-test"),
            spend_cap_micro=CAP_MICRO,
        )
        assert _spend(project).spent_micro == 5_000_000
    finally:
        project.close()


# ---------------------------------------------------------------------------
# Wire 2: the launch check


def _classify_spec(sheet_id: int, model: str = "anthropic/claude-haiku-4-5") -> dict:
    """Runner-spec values bound to the typed ``map.classify`` program by
    ``prepare_model_run``, which is what the executor hands ``MapRunner``."""
    return {
        "action_kind": "map.classify",
        "engine": "llm",
        "model": model,
        "sheet_id": sheet_id,
        "input_columns": ["text"],
        "context": "test",
        "fields": [
            {"name": "relevance", "type": "score", "description": "0-10 relevance"}
        ],
    }


def _runner_and_spec(project: Project, *, router: ModelRouter | None = None):
    sheet_id = project.add_sheet("data")
    cols = {"text": project.add_column(sheet_id, "text")}
    project.add_rows(sheet_id, [{"text": "hello there"}], cols)
    router = router or ModelRouter(
        keys={"anthropic": "sk-ant-test"}, cache=None, cache_mode="off"
    )
    runner = MapRunner(project, router, authority=UnroutedOnlyAuthority(project))
    return runner, _classify_spec(sheet_id)


def _prepare_after_exact_confirmation(runner: MapRunner, spec: dict):
    """Exercise the real first-response -> exact-echo confirmation protocol.

    These tests target the spend-cap decision that follows consent. A bare
    ``confirmed=True`` is intentionally no longer sufficient: first obtain the
    current scope-and-quote hash, then retry the unchanged spec with that echo.
    """
    try:
        return prepare_model_run(runner, spec, confirmed=False)
    except CostGate as gate:
        assert gate.promise_set_hash is not None
        retry = copy.deepcopy(spec)
        retry["consented_promise_set_hash"] = gate.promise_set_hash
        return prepare_model_run(runner, retry, confirmed=True)


def _spend_to(project: Project, micro: int) -> None:
    """Move accrued spend to an exact figure through the REAL accrual path."""
    run_id = _run_id(project)
    _record(project, run_id, [_call(cost_usd=micro / 1_000_000)])
    assert _spend(project).spent_micro == micro


def test_launch_refuses_once_the_key_is_past_its_cap(tmp_path) -> None:
    """Spend past the cap, then try to launch. Cut the check in
    validate_spec and this goes red."""
    project = _project(tmp_path)
    try:
        _spend_to(project, 26_000_000)  # $26 against a $25 cap
        runner, spec = _runner_and_spec(project)

        with pytest.raises(ProviderSpendCapExceeded) as excinfo:
            _prepare_after_exact_confirmation(runner, spec)

        exc = excinfo.value
        assert exc.provider == "anthropic"
        assert exc.error_code == "provider_spend_cap_exceeded"
        # names the knob, and both numbers
        assert "$26.00" in str(exc)
        assert "$25.00" in str(exc)
        assert "spend cap" in str(exc)
        assert exc.details["setting"] == "spend_cap_usd"

        # refused BEFORE any run row existed — nothing queued, nothing spent
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM runs WHERE action_kind='map.classify'"
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_launch_refuses_under_the_default_replay_router(tmp_path) -> None:
    """The live-proof defect (2026-07-26): the cap check used to sit inside
    the missing-key guard, which exempts plain 'replay' with a cache
    configured — the DEFAULT local-tier router — so in the shipped app an
    over-cap run was accepted, queued and billed. Nothing here proves every
    target row is a cache hit, so money fails closed.

    Re-nest the cap check under `not cache_can_serve_replay` and this goes
    red while every other test in the file stays green.
    """
    project = _project(tmp_path)
    try:
        _spend_to(project, 26_000_000)
        runner, spec = _runner_and_spec(
            project,
            router=ModelRouter(
                keys={"anthropic": "sk-ant-test"},
                cache=ResponseCache(project.path / "project.cache.db"),
                cache_mode="replay",
            ),
        )
        with pytest.raises(ProviderSpendCapExceeded):
            _prepare_after_exact_confirmation(runner, spec)
    finally:
        project.close()


def test_replay_strict_never_refuses_on_the_cap(tmp_path) -> None:
    """replay_strict provably never calls a live adapter (a miss raises
    CacheMiss), so it spends nothing and keeps its exemption."""
    project = _project(tmp_path)
    try:
        _spend_to(project, 26_000_000)
        runner, spec = _runner_and_spec(
            project,
            router=ModelRouter(
                keys={"anthropic": "sk-ant-test"},
                cache=ResponseCache(project.path / "project.cache.db"),
                cache_mode="replay_strict",
            ),
        )
        _prepare_after_exact_confirmation(runner, spec)  # does not raise
    finally:
        project.close()


def test_the_cap_answers_before_the_missing_key(tmp_path) -> None:
    """An exhausted cap on a key the router cannot see must say so, not
    "no API key is configured" about a key that exists and just billed.

    Live 2026-07-26: the over-cap classify run failed every row with
    `missing_provider_key` while the journalist's working key sat in the
    project. Put the missing-key raise back above the cap check and this
    goes red.
    """
    project = _project(tmp_path)
    try:
        _spend_to(project, 26_000_000)
        # A router with NO key for the capped provider: the withheld-key
        # scene, whatever withheld it.
        runner, spec = _runner_and_spec(
            project, router=ModelRouter(keys={}, cache=None, cache_mode="off")
        )
        with pytest.raises(ProviderSpendCapExceeded):
            _prepare_after_exact_confirmation(runner, spec)
    finally:
        project.close()


def test_launch_refuses_exactly_at_the_cap(tmp_path) -> None:
    """A cap is a bound: reaching it is reaching it."""
    project = _project(tmp_path)
    try:
        _spend_to(project, CAP_MICRO)
        runner, spec = _runner_and_spec(project)
        with pytest.raises(ProviderSpendCapExceeded):
            _prepare_after_exact_confirmation(runner, spec)
    finally:
        project.close()


def test_launch_proceeds_while_under_the_cap(tmp_path) -> None:
    project = _project(tmp_path)
    try:
        _spend_to(project, 24_999_999)
        runner, spec = _runner_and_spec(project)
        _prepare_after_exact_confirmation(runner, spec)  # does not raise
    finally:
        project.close()


def test_no_cap_set_never_refuses(tmp_path) -> None:
    """A key with no cap is unbounded by the journalist's own choice."""
    project = _project(tmp_path, cap_micro=None)
    try:
        _spend_to(project, 900_000_000)  # $900, no cap
        runner, spec = _runner_and_spec(project)
        _prepare_after_exact_confirmation(runner, spec)  # does not raise
    finally:
        project.close()


def test_unpriced_spend_on_a_capped_key_refuses_as_unenforceable(tmp_path) -> None:
    """Amends the 2026-07-25 ruling ("undeterminable spend is RECORDED,
    never a refusal reason of its own"), which the 2026-07-26 keyed proof
    showed to be a hole: a real billed vision-OCR call rated unpriceable and
    the capped key went on reporting its pre-spend total as if that were the
    truth. A cap whose spend cannot be determined cannot be enforced, and a
    promise we cannot keep says so instead of passing.

    Not a fabricated floor: `CostBasis`'s vocabulary already distinguishes
    Unpriceable from priced and from operator-borne-zero, and the product
    has no fallback price anywhere on purpose. The refusal is about the CAP,
    not about the amount.
    """
    project = _project(tmp_path)
    try:
        run_id = _run_id(project)
        _record(project, run_id, [_call(cost_usd=None) for _ in range(50)])
        assert _spend(project).unmetered_calls == 50
        assert _spend(project).spent_micro == 0  # nowhere near the $25 cap

        runner, spec = _runner_and_spec(project)
        with pytest.raises(ProviderSpendCapUnenforceable) as excinfo:
            _prepare_after_exact_confirmation(runner, spec)

        exc = excinfo.value
        assert exc.error_code == "provider_spend_cap_unenforceable"
        assert exc.details["setting"] == "spend_cap_usd"
        assert exc.details["unmetered_calls"] == 50
        assert "no published price" in str(exc)
        assert "cannot be enforced" in str(exc)
        assert "$25.00" in str(exc)
    finally:
        project.close()


def test_unpriced_spend_on_an_uncapped_key_never_refuses(tmp_path) -> None:
    """The journalist bounded nothing, so there is nothing to fail to
    enforce: unpriced calls stay a recorded fact, not a refusal."""
    project = _project(tmp_path, cap_micro=None)
    try:
        run_id = _run_id(project)
        _record(project, run_id, [_call(cost_usd=None) for _ in range(50)])
        assert _spend(project).unmetered_calls == 50

        runner, spec = _runner_and_spec(project)
        _prepare_after_exact_confirmation(runner, spec)  # does not raise
    finally:
        project.close()


def test_exceeded_wins_over_unenforceable(tmp_path) -> None:
    """Both true at once: the exceeded refusal is the more specific answer
    and its copy already admits the total is a lower bound."""
    project = _project(tmp_path)
    try:
        run_id = _run_id(project)
        _record(project, run_id, [_call(cost_usd=30.0), _call(cost_usd=None)])
        runner, spec = _runner_and_spec(project)
        with pytest.raises(ProviderSpendCapExceeded):
            _prepare_after_exact_confirmation(runner, spec)
    finally:
        project.close()


def test_refusal_message_admits_the_total_is_a_lower_bound(tmp_path) -> None:
    project = _project(tmp_path)
    try:
        run_id = _run_id(project)
        _record(project, run_id, [_call(cost_usd=30.0), _call(cost_usd=None)])
        runner, spec = _runner_and_spec(project)

        with pytest.raises(ProviderSpendCapExceeded) as excinfo:
            _prepare_after_exact_confirmation(runner, spec)

        message = str(excinfo.value)
        assert "no published price" in message
        assert "higher" in message
        assert excinfo.value.details["unmetered_calls"] == 1
    finally:
        project.close()
