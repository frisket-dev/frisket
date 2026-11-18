"""Returned child facts retain their purchase owner across parent recovery."""

from frisket.execution.purchases import LOCAL_PURCHASE_POLICY

import asyncio
import pytest

from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.effect_checkpoints import (
    EffectCheckpointStore,
    InvalidCheckpointState,
)
from frisket.cloud_llm_checkpoint import CloudLLMCheckpointCoordinator
from frisket.cloud_execution import CloudEstimateAuthority
from tests.test_cloud_llm_checkpoint import (
    _claimless_run,
    _payer,
    _Transport,
    _response,
    _request,
    MODEL,
)


def _cloud_route(project, run_id, attempt_id):
    from frisket.engine.store.execution_routes import RouteStore

    routes = RouteStore.for_run(project, run_id)
    promises = routes.append_promise_set(promises=[], predecessor_id=None)
    route = routes.append_route(
        promise_set_id=promises.id,
        engine=MODEL,
        options={},
        target_snapshot={
            "target_id": "frisket-cloud:41:frisket_credits:anthropic",
            "capability": "llm.complete",
            "transport": "frisket.cloud.llm.v1",
            "run_scoped": True,
            "cloud_payer": _payer().to_snapshot(),
        },
        operator="frisket",
        egress_class="frisket_shared",
        region=None,
        credential_source="platform_key",
        cost_posture="platform_metered",
        predecessor_id=None,
    )
    project.db.execute(
        "UPDATE execution_attempts SET head_route_id=? WHERE id=?",
        (route.id, attempt_id),
    )
    project.db.commit()
    return route


