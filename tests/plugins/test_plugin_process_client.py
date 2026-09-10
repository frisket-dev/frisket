from __future__ import annotations

import ast
import inspect
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, get_origin, get_type_hints

import pytest

from frisket.contracts import plugin_rpc as rpc
from frisket.engine.sandbox.shim import SandboxResult
from frisket.plugins import process_client
from frisket.plugins.process_client import (
    PluginProcessClient,
    PluginProcessError,
)


def _importer_request() -> rpc.ImporterRequest:
    return rpc.ImporterRequest.model_validate(
        {
            "pluginId": "example.plugin",
            "handlerKey": "handler",
            "importerKind": "example.importer",
            "modulePath": "plugin.py",
            "source": {},
            "context": {
                "projectId": "project",
                "pluginId": "example.plugin",
                "handlerKey": "handler",
                "importerKind": "example.importer",
                "capabilities": ["project:write"],
            },
        }
    )


def _projection_request() -> rpc.ProjectionRequest:
    return rpc.ProjectionRequest.model_validate(
        {
            "pluginId": "example.plugin",
            "handlerKey": "handler",
            "projectionKind": "example.projection",
            "modulePath": "plugin.py",
            "context": {
                "projectId": "project",
                "pluginId": "example.plugin",
                "handlerKey": "handler",
                "projectionKind": "example.projection",
                "capabilities": ["projection.build"],
            },
        }
    )


def _rate_limited_error_frame() -> dict[str, Any]:
    return {
        "type": "error",
        "error": {
            "code": "plugin_provider_rate_limited",
            "message": "External service rate limited the plugin request",
            "details": {
                "retryable": True,
                "retry_after_ms": 3000,
            },
        },
    }


def _streaming_result(frame: dict[str, Any]) -> Any:
    async def fake_lines(
        _argv: list[str],
        *,
        on_stdout_line: Any,
        **_kwargs: Any,
    ) -> SandboxResult:
        on_stdout_line(json.dumps(frame))
        return SandboxResult(returncode=0, stdout="", stderr="")

    return fake_lines


def _operator_request() -> rpc.OperatorRequest:
    return rpc.OperatorRequest.model_validate(
        {
            "pluginId": "example.plugin",
            "handlerKey": "handler",
            "operatorKind": "example.operator",
            "modulePath": "plugin.py",
            "context": {
                "projectId": "project",
                "pluginId": "example.plugin",
                "handlerKey": "handler",
                "operatorKind": "example.operator",
                "capabilities": ["operator.filter"],
            },
        }
    )


def _completed_action_result() -> SandboxResult:
    return SandboxResult(
        returncode=0,
        stdout=json.dumps(
            {
                "status": "completed",
                "plan": None,
                "errors": [],
                "warnings": [],
            }
        ),
        stderr="",
    )


def test_client_entry_points_have_no_project_or_database_handle_types() -> None:
    entry_points = (
        PluginProcessClient.importer,
        PluginProcessClient.projection,
        PluginProcessClient.operator,
    )
    forbidden = (r"\bProject\b", r"\bsqlite3\b", r"\bConnection\b", r"\bdb_handle\b")
    for entry_point in entry_points:
        signature = inspect.signature(entry_point)
        rendered = str(signature)
        assert not any(re.search(pattern, rendered) for pattern in forbidden), rendered


def test_env_allowlist_and_extra_env_are_forwarded_exactly() -> None:
    captured: list[tuple[list[str], dict[str, str], dict[str, str]]] = []

    async def fake_run(
        _argv: list[str],
        *,
        policy: Any,
        extra_env: dict[str, str],
        **_kwargs: Any,
    ) -> SandboxResult:
        captured.append((policy.allowed_extra_env, extra_env, dict(extra_env)))
        return _completed_action_result()

    client = PluginProcessClient("plugin-root", run_sandboxed_call=fake_run)
    envs = (
        {},
        {"PLUGIN_TOKEN": "one"},
        {"ZED": "last", "OPENAI_API_KEY": "caller-filtered-value", "ALPHA": "first"},
    )
    for env in envs:
        client.operator(_operator_request(), env)

    assert len(captured) == len(envs)
    for env, (allowed, forwarded, copied) in zip(envs, captured, strict=True):
        assert allowed == sorted(env)
        assert forwarded is env
        assert copied == env


def test_streams_are_iterators_and_worker_failures_are_structured() -> None:
    for method_name in ("importer", "projection"):
        annotation = get_type_hints(getattr(PluginProcessClient, method_name))["return"]
        value = getattr(annotation, "__value__", annotation)
        assert get_origin(value) is Iterator
        assert get_origin(value) not in {list, tuple}

    calls = 0

    async def fake_lines(
        _argv: list[str],
        *,
        on_stdout_line: Any,
        **_kwargs: Any,
    ) -> SandboxResult:
        nonlocal calls
        calls += 1
        on_stdout_line(
            json.dumps(
                {
                    "type": "done",
                }
            )
        )
        return SandboxResult(returncode=0, stdout="", stderr="")

    client = PluginProcessClient(
        "plugin-root",
        run_sandboxed_stdout_lines_call=fake_lines,
    )
    frames = client.projection(_projection_request(), {})
    assert isinstance(frames, Iterator)
    assert list(frames) == [rpc.ProjectionDoneFrame(type="done")]
    assert calls == 1

    async def worker_exception(
        _argv: list[str],
        **_kwargs: Any,
    ) -> SandboxResult:
        raise RuntimeError("sandbox worker diagnostic")

    worker_error_client = PluginProcessClient(
        "plugin-root",
        run_sandboxed_stdout_lines_call=worker_exception,
    )
    with pytest.raises(PluginProcessError) as worker_error:
        list(worker_error_client.projection(_projection_request(), {}))
    assert worker_error.value.failure.code == "plugin_subprocess_failed"
    assert isinstance(worker_error.value.__cause__, RuntimeError)
    assert str(worker_error.value.__cause__) == "sandbox worker diagnostic"


