"""Anchor O1 — open self-host as a named fixture.

The complete open composition, exercised end to end through the REAL queued
path: HTTP action route -> queue -> worker claim -> MapRunner -> local
faster_whisper engine (the sandboxed subprocess is the ONE stubbed seam) ->
committed transcript. Focused suites pin pieces of this; this file is
the named anchor asserting the whole composition and O1's RENT BOUND:

    "The trivial local run pays one config read and one recorded route.
     If O1 grows lines, the seam is charging rent."

Concretely:

- resolution consults exactly ONE provider lookup (``probe_counts``), only
  the mapped ``local`` target — gateway/Modal are never probed at any stage;
- exactly one route row + one promise set persist; ZERO consent rows;
- zero user claims compiled, so the gate never appears (no 402);
- the transcript commits through the ordinary run machinery;
- the model-call fact's promised fields are route-derived (provider grouping,
  provider_kind, credential_source, cost_source) with the epoch linked
  under the same route epoch.
"""

from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.actions.system import typed_action_for_request
from frisket.contracts.transcription_sidecar import TRANSCRIPTION_CONTRACT_VERSION
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.jobs.runs import register_project_run_handler
from frisket.engine.jobs.runs import build_attempt_authority
from frisket.engine.runner import MapRunner
from frisket.engine.jobs.worker import HandlerRegistry, Worker
from frisket.engine.store.execution_routes import RouteStore
from frisket.engine.store.media_blobs import media_cell
from frisket.engine.store.runs import RunResultStore

from frisket.execution.definitions import StaticExecutionTargetProvider
from frisket.execution.promises import Promise
from frisket.execution.provider import (
    ExecutionComposition,
    ExecutionCompositionContext,
    ExecutionCompositionFactory,
    open_execution_composition,
)
from frisket.server.app import create_app

pytestmark = pytest.mark.integration


def _route_rows(project, run_id: int):
    """The run's route chain, oldest first. ``RouteStore.routes()`` was
    deleted because production reads the head, so chain-SHAPE assertions read
    the table."""
    return project.db.execute(
        "SELECT * FROM routes WHERE subject_kind='run' AND subject_id=? ORDER BY seq",
        (str(run_id),),
    ).fetchall()


def _request(sheet_id: int):
    return {
        "action_id": "media.transcribe",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"source": "media", "engine": "faster_whisper"},
        "output_names": {"text": "transcript"},
        "idempotency_key": "anchor-o1-transcription",
    }


@pytest.fixture()
def counting_composition_factory() -> tuple[
    ExecutionCompositionFactory, list[StaticExecutionTargetProvider]
]:
    """Every provider the W3 composition factory builds, in construction order."""
    providers: list[StaticExecutionTargetProvider] = []

    def build(project, router, _context) -> ExecutionComposition:
        composition = open_execution_composition(
            project, router, ExecutionCompositionContext.direct()
        )
        provider = composition.provider
        assert isinstance(provider, StaticExecutionTargetProvider)
        providers.append(provider)
        return composition

    return build, providers


@pytest.fixture()
def local_whisper_stub(monkeypatch):
    """Stub ONLY the sandboxed faster_whisper subprocess (the GPU/model
    inference seam); everything else — queue, worker, typed action, route
    binding, fact writer — is real."""
    calls: list[bytes] = []

    async def fake_run_sandboxed(
        cmd, *, policy, stdin_data, should_cancel=None, extra_env=None
    ):
        calls.append(stdin_data)
        return SimpleNamespace(
            ok=True,
            cancelled=False,
            stdout=json.dumps(
                {
                    "contract_version": TRANSCRIPTION_CONTRACT_VERSION,
                    "results": [
                        {
                            "engine": "faster-whisper",
                            "text": "hello from the laptop",
                            "segments": [
                                {
                                    "start": 0.0,
                                    "end": 2.0,
                                    "text": "hello from the laptop",
                                }
                            ],
                            "language": "en",
                            "duration": 2.0,
                            "model_ids": ["faster-whisper/base"],
                            "revision": "runtime-resolved",
                            "device": "cpu",
                            "dtype": "int8",
                            "timings": {"inference_seconds": 0.1},
                            "warnings": [],
                            "accepted_options": {},
                        }
                    ],
                }
            ),
        )

    monkeypatch.setattr(
        "frisket.sdk.ops.transcription.faster_whisper.run_sandboxed", fake_run_sandboxed
    )
    return calls


