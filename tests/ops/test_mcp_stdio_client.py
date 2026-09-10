from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from frisket.ops.integrations import mcp_stdio
from frisket.ops.integrations.mcp_stdio import (
    McpStdioClient,
    McpStdioError,
    McpStdioServerConfig,
)


FIXTURE_SERVER = (
    Path(__file__).resolve().parents[1] / "fixtures" / "mcp_stdio_server.py"
)


def _events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def _wait_for_event(path: Path, event: str) -> dict[str, Any]:
    deadline = time.monotonic() + 2  # realtime: bound real child-process rendezvous
    while time.monotonic() < deadline:  # realtime: poll real child output
        matches = [item for item in _events(path) if item["event"] == event]
        if matches:
            return matches[-1]
        await asyncio.sleep(0.01)
    raise AssertionError(f"fixture did not record {event!r}: {_events(path)!r}")


async def _wait_for_method(path: Path, method: str) -> dict[str, Any]:
    deadline = time.monotonic() + 2  # realtime: bound real child-process rendezvous
    while time.monotonic() < deadline:  # realtime: poll real child output
        matches = [
            item
            for item in _events(path)
            if item["event"] == "request" and item["method"] == method
        ]
        if matches:
            return matches[-1]
        await asyncio.sleep(0.01)
    raise AssertionError(f"fixture did not receive {method!r}: {_events(path)!r}")


async def _assert_process_stopped(pid: int) -> None:
    deadline = time.monotonic() + 2  # realtime: bound real MCP process teardown
    while time.monotonic() < deadline:  # realtime: poll real OS process state
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"MCP process {pid} remained alive")


def _config(
    tmp_path: Path,
    *,
    scenario: str = "normal",
    events: Path | None = None,
    inventory: Path | None = None,
    marker: str = "literal argument",
    env: dict[str, str] | None = None,
    secret_values: tuple[str, ...] = (),
) -> McpStdioServerConfig:
    event_path = events or (tmp_path / "events.jsonl")
    args = [
        str(FIXTURE_SERVER),
        "--scenario",
        scenario,
        "--events",
        str(event_path),
        "--argv-marker",
        marker,
    ]
    if inventory is not None:
        args.extend(("--inventory", str(inventory)))
    return McpStdioServerConfig(
        # subprocess-boundary: exercise the real MCP stdio child protocol
        command=sys.executable,
        args=tuple(args),
        cwd=tmp_path,
        env=env or {},
        secret_values=secret_values,
    )


@pytest.mark.asyncio
async def test_session_launches_direct_argv_discovers_all_pages_and_terminates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = tmp_path / "events.jsonl"
    marker = "literal ; $(touch should-not-exist)"
    monkeypatch.setenv("MCP_AMBIENT", "must-not-be-forwarded")
    client = McpStdioClient(
        _config(
            tmp_path,
            events=events,
            marker=marker,
            env={"MCP_EXPLICIT": "forwarded"},
        ),
        startup_timeout_seconds=1,
        request_timeout_seconds=1,
    )

    async with client.session() as session:
        assert [tool.name for tool in session.tools] == [
            "inspect_launch",
            "mixed_result",
        ]
        result = await session.call_tool("inspect_launch", {"value": "row-1"})
        launch = json.loads(result.content[0]["text"])
        started = await _wait_for_event(events, "started")

    assert launch == {
        "ambient_env": None,
        "arguments": {"value": "row-1"},
        "argv_marker": marker,
        "cwd": str(tmp_path),
        "explicit_env": "forwarded",
    }
    assert not (tmp_path / "should-not-exist").exists()
    methods = [item["method"] for item in _events(events) if item["event"] == "request"]
    assert methods == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/list",
        "tools/call",
    ]
    await _assert_process_stopped(started["pid"])


@pytest.mark.asyncio
async def test_independent_sessions_see_current_tools_and_can_coexist(
    tmp_path: Path,
) -> None:
    inventory = tmp_path / "inventory.txt"
    inventory.write_text("old_tool", encoding="utf-8")
    first_events = tmp_path / "first.jsonl"
    second_events = tmp_path / "second.jsonl"
    first_client = McpStdioClient(
        _config(tmp_path, events=first_events, inventory=inventory),
    )
    second_client = McpStdioClient(
        _config(tmp_path, events=second_events, inventory=inventory),
    )

    async with first_client.session() as first:
        assert [tool.name for tool in first.tools] == ["old_tool"]
        inventory.write_text("new_tool", encoding="utf-8")
        async with second_client.session() as second:
            assert [tool.name for tool in second.tools] == ["new_tool"]
            first_started = await _wait_for_event(first_events, "started")
            second_started = await _wait_for_event(second_events, "started")
            assert first_started["pid"] != second_started["pid"]
            first_result, second_result = await asyncio.gather(
                first.call_tool("old_tool", {}),
                second.call_tool("new_tool", {}),
            )
            assert first_result.content[0]["text"] == "called old_tool"
            assert second_result.content[0]["text"] == "called new_tool"

    await _assert_process_stopped(first_started["pid"])
    await _assert_process_stopped(second_started["pid"])


