"""Autosaved extraction configuration and current document selection aids."""

from __future__ import annotations

import json

EXTRACTION_LAYOUTS_SCHEMA_SQL = """
-- EXTRACTION_LAYOUTS_BEGIN
CREATE TABLE IF NOT EXISTS extraction_layouts (
  id INTEGER PRIMARY KEY,
  sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
  source_column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL CHECK (ordinal > 0),
  reference_row_id INTEGER REFERENCES rows(id) ON DELETE SET NULL,
  draft TEXT NOT NULL CHECK (json_valid(draft)),
  repeat_group_id TEXT,
  has_applied INTEGER NOT NULL DEFAULT 0 CHECK (has_applied IN (0,1)),
  imported_recipe_id INTEGER UNIQUE,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (sheet_id, source_column_id, ordinal)
);
CREATE TABLE IF NOT EXISTS extraction_layout_selection (
  sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
  source_column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
  layout_id INTEGER NOT NULL REFERENCES extraction_layouts(id) ON DELETE CASCADE,
  PRIMARY KEY (sheet_id, source_column_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS extraction_layout_documents (
  source_column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
  row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
  layout_id INTEGER NOT NULL REFERENCES extraction_layouts(id) ON DELETE CASCADE,
  PRIMARY KEY (source_column_id, row_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_extraction_layout_documents_layout
  ON extraction_layout_documents(layout_id, row_id);
-- EXTRACTION_LAYOUTS_END
"""


def source_column(project, sheet_id, source):
    column = project.db.execute(
        "SELECT c.id,c.name FROM columns c JOIN sheets s ON s.id=c.sheet_id "
        "WHERE c.sheet_id=? AND c.name=? AND c.active=1 AND c.hidden=0 "
        "AND s.hidden=0 AND c.type IN ('file','image')",
        (sheet_id, source),
    ).fetchone()
    if column is None:
        raise ValueError("Choose a visible document column")
    return column


def _layout(row):
    if row is None:
        return None
    result = dict(row)
    result["draft"] = json.loads(result["draft"])
    result["name"] = f"Layout {result['ordinal']}"
    result["has_applied"] = bool(result["has_applied"])
    return result


def get_layout(project, layout_id):
    return _layout(
        project.db.execute(
            "SELECT * FROM extraction_layouts WHERE id=?", (layout_id,)
        ).fetchone()
    )


def list_layouts(project, sheet_id, source_column_id):
    return [
        _layout(row)
        for row in project.db.execute(
            "SELECT * FROM extraction_layouts WHERE sheet_id=? "
            "AND source_column_id=? ORDER BY ordinal",
            (sheet_id, source_column_id),
        )
    ]


def validate_layout_scope(project, layout_id, sheet_id, source):
    layout = get_layout(project, layout_id)
    column = source_column(project, sheet_id, source)
    if (
        layout is None
        or layout["sheet_id"] != sheet_id
        or layout["source_column_id"] != column["id"]
    ):
        raise ValueError("Extraction layout does not belong to this document column")
    return layout


def resolve_extraction_request_scope(
    project, *, sheet_id, source, layout_id=None, scope=None
):
    """Resolve the layout ownership and optional document scope for one request."""
    layout = (
        validate_layout_scope(project, layout_id, sheet_id, source)
        if layout_id is not None
        else None
    )
    if scope is None:
        return layout, None, None
    if scope.kind == "layout":
        if scope.layout_id is None:
            scope = scope.model_copy(update={"layout_id": layout_id})
        elif layout_id is not None and scope.layout_id != layout_id:
            raise ValueError("Scope must use the selected layout")
    row_ids = resolve_document_scope(
        project, sheet_id=sheet_id, source=source, scope=scope
    )
    return layout, scope, row_ids


def selected_layout_id(project, sheet_id, source_column_id):
    row = project.db.execute(
        "SELECT layout_id FROM extraction_layout_selection "
        "WHERE sheet_id=? AND source_column_id=?",
        (sheet_id, source_column_id),
    ).fetchone()
    return row[0] if row else None


def select_layout(project, *, sheet_id, source, layout_id, commit=True):
    layout = validate_layout_scope(project, layout_id, sheet_id, source)
    project.db.execute(
        "INSERT INTO extraction_layout_selection VALUES (?,?,?) "
        "ON CONFLICT(sheet_id,source_column_id) DO UPDATE SET layout_id=excluded.layout_id",
        (sheet_id, layout["source_column_id"], layout_id),
    )
    if commit:
        project.db.commit()