def _seeded_app_with_audio_row(
    tmp_path: Path,
    *,
    execution_composition_factory: ExecutionCompositionFactory,
):
    client = TestClient(
        create_app(
            tmp_path / "ws",
            execution_composition_factory=execution_composition_factory,
        )
    )
    project_id = client.post("/api/projects", json={"name": "O1 anchor"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Episodes")
    col = project.add_column(sheet_id, "media", type="audio")
    blob = project.add_blob(
        b"RIFFxxxxWAVEfmt O1",
        filename="ep1.wav",
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"duration_seconds": 2.0, "kind": "audio"}
        ),
    )
    [row_id] = project.add_rows(
        sheet_id,
        [{"media": media_cell(blob, mime="audio/wav", filename="ep1.wav")}],
        {"media": col},
    )
    return client, project_id, project, sheet_id, row_id


def test_anchor_o1_atomic_prepare_reuses_the_resolution_provider(
    tmp_path,
    monkeypatch,
    counting_composition_factory,
):
    """Resolution and same-invocation admission share one provider snapshot."""

    for name in ("FRISKET_MODELS_URL", "FRISKET_MODELS_TOKEN", "MODAL_TOKEN_ID"):
        monkeypatch.delenv(name, raising=False)
    from frisket.engine.store import Project

    project = Project.create(tmp_path / "o1-provider-reuse.frisket")
    try:
        sheet_id = project.add_sheet("Episodes")
        media_column = project.add_column(sheet_id, "media", type="audio")
        blob = project.add_blob(
            b"RIFFxxxxWAVEfmt O1",
            filename="ep1.wav",
            mime="audio/wav",
            metadata=owned_media_metadata_document(
                probe={"duration_seconds": 2.0, "kind": "audio"}
            ),
        )
        project.add_rows(
            sheet_id,
            [{"media": media_cell(blob, mime="audio/wav", filename="ep1.wav")}],
            {"media": media_column},
        )
        plan = build_typed_map_rows_plan(
            project, typed_action_for_request(_request(sheet_id))
        )
        composition_factory, counting_providers = counting_composition_factory
        router = ModelRouter()
        composition = composition_factory(
            project, router, ExecutionCompositionContext.direct()
        )
        runner = MapRunner(
            project,
            router,
            allow_action_lifecycle_only_recipes=True,
            authority=build_attempt_authority(
                project,
                composition=composition,
            ),
            execution_composition=composition,
        )
        output_fields = [dict(field) for field in plan.output_fields]

        project.db.execute("BEGIN IMMEDIATE")
        try:
            runner.prepare_admitted_run(
                plan.spec_dict(),
                program=plan.program,
                confirmed=False,
                output_fields=output_fields,
            )
        finally:
            project.db.rollback()

        assert len(counting_providers) == 1
        assert counting_providers[0].probe_counts == {"local": 1}
    finally:
        project.close()


