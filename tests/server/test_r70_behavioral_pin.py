"""Black-box pins for surviving routed consent behavior.

GD-06 removed the synthetic graduation/halt scenarios. This file retains the
production claim-bearing completion path and exact-match/new-scope consent
behavior.
"""

from __future__ import annotations

from frisket.engine.store.execution_routes import (
    CONSENT_BASIS_CONFIRMED,
    CONSENT_BASIS_EXACT_MATCH,
    RouteStore,
)
from frisket.ops.base import persisted_action_program_invocation_halt
from http_test_helpers import (
    add_audio_row,
    drain_one_project_run_job,
    launch_consented_audio_run,
    run_row,
    seed_audio_project,
    transcribe_action,
)


def _halt_marker(project, run_id: int):
    return persisted_action_program_invocation_halt(run_row(project, run_id)["params"])


def _transcript_values(project, sheet_id: int) -> list[str]:
    """The transcripts a user would actually see in the sheet."""
    column = next(
        (c for c in project.columns(sheet_id) if c["name"] == "transcript"), None
    )
    if column is None:
        return []
    values = project.get_values(sheet_id, int(column["id"]))
    return sorted(str(value) for value in values.values() if value)


# ---------------------------------------------------------------------------
# 1. The consented routed run completes.
# ---------------------------------------------------------------------------


def test_scenario1_consented_routed_run_completes_with_receipt_and_ledger(
    client, audio_consent_gateway_env, audio_transcribe_engine_stub
):
    """A consented claim-bearing run commits its cell and receipt."""
    pid, project, sheet_id = seed_audio_project(client)
    run_id, receipt_id = launch_consented_audio_run(
        client, pid, project, sheet_id, key="r70-happy@sha256:pin"
    )

    assert drain_one_project_run_job(client, worker_id="r70-happy") is True

    # The run reached its terminal green state and dispatched exactly its rows.
    assert run_row(project, run_id)["status"] == "completed"
    assert _halt_marker(project, run_id) is None
    assert audio_transcribe_engine_stub == ["moss"]

    # The receipt is readable over the real receipt route, and it is terminal.
    receipt = client.get(f"/api/projects/{pid}/actions/v1/receipts/{receipt_id}")
    assert receipt.status_code == 200, receipt.text
    receipt_body = receipt.json()
    assert receipt_body["schema_version"] == "frisket.receipt.v1"
    assert receipt_body["action_kind"] == "media.transcribe"
    assert receipt_body["status"] == "completed"

    # The transcript really landed in the sheet.
    assert _transcript_values(project, sheet_id) == ["stubbed transcript"]

    # Consent ledger: the launch's own confirmation is recorded against the
    # promise set the user was shown.
    store = RouteStore.for_run(project, run_id)
    _head_route, head_set = store.head()
    consents = store.consents()
    assert [c.promise_set_hash for c in consents] == [head_set.promise_set_hash]

    # The run got the route facts it consented to.
    assert store.violations() == []


# ---------------------------------------------------------------------------
# 6. An unchanged relaunch derives consent; a new-scope backfill re-asks.
# ---------------------------------------------------------------------------


def test_scenario6_unchanged_relaunch_derives_but_new_scope_backfill_reasks(
    client, audio_consent_gateway_env, audio_transcribe_engine_stub
):
    """An identical fresh relaunch can derive exact-match consent, but a new
    row was never part of the confirmed scope. The backfill must mint a fresh
    402 and bind only the exact echo before it may dispatch that row."""
    pid, project, sheet_id = seed_audio_project(client)

    run_id, _receipt_id = launch_consented_audio_run(
        client, pid, project, sheet_id, key="r70-backfill@sha256:pin"
    )
    assert drain_one_project_run_job(client, worker_id="r70-backfill") is True
    assert run_row(project, run_id)["status"] == "completed"
    assert len(audio_transcribe_engine_stub) == 1

    # First half of "without re-asking": a FRESH relaunch of the very same
    # action over the same rows compiles identical claims, so the gate ADMITS
    # it on the DERIVED consent (no 402 at all) and records its own consent row
    # saying exactly why it was allowed.
    relaunched = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json={
            **transcribe_action(sheet_id, key="r70-relaunch@sha256:pin"),
            "replace_existing": True,
        },
    )
    assert relaunched.status_code == 200, relaunched.text
    assert relaunched.json()["status"] == "queued"  # never needs_confirmation
    relaunch_run_id = int(
        project.db.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 1").fetchone()[
            "id"
        ]
    )
    assert relaunch_run_id != run_id
    derived = RouteStore.for_run(project, relaunch_run_id).consents()
    assert [c.grant_basis for c in derived] == [CONSENT_BASIS_EXACT_MATCH], derived
    assert drain_one_project_run_job(client, worker_id="r70-relaunch") is True
    assert run_row(project, relaunch_run_id)["status"] == "completed"
    dispatched_before_backfill = len(audio_transcribe_engine_stub)
    # Backfill will reconstruct this single source generation as a fresh scoped
    # run. Its consent must not mutate the source run's chain.
    store = RouteStore.for_run(project, relaunch_run_id)
    consents_before = {c.id for c in store.consents()}
    head_before = store.head()[1].id

    # Second half: a new row of the SAME duration arrives and is BACKFILLED
    # from the unchanged source generation above.
    add_audio_row(project, sheet_id)

    backfill_action = {
        "action_id": "run.backfill",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"column": "transcript"},
        "idempotency_key": "r70-backfill-run@sha256:pin",
    }
    gated = client.post(f"/api/projects/{pid}/actions/v1/run", json=backfill_action)
    assert gated.status_code == 402, gated.text
    gate_details = gated.json()["errors"][0]["details"]
    assert [claim["field"] for claim in gate_details["claims"]] == ["egress_class"]
    set_hash = gate_details["promise_set_hash"]
    assert set_hash
    assert len(audio_transcribe_engine_stub) == dispatched_before_backfill
    assert {c.id for c in store.consents()} == consents_before

    backfill_action["confirmation"] = set_hash
    response = client.post(f"/api/projects/{pid}/actions/v1/run", json=backfill_action)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    backfill_run_id = int(body["run_id"])
    assert backfill_run_id != relaunch_run_id
    output = next(item for item in body["outputs"] if item["kind"] == "run_backfill")
    assert output["ref"]["filled"] == 1
    # The new row really dispatched — the backfill is not a no-op 200.
    assert len(audio_transcribe_engine_stub) == dispatched_before_backfill + 1

    # Both rows carry a transcript, and both immutable generations remain green.
    assert _transcript_values(project, sheet_id) == [
        "stubbed transcript",
        "stubbed transcript",
    ]
    assert run_row(project, relaunch_run_id)["status"] == "completed"
    assert _halt_marker(project, relaunch_run_id) is None
    assert run_row(project, backfill_run_id)["status"] == "completed"
    assert _halt_marker(project, backfill_run_id) is None

    # The source chain is unchanged; the fresh run owns the confirmation.
    assert store.head()[1].id == head_before
    assert {c.id for c in store.consents()} == consents_before
    successor_store = RouteStore.for_run(project, backfill_run_id)
    successor_head = successor_store.head()[1]
    assert successor_head.promise_set_hash == set_hash
    successor_consents = successor_store.consents()
    assert len(successor_consents) == 1
    assert successor_consents[0].grant_basis == CONSENT_BASIS_CONFIRMED
    assert successor_consents[0].promise_set_hash == set_hash
