"""Review pages for saved-result Changes, without manufacturing executions."""

from __future__ import annotations

from .review import _COUNT_NAMES
from frisket.engine.store.review_values import read_review_results
from frisket.engine.store.current_value_windows import read_current_column


def copied_review_fields(project, change_id):
    return [
        dict(row)
        for row in project.db.execute(
            """
        SELECT copy.column_id,col.name column_name,col.type column_type,
          COUNT(*) eligible_count,
          SUM(copy.review_decision IS NOT NULL) reviewed_count,
          COALESCE(SUM(copy.review_decision='accept'),0) accepted_count,
          COALESCE(SUM(copy.review_decision IN ('reject','reject_clear')),0) incorrect_count,
          SUM(copy.review_state='unreviewed' AND copy.review_decision IS NULL) unreviewed_count,
          SUM(copy.confidence IS NOT NULL) confidence_count
        FROM copied_result_values copy JOIN changes op ON op.id=copy.change_id JOIN columns col ON col.id=copy.column_id
        JOIN json_each(op.spec,'$.output_bindings') binding ON json_extract(binding.value,'$.column_id')=copy.column_id
        WHERE copy.change_id=? AND copy.outcome='completed' AND json_extract(binding.value,'$.review_role')='field'
        GROUP BY copy.column_id ORDER BY col.position,copy.column_id
    """,
            (change_id,),
        )
    ]


def copied_review_source(project, change_id):
    row = project.db.execute(
        """
      SELECT op.id copied_change_id,NULL run_id,col.sheet_id,sheet.name sheet_name,
        'run.reapply' action_kind,'Saved results' action_name,NULL model,
        op.created_at started_at,NULL review_completed_at,'open' review_status
      FROM changes op JOIN copied_result_outcomes copy ON copy.change_id=op.id
      JOIN columns col ON col.id=copy.column_id JOIN sheets sheet ON sheet.id=col.sheet_id
      WHERE op.id=? AND op.status='applied' GROUP BY op.id,col.sheet_id
    """,
        (change_id,),
    ).fetchone()
    if row is None:
        return None
    fields = copied_review_fields(project, change_id)
    return {
        **dict(row),
        "fields": fields,
        "total": {
            name: sum(int(field[name] or 0) for field in fields)
            for name in _COUNT_NAMES
        },
    }