@pytest.mark.parametrize("successor", [False, True])
@pytest.mark.parametrize("supplemental", [False, True])
@pytest.mark.parametrize("new_purchase", [False, True])
def test_returned_child_keeps_original_owner_when_parent_recovers(
    make_project, successor, supplemental, new_purchase
):
    project = make_project("child-owner")
    sheet = project.add_sheet("data")
    source = project.add_column(sheet, "input", "text")
    output = project.add_column(sheet, "answer", "text")
    row = project.add_rows(sheet, [{"input": "question"}], {"input": source})[0]
    first_run = _claimless_run(project, attempt_id="attempt-first")
    original_route = _cloud_route(project, first_run, "attempt-first")
    coordinator = CloudLLMCheckpointCoordinator(
        project,
        _Transport([_response(execution_id="child-call")]),
        payer=_payer(),
        estimate_authority=CloudEstimateAuthority(100),
        group_key="same-logical-row",
        action_kind="map.extract",
        run_id=first_run,
        writer_attempt_id="attempt-first",
        claimless_direct_effect=True,
    )
    asyncio.run(coordinator.execute(_request()))
    project.db.execute(
        "UPDATE execution_attempts SET state='halted' WHERE id='attempt-first'"
    )
    project.db.execute(
        "UPDATE runs SET current_attempt_id=NULL WHERE id=?", (first_run,)
    )
    project.db.commit()
    if successor:
        current_run = _claimless_run(
            project, attempt_id="attempt-second", execution_root_run_id=first_run
        )
    else:
        current_run = first_run
        project.db.execute(
            "INSERT INTO execution_attempts(id,run_id,seq,state,action_identity_hash,scope_json,created_at) VALUES('attempt-second',?,1,'dispatching','test','[]',datetime('now'))",
            (current_run,),
        )
        project.db.execute(
            "UPDATE runs SET current_attempt_id='attempt-second' WHERE id=?",
            (current_run,),
        )
        project.db.commit()
    store = RunResultStore(project)
    store.reserve_row_effect_checkpoint(
        "parent",
        run_id=current_run,
        row_id=row,
        action_kind="map.extract",
        identity="parent-identity",
        authorized_attempt_id="attempt-second",
        writer_attempt_id="attempt-second",
        claimless_direct_effect=True,
    )
    # The normal tracing wrapper records a replayed child response before
    # the parent accounts/publishes it, so Explain retains the recovered call.
    from frisket.ai.llm import ModelRouter, LLMRequest
    from frisket.cloud_execution import (
        CloudLLMExecutionBinding,
        cloud_llm_execution_scope,
    )
    from frisket.operability.trace import TraceWriter, TracingRouter
    from frisket.execution.dispatch_control import RunDispatch
    from frisket.authoring.actions import run_trace_row

    replay_transport = _Transport(
        [_response(execution_id="new-call")] if new_purchase else []
    )
    coordinator = CloudLLMCheckpointCoordinator(
        project,
        replay_transport,
        payer=_payer(),
        estimate_authority=CloudEstimateAuthority(100),
        group_key="same-logical-row",
        action_kind="map.extract",
        run_id=current_run,
        writer_attempt_id="attempt-second",
        claimless_direct_effect=True,
    )

    async def trace_replay():
        router = ModelRouter(
            model_call_policy=LOCAL_PURCHASE_POLICY,
            use_env_keys=False,
            cache_mode="off",
            max_retries=0,
        )
        recorder = TraceWriter.open(
            project.path, current_run, action_kind="map.extract", model=MODEL
        )
        traced = TracingRouter(
            router.for_execution(RunDispatch(project, current_run, "attempt-second")),
            recorder.row_trace(row),
        )
        try:
            with cloud_llm_execution_scope(
                CloudLLMExecutionBinding(coordinator=coordinator)
            ):
                response = await traced.complete_transport(
                    LLMRequest(
                        model=MODEL,
                        messages=[{"role": "user", "content": "hello"}],
                        max_tokens=100,
                        temperature=0,
                    )
                )
            recorder.write_row(traced.record(data={"answer": response.content}))
        finally:
            await router.aclose()

    asyncio.run(trace_replay())
    assert not replay_transport.calls
    if new_purchase:
        asyncio.run(coordinator.execute(_request(message="new purchase")))
    accounting = coordinator.returned_accounting[0]
    extras = [
        {"row_id": row, "column_id": output, **entry}
        for entry in coordinator.returned_accounting[1:]
    ]
    payload = {"value": "answer", **accounting}
    if supplemental:
        payload["model_calls"] = []
    replay = {"answer": {**payload, "cost": 0}}
    delta = store.complete_row_effect_checkpoint(
        "parent",
        run_id=current_run,
        row_id=row,
        action_kind="map.extract",
        identity="parent-identity",
        batch=[{"row_id": row, "column_id": output, **payload}],
        replay_response=replay,
        child_checkpoints=coordinator.returned_checkpoints,
        supplemental_accounting=tuple(extras)
        + (
            ({"row_id": row, "column_id": output, **accounting},)
            if supplemental
            else ()
        ),
        writer_attempt_id="attempt-second",
        claimless_direct_effect=True,
    )
    fact = project.db.execute(
        "SELECT run_id,attempt_id FROM model_calls WHERE id='child-call'"
    ).fetchone()
    assert tuple(fact) == (first_run, "attempt-first")
    epoch_route = project.db.execute(
        "SELECT e.route_id FROM model_calls m JOIN binding_epochs e ON e.id=m.epoch_id "
        "WHERE m.id='child-call'"
    ).fetchone()
    assert epoch_route[0] == original_route.id
    assert replay["answer"]["model_calls"] == []
    assert (
        project.db.execute("SELECT count(*) FROM model_calls").fetchone()[0]
        == 1 + new_purchase
    )
    assert (
        EffectCheckpointStore(project.db).get(
            coordinator.returned_checkpoints[0].checkpoint_id
        )
        is None
    )
    assert store.model_calls(first_run)[0]["id"] == "child-call"
    explanation = run_trace_row("p", project, current_run, row, column_id=output)
    assert explanation["recorded"] is True
    assert explanation["trace"]["row"]["calls"][0]["raw_response"] == "answer"
    from frisket.engine.store.execution_groups import execution_runs

    root, group = execution_runs(project, current_run)
    assert root == first_run
    assert sum(len(store.model_calls(member)) for member in group) == 1 + new_purchase
    original_cost = project.db.execute(
        "SELECT cost_actual FROM runs WHERE id=?", (first_run,)
    ).fetchone()[0]
    assert original_cost == pytest.approx(
        0.00001 * (1 + (new_purchase and not successor))
    )
    assert delta == pytest.approx(0.00001 * (new_purchase + (not successor)))
    if new_purchase:
        assert tuple(
            project.db.execute(
                "SELECT run_id,attempt_id FROM model_calls WHERE id='new-call'"
            ).fetchone()
        ) == (current_run, "attempt-second")
    if successor:
        assert len(store.model_calls(current_run)) == int(new_purchase)
        assert project.db.execute(
            "SELECT cost_actual FROM runs WHERE id=?", (current_run,)
        ).fetchone()[0] == pytest.approx(0.00001 * new_purchase)


