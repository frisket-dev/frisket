"""Anchor O4 — dedicated box (defined, unlisted): zero-schema-change readiness.

The important O4 case:

    "instantiating O4 requires catalog rows, policy entries, and a rate-card
     line. Zero schema changes, zero new code paths. This test runs in CI
     against a fixture long before any buyer exists."

This file IS that CI fixture. Everything below uses ONLY the shipped route resolution
machinery — ``ExecutionTarget`` (which already carries ``region`` and the
reserved ``frisket_dedicated_org`` egress class), ``resolve`` over the
provider protocol, the transcription promise compiler,
the route store, the claim-label copy, and the promise evaluator. If any
assertion here ever requires touching a src/ module, the architecture has
failed the anchor.

Not covered here on purpose: the hosted composition wiring (WorkerPorts
provider injection). This anchor proves the open
tree's types, tables, and evaluators already EXPRESS the offering.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from frisket.contracts.transcription_sidecar import TranscriptionOptionSupport
from frisket.engine.store import Project
from frisket.engine.store.execution_routes import RouteStore
from frisket.engine.store.runs import RunResultStore
from frisket.execution.claim_labels import claim_display
from frisket.execution.promise_compiler import (
    OperatorBorneZeroCost,
    compile_route_promises,
)
from frisket.execution.promises import (
    EGRESS_ORDER_REF,
    SATISFIED,
    VIOLATED,
    Promise,
    evaluate,
    evaluate_set,
)
from frisket.execution.provider import CompositionFacts, ConnectionConfig
from frisket.execution.resolve_for_action import promise_rows, target_snapshot
from frisket.execution.resolver import ResolutionRequest, Resolution, resolve
from frisket.execution.targets import ExecutionTarget, TargetEngineSupport

DEDICATED_TARGET = ExecutionTarget(
    id="dedicated:acme-box-1",
    operator="frisket",
    egress_class="frisket_dedicated_org",  # reserved for O4
    region="us-east-1",  # a dedicated box, unlike shared, IS region-pinned
    engines=(
        TargetEngineSupport(
            engine="faster_whisper",
            transport="local",
            capability="transcribe",
            options=TranscriptionOptionSupport.model_validate(
                {
                    "diarization_mode": "none",
                    "speaker_hint": "none",
                    "language": True,
                    "model_size": True,
                    "vad": True,
                    "context": False,
                }
            ),
            sizes=("tiny", "base", "small", "medium", "large"),
        ),
    ),
)


class DedicatedBoxProvider:
    """A catalog-row-shaped ExecutionTargetProvider: exactly what the public-provider boundary's
    provider port would serve for a dedicated box. No new protocol, no new
    fields — the port as shipped."""

    def targets(self) -> Sequence[ExecutionTarget]:
        return (DEDICATED_TARGET,)

    def connection(self, target_id: str) -> ConnectionConfig | None:
        return ConnectionConfig() if target_id == DEDICATED_TARGET.id else None


def _resolve_dedicated() -> Resolution:
    outcome = resolve(
        ResolutionRequest(engine="faster_whisper"),
        DedicatedBoxProvider(),
        CompositionFacts(),
    )
    assert isinstance(outcome, Resolution), outcome
    return outcome


def test_anchor_o4_resolves_with_pinned_region_through_existing_resolver():
    resolution = _resolve_dedicated()
    facts = resolution.facts
    assert facts.target_id == "dedicated:acme-box-1"
    assert facts.operator == "frisket"
    assert facts.egress_class == "frisket_dedicated_org"
    assert facts.region == "us-east-1"  # region flows: recorded truthfully


def test_anchor_o4_compiles_region_promise_and_persists_route(tmp_path: Path):
    """The route row + promise set persist through the SAME store and writer
    columns every other offering uses — the region promise included. No new
    tables, no new columns, no new writer."""
    resolution = _resolve_dedicated()
    facts = resolution.facts
    promise_set = compile_route_promises(
        facts,
        OperatorBorneZeroCost(),
    )
    by_field = {p.field: p for p in promise_set.promises}
    # The region is pinned, so (and ONLY so) a region promise compiles.
    assert by_field["region"].op == "eq"
    assert by_field["region"].value == "us-east-1"
    # Dedicated egress is off-box: a user claim, gated on first consent.
    assert by_field["egress_class"].audience == "user_claim"

    project = Project.create(tmp_path / "o4.frisket")
    try:
        sheet_id = project.add_sheet("Media")
        cur = project.db.execute("INSERT INTO ops (kind, spec) VALUES ('map','{}')")
        project.db.commit()
        run_id = RunResultStore(project).start_run(
            int(cur.lastrowid), sheet_id, "media.transcribe"
        )
        store = RouteStore.for_run(project, run_id)
        stored_set = store.append_promise_set(
            promises=promise_rows(promise_set), predecessor_id=None
        )
        route = store.append_route(
            promise_set_id=stored_set.id,
            engine=facts.engine,
            options={},
            target_snapshot=target_snapshot(resolution),
            operator=facts.operator,
            egress_class=facts.egress_class,
            region=facts.region,
            credential_source=facts.credential_source,
            cost_posture=facts.cost_posture,
            predecessor_id=None,
        )
        head_route, head_set = store.head()
        assert head_route.id == route.id
        assert head_route.region == "us-east-1"  # the schema already has it
        assert head_route.egress_class == "frisket_dedicated_org"
        assert head_route.target_snapshot["target_id"] == "dedicated:acme-box-1"
        assert any(row["field"] == "region" for row in head_set.promises)
    finally:
        project.close()


def test_anchor_o4_claims_require_injected_dedicated_venue_copy():
    """The durable egress literal stays generic; an edition supplies copy."""
    resolution = _resolve_dedicated()
    facts = resolution.facts
    promise_set = compile_route_promises(
        facts,
        OperatorBorneZeroCost(),
    )
    egress_claim = next(p for p in promise_set.promises if p.field == "egress_class")
    display = claim_display(egress_claim, facts)
    assert display.startswith(
        "Media is handled by an execution venue of class 'frisket_dedicated_org'."
    )


def test_anchor_o4_satisfaction_evaluates_over_existing_evaluator():
    resolution = _resolve_dedicated()
    facts = resolution.facts
    promise_set = compile_route_promises(
        facts,
        OperatorBorneZeroCost(),
    )
    evaluation = evaluate_set(
        promise_set.promises,
        {
            "operator": "frisket",
            "egress_class": "frisket_dedicated_org",
            "region": "us-east-1",
            "credential_source": facts.credential_source,
        },
    )
    assert evaluation.overall == SATISFIED
    # ...and a region drift would be a determinate violation, not a shrug.
    drifted = evaluate_set(
        promise_set.promises,
        {
            "operator": "frisket",
            "egress_class": "frisket_dedicated_org",
            "region": "eu-west-1",
            "credential_source": facts.credential_source,
        },
    )
    assert drifted.overall == VIOLATED


def test_anchor_o4_egress_lattice_places_dedicated_below_shared():
    """The pinned ``egress.v1`` order already ranks dedicated safer than
    shared: a rebind from a shared-consented route onto dedicated hardware
    satisfies by order implication (the safer-rebind O4 upsell), while the
    reverse — dedicated consent quietly landing on shared — violates."""
    shared_consent = Promise.make(
        "egress_class",
        "satisfies_order",
        "frisket_shared",
        order_ref=EGRESS_ORDER_REF,
        audience="user_claim",
    )
    assert (
        evaluate(shared_consent, {"egress_class": "frisket_dedicated_org"}).status
        == SATISFIED
    )
    dedicated_consent = Promise.make(
        "egress_class",
        "satisfies_order",
        "frisket_dedicated_org",
        order_ref=EGRESS_ORDER_REF,
        audience="user_claim",
    )
    assert (
        evaluate(dedicated_consent, {"egress_class": "frisket_shared"}).status
        == VIOLATED
    )
