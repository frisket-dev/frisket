"""Small runtime primitives for the single-process Standalone server.

This module deliberately owns only process-local composition concerns.  The
team application still owns schema initialization and the queue still owns
worker heartbeats; keeping the lifetime lock here avoids turning either into a
deployment-topology abstraction.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path

from filelock import FileLock, Timeout

from frisket.server.workspace import ensure_writable_data_root


class StandaloneAlreadyRunning(RuntimeError):
    """The data root is already owned by another Standalone launcher."""


class StandaloneLifetimeLock:
    """An exclusive lock held for the complete Standalone process lifetime."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / ".standalone.lock"
        self._lock = FileLock(str(self.path), timeout=0)

    def acquire(self) -> None:
        # The root has to exist to contain its lock.  No application schema,
        # secrets, or worker state is initialized before this acquisition.
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # Probe before FileLock touches the root: a root-owned /data from a
        # pre-non-root image would otherwise die here as a bare PermissionError
        # traceback instead of the actionable DataRootUnwritable message.
        ensure_writable_data_root(self.data_dir)
        try:
            self._lock.acquire()
        except Timeout as exc:
            raise StandaloneAlreadyRunning(
                f"another frisket server already owns {self.data_dir}"
            ) from exc

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> "StandaloneLifetimeLock":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


@dataclass
class StandaloneRuntimeState:
    """Shared launcher state intentionally small enough for readiness to read."""

    worker_pid: int | None = None
    worker_exited_unexpectedly: bool = False
    _stopping: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    def begin_stopping(self) -> None:
        self._stopping.set()


__all__ = [
    "StandaloneAlreadyRunning",
    "StandaloneLifetimeLock",
    "StandaloneRuntimeState",
]
