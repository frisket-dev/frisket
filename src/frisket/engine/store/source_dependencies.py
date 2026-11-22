"""Admitted source identities fence direct consumers when objects disappear.

These identities are eligibility, not the values consumed or their freshness.
They live with the existing operation specification; no downstream cascade is
inferred from historical lineage or citations.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

SOURCE_BINDING = "execution_sources"


class ExecutionSourceUnavailable(ValueError):
    pass


def execution_source_binding(
    *, sheet_ids: Iterable[int] = (), column_ids: Iterable[int] = ()
) -> dict[str, list[int]]:
    return {
        "sheet_ids": sorted(set(map(int, sheet_ids))),
        "column_ids": sorted(set(map(int, column_ids))),
    }


def require_execution_sources(project: Any, op_id: int) -> None:
    row = project.db.execute(
        "SELECT spec FROM ops WHERE id=?", (int(op_id),)
    ).fetchone()
    if row is None:
        raise ExecutionSourceUnavailable("The execution no longer exists")
    spec = json.loads(row[0])
    if project.db.execute(
        "SELECT 1 FROM runs WHERE op_id=? AND cancel_requested_at IS NOT NULL LIMIT 1",
        (int(op_id),),
    ).fetchone():
        raise ExecutionSourceUnavailable(
            "The execution was cancelled; run the action again with available inputs"
        )
    from .import_sessions import ImportSessionConflict, require_import_execution_owner

    try:
        require_import_execution_owner(project.db, spec)
    except ImportSessionConflict as exc:
        raise ExecutionSourceUnavailable(str(exc)) from exc
    binding = spec.get(SOURCE_BINDING)
    if binding is None:
        return
    sheets = json.dumps(binding["sheet_ids"])
    columns = json.dumps(binding["column_ids"])
    missing = project.db.execute(
        "SELECT 1 FROM json_each(?) ids LEFT JOIN sheets s ON s.id=ids.value "
        "WHERE s.id IS NULL OR s.hidden=1 UNION ALL "
        "SELECT 1 FROM json_each(?) ids LEFT JOIN columns c ON c.id=ids.value "
        "LEFT JOIN sheets s ON s.id=c.sheet_id "
        "WHERE c.id IS NULL OR c.active=0 OR s.id IS NULL OR s.hidden=1 LIMIT 1",
        (sheets, columns),
    ).fetchone()
    if missing:
        raise ExecutionSourceUnavailable(
            "A required source sheet or column is unavailable"
        )


def stop_direct_consumers(
    project: Any, *, sheet_ids: Iterable[int] = (), column_ids: Iterable[int] = ()
) -> list[int]:
    """Close publication and request stopping in the deletion transaction.

    Dispatching attempts stay intact so a late provider return can retain its
    usage and saved response. Closed Changes cannot publish it into the sheet.
    """
    from .run_lifecycle import close_run_change
    from .runs import RunResultStore

    db = project.db
    if not db.in_transaction:
        raise RuntimeError("Source deletion and consumer fences must commit together")
    sheets, columns = set(map(int, sheet_ids)), set(map(int, column_ids))
    if not sheets and not columns:
        return []
    # Only live producers are examined; completed derivations remain independent.
    stopped = []
    for row in db.execute(
        "SELECT run.id,run.op_id,op.spec FROM runs run JOIN ops op ON op.id=run.op_id "
        "WHERE run.status='running' AND op.status='applied' AND (op.change_order IS NULL OR op.publication_closed=0)"
    ).fetchall():
        spec = json.loads(row["spec"])
        binding = spec.get(SOURCE_BINDING, {})
        if not (
            sheets.intersection(binding.get("sheet_ids", ()))
            or columns.intersection(binding.get("column_ids", ()))
        ):
            continue
        RunResultStore(project).request_cancel_in_transaction(int(row["id"]))
        close_run_change(project, int(row["id"]))
        stopped.append(int(row["id"]))
    # PDF page imports already persist their admitted row/column source IDs.
    # Their session cursor/token is the publication fence, including runless work.
    from .import_sessions import ImportSessionStore

    sessions = ImportSessionStore(project)
    for row in db.execute(
        "SELECT DISTINCT session.id FROM import_sessions session "
        "JOIN import_session_pdf_sources source ON source.session_id=session.id "
        "WHERE session.state IN ('active','paused') AND ("
        "source.sheet_id IN (SELECT value FROM json_each(?)) OR "
        "source.column_id IN (SELECT value FROM json_each(?)))",
        (json.dumps(sorted(sheets)), json.dumps(sorted(columns))),
    ).fetchall():
        session = sessions.get(str(row[0]))
        assert session is not None
        sessions.cancel_in_transaction(session)
    return stopped
