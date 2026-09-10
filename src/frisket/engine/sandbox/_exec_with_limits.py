"""POSIX process-entry helper for Frisket sandbox resource limits.

``subprocess`` runs ``preexec_fn`` after ``fork()`` but before ``exec()``.
That is unsafe when the Frisket server has threads: a lock inherited by the
child can never be released there, while the parent waits for the child to
either exec or report its failure.  Keep resource setup in this short,
stdlib-only executable instead.  The parent can then spawn it safely and it
applies the limits immediately before replacing itself with the requested
program.
"""

from __future__ import annotations

import json
import os
import sys


_BOOTSTRAP_ERROR_PREFIX = "FRISKET_SANDBOX_BOOTSTRAP_ERROR: "


def _parse(payload: str) -> tuple[int, int, list[str]]:
    """Decode only the parent-produced, data-only bootstrap payload."""
    decoded = json.loads(payload)
    if not isinstance(decoded, list) or len(decoded) != 3:
        raise ValueError("expected limits and command payload")
    cpu_seconds, memory_mb, command = decoded
    if type(cpu_seconds) is not int or cpu_seconds <= 0:
        raise ValueError("CPU limit must be a positive integer")
    if type(memory_mb) is not int or memory_mb <= 0:
        raise ValueError("memory limit must be a positive integer")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(item, str) for item in command)
    ):
        raise ValueError("command must be a non-empty list of strings")
    return cpu_seconds, memory_mb, command


def _restore_subprocess_default_signals() -> None:
    """Match ``subprocess``'s POSIX ``restore_signals=True`` before exec."""
    import signal

    for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"):
        signum = getattr(signal, name, None)
        if signum is not None:
            signal.signal(signum, signal.SIG_DFL)


def main(payload: str) -> None:
    """Apply encoded limits, then replace this helper with the target command."""
    cpu_seconds, memory_mb, command = _parse(payload)

    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    if sys.platform != "darwin":  # RLIMIT_AS is unreliable on macOS
        memory = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    _restore_subprocess_default_signals()
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("expected exactly one bootstrap payload")
        main(sys.argv[1])
    except BaseException as exc:
        sys.stderr.write(f"{_BOOTSTRAP_ERROR_PREFIX}{type(exc).__name__}: {exc}\n")
        sys.stderr.flush()
        os._exit(127)
