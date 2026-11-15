"""Offline, resumable removal of selected material and its recovery copies.

IDs remain tombstones; independent derived values are never traversed/deleted.
The caller excludes all other bundle users for the whole maintenance operation.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from itertools import islice
from typing import Any

PURGE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS project_purges (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 phase TEXT NOT NULL DEFAULT 'content' CHECK(phase IN ('content','files','complete')),
 created_at TEXT NOT NULL DEFAULT(datetime('now'))
);
CREATE TABLE IF NOT EXISTS project_purge_targets (
 kind TEXT NOT NULL CHECK(kind IN ('sheet','column','row','change','run','artifact','watch_run','text_surface')),
 object_id INTEGER NOT NULL,
 purge_id INTEGER NOT NULL REFERENCES project_purges(id),
 PRIMARY KEY(purge_id,kind,object_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS project_purge_imports (
 purge_id INTEGER NOT NULL REFERENCES project_purges(id),
 session_id TEXT NOT NULL,
 PRIMARY KEY(purge_id,session_id)
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


def require_restore_available(
    db, *, sheet_ids=(), column_ids=(), row_ids=(), op_ids=()
):
    for kind, ids in (
        ("sheet", sheet_ids),
        ("column", column_ids),
        ("row", row_ids),
        ("change", op_ids),
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
    db.execute("BEGIN IMMEDIATE")
    try:
        require_no_pending_purge(db)
        purge = int(db.execute("INSERT INTO project_purges DEFAULT VALUES").lastrowid)
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
            f"INSERT INTO project_purge_imports SELECT :purge,id FROM import_sessions WHERE sheet_id IN ({_targets('sheet')}) OR sheet_id IN (SELECT sheet_id FROM columns WHERE id IN ({_targets('column')})) OR id IN (SELECT session_id FROM import_session_rows WHERE row_id IN ({_targets('row')}))",
            p,
        )
        if db.execute(
            "SELECT 1 FROM import_sessions s JOIN project_purge_imports p ON p.session_id=s.id WHERE p.purge_id=? AND s.state IN ('active','paused','cancelled')",
            (purge,),
        ).fetchone():
            raise ValueError(
                "Finish or cancel and keep the import before permanently removing its content"
            )

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
            f"SELECT op_id id FROM import_sessions WHERE id IN (SELECT session_id FROM project_purge_imports WHERE purge_id=:purge) UNION SELECT parent_op_id FROM sheets WHERE id IN ({_targets('sheet')})",
        )
        for kind, key in (
            ("sheet", "created_sheets"),
            ("column", "created_columns"),
            ("row", "created_rows"),
            ("sheet", "deleted_sheets"),
            ("row", "deleted_rows"),
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
        remember(
            "text_surface",
            f"SELECT id FROM text_surfaces WHERE {_scope(sheet='text_sheet_id', column='text_column_id', row='text_row_id')} OR {_scope(sheet="json_extract(surface_ref_json,'$.identity.sheet_id')", row="json_extract(surface_ref_json,'$.identity.row_id')")}",
        )
        remember(
            "watch_run",
            f"SELECT run_id id FROM watch_run_hits WHERE {_scope(sheet='sheet_id', column='column_id', row='row_id')} UNION SELECT run_id FROM watch_run_events WHERE {_scope(sheet="json_extract(subject_ref,'$.sheet_id')", column="json_extract(subject_ref,'$.column_id')", row="json_extract(subject_ref,'$.row_id')")}",
        )
        # Offline destructive maintenance also stops affected producers. Keep
        # completed contributions applied; never turn purge into a cascade.
        from .changes import ChangeStore
        from .runs import RunResultStore

        changes = ChangeStore(project)
        runs = RunResultStore(project)
        for operation in db.execute(
            f"SELECT id FROM ops WHERE publication_closed=0 AND status='applied' AND id IN ({_targets('change')})",
            p,
        ):
            changes.close(int(operation[0]))
        for run in db.execute(
            f"SELECT id FROM runs WHERE status='running' AND id IN ({_targets('run')})",
            p,
        ):
            runs.request_cancel(int(run[0]), commit=False)
        db.execute(
            f"UPDATE ops SET redo_eligible=0 WHERE id IN ({_targets('change')})", p
        )
        db.execute(f"UPDATE rows SET hidden=1 WHERE id IN ({_targets('row')})", p)
        db.execute(
            f"UPDATE columns SET active=0,hidden=1 WHERE id IN ({_targets('column')})",
            p,
        )
        db.execute(f"UPDATE sheets SET hidden=1 WHERE id IN ({_targets('sheet')})", p)
        db.commit()
        return purge
    except BaseException:
        db.rollback()
        raise


def _effect_scope():
    return f"(family='row_effect' AND CAST(group_key AS INTEGER) IN ({_targets('run')})) OR authorized_attempt_id IN (SELECT id FROM execution_attempts WHERE run_id IN ({_targets('run')}))"


def _delete_batches(db, table, predicate, parameters, *, batch_size):
    # Explicit per-table primary keys also support WITHOUT ROWID tables.
    keys = [
        r[1]
        for r in sorted(db.execute(f"PRAGMA table_info({table})"), key=lambda r: r[5])
        if r[5]
    ]
    key = "(" + ",".join(keys) + ")" if len(keys) > 1 else keys[0] if keys else "rowid"
    while True:
        db.execute("BEGIN IMMEDIATE")
        try:
            changed = db.execute(
                f"DELETE FROM {table} WHERE {key} IN (SELECT {','.join(keys) if keys else 'rowid'} FROM {table} WHERE {predicate} LIMIT :batch)",
                {**parameters, "batch": batch_size},
            ).rowcount
            db.commit()
        except BaseException:
            db.rollback()
            raise
        if changed < batch_size:
            break


def finish_purge(project: Any, purge_id: int, *, batch_size: int = 1000) -> dict:
    """Idempotently finish one obligation under the caller's offline boundary."""
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
    if state[0] == "content":
        _scrub_content(project, p, batch_size)
        with db:
            db.execute("UPDATE project_purges SET phase='files' WHERE id=:purge", p)
    _purge_files(project, p)
    from .project_blobs import reclaim_local_blobs

    summary = reclaim_local_blobs(project, dry_run=False)
    # Secure delete removes DB freelist payloads; checkpoint removes old WAL
    # copies. This does not promise erasure from backups or SSD snapshots.
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    with db:
        db.execute("UPDATE project_purges SET phase='complete' WHERE id=:purge", p)
    return {**summary, "purge_id": purge_id, "phase": "complete"}


