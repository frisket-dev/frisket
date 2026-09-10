"""`frisket reconcile` — the operator lever for stuck paid-effect checkpoints.

The lane-2.6 contract: `list` surfaces every stuck shape across the unified
store (ambiguous reserved, non-replayable returned, orphans), `discard` makes
the unit retryable end-to-end, `accept-charged` records the decision and
refuses a re-call, refusals are named, and payload bodies (which can hold
provider responses and key-looking strings) never reach the output.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from frisket.ai.llm import LLMResponse, ModelRouter
from frisket.cli.reconcile import reconcile
from frisket.engine.runner import CostGate, MapRunner
from frisket.engine.store import Project
from frisket.engine.store.effect_checkpoints import (
    OPERATOR_RESOLUTION_SCHEMA_VERSION,
    EffectCheckpointStore,
)
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from typed_model_fixtures import model_plan

# Planted in payloads and identities; must never appear in reconcile output.
FAKE_SECRET = "sk-ant-api03-SHOULD-NEVER-PRINT"


def _store_project(tmp_path) -> Project:
    return Project.create(tmp_path / "reconcile.frisket", name="reconcile")


def _live_run_id(project: Project) -> int:
    """A real runs row so a row_effect group has a live referent."""

    sheet = project.add_sheet("data")
    op_id = project.db.execute("INSERT INTO ops (kind) VALUES ('map')").lastrowid
    run_id = project.db.execute(
        "INSERT INTO runs (op_id, sheet_id, action_kind) VALUES (?, ?, 'map.classify')",
        (op_id, sheet),
    ).lastrowid
    project.db.commit()
    return int(run_id)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_surfaces_each_family_stuck_shape_and_orphans(tmp_path, capsys):
    project = _store_project(tmp_path)
    try:
        store = EffectCheckpointStore(project.db)
        run_id = _live_run_id(project)

        # Ambiguous reserved rows, one per family. model_call carries its
        # reserve-time recovery payload (where a secret could lurk).
        assert store.reserve(
            "cp-row-live",
            family="row_effect",
            group_key=str(run_id),
            unit_key="7",
            action_kind="classify",
            identity=f"digest-{FAKE_SECRET}",
        )
        assert store.reserve(
            "cp-row-orphan",
            family="row_effect",
            group_key="424242",
            unit_key="9",
            action_kind="classify",
            identity="digest-2",
        )
        assert store.reserve(
            "cp-model-call",
            family="model_call",
            group_key="entities.extract",
            unit_key="cp-model-call",
            action_kind="entities.extract",
            identity="cp-model-call",
            payload={"payload": {"api_key": FAKE_SECRET}, "provider_fact_id": None},
        )
        assert store.reserve(
            "cp-plugin",
            family="plugin_effect",
            group_key="ns.effect",
            unit_key="unit-1",
            action_kind="plugin.kind",
            identity="{}",
        )
        assert store.reserve(
            "cp-reduce",
            family="reduce_group_summary",
            group_key="params:abc",
            unit_key="group:def",
            action_kind="reduce.group_summary",
            identity="digest-3",
        )
        assert store.reserve(
            "cp-embed-orphan",
            family="embedding_index_refresh",
            group_key="idx-gone",
            unit_key="batch-1",
            action_kind="embeddings.refresh",
            identity="digest-4",
        )

        # A returned row_effect carrying a durable provider ERROR: stuck
        # (replay re-commits the error), so listed.
        assert store.reserve(
            "cp-row-error",
            family="row_effect",
            group_key=str(run_id),
            unit_key="8",
            action_kind="classify",
            identity="digest-5",
        )
        store.complete(
            "cp-row-error",
            family="row_effect",
            group_key=str(run_id),
            unit_key="8",
            action_kind="classify",
            identity="digest-5",
            payload={"field": {"error": f"provider said no ({FAKE_SECRET})"}},
        )

        # A returned SUCCESS with a live referent: automatic resume consumes
        # it free — not stuck, must NOT be listed.
        assert store.reserve(
            "cp-row-success",
            family="row_effect",
            group_key=str(run_id),
            unit_key="11",
            action_kind="classify",
            identity="digest-6",
        )
        store.complete(
            "cp-row-success",
            family="row_effect",
            group_key=str(run_id),
            unit_key="11",
            action_kind="classify",
            identity="digest-6",
            payload={"field": {"value": FAKE_SECRET, "cost": 0.1}},
        )

        # A returned row whose payload got corrupted: replay refuses forever,
        # so listed for any family.
        assert store.reserve(
            "cp-corrupt",
            family="reduce_group_summary",
            group_key="params:zzz",
            unit_key="group:zzz",
            action_kind="reduce.group_summary",
            identity="digest-7",
        )
        store.complete(
            "cp-corrupt",
            family="reduce_group_summary",
            group_key="params:zzz",
            unit_key="group:zzz",
            action_kind="reduce.group_summary",
            identity="digest-7",
            payload={"ok": True},
        )
        project.db.execute(
            "UPDATE effect_checkpoints SET payload='{not json' WHERE id='cp-corrupt'"
        )
        project.db.commit()

        # A consumed provider-audit row (the retained-forever plugin shape):
        # deliberately kept, not stuck, must NOT be listed.
        assert store.reserve(
            "cp-plugin-audit",
            family="plugin_effect",
            group_key="ns.done",
            unit_key="unit-2",
            action_kind="plugin.kind",
            identity="{}",
        )
        store.complete(
            "cp-plugin-audit",
            family="plugin_effect",
            group_key="ns.done",
            unit_key="unit-2",
            action_kind="plugin.kind",
            identity="{}",
            payload={"envelope": FAKE_SECRET},
        )
        store.consume_group_retained(
            family="plugin_effect",
            group_key="ns.done",
            action_kind="plugin.kind",
            audit=lambda raw: {"response_bytes": len(raw or "")},
        )
    finally:
        project.close()

    rc = reconcile(["list", "--project", str(tmp_path / "reconcile.frisket")])
    out = capsys.readouterr()
    assert rc == 0
    listed = out.out
    for stuck_id in (
        "cp-row-live",
        "cp-row-orphan",
        "cp-model-call",
        "cp-plugin",
        "cp-reduce",
        "cp-embed-orphan",
        "cp-row-error",
        "cp-corrupt",
    ):
        assert stuck_id in listed, stuck_id
    assert "cp-row-success" not in listed
    assert "cp-plugin-audit" not in listed
    # Orphans are flagged where the group referent is derivable and gone.
    orphan_lines = [line for line in listed.splitlines() if "ORPHANED" in line]
    assert any("cp-row-orphan" in line for line in orphan_lines)
    assert any("cp-embed-orphan" in line for line in orphan_lines)
    assert not any("cp-row-live" in line for line in orphan_lines)
    # One line per effect: family, kind, group/unit, state, age all present.
    row_line = next(line for line in listed.splitlines() if "cp-row-live" in line)
    for token in (
        "row_effect",
        "kind=classify",
        f"run={run_id}",
        "row=7",
        "state=reserved",
        "age=",
    ):
        assert token in row_line, token
    # Payload bodies (provider responses, keys) never reach the output.
    assert FAKE_SECRET not in out.out + out.err


def test_list_reports_nothing_stuck_and_rejects_non_bundle(tmp_path, capsys):
    project = _store_project(tmp_path)
    project.close()
    rc = reconcile(["list", "--project", str(tmp_path / "reconcile.frisket")])
    assert rc == 0
    assert "no stuck paid effects" in capsys.readouterr().out

    rc = reconcile(["list", "--project", str(tmp_path / "nope")])
    assert rc == 2
    assert "not a frisket bundle" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# end-to-end on the row_effect family (mirrors the engine crash-window tests)
# ---------------------------------------------------------------------------


class SimulatedProcessKill(BaseException):
    pass


class _KilledThenReturn:
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


def _spec(sheet_id: int) -> dict:
    return {
        "action_kind": "map.classify",
        "model": "anthropic/claude-haiku-4-5",
        "sheet_id": sheet_id,
        "input_columns": ["text"],
        "context": "Test rows.",
        "fields": [{"name": "relevance", "type": "score", "description": "0-10"}],
    }


def _runner(project: Project, router: ModelRouter) -> MapRunner:
    return MapRunner(
        project,
        router,
        concurrency=1,
        allow_action_lifecycle_only_recipes=True,
        authority=UnroutedOnlyAuthority(project),
    )


def _program(spec: dict):
    """Rebuild the typed classify program from its durable runner spec, the
    way the worker does for every dispatch and resume."""

    return model_plan(spec).program


def _confirmed_spec(runner: MapRunner, spec: dict) -> dict:
    bound = {**spec, **model_plan(spec).spec_dict()}
    with pytest.raises(CostGate) as challenge:
        runner.prepare_run(bound, program=_program(bound), confirmed=False)
    return {**bound, "consented_promise_set_hash": challenge.value.promise_set_hash}


def _age_run_attempts(project: Project, run_id: int) -> None:
    """Model the recovery detector observing old attempts and old progress."""

    project.db.execute(
        "UPDATE execution_attempts SET created_at=datetime('now', '-7 hours') "
        "WHERE run_id=?",
        (run_id,),
    )
    project.db.execute(
        "UPDATE model_calls SET created_at=datetime('now', '-7 hours') WHERE run_id=?",
        (run_id,),
    )
    project.db.execute(
        "UPDATE output_column_claims SET "
        "lease_expires_at=datetime('now', '-1 second') "
        "WHERE run_id=? AND status='active'",
        (run_id,),
    )
    project.db.commit()


def _ambiguous_run(
    project: Project,
) -> tuple[int, str, str, _KilledThenReturn, dict]:
    """A run whose one paid row died mid-flight: reserved, stuck by design."""

    sheet = project.add_sheet("data")
    columns = {"text": project.add_column(sheet, "text")}
    project.add_rows(sheet, [{"text": "unknown outcome"}], columns)
    provider = _KilledThenReturn()
    router = ModelRouter(keys={"anthropic": "test"}, cache_mode="off")
    router._adapters["anthropic"] = provider  # noqa: SLF001
    first = _runner(project, router)
    confirmed = _confirmed_spec(first, _spec(sheet))
    claim_token = "output-claim:test:cli-reconcile"
    claims, conflict = OutputColumnClaimStore(project).acquire(
        sheet_id=sheet,
        output_names=["relevance"],
        action_kind="classify",
        claim_token=claim_token,
        lease_seconds=6 * 60 * 60,
    )
    assert conflict is None
    assert len(claims) == 1
    prepared = first._prepare(  # noqa: SLF001 - raw runner contract probe
        confirmed,
        program=_program(confirmed),
        confirmed=True,
        resume_run_id=None,
    )
    assert (
        OutputColumnClaimStore(project).bind_to_run(
            claim_token=claim_token,
            run_id=prepared.run_id,
            expected_output_names=["relevance"],
        )
        == 1
    )
    with pytest.raises(SimulatedProcessKill):
        asyncio.run(
            first.run(
                confirmed,
                program=_program(confirmed),
                confirmed=True,
                prepared_run=prepared,
                claim_token=claim_token,
            )
        )
    run_id = prepared.run_id
    checkpoint_id = str(
        project.db.execute(
            "SELECT id FROM effect_checkpoints WHERE family='row_effect' "
            "AND group_key=CAST(? AS TEXT)",
            (run_id,),
        ).fetchone()[0]
    )
    return run_id, checkpoint_id, claim_token, provider, confirmed


def _resume(
    project: Project,
    provider,
    confirmed: dict,
    run_id: int,
    claim_token: str,
):
    _age_run_attempts(project, run_id)
    router = ModelRouter(keys={"anthropic": "test"}, cache_mode="off")
    router._adapters["anthropic"] = provider  # noqa: SLF001
    return asyncio.run(
        _runner(project, router).run(
            confirmed,
            program=_program(confirmed),
            confirmed=True,
            resume_run_id=run_id,
            claim_token=claim_token,
        )
    )


def _fresh_successor(project: Project, provider, confirmed: dict):
    router = ModelRouter(keys={"anthropic": "test"}, cache_mode="off")
    router._adapters["anthropic"] = provider  # noqa: SLF001
    runner = _runner(project, router)
    token = f"output-claim:test:cli-successor:{uuid.uuid4()}"
    claims, conflict = OutputColumnClaimStore(project).acquire(
        sheet_id=int(confirmed["sheet_id"]),
        output_names=["relevance"],
        action_kind="classify",
        claim_token=token,
        lease_seconds=6 * 60 * 60,
    )
    assert conflict is None and len(claims) == 1
    prepared = runner._prepare(  # noqa: SLF001 - exact fresh-run probe
        confirmed,
        program=_program(confirmed),
        confirmed=True,
        resume_run_id=None,
    )
    assert (
        OutputColumnClaimStore(project).bind_to_run(
            claim_token=token,
            run_id=prepared.run_id,
            expected_output_names=["relevance"],
        )
        == 1
    )
    return asyncio.run(
        runner.run(
            confirmed,
            program=_program(confirmed),
            confirmed=True,
            prepared_run=prepared,
            claim_token=token,
        )
    )


def test_discard_makes_the_ambiguous_row_retryable_end_to_end(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    bundle = tmp_path / "ambiguous.frisket"
    project = Project.create(bundle)
    try:
        run_id, checkpoint_id, claim_token, provider, confirmed = _ambiguous_run(
            project
        )

        # Stuck by design: resume refuses instead of re-buying.
        refused = _resume(project, provider, confirmed, run_id, claim_token)
        assert refused.halted_code == "external_effect_reconciliation_required"
        assert provider.calls == 1
        OutputColumnClaimStore(project).release(
            claim_token=claim_token, status="failed"
        )

        rc = reconcile(["list", "--project", str(bundle)])
        assert rc == 0
        assert checkpoint_id in capsys.readouterr().out

        # The operator decides: the effect did not happen. The unit is
        # retryable and resume buys it fresh — exactly one more call.
        rc = reconcile(["discard", checkpoint_id, "--project", str(bundle)])
        out = capsys.readouterr()
        assert rc == 0
        assert "retryable" in out.out

        resumed = _fresh_successor(project, provider, confirmed)
        assert resumed.run_id != run_id
        assert resumed.done is True
        assert resumed.halted_code is None
        assert resumed.completed == 1
        assert provider.calls == 2
    finally:
        project.close()


def test_accept_charged_records_the_decision_and_refuses_a_recall(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    bundle = tmp_path / "accepted.frisket"
    project = Project.create(bundle)
    try:
        run_id, checkpoint_id, claim_token, provider, confirmed = _ambiguous_run(
            project
        )

        rc = reconcile(["accept-charged", checkpoint_id, "--project", str(bundle)])
        out = capsys.readouterr()
        assert rc == 0
        assert "never be called again" in out.out
        # Honest limits are stated, not hidden.
        assert "cost" in out.out and "unknown" in out.out

        # The decision is durable: consumed with the operator attestation,
        # not a fabricated provider response.
        store = EffectCheckpointStore(project.db)
        decided = store.get(checkpoint_id)
        assert decided is not None
        assert decided["state"] == "consumed"
        assert (
            decided["payload"]["schema_version"] == OPERATOR_RESOLUTION_SCHEMA_VERSION
        )
        assert decided["payload"]["resolution"] == "accepted_charged"

        # Re-call is refused mechanically: the unit's reservation slot is
        # taken forever, and resume refuses rather than calling the provider.
        assert (
            store.reserve(
                "cp-fresh-attempt",
                family="row_effect",
                group_key=decided["group_key"],
                unit_key=decided["unit_key"],
                action_kind=decided["action_kind"],
                identity=decided["identity"],
            )
            is False
        )
        refused = _resume(project, provider, confirmed, run_id, claim_token)
        assert refused.halted_code == "external_effect_reconciliation_required"
        OutputColumnClaimStore(project).release(
            claim_token=claim_token, status="failed"
        )
        assert provider.calls == 1

        # The decision shows in list as decided (the stuck question drained),
        # and accepting twice refuses by name.
        rc = reconcile(["list", "--project", str(bundle)])
        assert rc == 0
        assert "decided accept-charged" in capsys.readouterr().out
        rc = reconcile(["accept-charged", checkpoint_id, "--project", str(bundle)])
        out = capsys.readouterr()
        assert rc == 1
        assert "invalid_checkpoint_state" in out.err

        # The operator may reverse their own attestation: discard, then the
        # row is genuinely retryable.
        rc = reconcile(["discard", checkpoint_id, "--project", str(bundle)])
        assert rc == 0
        capsys.readouterr()
        resumed = _fresh_successor(project, provider, confirmed)
        assert resumed.run_id != run_id
        assert resumed.done is True
        assert resumed.completed == 1
        assert provider.calls == 2
    finally:
        project.close()


# ---------------------------------------------------------------------------
# refusals and payload policy
# ---------------------------------------------------------------------------


def _returned_unit(
    store: EffectCheckpointStore, unit_key: str, payload: dict, group_key: str
) -> str:
    checkpoint_id = f"cp-{unit_key}"
    assert store.reserve(
        checkpoint_id,
        family="row_effect",
        group_key=group_key,
        unit_key=unit_key,
        action_kind="classify",
        identity=f"digest-{unit_key}",
    )
    store.complete(
        checkpoint_id,
        family="row_effect",
        group_key=group_key,
        unit_key=unit_key,
        action_kind="classify",
        identity=f"digest-{unit_key}",
        payload=payload,
    )
    return checkpoint_id


def test_discard_refusals_are_named_and_leave_rows_intact(tmp_path, capsys):
    bundle = tmp_path / "refuse.frisket"
    project = Project.create(bundle)
    try:
        store = EffectCheckpointStore(project.db)
        run_id = _live_run_id(project)
        success_id = _returned_unit(
            store,
            "31",
            {"field": {"value": FAKE_SECRET, "cost": 0.2}},
            str(run_id),
        )
        assert store.reserve(
            "cp-audit",
            family="plugin_effect",
            group_key="ns.x",
            unit_key="u1",
            action_kind="plugin.kind",
            identity="{}",
        )
        store.complete(
            "cp-audit",
            family="plugin_effect",
            group_key="ns.x",
            unit_key="u1",
            action_kind="plugin.kind",
            identity="{}",
            payload={"envelope": "raw"},
        )
        store.consume_group_retained(
            family="plugin_effect",
            group_key="ns.x",
            action_kind="plugin.kind",
            audit=lambda raw: {"response_bytes": len(raw or "")},
        )
    finally:
        project.close()
    args = ["--project", str(bundle)]

    # Unknown id: named, nothing guessed.
    rc = reconcile(["discard", "cp-does-not-exist", *args])
    out = capsys.readouterr()
    assert rc == 1
    assert "checkpoint_lost" in out.err

    # A replayable returned SUCCESS is paid truth resume consumes free:
    # discarding it would only buy the unit again. Named refusal, row kept.
    rc = reconcile(["discard", success_id, *args])
    out = capsys.readouterr()
    assert rc == 1
    assert "invalid_checkpoint_state" in out.err
    assert "durable successful response" in out.err
    assert FAKE_SECRET not in out.out + out.err

    # A consumed provider-audit row is immutable historical truth.
    rc = reconcile(["discard", "cp-audit", *args])
    out = capsys.readouterr()
    assert rc == 1
    assert "invalid_checkpoint_state" in out.err

    # accept-charged refuses returned rows: nothing unknown to accept.
    rc = reconcile(["accept-charged", success_id, *args])
    out = capsys.readouterr()
    assert rc == 1
    assert "already durable" in out.err

    project = Project(bundle)
    try:
        store = EffectCheckpointStore(project.db)
        kept = store.get(success_id)
        assert kept is not None and kept["state"] == "returned"
        audit = store.get("cp-audit")
        assert audit is not None and audit["state"] == "consumed"
    finally:
        project.close()


def test_discard_clears_returned_error_and_corrupt_and_orphaned_rows(tmp_path, capsys):
    bundle = tmp_path / "clearable.frisket"
    project = Project.create(bundle)
    try:
        store = EffectCheckpointStore(project.db)
        run_id = _live_run_id(project)
        # Durable provider error: the operator may clear it to retry fresh.
        error_id = _returned_unit(
            store, "41", {"field": {"error": f"boom {FAKE_SECRET}"}}, str(run_id)
        )
        # Corrupt replay payload: refuses replay forever without an operator.
        corrupt_id = _returned_unit(store, "42", {"ok": True}, str(run_id))
        project.db.execute(
            "UPDATE effect_checkpoints SET payload='{not json' WHERE id=?",
            (corrupt_id,),
        )
        project.db.commit()
        # Orphaned returned success: its run is gone, nothing can ever
        # consume it — discard removes the dead record.
        orphan_id = _returned_unit(
            store, "43", {"field": {"value": 1, "cost": 0.1}}, "424242"
        )
    finally:
        project.close()
    args = ["--project", str(bundle)]

    # Live-referent shapes report a retry path; the orphan reports removal of
    # a dead record instead (its run cannot be resumed), and neither claims
    # "facts stay booked" where none were (review A3): the error and corrupt
    # rows completed WITHOUT an accrue hook, and the orphan's facts belonged
    # to the deleted run.
    for checkpoint_id in (error_id, corrupt_id):
        rc = reconcile(["discard", checkpoint_id, *args])
        out = capsys.readouterr()
        assert rc == 0, out.err
        assert "retryable" in out.out
        assert "No spend facts were booked" in out.out
        assert FAKE_SECRET not in out.out + out.err
    rc = reconcile(["discard", orphan_id, *args])
    out = capsys.readouterr()
    assert rc == 0, out.err
    assert "no longer exists" in out.out and "dead record" in out.out
    assert "retryable" not in out.out
    assert FAKE_SECRET not in out.out + out.err

    project = Project(bundle)
    try:
        store = EffectCheckpointStore(project.db)
        assert store.get(error_id) is None
        assert store.get(corrupt_id) is None
        assert store.get(orphan_id) is None
    finally:
        project.close()


def test_discard_refuses_when_checkpoint_completes_between_read_and_delete(
    tmp_path, capsys, monkeypatch
):
    """TOCTOU regression (review F1): the CLI decides its payload policy on a
    snapshot read. If the supposedly-dead process's ``complete()`` lands in
    the gap before the store's delete, the ``expected_state`` pin must refuse
    (the operator decided on a 'reserved' row that no longer exists) instead
    of silently deleting the provider's durable paid response."""

    bundle = tmp_path / "race.frisket"
    project = Project.create(bundle)
    try:
        store = EffectCheckpointStore(project.db)
        run_id = _live_run_id(project)
    finally:
        project.close()
    unit = {
        "family": "row_effect",
        "group_key": str(run_id),
        "unit_key": "7",
        "action_kind": "classify",
        "identity": "digest-race",
    }
    project = Project(bundle)
    try:
        store = EffectCheckpointStore(project.db)
        assert store.reserve("cp-race", **unit)
    finally:
        project.close()

    real_discard = EffectCheckpointStore.operator_discard

    def complete_lands_first(self, checkpoint_id, *, expected_state):
        # The in-flight process was NOT dead: its paid response lands between
        # the CLI's policy read and the operator delete.
        self.complete(
            checkpoint_id,
            **unit,
            payload={"field": {"value": FAKE_SECRET, "cost": 0.1}},
        )
        return real_discard(self, checkpoint_id, expected_state=expected_state)

    monkeypatch.setattr(EffectCheckpointStore, "operator_discard", complete_lands_first)
    rc = reconcile(["discard", "cp-race", "--project", str(bundle)])
    out = capsys.readouterr()
    assert rc == 1
    assert "checkpoint_lost" in out.err
    assert "changed under this discard" in out.err
    assert FAKE_SECRET not in out.out + out.err

    # The durable paid response SURVIVES for automatic resume to consume.
    project = Project(bundle)
    try:
        survived = EffectCheckpointStore(project.db).get("cp-race")
        assert survived is not None
        assert survived["state"] == "returned"
    finally:
        project.close()


