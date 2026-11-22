"""Offline, resumable removal of selected material and its recovery copies.

IDs remain tombstones; independent derived values are never traversed/deleted.
The caller excludes all other bundle users for the whole maintenance operation.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from itertools import islice
from typing import Any
from frisket.engine.store.extraction_cache import delete_extraction_cache_for_blobs
from frisket.engine.store.transactions import inserted_rowid, write_transaction

PURGE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS project_purges (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 phase TEXT NOT NULL DEFAULT 'content' CHECK(phase IN ('content','complete')),
 created_at TEXT NOT NULL DEFAULT(datetime('now'))
);
CREATE TABLE IF NOT EXISTS project_purge_targets (
 kind TEXT NOT NULL CHECK(kind IN ('sheet','column','row','change','run','artifact','alert_check','text_surface','qa_thread')),
 object_id INTEGER NOT NULL,
 purge_id INTEGER NOT NULL REFERENCES project_purges(id),
 PRIMARY KEY(purge_id,kind,object_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS project_purge_imports (
 purge_id INTEGER NOT NULL REFERENCES project_purges(id),
 session_id TEXT NOT NULL,
 PRIMARY KEY(purge_id,session_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS project_purge_blobs (
 purge_id INTEGER NOT NULL REFERENCES project_purges(id),
 hash TEXT NOT NULL,
 PRIMARY KEY(purge_id,hash)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_project_purge_targets_object ON project_purge_targets(kind,object_id);
"""


class PurgedMaterialUnavailable(ValueError):
    """Required target or recovery material has been permanently removed."""


def require_no_pending_purge(db: sqlite3.Connection) -> None:
    if db.execute(
        "SELECT 1 FROM project_purges WHERE phase!='complete' LIMIT 1"
    ).fetchone():
        raise PurgedMaterialUnavailable(
            "Permanent cleanup is unfinished. Stop Frisket and resume with "
            "frisket cleanup <project.frisket> --apply."
        )


def _require_maintenance(project: Any) -> None:
    if not bool(getattr(project, "maintenance", False)):
        raise ValueError(
            "Permanent cleanup requires an offline maintenance Project; "
            "reopen with maintenance=True after stopping all bundle users"
        )


def require_restore_available(db, *, sheet_ids=(), column_ids=(), row_ids=()):
    for kind, ids in (
        ("sheet", sheet_ids),
        ("column", column_ids),
        ("row", row_ids),
    ):
        remaining = iter(ids)
        while batch := list(islice(remaining, 500)):
            found = db.execute(
                "SELECT object_id FROM project_purge_targets WHERE kind=? AND "
                f"object_id IN ({','.join('?' for _ in batch)}) LIMIT 1",
                (kind, *batch),
            ).fetchone()
            if found:
                raise PurgedMaterialUnavailable(
                    f"Cannot restore permanently purged {kind} {found[0]}"
                )


def require_publication_targets(db, *, sheet_id=None, column_ids=(), row_ids=()):
    require_restore_available(
        db,
        sheet_ids=() if sheet_id is None else (sheet_id,),
        column_ids=column_ids,
        row_ids=row_ids,
    )


def _targets(kind: str) -> str:
    return f"SELECT object_id FROM project_purge_targets WHERE kind='{kind}' AND purge_id=:purge"


def _scope(*, sheet=None, column=None, row=None) -> str:
    return (
        "("
        + " OR ".join(
            f"{field} IN ({_targets(kind)})"
            for kind, field in (("sheet", sheet), ("column", column), ("row", row))
            if field
        )
        + ")"
    )


