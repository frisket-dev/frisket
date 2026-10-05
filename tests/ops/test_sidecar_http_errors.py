"""Sidecar callers can distinguish HTTP failures without private response text."""

from types import SimpleNamespace

import httpx
import pytest

from frisket.execution.provider import ConnectionConfig
from frisket.ops._sidecar import SidecarHTTPError, sidecar_post


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 422, 500])
async def test_sidecar_http_error_preserves_status_but_not_response_body(status):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text="private document secret-token")
        )
    ) as client:
        with pytest.raises(SidecarHTTPError) as caught:
            await sidecar_post(
                SimpleNamespace(http=client),
                "/classify",
                op="classify",
                json={"text": "input"},
                connection=ConnectionConfig(
                    base_url="http://models.test", token="test"
                ),
            )
    assert isinstance(caught.value, RuntimeError)
    assert caught.value.status_code == status
    assert str(caught.value) == f"sidecar classify failed ({status})"
