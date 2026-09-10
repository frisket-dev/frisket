"""Typed safe errors remain supported across the projection subprocess boundary."""

from __future__ import annotations

import asyncio
import json
import io
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from frisket.contracts.plugin_rpc import ProjectionRequest, ProjectionFrame
from pydantic import TypeAdapter
from frisket.plugins.subprocess_runner import run_projection_async


PLUGIN_SOURCE = """
from frisket.plugins.sdk import Plugin, PluginRateLimited, PluginUserError

plugin = Plugin()


@plugin.projection("boom")
def boom(ctx, rows, *, params, target):
    mode = params["mode"]
    if mode == "user_error":
        raise PluginUserError("minimum_match_score must be a number from 0 to 100")
    if mode == "rate_limited":
        raise PluginRateLimited(
            "leaky provider detail MARKER-RL", retry_after_seconds=2.5
        )
    if mode == "rate_limited_bare":
        raise PluginRateLimited()
    if mode == "rate_limited_header":
        raise PluginRateLimited(retry_after_seconds="3")
    if mode == "duck_429":
        class _Response:
            status_code = 429
            headers = {"Retry-After": "3"}

        err = RuntimeError("upstream MARKER-DUCK")
        err.response = _Response()
        raise err
    raise ValueError("internal MARKER-GENERIC /home/user/.netrc")
"""


def _run(tmp_path: Path, mode: str) -> dict[str, Any]:
    plugin_root = tmp_path / "plugin_pkg"
    plugin_root.mkdir(exist_ok=True)
    (plugin_root / "plugin.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    payload = {
        "pluginId": "",
        "handlerKey": "boom",
        "projectionKind": "boom",
        "modulePath": "plugin.py",
        "params": {"mode": mode},
        "context": {
            "projectId": "p1",
            "pluginId": "",
            "handlerKey": "boom",
            "projectionKind": "boom",
            "capabilities": ["projection.build"],
        },
        "rows": [],
    }
    output = io.StringIO()
    with redirect_stdout(output):
        asyncio.run(
            run_projection_async(plugin_root, ProjectionRequest.model_validate(payload))
        )
    return json.loads(output.getvalue())


def _first_error(response: dict[str, Any]) -> dict[str, Any]:
    assert response["type"] == "error"
    return response["error"]


def test_plugin_user_error_message_surfaces_verbatim(tmp_path: Path) -> None:
    error = _first_error(_run(tmp_path, "user_error"))
    assert error["code"] == "plugin_user_error"
    assert error["message"] == "minimum_match_score must be a number from 0 to 100"


def test_generic_exception_stays_redacted(tmp_path: Path) -> None:
    response = _run(tmp_path, "generic")
    error = _first_error(response)
    assert error["code"] == "plugin_subprocess_failed"
    assert error["message"] == "Trusted-local plugin subprocess failed"
    assert "MARKER-GENERIC" not in json.dumps(response)
    assert ".netrc" not in json.dumps(response)


def test_plugin_rate_limited_maps_retry_metadata_not_message(tmp_path: Path) -> None:
    response = _run(tmp_path, "rate_limited")
    error = _first_error(response)
    assert error["code"] == "plugin_provider_rate_limited"
    assert error["message"] == "External service rate limited the plugin request"
    assert error["details"]["retryable"] is True
    assert error["details"]["retry_after_ms"] == 2500
    # The exception message never crosses the boundary — only retry metadata.
    assert "MARKER-RL" not in json.dumps(response)


def test_plugin_rate_limited_without_retry_after(tmp_path: Path) -> None:
    error = _first_error(_run(tmp_path, "rate_limited_bare"))
    assert error["code"] == "plugin_provider_rate_limited"
    assert error["details"]["retryable"] is True
    assert "retry_after_ms" not in error["details"]


def test_plugin_rate_limited_accepts_retry_after_header_value(tmp_path: Path) -> None:
    error = _first_error(_run(tmp_path, "rate_limited_header"))
    assert error["code"] == "plugin_provider_rate_limited"
    assert error["details"]["retry_after_ms"] == 3000


def test_untyped_http_429_exception_stays_redacted(tmp_path: Path) -> None:
    response = _run(tmp_path, "duck_429")
    error = _first_error(response)
    assert error["code"] == "plugin_subprocess_failed"
    assert error["details"] == {}
    assert "MARKER-DUCK" not in json.dumps(response)


def test_plugin_rate_limited_round_trips_through_projection_contract(
    tmp_path: Path,
) -> None:
    response = _run(tmp_path, "rate_limited")
    frame = TypeAdapter(ProjectionFrame).validate_python(response)
    assert frame.error.details.retry_after_ms == 2500
    assert frame.model_dump(mode="json", exclude_none=True) == response
