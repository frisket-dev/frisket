"""The route-observation point threaded through the fact writer.

``RunResultStore.write_model_calls`` is THE model-call fact writer; a
route-bound transcription fact arrives carrying the transient observation
payload and this suite proves, against a real bundle store:

- epoch stamping: the fact row's ``epoch_id`` links a ``binding_epochs`` row
  under the run's route; identical provenance dedupes to ONE epoch.
- divergence: an observed credential fact differing
  from the route row appends a SUCCESSOR route with the ACTUAL facts and
  opens the epoch under the successor. No credential promise is synthesized;
  the fact's settlement-visible ``credential_source`` is the ACTUAL value.
- play scene (deep-spec test 5): a redeploy mid-run (two worker revisions
  across a resume) yields TWO epochs under ONE route — per-row ``epoch_id``
  distinguishes provenance, receipts can derive mixed provenance, and NO
  violation is recorded (revision is observation-only, never promised).
- epoch invariant: a route-bound fact whose observation cannot be
  resolved fails loudly; a fact without route scope keeps the honest NULL.
"""

from __future__ import annotations

import frisket.sdk.ops.transcribe_engines as transcribe_engines

import pytest

from frisket.engine.store import Project
from frisket.engine.store.execution_routes import RouteStore
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.runs import RunResultStore
from frisket.execution.runtime_binding import (
    ROUTE_OBSERVATION_KEY,
    bind_fact_to_route,
)

PROMISES = [
    {
        "field": "egress_class",
        "op": "satisfies_order",
        "value": "third_party_api",
        "basis": None,
        "order_ref": "egress.v1",
        "audience": "user_claim",
    }
]


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "t.frisket", name="t")
    yield p
    p.close()


class _CallFact(dict):
    def as_dict(self) -> dict:
        return dict(self)


def _model_call_for(*args, **kwargs) -> _CallFact:
    return _CallFact(transcribe_engines.transcription_model_calls(*args, **kwargs)[0])


def _make_run(project) -> int:
    sheet_id = project.add_sheet("Media")
    output_column_id = project.add_column(
        sheet_id,
        "transcript",
        ai_generated=True,
    )
    row_ids = project.add_rows(sheet_id, [{}, {}], {})
    cur = project.db.execute("INSERT INTO ops (kind, spec) VALUES ('map', '{}')")
    op_id = int(cur.lastrowid)
    project.db.commit()
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "media.transcribe",
        row_ids=row_ids,
    )
    claim_token = f"output-claim:route-observation:{run_id}"
    claims, conflict = OutputColumnClaimStore(project).acquire(
        sheet_id=sheet_id,
        output_names=["transcript"],
        action_kind="media.transcribe",
        run_id=run_id,
        claim_token=claim_token,
    )
    assert conflict is None
    assert len(claims) == 1
    OutputColumnClaimStore(project).bind_to_run(
        claim_token=claim_token,
        run_id=run_id,
        expected_output_names=["transcript"],
    )
    assert int(claims[0]["column_id"]) == output_column_id
    attempt_id = f"attempt_route_observation_{run_id}"
    project.db.execute(
        "INSERT INTO execution_attempts "
        "(id, run_id, seq, state, action_identity_hash, scope_json, "
        "created_at) VALUES (?, ?, 0, 'dispatching', 'route-observation', "
        "?, datetime('now'))",
        (attempt_id, run_id, str(row_ids)),
    )
    project.db.execute(
        "UPDATE runs SET current_attempt_id=? WHERE id=?",
        (attempt_id, run_id),
    )
    project.db.commit()
    return run_id


def _persist_route(
    project,
    run_id: int,
    *,
    target_id: str,
    transport: str,
    engine: str,
    credential_source: str = "local",
):
    store = RouteStore.for_run(project, run_id)
    promise_set = store.append_promise_set(promises=PROMISES, predecessor_id=None)
    return store.append_route(
        promise_set_id=promise_set.id,
        engine=engine,
        options={},
        target_snapshot={
            "target_id": target_id,
            "capability": "transcribe",
            "transport": transport,
            "run_scoped": False,
        },
        operator="self" if not target_id.startswith("modal:") else "modal",
        egress_class="none" if transport == "local" else "third_party_api",
        region=None,
        credential_source=credential_source,
        cost_posture="operator_borne",
        predecessor_id=None,
    )


