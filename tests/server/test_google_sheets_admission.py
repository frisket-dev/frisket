"""ACTION-06B: Google Sheets admission precedes every durable/effect write."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.actions.system import typed_action_for_request, validate_root_action
from frisket.actions.types import ActionRequest
from frisket.engine.executor import ExecutorDeps
from frisket.engine.executor.action_inventory import _TypedProjectEnvelope
from frisket.engine.executor.google_sheets_action import (
    prepare_google_sheets_action_job,
)
from frisket.engine.executor.map_rows_action import typed_request_hash
from frisket.engine.executor.action_jobs import (
    ActionJobEnvelope,
    launch_queued_action_job,
    reserve_queued_action_job_receipt,
)
from frisket.engine.executor.action_specs import execution_spec_for
from frisket.engine.jobs.queue import ACTION_RUN_KIND
from frisket.engine.jobs.worker import Worker
from frisket.engine.store.effect_checkpoints import EffectCheckpointStore
from frisket.server.app import create_app


class _GoogleClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def export_tabs(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "spreadsheet_id": "created-sheet",
            "spreadsheet_url": "https://docs.google.com/spreadsheets/d/created-sheet",
            "updated_tabs": [],
        }


def _account(provider: str, connection_id: str) -> dict[str, Any] | None:
    if (provider, connection_id) != ("google", "google-connection-1"):
        return None
    return {
        "id": connection_id,
        "provider": provider,
        "external_subject": "google-subject-1",
        "external_email": "reporter@example.test",
        "refresh_token": "not-durable",
    }


def _account_must_not_resolve(_provider: str, _connection_id: str) -> None:
    raise AssertionError("account lookup ran before the Google client check")


def _client(
    root: Path,
    *,
    google_client: Any | None,
    resolver: Any | None,
) -> TestClient:
    def deps_factory(_project_id: str, _request: Any) -> ExecutorDeps:
        return ExecutorDeps(
            google_sheets_client=google_client,
            connected_account_resolver=resolver,
        )

    return TestClient(create_app(root, executor_deps_factory=deps_factory))


def _seed(client: TestClient) -> tuple[str, int]:
    project_id = client.post("/api/projects", json={"name": "Admission"}).json()["id"]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("rows.csv", "name\nAda\n", "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    return project_id, int(imported.json()["sheet_id"])


def _action(sheet_id: int, *, key: str = "action-06b@sha256:stable") -> dict[str, Any]:
    return {
        "action_id": "export.google_sheets",
        "scope": {"kind": "project"},
        "output_names": {},
        "params": {
            "connection_id": "google-connection-1",
            "source": {"kind": "current_sheet", "sheet_id": sheet_id},
            "destination": {
                "kind": "google_sheets",
                "mode": "new_spreadsheet",
                "spreadsheet_title": "Admission proof",
            },
            "write_policy": "replace_managed_tabs",
        },
        "idempotency_key": key,
    }


def _artifact_counts(client: TestClient, project_id: str) -> dict[str, int]:
    project = client.app.state.workspace.get(project_id)
    return {
        "runs": int(project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]),
        "receipts": int(
            project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
        ),
        "jobs": sum(client.app.state.workspace.queue.counts().values()),
        "checkpoints": len(EffectCheckpointStore(project.db).list_all()),
    }


def _challenge(
    client: TestClient, project_id: str, action: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    response = client.post(f"/api/projects/{project_id}/actions/v1/run", json=action)
    assert response.status_code == 402, response.text
    body = response.json()
    assert body["status"] == "needs_confirmation"
    [error] = body["errors"]
    assert error["code"] == "irreversible_external_requires_confirmation"
    assert error["field"] == "confirmation"
    assert error["details"]["reason"] == "irreversible_external"
    assert "estimate" not in error["details"]
    assert error["details"]["claims"]
    promise_hash = error["details"]["promise_set_hash"]
    assert isinstance(promise_hash, str) and len(promise_hash) == 64
    return promise_hash, body


def _confirmed(action: dict[str, Any], promise_hash: str) -> dict[str, Any]:
    confirmed = deepcopy(action)
    confirmed["confirmation"] = promise_hash
    return confirmed


def _prepared_envelope(client, project_id, action, google_client) -> ActionJobEnvelope:
    promise_hash, _ = _challenge(client, project_id, action)
    bound = typed_action_for_request(_confirmed(action, promise_hash))
    envelope = prepare_google_sheets_action_job(
        client.app.state.workspace.get(project_id),
        project_id,
        bound,
        deps=ExecutorDeps(
            google_sheets_client=google_client, connected_account_resolver=_account
        ),
    )
    assert isinstance(envelope, ActionJobEnvelope)
    assert envelope.resolve_phase == "launch"
    assert envelope.resolved_snapshot["kind"] == "google_sheets_export_intent"
    assert envelope.resolved_snapshot["intent"] == bound.params.model_dump(mode="json")
    assert google_client.calls == []
    return envelope


def test_schema_catalog_and_lifecycle_declare_no_money_irreversible_gate(
    tmp_path: Path,
) -> None:
    client = _client(
        tmp_path / "workspace",
        google_client=_GoogleClient(),
        resolver=_account,
    )
    project_id, _sheet_id = _seed(client)
    catalog = client.get(f"/api/projects/{project_id}/actions/v1/catalog").json()
    entry = next(
        item for item in catalog["actions"] if item["kind"] == "export.google_sheets"
    )
    properties = entry["input_schema"]["properties"]
    assert set(properties) == {"connection_id", "source", "destination", "write_policy"}
    assert "confirmation" in ActionRequest.model_json_schema()["properties"]
    assert entry["cost_policy"]["kind"] == "none"
    assert "irreversible_external_requires_confirmation" in {
        error["code"] for error in entry["errors"]
    }

    spec = execution_spec_for("export.google_sheets")
    assert spec is not None
    assert spec.lifecycle.confirmation.required is True
    assert spec.lifecycle.confirmation.reason == "irreversible_external"
    assert spec.lifecycle.external_claim.claimed is True
    assert spec.lifecycle.reservation.reserves_receipt is True
    assert spec.lifecycle.failure_commit.persist_failure is True


@pytest.mark.parametrize(
    ("google_client", "resolver", "expected_code"),
    [
        (None, _account_must_not_resolve, "google_sheets_client_unavailable"),
        (_GoogleClient(), None, "connected_account_not_found"),
        (
            _GoogleClient(),
            lambda _provider, _connection: None,
            "connected_account_not_found",
        ),
        (
            _GoogleClient(),
            lambda provider, _connection: {"id": "different", "provider": provider},
            "connected_account_not_found",
        ),
        (
            _GoogleClient(),
            lambda _provider, connection: {"id": connection, "provider": "other"},
            "connected_account_not_found",
        ),
    ],
    ids=(
        "missing-client",
        "missing-resolver",
        "missing-exact-account",
        "wrong-account-id",
        "wrong-account-provider",
    ),
)
def test_fresh_dependency_refusal_precedes_confirmation_and_creates_nothing(
    tmp_path: Path,
    google_client: Any | None,
    resolver: Any | None,
    expected_code: str,
) -> None:
    client = _client(
        tmp_path / expected_code,
        google_client=google_client,
        resolver=resolver,
    )
    project_id, sheet_id = _seed(client)
    baseline = _artifact_counts(client, project_id)
    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run", json=_action(sheet_id)
    )
    assert response.status_code == 400, response.text
    assert response.json()["errors"][0]["code"] == expected_code
    assert _artifact_counts(client, project_id) == baseline
    assert not getattr(google_client, "calls", [])


def test_wrong_echo_rechallenges_and_bare_legacy_approval_refuses_without_writes(
    tmp_path: Path,
) -> None:
    google_client = _GoogleClient()
    client = _client(
        tmp_path / "workspace", google_client=google_client, resolver=_account
    )
    project_id, sheet_id = _seed(client)
    action = _action(sheet_id)
    baseline = _artifact_counts(client, project_id)

    offered_hash, first = _challenge(client, project_id, action)
    bare = deepcopy(action)
    bare["params"]["confirmed"] = True
    bare_response = client.post(f"/api/projects/{project_id}/actions/v1/run", json=bare)
    assert bare_response.status_code == 400, bare_response.text
    assert bare_response.json()["errors"][0]["code"] == "invalid_action_request"
    wrong = _confirmed(action, "f" * 64)
    wrong_hash, wrong_body = _challenge(client, project_id, wrong)

    assert wrong_hash == offered_hash
    assert wrong_body["errors"][0]["details"] == first["errors"][0]["details"]
    assert _artifact_counts(client, project_id) == baseline
    assert google_client.calls == []


def test_exact_echo_queues_then_completed_replay_precedes_removed_dependencies(
    tmp_path: Path,
) -> None:
    google_client = _GoogleClient()
    client = _client(
        tmp_path / "workspace", google_client=google_client, resolver=_account
    )
    project_id, sheet_id = _seed(client)
    action = _action(sheet_id)
    promise_hash, _ = _challenge(client, project_id, action)
    confirmed = _confirmed(action, promise_hash)

    accepted = client.post(f"/api/projects/{project_id}/actions/v1/run", json=confirmed)
    assert accepted.status_code == 200, accepted.text
    queued = accepted.json()
    assert queued["status"] == "queued"
    assert queued["run_id"] is None
    assert queued["receipt_id"] and queued["job_id"]
    assert google_client.calls == []
    assert Worker(
        client.app.state.workspace.queue, client.app.state.workspace.registry
    ).run_once()
    assert len(google_client.calls) == 1

    # Replay/conflict inspection is receipt-owned and must not depend on the
    # composition still having the client/account that admitted the first call.
    client.app.state.workspace.executor_deps_factory = None
    replay = client.post(f"/api/projects/{project_id}/actions/v1/run", json=confirmed)
    assert replay.status_code == 200, replay.text
    assert replay.json()["status"] == "completed"
    assert replay.json()["receipt_id"] == queued["receipt_id"]
    assert len(google_client.calls) == 1

    changed = deepcopy(confirmed)
    changed["params"]["destination"]["spreadsheet_title"] = "Different"
    conflict = client.post(f"/api/projects/{project_id}/actions/v1/run", json=changed)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["errors"][0]["code"] == "idempotency_conflict"
    assert len(google_client.calls) == 1


def test_unpublished_prepared_receipt_does_not_bypass_current_dependency_gate(
    tmp_path: Path,
) -> None:
    client = _client(
        tmp_path / "workspace",
        google_client=None,
        resolver=_account,
    )
    project_id, sheet_id = _seed(client)
    project = client.app.state.workspace.get(project_id)
    action_body = _action(sheet_id, key="action-06b@sha256:prepared")
    bound = typed_action_for_request(action_body)
    action = _TypedProjectEnvelope(
        kind=bound.action.action_id,
        idempotency_key=bound.request.idempotency_key,
        params=bound.params.model_dump(mode="json"),
    )
    reserved = reserve_queued_action_job_receipt(
        project,
        action,
        project_id=project_id,
        params_hash=typed_request_hash(bound),
    )
    assert isinstance(reserved, dict)
    baseline = _artifact_counts(client, project_id)

    refused = client.post(
        f"/api/projects/{project_id}/actions/v1/run", json=action_body
    )

    assert refused.status_code == 400, refused.text
    assert refused.json()["errors"][0]["code"] == "google_sheets_client_unavailable"
    assert _artifact_counts(client, project_id) == baseline
    assert baseline["jobs"] == 0


@pytest.mark.parametrize(
    ("removed_dependency", "expected_code"),
    [
        ("client", "google_sheets_client_unavailable"),
        ("account", "connected_account_not_found"),
    ],
)
def test_worker_rechecks_removed_dependency_before_render_checkpoint_or_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    removed_dependency: str,
    expected_code: str,
) -> None:
    google_client = _GoogleClient()
    deps_state: dict[str, Any] = {
        "client": google_client,
        "resolver": _account,
    }

    def deps_factory(_project_id: str, _request: Any) -> ExecutorDeps:
        return ExecutorDeps(
            google_sheets_client=deps_state["client"],
            connected_account_resolver=deps_state["resolver"],
        )

    client = TestClient(
        create_app(tmp_path / "workspace", executor_deps_factory=deps_factory)
    )
    project_id, sheet_id = _seed(client)
    action = _action(
        sheet_id, key=f"action-06b@sha256:removed-worker-{removed_dependency}"
    )
    promise_hash, _ = _challenge(client, project_id, action)
    launched = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_confirmed(action, promise_hash),
    ).json()
    assert launched["status"] == "queued"
    baseline_runs = _artifact_counts(client, project_id)["runs"]
    if removed_dependency == "client":
        deps_state["client"] = None
        deps_state["resolver"] = _account_must_not_resolve
    else:
        deps_state["resolver"] = lambda _provider, _connection: None

    from frisket.engine.executor.action_families import exports as export_family

    monkeypatch.setattr(
        export_family,
        "_google_sheets_tabs_for_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("worker rendered rows before account recheck")
        ),
    )
    workspace = client.app.state.workspace
    assert Worker(workspace.queue, workspace.registry).run_once()

    project = workspace.get(project_id)
    receipt = project.db.execute(
        "SELECT status, body FROM receipts WHERE id=?", (launched["receipt_id"],)
    ).fetchone()
    assert receipt is not None and receipt["status"] == "failed"
    assert json.loads(receipt["body"])["errors"][0]["code"] == expected_code
    assert _artifact_counts(client, project_id)["runs"] == baseline_runs
    assert EffectCheckpointStore(project.db).list_all() == []
    assert google_client.calls == []


@pytest.mark.parametrize(
    ("confirmation", "expected_code"),
    [
        (None, "irreversible_external_requires_confirmation"),
        (True, "invalid_action_request"),
        ("0" * 64, "irreversible_external_requires_confirmation"),
    ],
    ids=("unconfirmed", "boolean-approval", "wrong-echo"),
)
def test_worker_rechecks_forged_confirmation_before_render_checkpoint_or_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    confirmation: str | bool | None,
    expected_code: str,
) -> None:
    google_client = _GoogleClient()
    client = _client(
        tmp_path / "workspace", google_client=google_client, resolver=_account
    )
    project_id, sheet_id = _seed(client)
    workspace = client.app.state.workspace
    project = workspace.get(project_id)
    case = str(confirmation)
    body = _action(sheet_id, key=f"action-06b@sha256:forged-worker-{case}")
    prepared = _prepared_envelope(client, project_id, body, google_client)
    forged_action = deepcopy(prepared.action)
    if confirmation is None:
        forged_action.pop("confirmation")
    else:
        forged_action["confirmation"] = confirmation
    # Keep the genuinely admitted intent and reservation. Only consent is forged.
    launched = launch_queued_action_job(
        envelope=replace(prepared, action=forged_action),
        queue=workspace.queue,
        job_kind=ACTION_RUN_KIND,
        max_attempts=2,
        payload_extra={"workspace_root": str(workspace.root)},
        project=project,
    )

    from frisket.engine.executor.action_families import exports as export_family

    monkeypatch.setattr(
        export_family,
        "_google_sheets_tabs_for_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("worker rendered rows before confirmation recheck")
        ),
    )
    assert Worker(workspace.queue, workspace.registry).run_once()

    receipt = project.db.execute(
        "SELECT status, body FROM receipts WHERE id=?", (launched.receipt_id,)
    ).fetchone()
    assert receipt is not None and receipt["status"] == "failed"
    assert json.loads(receipt["body"])["errors"][0]["code"] == expected_code
    assert EffectCheckpointStore(project.db).list_all() == []
    assert google_client.calls == []


@pytest.mark.parametrize(
    "drop_idempotency_key",
    [True, False],
    ids=("missing-idempotency", "caller-capabilities"),
)
def test_worker_canonical_validation_refuses_forged_outer_valid_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drop_idempotency_key: bool,
) -> None:
    google_client = _GoogleClient()
    client = _client(
        tmp_path / "workspace", google_client=google_client, resolver=_account
    )
    project_id, sheet_id = _seed(client)
    workspace = client.app.state.workspace
    project = workspace.get(project_id)

    # Begin with a real launch-admitted reservation, then corrupt only its wire
    # action. Caller-declared capabilities are no longer a typed request field.
    prepared = _prepared_envelope(client, project_id, _action(sheet_id), google_client)
    body = deepcopy(prepared.action)
    if drop_idempotency_key:
        body.pop("idempotency_key")
    else:
        body["capabilities"] = []
    canonical = validate_root_action(body)
    assert canonical.ok is False
    assert canonical.error is not None
    assert canonical.error.code == "invalid_action_request"
    launched = launch_queued_action_job(
        envelope=replace(prepared, action=body),
        queue=workspace.queue,
        job_kind=ACTION_RUN_KIND,
        max_attempts=2,
        payload_extra={"workspace_root": str(workspace.root)},
        project=project,
    )

    render_calls: list[bool] = []

    def forbidden_render(*_args: Any, **_kwargs: Any) -> Any:
        render_calls.append(True)
        raise AssertionError("worker rendered rows before canonical validation")

    from frisket.engine.executor.action_families import exports as export_family

    monkeypatch.setattr(
        export_family,
        "_google_sheets_tabs_for_source",
        forbidden_render,
    )
    assert Worker(workspace.queue, workspace.registry).run_once()

    receipt = project.db.execute(
        "SELECT status, body FROM receipts WHERE id=?", (launched.receipt_id,)
    ).fetchone()
    assert receipt is not None and receipt["status"] == "failed"
    persisted_error = json.loads(receipt["body"])["errors"][0]
    assert persisted_error["code"] == canonical.error.code
    assert persisted_error["action_kind"] == "export.google_sheets"
    assert render_calls == []
    assert EffectCheckpointStore(project.db).list_all() == []
    assert google_client.calls == []
