"""Bundle-level I/O for a project store: atomic zip export, raw SQLite
snapshot export, safe bundle import (member-path and symlink validation
before extraction, blob hash verification after), compaction of discarded
history, and bundle deletion. Free functions over the facade's per-thread
SQLite connection; ``project`` stays duck-typed (``Any``) so this leaf never
re-imports the facade module."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from pathlib import Path
from typing import Any


def _reject_unsafe_bundle_member(info: zipfile.ZipInfo, target_root: str) -> None:
    """Refuse a bundle zip member that is unsafe to extract.

    A member is rejected if its resolved destination would land outside
    ``target_root`` (an absolute path, a Windows drive letter, or a ``../``
    escape) or if it is a symlink (the Unix mode's S_ISLNK bit in
    ``external_attr``, the same check ``bundle_backup._reject_symlink`` applies
    to already-extracted files -- here it must run on the zip member itself,
    before anything is written to disk). This runs on every member before
    ``extractall`` touches the filesystem, since extractall does not sanitize
    member paths.
    """
    name = info.filename
    if os.path.isabs(name) or name.startswith(("/", "\\")):
        raise ValueError(f"bundle member {name!r} has an unsafe absolute path")
    if len(name) >= 2 and name[1] == ":" and name[0].isalpha():
        raise ValueError(f"bundle member {name!r} has an unsafe absolute path")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise ValueError(f"bundle member {name!r} is a symlink")
    dest = os.path.realpath(os.path.join(target_root, name))
    if dest != target_root and not dest.startswith(target_root + os.sep):
        raise ValueError(f"bundle member {name!r} escapes the target directory")


def delete_bundle(path: str | Path) -> None:
    """Remove a bundle directory. The only truly destructive call; not an op."""
    p = Path(path)
    if (p / "manifest.json").exists():
        shutil.rmtree(p)


def export(
    project: Any,
    target_zip: str | Path,
    include_media: bool = True,
    *,
    include_traces: bool = False,
) -> Path:
    """Atomically publish a complete bundle export."""
    target = Path(target_zip)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(fd)
    temp_target = Path(raw_temp)
    db = project.db
    if db.in_transaction:
        temp_target.unlink(missing_ok=True)
        raise RuntimeError("cannot export while this connection has pending writes")
    try:
        # One SQLite write reservation freezes the DB/blob-reference boundary
        # while hash enumeration and serialization capture the same snapshot.
        # Once captured, immutable retained objects can be leased without
        # blocking later writes; those writes belong to the next snapshot.
        db.execute("BEGIN IMMEDIATE")
        try:
            blob_hashes = [r["hash"] for r in db.execute("SELECT hash FROM blobs")]
            database_snapshot = db.serialize()
            manifest = json.loads((project.path / "manifest.json").read_text())
            manifest["blobs"] = blob_hashes
            manifest["include_media"] = include_media
            manifest["include_traces"] = include_traces
        finally:
            db.rollback()
        with zipfile.ZipFile(temp_target, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            zf.writestr("project.db", database_snapshot)
            if include_media:
                for digest in blob_hashes:
                    with project.materialize_blob(digest) as path:
                        zf.write(Path(path), f"blobs/{digest[:2]}/{digest}")
            if include_traces:
                trace_dir = project.path / "traces"
                if trace_dir.is_dir() and not trace_dir.is_symlink():
                    for trace in sorted(trace_dir.glob("run-*.jsonl.gz")):
                        if trace.is_file() and not trace.is_symlink():
                            zf.write(trace, f"traces/{trace.name}")
        os.replace(temp_target, target)
    finally:
        temp_target.unlink(missing_ok=True)
    return target


def export_database(project: Any, target_db: str | Path) -> Path:
    """Write a consistent raw SQLite snapshot to a standalone db file."""
    target = Path(target_db)
    target.parent.mkdir(parents=True, exist_ok=True)
    project.db.execute("PRAGMA wal_checkpoint(PASSIVE)")
    dest = sqlite3.connect(target)
    try:
        project.db.backup(dest)
    finally:
        dest.close()
    return target


def compact(
    project: Any, vacuum: bool = True, *, force: bool = False
) -> dict[str, Any]:
    """Reclaim space without touching live data (the un-append-forever).

    Three passes:
      1. prune results/runs on discarded (un-redoable) op branches,
      2. GC blobs no live value references,
      3. VACUUM the db to return freed pages to the filesystem and
         checkpoint the WAL.

    Reachable history — applied ops and the still-redoable undo branch — is
    never touched, so undo/redo and provenance stay intact. Returns a
    summary of what was reclaimed.
    """
    size_before = project.db_path.stat().st_size if project.db_path.exists() else 0
    if project.retention_policy()["no_compact"] and not force:
        return {
            "skipped": True,
            "reason": "project_no_compact",
            "results_pruned": 0,
            "blobs_removed": 0,
            "bytes_freed": 0,
            "db_bytes_before": size_before,
            "db_bytes_after": size_before,
            "db_bytes_reclaimed": 0,
        }
    try:
        project.db.execute("BEGIN IMMEDIATE")
        results_pruned = _prune_dead_runs(project)
        project.db.commit()
    except BaseException:
        project.db.rollback()
        raise
    blob_summary = project.gc_blobs()
    if vacuum:
        # checkpoint first so the VACUUM sees a clean main db
        project.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        project.db.execute("VACUUM")
        project.db.commit()
    size_after = project.db_path.stat().st_size if project.db_path.exists() else 0
    return {
        "skipped": False,
        "reason": None,
        "results_pruned": results_pruned,
        "blobs_removed": blob_summary["blobs_removed"],
        "bytes_freed": blob_summary["bytes_freed"],
        "db_bytes_before": size_before,
        "db_bytes_after": size_after,
        "db_bytes_reclaimed": max(0, size_before - size_after),
    }


def _prune_dead_runs(project: Any) -> int:
    """Delete discarded managed journals and unreachable legacy results.
    Applied ops and the redoable undo branch are preserved.

    Paid-effect checkpoints are exempt: they answer a money question
    reachability cannot (see the comment below), and survive as orphans for
    the operator to decide."""
    from frisket.engine.store.result_generations import ResultGenerationStore

    generations = ResultGenerationStore(project)
    discarded_bindings = [
        binding
        for op in project.db.execute(
            "SELECT id FROM ops WHERE status='discarded' ORDER BY id"
        ).fetchall()
        for binding in generations.bindings_for_op(int(op["id"]))
    ]
    surviving_base_run_ids = {
        int(row["expected_base_run_id"])
        for row in project.db.execute(
            "SELECT DISTINCT generation.expected_base_run_id "
            "FROM run_output_generations generation "
            "JOIN runs run ON run.id=generation.run_id "
            "JOIN ops op ON op.id=run.op_id "
            "WHERE generation.expected_base_run_id IS NOT NULL "
            "AND op.status!='discarded'"
        ).fetchall()
    }
    managed_run_ids = sorted(
        {binding.run_id for binding in discarded_bindings} - surviving_base_run_ids
    )
    if discarded_bindings:
        generations.rebuild_heads(
            {binding.column_id for binding in discarded_bindings}, commit=False
        )
        for run_id in managed_run_ids:
            project.db.execute(
                "UPDATE columns SET current_run_id=NULL WHERE current_run_id=? "
                "AND id IN (SELECT column_id FROM run_output_generations "
                "WHERE run_id=?)",
                (run_id, run_id),
            )
            project.db.execute(
                "DELETE FROM run_output_generations WHERE run_id=?", (run_id,)
            )

    deleted = 0
    for run_id in managed_run_ids:
        deleted += int(
            project.db.execute("DELETE FROM results WHERE run_id=?", (run_id,)).rowcount
        )

    cur = project.db.execute(
        "DELETE FROM results WHERE run_id IN ("
        "  SELECT r.id FROM runs r "
        "  JOIN ops o ON o.id = r.op_id "
        "  WHERE o.status='discarded' "
        "    AND r.id NOT IN ("
        "      SELECT generation.expected_base_run_id "
        "      FROM run_output_generations generation "
        "      JOIN runs owner ON owner.id=generation.run_id "
        "      JOIN ops owner_op ON owner_op.id=owner.op_id "
        "      WHERE generation.expected_base_run_id IS NOT NULL "
        "      AND owner_op.status!='discarded'"
        "    ) "
        "    AND r.id NOT IN (SELECT current_run_id FROM columns "
        "                     WHERE current_run_id IS NOT NULL))"
    )
    deleted += int(cur.rowcount)
    # Effect checkpoints are deliberately NOT pruned with their run.
    #
    # The retired per-family table carried a runs(id) ON DELETE CASCADE and
    # the shared lifecycle reproduced it here as an explicit same-predicate
    # DELETE — faithfully preserving a behaviour that was wrong all along.
    # Reachability is the wrong question to ask of a paid effect: a
    # ``reserved`` row records that a provider MAY already have charged for a
    # unit and a ``returned`` row that it DID, and neither fact stops being
    # true because the operator undid the op and reclaimed space.  Compaction
    # was silently answering the money question ("no charge happened") that
    # only a human with the provider's bill can answer.
    #
    # Every legal state of a runner-family checkpoint carries money meaning —
    # ``reserved`` is ambiguous egress, ``returned`` is a durable paid
    # response, and ``consumed`` here is the operator's own accept-charged
    # attestation — so there is no reachability-shaped subset left to
    # reclaim.  This is why the checkpoint schema has no run foreign key: the
    # rows outlive their referent as ORPHANED reconcilable records, which
    # ``frisket reconcile list`` surfaces with an honest reason, and the
    # OPERATOR pair (``operator_discard`` / ``operator_accept_charged``) is
    # the only authority that may retire one.
    #
    # the now-empty discarded runs themselves
    project.db.execute(
        "DELETE FROM runs WHERE op_id IN (SELECT id FROM ops WHERE status='discarded') "
        "AND id NOT IN ("
        "  SELECT generation.expected_base_run_id "
        "  FROM run_output_generations generation "
        "  JOIN runs owner ON owner.id=generation.run_id "
        "  JOIN ops owner_op ON owner_op.id=owner.op_id "
        "  WHERE generation.expected_base_run_id IS NOT NULL "
        "  AND owner_op.status!='discarded'"
        ") "
        "AND id NOT IN (SELECT current_run_id FROM columns "
        "               WHERE current_run_id IS NOT NULL) "
        "AND id NOT IN (SELECT DISTINCT run_id FROM results)"
    )
    return deleted


def _stamp_staged_manifest_for_target(staging: Path, target: Path) -> None:
    """Stamp path-derived manifest identity before publishing ``staging``.

    Project-open verification must run while the bundle is unpublished, and
    the staging directory must remain a direct sibling of ``target`` so the
    installation root (and therefore its consent principal) is the real
    workspace root. Opening that randomly named sibling stamps its temporary
    stem into ``manifest.json``; repair just that path-derived field through a
    unique sibling temp before the directory rename.
    """
    manifest_path = staging / "manifest.json"
    data = json.loads(manifest_path.read_text())
    if not isinstance(data, dict):
        raise ValueError("not a frisket bundle")
    data["project_id"] = target.stem
    fd, raw_temp = tempfile.mkstemp(
        dir=staging,
        prefix=".manifest.import-",
        suffix=".tmp",
    )
    scratch = Path(raw_temp)
    try:
        handle = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        with handle:
            handle.write(json.dumps(data, indent=2))
        os.replace(scratch, manifest_path)
    finally:
        if fd >= 0:
            os.close(fd)
        scratch.unlink(missing_ok=True)


def import_bundle(
    project_cls: Any, source_zip: str | Path, target_dir: str | Path
) -> Any:
    target = Path(target_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"{target} already exists")
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{target.name}.import-",
            suffix=".tmp",
            dir=target.parent,
        )
    )
    try:
        with zipfile.ZipFile(source_zip) as zf:
            try:
                manifest = json.loads(zf.read("manifest.json"))
            except KeyError:
                raise ValueError("not a frisket bundle")
            if not isinstance(manifest, dict):
                raise ValueError("not a frisket bundle")
            if manifest.get("format") != "frisket-bundle":
                raise ValueError("not a frisket bundle")
            # Every member is validated before archive bytes are written:
            # extractall does not sanitize member paths, so an absolute path,
            # a drive letter, a symlink, or a ``../`` escape must be caught
            # before extraction into the hidden sibling staging directory.
            staging_real_root = os.path.realpath(staging)
            for info in zf.infolist():
                _reject_unsafe_bundle_member(info, staging_real_root)
            zf.extractall(staging)
        # Integrity and the normal Project-open fences both run against the
        # unpublished staging tree. A malformed archive therefore never
        # becomes visible as target, even briefly.
        for h in manifest.get("blobs", []):
            p = staging / "blobs" / h[:2] / h
            if p.exists():
                actual = hashlib.sha256(p.read_bytes()).hexdigest()
                if actual != h:
                    raise ValueError(f"blob {h} failed hash verification")
        (staging / "blobs").mkdir(exist_ok=True)
        staged_project = project_cls(staging)
        staged_project.close()
        _stamp_staged_manifest_for_target(staging, target)

        # This atomic filesystem publication is deliberately scoped to the
        # local library import seam. Hosted blob/CAS import needs a separate
        # staged-object publication protocol and remains future work.
        if target.exists():
            raise FileExistsError(f"{target} already exists")
        os.replace(staging, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return project_cls(target)