def test_reconcile_never_prints_payload_secrets_on_decisions(tmp_path, capsys):
    """A reserved model_call checkpoint carries its reserve-time recovery
    payload; deciding it must print metadata only."""

    bundle = tmp_path / "secrets.frisket"
    project = Project.create(bundle)
    try:
        store = EffectCheckpointStore(project.db)
        assert store.reserve(
            "cp-secret",
            family="model_call",
            group_key="entities.extract",
            unit_key="cp-secret",
            action_kind="entities.extract",
            identity="cp-secret",
            payload={
                "payload": {"headers": {"x-api-key": FAKE_SECRET}},
                "provider_fact_id": None,
            },
        )
    finally:
        project.close()
    args = ["--project", str(bundle)]

    rc = reconcile(["list", *args])
    out = capsys.readouterr()
    assert rc == 0 and "cp-secret" in out.out
    assert FAKE_SECRET not in out.out + out.err

    rc = reconcile(["accept-charged", "cp-secret", *args])
    out = capsys.readouterr()
    assert rc == 0
    assert FAKE_SECRET not in out.out + out.err

    rc = reconcile(["discard", "cp-secret", *args])
    out = capsys.readouterr()
    assert rc == 0
    assert FAKE_SECRET not in out.out + out.err


# ---------------------------------------------------------------------------
# defect 4: embedding.index_create's dimension-probe receipt-evidence marker
# (embeddings.py's bespoke reservation-as-receipt lifecycle, NOT
# EffectCheckpointStore) is a SECOND stuck-state shape reconcile must see.
# ---------------------------------------------------------------------------


