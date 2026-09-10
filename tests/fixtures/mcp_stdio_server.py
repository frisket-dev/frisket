from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="normal")
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--argv-marker", default="")
    return parser.parse_args()


ARGS = _parse_args()


def _record(event: str, **values: Any) -> None:
    with ARGS.events.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"event": event, "pid": os.getpid(), **values},
                sort_keys=True,
            )
            + "\n"
        )


def _reply(request_id: int | str, result: dict[str, Any]) -> None:
    sys.stdout.write(
        json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "result": result},
            separators=(",", ":"),
        )
        + "\n"
    )
    sys.stdout.flush()


def _tool(name: str, description: str = "fixture tool") -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "additionalProperties": True,
        },
    }


def _listed_tools(cursor: str | None) -> dict[str, Any]:
    if ARGS.inventory is not None:
        name = ARGS.inventory.read_text(encoding="utf-8").strip()
        return {"tools": [_tool(name)]}
    if cursor is None:
        return {
            "tools": [_tool("inspect_launch")],
            "nextCursor": "fixture-page-2",
        }
    if cursor == "fixture-page-2":
        return {"tools": [_tool("mixed_result")]}
    return {"tools": []}


def _mixed_result() -> dict[str, Any]:
    return {
        "content": [
            {"type": "text", "text": "visible text " + ("x" * 256)},
            {
                "type": "resource",
                "resource": {
                    "uri": "fixture://note",
                    "mimeType": "text/plain",
                    "text": "embedded resource text",
                },
            },
            {
                "type": "image",
                "mimeType": "image/png",
                "data": "aGVsbG8=",
            },
            {
                "type": "future_block",
                "mimeType": "application/x-fixture",
                "payload": {"opaque": True},
            },
        ],
        "structuredContent": {"count": 3, "items": ["one", "two"]},
        "isError": True,
    }


def _handle(request: dict[str, Any]) -> None:
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    _record("request", method=method, request_id=request_id, params=params)

    if method == "initialize":
        if ARGS.scenario == "late_initialize":
            time.sleep(0.2)  # realtime: reply after the client's startup timeout
        _reply(
            request_id,
            {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "frisket-test", "version": "1.0"},
            },
        )
        return
    if method == "notifications/initialized":
        return
    if method == "tools/list":
        if ARGS.scenario == "eof":
            raise SystemExit(0)
        if ARGS.scenario == "malformed":
            sys.stdout.write("this is not json\n")
            sys.stdout.flush()
            return
        if ARGS.scenario == "oversized_frame":
            _reply(request_id, {"tools": [_tool("huge", "y" * 16_384)]})
            return
        if ARGS.scenario == "hang_list":
            while True:
                time.sleep(0.05)  # realtime: intentionally hang child for timeout test
        if ARGS.scenario == "secret_inventory":
            _reply(
                request_id,
                {
                    "tools": [
                        _tool(
                            "secret_inventory",
                            "credential " + os.environ.get("MCP_SECRET", ""),
                        )
                    ]
                },
            )
            return
        cursor = params.get("cursor")
        _reply(request_id, _listed_tools(cursor if isinstance(cursor, str) else None))
        return
    if method == "tools/call":
        name = params.get("name")
        if ARGS.scenario == "hang_call":
            while True:
                time.sleep(0.05)  # realtime: intentionally hang child for timeout test
        if ARGS.scenario == "malformed_call":
            sys.stdout.write("malformed tool response\n")
            sys.stdout.flush()
            return
        if ARGS.scenario in {"stderr_cutoff", "stderr_secret"}:
            raise SystemExit(2)
        if name == "inspect_launch":
            arguments = params.get("arguments")
            _reply(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "argv_marker": ARGS.argv_marker,
                                    "cwd": os.getcwd(),
                                    "explicit_env": os.environ.get("MCP_EXPLICIT"),
                                    "ambient_env": os.environ.get("MCP_AMBIENT"),
                                    "arguments": arguments,
                                },
                                sort_keys=True,
                            ),
                        }
                    ],
                    "isError": False,
                },
            )
            return
        if name == "mixed_result":
            _reply(request_id, _mixed_result())
            return
        _reply(
            request_id,
            {
                "content": [{"type": "text", "text": f"called {name}"}],
                "isError": False,
            },
        )


def main() -> None:
    _record("started", argv_marker=ARGS.argv_marker, cwd=os.getcwd())
    try:
        if ARGS.scenario == "descendant":
            descendant = subprocess.Popen(  # noqa: S603 -- fixed test fixture argv
                [
                    sys.executable,  # subprocess-boundary: teardown must reap a real descendant
                    "-c",
                    "import time; time.sleep(30)",  # realtime: keep descendant alive for teardown test
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _record("descendant_started", pid=descendant.pid)
        if ARGS.scenario == "stderr_secret":
            print(
                (
                    f"fixture failed with token {os.environ.get('MCP_SECRET', '')} "
                    + ("diagnostic " * 1_024)
                ),
                file=sys.stderr,
                flush=True,
            )
        if ARGS.scenario == "stderr_cutoff":
            print(
                ("p" * 124) + os.environ.get("MCP_SECRET", "") + " tail",
                file=sys.stderr,
                flush=True,
            )
        for line in sys.stdin:
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(request, dict):
                _handle(request)
    finally:
        _record("stopped")


if __name__ == "__main__":
    main()
