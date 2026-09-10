"""Red-first typed BYOK failures on real synchronous and queued producers."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.engine.jobs import Worker
from frisket.ai.llm import LLMError
from http_test_helpers import post_v1_action_with_exact_confirmation
from tests.ai.test_split_team_byok_runs import (
    _ProviderAdapter,
    _action,
    _app,
    _login,
    _project,
    _reduce_action,
)

pytestmark = pytest.mark.gap
SECRET = "sk-test-only-redaction-probe"


def _provider_failure(*, status: int, retryable: bool, provider_code: str) -> LLMError:
    failure = LLMError(
        f"provider rejected request carrying credential {SECRET}",
        status=status,
        retryable=retryable,
    )
    # Provider adapters must preserve the provider's machine-readable reason;
    # HTTP 429 alone cannot distinguish a short rate limit from exhausted BYOK.
    failure.provider_code = provider_code  # type: ignore[attr-defined]
    return failure


def _durable_error_codes(value: Any) -> set[str]:
    if isinstance(value, dict):
        found = {
            code
            for key in ("code", "error_code")
            if isinstance((code := value.get(key)), str) and code
        }
        return found | set().union(
            *(_durable_error_codes(child) for child in value.values())
        )
    if isinstance(value, list):
        return set().union(*(_durable_error_codes(child) for child in value))
    return set()


def _assert_no_key_detail(*payloads: Any) -> None:
    serialized = json.dumps(payloads, default=str).lower()
    assert SECRET.lower() not in serialized
    assert "key_hint" not in serialized


def _assert_canonical_errors(
    errors: Any, *, code: str, retryable: bool, resumable: bool = True
) -> None:
    assert isinstance(errors, list) and errors
    assert {error.get("code") for error in errors} == {code}, errors
    for error in errors:
        details = error.get("details") or {}
        assert details.get("retryable") is retryable, error
        assert details.get("resumable") is resumable, error


def _configured_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    failure: LLMError,
) -> tuple[Any, TestClient, str, int]:
    _ProviderAdapter.calls = []
    monkeypatch.setattr(_ProviderAdapter, "failure", failure)
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    _login(client, app)
    saved = client.post("/api/org/keys", json={"provider": "openai", "key": SECRET})
    assert saved.status_code == 200, saved.text
    project_id, sheet_id = _project(client)
    project = app.state.workspace.get(project_id)
    assert (
        app.state.workspace.router_for(project).credential_source_for("openai")
        == "org_byok"
    ), "the failure-fact seam must identify the saved organization BYOK credential"
    return app, client, project_id, sheet_id


def test_http_401_byok_is_typed_on_existing_direct_reduce_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client, project_id, sheet_id = _configured_client(
        tmp_path,
        monkeypatch,
        failure=_provider_failure(
            status=401, retryable=False, provider_code="invalid_api_key"
        ),
    )
    response = post_v1_action_with_exact_confirmation(
        client,
        project_id,
        _reduce_action(sheet_id, "typed-invalid-key-direct"),
    )
    assert response.status_code in {200, 400}, response.text
    body = response.json()
    assert body["job_id"] is None, "reduce.group_summary must use the direct seam"
    receipt = client.get(
        f"/api/projects/{project_id}/actions/v1/receipts/{body['receipt_id']}"
    )
    assert receipt.status_code == 200, receipt.text
    receipt_body = receipt.json()
    project = app.state.workspace.get(project_id)
    persisted = [
        dict(row)
        for row in project.db.execute(
            "SELECT error, error_code FROM results ORDER BY row_id, column_id"
        ).fetchall()
    ]
    raw_receipt = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (body["receipt_id"],)
    ).fetchone()
    assert raw_receipt is not None
    persisted_receipt = json.loads(raw_receipt["body"])
    _assert_no_key_detail(body, receipt_body, persisted, persisted_receipt)
    assert _ProviderAdapter.calls == [SECRET]
    assert persisted, "the direct reducer must persist its provider failure"
    assert {row["error_code"] for row in persisted} == {"invalid_provider_key"}, (
        "the durable result must carry the actionable credential code; an API-only "
        f"rewrite over MODEL_ERROR is not completion: {persisted}"
    )
    _assert_canonical_errors(
        body.get("errors"), code="invalid_provider_key", retryable=False
    )
    _assert_canonical_errors(
        receipt_body.get("errors"), code="invalid_provider_key", retryable=False
    )
    _assert_canonical_errors(
        persisted_receipt.get("errors"),
        code="invalid_provider_key",
        retryable=False,
    )


@pytest.mark.parametrize(
    ("provider_code", "expected_code", "retryable"),
    [
        ("rate_limit_exceeded", "provider_rate_limited", True),
        ("insufficient_quota", "provider_key_exhausted", False),
    ],
)
def test_real_queued_worker_produces_supported_provider_exhaustion_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_code: str,
    expected_code: str,
    retryable: bool,
) -> None:
    case_root = tmp_path / provider_code
    case_root.mkdir()
    app, client, project_id, sheet_id = _configured_client(
        case_root,
        monkeypatch,
        failure=_provider_failure(
            status=429, retryable=True, provider_code=provider_code
        ),
    )
    started = post_v1_action_with_exact_confirmation(
        client,
        project_id,
        _action(sheet_id, f"typed-{provider_code}-queued"),
    )
    assert started.status_code == 200, started.text
    run_id = int(started.json()["run_id"])
    job_id = int(started.json()["job_id"])
    assert Worker(app.state.workspace.queue, app.state.workspace.registry).run_once()
    status = client.get(f"/api/projects/{project_id}/actions/runs/{run_id}/status")
    assert status.status_code == 200, status.text
    status_body = status.json()
    project = app.state.workspace.get(project_id)
    persisted_results = [
        dict(row)
        for row in project.db.execute(
            "SELECT error, error_code FROM results WHERE run_id=? ORDER BY row_id, column_id",
            (run_id,),
        ).fetchall()
    ]
    assert persisted_results
    assert {row["error_code"] for row in persisted_results} == {expected_code}
    persisted_job = app.state.workspace.queue.get(job_id)
    assert persisted_job is not None
    persisted_job_payload = asdict(persisted_job)
    _assert_no_key_detail(
        started.json(), status_body, persisted_results, persisted_job_payload
    )
    assert _ProviderAdapter.calls and set(_ProviderAdapter.calls) == {SECRET}
    public_status = status_body["run"]["public_status"]
    _assert_canonical_errors(
        public_status["row_errors"]["groups"],
        code=expected_code,
        retryable=retryable,
    )
    durable_job_codes = _durable_error_codes(persisted_job_payload)
    assert durable_job_codes == {expected_code}, persisted_job_payload
