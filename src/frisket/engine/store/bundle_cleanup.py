"""Bounded offline removal of unowned prepared content and run diagnostics."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .blob_backend import _fsync_directory


def cleanup_unowned_bundle_content(project: Any, *, dry_run: bool) -> dict[str, int]:
    """Called by the existing offline sweep under its maintenance transaction.

    Retained cells, results, edits and document correction pins own prepared
    content regardless of visibility. A saved result copied elsewhere therefore
    protects the same immutable reference. Traces only belong to extant runs.
    Selected-scope purge must detach its owners before invoking this sweep.
    """
    db = project.db
    if not db.in_transaction:
        raise ValueError("bundle cleanup requires a maintenance transaction")
    counts = {}
    db.execute("CREATE TEMP TABLE gc_live_prepared_refs(id INTEGER PRIMARY KEY)")
    try:
        for table in ("cells", "results", "edits"):
            db.execute(
                "INSERT OR IGNORE INTO temp.gc_live_prepared_refs "
                f"SELECT value FROM {table} WHERE value_kind='prepared_content_ref'"
            )
        db.execute(
            "INSERT OR IGNORE INTO temp.gc_live_prepared_refs "
            "SELECT prepared_ref_id FROM document_text_manual_pins"
        )
        queries = {
            "prepared_refs": "SELECT id FROM prepared_content_refs WHERE id NOT IN (SELECT id FROM temp.gc_live_prepared_refs)",
            "prepared_sets": "SELECT id FROM prepared_content_sets WHERE NOT EXISTS (SELECT 1 FROM prepared_content_refs r JOIN temp.gc_live_prepared_refs live ON live.id=r.id WHERE r.set_id=prepared_content_sets.id)",
            "prepared_pages": "SELECT id FROM prepared_page_versions WHERE NOT EXISTS (SELECT 1 FROM prepared_content_set_pages p JOIN prepared_content_refs r ON r.set_id=p.set_id JOIN temp.gc_live_prepared_refs live ON live.id=r.id WHERE p.version_id=prepared_page_versions.id)",
        }
        for name, query in queries.items():
            counts[name + "_reclaimable"] = db.execute(
                f"SELECT count(*) FROM ({query})"
            ).fetchone()[0]
        if not dry_run:
            db.execute(
                "DELETE FROM prepared_content_refs WHERE id IN ("
                + queries["prepared_refs"]
                + ")"
            )
            db.execute(
                "DELETE FROM prepared_content_sets WHERE id IN ("
                + queries["prepared_sets"]
                + ")"
            )
            db.execute(
                "DELETE FROM prepared_page_versions WHERE id IN ("
                + queries["prepared_pages"]
                + ")"
            )
    finally:
        db.execute("DROP TABLE temp.gc_live_prepared_refs")

    counts["traces_reclaimable"] = 0
    counts["trace_bytes_reclaimable"] = 0
    counts["traces_removed"] = 0
    root = project.path / "traces"
    if not root.exists() or root.is_symlink():
        return counts
    with os.scandir(root) as entries:
        for entry in entries:
            match = re.fullmatch(r"run-([1-9][0-9]*)\.jsonl\.gz", entry.name)
            if not match or not entry.is_file(follow_symlinks=False):
                continue
            if db.execute("SELECT 1 FROM runs WHERE id=?", (int(match[1]),)).fetchone():
                continue
            counts["traces_reclaimable"] += 1
            counts["trace_bytes_reclaimable"] += entry.stat(
                follow_symlinks=False
            ).st_size
            if not dry_run:
                Path(entry.path).unlink(missing_ok=True)
                counts["traces_removed"] += 1
    if counts["traces_removed"]:
        _fsync_directory(root)
    return counts
