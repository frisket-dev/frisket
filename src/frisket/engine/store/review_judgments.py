"""Review ownership per result and field; payloads remain in Change records."""

from __future__ import annotations

import json
import hashlib
from typing import Any

JUDGMENT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS result_judgment_changes (
 op_id INTEGER NOT NULL REFERENCES ops(id) ON DELETE CASCADE,
 target TEXT NOT NULL,
 property TEXT NOT NULL CHECK(property IN ('state','decision','note')),
 PRIMARY KEY(target,property,op_id)
);
CREATE INDEX IF NOT EXISTS idx_judgment_change_op ON result_judgment_changes(op_id);
"""


def result_key(
    run_id: int | None, copied_op_id: int | None, row_id: int, column_id: int
) -> str:
    if (run_id is None) == (copied_op_id is None):
        raise ValueError("select exactly one run_id or copied_op_id")
    return (
        f"{run_id}:{row_id}:{column_id}"
        if run_id is not None
        else f"copy:{copied_op_id}:{row_id}:{column_id}"
    )


def parse_result_key(key: str) -> tuple[str, int, int, int]:
    parts = key.split(":")
    kind = "copy" if parts[0] == "copy" else "run"
    if kind == "copy":
        parts = parts[1:]
    if len(parts) != 3 or not all(p.isdecimal() and int(p) > 0 for p in parts):
        raise ValueError(f"invalid review target: {key!r}")
    return kind, *(int(p) for p in parts)


def _value(info, target, field, after):
    suffix = "_after" if after else ""
    if field == "state":
        return info["review_states" + suffix][target]
    return info["review_metadata" + suffix][target].get(field)


def record_judgment_changes(db, op_id: int, info) -> None:
    if not db.in_transaction:
        raise RuntimeError("judgment changes require a caller transaction")
    for field in ("state", "decision", "note"):
        section = "review_states" if field == "state" else "review_metadata"
        for target in info.get(section + "_after", {}):
            parse_result_key(target)
            if target not in info.get(section, {}):
                raise ValueError("judgment change lacks its before value")
            # Only explicitly written fields acquire ownership (even identical values).
            if field == "state" or field in info[section + "_after"][target]:
                db.execute(
                    "INSERT OR IGNORE INTO result_judgment_changes VALUES (?,?,?)",
                    (op_id, target, field),
                )


def project_judgments(project: Any, op_id: int) -> None:
    """Recompute only targets touched by a Change, after its applicability changes."""
    db = project.db
    if not db.in_transaction:
        raise RuntimeError("judgment projection requires a caller transaction")
    targets = db.execute(
        "SELECT DISTINCT target FROM result_judgment_changes WHERE op_id=?", (op_id,)
    ).fetchall()
    for (target,) in targets:
        fields = {}
        rows = db.execute(
            "WITH ordered AS (SELECT c.property,o.status,o.undo_info,"
            "FIRST_VALUE(o.undo_info) OVER (PARTITION BY c.property ORDER BY o.change_order) baseline,"
            "ROW_NUMBER() OVER (PARTITION BY c.property ORDER BY (o.status='applied') DESC,o.change_order DESC) rank "
            "FROM result_judgment_changes c JOIN ops o ON o.id=c.op_id "
            "WHERE c.target=? AND o.change_order IS NOT NULL) "
            "SELECT property,status,undo_info,baseline FROM ordered WHERE rank=1",
            (target,),
        )
        for field, status, info, baseline in rows:
            active = status == "applied"
            fields[field] = _value(
                json.loads(info if active else baseline), target, field, active
            )
        kind, owner, row_id, column_id = parse_result_key(target)
        table, owner_field = (
            ("results", "run_id")
            if kind == "run"
            else ("copied_result_outcomes", "op_id")
        )
        before = db.execute(
            f"SELECT review_state,review_decision,review_note FROM {table} WHERE {owner_field}=? AND row_id=? AND column_id=?",
            (owner, row_id, column_id),
        ).fetchone()
        if before is None:
            raise ValueError(f"review target unavailable: {target}")
        state, decision, note = (
            fields.get(key, before[i])
            for i, key in enumerate(("state", "decision", "note"))
        )
        if kind == "run":
            from .runs import RunResultStore

            store = RunResultStore(project)
            store.set_result_review_state(owner, row_id, column_id, state, commit=False)
            store.set_result_review_metadata(
                owner, row_id, column_id, decision, note, commit=False
            )
        else:
            db.execute(
                "UPDATE copied_result_outcomes SET review_state=?,review_decision=?,review_note=? WHERE op_id=? AND row_id=? AND column_id=?",
                (state, decision, note, owner, row_id, column_id),
            )
    if targets:
        from .current_cells import refresh_current_cell_pairs

        # This is a guard for readers that consume judgments. Value-only
        # fingerprints still compare the same immutable cell contributions.
        refresh_current_cell_pairs(
            db,
            [
                (parse_result_key(target)[2], parse_result_key(target)[3])
                for (target,) in targets
            ],
        )


def decision_scope_identity(
    db, run_id: int, column_ids, row_ids
) -> tuple[set[int], str]:
    """Stream effective judgment identities for one admitted saved-result scope."""
    digest = hashlib.sha256()
    owners: set[int] = set()
    rows = sorted(set(row_ids))
    columns = json.dumps(sorted(set(column_ids)))
    for offset in range(0, len(rows), 500):
        for row in db.execute(
            "SELECT wanted.value row_id,cols.value column_id,result.review_state,result.review_decision,"
            "coalesce((SELECT contribution.op_id FROM result_judgment_changes contribution "
            "JOIN ops op ON op.id=contribution.op_id "
            "WHERE contribution.target=(?||':'||wanted.value||':'||cols.value) "
            "AND contribution.property='decision' AND op.status='applied' AND op.change_order IS NOT NULL "
            "ORDER BY op.change_order DESC LIMIT 1),0) owner "
            "FROM json_each(?) wanted CROSS JOIN json_each(?) cols "
            "LEFT JOIN results result ON result.run_id=? AND result.row_id=wanted.value AND result.column_id=cols.value "
            "ORDER BY wanted.value,cols.value",
            (run_id, json.dumps(rows[offset : offset + 500]), columns, run_id),
        ):
            digest.update(
                json.dumps(tuple(row), separators=(",", ":")).encode() + b"\n"
            )
            if row["owner"]:
                owners.add(int(row["owner"]))
    return owners, digest.hexdigest()