def _stuck_probe_receipt(
    project: Project,
    *,
    receipt_id: str = "receipt_probe_stuck1",
    idempotency_key: str = "embedding.index_create@sha256:probe-stuck",
    attempt_id: str = "attempt_probe_stuck1",
) -> str:
    """Insert a receipt in the exact shape a crash between
    ``_mark_embedding_probe_egress_reserved`` and the provider's response
    leaves behind: 'running', an egress reservation evidence item, and NO
    returned checkpoint evidence item -- ambiguous, and (before defect 4's
    fix) invisible to `frisket reconcile`."""

    from frisket.contracts.action import Receipt, ReceiptEvidence
    from frisket.engine.store.receipts import receipt_body

    run_id = _live_run_id(project)
    receipt = Receipt(
        receipt_id=receipt_id,
        project_id="reconcile-probe",
        action_id="act_probe_stuck1",
        action_kind="embedding.index_create",
        idempotency_key=idempotency_key,
        params_hash="sha256:probe-stuck-params",
        status="running",
        run_id=run_id,
        op_ids=[run_id],
        evidence=[
            ReceiptEvidence(
                ref={
                    "kind": "embedding_dimension_probe_egress_reservation",
                    "run_id": 1,
                    "op_id": 1,
                    "attempt_id": attempt_id,
                    "provider_id": "openai",
                    "requested_model": "text-embedding-3-small",
                },
                retention="pinned",
            )
        ],
    )
    project.db.execute(
        "INSERT INTO receipts (id, action_kind, action_id, idempotency_key, "
        "params_hash, status, body) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            receipt.receipt_id,
            receipt.action_kind,
            receipt.action_id,
            receipt.idempotency_key,
            receipt.params_hash,
            receipt.status,
            receipt_body(receipt),
        ),
    )
    project.db.commit()
    return receipt_id


