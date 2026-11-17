"""Stdlib usage lease owned by a POSIX process guardian, not its app parent."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
import time

LOCK_NAME = ".runtime-use.lock"


@contextmanager
def runtime_use(
    root: Path | None,
    stopped: Callable[[], bool],
    *,
    exclusive: bool = False,
    require_ready: bool = True,
) -> Iterator[bool]:
    """Share an environment until its guarded target tree has stopped.

    Installation takes an exclusive lock on the same persistent file. Waiting
    users recheck readiness after repair, before starting their interpreter.
    Windows takes the equivalent file-sharing lease in its Job guardian.
    """
    if root is None:
        yield True
        return
    import fcntl

    with (root / LOCK_NAME).open("a+b") as lease:
        while not stopped():
            try:
                mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                fcntl.flock(lease.fileno(), mode | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.05)
        else:
            yield False
            return
        if stopped():
            yield False
            return
        if require_ready and not (root / ".ready").is_file():
            raise RuntimeError("optional model environment is not ready; retry setup")
        yield True
