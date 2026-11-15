"""Actual consumed input identities and reversible, demand-driven freshness.

Row results keep their immutable input references. Broad readers retain source
scope and bounded-read fingerprints instead of a version list for every cell.
All writes here belong to the caller's result/checkpoint transaction.
"""

from __future__ import annotations

from contextlib import nullcontext
import hashlib
import json
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Mapping, Sequence
from typing import Any


CONSUMED_SOURCE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS consumed_source_bundles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  capture_key TEXT NOT NULL UNIQUE,
  owner_op_id INTEGER NOT NULL REFERENCES ops(id) ON DELETE CASCADE,
  run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
  row_id INTEGER REFERENCES rows(id) ON DELETE CASCADE,
  output_sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK(kind IN ('row','aggregate')),
  input_fingerprint TEXT NOT NULL,
  capture_json TEXT NOT NULL CHECK(json_valid(capture_json)),
  state TEXT NOT NULL CHECK(state IN ('fresh','stale','unknown','checking')),
  checked_revisions_json TEXT NOT NULL CHECK(json_valid(checked_revisions_json))
);
CREATE INDEX IF NOT EXISTS idx_consumed_sources_sheet
  ON consumed_source_bundles(output_sheet_id,owner_op_id);
CREATE INDEX IF NOT EXISTS idx_consumed_sources_run_row
  ON consumed_source_bundles(run_id,row_id);
CREATE INDEX IF NOT EXISTS idx_consumed_sources_fingerprint
  ON consumed_source_bundles(input_fingerprint);
CREATE INDEX IF NOT EXISTS idx_consumed_sources_kind
  ON consumed_source_bundles(kind,id);
CREATE INDEX IF NOT EXISTS idx_consumed_sources_input_row
  ON consumed_source_bundles(json_extract(capture_json,'$.row_id')) WHERE kind='row';