def save_layout(
    project,
    *,
    sheet_id,
    source,
    draft,
    reference_row_id=None,
    repeat_group_id=None,
    layout_id=None,
    imported_recipe_id=None,
):
    """Save a draft atomically; it need not yet be an executable template."""
    column = source_column(project, sheet_id, source)
    if reference_row_id is not None and not project.visible_row_ids(
        sheet_id, [reference_row_id]
    ):
        raise ValueError("Reference document is not in the source sheet")
    db = project.db
    creating = layout_id is None
    db.execute("BEGIN IMMEDIATE")
    try:
        if imported_recipe_id is not None:
            existing = db.execute(
                "SELECT id FROM extraction_layouts WHERE imported_recipe_id=?",
                (imported_recipe_id,),
            ).fetchone()
            if existing is not None:
                db.commit()
                return get_layout(project, existing["id"])
        if layout_id is None:
            ordinal = db.execute(
                "SELECT COALESCE(MAX(ordinal),0)+1 FROM extraction_layouts "
                "WHERE sheet_id=? AND source_column_id=?",
                (sheet_id, column["id"]),
            ).fetchone()[0]
            layout_id = db.execute(
                "INSERT INTO extraction_layouts "
                "(sheet_id,source_column_id,ordinal,reference_row_id,draft,repeat_group_id,imported_recipe_id) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    sheet_id,
                    column["id"],
                    ordinal,
                    reference_row_id,
                    json.dumps(draft, allow_nan=False),
                    repeat_group_id,
                    imported_recipe_id,
                ),
            ).lastrowid
        else:
            validate_layout_scope(project, layout_id, sheet_id, source)
            db.execute(
                "UPDATE extraction_layouts SET reference_row_id=?,draft=?,repeat_group_id=?,"
                "updated_at=datetime('now') WHERE id=?",
                (
                    reference_row_id,
                    json.dumps(draft, allow_nan=False),
                    repeat_group_id,
                    layout_id,
                ),
            )
        if creating:
            select_layout(
                project,
                sheet_id=sheet_id,
                source=source,
                layout_id=layout_id,
                commit=False,
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return get_layout(project, layout_id)


def remember_success(project, layout_id, row_ids, *, commit=False):
    """Remember successful applications, including successful zero-record inputs.

    Call within the ordinary result publication transaction. Preview and draft
    persistence never call this; an empty cohort still marks the layout applied.
    """
    layout = get_layout(project, layout_id)
    if layout is None:
        raise ValueError("Extraction layout not found")
    row_ids = list(dict.fromkeys(row_ids))
    if len(project.visible_row_ids(layout["sheet_id"], row_ids)) != len(row_ids):
        raise ValueError("Applied documents do not belong to the layout source sheet")
    project.db.executemany(
        "INSERT INTO extraction_layout_documents VALUES (?,?,?) "
        "ON CONFLICT(source_column_id,row_id) DO UPDATE SET layout_id=excluded.layout_id",
        [(layout["source_column_id"], row_id, layout_id) for row_id in row_ids],
    )
    project.db.execute(
        "UPDATE extraction_layouts SET has_applied=1 WHERE id=?", (layout_id,)
    )
    if commit:
        project.db.commit()


def _scope_query(project, *, sheet_id, source, scope):
    from frisket.querysets import sheet_row_scope_plan

    values = scope.model_dump() if hasattr(scope, "model_dump") else scope
    column = source_column(project, sheet_id, source)
    kind = values["kind"]
    plan = sheet_row_scope_plan(
        project,
        sheet_id,
        filter_=json.dumps(values.get("filter"))
        if kind == "filter" and values.get("filter") is not None
        else None,
        parent_row_id=values.get("parent_row_id") if kind == "filter" else None,
        row_ids=values.get("scope_row_ids") if kind == "filter" else None,
    )
    # Document eligibility is a live, valid object envelope with a local blob.
    # JSON CASE guards ensure invalid text never enters JSON1 functions.
    envelope = "CASE WHEN d.value_kind='json' AND json_valid(d.value) THEN CASE WHEN json_type(d.value)='object' THEN d.value END END"
    sql = (
        f"FROM {plan.filter_from_sql} JOIN current_cell_values d "
        "ON d.row_id=r.id AND d.column_id=? AND d.validity='valid' "
        f"WHERE {plan.where_sql} AND json_type({envelope},'$.blob')='text' "
        f"AND EXISTS (SELECT 1 FROM blobs b WHERE b.hash=json_extract({envelope},'$.blob'))"
    )
    params = [*plan.filter_join_params, column["id"], *plan.where_params]
    if kind == "this":
        if values.get("row_id") is None:
            raise ValueError("This document scope requires a row")
        sql += " AND r.id=?"
        params.append(values["row_id"])
    elif kind == "layout":
        layout_id = values.get("layout_id")
        validate_layout_scope(project, layout_id, sheet_id, source)
        sql += " AND EXISTS (SELECT 1 FROM extraction_layout_documents a WHERE a.layout_id=? AND a.row_id=r.id AND a.source_column_id=d.column_id)"
        params.append(layout_id)
    elif kind not in {"all", "filter"}:
        raise ValueError("Unknown extraction scope")
    return sql, params


def resolve_document_scope(project, *, sheet_id, source, scope):
    sql, params = _scope_query(project, sheet_id=sheet_id, source=source, scope=scope)
    return [
        row[0]
        for row in project.db.execute(
            "SELECT r.id " + sql + " ORDER BY r.position,r.id", params
        )
    ]


def count_document_scope(project, *, sheet_id, source, scope):
    sql, params = _scope_query(project, sheet_id=sheet_id, source=source, scope=scope)
    return project.db.execute("SELECT COUNT(*) " + sql, params).fetchone()[0]