def test_cancelled_sandbox_result_preserves_action_cancellation() -> None:
    async def cancelled(*_args: Any, **_kwargs: Any) -> SandboxResult:
        return SandboxResult(
            returncode=-15,
            stdout="",
            stderr="",
            cancelled=True,
        )

    single = PluginProcessClient("plugin-root", run_sandboxed_call=cancelled)
    with pytest.raises(PluginProcessError) as single_error:
        single.operator(_operator_request(), {})
    assert single_error.value.failure.code == "action_cancelled"

    streamed = PluginProcessClient(
        "plugin-root", run_sandboxed_stdout_lines_call=cancelled
    )
    with pytest.raises(PluginProcessError) as stream_error:
        list(streamed.importer(_importer_request(), {}))
    assert stream_error.value.failure.code == "action_cancelled"


def test_client_contains_no_process_supervision_reimplementation() -> None:
    source_path = Path(inspect.getsourcefile(process_client) or "")
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_modules.update(
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )
    assert {"subprocess", "os", "signal"}.isdisjoint(imported_modules)
    forbidden_calls = {"Popen", "kill", "killpg", "create_subprocess_exec"}
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in forbidden_calls
        for node in ast.walk(tree)
    )
    assert "shim.run_sandboxed" in source
    assert "shim.run_sandboxed_stdout_lines" in source
    assert "join(timeout" not in source


def test_each_invocation_spawns_once_and_serializes_typed_rate_limit() -> None:
    calls = 0
    legacy_payload = {
        "status": "failed",
        "plan": None,
        "errors": [
            {
                "code": "plugin_provider_rate_limited",
                "message": "External service rate limited the plugin request",
                "details": {
                    "retryable": True,
                    "retry_after_ms": 3000,
                },
            }
        ],
        "warnings": [],
    }

    async def fake_run(_argv: list[str], **_kwargs: Any) -> SandboxResult:
        nonlocal calls
        calls += 1
        return SandboxResult(
            returncode=0,
            stdout=json.dumps(legacy_payload),
            stderr="",
        )

    client = PluginProcessClient("plugin-root", run_sandboxed_call=fake_run)
    first = client.operator(_operator_request(), {})
    second = client.operator(_operator_request(), {})

    assert calls == 2
    assert client.response_payload(first) == {
        k: v for k, v in legacy_payload.items() if v is not None
    }
    assert client.response_payload(second) == {
        k: v for k, v in legacy_payload.items() if v is not None
    }

    async def unexpected_schema(_argv: list[str], **_kwargs: Any) -> SandboxResult:
        payload = dict(legacy_payload)
        payload["schemaVersion"] = "obsolete"
        return SandboxResult(returncode=0, stdout=json.dumps(payload), stderr="")

    invalid_client = PluginProcessClient(
        "plugin-root",
        run_sandboxed_call=unexpected_schema,
    )
    with pytest.raises(PluginProcessError) as caught:
        invalid_client.operator(_operator_request(), {})
    assert caught.value.failure.code == "plugin_subprocess_invalid_response"


def test_importer_accepts_typed_rate_limit_error_frame() -> None:
    payload = _rate_limited_error_frame()
    client = PluginProcessClient(
        "plugin-root",
        run_sandboxed_stdout_lines_call=_streaming_result(payload),
    )

    frames = list(client.importer(_importer_request(), {}))

    assert len(frames) == 1
    frame = frames[0]
    assert isinstance(frame, rpc.TableErrorFrame)
    assert frame.error.code == "plugin_provider_rate_limited"
    assert client.frame_payload(frame) == payload


def test_projection_accepts_typed_rate_limit_error_frame() -> None:
    payload = _rate_limited_error_frame()
    client = PluginProcessClient(
        "plugin-root",
        run_sandboxed_stdout_lines_call=_streaming_result(payload),
    )

    frames = list(client.projection(_projection_request(), {}))

    assert len(frames) == 1
    frame = frames[0]
    assert isinstance(frame, rpc.ProjectionErrorFrame)
    assert frame.error.code == "plugin_provider_rate_limited"
    assert client.frame_payload(frame) == payload


def test_projection_stream_allows_output_over_legacy_total_cap() -> None:
    async def frames_over_legacy_cap(
        _argv: list[str], *, on_stdout_line: Any, **_kwargs: Any
    ) -> SandboxResult:
        for index in range(5_001):
            on_stdout_line(json.dumps({"type": "timeline_item", "item": {"id": index}}))
        on_stdout_line(json.dumps({"type": "done"}))
        return SandboxResult(returncode=0, stdout="", stderr="")

    client = PluginProcessClient(
        "plugin-root", run_sandboxed_stdout_lines_call=frames_over_legacy_cap
    )
    frames = list(client.projection(_projection_request(), {}))

    assert len(frames) == 5_002
    assert isinstance(frames[-1], rpc.ProjectionDoneFrame)


def test_streaming_error_details_forbid_retired_status_code() -> None:
    payload = _rate_limited_error_frame()
    payload["error"]["details"]["status_code"] = 429
    client = PluginProcessClient(
        "plugin-root",
        run_sandboxed_stdout_lines_call=_streaming_result(payload),
    )

    with pytest.raises(PluginProcessError) as caught:
        list(client.projection(_projection_request(), {}))

    assert caught.value.failure.code == "plugin_subprocess_invalid_response"
