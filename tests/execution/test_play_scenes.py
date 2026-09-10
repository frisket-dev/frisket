"""Named execution-scene tests not already pinned by the lane suites:

- Scene 5 — "Free, but it leaves the box": an answered preapproval covers the
  selected gateway provider even when the retail quote is zero.
- Scene 7 — "The three refusals": three failures, three vocabularies, three
  remedies — never one blurred "unavailable".

Scenes 1 (O1 anchor), 2/6 (O2/O3 anchors), and 3 (redeploy-resume
epoch test) live in their own named files.
"""

from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

import json
from pathlib import Path

import pytest

from frisket.ai.llm import ModelRouter
from frisket.actions.system import typed_action_for_request
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.jobs.ports import JobHandlerContext
from frisket.engine.jobs.queue import CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY
from frisket.engine.jobs.runs import register_project_run_handler
from frisket.engine.jobs.worker import HandlerRegistry
from frisket.engine.runner import validation
from frisket.execution.pricing_policy import default_pricing_policy
from frisket.execution.provider import (
    ExecutionCompositionContext,
    ExecutionCompositionFactory,
    open_execution_composition,
)
from frisket.engine.runner.validation import ExecutionResolutionRefused
from frisket.engine.store.execution_routes import CONSENT_BASIS_PREAPPROVED
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import media_cell
from frisket.engine.store.runs import RunResultStore
from frisket.project_identity import ProjectStorageKey
from frisket.server.action_enqueue import attach_run_edition_run_context
from frisket.server.services.action_runs import ActionRunService
from frisket.server.workspace import Workspace