def _scrub_content(project, p, batch_size):
    db = project.db
    db.execute("PRAGMA secure_delete=ON")
    cell = _scope(column="column_id", row="row_id")
    scope = _scope(sheet="sheet_id", column="column_id", row="row_id")
    citation_scope = _scope(
        sheet="json_extract(locator_json,'$.sheet_id')",
        column="json_extract(locator_json,'$.column_id')",
        row="json_extract(locator_json,'$.row_id')",
    )
    deletions = (
        (
            "project_qa_events",
            f"kind IN ('tool_started','tool_completed') AND turn_id IN (SELECT turn_id FROM project_qa_citations WHERE {citation_scope})",
        ),
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
        ("copied_result_outcomes", cell),
        (
            "results",
            cell
            + " AND NOT EXISTS (SELECT 1 FROM copied_result_outcomes copy WHERE copy.source_run_id=results.run_id AND copy.source_row_id=results.row_id AND copy.source_column_id=results.column_id)",
        ),
        ("edits", cell),
        ("cells", cell),
        ("run_rows", _scope(row="row_id")),
        ("source_items", _scope(row="row_id")),
        ("keyed_observations", _scope(row="row_id")),
        ("keyed_relationships", _scope(sheet="sheet_id")),
        ("import_session_rows", _scope(row="row_id")),
        ("replay_edit_dismissals", cell),
        ("column_descriptor_changes", _scope(column="column_id")),
        ("evidence_links", scope),
        ("watch_row_field_state", scope),
        ("watch_seen_rows", _scope(sheet="sheet_id", row="row_id")),
        (
            "notification_items",
            f"(source_kind='watch' AND json_extract(source_ref,'$.run_id') IN ({_targets('watch_run')})) OR EXISTS (SELECT 1 FROM json_each(source_event_ids) e JOIN watch_run_events w ON w.id=e.value WHERE w.run_id IN ({_targets('watch_run')}))",
        ),
        ("watch_runs", f"id IN ({_targets('watch_run')})"),
    )
    for table, predicate in deletions:
        _delete_batches(db, table, predicate, p, batch_size=batch_size)
    _delete_batches(
        db,
        "results",
        "(row_id IN (SELECT object_id FROM project_purge_targets WHERE kind='row') OR column_id IN (SELECT object_id FROM project_purge_targets WHERE kind='column')) AND NOT EXISTS (SELECT 1 FROM copied_result_outcomes copy WHERE copy.source_run_id=results.run_id AND copy.source_row_id=results.row_id AND copy.source_column_id=results.column_id)",
        p,
        batch_size=batch_size,
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
        with db:
            db.execute("BEGIN IMMEDIATE")
            purge_baseline_columns(db, ids)
        cursor = ids[-1]
    _purge_captures(db, p, batch_size)
    _purge_text_surface_payloads(db, p, batch_size)
    # Historical identity remains, while data-bearing descriptors disappear.
    updates = (
        (
            "source_items",
            "raw_ref_json='{}'",
            f"source_id IN (SELECT id FROM sources WHERE sheet_id IN (SELECT sheet_id FROM columns WHERE id IN ({_targets('column')})))",
        ),
        ("effect_checkpoints", "payload='{}'", _effect_scope()),
        (
            "project_qa_citations",
            "label='Unavailable source',excerpt=NULL,metadata_json='{}'",
            citation_scope,
        ),
        (
            "ops",
            "spec='{}',undo_info=" + _retained_descriptor_info() + ",label=NULL",
            f"id IN ({_targets('change')})",
        ),
        ("runs", "params='{}',work_progress=NULL", f"id IN ({_targets('run')})"),
        (
            "receipts",
            "body='{}',edition_run_context=NULL",
            f"run_id IN ({_targets('run')}) OR id IN (SELECT receipt_id FROM import_sessions WHERE id IN (SELECT session_id FROM project_purge_imports WHERE purge_id=:purge))",
        ),
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
            "sheets",
            "name='purged_'||id||'_'||lower(hex(randomblob(8)))",
            f"id IN ({_targets('sheet')})",
        ),
        ("model_calls", "warnings='[]'", f"run_id IN ({_targets('run')})"),
    )
    for table, assignments, predicate in updates:
        _update_batches(db, table, assignments, predicate, p, batch_size=batch_size)