@pytest.mark.asyncio
async def test_tool_result_normalization_is_bounded_and_preserves_error_semantics(
    tmp_path: Path,
) -> None:
    client = McpStdioClient(
        _config(tmp_path),
        max_text_chars=80,
        max_result_bytes=4_096,
    )

    async with client.session() as session:
        result = await session.call_tool("mixed_result", {})

    assert result.tool_name == "mixed_result"
    assert result.is_error is True
    assert result.structured_content == {"count": 3, "items": ["one", "two"]}
    assert result.content[0]["type"] == "text"
    assert result.content[0]["text"].startswith("visible text")
    assert result.content[0]["truncated"] is True
    assert result.content[1] == {
        "type": "text_resource",
        "uri": "fixture://note",
        "mime_type": "text/plain",
        "text": "embedded resource text",
        "truncated": False,
    }
    assert result.content[2] == {
        "type": "binary_descriptor",
        "content_type": "image",
        "mime_type": "image/png",
        "size_bytes": 5,
        "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
    }
    assert result.content[3]["type"] == "unknown_descriptor"
    assert result.content[3]["content_type"] == "future_block"
    assert result.truncated is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "expected_code"),
    [
        ("eof", "mcp_session_eof"),
        ("malformed", "mcp_protocol_error"),
        ("oversized_frame", "mcp_frame_too_large"),
        ("hang_list", "mcp_request_timeout"),
    ],
)
async def test_startup_failures_are_bounded_and_leave_no_process(
    tmp_path: Path,
    scenario: str,
    expected_code: str,
) -> None:
    events = tmp_path / f"{scenario}.jsonl"
    timeout_seconds = 0.2 if scenario == "hang_list" else 2
    client = McpStdioClient(
        _config(tmp_path, scenario=scenario, events=events),
        startup_timeout_seconds=timeout_seconds,
        request_timeout_seconds=timeout_seconds,
        max_frame_bytes=1_024,
    )

    with pytest.raises(McpStdioError) as caught:
        async with client.session():
            raise AssertionError("session unexpectedly started")

    started = await _wait_for_event(events, "started")
    assert caught.value.code == expected_code
    await _assert_process_stopped(started["pid"])


