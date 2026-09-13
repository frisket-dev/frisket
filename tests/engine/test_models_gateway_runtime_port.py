from __future__ import annotations

from frisket.ai.models.gateway_config import ModelsGatewayConnection
from frisket.engine.jobs.ports import (
    JobHandlerContext,
    ModelsGatewayPort,
    WorkerPorts,
)
from frisket.engine.jobs.runs import resolve_run_models_gateway
from frisket.execution.definitions import StaticExecutionTargetProvider


class _GatewayPort:
    calls: list[tuple[int, str | None]]

    def __init__(self) -> None:
        self.calls = []

    def models_gateway_connection(
        self, *, org_id: int, control_database_url: str | None
    ) -> ModelsGatewayConnection | None:
        self.calls.append((org_id, control_database_url))
        return ModelsGatewayConnection(
            origin="https://org-models.example.test",
            token="org-gateway-token",
            source="stored",
        )


def test_queued_gateway_uses_trusted_org_port_with_env_unset(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("FRISKET_MODELS_URL", raising=False)
    monkeypatch.delenv("FRISKET_MODELS_TOKEN", raising=False)
    port = _GatewayPort()
    assert isinstance(port, ModelsGatewayPort)
    ports = WorkerPorts(models_gateway_port=port)

    connection = resolve_run_models_gateway(
        tmp_path,
        handler_context=JobHandlerContext.from_claimed_job(trusted_org_id=47),
        ports=ports,
        control_database_url="postgresql://control.invalid/db",
    )
    assert connection is not None
    assert connection.origin == "https://org-models.example.test"
    assert port.calls == [(47, "postgresql://control.invalid/db")]

    target = StaticExecutionTargetProvider(
        env={}, models_gateway_resolver=lambda: connection
    ).connection("models-gateway")
    assert target is not None
    assert target.base_url == connection.origin
    assert target.token == connection.token

    payload = {"org_id": 47, "action_kind": "transcribe"}
    assert "token" not in repr(payload)
    assert connection.token not in repr(payload)


def test_workspace_gateway_resolves_stored_pair_and_environment_wins(
    tmp_path,
    monkeypatch,
) -> None:
    from frisket.server import provider_config

    provider_config.save_local_models_gateway(
        tmp_path,
        origin="https://stored.example.test",
        token="stored-token",
    )
    direct = resolve_run_models_gateway(
        tmp_path,
        handler_context=JobHandlerContext.without_job_row(),
        ports=WorkerPorts(),
        control_database_url=None,
        env={},
    )
    assert direct is not None
    assert direct.origin == "https://stored.example.test"

    overridden = resolve_run_models_gateway(
        tmp_path,
        handler_context=JobHandlerContext.without_job_row(),
        ports=WorkerPorts(),
        control_database_url=None,
        env={
            "FRISKET_MODELS_URL": "http://127.0.0.1:8400",
            "FRISKET_MODELS_TOKEN": "environment-token",
        },
    )
    assert overridden is not None
    assert overridden.source == "environment"
    assert overridden.origin == "http://127.0.0.1:8400"