def _retained_descriptor_info():
    # Descriptor contributions are shared authority, not diagnostic recovery.
    # Keep only those typed before/after fields for surviving columns; blanket
    # removal would break an unrelated column changed by the same operation.
    fields = (
        "column_types",
        "column_types_after",
        "column_formats",
        "column_formats_after",
        "column_semantic_types",
        "column_semantic_types_after",
    )
    return (
        "json_object("
        + ",".join(
            f"'{field}',json((SELECT json_group_object(key,value) FROM json_each(ops.undo_info,'$.{field}') WHERE CAST(key AS INTEGER) NOT IN (SELECT object_id FROM project_purge_targets WHERE kind='column')))"
            for field in fields
        )
        + ")"
    )


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
        db.execute("PRAGMA secure_delete=ON")
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
            db.execute("BEGIN IMMEDIATE")
            try:
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
                db.commit()
            except BaseException:
                db.rollback()
                raise
        _delete_batches(
            db,
            "representations",
            "representation_key NOT IN (SELECT representation_key FROM part_representations UNION SELECT representation_key FROM named_dataset_membership)",
            {},
            batch_size=500,
        )
        db.execute("DETACH DATABASE authority")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


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
        db.execute("BEGIN IMMEDIATE")
        try:
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
            db.commit()
        except BaseException:
            db.rollback()
            raise
        cursor = ids[-1]


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
                db.execute("BEGIN IMMEDIATE")
                try:
                    runs, changed = purge_consumed_sources(
                        db, **{argument: ids}, limit=min(batch_size, 500)
                    )
                    db.executemany(
                        "INSERT OR IGNORE INTO project_purge_targets(kind,object_id,purge_id) VALUES ('run',?,?)",
                        ((run, p["purge"]) for run in runs),
                    )
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
                if changed == 0:
                    break
            cursor = ids[-1]
