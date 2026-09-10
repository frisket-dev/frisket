"""Process-supervision gate for the combined native-server/worker image."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from pathlib import Path


ENTRYPOINT = Path(__file__).parents[1] / "entrypoint.sh"
DOCKERFILE = Path(__file__).parents[1] / "Dockerfile"


def test_dockerfile_can_install_into_ubuntu_system_python() -> None:
    assert "UV_BREAK_SYSTEM_PACKAGES=1" in DOCKERFILE.read_text(encoding="utf-8")


def test_dockerfile_includes_runtime_compilers() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert "gcc python3.12-dev" in dockerfile
    assert "VLLM_USE_FLASHINFER_SAMPLER=0" in dockerfile
    assert "cuda-nvcc" not in dockerfile


def test_native_server_dtype_matches_receipt() -> None:
    assert "--dtype bfloat16" in ENTRYPOINT.read_text(encoding="utf-8")


def test_native_server_encoder_cache_covers_long_form_audio() -> None:
    assert "--max-num-batched-tokens 73728" in ENTRYPOINT.read_text(encoding="utf-8")


def _executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    path.chmod(0o755)


def test_native_server_failure_terminates_and_reaps_worker(tmp_path: Path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ready = tmp_path / "worker.ready"
    terminated = tmp_path / "worker.terminated"
    worker_pid = tmp_path / "worker.pid"

    _executable(fake_bin / "python3", "printf 'pinned-test-value\\n'\n")
    _executable(fake_bin / "curl", "exit 0\n")
    _executable(
        fake_bin / "vllm",
        'while [ ! -e "$SUPERVISOR_READY" ]; do sleep 0.01; done\nexit 7\n',
    )
    _executable(
        fake_bin / "uvicorn",
        (
            "trap 'printf terminated > \"$SUPERVISOR_TERMINATED\"; exit 0' TERM\n"
            'printf "%s" "$$" > "$SUPERVISOR_WORKER_PID"\n'
            'printf ready > "$SUPERVISOR_READY"\n'
            "while :; do sleep 1; done\n"
        ),
    )

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "SUPERVISOR_READY": str(ready),
        "SUPERVISOR_TERMINATED": str(terminated),
        "SUPERVISOR_WORKER_PID": str(worker_pid),
    }
    try:
        result = subprocess.run(
            ["bash", str(ENTRYPOINT)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
        assert result.returncode == 7
        assert terminated.read_text(encoding="utf-8") == "terminated"
    finally:
        # Prevent a regression in the supervisor from leaking the fake worker
        # beyond the test process.
        if worker_pid.exists() and not terminated.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(worker_pid.read_text(encoding="utf-8")), signal.SIGTERM)


def test_sigint_is_translated_to_term_and_reaps_both_children(tmp_path: Path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    _executable(fake_bin / "python3", "printf 'pinned-test-value\\n'\n")
    _executable(fake_bin / "curl", "exit 0\n")
    child = """\
trap '' INT
trap 'printf terminated > "$SUPERVISOR_STATE/$(basename "$0").terminated"; exit 0' TERM
printf ready > "$SUPERVISOR_STATE/$(basename "$0").ready"
while :; do sleep 1; done
"""
    _executable(fake_bin / "vllm", child)
    _executable(fake_bin / "uvicorn", child)

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "SUPERVISOR_STATE": str(state_dir),
    }
    process = subprocess.Popen(
        ["bash", str(ENTRYPOINT)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 5
        ready = [state_dir / "vllm.ready", state_dir / "uvicorn.ready"]
        while not all(path.exists() for path in ready):
            assert time.monotonic() < deadline, "fake children did not start"
            time.sleep(0.01)

        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=5) == 130
        assert (state_dir / "vllm.terminated").exists()
        assert (state_dir / "uvicorn.terminated").exists()
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)


def test_worker_port_waits_for_native_server_readiness(tmp_path: Path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    _executable(fake_bin / "python3", "printf 'pinned-test-value\\n'\n")
    _executable(
        fake_bin / "curl",
        '[ -e "$SUPERVISOR_STATE/native.healthy" ]\n',
    )
    child = """\
trap 'exit 0' TERM
printf ready > "$SUPERVISOR_STATE/$(basename "$0").ready"
while :; do sleep 1; done
"""
    _executable(fake_bin / "vllm", child)
    _executable(fake_bin / "uvicorn", child)

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "SUPERVISOR_STATE": str(state_dir),
        "FRISKET_MOSS_NATIVE_STARTUP_TIMEOUT": "5",
    }
    process = subprocess.Popen(
        ["bash", str(ENTRYPOINT)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not (state_dir / "vllm.ready").exists():
            assert time.monotonic() < deadline, "fake native server did not start"
            time.sleep(0.01)

        time.sleep(0.1)
        assert not (state_dir / "uvicorn.ready").exists()

        (state_dir / "native.healthy").touch()
        while not (state_dir / "uvicorn.ready").exists():
            assert time.monotonic() < deadline, "worker did not start after readiness"
            time.sleep(0.01)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