def begin_purge(
    project: Any,
    *,
    sheet_ids: Iterable[int] = (),
    column_ids: Iterable[int] = (),
    row_ids: Iterable[int] = (),
) -> int:
    _require_maintenance(project)
    db = project.db
    if db.in_transaction:
        raise ValueError("Purge owns its transaction")
    if project.retention_policy()["no_compact"]:
        raise ValueError("Project retention policy disables permanent cleanup")
    from .blob_backend import FilesystemProjectBlobStore

    if (
        not isinstance(project.blob_store, FilesystemProjectBlobStore)
        or project.blob_store.root.absolute() != (project.path / "blobs").absolute()
        or (project.path / "blobs").is_symlink()
    ):
        raise ValueError("Selected purge currently requires offline local maintenance")
    choices = {
        "sheet": tuple(sheet_ids),
        "column": tuple(column_ids),
        "row": tuple(row_ids),
    }
    if not any(choices.values()):
        raise ValueError("Select at least one sheet, column or row")
    with write_transaction(db):
        require_no_pending_purge(db)
        purge = inserted_rowid(db.execute("INSERT INTO project_purges DEFAULT VALUES"))
        p = {"purge": purge}
        for kind, ids in choices.items():
            for identity in ids:
                if (
                    isinstance(identity, bool)
                    or not isinstance(identity, int)
                    or identity <= 0
                ):
                    raise ValueError("Purge targets must be positive integer IDs")
                if not db.execute(
                    f"SELECT 1 FROM {kind}s WHERE id=?", (identity,)
                ).fetchone():
                    raise ValueError(f"Unknown {kind} {identity}")
                db.execute(
                    "INSERT OR IGNORE INTO project_purge_targets VALUES (?,?,?)",
                    (kind, identity, purge),
                )
        for kind in ("row", "column"):
            db.execute(
                f"INSERT OR IGNORE INTO project_purge_targets SELECT '{kind}',id,:purge FROM {kind}s WHERE sheet_id IN ({_targets('sheet')})",
                p,
            )
        db.execute(
            f"INSERT INTO project_purge_imports SELECT :purge,id FROM import_sessions WHERE sheet_id IN ({_targets('sheet')}) OR sheet_id IN (SELECT sheet_id FROM columns WHERE id IN ({_targets('column')})) OR id IN (SELECT session_id FROM import_session_rows WHERE row_id IN ({_targets('row')})) OR id IN (SELECT session_id FROM import_session_pdf_sources WHERE {_scope(sheet='sheet_id', row='row_id', column='column_id')})",
            p,
        )
        if db.execute(
            "SELECT 1 FROM import_sessions s JOIN project_purge_imports p ON p.session_id=s.id WHERE p.purge_id=? AND s.state IN ('active','paused','cancelled')",
            (purge,),
        ).fetchone():
            raise ValueError(
                "Finish or cancel and keep the import before permanently removing its content"
            )
        from .project_qa_retention import remember_qa_purge_targets

        remember_qa_purge_targets(db, purge)

        def remember(kind, query):
            db.execute(
                f"INSERT OR IGNORE INTO project_purge_targets SELECT '{kind}',id,:purge FROM ({query}) WHERE id IS NOT NULL",
                p,
            )

        cells = _scope(column="column_id", row="row_id")
        remember(
            "run",
            f"SELECT run_id id FROM results WHERE {cells} UNION SELECT run_id FROM run_rows WHERE row_id IN ({_targets('row')}) UNION SELECT run_id FROM run_output_generations WHERE column_id IN ({_targets('column')}) UNION SELECT id FROM runs WHERE sheet_id IN ({_targets('sheet')})",
        )
        remember(
            "run",
            f"SELECT b.run_id id FROM consumed_source_bundles b JOIN consumed_source_columns d ON d.bundle_id=b.id JOIN columns c ON c.id=d.column_id WHERE d.column_id IN ({_targets('column')}) OR c.sheet_id IN ({_targets('sheet')}) OR (b.kind='row' AND json_extract(b.capture_json,'$.row_id') IN ({_targets('row')})) OR (b.kind='aggregate' AND c.sheet_id IN (SELECT sheet_id FROM rows WHERE id IN ({_targets('row')})))",
        )
        row_capture_scope = _scope(
            sheet="json_extract(capture_json,'$.sheet_id')",
            row="json_extract(capture_json,'$.row_id')",
        )
        remember(
            "run",
            f"SELECT run_id id FROM consumed_source_bundles WHERE (kind='row' AND ({row_capture_scope})) OR (kind='aggregate' AND EXISTS (SELECT 1 FROM json_each(capture_json,'$.scopes') scope WHERE json_extract(scope.value,'$.sheet_id') IN ({_targets('sheet')}) OR json_extract(scope.value,'$.sheet_id') IN (SELECT sheet_id FROM rows WHERE id IN ({_targets('row')}))))",
        )
        if db.execute(
            f"SELECT 1 FROM runs WHERE status='running' AND id IN ({_targets('run')}) LIMIT 1",
            p,
        ).fetchone():
            raise ValueError(
                "Cancel or finish affected work before permanent cleanup, then stop Frisket and retry"
            )
        if db.execute(
            f"SELECT 1 FROM effect_checkpoints WHERE ({_effect_scope()}) AND (state='reserved' OR (state='returned' AND accounting_persisted=0)) LIMIT 1",
            p,
        ).fetchone():
            raise ValueError(
                "Resolve affected provider operations before permanent cleanup; unrecorded usage must not be lost"
            )
        remember(
            "change",
            f"SELECT op_id id FROM copied_result_outcomes WHERE {cells} UNION SELECT op_id FROM runs WHERE id IN ({_targets('run')}) UNION SELECT op_id FROM edits WHERE {cells} UNION SELECT p.op_id FROM cells c JOIN base_cell_producers p ON p.id=c.producer_id WHERE {_scope(column='c.column_id', row='c.row_id')}",
        )
        remember(
            "change",
            "SELECT op_id id FROM structural_changes effect WHERE EXISTS (SELECT 1 "
            "FROM project_purge_targets target WHERE target.purge_id=:purge "
            "AND target.kind=effect.kind AND target.object_id=effect.object_id)",
        )
        remember(
            "change",
            f"SELECT op_id id FROM column_descriptor_changes WHERE column_id IN ({_targets('column')})",
        )
        remember(
            "change",
            f"SELECT op_id id FROM import_sessions WHERE id IN (SELECT session_id FROM project_purge_imports WHERE purge_id=:purge) UNION SELECT parent_op_id FROM sheets WHERE id IN ({_targets('sheet')})",
        )
        for kind, key in (
            ("sheet", "created_sheets"),
            ("column", "created_columns"),
            ("row", "created_rows"),
            ("sheet", "deleted_sheets"),
            ("row", "deleted_rows"),
            ("row", "restored_rows"),
            ("column", "deleted_columns"),
        ):
            remember(
                "change",
                f"SELECT o.id FROM ops o JOIN json_each(o.undo_info,'$.{key}') target WHERE target.value IN ({_targets(kind)})",
            )
        for kind, key in (
            ("sheet", "sheet_names"),
            ("column", "column_names"),
            ("column", "column_types"),
            ("column", "column_formats"),
            ("column", "column_semantic_types"),
        ):
            remember(
                "change",
                f"SELECT o.id FROM ops o JOIN json_each(o.undo_info,'$.{key}') target WHERE CAST(target.key AS INTEGER) IN ({_targets(kind)})",
            )
        remember(
            "artifact",
            f"SELECT id FROM source_artifacts WHERE {_scope(sheet='source_sheet_id', column='source_column_id', row='source_row_id')}",
        )
        _remember_source_blobs(db, p)
        remember(
            "artifact",
            "SELECT id FROM source_artifacts WHERE blob_hash IN "
            "(SELECT hash FROM project_purge_blobs WHERE purge_id=:purge)",
        )
        remember(
            "text_surface",
            f"SELECT id FROM text_surfaces WHERE {_scope(sheet='text_sheet_id', column='text_column_id', row='text_row_id')} OR {_scope(sheet="json_extract(surface_ref_json,'$.identity.sheet_id')", row="json_extract(surface_ref_json,'$.identity.row_id')")}",
        )
        remember(
            "alert_check",
            f"SELECT run_id id FROM alert_check_hits WHERE {_scope(sheet='sheet_id', column='column_id', row='row_id')} UNION SELECT run_id FROM alert_check_events WHERE {_scope(sheet="json_extract(subject_ref,'$.sheet_id')", column="json_extract(subject_ref,'$.column_id')", row="json_extract(subject_ref,'$.row_id')")}",
        )
        # Offline destructive maintenance also stops affected producers. Keep
        # completed contributions applied; never turn purge into a cascade.
        from .changes import ChangeStore

        changes = ChangeStore(project)
        for operation in db.execute(
            f"SELECT id FROM ops WHERE publication_closed=0 AND status='applied' AND id IN ({_targets('change')})",
            p,
        ):
            changes.close(int(operation[0]))
        db.execute(f"UPDATE rows SET hidden=1 WHERE id IN ({_targets('row')})", p)
        db.execute(
            f"UPDATE columns SET active=0,hidden=1 WHERE id IN ({_targets('column')})",
            p,
        )
        db.execute(f"UPDATE sheets SET hidden=1 WHERE id IN ({_targets('sheet')})", p)
        return purge


