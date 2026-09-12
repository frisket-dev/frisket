"""Apply POSIX resource limits in a fresh interpreter before exec, never preexec_fn."""

import json
import os
import signal
import sys


def main() -> None:
    import resource

    cpu, memory_mb, open_files, command = json.loads(sys.argv[1])
    if any(type(value) is not int or value <= 0 for value in (cpu, memory_mb)):
        raise ValueError("resource limits must be positive integers")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(arg, str) for arg in command)
    ):
        raise ValueError("resource limits require a command")
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    if sys.platform != "darwin":
        memory = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    if open_files is not None:
        if type(open_files) is not int or open_files <= 0:
            raise ValueError("open file limit must be a positive integer")
        resource.setrlimit(resource.RLIMIT_NOFILE, (open_files, open_files))
    for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"):
        signum = getattr(signal, name, None)
        if signum is not None:
            signal.signal(signum, signal.SIG_DFL)
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        sys.stderr.write(f"FRISKET_SANDBOX_BOOTSTRAP_ERROR: {type(exc).__name__}\n")
        sys.stderr.flush()
        os._exit(127)