def _write_call(project, run_id: int, call: dict, *, row_id=1, column_id=1):
    store = RunResultStore(project)
    authority = project.db.execute(
        "SELECT r.current_attempt_id, c.claim_token "
        "FROM runs r JOIN output_column_claims c ON c.run_id=r.id "
        "WHERE r.id=? AND c.status='active' LIMIT 1",
        (run_id,),
    ).fetchone()
    assert authority is not None
    store.write_model_calls(
        run_id,
        [{"row_id": row_id, "column_id": column_id, "model_calls": [call]}],
        writer_attempt_id=str(authority["current_attempt_id"]),
        claim_token=str(authority["claim_token"]),
        authorized_attempt_id=str(authority["current_attempt_id"]),
    )
    project.db.commit()


def _facts(project, run_id: int):
    return project.db.execute(
        "SELECT * FROM model_calls WHERE run_id=? ORDER BY created_at, id", (run_id,)
    ).fetchall()


def _epochs(project):
    return project.db.execute(
        "SELECT * FROM binding_epochs ORDER BY created_at, id"
    ).fetchall()


def _routes(project, run_id: int):
    """The run's route chain, oldest first — read straight from the table
    like ``_epochs``/``_facts`` above (``RouteStore.routes()`` was deleted in
    the cleanup; production reads the head, never the chain)."""
    return project.db.execute(
        "SELECT * FROM routes WHERE subject_kind='run' AND subject_id=? ORDER BY seq",
        (str(run_id),),
    ).fetchall()


# ---------------------------------------------------------------------------
# Epoch stamping + dedupe
# ---------------------------------------------------------------------------


def test_route_bound_fact_carries_epoch_and_dedupes_provenance(project):
    run_id = _make_run(project)
    route = _persist_route(
        project, run_id, target_id="local", transport="local", engine="faster_whisper"
    )
    for row_id in (1, 2):
        call = _model_call_for("faster_whisper", "/nonexistent.wav", {}, {}).as_dict()
        bound = bind_fact_to_route(route, call, {})
        _write_call(project, run_id, bound, row_id=row_id)

    facts = _facts(project, run_id)
    assert len(facts) == 2
    epochs = _epochs(project)
    assert len(epochs) == 1, "identical observed provenance opens ONE epoch"
    assert epochs[0]["route_id"] == route.id
    assert {f["epoch_id"] for f in facts} == {epochs[0]["id"]}
    # No divergence: the route chain stays linear at one row, no violations.
    assert len(_routes(project, run_id)) == 1
    assert RouteStore.for_run(project, run_id).violations() == []


def test_unrouted_run_fact_keeps_null_epoch(project):
    # A run with NO persisted route (before route resolution runs, previews, non-resolution
    # recipes): the honest NULL epoch — legacy rows stay legible.
    run_id = _make_run(project)
    call = _model_call_for("faster_whisper", "/nonexistent.wav", {}, {}).as_dict()
    _write_call(project, run_id, call)
    (fact,) = _facts(project, run_id)
    assert fact["epoch_id"] is None


def test_routed_run_fact_without_observation_fails_loudly(project):
    """A1 (ground truth): the epoch invariant keys on the PERSISTED route
    chain, not on transient payload presence. A route exists for this run,
    so a transcription fact arriving WITHOUT the observation payload is a
    dropped wiring link (extras not merged / recipe missed the extra) — the
    writer raises and records nothing, never a silent NULL-epoch fact."""
    run_id = _make_run(project)
    _persist_route(
        project, run_id, target_id="local", transport="local", engine="faster_whisper"
    )
    call = _model_call_for("faster_whisper", "/nonexistent.wav", {}, {}).as_dict()
    with pytest.raises(RuntimeError, match="epoch invariant"):
        _write_call(project, run_id, call)
    assert _facts(project, run_id) == []  # nothing half-written


