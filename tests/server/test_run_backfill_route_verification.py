"""Route-verification coverage: run.backfill resumes a routed recipe through
the DIRECT v1 action path (``_default_map_runner_factory`` /
``_composed_map_runner_factory``, engine/executor/actions.py), which — before
this fix — built its ``MapRunner`` with NO verification wired at all.
A routed, consent-gated transcription run could therefore backfill new rows
with zero verification: a revoked/lowered standing consent never re-gated,
and direct work-scope or target-availability checks never ran.

These tests exercise the surviving authority checks end-to-end through the
REAL direct dispatch path (POST .../actions/v1/run with action_id run.backfill):

- the resolution-aware backfill gate (engine/runner/validation.py's
  ``explicit_resume_scope`` branch), which now reads ``estimate_run``'s own
  ``claims``/``promise_set_hash`` instead of the legacy env-threshold
  CostGate;
- the shared attempt authority (``build_attempt_authority``) built into the
  direct MapRunner factories as a REQUIRED argument, so a resumed routed run
  mints its attempt and verifies the same direct authorization checks as a
  queued fresh claim;
- the typed ``no_live_target`` refusal when the direct path's route binding
  cannot deref.

The original (fresh) run in every test is created and driven to completion
through the REAL queued ``project.run`` handler (``register_project_run_
handler`` / ``verify_execution_route`` — already correctly wired before this
fix) so only the BACKFILL half exercises the new direct-path code.
"""

from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.actions.system import typed_action_for_request
from frisket.engine.executor.map_rows_action import (
    _typed_map_rows_plan,
    typed_program_from_runner_spec,
)
from frisket.engine.jobs.runs import register_project_run_handler
from frisket.engine.jobs.worker import HandlerRegistry, Worker
from frisket.engine.runner import MapRunner
from frisket.engine.store.result_generations import GenerationSealedError
from frisket.engine.store.execution_routes import RouteStore
from frisket.engine.store.media_blobs import media_cell
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.execution.pricing_policy import (
    QuoteFacts,
    RatedQuote,
    install_pricing_policy,
)
from frisket.server.app import create_app


class _AboveLimitPolicy:
    """Keep consent-path coverage above the default preapproval threshold."""

    policy_id = "test.backfill-route.above-limit.v1"

    def rate(self, facts: QuoteFacts) -> RatedQuote:
        return RatedQuote(
            billed_cost=2_000_001,
            provider_cost=facts.provider_cost,
            lane="cost_plus",
            policy_id=self.policy_id,
        )


# ---------------------------------------------------------------------------
# Shared harness (mirrors tests/engine/test_worker_route_verification.py and
# tests/server/test_run_backfill_executor.py).
# ---------------------------------------------------------------------------


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "workspace"))


def _seed_audio_project(client: TestClient) -> tuple[str, object, int]:
    pid = client.post(
        "/api/projects", json={"name": "Backfill route verification"}
    ).json()["id"]
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


@pytest.fixture()
def gateway_env(monkeypatch):
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models.test")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "secret")
    install_pricing_policy(_AboveLimitPolicy())


@pytest.fixture()
def run_engine_stub(monkeypatch):
    """Stub the engine boundary (not ``execute`` — tests/ops/test_transcribe_route_
    binding.py's idiom), so the resumed/original run flows through the real
    require_route_binding / bind_fact_to_route path instead of
    bypassing it wholesale."""
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


def _transcribe_spec(sheet_id: int, engine: str, **options) -> dict:
    bound = typed_action_for_request(
        {
            "action_id": "media.transcribe",
            "idempotency_key": f"backfill-route/{engine}",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {"source": "media", "engine": engine, **options},
            "output_names": {"text": "transcript", "segments": "transcript_segments"},
        }
    )
    return _typed_map_rows_plan(bound).spec_dict()


def _local_spec(sheet_id: int) -> dict:
    return _transcribe_spec(sheet_id, "faster_whisper", vad=True)


def _gateway_spec(sheet_id: int) -> dict:
    # Moss has one declared execution target: the models gateway.  Its route
    # therefore exercises the egress claim across both request and worker
    # compositions without relying on a Parakeet option to retarget execution.
    return _transcribe_spec(sheet_id, "moss")


