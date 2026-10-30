"""Exact value reads and source-only row identities shared with preview capture."""

from __future__ import annotations

import hashlib
import json

from frisket.engine.store.current_value_windows import read_current_column
from frisket.engine.store.retrieval_lifecycle import read_binding_revisions
from frisket.server.route_errors import RouteError


def value_hash(value):
    data = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def source_fingerprints(project, sheet_id, source_column_ids, row_ids):
    """Identity is row-bound and excludes generated output revisions."""
    ids = sorted(set(source_column_ids))
    descriptors = []
    for column_id in ids:
        column = project.get_column(column_id)
        if column is None or column["sheet_id"] != sheet_id or not column["active"]:
            raise RouteError(409, "Comparison source column changed")
        descriptors.append(
            (column_id, column["type"], column["semantic_type"], column["format"])
        )
    revisions = read_binding_revisions(
        project.db, [(row_id, column_id) for row_id in row_ids for column_id in ids]
    )
    return {
        str(row_id): value_hash(
            [
                sheet_id,
                row_id,
                descriptors,
                [[column_id, revisions.get((row_id, column_id))] for column_id in ids],
            ]
        )
        for row_id in row_ids
    }


def full_column(project, sheet_id, column_id, row_ids):
    """Keep typed values; bounded UI previews must never become score inputs."""
    return read_current_column(
        project.db,
        sheet_id=sheet_id,
        column_id=column_id,
        row_ids=row_ids,
        bounded=False,
        include_hidden=True,
    )


def current_rows(project, sheet_id, output_columns, row_ids):
    result = {str(row_id): {} for row_id in row_ids}
    for column in output_columns:
        if column["column_id"] is None:
            continue
        column_id = column["column_id"]
        reads = full_column(project, sheet_id, column_id, row_ids)
        for row_id in row_ids:
            read = reads.get(row_id)
            cell = {"state": "missing"}
            if read is not None:
                ref = read.value_ref
                error = None
                if ref.get("kind") == "run_result" and ref.get("run_id") is not None:
                    stored = project.db.execute(
                        "SELECT publication_effect,error FROM results WHERE run_id=? AND row_id=? AND column_id=?",
                        (ref["run_id"], row_id, column_id),
                    ).fetchone()
                    if (
                        stored is not None
                        and stored["publication_effect"] == "publish_error"
                    ):
                        error = stored["error"] or "This row failed"
                rejected_clear = False
                if ref.get("kind") == "manual_edit" and ref.get("op_id"):
                    op = project.db.execute(
                        "SELECT json_extract(spec,'$.params.decision') FROM ops WHERE id=? AND kind='review.decision'",
                        (ref["op_id"],),
                    ).fetchone()
                    rejected_clear = op is not None and op[0] == "reject_clear"
                if rejected_clear:
                    cell = {"state": "missing"}
                elif error:
                    cell = {"state": "error", "error": error}
                elif read.value_present:
                    cell = {
                        "state": "null" if read.value is None else "value",
                        "value": read.value,
                    }
            result[str(row_id)][column["logical_key"]] = cell
    return result