def test_unresolvable_observation_fails_loudly(project):
    run_id = _make_run(project)
    call = _model_call_for("faster_whisper", "/nonexistent.wav", {}, {}).as_dict()
    call[ROUTE_OBSERVATION_KEY] = {
        "route_id": "route_DOESNOTEXIST000000000000",
        "observed": {"credential_source": "local"},
    }
    with pytest.raises(RuntimeError, match="epoch invariant"):
        _write_call(project, run_id, call)


# ---------------------------------------------------------------------------
# Divergence path: actual credential vs the route's pin.
# ---------------------------------------------------------------------------


def test_credential_divergence_appends_successor_without_fabricated_violation(project):
    run_id = _make_run(project)
    route = _persist_route(
        project,
        run_id,
        target_id="remote-api:openai",
        transport="remote",
        engine="openai/whisper-1",
        credential_source="local",  # the pinned value
    )
    call = _model_call_for(
        "openai/whisper-1",
        "/nonexistent.wav",
        {},
        {"credential_source": "platform_key", "cost": 0.02},
    )
    bound = bind_fact_to_route(route, call, {})
    _write_call(project, run_id, bound)

    store = RouteStore.for_run(project, run_id)
    routes = _routes(project, run_id)
    assert len(routes) == 2, "divergence appends a successor route"
    successor = routes[1]
    assert successor["predecessor_id"] == route.id
    assert successor["credential_source"] == "platform_key", "successor carries ACTUAL"
    assert successor["promise_set_id"] == route.promise_set_id

    (epoch,) = _epochs(project)
    assert epoch["route_id"] == successor["id"], "epoch opens under the successor"
    (fact,) = _facts(project, run_id)
    assert fact["epoch_id"] == epoch["id"]
    # Settlement-visible fields are the ACTUAL facts.
    assert fact["credential_source"] == "platform_key"

    assert store.violations() == []

    # Dedupe: the same divergent observation again adds NO new successor,
    # epoch, or violation row.
    call = _model_call_for(
        "openai/whisper-1",
        "/nonexistent.wav",
        {},
        {"credential_source": "platform_key", "cost": 0.03},
    )
    bound2 = bind_fact_to_route(route, call, {})
    _write_call(project, run_id, bound2, row_id=2)
    assert len(_routes(project, run_id)) == 2
    assert len(_epochs(project)) == 1
    assert store.violations() == []
    facts = _facts(project, run_id)
    assert [f["epoch_id"] for f in facts] == [epoch["id"], epoch["id"]]


# ---------------------------------------------------------------------------
# Play scene: redeploy-resume — two revisions, one route, two epochs.
# ---------------------------------------------------------------------------


def test_redeploy_resume_yields_two_epochs_under_one_route(project):
    run_id = _make_run(project)
    route = _persist_route(
        project,
        run_id,
        target_id="models-gateway",
        transport="frisket.transcription.v1",
        engine="moss",
    )
    revisions = ["2026.07-a", "2026.07-b"]  # worker redeployed across a resume
    for row_id, revision in enumerate(revisions, start=1):
        out = {
            "revision": revision,
            "device": "cuda",
            "dtype": "float16",
            "model_ids": ["moss-large"],
        }
        call = _model_call_for("moss", "/nonexistent.wav", {}, out).as_dict()
        bound = bind_fact_to_route(route, call, out)
        _write_call(project, run_id, bound, row_id=row_id)

    store = RouteStore.for_run(project, run_id)
    assert len(_routes(project, run_id)) == 1, (
        "revision is observation-only: no successor"
    )
    assert store.violations() == [], "revision divergence is never a violation"
    epochs = _epochs(project)
    assert len(epochs) == 2
    assert {e["route_id"] for e in epochs} == {route.id}

    # Facts share a created_at second, so order by row_id (the resume scene's
    # stable key) rather than insert order.
    facts = sorted(_facts(project, run_id), key=lambda f: int(f["row_id"]))
    fact_epochs = [f["epoch_id"] for f in facts]
    assert len(set(fact_epochs)) == 2, "per-row epoch_id distinguishes provenance"
    # Receipts derive mixed provenance: each fact's epoch names its revision.
    import json

    by_epoch = {e["id"]: json.loads(e["observed_json"]) for e in epochs}
    assert [by_epoch[eid]["revision"] for eid in fact_epochs] == revisions