def _effect_scope():
    return f"(family='row_effect' AND CAST(group_key AS INTEGER) IN ({_targets('run')})) OR authorized_attempt_id IN (SELECT id FROM execution_attempts WHERE run_id IN ({_targets('run')}))"


def _delete_batches(db, table, predicate, parameters, *, batch_size, project=None):
    # Explicit per-table primary keys also support WITHOUT ROWID tables.
    keys = [
        r[1]
        for r in sorted(db.execute(f"PRAGMA table_info({table})"), key=lambda r: r[5])
        if r[5]
    ]
    key = "(" + ",".join(keys) + ")" if len(keys) > 1 else keys[0] if keys else "rowid"
    while True:
        with write_transaction(db):
            trigger = None
            if table == "run_output_generations":
                _require_maintenance(project)
                trigger = db.execute(
                    "SELECT sql FROM sqlite_schema WHERE name='trg_run_output_generations_delete_discarded_only'"
                ).fetchone()
                if trigger:
                    db.execute(
                        "DROP TRIGGER trg_run_output_generations_delete_discarded_only"
                    )
            if table == "results":
                result_rows = db.execute(
                    "SELECT run_id,row_id,column_id FROM results WHERE "
                    f"{predicate} LIMIT :batch",
                    {**parameters, "batch": batch_size},
                ).fetchall()
                with _authorize_purge_result_deletes(
                    project,
                    (
                        (int(row["run_id"]), int(row["row_id"]), int(row["column_id"]))
                        for row in result_rows
                    ),
                ):
                    changed = sum(
                        db.execute(
                            "DELETE FROM results WHERE run_id=? AND row_id=? AND column_id=?",
                            (row["run_id"], row["row_id"], row["column_id"]),
                        ).rowcount
                        for row in result_rows
                    )
            else:
                changed = db.execute(
                    f"DELETE FROM {table} WHERE {key} IN (SELECT {','.join(keys) if keys else 'rowid'} FROM {table} WHERE {predicate} LIMIT :batch)",
                    {**parameters, "batch": batch_size},
                ).rowcount
            if trigger:
                db.execute(trigger[0])
        if changed < batch_size:
            break


