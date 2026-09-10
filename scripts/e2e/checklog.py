#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_LOG = ROOT / ".frisket" / "check-runs.jsonl"


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def log_path() -> Path:
    raw = os.environ.get("FRISKET_CHECKLOG_PATH")
    return Path(raw).expanduser() if raw else DEFAULT_LOG


def disabled() -> bool:
    return os.environ.get("FRISKET_CHECKLOG_DISABLE") == "1"


def append_record(record: dict[str, Any]) -> None:
    if disabled():
        return
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    except OSError as exc:
        print(f"[checklog] unable to write {path}: {exc}", file=sys.stderr)


def build_record(
    *,
    kind: str,
    label: str,
    command: list[str] | str,
    cwd: Path,
    started_at: str,
    finished_at: str,
    elapsed_seconds: float,
    returncode: int,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source": "checklog",
        "kind": kind,
        "label": label,
        "command": command,
        "cwd": str(cwd),
        "outcome": "PASS" if returncode == 0 else "FAIL",
        "returncode": returncode,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "started_at": started_at,
        "finished_at": finished_at,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--kind", default="shell", help="check kind for timeline grouping"
    )
    parser.add_argument("--label", default="", help="short label shown in timeline")
    parser.add_argument("--cwd", type=Path, default=None, help="working directory")
    parser.add_argument(
        "--shell", metavar="COMMAND", help="run COMMAND through the shell"
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    cwd = (args.cwd or Path.cwd()).resolve()
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]

    if args.shell:
        if command:
            parser.error("pass either --shell COMMAND or a command after --, not both")
        run_args: list[str] | str = args.shell
        shell = True
        display_command: list[str] | str = args.shell
        label = args.label or args.shell
    else:
        if not command:
            parser.error("missing command; use -- COMMAND or --shell COMMAND")
        run_args = command
        shell = False
        display_command = command
        label = args.label or shlex.join(command)

    started_at = iso_now()
    started = time.monotonic()
    try:
        proc = subprocess.run(run_args, cwd=cwd, shell=shell, check=False)
        returncode = proc.returncode
    except KeyboardInterrupt:
        returncode = 130
    finally:
        finished_at = iso_now()
        elapsed = time.monotonic() - started
        append_record(
            build_record(
                kind=args.kind,
                label=label,
                command=display_command,
                cwd=cwd,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=elapsed,
                returncode=locals().get("returncode", 1),
            )
        )
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
