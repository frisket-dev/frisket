"""Team local-model endpoint catalog + provisioning routes:
``GET /api/org/local-endpoints``, ``POST .../pull``,
``GET .../pulls[/{id}]``, ``POST .../pulls/{id}/cancel``. Owner-gated
mutations, member-readable views, audit rows, DTO parity with the local
``/api/providers/models/pull*`` surface, and the scoped-cancel wrong-org
refusal end to end through the real team app.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from frisket.engine.jobs import model_pull_store as store
from frisket.engine.jobs.queue import QUEUE_DB_NAME, open_queue
from frisket.server import provider_config
from frisket.team.app import TeamConfig, create_team_app
from frisket.team.schema import audit_log, users
from tests.team_setup_helpers import (
    claim_server,
    seed_member_invite,
    sign_in_with_magic_link,
)

ORIGIN = "https://models.example.test"


def _config(tmp_path: Path) -> TeamConfig:
    return TeamConfig(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        # create_team_app fails fast at startup when
        # FRISKET_ENABLE_MODEL_PULL is truthy AND no run_queue_database_url
        # is configured (a bare `frisket worker` would fall back to a
        # DIFFERENT workspace-local queue file in that same unset-var case).
        # Almost every test below enables the flag, so this default fixture
        # must configure a real run-queue database -- the one test pinning
        # the "absent, flag disabled" fallback builds its own TeamConfig
        # directly instead of using this helper.
        run_queue_database_url=f"sqlite:///{tmp_path / 'run-queue.db'}",
        data_dir=tmp_path / "data",
        base_url="http://testserver",
        organization_name="Local Models Desk",
        admin_emails={"owner@example.com"},
    )


async def _mail(_email: str, _link: str) -> bool:
    return True


def _app(tmp_path: Path) -> Any:
    app = create_team_app(_config(tmp_path), send_magic_email=_mail)
    claim_server(app)
    seed_member_invite(app, "member@example.com")
    return app


def _sign_in(client: TestClient, app: Any, email: str) -> None:
    sign_in_with_magic_link(app, client, email)


def _owner(app: Any) -> TestClient:
    client = TestClient(app)
    _sign_in(client, app, "owner@example.com")
    return client


def _member(app: Any) -> TestClient:
    client = TestClient(app)
    _sign_in(client, app, "member@example.com")
    return client


def _native_reachable(monkeypatch, **overrides) -> dict:
    base = {
        "reachable": True,
        "status": 200,
        "models": [],
        "detail": None,
        "protocol": "ollama_native",
        "auth_status": "ok",
    }
    base.update(overrides)
    monkeypatch.setattr(provider_config, "ollama_reachable", lambda *a, **k: base)
    return base


def _local_ref(model: str) -> str:
    endpoint, notes = provider_config.resolve_env_local_endpoint()
    assert endpoint is not None, notes
    return f"ollama/@{endpoint.endpoint_id}/{model}"


def _audit_actions(app: Any) -> list[str]:
    with app.state.control_engine.connect() as cx:
        rows = cx.execute(sa.select(audit_log.c.action)).scalars().all()
    return list(rows)


# ---------------------------------------------------------------------------
# GET /api/org/local-endpoints
# ---------------------------------------------------------------------------


def test_get_local_endpoints_unconfigured_returns_empty_collection(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    monkeypatch.delenv("FRISKET_LLM_TOKEN", raising=False)
    monkeypatch.delenv("FRISKET_LLM_PROVISIONING_TOKEN", raising=False)
    monkeypatch.delenv("FRISKET_LLM_EDGE_AUTH", raising=False)
    app = _app(tmp_path)
    owner = _owner(app)

    resp = owner.get("/api/org/local-endpoints")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "schemaVersion": "frisket.local_endpoints.v1",
        "endpoints": [],
    }


def test_get_local_models_configured_reports_facts_and_pull_flag(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OLLAMA_URL", ORIGIN)
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")
    probe = _native_reachable(monkeypatch, models=["qwen3:8b"])
    app = _app(tmp_path)
    owner = _owner(app)

    resp = owner.get("/api/org/local-endpoints")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schemaVersion"] == "frisket.local_endpoints.v1"
    assert len(body["endpoints"]) == 1
    endpoint = body["endpoints"][0]
    assert endpoint["endpoint_id"]
    assert endpoint["origin"] == ORIGIN
    assert endpoint["authority"] == "organization"
    assert endpoint["source"] == "environment"
    assert endpoint["read_only"] is True
    assert endpoint["reachable"] == probe["reachable"]
    assert endpoint["protocol"] == probe["protocol"]
    assert endpoint["auth_status"] == probe["auth_status"]
    assert [model["id"] for model in endpoint["models"]] == [
        f"ollama/@{endpoint['endpoint_id']}/qwen3:8b"
    ]
    assert endpoint["installed_models"] == probe["models"]
    assert endpoint["pull_enabled"] is True


def test_get_local_models_member_can_read(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_URL", ORIGIN)
    _native_reachable(monkeypatch)
    app = _app(tmp_path)
    member = _member(app)
    resp = member.get("/api/org/local-endpoints")
    assert resp.status_code == 200, resp.text


def test_get_local_models_anonymous_is_refused(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_URL", ORIGIN)
    _native_reachable(monkeypatch)
    app = _app(tmp_path)
    resp = TestClient(app).get("/api/org/local-endpoints")
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# POST /api/org/models/pull
# ---------------------------------------------------------------------------


def test_pull_disabled_by_default_returns_409(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_URL", ORIGIN)
    monkeypatch.delenv("FRISKET_ENABLE_MODEL_PULL", raising=False)
    _native_reachable(monkeypatch)
    app = _app(tmp_path)
    owner = _owner(app)

    resp = owner.post("/api/org/models/pull", json={"ref": _local_ref("smollm:135m")})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "model_pull_disabled"


def test_pull_member_is_forbidden(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_URL", ORIGIN)
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")
    _native_reachable(monkeypatch)
    app = _app(tmp_path)
    member = _member(app)

    resp = member.post("/api/org/models/pull", json={"ref": _local_ref("smollm:135m")})
    assert resp.status_code == 403, resp.text


def test_pull_anonymous_is_unauthorized(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_URL", ORIGIN)
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")
    _native_reachable(monkeypatch)
    app = _app(tmp_path)

    resp = TestClient(app).post(
        "/api/org/models/pull", json={"ref": _local_ref("smollm:135m")}
    )
    assert resp.status_code == 401, resp.text


def test_pull_invalid_model_ref_returns_400(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_URL", ORIGIN)
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")
    _native_reachable(monkeypatch)
    app = _app(tmp_path)
    owner = _owner(app)

    resp = owner.post("/api/org/models/pull", json={"ref": "ollama:evil.example/x/y"})
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "invalid_model_ref"


def test_pull_unreachable_server_returns_503(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_URL", ORIGIN)
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")
    monkeypatch.setattr(
        provider_config,
        "ollama_reachable",
        lambda *a, **k: {
            "reachable": False,
            "status": None,
            "models": [],
            "detail": None,
            "protocol": "unknown",
            "auth_status": "unknown",
        },
    )
    app = _app(tmp_path)
    owner = _owner(app)

    resp = owner.post("/api/org/models/pull", json={"ref": _local_ref("smollm:135m")})
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["code"] == "local_server_unreachable"


def test_pull_unauthorized_server_returns_409(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_URL", ORIGIN)
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")
    monkeypatch.setenv("FRISKET_LLM_TOKEN", "shh-token-value")
    _native_reachable(monkeypatch, auth_status="unauthorized")
    app = _app(tmp_path)
    owner = _owner(app)

    resp = owner.post("/api/org/models/pull", json={"ref": _local_ref("smollm:135m")})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "local_server_unauthorized"
    assert "shh-token-value" not in resp.text


def test_pull_non_native_protocol_returns_409_pull_unsupported(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OLLAMA_URL", ORIGIN)
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")
    _native_reachable(monkeypatch, protocol="openai_compatible")
    app = _app(tmp_path)
    owner = _owner(app)

    resp = owner.post("/api/org/models/pull", json={"ref": _local_ref("smollm:135m")})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "pull_unsupported"


def test_pull_happy_path_enqueues_and_audits(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_URL", ORIGIN)
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")
    _native_reachable(monkeypatch)
    app = _app(tmp_path)
    owner = _owner(app)

    resp = owner.post("/api/org/models/pull", json={"ref": _local_ref("smollm:135m")})
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["deduplicated"] is False
    pull = body["pull"]
    assert pull["schemaVersion"] == "frisket.model_pull.v3"
    assert pull["model"] == _local_ref("smollm:135m")
    assert (
        pull["endpoint_id"]
        == provider_config.resolve_env_local_endpoint()[0].endpoint_id
    )
    assert pull["status"] in ("pending", "running")

    workspace = app.state.workspace
    row = store.get(workspace.queue.engine, pull["id"])
    assert row is not None
    assert row.job_id is not None
    job = workspace.queue.get(row.job_id)
    assert job is not None
    assert job.kind == "model.pull"
    assert job.org_id == str(app.state.team_org_id)

    assert "model_pull_requested" in _audit_actions(app)
    with app.state.control_engine.connect() as cx:
        audit_detail = cx.execute(
            sa.select(audit_log.c.detail).where(
                audit_log.c.action == "model_pull_requested"
            )
        ).scalar_one()
    assert audit_detail == f"{_local_ref('smollm:135m')}@{ORIGIN}"


def test_explicit_ollama_ref_forces_artifact_like_name_through_ollama(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OLLAMA_URL", ORIGIN)
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")
    probes = []

    def reachable(*args, **kwargs):
        probes.append((args, kwargs))
        return {
            "reachable": True,
            "status": 200,
            "models": [],
            "detail": None,
            "protocol": "ollama_native",
            "auth_status": "ok",
        }

    monkeypatch.setattr(provider_config, "ollama_reachable", reachable)
    owner = _owner(_app(tmp_path))

    response = owner.post(
        "/api/org/models/pull", json={"ref": _local_ref("opus-mt:en-es")}
    )

    assert response.status_code == 202, response.text
    assert probes
    assert response.json()["pull"]["model"] == _local_ref("opus-mt:en-es")
    assert response.json()["pull"]["artifact"] is None


def test_explicit_ollama_ref_uses_generic_artifact_length_guard(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OLLAMA_URL", ORIGIN)
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")
    owner = _owner(_app(tmp_path))
    model = "a" * 314

    response = owner.post("/api/org/models/pull", json={"ref": _local_ref(model)})

    assert response.status_code == 400
    assert response.json()["detail"] == {
        "code": "invalid_model_ref",
        "message": "artifact reference is too long (max 320 characters)",
    }


def test_pull_records_the_resolved_endpoint_origin(tmp_path, monkeypatch) -> None:
    """The team route resolves the local endpoint's URL for
    its own preflight probe but used to never record it on the pull row --
    unlike the local tier's ``/api/providers/models/pull`` route (which
    passes ``endpoint_origin=url`` to the SAME store function). Without it,
    the worker's endpoint-fingerprint check (a later URL change must not
    silently retarget an already-queued pull) has nothing to compare
    against for team-created pulls; the row's ``endpoint_origin`` stayed
    ``NULL`` forever, and the OWNER never got to see which endpoint their
    approved pull is actually bound to."""
    monkeypatch.setenv("OLLAMA_URL", ORIGIN)
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")
    _native_reachable(monkeypatch)
    app = _app(tmp_path)
    owner = _owner(app)

    resp = owner.post("/api/org/models/pull", json={"ref": _local_ref("smollm:135m")})
    assert resp.status_code == 202, resp.text
    body = resp.json()["pull"]
    assert body["endpoint_origin"] == ORIGIN

    workspace = app.state.workspace
    row = store.get(workspace.queue.engine, body["id"])
    assert row.endpoint_origin == ORIGIN


def test_pull_does_not_retry_an_internal_store_type_error(
    tmp_path, monkeypatch
) -> None:
    """The same-checkout store contract is called exactly once with provenance."""
    monkeypatch.setenv("OLLAMA_URL", ORIGIN)
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")
    _native_reachable(monkeypatch)
    app = _app(tmp_path)
    owner = _owner(app)
    calls: list[dict[str, Any]] = []

    def _raise_internal_type_error(*_args, **kwargs):
        calls.append(kwargs)
        raise TypeError("internal store failure")

    monkeypatch.setattr(store, "create_or_get_active", _raise_internal_type_error)

    with app.state.control_engine.connect() as connection:
        owner_user_id = connection.execute(
            sa.select(users.c.id).where(users.c.email == "owner@example.com")
        ).scalar_one()

    with pytest.raises(TypeError, match="internal store failure"):
        owner.post("/api/org/models/pull", json={"ref": _local_ref("smollm:135m")})

    assert calls == [
        {
            "workspace_root": str(app.state.workspace.root),
            "model_ref": _local_ref("smollm:135m"),
            "endpoint_id": provider_config.resolve_env_local_endpoint()[0].endpoint_id,
            "endpoint_origin": ORIGIN,
            "initiated_by": str(owner_user_id),
        }
    ]


def test_team_app_requires_run_queue_database_even_when_pull_is_disabled(
    tmp_path, monkeypatch
) -> None:
    """``create_team_app`` used to construct its core
    ``Workspace`` with NO run-queue argument at all, so ``Workspace.__init__``
    fell back to its own workspace-local ``.queue.db`` sqlite file -- a
    separately-run compose worker process (connected to the shared database)
    could never see a job enqueued there. That was later patched to a
    control-database fallback for the single-database topology, legal only
    while ``FRISKET_ENABLE_MODEL_PULL`` stayed off.

    Deferred follow-up from the queue-composition audit ("general run-queue mismatch"),
    now completed: ordinary project/action runs also enqueue through this
    queue regardless of the pull flag, so an absent run-queue locator is a
    broken topology either way -- the control-database fallback is gone, and
    ``create_team_app`` now requires ``run_queue_database_url``
    unconditionally (see also
    tests/test_team_entrypoint_hardening.py::test_create_team_app_requires_run_queue_database_unconditionally)."""
    monkeypatch.delenv("FRISKET_ENABLE_MODEL_PULL", raising=False)
    config = TeamConfig(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        data_dir=tmp_path / "data",
        base_url="http://testserver",
        organization_name="Local Models Desk",
        admin_emails={"owner@example.com"},
    )
    assert config.run_queue_database_url is None
    with pytest.raises(ValueError, match="FRISKET_RUN_QUEUE_DATABASE_URL"):
        create_team_app(config, send_magic_email=_mail)


def test_team_app_pull_enqueues_into_the_configured_run_queue_database_not_the_control_database(
    tmp_path, monkeypatch
) -> None:
    """The team app and worker must share a queue. ``create_team_app`` used to
    open its queue on
    ``config.database_url`` (the control-plane locator,
    ``FRISKET_TEAM_DATABASE_URL``) UNCONDITIONALLY -- but the compose worker
    (docker-compose.yml's ``worker`` service) and ``frisket worker``/
    ``frisket hosted-worker`` (cli.py's ``_run_worker``) both resolve their
    run-queue Postgres locator from a SEPARATE
    ``FRISKET_RUN_QUEUE_DATABASE_URL``. Whenever a real deployment configures
    distinct control/queue databases (the compose topology every wired-up
    worker actually uses), the old code enqueued into a database no worker
    was ever connected to. This test proves the app enqueues into the
    CONFIGURED run-queue database, genuinely distinct from the control
    database -- not merely a coincidentally-matching one."""
    monkeypatch.setenv("OLLAMA_URL", ORIGIN)
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")
    _native_reachable(monkeypatch)
    config = TeamConfig(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        run_queue_database_url=f"sqlite:///{tmp_path / 'run-queue.db'}",
        data_dir=tmp_path / "data",
        base_url="http://testserver",
        organization_name="Local Models Desk",
        admin_emails={"owner@example.com"},
    )
    app = create_team_app(config, send_magic_email=_mail)
    claim_server(app)
    owner = _owner(app)

    resp = owner.post("/api/org/models/pull", json={"ref": _local_ref("smollm:135m")})
    assert resp.status_code == 202, resp.text
    pull_id = resp.json()["pull"]["id"]

    assert not (config.data_dir / QUEUE_DB_NAME).exists()

    # The job lands in the CONFIGURED run-queue database...
    run_queue = open_queue(database_url=config.run_queue_database_url)
    row = store.get(run_queue.engine, pull_id)
    assert row is not None
    assert row.job_id is not None
    job = run_queue.get(row.job_id)
    assert job is not None
    assert job.kind == "model.pull"

    # ...and genuinely NOT in the control-plane database -- proving real
    # topology separation, not a coincidentally-shared single database.
    control_queue = open_queue(database_url=config.database_url)
    assert store.get(control_queue.engine, pull_id) is None


def test_pull_enqueue_failure_marks_the_row_failed_instead_of_wedging_the_workspace(
    tmp_path, monkeypatch
) -> None:
    """``operational_routes.py``'s pull route used to call
    ``workspace.queue.enqueue`` with no error boundary at all -- an
    exception there (already past ``create_or_get_active``, so the row
    exists and holds the one-active-pull-per-workspace slot) left an active,
    job-less row that nothing would ever terminalize, permanently blocking
    every future pull for this workspace. Mirrors the local tier's own
    ``/api/providers/models/pull`` boundary: mark the row failed
    (``enqueue_failed``) and return a 5xx BEFORE the slot is lost forever."""
    monkeypatch.setenv("OLLAMA_URL", ORIGIN)
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")
    _native_reachable(monkeypatch)
    app = _app(tmp_path)
    owner = _owner(app)

    workspace = app.state.workspace
    real_enqueue = workspace.queue.enqueue

    def _boom(*_a, **_k):
        raise RuntimeError("synthetic enqueue failure")

    workspace.queue.enqueue = _boom
    try:
        resp = owner.post(
            "/api/org/models/pull", json={"ref": _local_ref("smollm:135m")}
        )
    finally:
        workspace.queue.enqueue = real_enqueue
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["code"] == "enqueue_failed"

    rows = store.list_recent(workspace.queue.engine, str(workspace.root), limit=20)
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert rows[0].error_code == "enqueue_failed"

    # The workspace slot is free again -- a follow-up pull is NOT blocked by
    # the row from the failed attempt.
    retry = owner.post("/api/org/models/pull", json={"ref": _local_ref("smollm:135m")})
    assert retry.status_code == 202, retry.text


def test_pull_same_ref_dedupes(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_URL", ORIGIN)
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")
    _native_reachable(monkeypatch)
    app = _app(tmp_path)
    owner = _owner(app)

    first = owner.post("/api/org/models/pull", json={"ref": _local_ref("smollm:135m")})
    assert first.status_code == 202
    second = owner.post("/api/org/models/pull", json={"ref": _local_ref("smollm:135m")})
    assert second.status_code == 202
    assert second.json()["deduplicated"] is True
    assert second.json()["pull"]["id"] == first.json()["pull"]["id"]


def test_pull_different_ref_while_active_returns_409_busy(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OLLAMA_URL", ORIGIN)
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")
    _native_reachable(monkeypatch)
    app = _app(tmp_path)
    owner = _owner(app)

    first = owner.post("/api/org/models/pull", json={"ref": _local_ref("smollm:135m")})
    assert first.status_code == 202
    second = owner.post("/api/org/models/pull", json={"ref": _local_ref("qwen3:8b")})
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["code"] == "pull_busy"


# ---------------------------------------------------------------------------
# GET /api/org/models/pulls[/{id}]
# ---------------------------------------------------------------------------


def test_pulls_list_and_get_by_id(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_URL", ORIGIN)
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")
    _native_reachable(monkeypatch)
    app = _app(tmp_path)
    owner = _owner(app)
    created = owner.post(
        "/api/org/models/pull", json={"ref": _local_ref("smollm:135m")}
    ).json()["pull"]

    member = _member(app)
    listing = member.get("/api/org/models/pulls")
    assert listing.status_code == 200, listing.text
    assert any(p["id"] == created["id"] for p in listing.json()["pulls"])

    single = member.get(f"/api/org/models/pulls/{created['id']}")
    assert single.status_code == 200, single.text
    assert single.json()["id"] == created["id"]
    assert single.json()["schemaVersion"] == "frisket.model_pull.v3"

    missing = member.get("/api/org/models/pulls/999999")
    assert missing.status_code == 404, missing.text


def test_pulls_list_anonymous_is_unauthorized(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_URL", ORIGIN)
    app = _app(tmp_path)
    resp = TestClient(app).get("/api/org/models/pulls")
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# POST /api/org/models/pulls/{id}/cancel
# ---------------------------------------------------------------------------


def test_cancel_flips_state_and_audits(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_URL", ORIGIN)
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")
    _native_reachable(monkeypatch)
    app = _app(tmp_path)
    owner = _owner(app)
    created = owner.post(
        "/api/org/models/pull", json={"ref": _local_ref("smollm:135m")}
    ).json()["pull"]

    cancel_resp = owner.post(f"/api/org/models/pulls/{created['id']}/cancel")
    assert cancel_resp.status_code == 202, cancel_resp.text
    assert cancel_resp.json()["cancel_requested"] is True

    workspace = app.state.workspace
    row = store.get(workspace.queue.engine, created["id"])
    job = workspace.queue.get(row.job_id)
    assert job.status == "cancelled"
    assert "model_pull_cancelled" in _audit_actions(app)


def test_cancel_member_is_forbidden(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_URL", ORIGIN)
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")
    _native_reachable(monkeypatch)
    app = _app(tmp_path)
    owner = _owner(app)
    created = owner.post(
        "/api/org/models/pull", json={"ref": _local_ref("smollm:135m")}
    ).json()["pull"]

    member = _member(app)
    resp = member.post(f"/api/org/models/pulls/{created['id']}/cancel")
    assert resp.status_code == 403, resp.text


def test_cancel_scoped_wrong_org_job_is_not_cancelled(tmp_path, monkeypatch) -> None:
    """The cancel route resolves the queue job through ``cancel_scoped`` --
    a job that exists but belongs to a DIFFERENT org must not be flipped even
    if a pull row somehow pointed at it (defense in depth over the queue
    primitive itself, mirrored from tests/test_queue_scoped_access.py)."""
    monkeypatch.setenv("OLLAMA_URL", ORIGIN)
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")
    _native_reachable(monkeypatch)
    app = _app(tmp_path)
    owner = _owner(app)
    created = owner.post(
        "/api/org/models/pull", json={"ref": _local_ref("smollm:135m")}
    ).json()["pull"]

    workspace = app.state.workspace
    row = store.get(workspace.queue.engine, created["id"])
    other_org_job_id = row.job_id

    assert (
        workspace.queue.cancel_scoped(
            other_org_job_id, org_id="not-this-org", kind="model.pull"
        )
        is False
    )
    assert workspace.queue.get(other_org_job_id).status != "cancelled"


def test_cancel_refuses_without_audit_or_stamp_when_queue_cancel_does_not_succeed(
    tmp_path, monkeypatch
) -> None:
    """The cancel route used to stamp the cooperative ``cancel_requested_at``
    flag UNCONDITIONALLY, before ever calling ``cancel_scoped`` -- so even
    when ``cancel_scoped`` went on to refuse (wrong org/kind, already
    terminal), the row already carried the stamp. A running ``model.pull``
    handler polls that column with no org/kind scoping of its own, so the
    stamp alone could cooperatively stop a running pull despite the
    authorization check refusing the cancel -- silently, with no audit row
    (the audit was at least gated on success). Now a refusal must return 409
    and must not touch the row (no stamp) or the audit log at all."""
    monkeypatch.setenv("OLLAMA_URL", ORIGIN)
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")
    _native_reachable(monkeypatch)
    app = _app(tmp_path)
    owner = _owner(app)
    created = owner.post(
        "/api/org/models/pull", json={"ref": _local_ref("smollm:135m")}
    ).json()["pull"]

    workspace = app.state.workspace
    monkeypatch.setattr(workspace.queue, "cancel_scoped", lambda *a, **k: False)

    cancel_resp = owner.post(f"/api/org/models/pulls/{created['id']}/cancel")
    assert cancel_resp.status_code == 409, cancel_resp.text
    assert cancel_resp.json()["detail"]["code"] == "pull_not_cancellable"
    assert "model_pull_cancelled" not in _audit_actions(app)

    row = store.get(workspace.queue.engine, created["id"])
    assert row.cancel_requested_at is None, (
        "a refused cancel must not stamp cancel_requested_at -- a running "
        "handler cooperatively polls that column with no org/kind scoping "
        "of its own, so an unauthorized/refused cancel must never be able "
        "to stop a running pull through that side channel"
    )
    assert row.status in ("pending", "running")


def test_cancel_job_less_row_is_terminalized_and_audited(tmp_path, monkeypatch) -> None:
    """A job-less active row (an enqueue-wedge remnant, or any
    other row that never got as far as ``set_job_id``) has no queue job to
    scope a cancel through -- the route must terminalize the row directly
    (``model_pull_store.mark_cancelled``) and still audit the outcome,
    rather than only stamping the cooperative flag and leaving the row
    active forever."""
    monkeypatch.setenv("OLLAMA_URL", ORIGIN)
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")
    _native_reachable(monkeypatch)
    app = _app(tmp_path)
    owner = _owner(app)
    created = owner.post(
        "/api/org/models/pull", json={"ref": _local_ref("smollm:135m")}
    ).json()["pull"]

    workspace = app.state.workspace
    queue_engine = workspace.queue.engine
    # Simulate the enqueue-wedge remnant: an active row with no linked job.
    with queue_engine.begin() as cx:
        cx.execute(
            sa.text("UPDATE model_pulls SET job_id = NULL WHERE id = :id"),
            {"id": created["id"]},
        )

    cancel_resp = owner.post(f"/api/org/models/pulls/{created['id']}/cancel")
    assert cancel_resp.status_code == 202, cancel_resp.text
    assert cancel_resp.json()["status"] == "cancelled"
    assert "model_pull_cancelled" in _audit_actions(app)

    row = store.get(queue_engine, created["id"])
    assert row.status == "cancelled"


def test_cancel_not_found_returns_404(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_URL", ORIGIN)
    app = _app(tmp_path)
    owner = _owner(app)
    resp = owner.post("/api/org/models/pulls/999999/cancel")
    assert resp.status_code == 404, resp.text


# --- artifact-generic team pull (/api/org/models/pull) ---------------------


def test_org_artifact_pull_opus_mt_skips_daemon_and_audits(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")

    # An artifact ref must NOT probe the ollama daemon.
    def _boom(*a, **k):
        raise AssertionError("artifact refs must not probe the ollama daemon")

    monkeypatch.setattr(provider_config, "ollama_reachable", _boom)
    app = _app(tmp_path)
    owner = _owner(app)

    resp = owner.post("/api/org/models/pull", json={"ref": "opus-mt:en-es"})
    assert resp.status_code == 202, resp.text
    pull = resp.json()["pull"]
    assert pull["model"] == "opus-mt:en-es"
    assert pull["endpoint_origin"] is None  # no daemon for an artifact pull

    workspace = app.state.workspace
    row = store.get(workspace.queue.engine, pull["id"])
    job = workspace.queue.get(row.job_id)
    assert job.kind == "model.pull"
    assert job.org_id == str(app.state.team_org_id)
    assert "model_pull_requested" in _audit_actions(app)


def test_org_artifact_pull_member_forbidden(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")
    app = _app(tmp_path)
    member = _member(app)
    resp = member.post("/api/org/models/pull", json={"ref": "opus-mt:en-es"})
    assert resp.status_code == 403


def test_org_artifact_pull_unpinned_hf_requires_ack(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FRISKET_ENABLE_MODEL_PULL", "1")
    app = _app(tmp_path)
    owner = _owner(app)
    ref = "hf:owner/repo@abc123/model.gguf"
    resp = owner.post("/api/org/models/pull", json={"ref": ref})
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "unpinned_unacknowledged"
    resp2 = owner.post(
        "/api/org/models/pull", json={"ref": ref, "unpinned_acknowledged": True}
    )
    assert resp2.status_code == 202, resp2.text
