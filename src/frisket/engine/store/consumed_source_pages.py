"""Bounded source staging inside the caller's fixed read snapshot."""

from __future__ import annotations

import json

from .consumed_sources import _descriptors, _hash, _records, authority_revisions


def iter_consumed_source_pages(
    snapshot, sheet_id, column_ids, *, row_ids=None, batch_size=256, captures
):
    """Yield actual value/ref pages; retain one compact capture only on EOF.

    Whole-source iteration never materializes its full row roster or values.
    Explicit selected IDs remain the required frozen cohort. Closing early
    leaves no complete-source certificate for uncommitted staging work.
    """
    if not snapshot.db.in_transaction:
        raise RuntimeError("source pages require their actual read snapshot")
    if not 1 <= batch_size <= 1000:
        raise ValueError("source page size must be between 1 and 1000")
    columns = sorted(set(map(int, column_ids)))
    selected = sorted(set(map(int, row_ids))) if row_ids is not None else None
    descriptors = _descriptors(snapshot, columns)
    if any(value is None for value in descriptors.values()):
        raise ValueError("source column is unavailable")
    scope = {
        "sheet_id": int(sheet_id),
        "column_ids": columns,
        "intent": "whole" if selected is None else "selected",
        "cohort": selected,
        "max_row_id": 0,
        "partitions": [],
    }
    revisions = authority_revisions(snapshot, columns)
    restriction = (
        " AND row.id IN (SELECT value FROM json_each(?))"
        if selected is not None
        else ""
    )
    params = [sheet_id, json.dumps(selected)] if selected is not None else [sheet_id]
    cursor = snapshot.db.execute(
        "SELECT row.id,row.position FROM rows row JOIN sheets sheet ON sheet.id=row.sheet_id "
        "WHERE row.sheet_id=? AND row.hidden=0 AND sheet.hidden=0"
        + restriction
        + " ORDER BY row.id",
        params,
    )
    previous_hi, count = 0, 0
    try:
        while members := cursor.fetchmany(batch_size):
            page = [int(row["id"]) for row in members]
            values, refs = {}, {}
            for column in columns:
                values[column], refs[column] = snapshot.get_values_with_refs(
                    sheet_id, column, row_ids=page, include_validity=True
                )
            scope["partitions"].append(
                {
                    "lo": previous_hi + 1 if selected is None else page[0],
                    "hi": page[-1],
                    "fingerprint": _hash(
                        _records(
                            sheet_id,
                            columns,
                            [list(row) for row in members],
                            descriptors,
                            refs,
                        )
                    ),
                    "count": len(page),
                    "member_count": len(page),
                }
            )
            previous_hi = scope["max_row_id"] = page[-1]
            count += len(page)
            yield page, values, refs
        if selected is not None and count != len(selected):
            raise ValueError("selected source rows are unavailable")
        if not scope["partitions"]:
            scope["partitions"].append(
                {
                    "lo": 1 if selected is None else 0,
                    "hi": 0,
                    "fingerprint": _hash(
                        _records(
                            sheet_id,
                            columns,
                            [],
                            descriptors,
                            {column: {} for column in columns},
                        )
                    ),
                    "count": 0,
                    "member_count": 0,
                }
            )
        captures.append(
            {
                "schema_version": 1,
                "kind": "aggregate",
                "scopes": [scope],
                "revisions": revisions,
                "coherent": True,
                "fingerprint": _hash([scope]),
            }
        )
    finally:
        cursor.close()