def copied_review_page(
    project,
    change_id,
    *,
    offset=0,
    limit=25,
    include_reviewed=False,
    field_id=None,
    order="confidence",
    cursor=None,
    row_ids=None,
    exclude_row_ids=(),
):
    if order not in {"confidence", "row", "sample"}:
        raise ValueError("invalid review order")
    where = [
        "copy.change_id=?",
        "copy.outcome='completed'",
        "json_extract(binding.value,'$.review_role')='field'",
        "op.status='applied'",
        "target.hidden=0",
        "col.active=1",
        "sheet.hidden=0",
    ]
    params = [change_id]
    if not include_reviewed:
        where.extend(["copy.review_state='unreviewed'", "copy.review_decision IS NULL"])
    if field_id is not None:
        where.append("copy.column_id=?")
        params.append(field_id)
    joins = """FROM copied_result_values copy JOIN changes op ON op.id=copy.change_id
      JOIN rows target ON target.id=copy.row_id JOIN columns col ON col.id=copy.column_id
      JOIN sheets sheet ON sheet.id=col.sheet_id
      JOIN json_each(op.spec,'$.output_bindings') binding ON json_extract(binding.value,'$.column_id')=copy.column_id"""
    total = int(
        project.db.execute(
            "SELECT COUNT(DISTINCT copy.row_id) "
            + joins
            + " WHERE "
            + " AND ".join(where),
            params,
        ).fetchone()[0]
    )
    if cursor is not None:
        where.append("copy.row_id>?")
        params.append(cursor)
    if row_ids is not None:
        where.append("copy.row_id IN (" + ",".join("?" for _ in row_ids) + ")")
        params.extend(row_ids)
    if exclude_row_ids:
        where.append(
            "copy.row_id NOT IN (" + ",".join("?" for _ in exclude_row_ids) + ")"
        )
        params.extend(exclude_row_ids)
    ordering = {
        "confidence": "confidence ASC NULLS LAST,copy.row_id",
        "row": "copy.row_id",
        "sample": "random()",
    }[order]
    keys = [
        int(row[0])
        for row in project.db.execute(
            "SELECT copy.row_id,MIN(copy.confidence) confidence "
            + joins
            + " WHERE "
            + " AND ".join(where)
            + " GROUP BY copy.row_id ORDER BY "
            + ordering
            + " LIMIT ? OFFSET ?",
            (*params, limit + 1, offset),
        )
    ]
    has_more = len(keys) > limit
    keys = keys[:limit]
    if row_ids is not None:
        requested = {row: i for i, row in enumerate(row_ids)}
        keys.sort(key=requested.__getitem__)
    if not keys:
        records = []
    else:
        records = project.db.execute(
            """SELECT copy.row_id,copy.column_id,copy.source_run_id,copy.source_row_id,
          copy.source_column_id,copy.review_state,copy.review_decision,copy.review_note,copy.error,copy.outcome,copy.confidence,
          col.name column_name,col.type column_type,col.format,col.semantic_type,col.sheet_id,sheet.name sheet_name,
          source_col.sheet_id source_sheet_id,binding.key output_key,json_extract(binding.value,'$.review_role') review_role
          FROM copied_result_values copy JOIN changes op ON op.id=copy.change_id JOIN columns col ON col.id=copy.column_id
          JOIN sheets sheet ON sheet.id=col.sheet_id LEFT JOIN columns source_col ON source_col.id=copy.source_column_id
          JOIN json_each(op.spec,'$.output_bindings') binding ON json_extract(binding.value,'$.column_id')=copy.column_id
          WHERE copy.change_id=? AND copy.row_id IN ("""
            + ",".join("?" for _ in keys)
            + ") ORDER BY col.position,col.id",
            (change_id, *keys),
        ).fetchall()
    coordinates = [
        (r["source_run_id"], r["source_row_id"], r["source_column_id"])
        for r in records
        if r["source_run_id"] is not None
    ]
    resolved = read_review_results(project.db, coordinates)
    current_by_column = {
        column: read_current_column(
            project.db, sheet_id=sheet, column_id=column, row_ids=keys, bounded=True
        )
        for sheet, column in {(r["sheet_id"], r["column_id"]) for r in records}
    }
    from .review import _source_contexts

    context_keys = list(
        dict.fromkeys(
            (r["source_run_id"], r["source_sheet_id"], r["source_row_id"])
            for r in records
            if r["source_run_id"] is not None
        )
    )
    contexts = _source_contexts(project, context_keys)
    by_row = {key: [] for key in keys}
    for record in records:
        by_row[record["row_id"]].append(record)
    bundles = []
    for row_id in keys:
        items = []
        records = by_row[row_id]
        for r in records:
            result = resolved.get(
                (r["source_run_id"], r["source_row_id"], r["source_column_id"])
            )
            current = current_by_column[r["column_id"]].get(row_id)
            applied = bool(
                current
                and current.value_ref.get("kind") == "copied_result"
                and current.value_ref.get("change_id") == change_id
            )
            primary = r["review_role"] == "field" and r["outcome"] == "completed"
            bounded = (
                result.bounded_value.as_dict()
                if result and result.bounded_value
                else None
            )
            items.append(
                {
                    "run_id": None,
                    "copied_change_id": change_id,
                    "row_id": row_id,
                    "column_id": r["column_id"],
                    "output_key": r["output_key"],
                    "column_name": r["column_name"],
                    "column_type": r["column_type"],
                    "format": r["format"],
                    "semantic_type": r["semantic_type"],
                    "sheet_id": r["sheet_id"],
                    "value": result.value if result else None,
                    "bounded_value": bounded,
                    "confidence": r["confidence"],
                    "justification": result.justification if result else None,
                    "bounded_justification": result.bounded_justification.as_dict()
                    if result and result.bounded_justification
                    else None,
                    "error": r["error"],
                    "review_state": r["review_state"],
                    "review_decision": r["review_decision"],
                    "review_note": r["review_note"],
                    "role": "field" if primary else "supporting",
                    "chore": primary
                    and r["review_state"] == "unreviewed"
                    and r["review_decision"] is None,
                    "changed": False,
                    "applied": applied,
                    "can_edit": primary and applied and bounded is None,
                }
            )
        if not items:
            continue
        first = records[0]
        context = contexts.get(
            (first["source_run_id"], first["source_sheet_id"], first["source_row_id"]),
            {"source": {}, "sources": []},
        )
        bundles.append(
            {
                "id": f"copy:{change_id}:{row_id}",
                "run_id": None,
                "copied_change_id": change_id,
                "row_id": row_id,
                "sheet_id": first["sheet_id"],
                "sheet_name": first["sheet_name"],
                "action_kind": "run.reapply",
                "action_name": "Saved results",
                "model": None,
                "confidence": min(
                    (r["confidence"] for r in records if r["confidence"] is not None),
                    default=None,
                ),
                **context,
                "review_note": next(
                    (r["review_note"] for r in records if r["review_note"]), None
                ),
                "fields": [r for r in items if r["role"] == "field"],
                "supporting_columns": [r for r in items if r["role"] == "supporting"],
                "items": items,
            }
        )
    return {
        "schema_version": "frisket.review_bundles_page.v1",
        "offset": offset,
        "limit": limit,
        "total": total,
        "has_more": has_more,
        "next_offset": offset + len(bundles) if has_more and order != "row" else None,
        "next_cursor": keys[-1] if has_more and keys and order == "row" else None,
        "bundles": bundles,
    }
