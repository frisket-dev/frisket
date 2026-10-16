from __future__ import annotations

import json
from pathlib import Path
import struct
import subprocess

import pytest

from scripts.smoke import retrieval_runtime


def _worker_response(value: dict[str, object]) -> bytes:
    payload = json.dumps(value, separators=(",", ":")).encode()
    return struct.pack(">I", len(payload)) + payload


def test_probe_starts_managed_worker_and_accepts_typed_missing_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime-python"
    runtime.touch()
    monkeypatch.setattr(retrieval_runtime, "runtime_python", lambda: runtime)
    monkeypatch.setattr(
        retrieval_runtime,
        "worker_argv",
        lambda kind, *, runtime: [str(runtime.executable), kind],
    )
    monkeypatch.setattr(
        retrieval_runtime,
        "model_child_environment",
        lambda: {"PATH": "worker-path"},
    )
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout=_worker_response(
                {
                    "schema_version": 1,
                    "type": "error",
                    "code": "model_unavailable",
                    "message": "wording may change",
                }
            ),
            stderr=b"",
        )

    monkeypatch.setattr(retrieval_runtime.subprocess, "run", run)

    retrieval_runtime.probe_worker_start(tmp_path)

    assert calls[0][0] == [str(runtime), "embeddinggemma-session"]
    assert calls[0][1]["env"] == {"PATH": "worker-path"}
    size = struct.unpack(">I", calls[0][1]["input"][:4])[0]
    request = json.loads(calls[0][1]["input"][4 : 4 + size])
    assert request["type"] == "init"
    assert request["snapshot"] == str(
        tmp_path / "missing" / retrieval_runtime.MODEL_REVISION
    )


@pytest.mark.parametrize(
    ("returncode", "response"),
    [
        (0, {"schema_version": 1, "type": "ready", "code": None}),
        (1, {"schema_version": 1, "type": "error", "code": "wrong"}),
    ],
)
def test_probe_rejects_wrong_exit_or_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    response: dict[str, object],
) -> None:
    monkeypatch.setattr(retrieval_runtime, "runtime_python", lambda: tmp_path / "py")
    monkeypatch.setattr(
        retrieval_runtime,
        "worker_argv",
        lambda *_args, **_kwargs: ["worker"],
    )
    monkeypatch.setattr(
        retrieval_runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["worker"], returncode, stdout=_worker_response(response), stderr=b""
        ),
    )

    with pytest.raises(RuntimeError):
        retrieval_runtime.probe_worker_start(tmp_path)
