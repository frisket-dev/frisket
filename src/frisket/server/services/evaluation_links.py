"""Live expected-sheet lookup, without materializing joined target values."""

from __future__ import annotations

from collections import defaultdict

from frisket.contracts.http.evaluation import (
    ExpectedLinkInput,
    ExpectedLinkPreviewInput,
)
from frisket.engine.executor.joined_table import (
    _NULL_KEY,
    _normalize_key_cell,
    _pair_mode,
)
from frisket.engine.store import evaluation as store
from frisket.engine.store.current_value_windows import read_current_column
from frisket.server.route_errors import RouteError
from frisket.server.services.evaluation_values import full_column


def sheet(project, sheet_id):
    result = project.db.execute(
        "SELECT id,name FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
    ).fetchone()
    if result is None:
        raise RouteError(404, "Expected answer sheet was not found")
    return result


def column(project, column_id, sheet_id):
    result = project.get_column(column_id)
    if result is None or result["sheet_id"] != sheet_id or not result["active"]:
        raise RouteError(
            409, "Expected answer column is missing or belongs to another sheet"
        )
    return result


def link_payload(project, value):
    if value is None:
        raise RouteError(404, "Expected answer link was not found")
    return {
        **value,
        "target_sheet_name": sheet(project, value["target_sheet_id"])["name"],
        "expected_sheet_name": sheet(project, value["expected_sheet_id"])["name"],
    }


def validate(project, raw, *, preview=False):
    model = ExpectedLinkPreviewInput if preview else ExpectedLinkInput
    body = model.model_validate(raw).model_dump(mode="json")
    sheet(project, body["target_sheet_id"])
    sheet(project, body["expected_sheet_id"])
    for pair in body["keys"]:
        column(project, pair["expected_column_id"], body["expected_sheet_id"])
        if pair["target_column_id"] is not None:
            column(project, pair["target_column_id"], body["target_sheet_id"])
        elif any(field["source"] != "disagreements" for field in body["fields"]):
            raise RouteError(
                422, "Stable row keys are reserved for disagreement answers"
            )
    for field in body["fields"]:
        column(project, field["expected_column_id"], body["expected_sheet_id"])
        column(project, field["output_column_id"], body["target_sheet_id"])
    return body


def row_matches(project, body):
    """Return one-to-one matches plus cardinality facts over current keys."""
    target_id, expected_id = body["target_sheet_id"], body["expected_sheet_id"]
    target_rows = project.visible_row_ids(target_id)
    expected_rows = project.visible_row_ids(expected_id)
    pairs = []
    for pair in body["keys"]:
        ec = column(project, pair["expected_column_id"], expected_id)
        tc = (
            column(project, pair["target_column_id"], target_id)
            if pair["target_column_id"] is not None
            else None
        )
        expected = full_column(project, expected_id, ec["id"], expected_rows)
        target = full_column(project, target_id, tc["id"], target_rows) if tc else None
        mode = _pair_mode(ec["type"], tc["type"] if tc else "integer")
        pairs.append((expected, target, mode))

    def index(rows, expected):
        result = defaultdict(list)
        for row_id in rows:
            values = []
            for left, right, mode in pairs:
                read = (
                    left.get(row_id)
                    if expected
                    else right.get(row_id)
                    if right is not None
                    else None
                )
                raw = (
                    read.value
                    if read is not None and read.value_present
                    else row_id
                    if not expected and right is None
                    else None
                )
                value = _normalize_key_cell(raw, mode)
                if value is _NULL_KEY:
                    break
                values.append(value)
            else:
                result[tuple(values)].append(row_id)
        return result

    ei, ti = index(expected_rows, True), index(target_rows, False)
    mapping, matched, ambiguous_e, ambiguous_t = {}, [], [], []
    for key, erows in ei.items():
        trows = ti.get(key, [])
        if len(trows) > 1:
            ambiguous_e.extend(erows)
        if len(erows) > 1 and trows:
            ambiguous_t.extend(trows)
        if len(erows) == len(trows) == 1:
            mapping[trows[0]] = erows[0]
            matched.extend(erows)
    found = {row_id for key, erows in ei.items() if key in ti for row_id in erows}
    unmatched = [row_id for row_id in expected_rows if row_id not in found]
    preview = {
        "matched": len(matched),
        "not_found": len(unmatched),
        "ambiguous_expected": len(set(ambiguous_e)),
        "ambiguous_target": len(set(ambiguous_t)),
        "unmatched_expected_row_ids": unmatched[:100],
        "ambiguous_expected_row_ids": sorted(set(ambiguous_e))[:100],
        "ambiguous_target_row_ids": sorted(set(ambiguous_t))[:100],
    }
    return mapping, preview


