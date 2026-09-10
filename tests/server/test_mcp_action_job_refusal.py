"""MCP cannot dispatch the runless action-job placement it cannot observe."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest

from frisket.engine.executor.action_specs import declared_queued_action_job_kinds
from frisket.server.mcp.backends import HostedBackend, LocalBackend
from frisket.server.services.action_runs import ActionRunService


def _action(kind: str) -> dict[str, Any]:
    from frisket.actions.registry import ACTION_REGISTRY, NEW_ACTION_IDS

    if kind in NEW_ACTION_IDS:
        # Reach placement refusal with valid canonical intent, not malformed
        # Params that the typed boundary correctly rejects before placement.
        request = deepcopy(ACTION_REGISTRY.get(kind).catalog_entry()["examples"][0])
        request["idempotency_key"] = "mcp-action-job"
        return request
    return {
        "schema_version": "frisket.action.v2",
        "kind": kind,
        "params": {},
    }


@pytest.mark.parametrize("kind", sorted(declared_queued_action_job_kinds()))
def test_local_mcp_refuses_every_action_job_before_service_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    calls: list[str] = []

    def forbidden_dispatch(*_args: Any, **_kwargs: Any) -> Any:
        calls.append(kind)
        raise AssertionError("MCP must refuse before ActionRunService")

    monkeypatch.setattr(ActionRunService, "run_action", forbidden_dispatch)
    backend = LocalBackend(tmp_path / "workspace")
    with pytest.raises(
        ValueError,
        match=rf"queued_action_job_not_supported:.*{kind}",
    ):
        asyncio.run(backend.run_action("demo", _action(kind)))
    assert calls == []


@pytest.mark.parametrize("kind", sorted(declared_queued_action_job_kinds()))
def test_hosted_mcp_refuses_every_action_job_before_http(kind: str) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500, json={"detail": "must not dispatch"})

    async def run() -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://frisket.test",
        )
        try:
            backend = HostedBackend(client=client)
            with pytest.raises(
                ValueError,
                match=rf"queued_action_job_not_supported:.*{kind}",
            ):
                await backend.run_action("demo", _action(kind))
        finally:
            await client.aclose()

    asyncio.run(run())
    assert requests == []