def test_list_surfaces_the_stuck_embedding_probe_receipt(tmp_path, capsys):
    bundle = tmp_path / "probe-list.frisket"
    project = Project.create(bundle)
    try:
        receipt_id = _stuck_probe_receipt(project)
    finally:
        project.close()

    rc = reconcile(["list", "--project", str(bundle)])
    out = capsys.readouterr()
    assert rc == 0
    assert receipt_id in out.out
    assert "ambiguous" in out.out
    assert "embedding_dimension_probe_egress" in out.out


def test_discard_makes_the_stuck_probe_receipt_retryable(tmp_path, capsys):
    bundle = tmp_path / "probe-discard.frisket"
    project = Project.create(bundle)
    try:
        receipt_id = _stuck_probe_receipt(project)
        idempotency_key = "embedding.index_create@sha256:probe-stuck"

        rc = reconcile(["discard", receipt_id, "--project", str(bundle)])
        out = capsys.readouterr()
        assert rc == 0
        assert "retryable" in out.out

        row = project.db.execute(
            "SELECT 1 FROM receipts WHERE id=?", (receipt_id,)
        ).fetchone()
        assert row is None  # the reservation is gone; a fresh dispatch can proceed

        rc = reconcile(["list", "--project", str(bundle)])
        out = capsys.readouterr()
        assert rc == 0
        assert receipt_id not in out.out
        assert idempotency_key not in out.out
    finally:
        project.close()