@contextmanager
def _authorize_purge_result_deletes(project, keys):
    """Authorize only selected result keys inside one maintenance transaction."""
    if not bool(getattr(project, "maintenance", False)):
        raise ValueError("published result deletion requires offline maintenance")
    db = project.db
    if not db.in_transaction:
        raise RuntimeError("published result deletion requires a purge transaction")
    if not db.execute(
        "SELECT 1 FROM project_purges WHERE phase='content' LIMIT 1"
    ).fetchone():
        raise RuntimeError("published result deletion requires an active purge")
    if getattr(project._purge_result_delete_scope, "keys", None) is not None:
        raise RuntimeError("published result deletion authorization cannot be nested")
    project._purge_result_delete_scope.keys = frozenset(keys)
    try:
        yield
    finally:
        del project._purge_result_delete_scope.keys


def finish_purge(project: Any, purge_id: int, *, batch_size: int = 1000) -> dict:
    """Idempotently finish one obligation under the caller's offline boundary."""
    _require_maintenance(project)
    if not 1 <= batch_size <= 10000:
        raise ValueError("batch_size must be between 1 and 10000")
    db = project.db
    if db.in_transaction:
        raise ValueError("Purge owns its transactions")
    p = {"purge": purge_id}
    state = db.execute(
        "SELECT phase FROM project_purges WHERE id=?", (purge_id,)
    ).fetchone()
    if state is None:
        raise ValueError("Unknown purge")
    if state[0] == "complete":
        return {"purge_id": purge_id, "phase": "complete"}
    _scrub_content(project, p, batch_size)
    _purge_files(project, p)
    erased = _erase_source_blobs(project, p)
    from .project_blobs import reclaim_local_blobs

    summary = reclaim_local_blobs(project, dry_run=False)
    summary["blobs_removed"] += erased["blobs_removed"]
    summary["bytes_freed"] += erased["bytes_freed"]
    with db:
        db.execute("UPDATE project_purges SET phase='complete' WHERE id=:purge", p)
        db.execute(
            "DELETE FROM project_purge_targets WHERE purge_id=:purge AND kind NOT IN ('sheet','column','row')",
            p,
        )
        db.execute("DELETE FROM project_purge_imports WHERE purge_id=:purge", p)
        db.execute("DELETE FROM project_purge_blobs WHERE purge_id=:purge", p)
    return {**summary, "purge_id": purge_id, "phase": "complete"}


