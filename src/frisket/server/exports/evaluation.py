"""Single-header comparison and Review CSVs with separate settings metadata."""

from __future__ import annotations

import csv
import io

from frisket.engine.store.prepared_content import decode_logical_value
from frisket.review_predicate import is_exact_review_correction
from frisket.server.exports.sheet_csv import csv_display
from frisket.server.route_errors import RouteError
from frisket.server.services.evaluation_values import full_column


def _csv(rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    for row in rows:
        writer.writerow([csv_display(cell) for cell in row])
    return stream.getvalue()


def comparison_csv(result):
    fields, variants = result["output_columns"], result["variants"]
    header = ["id"]
    for field in fields:
        name = field["current_name"]
        header.append(f"{name}_expected")
        for variant in variants:
            header.extend(
                (f"{name}_{variant['key']}", f"{name}_{variant['key']}_match")
            )

    def rows():
        yield header
        for row_id in result["row_ids"]:
            rid = str(row_id)
            row = [row_id]
            for field in fields:
                key = field["logical_key"]
                row.append(result["expected"].get(rid, {}).get(key))
                for variant in variants:
                    cell = variant["rows"].get(rid, {}).get(key, {"state": "missing"})
                    state = cell["state"]
                    value = (
                        "#FAILED"
                        if state == "error"
                        else cell.get("value")
                        if state in {"value", "null"}
                        else "#NOT_RUN"
                    )
                    verdict = variant["verdicts"][rid][key]
                    row.extend(
                        (
                            value,
                            "yes"
                            if verdict == "match"
                            else "no"
                            if verdict == "miss"
                            else "",
                        )
                    )
            yield row

    return _csv(rows())


def comparison_settings(result):
    return {
        "schema_version": "frisket.comparison_settings.v1",
        "sheet_id": result["sheet_id"],
        "action_id": result["action_id"],
        "row_ids": result["row_ids"],
        "fields": result["link"]["fields"] if result["link"] else [],
        "variants": [
            {
                "key": variant["key"],
                "label": variant["label"],
                "settings": variant["settings"],
            }
            for variant in result["variants"]
        ],
    }


def review_csv(project, run_id):
    run = project.db.execute(
        "SELECT sheet_id FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    if run is None:
        raise RouteError(404, "Review run was not found")
    fields = project.db.execute(
        "SELECT column_id,column_name FROM run_review_fields WHERE run_id=? AND is_primary=1 ORDER BY column_position,column_id",
        (run_id,),
    ).fetchall()
    row_ids = [
        row[0]
        for row in project.db.execute(
            "SELECT DISTINCT result.row_id FROM results result JOIN rows target ON target.id=result.row_id AND target.hidden=0 WHERE result.run_id=? ORDER BY result.row_id",
            (run_id,),
        )
    ]
    header = ["id"]
    values = {}
    for field in fields:
        header.extend((field["column_name"], f"{field['column_name']}_reviewed"))
        current = full_column(project, run["sheet_id"], field["column_id"], row_ids)
        for row in project.db.execute(
            "SELECT row_id,value_kind,value,error,review_decision FROM results WHERE run_id=? AND column_id=?",
            (run_id, field["column_id"]),
        ):
            read = current.get(row["row_id"])
            ref = read.value_ref if read is not None else None
            op = (
                project.db.execute(
                    "SELECT id,status,kind,spec FROM ops WHERE id=?", (ref["op_id"],)
                ).fetchone()
                if ref and ref.get("kind") == "manual_edit" and ref.get("op_id")
                else None
            )
            corrected = is_exact_review_correction(
                run_id=run_id,
                row_id=row["row_id"],
                column_id=field["column_id"],
                ref=ref,
                op=dict(op) if op else None,
            )
            value = (
                read.value
                if corrected and read is not None
                else "#FAILED"
                if row["error"]
                else decode_logical_value(project.db, row["value_kind"], row["value"])
            )
            decision = row["review_decision"]
            values[(row["row_id"], field["column_id"])] = (
                value,
                "yes"
                if decision == "accept"
                else "no"
                if decision in {"reject", "reject_clear", "edit"}
                else "",
            )

    def rows():
        yield header
        for row_id in row_ids:
            row = [row_id]
            for field in fields:
                row.extend(values.get((row_id, field["column_id"]), (None, "")))
            yield row

    return _csv(rows())
