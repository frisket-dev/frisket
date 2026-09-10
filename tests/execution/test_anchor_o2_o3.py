"""Anchors O2 (cloud shared, metered) and O3 (cloud BYOK) as posture fixtures.

IMPORTANT SCOPE MARKER: these are POSTURE FIXTURES, not hosted-composition
tests. O2 and O3 are cloud offerings whose dynamic resolver, platform
credentials, and org context are not part of the open tree, so the hosted
composition cannot run here. What CAN run here is the shared mechanics: an
offering configures the architecture rather than forking it.

- O2-shaped: a ``platform_metered`` posture with ``frisket_shared`` egress,
  constructed through the resolver test seam (``RouteRowFacts`` — THE
  route-facts type and the compiler's exact input), gates with BOTH cost and
  egress claims, and
  the persisted route ROW is the sole source of the priced fact fields
  because the route is the one ledger.
- O3-shaped: an ``org_key`` posture whose observed dispatch credential is
  the platform key exercises the divergence path end to end — successor
  route with the ACTUAL facts, an epoch under it, and a settlement-visible
  actual on the committed fact. No O3 run changes billing posture without a
  recorded, surfaced route event. The FIRST answer to that scenario is
  pre-effect: the adapter
  refuses a credential class the consent did not name
  (``tests/execution/test_credential_use.py``) — and what remains here is
  the recorded end state if it happens anyway.

This file names the O3 scenario and asserts the END state a settlement reader
would find.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from frisket.ai.models.metadata import ModelCallMeta
from frisket.engine.store import Project
from frisket.engine.store.execution_routes import ConsentRegistry, RouteStore
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.runs import RunResultStore
from frisket.execution.promise_compiler import (
    PricedCostBasis,
    compile_route_promises,
)
from frisket.execution.commercial import CommercialPresentation
from frisket.execution.resolve_for_action import (
    gate_claims_payload,
    promise_rows,
    uncovered_user_claims,
)
from frisket.execution.resolver import RouteRowFacts
from frisket.execution.runtime_binding import bind_fact_to_route


def _route_rows(project, run_id: int):
    """The run's route chain, oldest first. ``RouteStore.routes()`` was
    deleted because production reads the head, so chain-SHAPE assertions read
    the table."""
    return project.db.execute(
        "SELECT * FROM routes WHERE subject_kind='run' AND subject_id=? ORDER BY seq",
        (str(run_id),),
    ).fetchall()


@pytest.fixture()
def project(tmp_path: Path):
    p = Project.create(tmp_path / "anchor.frisket")
    try:
        yield p
    finally:
        p.close()


def _make_run(project) -> int:
    sheet_id = project.add_sheet("Media")
    project.add_column(sheet_id, "transcript", ai_generated=True)
    row_ids = project.add_rows(sheet_id, [{}], {})
    cur = project.db.execute("INSERT INTO ops (kind, spec) VALUES ('map', '{}')")
    op_id = int(cur.lastrowid)
    project.db.commit()
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "media.transcribe",
        row_ids=row_ids,
    )
    claim_token = f"output-claim:anchor-o2-o3:{run_id}"
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
    attempt_id = f"attempt_anchor_o2_o3_{run_id}"
    project.db.execute(
        "INSERT INTO execution_attempts "
        "(id, run_id, seq, state, action_identity_hash, scope_json, "
        "created_at) VALUES (?, ?, 0, 'dispatching', 'anchor-o2-o3', "
        "?, datetime('now'))",
        (attempt_id, run_id, str(row_ids)),
    )
    project.db.execute(
        "UPDATE runs SET current_attempt_id=? WHERE id=?",
        (attempt_id, run_id),
    )
    project.db.commit()
    return run_id


def _writer_authority(project, run_id: int) -> dict[str, str]:
    row = project.db.execute(
        "SELECT r.current_attempt_id, c.claim_token "
        "FROM runs r JOIN output_column_claims c ON c.run_id=r.id "
        "WHERE r.id=? AND c.status='active' LIMIT 1",
        (run_id,),
    ).fetchone()
    assert row is not None
    attempt_id = str(row["current_attempt_id"])
    return {
        "writer_attempt_id": attempt_id,
        "authorized_attempt_id": attempt_id,
        "claim_token": str(row["claim_token"]),
    }


# ---------------------------------------------------------------------------
# O2-shaped: platform_metered posture, frisket_shared egress
# ---------------------------------------------------------------------------

O2_FACTS = RouteRowFacts(
    target_id="cloud-models",
    engine="parakeet-tdt",
    operator="frisket",
    egress_class="frisket_shared",
    region=None,
    credential_source="platform_key",
    cost_posture="platform_metered",
)

# Scene 2's $4.20: 30 estimated audio seconds at $0.14 under the shared
# deployment's pricing key.
O2_COST = PricedCostBasis(
    pricing_key="cloud.parakeet.audio_second",
    unit_rate="0.14",
    estimated_quantity="30",
    quantity_unit="audio_second",
    terms_version="anchor.o2.v1",
    quantity_rounding_mode="exact",
    quantity_rounding_decimal_places=None,
    meter_key="audio_seconds",
    meter_units_per_quantity_unit="1",
    ceiling_mode="consented_quantity",
    row_settlement_mode="all_metered",
    charge_authority="anchor-o2",
)

O2_SNAPSHOT = {
    "target_id": "cloud-models",
    "capability": "transcribe",
    "transport": "remote",
    "run_scoped": False,
}


def test_anchor_o2_gate_renders_cost_and_egress_claims(project):
    """O2's gate has cost and egress claims on first consent. Both rows are
    user claims, neither is covered by the standing
    sub-gate consent, and the rendered copy speaks the metered posture."""
    promise_set = compile_route_promises(O2_FACTS, O2_COST)
    from frisket.engine.store.execution_routes import instance_principal

    uncovered = uncovered_user_claims(
        promise_set,
        standing_consents=ConsentRegistry(project).standing_consents(),
        principal=instance_principal(project),
    )
    assert sorted(p.field for p in uncovered) == ["cost", "egress_class"]

    presentation = CommercialPresentation(
        venue_label="Acme-operated shared infrastructure",
        billing_label="metered and billed through Acme credits",
    )
    payload = gate_claims_payload(
        uncovered,
        O2_FACTS,
        presentation,
        has_cost_claim=any(promise.field == "cost" for promise in promise_set.promises),
    )
    by_field = {row["field"]: row["display"] for row in payload}
    assert "Acme-operated shared infrastructure" in by_field["egress_class"]
    assert "metered and billed through Acme credits" in by_field["egress_class"]
    assert by_field["cost"] == (
        "Maximum cost: $4.20 (metered and billed through Acme credits)."
    )


def test_anchor_o2_route_row_is_sole_source_of_priced_fact_fields(project):
    """O2's one-ledger anchor: "every O2 run's charge is derivable from its
    route alone." A fact bound to the persisted route takes its priced /
    promised fields from the route ROW + snapshot — a call dict arriving
    with contradictory path-local values is overridden, never trusted."""
    run_id = _make_run(project)
    store = RouteStore.for_run(project, run_id)
    promise_set = compile_route_promises(O2_FACTS, O2_COST)
    stored_set = store.append_promise_set(
        promises=promise_rows(promise_set), predecessor_id=None
    )
    route = store.append_route(
        promise_set_id=stored_set.id,
        engine="parakeet-tdt",
        options={},
        target_snapshot=O2_SNAPSHOT,
        operator=O2_FACTS.operator,
        egress_class=O2_FACTS.egress_class,
        region=O2_FACTS.region,
        credential_source=O2_FACTS.credential_source,
        cost_posture=O2_FACTS.cost_posture,
        predecessor_id=None,
    )

    call = ModelCallMeta.provider_call(
        capability="transcribe",
        engine="parakeet-tdt",
        provider="wrong",
        provider_kind="wrong",
        model_ids=["parakeet-tdt"],
        credential_source="platform_key",
        provider_reported_cost_usd=0.42,
        provider_cost_usd=0.42,
        units={"audio_seconds": 3},
        cost_source="wrong",
        duration_ms=None,
    ).as_dict()
    # Corrupt the path-local promised fields: the route must win on every one.
    call["provider"] = "env-derived-wrong"
    call["provider_kind"] = "wrong"
    call["cost_source"] = "wrong"
    assert call["credential_source"] == "platform_key"  # observed == pinned
    bound = bind_fact_to_route(route, call, {})
    assert bound["provider"] == "frisket"  # grouping from the route snapshot
    assert bound["provider_kind"] == "platform_api"
    assert bound["credential_source"] == "platform_key"
    # platform-metered remote path with an observed provider cost: the route
    # posture + transport derive the priced source, never the caller.
    assert bound["cost_source"] == "pricing_data"

    RunResultStore(project).write_model_calls(
        run_id,
        [{"row_id": 1, "column_id": 1, "model_calls": [bound]}],
        **_writer_authority(project, run_id),
    )
    project.db.commit()

    [fact] = RunResultStore(project).model_calls(run_id)
    assert fact["provider"] == "frisket"
    assert fact["credential_source"] == "platform_key"
    assert fact["cost_source"] == "pricing_data"
    assert fact["provider_cost_usd"] == 0.42
    # Observed matches the pin: the chain stays linear, no violations, and
    # the fact's epoch opens under the one route.
    assert len(_route_rows(project, run_id)) == 1
    assert store.violations() == []
    epoch = project.db.execute(
        "SELECT * FROM binding_epochs WHERE id=?", (fact["epoch_id"],)
    ).fetchone()
    assert epoch is not None and epoch["route_id"] == route.id


# ---------------------------------------------------------------------------
# O3-shaped: org_key posture, observed platform_key -> divergence
# ---------------------------------------------------------------------------

O3_FACTS = RouteRowFacts(
    target_id="cloud-models",
    engine="parakeet-tdt",
    operator="frisket",
    egress_class="frisket_shared",
    region=None,
    credential_source="org_key",
    cost_posture="org_key",
)


def test_anchor_o3_credential_divergence_is_recorded_and_settlement_visible(
    project,
):
    """O3's hard case: a silent fallback to the platform key must never be
    settled as if it were the org's own bill.

    The FIRST line of defence is before the effect: the
    adapter's selected credential class is checked against the consented one
    and refuses (``tests/execution/test_credential_use.py``, and at the real
    seat in ``tests/ops/test_transcribe_route_binding.py``). What this test
    pins is the SECOND line: if the divergence happens anyway, the
    post-effect observation leaves exactly this end state for a settlement
    reader:

    - a successor route carrying the ACTUAL facts (head of the chain);
    - the committed fact's settlement-visible ``credential_source`` is the
      ACTUAL ``platform_key`` (settlement keys on capability and credential
      source, so the successor truth corrects the zero-rating hazard), with
      its epoch under the successor;
    - and NO ``route_violations`` row, because no compiled promise covers
      the credential any more: the evidence lives on the epoch and the
      successor route, and a fabricated promise row would fingerprint
      against nothing any reader could find in ``promise_sets``.
    """
    run_id = _make_run(project)
    store = RouteStore.for_run(project, run_id)
    promise_set = compile_route_promises(O3_FACTS, O2_COST)
    stored_set = store.append_promise_set(
        promises=promise_rows(promise_set), predecessor_id=None
    )
    route = store.append_route(
        promise_set_id=stored_set.id,
        engine="parakeet-tdt",
        options={},
        target_snapshot=O2_SNAPSHOT,
        operator=O3_FACTS.operator,
        egress_class=O3_FACTS.egress_class,
        region=O3_FACTS.region,
        credential_source=O3_FACTS.credential_source,  # the pinned value
        cost_posture=O3_FACTS.cost_posture,
        predecessor_id=None,
    )

    call = ModelCallMeta.provider_call(
        capability="transcribe",
        engine="parakeet-tdt",
        provider="frisket",
        provider_kind="platform_api",
        model_ids=["parakeet-tdt"],
        credential_source="platform_key",
        provider_reported_cost_usd=0.07,
        provider_cost_usd=0.07,
        units={"audio_seconds": 1},
        cost_source="pricing_data",
        duration_ms=None,
    ).as_dict()
    bound = bind_fact_to_route(route, call, {})
    RunResultStore(project).write_model_calls(
        run_id,
        [{"row_id": 1, "column_id": 1, "model_calls": [bound]}],
        **_writer_authority(project, run_id),
    )
    project.db.commit()

    # Successor route with the ACTUAL facts, chained to the consented route.
    routes = _route_rows(project, run_id)
    assert len(routes) == 2
    predecessor = routes[0]
    assert routes[1]["predecessor_id"] == predecessor["id"]
    assert predecessor["credential_source"] == "org_key"
    successor, _head_set = store.head()  # the successor IS the head
    assert successor.id == routes[1]["id"]
    assert successor.credential_source == "platform_key"
    assert successor.promise_set_id == stored_set.id  # same consented set

    # No compiled promise covers the credential, so there is nothing to have
    # violated — and no synthesized ledger row naming a claim
    # the user never made.
    assert store.violations() == []
    assert not any(row["field"] == "credential_source" for row in stored_set.promises)

    # What a settlement reader sees on the fact itself: the ACTUAL
    # credential (never the consented aspiration), the provider-reported
    # cost, and an epoch that resolves to the successor's actual facts.
    [fact] = RunResultStore(project).model_calls(run_id)
    assert fact["capability"] == "transcribe"
    assert fact["credential_source"] == "platform_key"
    assert fact["provider_cost_usd"] == 0.07
    epoch = project.db.execute(
        "SELECT * FROM binding_epochs WHERE id=?", (fact["epoch_id"],)
    ).fetchone()
    assert epoch is not None and epoch["route_id"] == successor.id
    # Chain head (the row a resume or settlement sweep loads) IS the
    # successor: the billing-posture change is on the record, not backstage.
    head_route, _head_set = store.head()
    assert head_route.id == successor.id
