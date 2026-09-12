from __future__ import annotations

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from frisket.engine.jobs.ports import JobHandlerContext, WorkerPorts
from frisket.engine.jobs.runs import resolve_run_models_gateway
from frisket.execution.definitions import StaticExecutionTargetProvider
from frisket.team.gateway_routes import (
    TeamModelsGatewayStore,
    TeamOrgModelsGatewayPort,
    register_team_models_gateway_routes,
    team_models_gateway_service,
)
from frisket.team.schema import metadata, org_env_vars


ORIGIN = "https://team-models.example.test"
TOKEN = "team-gateway-secret"


def _encrypt(value: str) -> str:
    return f"sealed:{value}"


def _decrypt(value: str) -> str:
    assert value.startswith("sealed:")
    return value.removeprefix("sealed:")


def _app(tmp_path, monkeypatch) -> tuple[FastAPI, sa.Engine]:
    monkeypatch.delenv("FRISKET_MODELS_URL", raising=False)
    monkeypatch.delenv("FRISKET_MODELS_TOKEN", raising=False)
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'control.db'}")
    metadata.create_all(engine)

    def capabilities(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"service": "frisket-models", "version": "1", "engines": []},
        )

    service = team_models_gateway_service(
        engine,
        org_id=9,
        encrypt=_encrypt,
        decrypt=_decrypt,
        transport=httpx.MockTransport(capabilities),
    )
    app = FastAPI()

    def require_member(request: Request) -> dict:
        role = request.headers.get("x-role")
        if role not in {"member", "owner"}:
            raise HTTPException(401, "sign in required")
        return {"id": 1, "role": role}

    def require_owner(request: Request) -> dict:
        actor = require_member(request)
        if actor["role"] != "owner":
            raise HTTPException(403, "owner required")
        return actor

    register_team_models_gateway_routes(
        app,
        service=service,
        require_member=require_member,
        require_owner=require_owner,
        member_can_mutate=lambda actor: actor["role"] == "owner",
    )
    return app, engine


def test_member_reads_status_but_only_owner_validates_and_saves(
    tmp_path, monkeypatch
) -> None:
    app, _engine = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    member_headers = {"x-role": "member"}
    owner_headers = {"x-role": "owner"}

    status = client.get("/api/org/models-gateway", headers=member_headers)
    assert status.status_code == 200
    assert status.json()["authority"] == "organization"
    assert status.json()["can_mutate"] is False
    assert (
        client.post(
            "/api/org/models-gateway/validate",
            headers=member_headers,
            json={"origin": ORIGIN, "token": TOKEN},
        ).status_code
        == 403
    )

    validated = client.post(
        "/api/org/models-gateway/validate",
        headers=owner_headers,
        json={"origin": ORIGIN, "token": TOKEN},
    )
    assert validated.status_code == 200
    receipt = validated.json()["validation_token"]
    saved = client.put(
        "/api/org/models-gateway",
        headers=owner_headers,
        json={"origin": ORIGIN, "token": TOKEN, "validation_token": receipt},
    )
    assert saved.status_code == 200
    assert saved.json()["configured"] is True
    assert TOKEN not in saved.text


def test_team_save_writes_pair_in_one_transaction_and_resolves_it(
    tmp_path, monkeypatch
) -> None:
    _app_instance, engine = _app(tmp_path, monkeypatch)
    store = TeamModelsGatewayStore(
        engine,
        org_id=9,
        encrypt=_encrypt,
        decrypt=_decrypt,
        env={},
    )
    store.save(ORIGIN, TOKEN)
    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(org_env_vars.c.name, org_env_vars.c.encrypted).where(
                org_env_vars.c.org_id == 9
            )
        ).all()
    assert {row.name for row in rows} == {
        "FRISKET_MODELS_URL",
        "FRISKET_MODELS_TOKEN",
    }
    resolved = store.resolve()
    assert resolved is not None
    assert resolved.origin == ORIGIN
    assert resolved.token == TOKEN


