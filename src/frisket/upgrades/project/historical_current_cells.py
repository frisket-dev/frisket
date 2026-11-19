"""Frozen current-cell projector for pre-Change schema migrations only."""

from __future__ import annotations
import json
import sqlite3
from frisket.authoring import column_types
from frisket.engine.store.search_index_work import enqueue_dirty_scope
from frisket.engine.store.value_codec import decode_stored_value


_SQLITE_BIND_LIMIT = 900
INLINE_TEXT_BYTE_LIMIT = 512


def stamp_retrieval_revision(db: sqlite3.Connection, revision: int) -> None:
    """Stamp all historical heads during the retrieval-schema migration."""
    _require_transaction(db)
    db.execute("UPDATE current_cells SET retrieval_revision=?", (revision,))


def inline_scalar_eligibility_sql(
    *, descriptor_alias: str, value_kind_sql: str, value_sql: str
) -> str:
    """Return the bounded scalar-copy policy for the current projection.

    Native null, integer, real, and boolean values are always compact. Big integers
    stay bounded text, while text/category/date strings share a small byte ceiling.
    Longer text and every structured value remain authority-table references.
    """

    return (
        f"({value_kind_sql} IN ('null','integer','real','boolean') "
        f"OR ({value_kind_sql}='bigint' "
        f"AND {descriptor_alias}.type IN ('integer','number') "
        f"AND length(CAST({value_sql} AS BLOB))<={INLINE_TEXT_BYTE_LIMIT}) "
        f"OR ({value_kind_sql}='text' "
        f"AND {descriptor_alias}.type IN ('text','category','date') "
        f"AND length(CAST({value_sql} AS BLOB))<={INLINE_TEXT_BYTE_LIMIT}))"
    )


def backfill_inline_scalar_values(db: sqlite3.Connection) -> None:
    """Fill bounded scalar copies after the additive schema migration."""

    _require_transaction(db)
    eligible = inline_scalar_eligibility_sql(
        descriptor_alias="descriptor",
        value_kind_sql="live.value_kind",
        value_sql="live.value",
    )
    db.execute(
        "WITH eligible AS ("
        "SELECT live.column_id,live.row_id,live.value_kind,live.value "
        "FROM current_cell_values live "
        "JOIN columns descriptor ON descriptor.id=live.column_id "
        f"WHERE {eligible}"
        ") UPDATE current_cells AS head SET "
        "inline_value_kind=eligible.value_kind,inline_value=eligible.value "
        "FROM eligible WHERE head.column_id=eligible.column_id "
        "AND head.row_id=eligible.row_id"
    )


def backfill_short_text_values(db: sqlite3.Connection) -> None:
    """Backfill the 512-byte text cache during its data-preserving migration."""

    from frisket.upgrades.project.historical_schema import (
        SCALAR_CURRENT_CELL_VALUES_SQL,
    )

    _require_transaction(db)
    # This pinned scalar view returns raw refs, not expanded document
    # text. Prepared documents must keep both their locator and citation.
    raw_values = "SELECT" + SCALAR_CURRENT_CELL_VALUES_SQL.split("SELECT", 1)[1]
    db.execute(
        f"WITH raw_values AS ({raw_values}), eligible AS ("
        "SELECT live.column_id,live.row_id,live.value_kind,live.value "
        "FROM raw_values live JOIN columns descriptor ON descriptor.id=live.column_id "
        "WHERE live.value_kind='text' AND descriptor.type IN ('text','category','date') "
        "AND length(CAST(live.value AS BLOB))<=512) "
        "UPDATE current_cells AS head SET inline_value_kind=eligible.value_kind,"
        "inline_value=eligible.value FROM eligible "
        "WHERE head.column_id=eligible.column_id AND head.row_id=eligible.row_id "
        "AND head.inline_value_kind IS NULL"
    )


def decoded_cell_validity(column_type: str, value: object) -> str:
    """Classify one decoded value against its column descriptor."""

    if value is None:
        return "missing"
    return "valid" if column_types.validate_value(column_type, value) else "invalid"


def cell_validity(
    column_type: str, value_kind: str | None, stored_value: object
) -> str:
    """Classify one native stored value against its column descriptor."""

    # Prepared-content references are physically positive integer ids but are
    # logically text.  Projection refresh cannot query through the same SQLite
    # connection from this UDF, so validate the storage shape and the only
    # supported logical column type without resolving the body here.
    if value_kind == "prepared_content_ref":
        return (
            "valid"
            if column_type == "text"
            and isinstance(stored_value, int)
            and not isinstance(stored_value, bool)
            and stored_value > 0
            else "invalid"
        )
    try:
        value = decode_stored_value(value_kind, stored_value)
    except (TypeError, ValueError, RecursionError):
        return "invalid"
    return decoded_cell_validity(column_type, value)


