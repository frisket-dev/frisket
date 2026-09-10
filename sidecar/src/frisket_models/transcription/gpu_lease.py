"""Host-shared, nonblocking admission for one resident GPU worker.

The container entrypoint locks before importing a model runtime, then preserves
the descriptor across ``exec`` for the worker's lifetime.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import os
import shlex
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

DEFAULT_GPU_LEASE_FILE = "/run/frisket-gpu/gpu-worker.lock"
GPU_LEASE_BUSY_EXIT_CODE = 75  # sysexits.h EX_TEMPFAIL
_COMMAND_ENV = "FRISKET_GPU_WORKER_COMMAND"
_LEASE_ENV = "FRISKET_GPU_LEASE_FILE"
_REQUIRE_TOKEN_ENV = "FRISKET_GPU_WORKER_REQUIRE_TOKEN"
_TOKEN_ENV = "FRISKET_GPU_WORKER_TOKEN"


class GpuLeaseUnavailable(RuntimeError):
    """The shared GPU lease is already held."""


@dataclass(slots=True)
class GpuLease:
    """Live advisory lease."""

    path: Path
    fd: int = field(repr=False)
    _released: bool = field(default=False, init=False, repr=False)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)

    def __enter__(self) -> GpuLease:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def acquire_gpu_lease(path: str | os.PathLike[str]) -> GpuLease:
    """Acquire a regular, non-symlink lock without waiting.

    Never unlink the lock: a handoff could otherwise lock different inodes
    under the same path.
    """

    lease_path = Path(path)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lease_path, flags, 0o600)
    except OSError as exc:
        raise RuntimeError("GPU lease file could not be opened safely") from exc

    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("GPU lease target must be a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise GpuLeaseUnavailable(
                    "GPU lease is already held; only one worker may be resident"
                ) from None
            raise RuntimeError("GPU lease could not be acquired") from exc

        # The lease must survive exec to cover the worker lifetime.
        os.set_inheritable(fd, True)
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(fd)
        return GpuLease(path=lease_path, fd=fd)
    except BaseException:
        os.close(fd)
        raise


def _command_from_args(
    raw_command: Sequence[str], environ: os._Environ[str] | dict[str, str]
) -> list[str]:
    command = list(raw_command)
    if command[:1] == ["--"]:
        command = command[1:]
    if command:
        return command

    configured = environ.get(_COMMAND_ENV, "")
    if configured:
        try:
            command = shlex.split(configured)
        except ValueError as exc:
            raise ValueError(f"{_COMMAND_ENV} is not valid shell-style argv") from exc
    if not command:
        raise ValueError(
            f"pass a worker command after -- or set non-empty {_COMMAND_ENV}"
        )
    return command


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Acquire the shared GPU lease, then exec one worker",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--lock-file",
        default=os.environ.get(_LEASE_ENV, DEFAULT_GPU_LEASE_FILE),
        help=f"shared lease path (default: ${_LEASE_ENV} or {DEFAULT_GPU_LEASE_FILE})",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    if os.environ.get(_REQUIRE_TOKEN_ENV) == "1":
        token = os.environ.get(_TOKEN_ENV, "")
        if (
            not token
            or token != token.strip()
            or any(
                ord(character) < 0x21 or ord(character) > 0x7E for character in token
            )
        ):
            print(
                f"gpu-lease: {_TOKEN_ENV} must be a non-empty bearer token",
                file=sys.stderr,
            )
            return 78

    try:
        command = _command_from_args(args.command, os.environ)
    except ValueError as exc:
        print(f"gpu-lease: {exc}", file=sys.stderr)
        return 64

    try:
        lease = acquire_gpu_lease(args.lock_file)
    except GpuLeaseUnavailable as exc:
        print(f"gpu-lease: {exc}", file=sys.stderr)
        return GPU_LEASE_BUSY_EXIT_CODE
    except RuntimeError as exc:
        print(f"gpu-lease: {exc}", file=sys.stderr)
        return 74

    try:
        os.execvp(command[0], command)
    except OSError:
        # Full argv may contain credentials.
        print(f"gpu-lease: could not exec {command[0]!r}", file=sys.stderr)
        return 69
    finally:
        lease.release()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DEFAULT_GPU_LEASE_FILE",
    "GPU_LEASE_BUSY_EXIT_CODE",
    "GpuLease",
    "GpuLeaseUnavailable",
    "acquire_gpu_lease",
    "main",
]
