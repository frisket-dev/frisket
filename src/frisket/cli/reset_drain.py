"""CLI wrapper for the machine-readable PC-9 reset-drain preflight."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


class _MachineReadableParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise ValueError("invalid reset-drain arguments")


def reset_drain(argv: list[str]) -> int:
    parser = _MachineReadableParser(
        prog="frisket reset-drain",
        description=("read-only stop-the-world reset preflight; emits one JSON report"),
    )
    parser.add_argument(
        "--queue",
        default=None,
        help=(
            "run-queue database URL or SQLite path "
            "(default: FRISKET_RUN_QUEUE_DATABASE_URL)"
        ),
    )
    parser.add_argument(
        "--projects-root",
        default=None,
        help="bundle root (default: FRISKET_PROJECTS_ROOT)",
    )
    try:
        args = parser.parse_args(argv)
    except ValueError:
        from frisket.operability.reset_drain import configuration_error_report

        print(
            json.dumps(
                configuration_error_report("invalid_arguments"),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2

    queue_locator = args.queue or os.environ.get("FRISKET_RUN_QUEUE_DATABASE_URL")
    projects_root = args.projects_root or os.environ.get("FRISKET_PROJECTS_ROOT")

    from frisket.operability.reset_drain import (
        configuration_error_report,
        inspect_base_reset_drain,
    )

    missing: list[str] = []
    if not queue_locator:
        missing.append("run_queue_locator_required")
    if not projects_root:
        missing.append("projects_root_required")
    if missing:
        report = configuration_error_report(*missing)
    else:
        report = inspect_base_reset_drain(
            queue_locator=queue_locator,
            projects_root=Path(projects_root),
        )
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    if report["errors"]:
        return 2
    return 0 if report["ready"] else 1
