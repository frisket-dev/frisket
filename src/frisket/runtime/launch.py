"""Resolve app-owned workers without ambient PATH or child import assumptions."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
from typing import Any

from frisket.runtime._bootstrap import KINDS


@dataclass(frozen=True)
class PythonRuntime:
    executable: Path
    app_code_root: Path

    @classmethod
    def current(cls) -> PythonRuntime:
        # Resolving this symlink loses the venv: /venv/bin/python often points
        # at the shared base interpreter, whose site-packages are different.
        return cls(
            Path(os.path.abspath(sys.executable)), Path(__file__).resolve().parents[2]
        )

    def bootstrap(self) -> Path:
        if (
            not self.executable.is_absolute()
            or not self.executable.is_file()
            or not os.access(self.executable, os.X_OK)
        ):
            raise ValueError(
                "worker interpreter must be an existing absolute executable"
            )
        root = self.app_code_root
        path = root / "frisket/runtime/_bootstrap.py"
        if not root.is_absolute() or not path.is_file():
            raise ValueError("worker app_code_root must contain the packaged bootstrap")
        return path.resolve()


def worker_argv(
    kind: str, *args: str, runtime: PythonRuntime | None = None
) -> list[str]:
    if kind not in KINDS:
        raise ValueError(f"unknown worker kind: {kind}")
    selected = runtime or PythonRuntime.current()
    bootstrap = selected.bootstrap()
    return [str(selected.executable), "-I", str(bootstrap), kind, *args]


def is_worker_argv(argv: list[str]) -> bool:
    return (
        len(argv) >= 4
        and Path(argv[0]) == PythonRuntime.current().executable
        and argv[1] == "-I"
        and Path(argv[2]).resolve()
        == Path(__file__).with_name("_bootstrap.py").resolve()
        and argv[3] in KINDS
    )


def with_policy(argv: list[str], policy: dict[str, Any]) -> list[str]:
    if not is_worker_argv(argv):
        raise ValueError("worker policy requires a managed Python entrypoint")
    encoded = json.dumps(policy, separators=(",", ":"), allow_nan=False)
    return [*argv[:3], "--policy", encoded, *argv[3:]]


def limited_argv(
    argv: list[str], *, cpu_seconds: int, memory_mb: int, open_files: int | None = None
) -> list[str]:
    if os.name == "nt":
        return argv
    runtime = PythonRuntime.current()
    return [
        str(runtime.executable),
        "-I",
        str(Path(__file__).with_name("_limits.py").resolve()),
        json.dumps([cpu_seconds, memory_mb, open_files, argv], separators=(",", ":")),
    ]
