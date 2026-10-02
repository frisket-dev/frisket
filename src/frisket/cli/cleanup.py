"""Explicit offline reclamation for one local project bundle."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def cleanup(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="frisket cleanup",
        description=(
            "Reclaim unused local blob files. Stop Frisket, independent workers, "
            "MCP clients and any scripts using this workspace first. The launcher "
            "lock detects normal servers, not every possible reader or writer. "
            "Defaults to a dry run; this is not an online or hosted cleanup tool."
        ),
    )
    parser.add_argument("project", help="path to an existing .frisket project bundle")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="delete unused files (confirms all workspace users have been stopped)",
    )
    args = parser.parse_args(argv)
    path = Path(args.project).expanduser().resolve()
    if not path.is_dir() or not (path / "project.db").is_file():
        print(f"frisket cleanup: not a project bundle: {path}", file=sys.stderr)
        return 2

    from frisket.engine.store import Project
    from frisket.engine.store.project_blobs import reclaim_local_blobs
    from frisket.server.standalone import (
        StandaloneAlreadyRunning,
        StandaloneLifetimeLock,
    )

    try:
        # Same root as the local launcher/Desktop/standalone server. Independent
        # library clients remain the operator's offline-maintenance responsibility.
        with StandaloneLifetimeLock(path.parent):
            project = Project(path)
            try:
                summary = reclaim_local_blobs(project, dry_run=not args.apply)
            finally:
                project.close()
    except StandaloneAlreadyRunning:
        print(
            "frisket cleanup: stop Frisket and all workers/MCP clients using "
            "this workspace before cleanup",
            file=sys.stderr,
        )
        return 2
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"frisket cleanup: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0
