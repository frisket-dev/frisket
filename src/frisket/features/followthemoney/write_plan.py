"""Lower a planned entity dataset to the shared additive table writer."""

from typing import Any

from frisket.contracts.plugin_write_plan import MAX_ROWS_PER_APPEND, WritePlan


def import_plan_to_write_plan(plan: dict[str, Any]) -> WritePlan:
    """Preserve schema, technical columns, rows, and stable sheet ordering."""
    ops: list[dict[str, Any]] = []
    ordered_sheets = [
        *plan.get("sheets", []),
        *plan.get("unsupported_sheets", []),
        *plan.get("relationship_sheets", []),
    ]
    for sheet_index, sheet in enumerate(ordered_sheets):
        sheet_ref = f"s{sheet_index}"
        ops.append(
            {"op": "create_sheet", "sheet_ref": sheet_ref, "name": sheet["sheet_name"]}
        )
        column_ref_by_name: dict[str, str] = {}
        for column_index, column in enumerate(sheet["columns"]):
            column_ref = f"{sheet_ref}_c{column_index}"
            column_ref_by_name[column["name"]] = column_ref
            ops.append(
                {
                    "op": "create_column",
                    "sheet_ref": sheet_ref,
                    "column_ref": column_ref,
                    "name": column["name"],
                    "type": column["type"],
                    "hidden": bool(column.get("hidden")),
                }
            )
        rows = [
            {
                column_ref_by_name[name]: value
                for name, value in row["values"].items()
                if name in column_ref_by_name
            }
            for row in sheet["rows"]
        ]
        for start in range(0, len(rows), MAX_ROWS_PER_APPEND):
            ops.append(
                {
                    "op": "append_rows",
                    "sheet_ref": sheet_ref,
                    "rows": rows[start : start + MAX_ROWS_PER_APPEND],
                }
            )
    return WritePlan.model_validate({"ops": ops})
