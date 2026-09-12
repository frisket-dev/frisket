"""Black-box pins for surviving routed consent behavior.

GD-06 removed the synthetic graduation/halt scenarios. This file retains the
production claim-bearing completion path and exact-match/new-scope consent
behavior.
"""

from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.engine.jobs.runs import register_project_run_handler
from frisket.engine.jobs.worker import HandlerRegistry, Worker
from frisket.engine.store.execution_routes import (
    CONSENT_BASIS_CONFIRMED,
    CONSENT_BASIS_EXACT_MATCH,
    RouteStore,
)
from frisket.engine.store.media_blobs import media_cell
from frisket.execution.pricing_policy import (
    QuoteFacts,
    RatedQuote,
    install_pricing_policy,
)
from frisket.ops.base import persisted_recipe_invocation_halt
from frisket.server.app import create_app


class _AboveLimitPolicy:
    """A deployment tariff that makes this consent pin genuinely billable."""

    policy_id = "test.r70.above-limit.v1"

    def rate(self, facts: QuoteFacts) -> RatedQuote:
        return RatedQuote(
            billed_cost=2_000_001,
            provider_cost=facts.provider_cost,
            lane="cost_plus",
            policy_id=self.policy_id,
        )


@pytest.fixture()
def gateway_env(monkeypatch):
    """A live, routed, claim-bearing models-gateway target."""
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models.test")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "secret")
    install_pricing_policy(_AboveLimitPolicy())


@pytest.fixture()
def run_engine_stub(monkeypatch):
    """Stub the engine boundary only, never ``execute``: the
    resumed/original run still flows through the real route-binding and
    observation surfaces this pin is about."""
    calls: list[str] = []

    async def fake_run_engine(
        engine, path, spec, ctx, *, should_cancel=None, transport=None, media=None
    ):
        calls.append(engine)
        return transcribe_engines.TranscriptionEngineResult(
            output={
                "text": "stubbed transcript",
                "segments": [],
                "language": None,
                "cost": 0.0,
            },
            model_calls=(),
        )

    from frisket.sdk.ops import transcribe_engines

    monkeypatch.setattr(transcribe_engines, "run_transcription_engine", fake_run_engine)
    return calls


# ---------------------------------------------------------------------------
# Harness for durable transcription behavior
# ---------------------------------------------------------------------------


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "workspace"))


def _seed_audio_project(
    client: TestClient, *, name: str = "behavioral pin"
) -> tuple[str, object, int]:
    pid = client.post("/api/projects", json={"name": name}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("S")
    col = project.add_column(sheet_id, "media", type="audio")
    blob = project.add_blob(
        b"RIFFxxxxWAVEfmt ",
        filename="a.wav",
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"duration_seconds": 2.0, "kind": "audio"}
        ),
    )
    project.add_rows(
        sheet_id,
        [{"media": media_cell(blob, mime="audio/wav", filename="a.wav")}],
        {"media": col},
    )
    return pid, project, sheet_id


def _add_audio_row(project, sheet_id: int, *, duration_seconds: float = 2.0) -> None:
    col = next(c for c in project.columns(sheet_id) if c["name"] == "media")
    name = f"b{duration_seconds}.wav"
    blob = project.add_blob(
        b"RIFFyyyyWAVEfmt " + str(duration_seconds).encode(),
        filename=name,
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"duration_seconds": duration_seconds, "kind": "audio"}
        ),
    )
    project.add_rows(
        sheet_id,
        [{"media": media_cell(blob, mime="audio/wav", filename=name)}],
        {"media": col["id"]},
    )


def _transcribe_action(sheet_id: int, *, key: str) -> dict:
    return {
        "action_id": "media.transcribe",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "output_names": {"text": "transcript", "segments": "transcript_segments"},
        "params": {
            "source": "media",
            "engine": "moss",
        },
        "idempotency_key": key,
    }