def require_unambiguous(preview):
    if preview["ambiguous_expected"] or preview["ambiguous_target"]:
        raise RouteError(
            409, "Expected answer keys are ambiguous; add another key column"
        )


def resolve(project, link, row_ids):
    body = validate(project, link)
    mapping, preview = row_matches(project, body)
    require_unambiguous(preview)
    rows = {str(row_id): {} for row_id in row_ids}
    requested_expected_rows = [
        mapping[row_id] for row_id in row_ids if row_id in mapping
    ]
    for field in body["fields"]:
        values = full_column(
            project,
            body["expected_sheet_id"],
            field["expected_column_id"],
            requested_expected_rows,
        )
        for target_row_id in row_ids:
            expected_row_id = mapping.get(target_row_id)
            read = values.get(expected_row_id)
            value = read.value if read is not None and read.value_present else None
            rows[str(target_row_id)][field["logical_key"]] = {
                "value": value,
                "source_row_id": expected_row_id,
                "source_column_id": field["expected_column_id"],
                "checked": value is not None and value != "",
            }
    return rows


def projection(project, sheet_id, row_ids):
    sheet(project, sheet_id)
    if set(project.visible_row_ids(sheet_id, row_ids)) != set(row_ids):
        raise RouteError(422, "Projection rows must belong to the target sheet")
    links = [link_payload(project, item) for item in store.links(project, sheet_id)]
    columns, rows = [], {str(row_id): {} for row_id in row_ids}
    for link in links:
        cells = resolve(
            project,
            {
                key: value
                for key, value in link.items()
                if key not in {"id", "target_sheet_name", "expected_sheet_name"}
            },
            row_ids,
        )
        for field in link["fields"]:
            virtual_id = f"expected:{link['id']}:{field['expected_column_id']}"
            descriptor = column(
                project, field["expected_column_id"], link["expected_sheet_id"]
            )
            columns.append(
                {
                    "id": virtual_id,
                    "link_id": link["id"],
                    "expected_column_id": field["expected_column_id"],
                    "output_column_id": field["output_column_id"],
                    "logical_key": field["logical_key"],
                    "name": descriptor["name"],
                    "rule": field["rule"],
                    "hidden": field["hidden"] or link["hidden"],
                    "source": field["source"],
                }
            )
            for row_id in row_ids:
                rows[str(row_id)][virtual_id] = cells[str(row_id)][field["logical_key"]]
    return {"links": links, "columns": columns, "rows": rows}


def checked_rows(project, link_id, limit):
    """Resolve keys once; count checked rows without loading full expected text."""
    link = store.link(project, link_id)
    if link is None:
        raise RouteError(404, "Expected answer link was not found")
    body = validate(project, {key: value for key, value in link.items() if key != "id"})
    mapping, preview = row_matches(project, body)
    require_unambiguous(preview)
    matched_targets = sorted(mapping)
    selected, total = [], 0
    for start in range(0, len(matched_targets), 500):
        targets = matched_targets[start : start + 500]
        expected_ids = [mapping[row_id] for row_id in targets]
        checked = set()
        for field in body["fields"]:
            reads = read_current_column(
                project.db,
                sheet_id=body["expected_sheet_id"],
                column_id=field["expected_column_id"],
                row_ids=expected_ids,
                bounded=True,
                include_hidden=True,
            )
            for target_row_id in targets:
                read = reads.get(mapping[target_row_id])
                if read is not None and (
                    read.bounded_value is not None
                    or (
                        read.value_present
                        and read.value is not None
                        and read.value != ""
                    )
                ):
                    checked.add(target_row_id)
            if len(checked) == len(targets):
                break
        for row_id in targets:
            if row_id in checked:
                total += 1
                if len(selected) < limit:
                    selected.append(row_id)
    return {"row_ids": selected, "total": total}
