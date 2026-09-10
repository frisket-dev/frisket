"""Adversarial regressions for consent that must follow the paid data scope."""

from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import LLMResponse, ModelRouter
from frisket.engine.executor.map_rows_action import typed_program_from_runner_spec
from frisket.engine.store.execution_routes import RouteStore
from frisket.engine.store.media_blobs import media_cell
from frisket.engine.store.runs import RunResultStore
from tests.execution_composition_helpers import open_attempt_authority
from frisket.execution.runtime_binding import ExecutionRouteVerificationFailed
from frisket.ops.base import persisted_recipe_invocation_halt
from frisket.server.app import create_app
from http_test_helpers import drain_queue, post_cell_edit_as_v1_action
from tests.server.test_r70_behavioral_pin import (
    _add_audio_row,
    _client,
    _drain_one_job,
    _launch_consented_run,
    _run_row,
    _seed_audio_project,
    _transcribe_action,
)

pytest_plugins = ("tests.server.test_r70_behavioral_pin",)


class _RecordingClassifyAdapter:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def complete(self, req, client):  # noqa: ANN001
        self.calls.append(req)
        return LLMResponse(
            content='{"relevance": 7}',
            data={"relevance": 7},
            tokens_in=10,
            tokens_out=5,
            cost=0.0001,
            model=req.model,
        )


def _classify_action(sheet_id: int) -> dict:
    """A typed paid map request; consent only ever arrives as the top-level
    ``confirmation`` echo of a quote, never as a flag inside ``params``."""
    return {
        "action_id": "map.classify",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["text"],
            "engine": "llm",
            "model": "anthropic/claude-opus-4-8",
            "context": "Classify each row.",
            "fields": [
                {
                    "name": "relevance",
                    "type": "score",
                    "description": "0-10 relevance",
                }
            ],
        },
        "output_names": {},
        "idempotency_key": "scope-content@sha256:equal-estimate",
    }


def _stored_program(project, stored_spec: dict):
    """Rebuild the typed program from the durable run spec — the binding
    route verification and backfill use, and hash-equivalent to the queued
    worker's ``envelope.program`` — so the mint below is adjudicated against
    exactly what was consented to."""
    program = typed_program_from_runner_spec(project, stored_spec)
    assert program is not None, stored_spec
    return program