def _launch_consented_run(
    client: TestClient, pid: str, project, sheet_id: int, *, key: str
) -> tuple[int, str]:
    """The REAL launch: an unconfirmed POST is gated with its claim lines and
    promise-set hash, the confirm echoes that hash, and the run is queued with
    a receipt. Returns (run_id, receipt_id)."""
    action = _transcribe_action(sheet_id, key=key)
    gated = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=action,
    )
    assert gated.status_code == 402, gated.text
    body = gated.json()
    assert body["status"] == "needs_confirmation"
    details = body["errors"][0]["details"]
    # A genuinely claim-bearing target: the user is asked about egress.
    assert {claim["field"] for claim in details["claims"]} == {"egress_class"}, details[
        "claims"
    ]
    set_hash = details["promise_set_hash"]
    assert set_hash

    action["confirmation"] = set_hash
    queued = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=action,
    )
    assert queued.status_code == 200, queued.text
    launched = queued.json()
    assert launched["status"] == "queued"
    receipt_id = launched["receipt_id"]
    assert receipt_id
    run_id = int(
        project.db.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 1").fetchone()[
            "id"
        ]
    )
    return run_id, receipt_id


def _drain_one_job(client: TestClient, *, worker_id: str) -> bool:
    """The REAL worker over the REAL queued ``project.run`` handler."""
    registry = HandlerRegistry()
    register_project_run_handler(
        registry,
        workspace_root=client.app.state.workspace.root,
        router=ModelRouter(),
    )
    worker = Worker(client.app.state.workspace.queue, registry, worker_id=worker_id)
    return worker.run_once()


def _run_row(project, run_id: int):
    return project.db.execute(
        "SELECT status, params FROM runs WHERE id=?", (run_id,)
    ).fetchone()


def _halt_marker(project, run_id: int):
    return persisted_recipe_invocation_halt(_run_row(project, run_id)["params"])


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
    tmp_path, gateway_env, run_engine_stub
):
    """A consented claim-bearing run commits its cell and receipt."""
    client = _client(tmp_path)
    pid, project, sheet_id = _seed_audio_project(client)
    run_id, receipt_id = _launch_consented_run(
        client, pid, project, sheet_id, key="r70-happy@sha256:pin"
    )

    assert _drain_one_job(client, worker_id="r70-happy") is True

    # The run reached its terminal green state and dispatched exactly its rows.
    assert _run_row(project, run_id)["status"] == "completed"
    assert _halt_marker(project, run_id) is None
    assert run_engine_stub == ["moss"]

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
    tmp_path, gateway_env, run_engine_stub
):
    """An identical fresh relaunch can derive exact-match consent, but a new
    row was never part of the confirmed scope. The backfill must mint a fresh
    402 and bind only the exact echo before it may dispatch that row."""
    client = _client(tmp_path)
    pid, project, sheet_id = _seed_audio_project(client)

    run_id, _receipt_id = _launch_consented_run(
        client, pid, project, sheet_id, key="r70-backfill@sha256:pin"
    )
    assert _drain_one_job(client, worker_id="r70-backfill") is True
    assert _run_row(project, run_id)["status"] == "completed"
    assert len(run_engine_stub) == 1

    # First half of "without re-asking": a FRESH relaunch of the very same
    # action over the same rows compiles identical claims, so the gate ADMITS
    # it on the DERIVED consent (no 402 at all) and records its own consent row
    # saying exactly why it was allowed.
    relaunched = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json={
            **_transcribe_action(sheet_id, key="r70-relaunch@sha256:pin"),
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
    assert _drain_one_job(client, worker_id="r70-relaunch") is True
    assert _run_row(project, relaunch_run_id)["status"] == "completed"
    dispatched_before_backfill = len(run_engine_stub)
    # Backfill will reconstruct this single source generation as a fresh scoped
    # run. Its consent must not mutate the source run's chain.
    store = RouteStore.for_run(project, relaunch_run_id)
    consents_before = {c.id for c in store.consents()}
    head_before = store.head()[1].id

    # Second half: a new row of the SAME duration arrives and is BACKFILLED
    # from the unchanged source generation above.
    _add_audio_row(project, sheet_id)

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
    assert len(run_engine_stub) == dispatched_before_backfill
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
    assert len(run_engine_stub) == dispatched_before_backfill + 1

    # Both rows carry a transcript, and both immutable generations remain green.
    assert _transcript_values(project, sheet_id) == [
        "stubbed transcript",
        "stubbed transcript",
    ]
    assert _run_row(project, relaunch_run_id)["status"] == "completed"
    assert _halt_marker(project, relaunch_run_id) is None
    assert _run_row(project, backfill_run_id)["status"] == "completed"
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