def _run_original_to_completion(client: TestClient, project_id: str, project, spec):
    """Launch the canonical typed request through the real queued worker."""
    action = {
        "action_id": "media.transcribe",
        "scope": {"kind": "sheet_rows", "sheet_id": spec["sheet_id"]},
        "params": dict(spec["params"]),
        "output_names": dict(spec["output_names"]),
        "idempotency_key": f"backfill-route-original/{spec['engine']}@sha256:stable",
    }
    path = f"/api/projects/{project_id}/actions/v1/run"
    launched_response = client.post(path, json=action)
    if launched_response.status_code == 402:
        challenge = launched_response.json()["errors"][0]["details"]
        action["confirmation"] = challenge["promise_set_hash"]
        launched_response = client.post(path, json=action)
    assert launched_response.status_code == 200, launched_response.text
    launched = launched_response.json()
    assert launched["status"] == "queued", launched
    run_id = int(launched["run_id"])
    registry = HandlerRegistry()
    root = client.app.state.workspace.root
    register_project_run_handler(registry, workspace_root=root, router=ModelRouter())
    worker = Worker(client.app.state.workspace.queue, registry)
    assert worker.run_once()
    job = client.app.state.workspace.queue.get(int(launched["job_id"]))
    assert job is not None and job.status == "done", job.error if job else None
    return run_id


def _backfill_action(*, key: str, confirmation: str | None = None) -> dict:
    return {
        "action_id": "run.backfill",
        "scope": {"kind": "sheet_rows", "sheet_id": 0},
        "params": {"column": "transcript"},
        "output_names": {},
        "idempotency_key": key,
        **({"confirmation": confirmation} if confirmation is not None else {}),
    }


def _post_backfill(client, project_id, sheet_id, *, key, confirmation=None):
    action = _backfill_action(key=key, confirmation=confirmation)
    action["scope"]["sheet_id"] = sheet_id
    return client.post(f"/api/projects/{project_id}/actions/v1/run", json=action)


def _counts(project) -> dict[str, int]:
    return {
        table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("runs", "results", "receipts")
    }


# ---------------------------------------------------------------------------
# (a) resolution-aware backfill gate binds the changed egress scope.
# The longer row is new egress scope, so the 402 must carry that claim and the
# retry must echo its hash before any effect is dispatched.
# ---------------------------------------------------------------------------