def test_accept_charged_decides_the_stuck_probe_receipt_and_refuses_recall(
    tmp_path, capsys
):
    bundle = tmp_path / "probe-accept.frisket"
    project = Project.create(bundle)
    try:
        receipt_id = _stuck_probe_receipt(project)

        rc = reconcile(["accept-charged", receipt_id, "--project", str(bundle)])
        out = capsys.readouterr()
        assert rc == 0
        assert "never be called again" in out.out
        assert "cost" in out.out and "unknown" in out.out

        row = project.db.execute(
            "SELECT status, body FROM receipts WHERE id=?", (receipt_id,)
        ).fetchone()
        assert row is not None
        assert row["status"] == "failed"  # terminal: no longer intercepts a retry
        assert "embedding_dimension_probe_operator_decision" in row["body"]

        # Decided, not stuck: it drops out of the "needs a decision" list.
        rc = reconcile(["list", "--project", str(bundle)])
        out = capsys.readouterr()
        assert rc == 0
        assert receipt_id not in out.out

        # A second decision on the same (now-terminal, no-longer-stuck)
        # receipt refuses by name -- it is no longer a live reservation at
        # all, so this is the same "nothing to accept" refusal an unknown
        # checkpoint id gets.
        rc = reconcile(["accept-charged", receipt_id, "--project", str(bundle)])
        out = capsys.readouterr()
        assert rc == 1
        assert "checkpoint_lost" in out.err
    finally:
        project.close()