def test_confirmation_echo_refuses_same_row_after_equal_quote_content_edit(
    tmp_path, monkeypatch
) -> None:
    """The approved row identity and price do not authorize different data.

    This is the ordinary UI ordering: quote, edit the source cell while the
    dialog is open, then confirm.  Equal-length content deliberately keeps the
    token estimate and dollar quote identical, so only content ownership can
    distinguish what the user saw from what the provider would receive.
    """
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    adapter = _RecordingClassifyAdapter()
    router = ModelRouter(keys={"anthropic": "test"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    client = TestClient(create_app(tmp_path / "workspace", router=router))
    project_id = client.post("/api/projects", json={"name": "Scope"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("data")
    column_id = project.add_column(sheet_id, "text", type="text")
    [row_id] = project.add_rows(
        sheet_id,
        [{"text": "A" * 800}],
        {"text": column_id},
    )
    action = _classify_action(sheet_id)

    gated = client.post(f"/api/projects/{project_id}/actions/v1/run", json=action)
    assert gated.status_code == 402, gated.text
    details = gated.json()["errors"][0]["details"]
    quoted_estimate = details["estimate"]
    confirmation_hash = details["promise_set_hash"]

    edited = post_cell_edit_as_v1_action(
        client,
        project_id,
        [
            {
                "row_id": row_id,
                "column_id": column_id,
                "value": "B" * 800,
            }
        ],
        idempotency_key="scope-content-edit@sha256:equal-estimate",
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["status"] == "completed"

    estimated_after_edit = client.post(
        f"/api/projects/{project_id}/actions/v1/estimate",
        json={"action": action},
    )
    assert estimated_after_edit.status_code == 200, estimated_after_edit.text
    estimate_fields = {
        "cost",
        "rows",
        "llm",
        "avg_input_tokens",
        "est_output_tokens",
    }
    assert {
        key: value
        for key, value in estimated_after_edit.json()["estimate"].items()
        if key in estimate_fields
    } == {
        key: value for key, value in quoted_estimate.items() if key in estimate_fields
    }

    confirmed = deepcopy(action)
    confirmed["confirmation"] = confirmation_hash
    retry = client.post(f"/api/projects/{project_id}/actions/v1/run", json=confirmed)
    # Drain regardless of outcome: if the stale echo was wrongly admitted,
    # this turns the latent queued effect into an observed provider call.
    drain_queue(client, worker_id="scope-content-worker")

    observed = {
        "http_status": retry.status_code,
        "action_status": retry.json().get("status"),
        "runs": project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
        "provider_calls": len(adapter.calls),
        "model_calls": project.db.execute(
            "SELECT COUNT(*) FROM model_calls"
        ).fetchone()[0],
    }
    assert observed == {
        "http_status": 402,
        "action_status": "needs_confirmation",
        "runs": 0,
        "provider_calls": 0,
        "model_calls": 0,
    }


def test_exact_match_consent_does_not_cross_to_a_different_equal_quote_row(
    tmp_path, gateway_env, run_engine_stub
) -> None:
    """Consent to send row A cannot silently authorize equal-price row B."""
    client = _client(tmp_path)
    project_id, project, sheet_id = _seed_audio_project(client, name="Scope rows")
    _add_audio_row(project, sheet_id, duration_seconds=2.0)
    row_a, row_b = [
        int(row["id"])
        for row in project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (sheet_id,)
        ).fetchall()
    ]

    first = _transcribe_action(sheet_id, key="scope-row-a@sha256:consent")
    first["scope"]["row_ids"] = [row_a]
    gated = client.post(f"/api/projects/{project_id}/actions/v1/run", json=first)
    assert gated.status_code == 402, gated.text
    confirmation_hash = gated.json()["errors"][0]["details"]["promise_set_hash"]

    confirmed = deepcopy(first)
    confirmed["confirmation"] = confirmation_hash
    accepted = client.post(f"/api/projects/{project_id}/actions/v1/run", json=confirmed)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "queued"
    assert _drain_one_job(client, worker_id="scope-row-a") is True
    assert len(run_engine_stub) == 1

    runs_before = project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    second = _transcribe_action(sheet_id, key="scope-row-b@sha256:fresh")
    second["scope"]["row_ids"] = [row_b]
    second["replace_existing"] = True
    unconfirmed = client.post(f"/api/projects/{project_id}/actions/v1/run", json=second)
    drained = _drain_one_job(client, worker_id="scope-row-b")

    observed = {
        "http_status": unconfirmed.status_code,
        "action_status": unconfirmed.json().get("status"),
        "runs": project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
        "provider_calls": len(run_engine_stub),
        "second_job_drained": drained,
    }
    assert observed == {
        "http_status": 402,
        "action_status": "needs_confirmation",
        "runs": runs_before,
        "provider_calls": 1,
        "second_job_drained": False,
    }


def test_worker_refuses_when_confirmed_source_changes_after_enqueue(
    tmp_path, gateway_env, run_engine_stub
) -> None:
    """Worker admission rechecks the scope that the request-time 402 bound.

    This exercises the post-enqueue window after generic promise interruption
    was removed: source-scope identity remains direct authorization and must
    refuse before the provider sees the replacement.
    """
    client = _client(tmp_path)
    project_id, project, sheet_id = _seed_audio_project(
        client, name="Post-enqueue scope"
    )
    run_id, receipt_id = _launch_consented_run(
        client,
        project_id,
        project,
        sheet_id,
        key="post-enqueue-scope@sha256:confirmed",
    )

    source_column = next(
        column for column in project.columns(sheet_id) if column["name"] == "media"
    )
    row_id = int(
        project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position LIMIT 1",
            (sheet_id,),
        ).fetchone()["id"]
    )
    replacement = project.add_blob(
        b"RIFFreplacementWAVEfmt ",
        filename="replacement.wav",
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"duration_seconds": 2.0, "kind": "audio"}
        ),
    )
    edited = post_cell_edit_as_v1_action(
        client,
        project_id,
        [
            {
                "row_id": row_id,
                "column_id": int(source_column["id"]),
                "value": media_cell(
                    replacement,
                    mime="audio/wav",
                    filename="replacement.wav",
                ),
            }
        ],
        idempotency_key="post-enqueue-source-edit@sha256:replacement",
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["status"] == "completed"

    assert _drain_one_job(client, worker_id="post-enqueue-scope") is True

    # Typed admission's input snapshot now catches the changed source before
    # routed dispatch. It does not require a resumable invocation halt.
    store = RouteStore.for_run(project, run_id)
    _route, promise_set = store.head()
    egress_claim = next(
        promise
        for promise in promise_set.promises
        if promise["field"] == "egress_class"
    )
    assert egress_claim["basis"]["work_scope"]

    run_row = _run_row(project, run_id)
    assert run_row["status"] == "failed", dict(run_row)
    halt = persisted_recipe_invocation_halt(run_row["params"])
    assert halt is None
    assert run_engine_stub == []
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM model_calls WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        == 0
    )

    receipt = client.get(f"/api/projects/{project_id}/actions/v1/receipts/{receipt_id}")
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["status"] == "failed"
    assert receipt.json()["errors"][0]["code"] == "stale_input"


def _unrouted_classify_client(tmp_path, monkeypatch, *, name: str):
    """A gated unrouted (LLM map) project: stub adapter, one 800-char row."""
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    adapter = _RecordingClassifyAdapter()
    router = ModelRouter(keys={"anthropic": "test"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    client = TestClient(create_app(tmp_path / "workspace", router=router))
    project_id = client.post("/api/projects", json={"name": name}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("data")
    column_id = project.add_column(sheet_id, "text", type="text")
    [row_id] = project.add_rows(
        sheet_id,
        [{"text": "A" * 800}],
        {"text": column_id},
    )
    return adapter, client, project_id, project, sheet_id, column_id, row_id


def _confirm_and_enqueue(
    client: TestClient, project_id: str, action: dict
) -> tuple[int, str]:
    """402-gate then echo the hash; returns the queued (run_id, receipt_id)."""
    gated = client.post(f"/api/projects/{project_id}/actions/v1/run", json=action)
    assert gated.status_code == 402, gated.text
    confirmation_hash = gated.json()["errors"][0]["details"]["promise_set_hash"]
    confirmed = deepcopy(action)
    confirmed["confirmation"] = confirmation_hash
    accepted = client.post(f"/api/projects/{project_id}/actions/v1/run", json=confirmed)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "queued"
    return int(accepted.json()["run_id"]), accepted.json()["receipt_id"]


def test_unrouted_worker_refuses_when_confirmed_source_changes_after_enqueue(
    tmp_path, monkeypatch
) -> None:
    """The unrouted sibling of the routed post-enqueue scope recheck above.

    Worker admission (``AttemptAuthority._admit``, unrouted branch) recomputes
    the confirmation context hash from the CURRENT cells and quote and requires
    it to equal a persisted consent.  Editing the source cell between confirm
    and drain — with equal-length content, so the token estimate and dollar
    quote stay identical and only content identity can distinguish what the
    user approved from what the provider would receive — must fail the run
    durably with ``consent_missing`` before any provider call.
    """
    adapter, client, project_id, project, sheet_id, column_id, row_id = (
        _unrouted_classify_client(tmp_path, monkeypatch, name="Unrouted recheck")
    )
    action = _classify_action(sheet_id)
    action["idempotency_key"] = "unrouted-post-enqueue@sha256:confirmed"
    run_id, receipt_id = _confirm_and_enqueue(client, project_id, action)

    edited = post_cell_edit_as_v1_action(
        client,
        project_id,
        [
            {
                "row_id": row_id,
                "column_id": column_id,
                "value": "B" * 800,
            }
        ],
        idempotency_key="unrouted-post-enqueue-edit@sha256:replacement",
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["status"] == "completed"

    drain_queue(client, worker_id="unrouted-post-enqueue")

    receipt = client.get(f"/api/projects/{project_id}/actions/v1/receipts/{receipt_id}")
    assert receipt.status_code == 200, receipt.text
    run_row = project.db.execute(
        "SELECT status FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    observed = {
        "run_status": run_row["status"],
        "provider_calls": len(adapter.calls),
        "model_calls": project.db.execute(
            "SELECT COUNT(*) FROM model_calls WHERE run_id=?", (run_id,)
        ).fetchone()[0],
        "receipt_status": receipt.json()["status"],
        "receipt_error_code": receipt.json()["errors"][0]["code"],
    }
    assert observed == {
        "run_status": "failed",
        "provider_calls": 0,
        "model_calls": 0,
        "receipt_status": "failed",
        "receipt_error_code": "consent_missing",
    }


def test_unrouted_attempt_naming_rows_outside_recorded_scope_is_refused(
    tmp_path, monkeypatch
) -> None:
    """An attempt cannot widen its scope past the durable run row scope.

    The subset fence in ``AttemptAuthority._admit`` refuses BEFORE the quote
    is recomputed, so a widened attempt is ``consent_missing`` even when every
    named row exists.  The distinctive message is asserted so this pins the
    subset fence itself, not the live-hash recheck downstream of it.
    """
    adapter, client, project_id, project, sheet_id, column_id, row_id = (
        _unrouted_classify_client(tmp_path, monkeypatch, name="Unrouted subset")
    )
    action = _classify_action(sheet_id)
    action["idempotency_key"] = "unrouted-scope-subset@sha256:confirmed"
    run_id, _receipt_id = _confirm_and_enqueue(client, project_id, action)
    drain_queue(client, worker_id="unrouted-scope-subset")
    assert len(adapter.calls) == 1  # the consented run itself completed

    # The fence compares against the durable run row scope; prove that scope
    # exists so the assertion below exercises the recorded-scope branch.
    assert RunResultStore(project).has_run_row_scope(run_id) is True
    [extra_row_id] = project.add_rows(
        sheet_id,
        [{"text": "C" * 800}],
        {"text": column_id},
    )
    stored_spec = json.loads(
        project.db.execute("SELECT params FROM runs WHERE id=?", (run_id,)).fetchone()[
            "params"
        ]
    )

    with pytest.raises(ExecutionRouteVerificationFailed) as refusal:
        open_attempt_authority(project).mint(
            recipe=_stored_program(project, stored_spec),
            spec=stored_spec,
            run_id=run_id,
            scope=(row_id, extra_row_id),
        )

    assert refusal.value.code == "consent_missing"
    assert "outside the scope recorded" in str(refusal.value)
    assert len(adapter.calls) == 1
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM model_calls WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        == 1
    )


def _consented_unrouted_run(tmp_path, monkeypatch, *, name: str, key: str):
    """Mint a real unrouted consent through the 402 echo, drain it, and hand
    back everything needed to re-admit the same run at dispatch."""
    adapter, client, project_id, project, sheet_id, column_id, row_id = (
        _unrouted_classify_client(tmp_path, monkeypatch, name=name)
    )
    action = _classify_action(sheet_id)
    action["idempotency_key"] = f"{key}@sha256:confirmed"
    run_id, _receipt_id = _confirm_and_enqueue(client, project_id, action)
    drain_queue(client, worker_id=key)
    assert len(adapter.calls) == 1
    stored_spec = json.loads(
        project.db.execute("SELECT params FROM runs WHERE id=?", (run_id,)).fetchone()[
            "params"
        ]
    )
    return adapter, project, run_id, row_id, stored_spec


def test_unrouted_dispatch_refuses_when_the_live_quote_cannot_be_projected(
    tmp_path, monkeypatch
) -> None:
    """An unprojectable live estimate is a REFUSAL, not an exception.

    Validation can refuse a malformed estimate outright — a 400 the user can
    act on. Dispatch has no such seat: its only modeled outcomes are admit and
    ``consent_missing``. Letting ``ConsentQuoteRefused`` escape here would be
    strictly worse than a mismatch, because the same estimate throws on every
    retry: the consented run would become permanently unrunnable rather than
    re-confirmable, and the receipt would carry an unhandled exception instead
    of a reason.

    A recipe growing an unclassified money key between confirm and dispatch is
    the realistic path: the quote hash was minted before the key existed.
    """
    from frisket.engine.runner import validation

    adapter, project, run_id, row_id, stored_spec = _consented_unrouted_run(
        tmp_path, monkeypatch, name="Unrouted unprojectable", key="unrouted-unproject"
    )

    real_estimate_run = validation.estimate_run

    def _estimate_with_an_unclassified_money_key(*args, **kwargs):
        return {**real_estimate_run(*args, **kwargs), "surcharge_usd": 4.0}

    monkeypatch.setattr(
        validation, "estimate_run", _estimate_with_an_unclassified_money_key
    )

    with pytest.raises(ExecutionRouteVerificationFailed) as refusal:
        open_attempt_authority(project).mint(
            recipe=_stored_program(project, stored_spec),
            spec=stored_spec,
            run_id=run_id,
            scope=(row_id,),
        )

    assert refusal.value.code == "consent_missing"
    # The offending key is named, so the receipt says what to fix.
    assert "surcharge_usd" in str(refusal.value)
    assert len(adapter.calls) == 1


def test_unrouted_dispatch_normalizes_a_non_finite_runner_spec_value(
    tmp_path, monkeypatch
) -> None:
    """The catch is the whole recompute, not just the projection.

    ``json.dumps`` defaults to ``allow_nan=True``, so a raw-runner spec can
    carry a NaN through the queue payload round-trip and only blow up inside
    ``confirmation_context_hash``'s ``allow_nan=False`` encode — in the
    ``runner_spec`` half, which the typed quote projection never sees. That
    bare ValueError used to escape unmodeled, one dict over from the failure
    the projection's refusal fixed.
    """
    adapter, project, run_id, row_id, stored_spec = _consented_unrouted_run(
        tmp_path, monkeypatch, name="Unrouted nan spec", key="unrouted-nan"
    )
    stored_spec["temperature"] = float("nan")

    with pytest.raises(ExecutionRouteVerificationFailed) as refusal:
        open_attempt_authority(project).mint(
            recipe=_stored_program(project, stored_spec),
            spec=stored_spec,
            run_id=run_id,
            scope=(row_id,),
        )

    assert refusal.value.code == "consent_missing"
    assert "ValueError" in str(refusal.value)
    assert len(adapter.calls) == 1


def test_unrouted_dispatch_lets_infrastructure_errors_stay_infrastructure(
    tmp_path, monkeypatch
) -> None:
    """The normalization is scoped, not a bare ``except Exception``.

    Reporting a disk or memory failure as "consent is missing" would send the
    operator to re-confirm a run whose consent is perfectly valid. Only the
    value/lookup family becomes ``consent_missing``; everything else keeps its
    own identity, exactly as the routed branch does.
    """
    from frisket.engine.runner import validation

    adapter, project, run_id, row_id, stored_spec = _consented_unrouted_run(
        tmp_path, monkeypatch, name="Unrouted infra", key="unrouted-infra"
    )

    def _disk_failure(*args, **kwargs):
        raise OSError("disk went away")

    monkeypatch.setattr(validation, "estimate_run", _disk_failure)

    with pytest.raises(OSError, match="disk went away"):
        open_attempt_authority(project).mint(
            recipe=_stored_program(project, stored_spec),
            spec=stored_spec,
            run_id=run_id,
            scope=(row_id,),
        )
    assert len(adapter.calls) == 1