def _scrub_content(project, p, batch_size):
    db = project.db
    from .project_qa_retention import purge_qa_content

    while True:
        with write_transaction(db):
            changed = purge_qa_content(db, p["purge"], limit=batch_size)
        if not changed:
            break
    cell = _scope(column="column_id", row="row_id")
    scope = _scope(sheet="sheet_id", column="column_id", row="row_id")
    citation_scope = _scope(
        sheet="json_extract(locator_json,'$.sheet_id')",
        column="json_extract(locator_json,'$.column_id')",
        row="json_extract(locator_json,'$.row_id')",
    )
    _scrub_history(db, p, batch_size)
    deletions = (
        (
            "structural_changes",
            "EXISTS (SELECT 1 FROM project_purge_targets target "
            "WHERE target.purge_id=:purge AND target.kind=structural_changes.kind "
            "AND target.object_id=structural_changes.object_id)",
        ),
        ("result_judgment_changes", _judgment_scope()),
        (
            "runtime_projection_artifacts",
            _scope(
                sheet="json_extract(target_json,'$.sheet_id')",
                column="json_extract(target_json,'$.column_id')",
                row="json_extract(target_json,'$.row_id')",
            ),
        ),
        ("document_text_manual_pins", _scope(column="text_column_id", row="row_id")),
        ("current_cells", cell),
        ("cell_baseline", cell),
        ("cell_result_heads", cell),
        ("run_output_generations", _scope(column="column_id")),
        ("copied_result_outcomes", cell),
        (
            "results",
            cell
            + " AND NOT EXISTS (SELECT 1 FROM copied_result_outcomes copy WHERE copy.source_run_id=results.run_id AND copy.source_row_id=results.row_id AND copy.source_column_id=results.column_id)",
        ),
        ("edits", cell),
        ("cells", cell),
        ("run_rows", _scope(row="row_id")),
        ("keyed_observations", _scope(row="row_id")),
        ("keyed_relationships", _scope(sheet="sheet_id")),
        ("import_session_rows", _scope(row="row_id")),
        ("import_session_pdf_sources", scope),
        ("replay_edit_dismissals", cell),
        ("column_descriptor_changes", _scope(column="column_id")),
        ("citations", scope),
        ("alert_row_field_state", scope),
        ("alert_seen_rows", _scope(sheet="sheet_id", row="row_id")),
        (
            "alert_check_hits",
            _scope(sheet="sheet_id", column="column_id", row="row_id"),
        ),
        (
            "alert_check_events",
            _scope(
                sheet="json_extract(subject_ref,'$.sheet_id')",
                column="json_extract(subject_ref,'$.column_id')",
                row="json_extract(subject_ref,'$.row_id')",
            ),
        ),
        (
            "notification_items",
            f"(source_kind='alert' AND json_extract(source_ref,'$.run_id') IN ({_targets('alert_check')})) OR EXISTS (SELECT 1 FROM json_each(source_event_ids) e JOIN alert_check_events w ON w.id=e.value WHERE w.run_id IN ({_targets('alert_check')}))",
        ),
    )
    for table, predicate in deletions:
        if table == "copied_result_outcomes":
            _delete_copied_results(db, p, project=project, batch_size=batch_size)
        else:
            _delete_batches(
                db, table, predicate, p, batch_size=batch_size, project=project
            )
    from .keyed_updates import purge_baseline_columns

    cursor = 0
    while True:
        ids = [
            r[0]
            for r in db.execute(
                "SELECT object_id FROM project_purge_targets WHERE purge_id=:purge AND kind='column' AND object_id>:cursor ORDER BY object_id LIMIT 100",
                {**p, "cursor": cursor},
            )
        ]
        if not ids:
            break
        with write_transaction(db):
            purge_baseline_columns(db, ids)
        cursor = ids[-1]
    _purge_captures(db, p, batch_size)
    _purge_text_surface_payloads(db, p, batch_size)
    _purge_citation_snapshots(db, p, batch_size)
    # Historical identity remains, while data-bearing descriptors disappear.
    updates = (
        ("effect_checkpoints", "payload='{}'", _effect_scope()),
        (
            "project_qa_citations",
            "label='Unavailable source',excerpt=NULL,metadata_json='{}'",
            citation_scope,
        ),
        ("runs", "params='{}',work_progress=NULL", f"id IN ({_targets('run')})"),
        (
            "source_spans",
            "quote=NULL,snippet=NULL,preview_json='{}',metadata='{}',selector_json='{}'",
            f"artifact_id IN ({_targets('artifact')}) OR text_surface_id IN ({_targets('text_surface')})",
        ),
        (
            "source_artifacts",
            "blob_hash=NULL,source_url=NULL,canonical_url=NULL,title=NULL,filename=NULL,external_ref_json='{}',metadata='{}'",
            f"id IN ({_targets('artifact')})",
        ),
        ("columns", "name='purged_'||id,format=NULL", f"id IN ({_targets('column')})"),
        (
            "alert_checks",
            "resolved_query='{}',resolved_query_hash=NULL,detection_policy=NULL,error=NULL",
            f"id IN ({_targets('alert_check')})",
        ),
        (
            "sheets",
            "name='purged_'||id||'_'||lower(hex(randomblob(8)))",
            f"id IN ({_targets('sheet')})",
        ),
        ("model_calls", "warnings='[]'", f"run_id IN ({_targets('run')})"),
    )
    for table, assignments, predicate in updates:
        _update_batches(db, table, assignments, predicate, p, batch_size=batch_size)
    _scrub_receipts(project, p, batch_size)


def _judgment_scope():
    tokenized = "'[\"' || replace(target,':','\",\"') || '\"]'"
    copy = f"json_extract({tokenized},'$[0]')='copy'"
    row = f"CAST(json_extract({tokenized},CASE WHEN {copy} THEN '$[2]' ELSE '$[1]' END) AS INTEGER)"
    column = f"CAST(json_extract({tokenized},CASE WHEN {copy} THEN '$[3]' ELSE '$[2]' END) AS INTEGER)"
    return _scope(row=row, column=column)


