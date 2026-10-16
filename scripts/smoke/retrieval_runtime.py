"""Installed-product smoke for the optional multimodal retrieval runtime."""

from __future__ import annotations

import json
from pathlib import Path
import struct
import subprocess
from typing import Any

import frisket
from frisket.engine._workers.embeddinggemma_model import MODEL_REVISION
from frisket.runtime.embeddinggemma_install import (
    install_embeddinggemma,
    is_installed,
    runtime_python,
)
from frisket.runtime.launch import PythonRuntime, worker_argv
from frisket.runtime.model_install import model_child_environment


def _frame(value: dict[str, Any]) -> bytes:
    payload = json.dumps(value, separators=(",", ":")).encode()
    return struct.pack(">I", len(payload)) + payload


def _response(data: bytes) -> dict[str, Any]:
    if len(data) < 4:
        raise RuntimeError("retrieval worker returned no framed response")
    (size,) = struct.unpack(">I", data[:4])
    if len(data) != size + 4:
        raise RuntimeError("retrieval worker returned an invalid framed response")
    value = json.loads(data[4:].decode())
    if not isinstance(value, dict):
        raise RuntimeError("retrieval worker response must be an object")
    return value


def probe_worker_start(workspace: Path) -> None:
    """Start the installed worker without loading weights or encoding input."""

    app_root = Path(frisket.__file__).resolve().parent.parent
    argv = worker_argv(
        "embeddinggemma-session",
        runtime=PythonRuntime(runtime_python(), app_root),
    )
    request = {
        "schema_version": 1,
        "type": "init",
        "model_revision": MODEL_REVISION,
        "snapshot": str(workspace / "missing" / MODEL_REVISION),
        "staging_root": str(workspace),
    }
    result = subprocess.run(  # noqa: S603
        argv,
        input=_frame(request),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=model_child_environment(),
        timeout=30,
        check=False,
    )
    if result.returncode != 1:
        raise RuntimeError(
            "retrieval worker did not refuse the absent model snapshot: "
            + result.stderr.decode(errors="replace")[-1000:]
        )
    response = _response(result.stdout)
    expected = {
        "schema_version": 1,
        "type": "error",
        "code": "model_unavailable",
    }
    if any(response.get(key) != value for key, value in expected.items()):
        raise RuntimeError(f"unexpected retrieval worker response: {response!r}")


def main() -> int:
    install_embeddinggemma(should_cancel=lambda: False, progress=print)
    if not is_installed():
        raise RuntimeError("multimodal retrieval runtime was not installed")
    probe_worker_start(Path.cwd())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