def test_reconcile_never_prints_probe_receipt_body_secrets(tmp_path, capsys):
    """The probe-receipt marker's body can carry the same kind of provider
    metadata an effect_checkpoint payload does; reconcile prints ids/state
    only, never the receipt body."""

    bundle = tmp_path / "probe-secrets.frisket"
    project = Project.create(bundle)
    try:
        from frisket.contracts.action import Receipt, ReceiptEvidence
        from frisket.engine.store.receipts import receipt_body

        run_id = _live_run_id(project)
        receipt = Receipt(
            receipt_id="receipt_probe_secret1",
            project_id="reconcile-probe",
            action_id="act_probe_secret1",
            action_kind="embedding.index_create",
            idempotency_key="embedding.index_create@sha256:probe-secret",
            params_hash="sha256:probe-secret-params",
            status="running",
            run_id=run_id,
            op_ids=[run_id],
            evidence=[
                ReceiptEvidence(
                    ref={
                        "kind": "embedding_dimension_probe_egress_reservation",
                        "run_id": 1,
                        "op_id": 1,
                        "attempt_id": "attempt_probe_secret1",
                        "provider_id": "openai",
                        "requested_model": FAKE_SECRET,
                    },
                    retention="pinned",
                )
            ],
        )
        project.db.execute(
            "INSERT INTO receipts (id, action_kind, action_id, idempotency_key, "
            "params_hash, status, body) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                receipt.receipt_id,
                receipt.action_kind,
                receipt.action_id,
                receipt.idempotency_key,
                receipt.params_hash,
                receipt.status,
                receipt_body(receipt),
            ),
        )
        project.db.commit()
    finally:
        project.close()
    args = ["--project", str(bundle)]

    rc = reconcile(["list", *args])
    out = capsys.readouterr()
    assert rc == 0 and "receipt_probe_secret1" in out.out
    assert FAKE_SECRET not in out.out + out.err

    rc = reconcile(["accept-charged", "receipt_probe_secret1", *args])
    out = capsys.readouterr()
    assert rc == 0
    assert FAKE_SECRET not in out.out + out.err