def legacy_cell_validity(column_type: str, encoded_value: str | None) -> str:
    """Historical JSON-layout validity used while older migrations advance."""

    if encoded_value is None:
        return "missing"
    try:
        value = json.loads(encoded_value)
    except (TypeError, ValueError, RecursionError):
        return "invalid"
    return decoded_cell_validity(column_type, value)


def live_edit_precedence_predicate(*, edit_alias: str, op_alias: str) -> str:
    """Keep edits except reject-clear overlays superseded by a replacement."""

    return (
        f"({op_alias}.kind<>'review.decision' "
        f"OR COALESCE(json_extract({op_alias}.spec,'$.action_id'),'')<>"
        "'review.decision' "
        f"OR COALESCE(json_extract({op_alias}.spec,'$.params.decision'),'')<>"
        "'reject_clear' OR NOT EXISTS ("
        "SELECT 1 FROM cell_result_heads replacement_head "
        "JOIN runs replacement_run ON replacement_run.id=replacement_head.run_id "
        "JOIN ops replacement_op ON replacement_op.id=replacement_run.op_id "
        f"WHERE replacement_head.column_id={edit_alias}.column_id "
        f"AND replacement_head.row_id={edit_alias}.row_id "
        f"AND replacement_run.op_id>{edit_alias}.op_id "
        "AND replacement_op.status='applied' "
        "AND COALESCE(json_extract(replacement_op.spec,"
        "'$.replace_existing'),0)=1))"
    )


def _descriptor_boundary_predicate(*, edit_alias: str) -> str:
    """Exclude edits at or before the latest applied descriptor replacement."""

    return (
        "NOT EXISTS ("
        "SELECT 1 FROM run_output_generations boundary_generation "
        "JOIN runs boundary_run ON boundary_run.id=boundary_generation.run_id "
        "JOIN ops boundary_op ON boundary_op.id=boundary_run.op_id "
        f"WHERE boundary_generation.column_id={edit_alias}.column_id "
        "AND boundary_generation.state='sealed' "
        "AND boundary_op.status='applied' "
        f"AND boundary_op.id>={edit_alias}.op_id "
        "AND ("
        "EXISTS (SELECT 1 FROM json_each(COALESCE(json_extract("
        "boundary_op.undo_info,'$.column_types_after'),'{}')) item "
        f"WHERE item.key=CAST({edit_alias}.column_id AS TEXT)) "
        "OR EXISTS (SELECT 1 FROM json_each(COALESCE(json_extract("
        "boundary_op.undo_info,'$.column_formats_after'),'{}')) item "
        f"WHERE item.key=CAST({edit_alias}.column_id AS TEXT)) "
        "OR EXISTS (SELECT 1 FROM json_each(COALESCE(json_extract("
        "boundary_op.undo_info,'$.column_semantic_types_after'),'{}')) item "
        f"WHERE item.key=CAST({edit_alias}.column_id AS TEXT))"
        "))"
    )


def _require_transaction(db: sqlite3.Connection) -> None:
    if not db.in_transaction:
        raise RuntimeError(
            "current-cell projection writes require a caller transaction"
        )


def _delete_obsolete_heads(
    db: sqlite3.Connection, *, target_sql: str, params: list[int]
) -> None:
    """Delete targeted projection rows whose exact authority no longer exists."""

    edit_precedence = live_edit_precedence_predicate(
        edit_alias="source", op_alias="source_op"
    )
    descriptor_precedence = _descriptor_boundary_predicate(edit_alias="source")
    db.execute(
        "DELETE FROM current_cells AS head WHERE "
        + target_sql
        + " AND NOT ((head.origin_kind='source_cell' AND EXISTS ("
        "SELECT 1 FROM cells source "
        "JOIN rows source_row ON source_row.id=source.row_id "
        "JOIN columns source_column ON source_column.id=source.column_id "
        "AND source_column.sheet_id=source_row.sheet_id "
        "WHERE source.column_id=head.column_id AND source.row_id=head.row_id "
        "AND source.producer_id IS head.base_producer_id)) OR "
        "(head.origin_kind='run_result' AND EXISTS ("
        "SELECT 1 FROM cell_result_heads winner "
        "JOIN results result ON result.run_id=winner.run_id "
        "AND result.row_id=winner.row_id AND result.column_id=winner.column_id "
        "JOIN runs run ON run.id=result.run_id "
        "JOIN rows result_row ON result_row.id=result.row_id "
        "JOIN columns result_column ON result_column.id=result.column_id "
        "AND result_column.sheet_id=result_row.sheet_id "
        "WHERE winner.column_id=head.column_id AND winner.row_id=head.row_id "
        "AND winner.run_id=head.origin_run_id)) OR "
        "(head.origin_kind='manual_edit' AND EXISTS ("
        "SELECT 1 FROM edits source "
        "JOIN ops source_op ON source_op.id=source.op_id "
        "JOIN rows edit_row ON edit_row.id=source.row_id "
        "JOIN columns edit_column ON edit_column.id=source.column_id "
        "AND edit_column.sheet_id=edit_row.sheet_id "
        "WHERE source.column_id=head.column_id AND source.row_id=head.row_id "
        "AND source.op_id=head.origin_op_id AND source_op.status='applied' AND "
        + descriptor_precedence
        + " AND "
        + edit_precedence
        + ")))",
        params,
    )


