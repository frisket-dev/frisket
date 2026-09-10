"""HTTP contract for connecting saved Solo MCP settings to stdio discovery."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.server.app import create_app


FIXTURE_SERVER = (
    Path(__file__).resolve().parents[1] / "fixtures" / "mcp_stdio_server.py"
)


def _events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _wait_for_event(path: Path, event: str) -> dict[str, Any]:
    deadline = time.monotonic() + 2  # realtime: bound real child-process rendezvous
    while time.monotonic() < deadline:  # realtime: poll real child output
        matches = [item for item in _events(path) if item["event"] == event]
        if matches:
            return matches[-1]
        time.sleep(0.01)  # realtime: yield while real child records output
    raise AssertionError(f"fixture did not record {event!r}: {_events(path)!r}")


def _assert_process_stopped(pid: int) -> None:
    deadline = time.monotonic() + 2  # realtime: bound real MCP process teardown
    while time.monotonic() < deadline:  # realtime: poll real OS process state
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)  # realtime: yield while real OS process exits
    raise AssertionError(f"MCP process {pid} remained alive")


def test_saved_project_mcp_server_tests_through_stdio_discovery_without_tool_calls(
    tmp_path: Path,
) -> None:
    """The settings endpoint only probes the configured outbound MCP server.

    A successful probe is an initialize plus complete tool inventory, never a
    tool invocation.  Project-secret values are passed to the child only for
    that probe, while persistence and the public test report keep them opaque.
    """
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = client.post("/api/projects", json={"name": "MCP runtime"}).json()["id"]
    secret = "project-secret-must-not-appear-in-mcp-settings-or-diagnostics"
    saved_secret = client.post(
        f"/api/projects/{project_id}/secrets",
        json={"name": "MCP_SECRET", "value": secret},
    )
    assert saved_secret.status_code == 200, saved_secret.text

    events = tmp_path / "mcp-events.jsonl"
    created = client.post(
        f"/api/projects/{project_id}/mcp-servers",
        json={
            "name": "Fixture stdio server",
            # subprocess-boundary: persisted settings must launch a real MCP stdio child
            "command": sys.executable,
            "args": [
                str(FIXTURE_SERVER),
                "--scenario",
                "stderr_secret",
                "--events",
                str(events),
            ],
            "cwd": str(tmp_path),
            "env": {
                "MCP_EXPLICIT": {"value": "launch-only-literal"},
                "MCP_SECRET": {"project_secret": "MCP_SECRET"},
            },
        },
    )
    assert created.status_code == 200, created.text
    server = created.json()
    assert secret not in created.text

    # The project secret reference, not its plaintext, is the only value that
    # survives as settings metadata before the child launch.
    project = client.app.state.workspace.get(project_id)
    assert secret not in (project.get_meta("project_mcp_servers_v1") or "")

    tested = client.post(f"/api/projects/{project_id}/mcp-servers/{server['id']}/test")
    assert tested.status_code == 200, tested.text
    report = tested.json()["lastTest"]
    assert report["status"] == "succeeded"
    assert report["tested_at"].endswith("Z")
    assert tested.json()["last_discovered_tool_count"] == 2
    # The HTTP contract's success term remains ``succeeded``; human-facing
    # diagnostics are advisory inventory, not an alternate state vocabulary.
    assert "passed" in report["diagnostic"].lower()
    assert "2" in report["diagnostic"]
    assert "inspect_launch" in report["diagnostic"]
    assert "mixed_result" in report["diagnostic"]
    assert secret not in tested.text
    assert secret not in json.dumps(report, sort_keys=True)
    persisted = client.get(f"/api/projects/{project_id}/mcp-servers")
    assert persisted.status_code == 200, persisted.text
    assert persisted.json()["servers"][0]["lastTest"] == report
    assert persisted.json()["servers"][0]["last_discovered_tool_count"] == 2

    started = _wait_for_event(events, "started")
    methods = [item["method"] for item in _events(events) if item["event"] == "request"]
    assert methods == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/list",
    ]
    _assert_process_stopped(started["pid"])

    disabled = client.patch(
        f"/api/projects/{project_id}/mcp-servers/{server['id']}",
        json={"enabled": False},
    )
    assert disabled.status_code == 200, disabled.text
    events_before_disabled_test = _events(events)
    disabled_test = client.post(
        f"/api/projects/{project_id}/mcp-servers/{server['id']}/test"
    )
    assert disabled_test.status_code == 200, disabled_test.text
    assert disabled_test.json()["lastTest"]["status"] == "failed"
    assert _events(events) == events_before_disabled_test

    missing = client.post(f"/api/projects/{project_id}/mcp-servers/mcp_missing/test")
    assert missing.status_code == 404