@pytest.mark.asyncio
async def test_startup_timeout_survives_a_late_initialize_response(
    tmp_path: Path,
) -> None:
    events = tmp_path / "late-initialize.jsonl"
    client = McpStdioClient(
        _config(tmp_path, scenario="late_initialize", events=events),
        startup_timeout_seconds=0.05,
        request_timeout_seconds=0.05,
    )

    with pytest.raises(McpStdioError) as caught:
        async with client.session():
            raise AssertionError("session unexpectedly started")

    started = await _wait_for_event(events, "started")
    assert caught.value.code == "mcp_request_timeout"
    await _assert_process_stopped(started["pid"])


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_path", ["normal", "startup_failure", "startup_cancel"])
async def test_transport_cleanup_failure_still_closes_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_path: str,
) -> None:
    captures: list[mcp_stdio._StderrCapture] = []
    capture_class = mcp_stdio._StderrCapture
    transport = mcp_stdio.stdio_client
    initialize = McpStdioClient._initialize_and_discover
    cleanup_error = RuntimeError("transport cleanup failed")

    def capture_stderr(
        max_chars: int, secret_values: tuple[str, ...]
    ) -> mcp_stdio._StderrCapture:
        capture = capture_class(max_chars, secret_values)
        captures.append(capture)
        return capture

    @asynccontextmanager
    async def failing_transport(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        async with transport(*args, **kwargs) as streams:
            yield streams
        raise cleanup_error

    async def initialize_then_fail(
        self: McpStdioClient, *args: Any
    ) -> list[mcp_stdio.McpTool]:
        tools = await initialize(self, *args)
        if exit_path == "startup_failure":
            raise McpStdioError("mcp_protocol_error")
        if exit_path == "startup_cancel":
            raise asyncio.CancelledError
        return tools

    monkeypatch.setattr(mcp_stdio, "_StderrCapture", capture_stderr)
    monkeypatch.setattr(mcp_stdio, "stdio_client", failing_transport)
    monkeypatch.setattr(
        McpStdioClient, "_initialize_and_discover", initialize_then_fail
    )
    client = McpStdioClient(_config(tmp_path))

    try:
        with pytest.raises(RuntimeError) as caught:
            async with client.session():
                assert exit_path == "normal"
        assert caught.value is cleanup_error
        capture = captures[0]
        assert capture.writer.closed
        assert capture._task.done()
        assert capture._task.result() is None
        started = await _wait_for_event(tmp_path / "events.jsonl", "started")
        await _assert_process_stopped(started["pid"])
    finally:
        # Also release the real pipe if this regression fails before cleanup.
        for capture in captures:
            await capture.aclose()


@pytest.mark.asyncio
async def test_call_timeout_and_cancellation_close_the_server_process(
    tmp_path: Path,
) -> None:
    timeout_events = tmp_path / "timeout.jsonl"
    timeout_client = McpStdioClient(
        _config(tmp_path, scenario="hang_call", events=timeout_events),
        request_timeout_seconds=0.2,
    )
    async with timeout_client.session() as session:
        timeout_started = await _wait_for_event(timeout_events, "started")
        with pytest.raises(McpStdioError) as caught:
            await session.call_tool("inspect_launch", {})
        assert caught.value.code == "mcp_request_timeout"
        assert caught.value.may_have_dispatched is True
        assert caught.value.retryable is False
    await _assert_process_stopped(timeout_started["pid"])

    cancel_events = tmp_path / "cancel.jsonl"
    cancel_client = McpStdioClient(
        _config(tmp_path, scenario="hang_call", events=cancel_events),
        request_timeout_seconds=10,
    )
    async with cancel_client.session() as session:
        cancel_started = await _wait_for_event(cancel_events, "started")
        call = asyncio.create_task(session.call_tool("inspect_launch", {}))
        await _wait_for_method(cancel_events, "tools/call")
        call.cancel()
        with pytest.raises(McpStdioError) as cancelled:
            await call
        assert cancelled.value.code == "mcp_call_cancelled"
        assert cancelled.value.may_have_dispatched is True
        assert cancelled.value.retryable is False
    await _assert_process_stopped(cancel_started["pid"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "expected_code"),
    [
        ("malformed_call", "mcp_protocol_error"),
        ("stderr_secret", "mcp_session_eof"),
    ],
)
async def test_post_dispatch_protocol_and_process_failures_are_non_retryable(
    tmp_path: Path,
    scenario: str,
    expected_code: str,
) -> None:
    configured_value = "opaque_value_Z9_458"
    client = McpStdioClient(
        _config(
            tmp_path,
            scenario=scenario,
            env={"MCP_SECRET": configured_value},
            secret_values=(configured_value,),
        )
    )

    async with client.session() as session:
        with pytest.raises(McpStdioError) as caught:
            await session.call_tool("inspect_launch", {})

    assert caught.value.code == expected_code
    assert caught.value.may_have_dispatched is True
    assert caught.value.retryable is False
    assert caught.value.diagnostics["may_have_dispatched"] is True
    assert caught.value.diagnostics["retryable"] is False


@pytest.mark.asyncio
async def test_session_teardown_terminates_descendant_after_server_root_exits(
    tmp_path: Path,
) -> None:
    events = tmp_path / "descendant.jsonl"
    client = McpStdioClient(
        _config(tmp_path, scenario="descendant", events=events),
    )

    async with client.session():
        root = await _wait_for_event(events, "started")
        descendant = await _wait_for_event(events, "descendant_started")

    await _assert_process_stopped(root["pid"])
    await _assert_process_stopped(descendant["pid"])


@pytest.mark.asyncio
async def test_configured_secrets_are_redacted_from_failure_diagnostics(
    tmp_path: Path,
) -> None:
    configured_value = "opaque_value_Z9_458"
    events = tmp_path / "secret.jsonl"
    client = McpStdioClient(
        _config(
            tmp_path,
            scenario="stderr_secret",
            events=events,
            env={"MCP_SECRET": configured_value},
            secret_values=(configured_value,),
        ),
        max_stderr_chars=128,
    )

    async with client.session() as session:
        with pytest.raises(McpStdioError) as caught:
            await session.call_tool("missing_tool", {})

    rendered = json.dumps(caught.value.diagnostics, sort_keys=True) + str(caught.value)
    assert configured_value not in rendered
    assert "[REDACTED]" in rendered
    assert len(rendered) < 1_024


@pytest.mark.asyncio
async def test_secret_crossing_stderr_cutoff_does_not_leak_a_prefix(
    tmp_path: Path,
) -> None:
    configured_value = "opaque_value_crossing_cutoff_Z9"
    client = McpStdioClient(
        _config(
            tmp_path,
            scenario="stderr_cutoff",
            env={"MCP_SECRET": configured_value},
            secret_values=(configured_value,),
        ),
        max_stderr_chars=128,
    )

    async with client.session() as session:
        with pytest.raises(McpStdioError) as caught:
            await session.call_tool("inspect_launch", {})

    stderr = caught.value.diagnostics["stderr"]
    assert len(stderr) <= 128
    assert configured_value not in stderr
    assert configured_value[:12] not in stderr
    assert caught.value.diagnostics["stderr_truncated"] is True