def _scrub_history(db, p, batch_size):
    """Erase selected effects, retaining each surviving object's undo data."""
    cursor = 0
    while operations := db.execute(
        f"SELECT id,undo_info FROM ops WHERE id>:cursor AND (id IN ({_targets('change')}) "
        f"OR id IN (SELECT op_id FROM result_judgment_changes WHERE {_judgment_scope()})) "
        "ORDER BY id LIMIT :batch",
        {**p, "cursor": cursor, "batch": batch_size},
    ).fetchall():
        with db:
            for op_id, encoded in operations:
                info = json.loads(encoded or "{}")
                for field, value in list(info.items()):
                    kind = next(
                        (
                            kind
                            for kind in ("sheet", "column", "row")
                            if field.startswith(kind + "_")
                            or field.endswith(kind + "s")
                        ),
                        None,
                    )
                    if kind and isinstance(value, (dict, list)):
                        identity = "key" if isinstance(value, dict) else "value"
                        removed = {
                            str(item[0])
                            for item in db.execute(
                                f"SELECT {identity} FROM json_each(:values) WHERE CAST({identity} AS INTEGER) IN ({_targets(kind)})",
                                {**p, "values": json.dumps(value)},
                            )
                        }
                        info[field] = (
                            {
                                key: item
                                for key, item in value.items()
                                if key not in removed
                            }
                            if isinstance(value, dict)
                            else [item for item in value if str(item) not in removed]
                        )
                    if field in (
                        "review_states",
                        "review_states_after",
                        "review_metadata",
                        "review_metadata_after",
                    ) and isinstance(value, dict):
                        removed = {
                            item[0]
                            for item in db.execute(
                                f"SELECT target FROM result_judgment_changes WHERE op_id=:op AND ({_judgment_scope()})",
                                {**p, "op": op_id},
                            )
                        }
                        info[field] = {
                            key: item
                            for key, item in value.items()
                            if key not in removed
                        }
                db.execute(
                    "UPDATE ops SET spec='{}',label=NULL,undo_info=? WHERE id=?",
                    (json.dumps(info), op_id),
                )
        cursor = operations[-1][0]


def _scrub_receipts(project, p, batch_size):
    """Keep a valid, idempotently replayable refusal without old output data."""
    from frisket.contracts.action import ActionError, Receipt
    from .receipts import receipt_body

    predicate = (
        f"run_id IN ({_targets('run')}) OR id IN ("
        "SELECT receipt_id FROM import_sessions WHERE id IN ("
        "SELECT session_id FROM project_purge_imports WHERE purge_id=:purge))"
    )
    cursor = None
    while True:
        rows = project.db.execute(
            "SELECT id,run_id,action_kind,action_id,idempotency_key,params_hash,body "
            f"FROM receipts WHERE ({predicate}) AND (:cursor IS NULL OR id>:cursor) "
            "ORDER BY id LIMIT :batch",
            {**p, "cursor": cursor, "batch": batch_size},
        ).fetchall()
        if not rows:
            return
        with project.db:
            for row in rows:
                receipt = Receipt(
                    receipt_id=str(row["id"]),
                    project_id=project.storage_identity,
                    action_id=str(row["action_id"] or row["action_kind"]),
                    action_kind=str(row["action_kind"]),
                    run_id=(int(row["run_id"]) if row["run_id"] is not None else None),
                    idempotency_key=row["idempotency_key"],
                    params_hash=row["params_hash"],
                    provider_use=json.loads(row["body"]).get("provider_use", []),
                    status="failed",
                    errors=[
                        ActionError(
                            code="purged_material_unavailable",
                            message="This action's recovery material was permanently removed.",
                            action_kind=str(row["action_kind"]),
                        )
                    ],
                )
                project.db.execute(
                    "UPDATE receipts SET status='failed',body=?,edition_run_context=NULL WHERE id=?",
                    (receipt_body(receipt), str(row["id"])),
                )
        cursor = str(rows[-1]["id"])


def _update_batches(db, table, assignments, predicate, parameters, *, batch_size):
    cursor = None
    while True:
        ids = [
            r[0]
            for r in db.execute(
                f"SELECT id FROM {table} WHERE ({predicate}) AND (:cursor IS NULL OR id>:cursor) ORDER BY id LIMIT :batch",
                {**parameters, "cursor": cursor, "batch": batch_size},
            )
        ]
        if not ids:
            return
        with db:
            db.execute(
                f"UPDATE {table} SET {assignments} WHERE id IN ({','.join('?' for _ in ids)})",
                ids,
            )
        cursor = ids[-1]


def _remember_source_blobs(db, p):
    """Source values own originals; saved action results remain independent."""
    from .project_blobs import _value_blob_hashes
    from .value_codec import decode_stored_value
    from .blob_backend import validate_blob_digest

    def remember(hashes):
        db.executemany(
            "INSERT OR IGNORE INTO project_purge_blobs(purge_id,hash) VALUES (?,?)",
            ((p["purge"], validate_blob_digest(digest)) for digest in hashes),
        )

    for table in ("cells", "edits"):
        for kind, value in db.execute(
            f"SELECT value_kind,value FROM {table} WHERE {_scope(row='row_id', column='column_id')} "
            "AND value_kind IN ('json','legacy_invalid')",
            p,
        ):
            remember(_value_blob_hashes(decode_stored_value(kind, value)))
    for (digest,) in db.execute(
        f"SELECT blob_hash FROM source_artifacts WHERE id IN ({_targets('artifact')}) AND blob_hash IS NOT NULL",
        p,
    ):
        remember([digest])
    # Page images are owned previews. Derivation edges alone are provenance,
    # and do not authorize erasing independent clips or other action outputs.
    for (metadata,) in db.execute(
        f"SELECT metadata FROM source_artifacts WHERE id IN ({_targets('artifact')}) "
        "OR blob_hash IN (SELECT hash FROM project_purge_blobs WHERE purge_id=:purge)",
        p,
    ):
        images = json.loads(metadata or "{}").get("page_images", {})
        remember(
            image["blob_hash"] for image in images.values() if image.get("blob_hash")
        )