def test_failed_team_pair_save_rolls_back_both_rows(tmp_path, monkeypatch) -> None:
    _app_instance, engine = _app(tmp_path, monkeypatch)
    store = TeamModelsGatewayStore(
        engine, org_id=9, encrypt=_encrypt, decrypt=_decrypt, env={}
    )
    store.save(ORIGIN, TOKEN)

    def fail_token_encryption(value: str) -> str:
        if value == "replacement-token":
            raise ValueError("encryption failed")
        return _encrypt(value)

    failing_store = TeamModelsGatewayStore(
        engine, org_id=9, encrypt=fail_token_encryption, decrypt=_decrypt, env={}
    )
    with pytest.raises(ValueError, match="encryption failed"):
        failing_store.save("https://replacement.example.test", "replacement-token")
    saved = store.resolve()
    assert saved is not None
    assert saved.origin == ORIGIN
    assert saved.token == TOKEN


def test_team_environment_pair_is_read_only_and_partial_pair_refuses_fallback(
    tmp_path, monkeypatch
) -> None:
    _app_instance, engine = _app(tmp_path, monkeypatch)
    stored = TeamModelsGatewayStore(
        engine,
        org_id=9,
        encrypt=_encrypt,
        decrypt=_decrypt,
        env={},
    )
    stored.save(ORIGIN, TOKEN)
    env = {
        "FRISKET_MODELS_URL": "http://localhost:8400",
        "FRISKET_MODELS_TOKEN": "env-token",
    }
    service = team_models_gateway_service(
        engine,
        org_id=9,
        encrypt=_encrypt,
        decrypt=_decrypt,
        env=env,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"service": "frisket-models", "version": "1", "engines": []},
            )
        ),
    )
    status = service.status(can_mutate=True)
    assert status["source"] == "environment"
    assert status["can_mutate"] is False

    env.pop("FRISKET_MODELS_TOKEN")
    partial = service.status(can_mutate=True)
    assert partial["configured"] is False
    assert "FRISKET_MODELS_TOKEN is required" in partial["error"]


def test_saved_team_pair_reaches_queued_target_via_trusted_org_port(
    tmp_path, monkeypatch
) -> None:
    _app_instance, engine = _app(tmp_path, monkeypatch)
    TeamModelsGatewayStore(
        engine,
        org_id=9,
        encrypt=_encrypt,
        decrypt=_decrypt,
        env={},
    ).save(ORIGIN, TOKEN)
    ports = WorkerPorts(
        models_gateway_port=TeamOrgModelsGatewayPort(_decrypt),
    )
    resolved = resolve_run_models_gateway(
        tmp_path,
        handler_context=JobHandlerContext.from_claimed_job(trusted_org_id=9),
        ports=ports,
        control_database_url=str(engine.url),
    )
    assert resolved is not None
    target = StaticExecutionTargetProvider(
        env={},
        models_gateway_resolver=lambda: resolved,
    ).connection("models-gateway")
    assert target is not None
    assert target.base_url == ORIGIN
    assert target.token == TOKEN