def test_backfill_direct_path_reasks_for_changed_scope(
    tmp_path, gateway_env, run_engine_stub
):
    client = _client(tmp_path)
    pid, project, sheet_id = _seed_audio_project(client)

    spec = _gateway_spec(sheet_id)
    run_id = _run_original_to_completion(client, pid, project, spec)
    assert len(run_engine_stub) == 1

    # A longer new row changes the source scope. It was not covered by the
    # launch's narrower confirmation.
    _add_audio_row(project, sheet_id, duration_seconds=40.0)

    before = _counts(project)
    response = _post_backfill(
        client, pid, sheet_id, key="backfill-scope-change@sha256:test"
    )
    assert response.status_code == 402, response.text
    body = response.json()
    assert body["status"] == "needs_confirmation"
    error = body["errors"][0]
    assert error["code"] == "model_cost_requires_confirmation"
    assert error["needs_confirmation"] is True
    # The fresh challenge carries the changed scoped-egress fact.
    claim_fields = sorted(claim["field"] for claim in error["details"]["claims"])
    assert claim_fields == ["egress_class"]
    set_hash = error["details"]["promise_set_hash"]
    assert set_hash
    # Nothing executed and nothing new was written: the gate fires before
    # any row dispatch, exactly like a fresh over-gate run.
    assert _counts(project) == before
    assert len(run_engine_stub) == 1
    run_row = project.db.execute(
        "SELECT status FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    assert run_row["status"] == "completed"  # the ORIGINAL run is untouched

    # Only an exact echo of that challenge admits the new scope.
    confirmed_action = _backfill_action(
        key="backfill-scope-change@sha256:test", confirmation=set_hash
    )
    confirmed_action["scope"]["sheet_id"] = sheet_id
    response = client.post(f"/api/projects/{pid}/actions/v1/run", json=confirmed_action)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    assert len(run_engine_stub) == 2


# ---------------------------------------------------------------------------
# A MapRunner whose authority can never
# authorize routed work refuses loudly before any row dispatch.
# ---------------------------------------------------------------------------


def test_maprunner_refuses_to_reopen_a_sealed_routed_generation(
    tmp_path, gateway_env, run_engine_stub
) -> None:
    client = _client(tmp_path)
    pid, project, sheet_id = _seed_audio_project(client)
    spec = _gateway_spec(sheet_id)
    run_id = _run_original_to_completion(client, pid, project, spec)
    assert len(run_engine_stub) == 1

    unverified_runner = MapRunner(
        project,
        ModelRouter(),
        authority=UnroutedOnlyAuthority(project),
        allow_action_lifecycle_only_recipes=True,
    )
    stored_spec = json.loads(
        project.db.execute("SELECT params FROM runs WHERE id=?", (run_id,)).fetchone()[
            0
        ]
    )
    with pytest.raises(GenerationSealedError, match="fresh explicitly scoped run"):
        asyncio.run(
            unverified_runner.run(
                stored_spec,
                confirmed=True,
                resume_run_id=run_id,
                program=typed_program_from_runner_spec(project, stored_spec),
            )
        )
    assert len(run_engine_stub) == 1
    run_row = project.db.execute(
        "SELECT status FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    assert run_row["status"] == "completed"


# ---------------------------------------------------------------------------
# (d) fully-covered claims proceed without a false 402.
# ---------------------------------------------------------------------------


def test_backfill_direct_path_fully_covered_claims_proceeds(tmp_path, run_engine_stub):
    client = _client(tmp_path)
    pid, project, sheet_id = _seed_audio_project(client)
    spec = _local_spec(sheet_id)  # local/free: zero user claims, no gate ever
    run_id = _run_original_to_completion(client, pid, project, spec)
    assert len(run_engine_stub) == 1

    _add_audio_row(project, sheet_id)

    response = _post_backfill(client, pid, sheet_id, key="backfill-covered@sha256:test")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    output = next(item for item in body["outputs"] if item["kind"] == "run_backfill")
    assert output["ref"]["filled"] == 1
    assert len(run_engine_stub) == 2
    run_row = project.db.execute(
        "SELECT status FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    assert run_row["status"] == "completed"


# ---------------------------------------------------------------------------
# (e) RouteBindingUnavailable on the direct dispatch surfaces as
# no_live_target, not a generic map_error_code.
# ---------------------------------------------------------------------------


def test_backfill_direct_path_dead_target_surfaces_no_live_target(
    tmp_path, gateway_env, run_engine_stub, monkeypatch
):
    client = _client(tmp_path)
    pid, project, sheet_id = _seed_audio_project(client)
    spec = _gateway_spec(sheet_id)
    run_id = _run_original_to_completion(client, pid, project, spec)
    assert len(run_engine_stub) == 1

    _add_audio_row(project, sheet_id)
    # The gateway dies between the original run and this backfill.
    monkeypatch.delenv("FRISKET_MODELS_URL", raising=False)
    monkeypatch.delenv("FRISKET_MODELS_TOKEN", raising=False)

    response = _post_backfill(
        client, pid, sheet_id, key="backfill-dead-target@sha256:test"
    )
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["status"] == "failed"
    error = body["errors"][0]
    assert error["code"] == "no_live_target"
    assert "FRISKET_MODELS_URL" in error["message"]
    assert len(run_engine_stub) == 1
    run_row = project.db.execute(
        "SELECT status FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    # The resume admitted (MapRunner.run's begin_run_resume flipped the
    # run to "running") before the route binding refused, but the pre-dispatch
    # refusal reverts the run to the EXACT status admission observed — zero
    # rows dispatched means nothing about the run changed. The failure belongs
    # to the action, whose boundary cleans up its reservation and output
    # claims; the `runs` row is never left stale at "running".
    assert run_row["status"] == "completed"


# ---------------------------------------------------------------------------
# (f) A routed, claim-bearing backfill with an unchanged quote still re-asks
# for a new row's egress scope, then proceeds through the direct path only
# after the exact scope-bound echo. (d) above remains the zero-claim control:
# its local engine compiles no user_claim, so no consent gate is expected.
# ---------------------------------------------------------------------------


def test_backfill_direct_path_claim_bearing_new_scope_reasks_then_proceeds(
    tmp_path, gateway_env, run_engine_stub
):
    client = _client(tmp_path)
    pid, project, sheet_id = _seed_audio_project(client)
    spec = _gateway_spec(sheet_id)
    run_id = _run_original_to_completion(client, pid, project, spec)
    store = RouteStore.for_run(project, run_id)
    claims = [p for p in store.head()[1].promises if p.get("audience") == "user_claim"]
    # Genuinely claim-bearing (egress), and genuinely consented.
    assert [p["field"] for p in claims] == ["egress_class"]
    assert store.consents()

    # A row of the SAME duration leaves the quote unchanged, but its source
    # value was never in the launch's confirmed scope.
    _add_audio_row(project, sheet_id)

    gated = _post_backfill(
        client, pid, sheet_id, key="backfill-claim-bearing@sha256:test"
    )
    assert gated.status_code == 402, gated.text
    details = gated.json()["errors"][0]["details"]
    assert [claim["field"] for claim in details["claims"]] == ["egress_class"]
    set_hash = details["promise_set_hash"]
    assert set_hash
    assert len(run_engine_stub) == 1

    confirmed_action = _backfill_action(
        key="backfill-claim-bearing@sha256:test", confirmation=set_hash
    )
    confirmed_action["scope"]["sheet_id"] = sheet_id
    response = client.post(f"/api/projects/{pid}/actions/v1/run", json=confirmed_action)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    output = next(item for item in body["outputs"] if item["kind"] == "run_backfill")
    assert output["ref"]["filled"] == 1
    assert len(run_engine_stub) == 2  # the new row really dispatched
    run_row = project.db.execute(
        "SELECT status FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    assert run_row["status"] == "completed"


def test_backfill_row_scope_does_not_shift_action_identity(
    tmp_path, gateway_env, run_engine_stub
):
    """The mechanism behind (f), asserted directly: the runner spec a
    backfill threads (the stored spec + injected row scope + the boundary's
    flow flags and terminal markers) projects to the SAME consent identity as
    the pristine launch spec. Every one of those extra keys is either outside
    the declaration-derived allowlist or explicitly excluded from it."""
    from frisket.execution.resolve_for_action import action_identity_hash

    client = _client(tmp_path)
    _pid, _project, sheet_id = _seed_audio_project(client)
    launch = _gateway_spec(sheet_id)
    backfill_shaped = dict(
        launch,
        row_ids=[7, 8],
        confirmed=True,
        consented_promise_set_hash="deadbeef",
        halted_code="promise_violation",
        halted_reason="a prior halt",
        group_label="grp",
        overwrite=True,
        _frisket_queued_action_run={"schema_version": "x"},
    )
    assert action_identity_hash(backfill_shaped) == action_identity_hash(launch)
    # ...while a genuine intent change (the engine) still shifts it.
    assert action_identity_hash(
        dict(
            launch,
            engine="faster_whisper",
            params={"source": "media", "engine": "faster_whisper"},
        )
    ) != action_identity_hash(launch)


# ---------------------------------------------------------------------------
# (g) The direct path's TYPED verification branch. A worker-verification
# refusal on a direct dispatch used to fall into the generic
# (ValueError, RuntimeError) arm and surface as the op's ``map_error_code``
# (``transcribe_run_failed``) — an untyped failure with no remedy, over a run
# the MapRunner had already reverted. The receipt now carries the code and
# the remedy, so the refusal is actionable instead of opaque.
# ---------------------------------------------------------------------------


def test_backfill_fresh_run_does_not_depend_on_old_run_route_artifacts(
    tmp_path, run_engine_stub
):
    client = _client(tmp_path)
    pid, project, sheet_id = _seed_audio_project(client)
    spec = _local_spec(sheet_id)
    run_id = _run_original_to_completion(client, pid, project, spec)
    _add_audio_row(project, sheet_id)

    # The disclosed crash window: the run row committed, the route
    # artifacts never landed. Verification must fail closed.
    project.db.execute(
        "DELETE FROM binding_epochs WHERE route_id IN "
        "(SELECT id FROM routes WHERE subject_kind='run' AND subject_id=?)",
        (str(run_id),),
    )
    # No route_violations cleanup needed: a local run's every compiled
    # promise is evaluable and satisfied at run start (credential_source
    # included — the route's pinned source is the dispatch fact on a
    # zero-tariff transport), so the completed original leaves an EMPTY
    # ledger and nothing FK-references the routes about to be deleted.
    assert RouteStore.for_run(project, run_id).violations() == []
    # The run's execution_attempts rows FK-reference the route,
    # promise set and consent this crash window erases, so they go first — an
    # attempt cannot outlive the authorization it names.
    project.db.execute("UPDATE runs SET current_attempt_id=NULL WHERE id=?", (run_id,))
    project.db.execute("DELETE FROM execution_attempts WHERE run_id=?", (run_id,))
    for table in ("routes", "promise_sets", "consents"):
        project.db.execute(
            f"DELETE FROM {table} WHERE subject_kind='run' AND subject_id=?",
            (str(run_id),),
        )
    project.db.commit()

    response = _post_backfill(
        client, pid, sheet_id, key="backfill-unverifiable@sha256:test"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["run_id"] != run_id
    assert len(run_engine_stub) == 2
    run_row = project.db.execute(
        "SELECT status FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    assert run_row["status"] == "completed"
