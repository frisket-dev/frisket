from __future__ import annotations

import asyncio

import httpx
import pytest

from frisket.actions.api_call import ApiCallOutput, ApiCallParams
from frisket.actions.http_types import HttpRequest, HttpRequester
from frisket.actions.types import Row, RowError, RowResult
from frisket.ai.llm import ModelRouter
from frisket.engine.executor import http_request
from frisket.engine.executor.http_request import (
    AdmittedHttpRequester,
    HttpRequestCancelled,
)
from frisket.engine.store import Project
from frisket.ops.base import OpContext
from tests.engine.test_typed_api_call import _confirmed, _install_handler
from tests.ops.test_api_call import _allow_egress as _allow_egress


@pytest.mark.parametrize("reason", ["cancelled", "closed"])
@pytest.mark.parametrize("when", ["before_call", "after_pacing"])
def test_requester_refuses_revoked_lifetime_before_egress(
    monkeypatch, _allow_egress, reason, when
):
    calls = []

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: (
                    calls.append(request),
                    httpx.Response(200, json={"ok": True}),
                )[1]
            )
        ) as client:
            cancelled = False
            ctx = OpContext(http=client, extras={"cancelled": lambda: cancelled})
            sender = AdmittedHttpRequester(ctx)

            async def revoke():
                nonlocal cancelled
                if reason == "closed":
                    await sender.aclose()
                else:
                    cancelled = True

            if when == "before_call":
                await revoke()
            else:

                async def pace(ctx, rate):
                    await revoke()
                    return True

                monkeypatch.setattr(http_request, "_pace_request", pace)

            error_type = HttpRequestCancelled if reason == "cancelled" else RowError
            with pytest.raises(error_type) as caught:
                await sender.request_json(
                    HttpRequest(url="https://api.test.example/items"), Row({})
                )
            assert caught.value.code == (
                "cancelled" if reason == "cancelled" else "request_failed"
            )
            assert not calls
            assert "api_call_rate_limiter" not in ctx.extras.get("run_state", {})
            assert not client.is_closed  # Shared transport remains its owner's.

    asyncio.run(run())


def test_requester_cannot_escape_completed_typed_invocation(
    tmp_path, monkeypatch, _allow_egress
):
    captured = []

    async def escape(
        params: ApiCallParams, row: Row, sender: HttpRequester
    ) -> RowResult[ApiCallOutput]:
        captured.append((sender, row, params.request))
        return RowResult(output=ApiCallOutput(api_result={"ok": True}))

    _install_handler(monkeypatch, escape)
    project = Project.create(tmp_path / "escape.frisket")
    sheet = project.add_sheet("Rows")
    project.add_rows(sheet, [{}], {})
    calls = []
    router = ModelRouter(cache=None, cache_mode="off")
    router._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: (
                calls.append(request),
                httpx.Response(200, json={"ok": True}),
            )[1]
        )
    )
    try:
        result, _, _ = _confirmed(
            project,
            {
                "action_id": "map.api_call",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet},
                "params": {"request": {"url": "https://api.test.example/items"}},
                "idempotency_key": "escape",
            },
            router,
        )
        assert result.status == "completed", result.errors
        assert len(captured) == 1 and not calls
        sender, row, request = captured[0]
        with pytest.raises(RowError, match="closed"):
            asyncio.run(sender.request_json(request, row))
        assert not calls
        assert not router._client.is_closed
    finally:
        asyncio.run(router._client.aclose())
        project.close()
