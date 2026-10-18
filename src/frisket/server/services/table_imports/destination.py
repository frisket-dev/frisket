"""Destination validation, comparison, and exact existing-key snapshots."""

from __future__ import annotations

import os
import secrets
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from frisket.engine.store.table_import_intake import table_dedup_key
from frisket.engine.store.value_codec import decode_stored_value
from frisket.table_import import TableReadSettings, convert_cell, open_table

from .storage import TableImportDraftError


def compare_destination(
    workspace: Any,
    project_id: str,
    directory: Path,
    header: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    source_path: Path,
    settings: TableReadSettings,
    scan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    project = workspace.get(project_id)
    choices = {
        int(item["source_index"]): item
        for item in header["column_choices"]
        if item.get("included")
    }
    destination = request["destination"]
    kind = destination.get("kind")
    targets: dict[int, Any] = {}
    if kind == "new":
        if not str(destination.get("sheet_name") or "").strip():
            raise _error(422, "invalid_destination", "A new sheet name is required.")
    elif kind == "existing" and type(destination.get("sheet_id")) is int:
        sheet_id = int(destination["sheet_id"])
        if (
            project.db.execute(
                "SELECT 1 FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
            ).fetchone()
            is None
        ):
            raise _error(
                409, "destination_changed", "The destination sheet is unavailable."
            )
        targets = {
            int(row["id"]): row
            for row in project.db.execute(
                "SELECT id,name,type FROM columns "
                "WHERE sheet_id=? AND active=1 AND hidden=0",
                (sheet_id,),
            )
        }
    else:
        raise _error(422, "invalid_destination", "The import destination is invalid.")

    mappings = request.get("mappings", [])
    seen_sources: set[int] = set()
    seen_targets: set[int] = set()
    new_target_names: set[str] = set()
    existing_target_names = {str(row["name"]) for row in targets.values()}
    reserve_extra_values = needs_extra_values(scan)
    if reserve_extra_values and "extra values" in existing_target_names:
        raise _error(
            409,
            "destination_changed",
            "The destination already contains the reserved 'extra values' column.",
        )
    matched = new = clashes = 0
    for mapping in mappings:
        source_index = mapping.get("source_index")
        if source_index not in choices or source_index in seen_sources:
            raise _error(
                422,
                "invalid_destination",
                "Mappings must name each imported column once.",
            )
        seen_sources.add(source_index)
        target = mapping.get("target", {})
        if target.get("kind") == "new":
            target_name = str(target.get("name") or "").strip()
            if not target_name:
                raise _error(422, "invalid_destination", "New columns require names.")
            if (
                (reserve_extra_values and target_name == "extra values")
                or target_name in new_target_names
                or target_name in existing_target_names
            ):
                raise _error(
                    422,
                    "invalid_destination",
                    "Destination column names must be unique; "
                    "'extra values' is reserved for overflow values.",
                )
            new_target_names.add(target_name)
            target["name"] = target_name
            new += 1
            continue
        if kind == "new":
            raise _error(
                422,
                "invalid_destination",
                "A new sheet can only contain new columns.",
            )
        if (
            target.get("kind") != "existing"
            or type(target.get("column_id")) is not int
            or int(target["column_id"]) not in targets
        ):
            raise _error(
                409,
                "destination_changed",
                "A mapped destination column is unavailable.",
            )
        target_id = int(target["column_id"])
        if target_id in seen_targets:
            raise _error(
                422,
                "invalid_destination",
                "Mappings cannot target a column twice.",
            )
        seen_targets.add(target_id)
        if str(targets[target_id]["name"]) == "extra values":
            raise _error(
                422,
                "invalid_destination",
                "'extra values' is reserved for overflow values.",
            )
        if str(targets[target_id]["type"]) != str(choices[source_index]["target_type"]):
            clashes += 1
        else:
            matched += 1
    if seen_sources != set(choices):
        raise _error(
            422,
            "invalid_destination",
            "Mappings must review every imported column.",
        )

    duplicate_rows = 0
    import_rows = int(scan["data_rows"]) if scan and scan.get("complete") else None
    complete = bool(scan and scan.get("complete"))
    dedup = request.get("dedup", {})
    if dedup.get("enabled"):
        key = dedup.get("key_source_index")
        if type(key) is not int or key not in choices:
            raise _error(
                422,
                "invalid_dedup",
                "Choose an imported key column for duplicate checking.",
            )
        key_mapping = next(item for item in mappings if item["source_index"] == key)
        target = key_mapping["target"]
        if target["kind"] != "existing":
            raise _error(
                422,
                "invalid_dedup",
                "Duplicate checking requires an existing key column.",
            )
        if not complete:
            return {
                "state": "running",
                "matched_columns": matched,
                "new_columns": new,
                "type_clashes": clashes,
                "duplicate_rows": None,
                "import_rows": None,
                "complete": False,
                "error": None,
            }
        duplicate_rows = count_existing_duplicates(
            project,
            directory,
            header,
            int(key),
            int(target["column_id"]),
            source_path=source_path,
            settings=settings,
            total_record_count=int(scan["record_count"]),
        )
        import_rows = max(0, int(scan["data_rows"]) - duplicate_rows)
    return {
        "state": "ready",
        "matched_columns": matched,
        "new_columns": new,
        "type_clashes": clashes,
        "duplicate_rows": duplicate_rows,
        "import_rows": import_rows,
        "complete": complete,
        "error": None,
    }


def count_existing_duplicates(
    project: Any,
    directory: Path,
    header: Mapping[str, Any],
    source_index: int,
    target_column_id: int,
    *,
    source_path: Path,
    settings: TableReadSettings,
    total_record_count: int,
) -> int:
    scratch = directory / f".dedup-{os.getpid()}-{secrets.token_hex(4)}.db"
    connection = sqlite3.connect(scratch)
    try:
        connection.execute("CREATE TABLE existing_keys(value TEXT PRIMARY KEY)")
        connection.executemany(
            "INSERT OR IGNORE INTO existing_keys(value) VALUES (?)",
            ((value,) for value in iter_existing_dedup_keys(project, target_column_id)),
        )
        connection.commit()
        choice = next(
            item
            for item in header["column_choices"]
            if int(item["source_index"]) == source_index
        )
        count = 0
        with open_table(
            source_path, settings, total_record_count=total_record_count
        ) as reader:
            for row in reader:
                if row.role != "data":
                    continue
                converted = convert_cell(
                    row.cells[source_index],
                    choice["target_type"],
                    decimal_separator=settings.decimal_separator,
                    thousands_separator=settings.thousands_separator,
                    date_order=choice.get("date_order") or "month_first",
                )
                key = table_dedup_key(converted.value)
                if key is None:
                    continue
                count += (
                    connection.execute(
                        "SELECT 1 FROM existing_keys WHERE value=?", (key,)
                    ).fetchone()
                    is not None
                )
        return count
    finally:
        connection.close()
        scratch.unlink(missing_ok=True)


def iter_existing_dedup_keys(project: Any, target_column_id: int) -> Iterable[str]:
    rows = project.db.execute(
        "SELECT live.value_kind,live.value FROM current_cell_values live "
        "JOIN rows r ON r.id=live.row_id "
        "JOIN columns c ON c.id=live.column_id "
        "WHERE live.column_id=? AND r.hidden=0 AND c.active=1 AND c.hidden=0",
        (target_column_id,),
    )
    for row in rows:
        value = decode_stored_value(
            row["value_kind"], row["value"], tolerate_errors=True
        )
        key = table_dedup_key(value)
        if key is not None:
            yield key


def public_destination(
    workspace: Any,
    header: Mapping[str, Any],
    columns: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    request = header["destination_request"]
    by_index = {int(item["source_index"]): item for item in columns}
    mappings = []
    project = workspace.get(header["project_id"])
    target_rows: dict[int, Any] = {}
    if request["destination"]["kind"] == "existing":
        sheet_id = int(request["destination"]["sheet_id"])
        target_rows = {
            int(row["id"]): row
            for row in project.db.execute(
                "SELECT id,name,type FROM columns "
                "WHERE sheet_id=? AND active=1 AND hidden=0",
                (sheet_id,),
            )
        }
    for raw in request["mappings"]:
        source = by_index[int(raw["source_index"])]
        target = raw["target"]
        match = "new"
        detail = None
        if target["kind"] == "existing":
            row = target_rows.get(int(target["column_id"]))
            if row is None:
                match, detail = "type_clash", "Destination column is unavailable."
            elif str(row["type"]) != source["target_type"]:
                match, detail = "type_clash", f"Sheet column is {row['type']}."
            else:
                match = (
                    "matched"
                    if _normalized_name(source["name"])
                    == _normalized_name(str(row["name"]))
                    else "similar"
                )
        mappings.append(
            {
                "source_index": source["source_index"],
                "source_name": source["name"],
                "source_type": source["target_type"],
                "target": target,
                "match": match,
                "detail": detail,
            }
        )
    return {
        "configuration": request["destination"],
        "mappings": mappings,
        "dedup": request["dedup"],
        "comparison": request["comparison"],
    }


def new_destination(scan: Mapping[str, Any], sheet_name: str) -> dict[str, Any]:
    mappings = [
        {
            "source_index": int(item["column"]["index"]),
            "target": {"kind": "new", "name": item["column"]["name"]},
        }
        for item in scan["stats"]["columns"]
        if item["suggested_type"] != "omit"
    ]
    complete = bool(scan.get("complete"))
    names = [str(item["target"]["name"]) for item in mappings]
    valid_names = len(names) == len(set(names)) and not (
        needs_extra_values(scan) and "extra values" in names
    )
    return {
        "destination": {"kind": "new", "sheet_name": sheet_name},
        "mappings": mappings,
        "dedup": {"enabled": False, "key_source_index": None},
        "comparison": {
            "state": "ready" if valid_names else "error",
            "matched_columns": 0,
            "new_columns": len(mappings),
            "type_clashes": 0,
            "duplicate_rows": 0,
            "import_rows": int(scan["data_rows"]) if complete else None,
            "complete": complete,
            "error": (
                None
                if valid_names
                else "Destination column names must be unique; "
                "'extra values' is reserved for overflow values."
            ),
        },
    }


def _normalized_name(value: str) -> str:
    return "".join(
        character for character in value.casefold() if character not in {" ", "_"}
    )


def needs_extra_values(scan: Mapping[str, Any] | None) -> bool:
    return bool(
        scan is None
        or not scan.get("complete")
        or int(scan.get("issue_counts", {}).get("extra_values", 0)) > 0
    )


def _error(status_code: int, code: str, message: str) -> TableImportDraftError:
    return TableImportDraftError(status_code, {"code": code, "message": message})


__all__ = [
    "compare_destination",
    "iter_existing_dedup_keys",
    "new_destination",
    "needs_extra_values",
    "public_destination",
]
