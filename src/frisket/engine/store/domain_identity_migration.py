"""Preserve durable object identities during the domain schema cutover.

The enclosing migration owns writer exclusion, PRAGMAs, transaction and schema
stamp. This helper neither commits nor changes object visibility/history.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator


IDENTITY_TABLES = (
    "sheets",
    "columns",
    "rows",
    "ops",
    "base_cell_producers",
    "prepared_page_versions",
    "prepared_content_sets",
    "prepared_content_refs",
)

# Historical coordinates intentionally survive without live-object foreign keys.
_REFERENCE_NAMES = {
    "ops": {"op_id", "origin_op_id", "parent_op_id", "source_op_id", "producing_op_id"},
    "base_cell_producers": {"producer_id", "base_producer_id"},
    "sheets": {
        "sheet_id",
        "source_sheet_id",
        "target_sheet_id",
        "parent_sheet_id",
        "text_sheet_id",
        "expected_sheet_id",
    },
    "columns": {
        "column_id",
        "source_column_id",
        "target_column_id",
        "input_column_id",
        "output_column_id",
        "text_column_id",
        "expected_column_id",
        "title_column_id",
    },
    "rows": {
        "row_id",
        "source_row_id",
        "target_row_id",
        "parent_row_id",
        "materialized_row_id",
        "text_row_id",
        "left_row_id",
        "right_row_id",
    },
    "prepared_page_versions": set(),
    "prepared_content_sets": set(),
    "prepared_content_refs": {"prepared_ref_id", "provenance_ref_id"},
}
_JSON_COLUMNS = {
    "ops": ("spec", "undo_info"),
    "receipts": ("body",),
    "views": ("spec",),
    "lenses": ("spec",),
    "source_artifacts": ("external_ref_json",),
    "source_spans": ("selector_json",),
    "text_surfaces": ("value_ref_json", "surface_ref_json"),
    "evidence_links": ("subject_ref_json", "producer_json"),
    "watches": ("query_json", "query", "detection_policy"),
    "watch_runs": ("resolved_query",),
    "watch_run_events": ("subject_ref",),
    "saved_compare_variants": ("rows_json", "output_columns_json"),
}
_ID_LISTS = {
    "sheet_ids": "sheets",
    "created_sheets": "sheets",
    "column_ids": "columns",
    "created_columns": "columns",
    "row_ids": "rows",
    "created_rows": "rows",
    "hidden_rows": "rows",
}
_CONTAINERS = {
    "scope",
    "ref",
    "inputs",
    "outputs",
    "reads",
    "sources",
    "source",
    "target",
    "source_refs",
    "source_cells",
    "subject_ref",
    "query",
    "original_source_cell",
    "before",
    "after",
}


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _positive_id(value: object) -> int:
    return value if type(value) is int and 0 < value < 2**63 else 0


def _json_ids(value: object) -> Iterator[tuple[str, int]]:
    """Read typed contract locations, never arbitrary params or user payloads."""
    if isinstance(value, list):
        for item in value:
            yield from _json_ids(item)
    elif isinstance(value, dict):
        for name, item in value.items():
            for table, names in _REFERENCE_NAMES.items():
                if name in names:
                    yield table, _positive_id(item)
            if name in _ID_LISTS:
                values = item.values() if isinstance(item, dict) else item
                if isinstance(values, (list, tuple)) or isinstance(item, dict):
                    for identifier in values:
                        yield _ID_LISTS[name], _positive_id(identifier)
            elif (
                name in {"source_cell", "text_cell"}
                and isinstance(item, list)
                and len(item) == 3
            ):
                for table, identifier in zip(("sheets", "rows", "columns"), item):
                    yield table, _positive_id(identifier)
            elif name in _CONTAINERS:
                yield from _json_ids(item)


def _allocation_floors(db: sqlite3.Connection) -> dict[str, int]:
    floors = dict.fromkeys(IDENTITY_TABLES, 0)
    tables = [
        str(row[0])
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    for table in tables:
        columns = {
            str(row[1]) for row in db.execute(f"PRAGMA table_info({_quoted(table)})")
        }
        refs: set[tuple[str, str]] = set()
        if table in floors:
            refs.add((table, "id"))
        for target, names in _REFERENCE_NAMES.items():
            refs.update((target, name) for name in columns & names)
        for fk in db.execute(f"PRAGMA foreign_key_list({_quoted(table)})"):
            if fk[2] in floors and fk[4] == "id":
                refs.add((str(fk[2]), str(fk[3])))
        if table in {"cells", "results", "edits"}:
            value = db.execute(
                f"SELECT MAX(value) FROM {_quoted(table)} "
                "WHERE value_kind='prepared_content_ref' AND typeof(value)='integer'"
            ).fetchone()[0]
            floors["prepared_content_refs"] = max(
                floors["prepared_content_refs"], _positive_id(value)
            )
        for target, column in refs:
            value = db.execute(
                f"SELECT MAX({_quoted(column)}) FROM {_quoted(table)}"
            ).fetchone()[0]
            floors[target] = max(floors[target], _positive_id(value))
        for column in columns & set(_JSON_COLUMNS.get(table, ())):
            for row in db.execute(
                f"SELECT {_quoted(column)} FROM {_quoted(table)} WHERE {_quoted(column)} IS NOT NULL"
            ):
                try:
                    value = json.loads(row[0])
                except (ValueError, TypeError):
                    continue
                # These two contracts are bare ID lists/maps, not arbitrary JSON.
                if table == "saved_compare_variants":
                    value = {
                        "row_ids" if column == "rows_json" else "column_ids": value
                    }
                for target, identifier in _json_ids(value):
                    floors[target] = max(floors[target], identifier)
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'"
    ).fetchone():
        for table, value in db.execute("SELECT name,seq FROM sqlite_sequence"):
            if table in floors:
                floors[table] = max(floors[table], _positive_id(value))
    return floors


def migrate_identity_high_water(db: sqlite3.Connection) -> dict[str, int]:
    """Rebuild only durable identity tables; preserve data and allocator floors.

    Call before creating new cutover objects and before pruning old references.
    The caller restores PRAGMAs and checks foreign keys before its final commit.
    """
    if not db.in_transaction:
        raise RuntimeError("identity migration requires a caller transaction")
    if db.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise RuntimeError(
            "identity migration requires foreign keys disabled before BEGIN"
        )
    if not db.execute("PRAGMA legacy_alter_table").fetchone()[0]:
        raise RuntimeError(
            "identity migration requires legacy_alter_table during table rebuild"
        )
    floors = _allocation_floors(db)
    for table in IDENTITY_TABLES:
        original = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if original is None:
            raise RuntimeError(f"identity migration missing {table}")
        sql = str(original[0])
        if not re.search(
            r"\bid\s+INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", sql, re.IGNORECASE
        ):
            replacement = f"{table}_identity_new"
            create, count = re.subn(
                r"^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:\""
                + table
                + r"\"|"
                + table
                + r")",
                f"CREATE TABLE {_quoted(replacement)}",
                sql,
                count=1,
                flags=re.IGNORECASE,
            )
            create, identity_count = re.subn(
                r"\bid\s+INTEGER\s+PRIMARY\s+KEY\b",
                "id INTEGER PRIMARY KEY AUTOINCREMENT",
                create,
                count=1,
                flags=re.IGNORECASE,
            )
            if count != 1 or identity_count != 1:
                raise RuntimeError(f"unexpected identity declaration for {table}")
            objects = [
                str(row[0])
                for row in db.execute(
                    "SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') "
                    "AND sql IS NOT NULL ORDER BY type,name",
                    (table,),
                )
            ]
            names = ",".join(
                _quoted(str(row[1]))
                for row in db.execute(f"PRAGMA table_info({_quoted(table)})")
            )
            db.execute(create)
            db.execute(
                f"INSERT INTO {_quoted(replacement)}({names}) SELECT {names} FROM {_quoted(table)}"
            )
            db.execute(f"DROP TABLE {_quoted(table)}")
            db.execute(f"ALTER TABLE {_quoted(replacement)} RENAME TO {_quoted(table)}")
            for statement in objects:
                db.execute(statement)
        changed = db.execute(
            "UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name=?",
            (floors[table], table),
        ).rowcount
        if not changed:
            db.execute(
                "INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)",
                (table, floors[table]),
            )
    return floors