CREATE TABLE IF NOT EXISTS consumed_source_columns (
  bundle_id INTEGER NOT NULL REFERENCES consumed_source_bundles(id) ON DELETE CASCADE,
  column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
  PRIMARY KEY(bundle_id,column_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_consumed_sources_column
  ON consumed_source_columns(column_id,bundle_id);
"""

CAPTURE_BATCH_SIZE = 1000
INLINE_CHECK_ROWS = 1000


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _identity_ref(ref: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if ref is None:
        return None
    return {
        key: value
        for key, value in ref.items()
        if key
        in {
            "kind",
            "op_id",
            "run_id",
            "producer_id",
            "row_id",
            "column_id",
            "prepared_ref_id",
            "outcome",
            "validity",
        }
    }


def _current_refs(
    project: Any,
    sheet_id: int,
    column_id: int,
    row_ids: Sequence[int],
    *,
    include_validity: bool,
) -> dict[int, dict[str, Any]]:
    """Use the canonical read relation without fetching/decoding input payloads."""
    out = {}
    for offset in range(0, len(row_ids), 500):
        part = row_ids[offset : offset + 500]
        placeholders = ",".join("?" for _ in part)
        for row in project.db.execute(
            "SELECT r.id,v.origin_kind,v.origin_op_id,v.origin_run_id,v.base_producer_id,"
            "v.prepared_ref_id,v.outcome,coalesce(v.validity,'missing') AS validity "
            "FROM rows r JOIN sheets s ON s.id=r.sheet_id AND s.hidden=0 "
            "LEFT JOIN current_cell_values v ON v.row_id=r.id AND v.column_id=? "
            f"WHERE r.sheet_id=? AND r.hidden=0 AND r.id IN ({placeholders})",
            [column_id, sheet_id, *part],
        ):
            row_id = int(row["id"])
            out[row_id] = {
                "kind": row["origin_kind"] or "missing",
                "op_id": row["origin_op_id"],
                "run_id": row["origin_run_id"],
                "row_id": row_id,
                "column_id": column_id,
                "outcome": row["outcome"],
                **(
                    {"producer_id": int(row["base_producer_id"])}
                    if row["base_producer_id"] is not None
                    else {}
                ),
                **(
                    {"prepared_ref_id": int(row["prepared_ref_id"])}
                    if row["prepared_ref_id"] is not None
                    else {}
                ),
                **({"validity": row["validity"]} if include_validity else {}),
            }
    return out


def authority_revisions(project: Any, column_ids: Sequence[int]) -> dict[str, int]:
    ids = sorted(set(map(int, column_ids)))
    revisions = {str(column_id): -1 for column_id in ids}
    for offset in range(0, len(ids), 500):
        part = ids[offset : offset + 500]
        placeholders = ",".join("?" for _ in part)
        for row in project.db.execute(
            "SELECT c.id AS column_id,coalesce(r.revision,0) AS revision "
            "FROM columns c JOIN sheets s ON s.id=c.sheet_id "
            "LEFT JOIN cell_authority_revisions r ON r.column_id=c.id "
            f"WHERE c.active=1 AND s.hidden=0 AND c.id IN ({placeholders})",
            part,
        ):
            revisions[str(row["column_id"])] = int(row["revision"])
    return revisions


def _descriptors(project: Any, column_ids: Sequence[int]) -> dict[str, Any]:
    from frisket.engine.store.column_descriptors import descriptor_identity

    return {
        str(column_id): descriptor_identity(project, column_id)
        for column_id in column_ids
    }


def _members(project: Any, sheet_id: int, row_ids: Sequence[int]) -> list[list[int]]:
    members = []
    for offset in range(0, len(row_ids), 500):
        part = row_ids[offset : offset + 500]
        placeholders = ",".join("?" for _ in part)
        members.extend(
            [int(row["id"]), int(row["position"])]
            for row in project.db.execute(
                "SELECT r.id,r.position FROM rows r JOIN sheets s ON s.id=r.sheet_id "
                "WHERE r.sheet_id=? AND r.hidden=0 AND s.hidden=0 "
                f"AND r.id IN ({placeholders}) ORDER BY r.id",
                [sheet_id, *part],
            )
        )
    return sorted(members)


def _records(
    sheet_id: int,
    column_ids: Sequence[int],
    members: Sequence[Sequence[int]],
    descriptors: Mapping[str, Any],
    refs: Mapping[int, Mapping[int, Mapping[str, Any]]],
) -> list[Any]:
    records: list[Any] = [
        ["descriptor", column_id, descriptors[str(column_id)]]
        for column_id in column_ids
    ]
    for row_id, position in members:
        records.append(["member", sheet_id, row_id, position])
        for column_id in column_ids:
            records.append(
                [
                    "cell",
                    sheet_id,
                    row_id,
                    column_id,
                    _identity_ref(refs[column_id].get(row_id)),
                ]
            )
    return records


def read_consumed_source(
    project: Any,
    sheet_id: int,
    column_ids: Sequence[int],
    *,
    row_ids: Sequence[int] | None = None,
    scope_intent: str = "whole",
    batch_size: int = CAPTURE_BATCH_SIZE,
    refs_out: dict[int, dict[int, dict[str, Any]]] | None = None,
) -> tuple[list[int], dict[int, dict[int, Any]], dict[str, Any]]:
    """Read values and capture their real identity stream in short snapshots.

    Enumerating the execution cohort does not capture its values. Every actual
    read below is bounded and ends before the caller executes external work.
    A surrounding caller-owned snapshot/transaction is respected.
    """
    if scope_intent not in {"whole", "selected"}:
        raise ValueError("source scope must retain whole or selected intent")
    columns = sorted(set(map(int, column_ids)))
    cohort = list(row_ids) if row_ids is not None else project.visible_row_ids(sheet_id)
    ordered = sorted(set(map(int, cohort)))
    scope: dict[str, Any] = {
        "sheet_id": int(sheet_id),
        "column_ids": columns,
        "intent": scope_intent,
        "cohort": ordered if scope_intent == "selected" else None,
        "max_row_id": max(ordered, default=0),
        "partitions": [],
    }
    values: dict[int, dict[int, Any]] = {column_id: {} for column_id in columns}
    revisions = None
    coherent = True
    # Empty input still depends on descriptor identity and whole-sheet membership.
    parts = (
        ordered[offset : offset + batch_size]
        for offset in range(0, max(1, len(ordered)), batch_size)
    )
    previous_hi = 0
    for part in parts:
        context = (
            nullcontext(project)
            if project.db.in_transaction
            else project.read_snapshot()
        )
        with context as snapshot:
            current_revisions = authority_revisions(snapshot, columns)
            if revisions is None:
                revisions = current_revisions
            coherent = coherent and revisions == current_revisions
            descriptors = _descriptors(snapshot, columns)
            members = _members(snapshot, sheet_id, part)
            refs = {}
            for column_id in columns:
                got, column_refs = snapshot.get_values_with_refs(
                    sheet_id, column_id, row_ids=part, include_validity=True
                )
                values[column_id].update(got)
                refs[column_id] = column_refs
                if refs_out is not None:
                    refs_out.setdefault(column_id, {}).update(column_refs)
            records = _records(sheet_id, columns, members, descriptors, refs)
        scope["partitions"].append(
            {
                "lo": previous_hi + 1
                if scope_intent == "whole"
                else min(part, default=0),
                "hi": max(part, default=0),
                "fingerprint": _hash(records),
                "count": len(part),
                "member_count": len(members),
            }
        )
        previous_hi = max(part, default=0)
    capture = {
        "schema_version": 1,
        "kind": "aggregate",
        "scopes": [scope],
        "revisions": revisions or {},
        "coherent": coherent,
        "fingerprint": _hash([scope]),
    }
    return cohort, values, capture


def merge_captures(captures: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Keep every actual read partition when outputs share several sources."""
    scopes = []
    revisions: dict[str, int] = {}
    coherent = True
    for capture in captures:
        if capture["kind"] != "aggregate":
            raise ValueError("aggregate sources require aggregate capture")
        scopes.extend(capture["scopes"])
        coherent = coherent and bool(capture["coherent"])
        for column_id, revision in capture["revisions"].items():
            coherent = coherent and revisions.get(column_id, revision) == revision
            revisions[column_id] = revision
    return {
        "schema_version": 1,
        "kind": "aggregate",
        "scopes": scopes,
        "revisions": revisions,
        "coherent": coherent,
        "fingerprint": _hash(scopes),
    }


def row_capture(
    *,
    sheet_id: int,
    row_id: int,
    source_cells: Mapping[str, Mapping[str, Any]],
    descriptors: Mapping[int, Any],
    revisions: Mapping[str, int],
) -> dict[str, Any]:
    """References only: never duplicate captured source text for freshness."""
    inputs = [
        {
            "role": role,
            "column_id": int(cell["column_id"]),
            "descriptor": descriptors.get(int(cell["column_id"])),
            "ref": _identity_ref(cell.get("value_ref")),
        }
        for role, cell in sorted(source_cells.items())
    ]
    return {
        "schema_version": 1,
        "kind": "row",
        "sheet_id": int(sheet_id),
        "row_id": int(row_id),
        "inputs": inputs,
        "revisions": dict(revisions),
        "coherent": True,
    }


def capture_columns(capture: Mapping[str, Any]) -> list[int]:
    if capture["kind"] == "purged":
        return []
    if capture["kind"] == "row":
        return sorted({int(item["column_id"]) for item in capture["inputs"]})
    return sorted(
        {
            int(column_id)
            for scope in capture["scopes"]
            for column_id in scope["column_ids"]
        }
    )


def capture_fingerprint(capture: Mapping[str, Any]) -> str:
    """Stable input/check identity, excluding monotonic cache revisions."""
    return _hash(
        {
            key: value
            for key, value in capture.items()
            if key not in {"revisions", "coherent", "fingerprint"}
        }
    )


def store_capture(
    project: Any,
    capture: Mapping[str, Any],
    *,
    owner_op_id: int,
    output_sheet_id: int,
    run_id: int | None = None,
    row_id: int | None = None,
) -> int:
    """Publish capture in the same transaction as its result or checkpoint."""
    if not project.db.in_transaction:
        raise RuntimeError("consumed inputs require the output publication transaction")
    key = f"{owner_op_id}:{run_id}:{row_id}:{output_sheet_id}"
    encoded = _json(capture)
    existing = project.db.execute(
        "SELECT id,capture_json FROM consumed_source_bundles WHERE capture_key=?",
        (key,),
    ).fetchone()
    if existing is not None:
        if existing["capture_json"] != encoded:
            raise ValueError(
                "saved output cannot be paired with newly captured sources"
            )
        return int(existing["id"])
    columns = capture_columns(capture)
    current = authority_revisions(project, columns)
    fresh = bool(capture.get("coherent")) and current == capture["revisions"]
    cursor = project.db.execute(
        "INSERT INTO consumed_source_bundles(capture_key,owner_op_id,run_id,row_id,"
        "output_sheet_id,kind,input_fingerprint,capture_json,state,checked_revisions_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            key,
            owner_op_id,
            run_id,
            row_id,
            output_sheet_id,
            capture["kind"],
            capture_fingerprint(capture),
            encoded,
            "fresh" if fresh else "unknown",
            _json(current) if fresh else "{}",
        ),
    )
    identity = int(cursor.lastrowid)
    project.db.executemany(
        "INSERT INTO consumed_source_columns VALUES(?,?)",
        ((identity, column_id) for column_id in columns),
    )
    return identity


def _verify_row(snapshot: Any, capture: Mapping[str, Any]) -> bool:
    sheet_id, row_id = int(capture["sheet_id"]), int(capture["row_id"])
    columns = capture_columns(capture)
    descriptors = _descriptors(snapshot, columns)
    if any(value is None for value in descriptors.values()):
        return False
    refs = {
        column_id: _current_refs(
            snapshot, sheet_id, column_id, [row_id], include_validity=False
        ).get(row_id)
        for column_id in columns
    }
    return all(
        item["descriptor"] == descriptors[str(item["column_id"])]
        and item["ref"] == refs[item["column_id"]]
        for item in capture["inputs"]
    )


def _verify_aggregate(snapshot: Any, capture: Mapping[str, Any]) -> bool:
    for scope in capture["scopes"]:
        sheet_id = int(scope["sheet_id"])
        columns = scope["column_ids"]
        descriptors = _descriptors(snapshot, columns)
        if any(value is None for value in descriptors.values()):
            return False
        if (
            scope["intent"] == "whole"
            and snapshot.db.execute(
                "SELECT 1 FROM rows WHERE sheet_id=? AND hidden=0 AND id>? LIMIT 1",
                (sheet_id, scope["max_row_id"]),
            ).fetchone()
        ):
            return False
        selected = scope["cohort"] or []
        for part in scope["partitions"]:
            if scope["intent"] == "selected":
                ids = selected[
                    bisect_left(selected, part["lo"]) : bisect_right(
                        selected, part["hi"]
                    )
                ]
            else:
                ids = [
                    int(row["id"])
                    for row in snapshot.db.execute(
                        "SELECT id FROM rows WHERE sheet_id=? AND hidden=0 AND id BETWEEN ? AND ? "
                        "ORDER BY id LIMIT ?",
                        (sheet_id, part["lo"], part["hi"], part["member_count"] + 1),
                    )
                ]
                if len(ids) != part["member_count"]:
                    return False
            members = _members(snapshot, sheet_id, ids)
            refs = {
                column_id: _current_refs(
                    snapshot, sheet_id, column_id, ids, include_validity=True
                )
                for column_id in columns
            }
            if (
                _hash(_records(sheet_id, columns, members, descriptors, refs))
                != part["fingerprint"]
            ):
                return False
    return True


def verify_bundle(project: Any, bundle_id: int) -> str:
    """Compare one consumed bundle, fencing publication by relevant revisions."""
    bundle = project.db.execute(
        "SELECT * FROM consumed_source_bundles WHERE id=?", (bundle_id,)
    ).fetchone()
    if bundle is None:
        return "unknown"
    capture = json.loads(bundle["capture_json"])
    if capture["kind"] == "purged":
        return "unknown"
    columns = capture_columns(capture)
    context = (
        nullcontext(project) if project.db.in_transaction else project.read_snapshot()
    )
    with context as snapshot:
        revisions = authority_revisions(snapshot, columns)
        equal = (
            _verify_row(snapshot, capture)
            if capture["kind"] == "row"
            else _verify_aggregate(snapshot, capture)
        )
    state = "fresh" if equal else "stale"
    # No provider work: a source edit races only this small cache publication.
    started = not project.db.in_transaction
    if started:
        project.db.execute("BEGIN IMMEDIATE")
    try:
        if authority_revisions(project, columns) != revisions:
            state = "unknown"
        project.db.execute(
            "UPDATE consumed_source_bundles SET state=?,checked_revisions_json=? "
            "WHERE id=? OR (kind='aggregate' AND input_fingerprint=?)",
            (
                state,
                _json(revisions) if state != "unknown" else "{}",
                bundle_id,
                bundle["input_fingerprint"] if capture["kind"] == "aggregate" else "",
            ),
        )
        if started:
            project.db.commit()
    except BaseException:
        if started:
            project.db.rollback()
        raise
    return state


def bundle_state(
    project: Any,
    bundle_id: int,
    *,
    request_verification: Callable[[int], int | None] | None = None,
    allow_inline: bool = True,
) -> str:
    bundle = project.db.execute(
        "SELECT * FROM consumed_source_bundles WHERE id=?", (bundle_id,)
    ).fetchone()
    if bundle is None:
        return "unknown"
    capture = json.loads(bundle["capture_json"])
    if capture["kind"] == "purged":
        return "unknown"
    revisions = authority_revisions(project, capture_columns(capture))
    if bundle["state"] in {"fresh", "stale"} and revisions == json.loads(
        bundle["checked_revisions_json"]
    ):
        return str(bundle["state"])
    count = (
        1
        if capture["kind"] == "row"
        else sum(
            part["count"] for scope in capture["scopes"] for part in scope["partitions"]
        )
    )
    if allow_inline and count <= INLINE_CHECK_ROWS:
        return verify_bundle(project, bundle_id)
    if request_verification is not None:
        return "checking" if request_verification(bundle_id) is not None else "unknown"
    return "unknown"


def row_input_state(
    project: Any, run_id: int, row_id: int, *, allow_inline: bool = True
) -> str:
    row = project.db.execute(
        "SELECT id FROM consumed_source_bundles WHERE kind='row' AND run_id=? AND row_id=?",
        (run_id, row_id),
    ).fetchone()
    return (
        bundle_state(project, int(row["id"]), allow_inline=allow_inline)
        if row is not None
        else "unknown"
    )


def purge_consumed_sources(
    db: Any, *, row_ids=(), column_ids=(), sheet_ids=(), limit: int = 500
) -> tuple[list[int], int]:
    """Forget one bounded capture batch, returning (affected run IDs, count).

    Maintenance repeats until count is zero, committing its ordinary cleanup
    checkpoint per batch. Call before physical scope deletion. Purged captures
    no longer match any source selector; independently retained outputs remain.
    """
    if not db.in_transaction:
        raise RuntimeError("source capture purge requires maintenance transaction")
    if not 1 <= limit <= 5000:
        raise ValueError("capture purge batch limit must be between 1 and 5000")
    for table, values in (
        ("rows", row_ids),
        ("columns", column_ids),
        ("sheets", sheet_ids),
    ):
        name = f"_consumed_purge_{table}"
        db.execute(f"DROP TABLE IF EXISTS temp.{name}")
        db.execute(f"CREATE TEMP TABLE {name}(id INTEGER PRIMARY KEY)")
        db.executemany(
            f"INSERT OR IGNORE INTO {name} VALUES(?)",
            ((int(value),) for value in values),
        )
    db.execute("DROP TABLE IF EXISTS temp._consumed_purge_bundles")
    db.execute("CREATE TEMP TABLE _consumed_purge_bundles(id INTEGER PRIMARY KEY)")
    queries = (
        "SELECT dep.bundle_id FROM _consumed_purge_columns selected "
        "JOIN consumed_source_columns dep ON dep.column_id=selected.id",
        "SELECT dep.bundle_id FROM _consumed_purge_sheets selected "
        "JOIN columns col ON col.sheet_id=selected.id "
        "JOIN consumed_source_columns dep ON dep.column_id=col.id",
        "SELECT b.id FROM _consumed_purge_rows selected "
        "JOIN consumed_source_bundles b ON json_extract(b.capture_json,'$.row_id')=selected.id "
        "WHERE b.kind='row'",
        "SELECT b.id FROM consumed_source_bundles b "
        "WHERE b.kind='aggregate' AND json_extract(b.capture_json,'$.kind')='aggregate' "
        "AND EXISTS(SELECT 1 FROM json_each(b.capture_json,'$.scopes') scope "
        "JOIN _consumed_purge_sheets selected ON selected.id=json_extract(scope.value,'$.sheet_id'))",
        "SELECT b.id FROM consumed_source_bundles b "
        "WHERE b.kind='aggregate' AND json_extract(b.capture_json,'$.kind')='aggregate' "
        "AND EXISTS(SELECT 1 FROM json_each(b.capture_json,'$.scopes') scope "
        "JOIN rows source ON source.sheet_id=json_extract(scope.value,'$.sheet_id') "
        "JOIN _consumed_purge_rows selected ON selected.id=source.id "
        "WHERE (json_extract(scope.value,'$.intent')='whole' "
        "AND source.id<=json_extract(scope.value,'$.max_row_id')) "
        "OR (json_extract(scope.value,'$.intent')='selected' AND EXISTS("
        "SELECT 1 FROM json_each(scope.value,'$.cohort') member WHERE member.value=source.id)))",
    )
    for query in queries:
        remaining = limit - int(
            db.execute("SELECT count(*) FROM _consumed_purge_bundles").fetchone()[0]
        )
        if remaining == 0:
            break
        db.execute(
            "INSERT OR IGNORE INTO _consumed_purge_bundles " + query + " LIMIT ?",
            (remaining,),
        )
    changed = int(
        db.execute("SELECT count(*) FROM _consumed_purge_bundles").fetchone()[0]
    )
    runs = [
        int(row[0])
        for row in db.execute(
            "SELECT DISTINCT b.run_id FROM _consumed_purge_bundles selected "
            "JOIN consumed_source_bundles b ON b.id=selected.id WHERE b.run_id IS NOT NULL ORDER BY b.run_id"
        )
    ]
    db.execute(
        "DELETE FROM consumed_source_columns WHERE bundle_id IN (SELECT id FROM _consumed_purge_bundles)"
    )
    db.execute(
        "UPDATE consumed_source_bundles SET capture_json=?,state='unknown',checked_revisions_json='{}',"
        "input_fingerprint='purged:'||id WHERE id IN (SELECT id FROM _consumed_purge_bundles)",
        (_json({"schema_version": 1, "kind": "purged"}),),
    )
    return runs, changed