def test_anchor_o1_open_selfhost_local_run_pays_no_rent(
    tmp_path, monkeypatch, counting_composition_factory, local_whisper_stub
):
    # The open edition with nothing configured: no gateway, no Modal, no
    # provider keys required. A configured ambient env must not leak in.
    for name in ("FRISKET_MODELS_URL", "FRISKET_MODELS_TOKEN", "MODAL_TOKEN_ID"):
        monkeypatch.delenv(name, raising=False)

    composition_factory, counting_providers = counting_composition_factory
    client, project_id, project, sheet_id, row_id = _seeded_app_with_audio_row(
        tmp_path,
        execution_composition_factory=composition_factory,
    )

    # --- enqueue over the real HTTP action route, UNCONFIRMED ---------------
    # O1's gate rule: fires only on off-box egress — never here. The launch
    # must queue directly with no 402 and no confirmation round-trip.
    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_request(sheet_id),
    )
    assert response.status_code == 200, response.text
    launched = response.json()
    assert launched["status"] == "queued"
    receipt_id = launched["receipt_id"]

    # --- RENT BOUND: resolution consulted exactly one provider lookup ------
    # The enqueue resolved once: that resolution probed exactly the mapped
    # 'local' target — one config read, nothing else.
    assert len(counting_providers) == 1, (
        "the enqueue must build exactly one provider (one resolution per "
        f"invocation); got {len(counting_providers)}"
    )
    assert counting_providers[0].probe_counts == {"local": 1}

    # --- the real durable worker claims and runs the job --------------------
    registry = HandlerRegistry()
    register_project_run_handler(
        registry,
        workspace_root=client.app.state.workspace.root,
        router=ModelRouter(),
        execution_composition_factory=composition_factory,
    )
    worker = Worker(client.app.state.workspace.queue, registry, worker_id="anchor-o1")
    assert worker.run_once() is True
    assert len(local_whisper_stub) == 1  # the one stubbed inference call

    # No stage of the run — resolution, worker verification, or runtime
    # binding — ever probed a non-local target (the O1 short-circuit).
    probed = {
        target for provider in counting_providers for target in provider.probe_counts
    }
    assert probed == {"local"}

    # --- exactly one route + one promise set, zero consents, zero claims ---
    run_row = project.db.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert run_row is not None and run_row["status"] == "completed"
    run_id = int(run_row["id"])

    store = RouteStore.for_run(project, run_id)
    assert len(_route_rows(project, run_id)) == 1
    route, _head_set = store.head()
    [promise_set] = store.promise_sets()
    assert store.consents() == []  # O1: no consent event ever happened
    assert promise_set.consent_id is None
    assert route.promise_set_id == promise_set.id

    # Route facts: the honest open-edition local answer.
    assert route.engine == "faster_whisper"
    assert route.operator == "self"
    assert route.egress_class == "none"
    assert route.region is None  # honest absence, never pinned in open
    assert route.credential_source == "local"
    assert route.cost_posture == "operator_borne"
    assert route.target_snapshot["target_id"] == "local"
    assert route.target_snapshot["transport"] == "local"

    # Zero user claims (no cost row at all — absence, not a claim of zero).
    promises = [Promise.from_row(row) for row in promise_set.promises]
    assert all(p.audience == "system_promise" for p in promises)
    assert not any(p.field == "cost" for p in promises)

    # --- transcript committed through the ordinary machinery ----------------
    columns = {
        str(c["name"]): c
        for c in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=?", (sheet_id,)
        ).fetchall()
    }
    transcript = project.get_values(
        sheet_id, int(columns["transcript"]["id"]), row_ids=[row_id]
    )[row_id]
    assert transcript == "hello from the laptop"
    receipt = project.db.execute(
        "SELECT status FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    assert receipt is not None and receipt["status"] == "completed"

    # --- fact fields route-derived and epoch-linked -------------------------
    [fact] = RunResultStore(project).model_calls(run_id)
    assert fact["capability"] == "transcribe"
    assert fact["engine"] == "faster_whisper"
    assert fact["provider"] == "local"  # provider grouping from the snapshot
    assert fact["provider_kind"] == "local_process"
    assert fact["credential_source"] == "local"  # the route's pinned source
    assert fact["cost_source"] == "free_local"  # genuinely zero, not unknown
    assert fact["epoch_id"] is not None
    epoch = project.db.execute(
        "SELECT * FROM binding_epochs WHERE id=?", (fact["epoch_id"],)
    ).fetchone()
    assert epoch is not None
    assert epoch["route_id"] == route.id
    # No divergence on the trivial path, and NOTHING in the violations
    # ledger: every compiled promise is evaluable at run start and satisfied.
    # ``credential_source`` compiles NO promise row. As a run-start claim it
    # was tautological here (the route row scored against
    # itself) and unevaluable on the paths where it mattered. The credential
    # question is answered before the effect, at the adapter, by
    # ``frisket.execution.credential_use``; what O1 still pins is that the
    # route's pinned source rides the settlement fact above.
    assert not any(p.field == "credential_source" for p in promises)
    assert store.violations() == []