def _erase_source_blobs(project, p):
    """Erase selected bytes even when another row references the same hash."""
    from .blob_backend import validate_blob_digest, _fsync_directory

    root = project.path / "blobs"
    if root.is_symlink():
        raise ValueError("Refusing symlinked blob directory during purge")
    counts = {"blobs_removed": 0, "bytes_freed": 0}
    erased_hashes: list[str] = []
    for (digest,) in project.db.execute(
        "SELECT hash FROM project_purge_blobs WHERE purge_id=:purge",
        p,
    ):
        digest = validate_blob_digest(digest)
        path = root / digest[:2] / digest
        if path.parent.is_symlink() or path.is_symlink():
            raise ValueError("Refusing symlinked source blob during purge")
        if path.exists():
            size = path.stat().st_size
            path.unlink()
            _fsync_directory(path.parent)
            counts["blobs_removed"] += 1
            counts["bytes_freed"] += size
        with project.db:
            project.db.execute("DELETE FROM blobs WHERE hash=?", (digest,))
        erased_hashes.append(digest)
    if erased_hashes:
        delete_extraction_cache_for_blobs(project, erased_hashes)
    return counts


def _purge_files(project, p):
    from .blob_backend import _fsync_directory

    if (project.path / "traces").is_symlink():
        raise ValueError("Refusing symlinked trace directory during purge")
    for row in project.db.execute(
        "SELECT object_id FROM project_purge_targets WHERE kind='run' AND purge_id=:purge",
        p,
    ):
        path = project.path / "traces" / f"run-{int(row[0])}.jsonl.gz"
        if path.is_symlink():
            raise ValueError("Refusing symlinked trace during purge")
        path.unlink(missing_ok=True)
    traces = project.path / "traces"
    if traces.exists():
        _fsync_directory(traces)
    # Cache entries cannot be reliably attributed to coordinates. Remove only
    # disposable response caches; user values remain in the project database.
    for name in ("project.cache.db", "project.cache.db-wal", "project.cache.db-shm"):
        path = project.path / name
        if path.is_symlink():
            raise ValueError("Refusing symlinked cache during purge")
        path.unlink(missing_ok=True)
    _purge_retrieval(project, p)
    _purge_imports(project, p)
    _fsync_directory(project.path)


def _purge_retrieval(project, p):
    path = project.path / "project.retrieval.db"
    if not path.exists():
        return
    if path.is_symlink():
        raise ValueError("Refusing symlinked retrieval database")
    from frisket.retrieval.store import RetrievalStore

    with RetrievalStore(project.path) as store:
        db = store.db
        db.execute("ATTACH DATABASE ? AS authority", (str(project.db_path),))
        scope = _scope(sheet="sheet_id", row="row_id", column="column_id").replace(
            "FROM project_purge_targets", "FROM authority.project_purge_targets"
        )
        dependency_scope = _scope(
            row="dependency.row_id", column="dependency.column_id"
        ).replace("FROM project_purge_targets", "FROM authority.project_purge_targets")
        _delete_batches(
            db,
            "bindings",
            scope
            + " OR source_key IN (SELECT source_key FROM sources WHERE source_kind='blob' "
            "AND version_value IN (SELECT hash FROM authority.project_purge_blobs WHERE purge_id=:purge))"
            + f" OR EXISTS (SELECT 1 FROM binding_dependencies dependency WHERE dependency.binding_key=bindings.binding_key AND {dependency_scope})",
            p,
            batch_size=500,
        )
        while True:
            keys = [
                r[0]
                for r in db.execute(
                    "SELECT source_key FROM sources WHERE source_key NOT IN (SELECT source_key FROM bindings) LIMIT 500"
                )
            ]
            if not keys:
                break
            placeholders = ",".join("?" for _ in keys)
            with write_transaction(db):
                db.execute(
                    f"DELETE FROM lexical_fts WHERE rowid IN (SELECT rowid FROM lexical_sources WHERE source_key IN ({placeholders}))",
                    keys,
                )
                db.execute(
                    f"DELETE FROM preparation WHERE source_id IN (SELECT source_id FROM sources WHERE source_key IN ({placeholders})) AND source_id NOT IN (SELECT s.source_id FROM sources s JOIN bindings b ON b.source_key=s.source_key)",
                    keys,
                )
                db.execute(
                    f"DELETE FROM sources WHERE source_key IN ({placeholders})", keys
                )
        _delete_batches(
            db,
            "representations",
            "representation_key NOT IN (SELECT representation_key FROM part_representations UNION SELECT representation_key FROM named_dataset_membership)",
            {},
            batch_size=500,
        )
        db.execute("DETACH DATABASE authority")