def _register_validity(db: sqlite3.Connection) -> None:
    """Install UDFs before opening a cursor that spans projection batches."""
    db.create_function("frisket_cell_validity", 3, cell_validity, deterministic=True)
    db.create_function(
        "frisket_cell_validity", 2, legacy_cell_validity, deterministic=True
    )


def _retrieval_projection(
    has_retrieval_revision: bool, retrieval_revision: int | None
) -> tuple[str, str]:
    """The retrieval_revision column and value to insert, when the table has it."""
    if not has_retrieval_revision:
        return "", ""
    if retrieval_revision is None or retrieval_revision <= 0:
        raise RuntimeError("retrieval-aware current-cell writes require a revision")
    return ",retrieval_revision", f",{int(retrieval_revision)}"


def _rebuild_column_batch(
    db: sqlite3.Connection,
    *,
    column_ids: list[int],
    retrieval_revision: int | None,
) -> int:
    typed_authorities = any(
        str(row[1]) == "value_kind" for row in db.execute("PRAGMA table_info(cells)")
    )
    projection_columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(current_cells)")
    }
    has_validity = "validity" in projection_columns
    has_inline_scalars = {
        "inline_value_kind",
        "inline_value",
    } <= projection_columns
    has_retrieval_revision = "retrieval_revision" in projection_columns
    retrieval_column, retrieval_value = _retrieval_projection(
        has_retrieval_revision, retrieval_revision
    )
    validity_column = ",validity" if has_validity else ""
    validity_value = (
        (
            ",frisket_cell_validity(descriptor.type,value_kind,value)"
            if typed_authorities
            else ",frisket_cell_validity(descriptor.type,value)"
        )
        if has_validity
        else ""
    )
    source_payload = (
        "source.value_kind,source.value," if typed_authorities else "source.value,"
    )
    result_payload = (
        "CASE result.publication_effect "
        "WHEN 'publish_value' THEN result.value_kind "
        "WHEN 'publish_null' THEN 'null' END,"
        "CASE WHEN result.publication_effect='publish_value' "
        "THEN result.value ELSE NULL END,"
    )
    if not typed_authorities:
        result_payload = (
            "CASE WHEN result.publication_effect='publish_value' "
            "THEN result.value ELSE NULL END,",
        )
    result_payload_sql = "".join(result_payload)
    edit_payload = (
        "source.value_kind,source.value," if typed_authorities else "source.value,"
    )
    insert_payload_column = "" if typed_authorities else "value,"
    select_payload_column = "" if typed_authorities else "value,"
    inline_columns = ",inline_value_kind,inline_value" if has_inline_scalars else ""
    inline_values = ""
    if has_inline_scalars:
        eligible = inline_scalar_eligibility_sql(
            descriptor_alias="descriptor",
            value_kind_sql="ranked.value_kind",
            value_sql="ranked.value",
        )
        inline_values = (
            f",CASE WHEN {eligible} THEN ranked.value_kind END"
            f",CASE WHEN {eligible} THEN ranked.value END"
        )
    update_columns = [
        "origin_kind",
        "origin_op_id",
        "origin_run_id",
        "base_producer_id",
    ]
    if not typed_authorities:
        update_columns.insert(0, "value")
    if has_validity:
        update_columns.append("validity")
    if has_inline_scalars:
        update_columns.extend(("inline_value_kind", "inline_value"))
    update_values = ",".join(f"{column}=excluded.{column}" for column in update_columns)
    if has_retrieval_revision:
        same_head = (
            "current_cells.origin_kind=excluded.origin_kind "
            "AND current_cells.origin_op_id IS excluded.origin_op_id "
            "AND current_cells.origin_run_id IS excluded.origin_run_id "
            "AND current_cells.base_producer_id IS excluded.base_producer_id"
        )
        if has_validity:
            same_head += " AND current_cells.validity IS excluded.validity"
        if has_inline_scalars:
            same_head += (
                " AND current_cells.inline_value_kind IS excluded.inline_value_kind"
                " AND current_cells.inline_value IS excluded.inline_value"
            )
        update_values += (
            ",retrieval_revision=CASE WHEN "
            + same_head
            + " THEN current_cells.retrieval_revision "
            "ELSE excluded.retrieval_revision END"
        )
    edit_precedence = live_edit_precedence_predicate(
        edit_alias="source", op_alias="source_op"
    )
    descriptor_precedence = _descriptor_boundary_predicate(edit_alias="source")
    # SQLite's ``CROSS JOIN ... ON`` is an intentional join-order fence, not
    # a Cartesian product: it keeps the bounded target relation outermost
    # while the ON clauses perform indexed lookups into the large tables.
    db.execute(
        "WITH target_columns(id) AS (VALUES "
        + ",".join("(?)" for _ in column_ids)
        + "), candidates AS ("
        f"SELECT source.column_id,source.row_id,{source_payload}"
        "'source_cell' AS origin_kind,NULL AS origin_op_id,"
        "NULL AS origin_run_id,source.producer_id AS base_producer_id,"
        "0 AS layer_precedence,0 AS origin_precedence "
        + "FROM target_columns target_column JOIN cells source "
        "ON source.column_id=target_column.id "
        + " CROSS JOIN rows source_row ON source_row.id=source.row_id "
        "CROSS JOIN columns source_column ON source_column.id=source.column_id "
        "AND source_column.sheet_id=source_row.sheet_id "
        "UNION ALL "
        f"SELECT result.column_id,result.row_id,{result_payload_sql}"
        "'run_result',run.op_id,result.run_id,NULL,1,result.run_id "
        + "FROM target_columns target_column JOIN cell_result_heads head "
        "ON head.column_id=target_column.id "
        + " CROSS JOIN results result ON result.run_id=head.run_id "
        "AND result.row_id=head.row_id AND result.column_id=head.column_id "
        "CROSS JOIN runs run ON run.id=result.run_id "
        "CROSS JOIN rows result_row ON result_row.id=result.row_id "
        "CROSS JOIN columns result_column ON result_column.id=result.column_id "
        "AND result_column.sheet_id=result_row.sheet_id "
        "UNION ALL "
        f"SELECT source.column_id,source.row_id,{edit_payload}"
        "'manual_edit',source.op_id,NULL,NULL,2,source.op_id "
        + "FROM target_columns target_column JOIN edits source "
        "ON source.column_id=target_column.id "
        + " CROSS JOIN ops source_op ON source_op.id=source.op_id "
        "CROSS JOIN rows edit_row ON edit_row.id=source.row_id "
        "CROSS JOIN columns edit_column ON edit_column.id=source.column_id "
        "AND edit_column.sheet_id=edit_row.sheet_id "
        "WHERE source_op.status='applied' AND "
        + descriptor_precedence
        + " AND "
        + edit_precedence
        + "), ranked AS ("
        "SELECT *,ROW_NUMBER() OVER ("
        "PARTITION BY column_id,row_id "
        "ORDER BY layer_precedence DESC,origin_precedence DESC"
        ") AS rank FROM candidates) "
        "INSERT INTO current_cells "
        f"(column_id,row_id,{insert_payload_column}"
        "origin_kind,origin_op_id,origin_run_id,"
        "base_producer_id" + validity_column + inline_columns + retrieval_column + ") "
        f"SELECT column_id,row_id,{select_payload_column}"
        "origin_kind,origin_op_id,origin_run_id,"
        "base_producer_id" + validity_value + inline_values + retrieval_value + " "
        "FROM ranked CROSS JOIN columns descriptor "
        "ON descriptor.id=ranked.column_id "
        "WHERE rank=1 ON CONFLICT(column_id,row_id) DO UPDATE SET " + update_values,
        column_ids,
    )
    return int(db.execute("SELECT changes()").fetchone()[0])


def rebuild_current_cells(
    db: sqlite3.Connection, *, invalidate_search: bool = True
) -> int:
    """Rebuild the complete projection in bounded column batches.

    Historical migrations that predate the search worklist explicitly suppress
    invalidation; their later search-schema migration seeds a full repair.
    """

    _require_transaction(db)
    _register_validity(db)
    retrieval_revision = None
    if invalidate_search:
        retrieval_revision = enqueue_dirty_scope(db)
    elif any(
        str(row[1]) == "retrieval_revision"
        for row in db.execute("PRAGMA table_info(current_cells)")
    ):
        raise RuntimeError("retrieval-aware rebuilds must invalidate search")
    total = 0
    columns = db.execute("SELECT id FROM columns ORDER BY id")
    while batch := columns.fetchmany(_SQLITE_BIND_LIMIT):
        total += _rebuild_column_batch(
            db,
            column_ids=[int(row[0]) for row in batch],
            retrieval_revision=retrieval_revision,
        )
    _delete_obsolete_heads(db, target_sql="1", params=[])
    return total