@pytest.mark.parametrize("custom_factory", [False, True])
def test_actual_queued_handler_resolves_team_store_and_preserves_external_factory(
    tmp_path, monkeypatch, custom_factory
) -> None:
    from frisket.engine.jobs import Worker
    import frisket.engine.jobs.runs as runs
    from frisket.server.app import create_app

    _app_instance, engine = _app(tmp_path, monkeypatch)
    TeamModelsGatewayStore(
        engine, org_id=9, encrypt=_encrypt, decrypt=_decrypt, env={}
    ).save(ORIGIN, TOKEN)
    port = TeamOrgModelsGatewayPort(_decrypt)
    calls = []
    real_open = runs.open_execution_composition

    def inspect_composition(project, router, context, **kwargs):
        composition = real_open(project, router, context, **kwargs)
        connection = composition.provider.connection("models-gateway")
        calls.append((context.trusted_job_org_id, kwargs, connection))
        return composition

    options = {}
    if custom_factory:
        # The external factory keeps its existing three-argument contract.
        def external_factory(project, router, context):
            return inspect_composition(project, router, context)

        options["execution_composition_factory"] = external_factory
    else:
        monkeypatch.setattr(runs, "open_execution_composition", inspect_composition)

    client = TestClient(
        create_app(
            tmp_path / "workspace",
            queue_payload_extra={"org_id": 9},
            control_database_url=str(engine.url),
            worker_ports=WorkerPorts(models_gateway_port=port),
            run_status_grace_seconds=3600,
            **options,
        )
    )
    project_id = client.post("/api/projects", json={"name": "Queued gateway"}).json()[
        "id"
    ]
    workspace = client.app.state.workspace
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("Notes")
    column_id = project.add_column(sheet_id, "note", type="text")
    project.add_rows(sheet_id, [{"note": "Call 212-555-0123"}], {"note": column_id})
    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json={
            "action_id": "map.regex_extract",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {"input_columns": ["note"], "pattern": r"\d{3}-\d{3}-\d{4}"},
            "output_names": {"extracted": "phone"},
            "idempotency_key": "queued-gateway-port",
        },
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    job = workspace.queue.get(job_id)
    assert job is not None
    assert ORIGIN not in repr(job.payload)
    assert TOKEN not in repr(job.payload)
    calls.clear()
    worker = Worker(workspace.queue, workspace.registry, poll_interval=0.01)
    assert worker.run_once()
    assert len(calls) == 1
    trusted_org_id, factory_options, connection = calls[0]
    assert trusted_org_id == 9
    if custom_factory:
        assert factory_options == {}
        assert connection is None
    else:
        assert callable(factory_options["models_gateway_resolver"])
        assert connection is not None
        assert connection.base_url == ORIGIN
        assert connection.token == TOKEN
        assert TOKEN not in repr(connection)
    finished = workspace.queue.get(job_id)
    assert finished is not None
    assert finished.status == "done"
    assert TOKEN not in repr(finished.result)


@pytest.mark.parametrize("team_context", [False, True])
def test_standalone_worker_supplies_team_gateway_port_only_for_explicit_team_context(
    tmp_path, monkeypatch, team_context
) -> None:
    from frisket.cli import _run_worker
    import frisket.engine.jobs as jobs
    from frisket.team.config import team_config_from_env
    from frisket.team.secret_box import TeamSecretBox

    monkeypatch.delenv("FRISKET_TEAM_DATABASE_URL", raising=False)
    monkeypatch.delenv("FRISKET_MODELS_URL", raising=False)
    monkeypatch.delenv("FRISKET_MODELS_TOKEN", raising=False)
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'worker-control.db'}")
    metadata.create_all(engine)
    if team_context:
        monkeypatch.setenv("FRISKET_TEAM_DATABASE_URL", str(engine.url))
        monkeypatch.setenv("FRISKET_SECRETS_MASTER_KEY", "worker-test-master-key")
        box = TeamSecretBox(team_config_from_env())
        TeamModelsGatewayStore(
            engine, org_id=9, encrypt=box.encrypt, decrypt=box.decrypt, env={}
        ).save(ORIGIN, TOKEN)
    captured = []
    real_register = jobs.register_production_handlers

    def inspect_registration(registry, **kwargs):
        captured.append(kwargs)
        return real_register(registry, **kwargs)

    monkeypatch.setattr(jobs, "register_production_handlers", inspect_registration)
    result = _run_worker(
        [
            "--database-url",
            f"sqlite:///{tmp_path / 'queue.db'}",
            "--drain",
            "--schedule-sources-interval",
            "0",
            "--schedule-notification-digests-interval",
            "0",
        ],
        hosted=False,
    )
    assert result == 0
    assert len(captured) == 1
    if team_context:
        assert captured[0]["control_database_url"] == str(engine.url)
        resolved = (
            captured[0]["worker_ports"]
            .models_gateway()
            .models_gateway_connection(org_id=9, control_database_url=str(engine.url))
        )
        assert resolved is not None
        assert resolved.origin == ORIGIN
        assert resolved.token == TOKEN
        assert TOKEN not in repr(resolved)
    else:
        assert "worker_ports" not in captured[0]