@pytest.fixture()
def workspace(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    project = Project.create(root / "proj.frisket")
    sheet_id = project.add_sheet("S")
    col = project.add_column(sheet_id, "media", type="audio")
    blob = project.add_blob(
        b"RIFFxxxxWAVEfmt scenes",
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
    try:
        yield root, project, sheet_id
    finally:
        project.close()


@pytest.fixture()
def adapter_stub(monkeypatch):
    calls: list[dict] = []

    async def fake_transcribe(engine, path, spec, ctx, **kwargs):
        calls.append({"engine": engine})
        return transcribe_engines.TranscriptionEngineResult(
            output={"text": "stubbed", "segments": [], "language": "en", "cost": 0},
            model_calls=(),
        )

    from frisket.sdk.ops import transcribe_engines

    monkeypatch.setattr(transcribe_engines, "run_transcription_engine", fake_transcribe)
    return calls


def _spec(sheet_id: int, **overrides):
    params = {"source": "media", "engine": "faster_whisper", "vad": True}
    params.update(overrides)
    if params["engine"] == "moss":
        # The v1 moss engine refuses a supplied vad at authoring, so a
        # production runner spec for it has no vad key — mirror that here
        # (the resolver refuses, never drops, a vad the target cannot
        # honor).
        params.pop("vad", None)
    return {
        "action_id": "media.transcribe",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": params,
        "output_names": {"text": "transcript"},
        "idempotency_key": f"play-scenes/{params['engine']}",
    }


def _validate(project, spec, *, confirmed=False):
    router = ModelRouter()
    plan = build_typed_map_rows_plan(project, typed_action_for_request(spec))
    return validation.validate_spec(
        project,
        router,
        RunResultStore(project),
        plan.spec_dict(),
        program=plan.program,
        confirmed=confirmed,
        resume_run_id=None,
        pricing_policy=default_pricing_policy(),
        composition=open_execution_composition(
            project, router, ExecutionCompositionContext.direct()
        ),
    )


def _handle(
    root: Path,
    run_id: int,
    payload: dict,
    *,
    execution_composition_factory: ExecutionCompositionFactory | None = None,
) -> dict:
    registry = HandlerRegistry()
    register_project_run_handler(
        registry,
        workspace_root=root,
        router=ModelRouter(),
        execution_composition_factory=execution_composition_factory,
    )
    assert int(payload["run_id"]) == run_id
    handler = registry.get("project.run")
    assert handler is not None
    return handler(dict(payload), JobHandlerContext.without_job_row())


def test_queued_composition_uses_claimed_and_durable_facts_then_replays_without_it(
    workspace, adapter_stub
) -> None:
    root, project, sheet_id = workspace
    observed: list[ExecutionCompositionContext] = []

    def factory(project, router, context):
        observed.append(context)
        return open_execution_composition(project, router, context)

    run_id, payload = _prepare_queued_run(
        root,
        sheet_id,
        _spec(sheet_id),
        execution_composition_factory=factory,
    )
    observed.clear()
    snapshot = {"reservation_id": 17, "owner": {"kind": "edition"}}
    attach_run_edition_run_context(project, run_id, snapshot)
    storage_key = ProjectStorageKey(storage_org_id=41, project_slug="proj")
    payload[CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY] = storage_key
    payload["edition_run_context"] = {"reservation_id": "payload-is-not-authority"}

    registry = HandlerRegistry()
    register_project_run_handler(
        registry,
        workspace_root=root,
        router=ModelRouter(),
        require_storage_identity=True,
        workspace_root_storage_org_id=41,
        execution_composition_factory=factory,
    )
    handler = registry.get("project.run")
    assert handler is not None
    result = handler(
        dict(payload), JobHandlerContext.from_claimed_job(trusted_org_id=73)
    )
    assert result["status"] == "completed"
    assert len(observed) == 1
    context = observed[0]
    assert context.storage_key == storage_key
    assert context.run_id == run_id
    assert context.trusted_job_org_id == 73
    assert context.edition_snapshot == snapshot
    assert adapter_stub == [{"engine": "faster_whisper"}]

    replay_registry = HandlerRegistry()

    def unexpected_factory(*_args):
        raise AssertionError("terminal replay must not recompose execution")

    register_project_run_handler(
        replay_registry,
        workspace_root=root,
        router=ModelRouter(),
        require_storage_identity=True,
        workspace_root_storage_org_id=41,
        execution_composition_factory=unexpected_factory,
    )
    replay_handler = replay_registry.get("project.run")
    assert replay_handler is not None
    replay = replay_handler(
        dict(payload), JobHandlerContext.from_claimed_job(trusted_org_id=73)
    )
    assert replay["status"] == "completed"
    assert replay["skipped"] is True
    assert adapter_stub == [{"engine": "faster_whisper"}]


# ---------------------------------------------------------------------------
# Scene 4 — route-cost authorization
# ---------------------------------------------------------------------------


def _prepare_queued_run(
    root: Path,
    sheet_id: int,
    spec: dict,
    *,
    execution_composition_factory: ExecutionCompositionFactory | None = None,
) -> tuple[int, dict]:
    """Prepare through the receipt/claim/admitted-attempt queue boundary."""

    workspace = Workspace(
        root,
        router=ModelRouter(),
        enable_local_model_pull=False,
        execution_composition_factory=execution_composition_factory,
    )
    action = dict(spec)
    service = ActionRunService(workspace)
    response = service.run_action("proj", action)
    if response.status_code == 402:
        challenge = response.payload["errors"][0]["details"]
        action["confirmation"] = challenge["promise_set_hash"]
        response = service.run_action("proj", action)
    assert response.status_code == 200, response.payload
    assert response.payload["status"] == "queued", response.payload
    job = workspace.queue.get(int(response.payload["job_id"]))
    assert job is not None
    return int(response.payload["run_id"]), dict(job.payload)


# ---------------------------------------------------------------------------
# Scene 5 — an under-limit quote includes its selected provider
# ---------------------------------------------------------------------------


def test_scene5_free_gateway_uses_preapproval_for_its_selected_provider(
    workspace, monkeypatch
):
    """A saved spend approval includes sending this run's data to the provider
    the user selected. A zero-dollar quote therefore needs no second egress
    prompt, but admission still materializes an exact provider-bound consent."""
    _root, project, sheet_id = workspace
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models.example.internal")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "secret")

    validated = _validate(project, _spec(sheet_id, engine="moss"))

    assert validated.est is not None
    assert validated.est["billed_cost"] == 0
    assert validated.est["requires_confirmation"] is False
    assert validated.consent_required is True
    assert validated.consent_basis == CONSENT_BASIS_PREAPPROVED
    assert validated.resolved_execution is not None
    claims = [
        promise
        for promise in validated.resolved_execution.promise_set.promises
        if promise.audience == "user_claim"
    ]
    assert [(claim.field, claim.value) for claim in claims] == [
        ("egress_class", "operator_lan")
    ]


# ---------------------------------------------------------------------------
# Scene 7 — the three refusals
# ---------------------------------------------------------------------------


def test_scene7_three_refusals_speak_distinct_vocabularies(
    workspace, adapter_stub, monkeypatch
):
    """Scene 7: three runs fail to resolve/verify; each failure speaks its
    own vocabulary with its own remedy — never one blurred 'unavailable'.

    route resolution's three: ``no_capable_target`` (the authored engine option names an
    ability no target declares), ``no_live_target`` (the mapped gateway
    target exists but is unconfigured — remedy names the exact env), and
    ``stale_head`` (the queued receipt no longer names its exact admitted
    attempt, fail-closed). The play's policy/funding
    refusals (``policy_denied`` / ``unfunded``) are named in the vocabulary
    but first produced by later lanes.
    """
    root, project, sheet_id = workspace

    # 1. no_capable_target — valid authoring, but no target declares it.
    # Unknown engine names are rejected even earlier by typed Params.
    from frisket.execution.definitions import StaticExecutionTargetProvider

    with monkeypatch.context() as missing_target:
        missing_target.setattr(
            StaticExecutionTargetProvider, "targets", lambda self: ()
        )
        with pytest.raises(ExecutionResolutionRefused) as no_capable:
            _validate(project, _spec(sheet_id))
    assert no_capable.value.family == "no_capable_target"

    # 2. no_live_target — the mapped target exists; it is not configured.
    monkeypatch.delenv("FRISKET_MODELS_URL", raising=False)
    monkeypatch.delenv("FRISKET_MODELS_TOKEN", raising=False)
    with pytest.raises(ExecutionResolutionRefused) as no_live:
        _validate(project, _spec(sheet_id, engine="moss"))
    assert no_live.value.family == "no_live_target"
    assert "FRISKET_MODELS_URL" in str(no_live.value)  # the remedy, not a shrug

    # 3. stale_head — the queued receipt must name one exact admitted attempt.
    run_id, payload = _prepare_queued_run(root, sheet_id, _spec(sheet_id))
    receipt_id = payload["v1_receipt_id"]
    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?",
        (receipt_id,),
    ).fetchone()
    receipt_body = json.loads(receipt_row["body"])
    prepared = next(
        evidence["ref"]
        for evidence in receipt_body["evidence"]
        if evidence["ref"].get("kind") == "queued_action_run_prepared"
    )
    prepared["attempt_id"] = "attempt_missing_scene7"
    project.db.execute(
        "UPDATE receipts SET body=? WHERE id=?",
        (json.dumps(receipt_body), receipt_id),
    )
    project.db.commit()
    result = _handle(root, run_id, payload)
    assert result["status"] == "failed"
    stale_error = result["action_result"]["errors"][0]
    assert stale_error["code"] == "stale_head"
    assert adapter_stub == []  # fail closed: nothing executed

    # Three failures, three vocabularies, three remedies.
    vocabularies = {
        no_capable.value.family,
        no_live.value.family,
        stale_error["code"],
    }
    assert vocabularies == {
        "no_capable_target",
        "no_live_target",
        "stale_head",
    }
    remedies = {
        str(no_capable.value),
        str(no_live.value),
        stale_error["message"],
    }
    assert len(remedies) == 3
