"""Process-level proofs for the host-shared GPU admission lease."""

from __future__ import annotations

import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

from frisket_models.transcription.gpu_lease import (
    GPU_LEASE_BUSY_EXIT_CODE,
    acquire_gpu_lease,
)


def _lease_command(lock_file: Path, command: list[str]) -> list[str]:
    return [
        sys.executable,
        "-m",
        "frisket_models.transcription.gpu_lease",
        "--lock-file",
        str(lock_file),
        "--",
        *command,
    ]


def test_lease_is_nonblocking_and_reusable_after_release(tmp_path: Path) -> None:
    lock_file = tmp_path / "gpu.lock"
    with acquire_gpu_lease(lock_file) as lease:
        assert os.get_inheritable(lease.fd) is True
        assert lock_file.read_text() == f"pid={os.getpid()}\n"

        contender = subprocess.run(
            _lease_command(lock_file, [sys.executable, "-c", "pass"]),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert contender.returncode == GPU_LEASE_BUSY_EXIT_CODE
        assert "already held" in contender.stderr

    successor = subprocess.run(
        _lease_command(lock_file, [sys.executable, "-c", "pass"]),
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert successor.returncode == 0


def test_lease_descriptor_survives_exec_until_worker_exits(tmp_path: Path) -> None:
    lock_file = tmp_path / "gpu.lock"
    holder = subprocess.Popen(
        _lease_command(
            lock_file,
            [
                sys.executable,
                "-c",
                "import signal; print('ready', flush=True); signal.pause()",
            ],
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        readable, _, _ = select.select([holder.stdout], [], [], 5)
        assert readable, "lease holder did not exec its worker command"
        assert holder.stdout.readline().strip() == "ready"

        contender = subprocess.run(
            _lease_command(lock_file, [sys.executable, "-c", "pass"]),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert contender.returncode == GPU_LEASE_BUSY_EXIT_CODE
    finally:
        holder.terminate()
        holder.wait(timeout=5)

    successor = subprocess.run(
        _lease_command(lock_file, [sys.executable, "-c", "pass"]),
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert successor.returncode == 0


def test_lease_refuses_a_symlink_target(tmp_path: Path) -> None:
    target = tmp_path / "target.lock"
    target.touch()
    link = tmp_path / "gpu.lock"
    link.symlink_to(target)

    with pytest.raises(RuntimeError, match="opened safely"):
        acquire_gpu_lease(link)


def test_required_worker_token_fails_before_exec(tmp_path: Path) -> None:
    marker = tmp_path / "worker-imported"
    environ = {
        **os.environ,
        "FRISKET_GPU_WORKER_REQUIRE_TOKEN": "1",
        "FRISKET_GPU_WORKER_TOKEN": "",
    }
    result = subprocess.run(
        _lease_command(
            tmp_path / "gpu.lock",
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).touch()",
            ],
        ),
        env=environ,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 78
    assert "must be a non-empty bearer token" in result.stderr
    assert not marker.exists()