def test_unaccounted_cloud_child_cannot_be_retired(make_project):
    project = make_project("unaccounted-child")
    project.add_sheet("data")
    run = _claimless_run(project, attempt_id="attempt-first")
    coordinator = CloudLLMCheckpointCoordinator(
        project,
        _Transport([_response(execution_id="child-call")]),
        payer=_payer(),
        estimate_authority=CloudEstimateAuthority(100),
        group_key="row",
        action_kind="map.extract",
        run_id=run,
        writer_attempt_id="attempt-first",
        claimless_direct_effect=True,
    )
    asyncio.run(coordinator.execute(_request()))
    ref = coordinator.returned_checkpoints[0]
    store = EffectCheckpointStore(project.db)
    with pytest.raises(InvalidCheckpointState, match="accounting"):
        store.consume_and_retire(
            ref.checkpoint_id,
            family="model_call",
            group_key=ref.group_key,
            unit_key=ref.unit_key,
            action_kind=ref.action_kind,
            identity=ref.identity,
            run_id=run,
            writer_attempt_id="attempt-first",
            claimless_direct_effect=True,
        )
    assert store.get(ref.checkpoint_id)["state"] == "returned"


@pytest.mark.parametrize("reason", ["pending", "invalid", "failed"])
def test_equal_decoded_input_with_new_unavailability_cannot_adopt_reserved_effect(
    make_project, reason
):
    from types import SimpleNamespace
    from frisket.engine.runner.map_runner import _row_effect_identity
    from frisket.engine.runner.row_inputs import RowInputValues
    from frisket.engine.store.effect_checkpoints import IdentityMismatch

    project = make_project("availability")
    args = dict(
        project_storage_identity=project.storage_identity,
        run_id=1,
        row_id=1,
        program=SimpleNamespace(version="1"),
        spec={"action_kind": "map.extract"},
        out_cols={"answer": 2},
        cloud_payer=_payer().to_snapshot(),
    )
    valid = RowInputValues({"source": None}, source_cells_empty=True)
    unavailable = RowInputValues(
        {"source": None},
        source_cells_empty=True,
        unavailable_inputs=(
            {"column": "source", "code": "input_" + reason, "message": reason},
        ),
    )
    checkpoint, identity = _row_effect_identity(**args, values=valid)
    changed_checkpoint, changed_identity = _row_effect_identity(
        **args, values=unavailable
    )
    assert checkpoint == changed_checkpoint
    assert identity != changed_identity
    store = EffectCheckpointStore(project.db)
    unit = dict(
        family="row_effect", group_key="1", unit_key="1", action_kind="map.extract"
    )
    store.reserve(checkpoint, identity=identity, **unit)
    with pytest.raises(IdentityMismatch):
        store.returned_for_replay(identity=changed_identity, **unit)
    assert store.get(checkpoint)["state"] == "reserved"
    assert store.get(checkpoint)["accounting_persisted"] is False