def _purge_imports(project, p):
    import shutil
    from .import_intake import read_import_header, validate_import_ref

    root = project.path / ".imports"
    if root.is_symlink():
        raise ValueError("Refusing symlinked import root")
    for row in project.db.execute(
        "SELECT session_id FROM project_purge_imports WHERE purge_id=:purge", p
    ):
        directory = root / validate_import_ref(row[0])
        if directory.is_symlink():
            raise ValueError("Refusing symlinked import inventory")
        if not directory.exists():
            continue
        if read_import_header(directory).storage_identity != project.storage_identity:
            raise ValueError("Import inventory belongs to another project")
        shutil.rmtree(directory)


def _purge_citation_snapshots(db, p, batch_size):
    while hashes := db.execute(
        "SELECT content_hash FROM citation_texts WHERE content_hash IN ("
        f"SELECT json_extract(metadata,'$.captured_text_hash') FROM source_artifacts WHERE id IN ({_targets('artifact')})) LIMIT :batch",
        {**p, "batch": batch_size},
    ).fetchall():
        with write_transaction(db):
            trigger = db.execute(
                "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name='trg_citation_texts_no_delete'"
            ).fetchone()
            if trigger:
                db.execute("DROP TRIGGER trg_citation_texts_no_delete")
            db.executemany("DELETE FROM citation_texts WHERE content_hash=?", hashes)
            if trigger:
                db.execute(trigger[0])


def _purge_text_surface_payloads(db, p, batch_size):
    predicate = f"id IN ({_targets('text_surface')})"
    cursor = 0
    while True:
        ids = [
            r[0]
            for r in db.execute(
                f"SELECT id FROM text_surfaces WHERE ({predicate}) AND id>:cursor ORDER BY id LIMIT :batch",
                {**p, "cursor": cursor, "batch": batch_size},
            )
        ]
        if not ids:
            return
        with write_transaction(db):
            trigger = db.execute(
                "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name='trg_text_surfaces_no_update'"
            ).fetchone()
            if trigger:
                db.execute("DROP TRIGGER trg_text_surfaces_no_update")
            db.execute(
                f"UPDATE text_surfaces SET value_ref_json=NULL,surface_ref_json='{{}}' WHERE id IN ({','.join('?' for _ in ids)})",
                ids,
            )
            if trigger:
                db.execute(trigger[0])
        cursor = ids[-1]


def _delete_copied_results(db, p, *, project, batch_size):
    """Delete selected copies and only their newly orphaned purged sources."""
    while True:
        rows = db.execute(
            "SELECT op_id,row_id,column_id,source_run_id,source_row_id,source_column_id "
            "FROM copied_result_outcomes WHERE "
            f"row_id IN ({_targets('row')}) OR column_id IN ({_targets('column')}) "
            "ORDER BY op_id,row_id,column_id LIMIT :batch",
            {**p, "batch": batch_size},
        ).fetchall()
        if not rows:
            return
        with write_transaction(db):
            for row in rows:
                db.execute(
                    "DELETE FROM copied_result_outcomes WHERE op_id=? AND row_id=? AND column_id=?",
                    (row["op_id"], row["row_id"], row["column_id"]),
                )
                if row["source_run_id"] is not None:
                    source_key = (
                        int(row["source_run_id"]),
                        int(row["source_row_id"]),
                        int(row["source_column_id"]),
                    )
                    with _authorize_purge_result_deletes(project, [source_key]):
                        db.execute(
                            "DELETE FROM results WHERE run_id=? AND row_id=? AND column_id=? "
                            "AND NOT EXISTS (SELECT 1 FROM copied_result_outcomes copy "
                            "WHERE copy.source_run_id=results.run_id "
                            "AND copy.source_row_id=results.row_id "
                            "AND copy.source_column_id=results.column_id) "
                            "AND (EXISTS (SELECT 1 FROM project_purge_targets target "
                            "WHERE target.kind='row' AND target.object_id=results.row_id) "
                            "OR EXISTS (SELECT 1 FROM project_purge_targets target "
                            "WHERE target.kind='column' AND target.object_id=results.column_id))",
                            source_key,
                        )


def _purge_captures(db, p, batch_size):
    from .consumed_sources import purge_consumed_sources

    for kind, argument in (
        ("row", "row_ids"),
        ("column", "column_ids"),
        ("sheet", "sheet_ids"),
    ):
        cursor = 0
        while True:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT object_id FROM project_purge_targets WHERE purge_id=:purge AND kind=:kind AND object_id>:cursor ORDER BY object_id LIMIT 500",
                    {**p, "kind": kind, "cursor": cursor},
                )
            ]
            if not ids:
                break
            while True:
                with write_transaction(db):
                    runs, changed = purge_consumed_sources(
                        db, **{argument: ids}, limit=min(batch_size, 500)
                    )
                    db.executemany(
                        "INSERT OR IGNORE INTO project_purge_targets(kind,object_id,purge_id) VALUES ('run',?,?)",
                        ((run, p["purge"]) for run in runs),
                    )
                if changed == 0:
                    break
            cursor = ids[-1]
