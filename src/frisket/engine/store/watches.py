"""Watchlist store for a project bundle: watch definitions, watch runs and
their hits/events, per-row field state for row-changed detection, and the
event synthesis that ``record_watch_run`` persists in one transaction. Free
functions over the facade's per-thread SQLite connection; ``project`` stays
duck-typed (``Any``) so this leaf never re-imports the facade module."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from frisket.features.watchlists.events import (
    ALLOWED_EVENT_KINDS,
    ALLOWED_SUBJECT_KINDS,
    count_changed_event,
    event_json,
    row_changed_event,
    row_entered_event,
    row_exited_event,
    threshold_crossed_event,
)
from frisket.features.watchlists.specs import (
    DETECTION_POLICY_COUNT_CHANGED,
    DETECTION_POLICY_MEMBERSHIP_CHANGED,
    DETECTION_POLICY_NEW_MATCHES,
    DETECTION_POLICY_ROW_CHANGED,
    DETECTION_POLICY_THRESHOLD_CROSSED,
    normalize_detection_policy,
    canonical_json,
    watch_spec_metadata,
)


def _decode_watch_detection_policy(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        raw = value
    else:
        try:
            raw = json.loads(value or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return normalize_detection_policy(raw)


def _watch_value_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _watch_value_hash(value_json: str) -> str:
    digest = hashlib.sha256(value_json.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _decode_watch_value(value_json: Any) -> Any:
    try:
        return json.loads(value_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _threshold_crossing(
    *,
    previous_count: int,
    current_count: int,
    threshold: int,
    direction: str,
) -> str | None:
    crossed_up = previous_count < threshold <= current_count
    crossed_down = previous_count >= threshold > current_count
    if direction == "at_or_above" and crossed_up:
        return "up"
    if direction == "below" and crossed_down:
        return "down"
    if direction == "any":
        if crossed_up:
            return "up"
        if crossed_down:
            return "down"
    return None


def watches(project: Any) -> list[sqlite3.Row]:
    return project.db.execute(
        "SELECT * FROM watches ORDER BY created_at, id"
    ).fetchall()


def get_watch(project: Any, watch_id: int) -> sqlite3.Row | None:
    return project.db.execute(
        "SELECT * FROM watches WHERE id=?", (watch_id,)
    ).fetchone()


def add_watch(
    project: Any,
    name: str,
    *,
    scope: str,
    query: dict[str, Any],
    sheet_id: int | None = None,
    detection_policy: dict[str, Any] | None = None,
    enabled: bool = True,
    commit: bool = True,
) -> int:
    metadata = watch_spec_metadata(
        query,
        scope=scope,
        sheet_id=sheet_id,
        detection_policy=detection_policy,
    )
    query_json = json.dumps(query, sort_keys=True)
    cur = project.db.execute(
        "INSERT INTO watches (name, scope, sheet_id, query, query_version, "
        "query_hash, detection_policy, enabled, last_status) "
        "VALUES (?,?,?,?,?,?,?,?, 'never')",
        (
            name,
            scope,
            sheet_id,
            query_json,
            metadata["query_version"],
            metadata["query_hash"],
            metadata["detection_policy"],
            int(enabled),
        ),
    )
    if commit:
        project.db.commit()
    return cur.lastrowid


def update_watch(
    project: Any,
    watch_id: int,
    *,
    name: str | None = None,
    enabled: bool | None = None,
) -> sqlite3.Row | None:
    """Apply the small lifecycle surface without touching execution state."""
    sets: list[str] = []
    values: list[Any] = []
    if name is not None:
        sets.append("name=?")
        values.append(name)
    if enabled is not None:
        sets.append("enabled=?")
        values.append(int(enabled))
    if not sets:
        return get_watch(project, watch_id)
    sets.append("updated_at=datetime('now')")
    values.append(int(watch_id))
    cur = project.db.execute(
        f"UPDATE watches SET {', '.join(sets)} WHERE id=?",
        values,
    )
    if cur.rowcount == 0:
        project.db.rollback()
        return None
    project.db.commit()
    return get_watch(project, watch_id)


def delete_watch(project: Any, watch_id: int) -> bool:
    """Delete one Watch and the local notifications explicitly scoped to it.

    Watch-owned runs and state are covered by the schema foreign keys.  The
    notification tables intentionally use JSON source references, so their
    matching local projections and routes must be removed in this transaction
    before deleting the Watch.  Delivery outside this database is not touched.
    """
    watch_id = int(watch_id)
    try:
        cur = project.db.cursor()
        cur.execute("BEGIN IMMEDIATE")
        if (
            cur.execute("SELECT 1 FROM watches WHERE id=?", (watch_id,)).fetchone()
            is None
        ):
            project.db.rollback()
            return False
        cur.execute(
            "DELETE FROM notification_items "
            "WHERE source_kind='watch' "
            "AND json_extract(source_ref, '$.watch_id')=?",
            (watch_id,),
        )
        cur.execute(
            "DELETE FROM notification_routes "
            "WHERE source_kind='watch' "
            "AND json_extract(source_ref_match_json, '$.watch_id')=?",
            (watch_id,),
        )
        cur.execute("DELETE FROM watches WHERE id=?", (watch_id,))
        project.db.commit()
        return True
    except Exception:
        project.db.rollback()
        raise


def watch_latest_run(project: Any, watch_id: int) -> sqlite3.Row | None:
    return project.db.execute(
        "SELECT * FROM watch_runs WHERE watch_id=? "
        "ORDER BY started_at DESC, id DESC LIMIT 1",
        (watch_id,),
    ).fetchone()


def get_watch_run(project: Any, run_id: int) -> sqlite3.Row | None:
    return project.db.execute(
        "SELECT * FROM watch_runs WHERE id=?",
        (run_id,),
    ).fetchone()


def watch_runs_page(
    project: Any,
    watch_id: int,
    offset: int = 0,
    limit: int = 50,
) -> list[sqlite3.Row]:
    return project.db.execute(
        "SELECT * FROM watch_runs WHERE watch_id=? "
        "ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
        (watch_id, limit, offset),
    ).fetchall()


def watch_runs_total(project: Any, watch_id: int) -> int:
    row = project.db.execute(
        "SELECT COUNT(*) AS count FROM watch_runs WHERE watch_id=?",
        (watch_id,),
    ).fetchone()
    return int(row["count"] if row else 0)


def watch_run_hits(
    project: Any,
    run_id: int,
    limit: int = 20,
) -> list[sqlite3.Row]:
    return project.db.execute(
        "SELECT * FROM watch_run_hits WHERE run_id=? ORDER BY rank LIMIT ?",
        (run_id, limit),
    ).fetchall()


def watch_run_events(
    project: Any,
    run_id: int,
    *,
    offset: int = 0,
    limit: int = 50,
    event_kind: str | None = None,
) -> list[sqlite3.Row]:
    where = "run_id=?"
    params: list[Any] = [run_id]
    if event_kind is not None:
        where += " AND event_kind=?"
        params.append(event_kind)
    return project.db.execute(
        f"SELECT * FROM watch_run_events WHERE {where} "
        "ORDER BY rank ASC, id ASC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()


def watch_run_events_total(
    project: Any,
    run_id: int,
    *,
    event_kind: str | None = None,
) -> int:
    where = "run_id=?"
    params: list[Any] = [run_id]
    if event_kind is not None:
        where += " AND event_kind=?"
        params.append(event_kind)
    row = project.db.execute(
        f"SELECT COUNT(*) AS count FROM watch_run_events WHERE {where}",
        tuple(params),
    ).fetchone()
    return int(row["count"] if row else 0)


def watch_events_for_notification(
    project: Any, *, watch_id: int, run_id: int, event_ids: list[int]
) -> list[sqlite3.Row]:
    if not event_ids:
        return []
    placeholders = ",".join("?" for _ in event_ids)
    return project.db.execute(
        "SELECT * FROM watch_run_events WHERE watch_id=? AND run_id=? "
        f"AND id IN ({placeholders}) ORDER BY id ASC",
        (int(watch_id), int(run_id), *(int(value) for value in event_ids)),
    ).fetchall()


def record_watch_run(
    project: Any,
    watch_id: int,
    *,
    hits: list[dict[str, Any]],
    status: str = "ok",
    error: str | None = None,
    op_cursor_before: int,
    op_cursor_after: int,
    resolved_query_hash: str | None = None,
    resolved_query: dict[str, Any] | None = None,
    error_code: str | None = None,
    advance_cursor: bool = True,
) -> int:
    """Record one manual watch evaluation and update seen-row state."""
    watch_row = project.get_watch(watch_id)
    if watch_row is None:
        raise ValueError("watch not found")
    detection_policy = _decode_watch_detection_policy(watch_row["detection_policy"])
    policy_kind = detection_policy["kind"]
    resolved_query_json = canonical_json(resolved_query or {})
    unique_hits: list[dict[str, Any]] = []
    seen_this_run: set[tuple[int, int]] = set()
    for hit in hits:
        key = (int(hit["sheet_id"]), int(hit["row_id"]))
        if key in seen_this_run:
            continue
        seen_this_run.add(key)
        unique_hits.append(hit)
    if status != "ok":
        unique_hits = []

    cur = project.db.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        if unique_hits:
            visible_rows: set[tuple[int, int]] = set()
            rows_by_sheet: dict[int, set[int]] = {}
            for hit in unique_hits:
                rows_by_sheet.setdefault(int(hit["sheet_id"]), set()).add(
                    int(hit["row_id"])
                )
            for sheet_id, row_ids in rows_by_sheet.items():
                placeholders = ",".join("?" for _ in row_ids)
                visible_rows.update(
                    (int(row["sheet_id"]), int(row["id"]))
                    for row in cur.execute(
                        "SELECT sheet_id, id FROM rows "
                        f"WHERE sheet_id=? AND hidden=0 AND id IN ({placeholders})",
                        (sheet_id, *row_ids),
                    )
                )
            unique_hits = [
                hit
                for hit in unique_hits
                if (int(hit["sheet_id"]), int(hit["row_id"])) in visible_rows
            ]
        previous_run = cur.execute(
            "SELECT * FROM watch_runs WHERE watch_id=? AND status='ok' "
            "ORDER BY id DESC LIMIT 1",
            (watch_id,),
        ).fetchone()
        previous_run_id = int(previous_run["id"]) if previous_run is not None else None
        previous_count = (
            int(previous_run["matched_rows"]) if previous_run is not None else 0
        )
        previous_members: set[tuple[int, int]] = set()
        if previous_run_id is not None:
            previous_members = {
                (int(row["sheet_id"]), int(row["row_id"]))
                for row in cur.execute(
                    "SELECT sheet_id, row_id FROM watch_seen_rows "
                    "WHERE watch_id=? AND last_seen_run_id=?",
                    (watch_id, previous_run_id),
                )
            }
        existing = {
            (int(row["sheet_id"]), int(row["row_id"]))
            for row in cur.execute(
                "SELECT sheet_id, row_id FROM watch_seen_rows WHERE watch_id=?",
                (watch_id,),
            )
        }
        current_members = {
            (int(hit["sheet_id"]), int(hit["row_id"])) for hit in unique_hits
        }
        run = cur.execute(
            "INSERT INTO watch_runs (watch_id, status, op_cursor_before, "
            "op_cursor_after, matched_rows, new_rows, error, "
            "resolved_query_hash, resolved_query, error_code, finished_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?, datetime('now'))",
            (
                watch_id,
                status,
                op_cursor_before,
                op_cursor_after,
                len(unique_hits),
                0,
                error,
                resolved_query_hash,
                resolved_query_json,
                error_code,
            ),
        )
        run_id = int(run.lastrowid)
        new_rows = 0
        events: list[dict[str, Any]] = []
        for index, hit in enumerate(unique_hits, start=1):
            key = (int(hit["sheet_id"]), int(hit["row_id"]))
            is_new = key not in existing
            if is_new:
                new_rows += 1
                existing.add(key)
                cur.execute(
                    "INSERT INTO watch_seen_rows "
                    "(watch_id, sheet_id, row_id, first_seen_run_id, "
                    "last_seen_run_id) VALUES (?,?,?,?,?)",
                    (watch_id, key[0], key[1], run_id, run_id),
                )
            else:
                cur.execute(
                    "UPDATE watch_seen_rows SET last_seen_run_id=? "
                    "WHERE watch_id=? AND sheet_id=? AND row_id=?",
                    (run_id, watch_id, key[0], key[1]),
                )
            if (
                policy_kind == DETECTION_POLICY_NEW_MATCHES
                and is_new
                or policy_kind == DETECTION_POLICY_MEMBERSHIP_CHANGED
                and key not in previous_members
            ):
                event = row_entered_event(
                    run_id=run_id,
                    watch_id=watch_id,
                    sheet_id=key[0],
                    row_id=key[1],
                    rank=index,
                    snippet=hit.get("snippet"),
                    is_new=is_new,
                    reentered=(
                        policy_kind == DETECTION_POLICY_MEMBERSHIP_CHANGED
                        and not is_new
                    ),
                )
                events.append(event)
            cur.execute(
                "INSERT INTO watch_run_hits "
                "(run_id, sheet_id, row_id, column_id, rank, snippet, is_new) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    run_id,
                    key[0],
                    key[1],
                    hit.get("column_id"),
                    index,
                    hit.get("snippet"),
                    int(is_new),
                ),
            )
        if status == "ok":
            if policy_kind == DETECTION_POLICY_MEMBERSHIP_CHANGED:
                for key in sorted(previous_members - current_members):
                    events.append(
                        row_exited_event(
                            run_id=run_id,
                            watch_id=watch_id,
                            sheet_id=key[0],
                            row_id=key[1],
                            rank=len(events) + 1,
                        )
                    )
            elif (
                policy_kind == DETECTION_POLICY_COUNT_CHANGED
                and previous_count != len(unique_hits)
            ):
                events.append(
                    count_changed_event(
                        run_id=run_id,
                        watch_id=watch_id,
                        previous_run_id=previous_run_id,
                        previous_count=previous_count,
                        current_count=len(unique_hits),
                    )
                )
            elif policy_kind == DETECTION_POLICY_THRESHOLD_CROSSED:
                crossing = _threshold_crossing(
                    previous_count=previous_count,
                    current_count=len(unique_hits),
                    threshold=int(detection_policy["threshold"]),
                    direction=str(detection_policy["direction"]),
                )
                if crossing is not None:
                    events.append(
                        threshold_crossed_event(
                            run_id=run_id,
                            watch_id=watch_id,
                            previous_run_id=previous_run_id,
                            previous_count=previous_count,
                            current_count=len(unique_hits),
                            threshold=int(detection_policy["threshold"]),
                            direction=str(detection_policy["direction"]),
                            crossing=crossing,
                        )
                    )
            elif policy_kind == DETECTION_POLICY_ROW_CHANGED:
                events.extend(
                    _record_watch_row_field_state(
                        project,
                        cur,
                        watch_id=watch_id,
                        run_id=run_id,
                        fields=detection_policy["fields"],
                        current_hits=unique_hits,
                        previous_members=previous_members,
                    )
                )
        for event in events:
            event_kind = str(event["event_kind"])
            subject_kind = str(event["subject_kind"])
            if event_kind not in ALLOWED_EVENT_KINDS:
                raise ValueError(f"unsupported watch event kind: {event_kind}")
            if subject_kind not in ALLOWED_SUBJECT_KINDS:
                raise ValueError(f"unsupported watch subject kind: {subject_kind}")
            cur.execute(
                "INSERT INTO watch_run_events "
                "(run_id, watch_id, event_kind, subject_kind, "
                "subject_ref, before_json, after_json, delta_json, "
                "severity, rank, snippet) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event["run_id"],
                    event["watch_id"],
                    event_kind,
                    subject_kind,
                    event_json(event["subject_ref"]),
                    event_json(event["before_json"]),
                    event_json(event["after_json"]),
                    event_json(event["delta_json"]),
                    event["severity"],
                    event["rank"],
                    event["snippet"],
                ),
            )
        cur.execute(
            "UPDATE watch_runs SET new_rows=? WHERE id=?",
            (new_rows, run_id),
        )
        if advance_cursor:
            cur.execute(
                "UPDATE watches SET last_evaluated_op=?, last_run_id=?, "
                "last_status=?, updated_at=datetime('now') WHERE id=?",
                (op_cursor_after, run_id, status, watch_id),
            )
        else:
            cur.execute(
                "UPDATE watches SET last_run_id=?, last_status=?, "
                "updated_at=datetime('now') WHERE id=?",
                (run_id, status, watch_id),
            )
        project.db.commit()
        return run_id
    except Exception:
        project.db.rollback()
        raise


def _record_watch_row_field_state(
    project: Any,
    cur: sqlite3.Cursor,
    *,
    watch_id: int,
    run_id: int,
    fields: list[dict[str, Any]],
    current_hits: list[dict[str, Any]],
    previous_members: set[tuple[int, int]],
) -> list[dict[str, Any]]:
    current_keys = [(int(hit["sheet_id"]), int(hit["row_id"])) for hit in current_hits]
    hit_rank = {key: index for index, key in enumerate(current_keys, start=1)}
    current_by_sheet: dict[int, list[int]] = {}
    for sheet_id, row_id in current_keys:
        current_by_sheet.setdefault(sheet_id, []).append(row_id)
    configured_fields = {
        (int(field["sheet_id"]), int(field["column_id"])) for field in fields
    }
    for row in cur.execute(
        "SELECT sheet_id, row_id, column_id FROM watch_row_field_state "
        "WHERE watch_id=?",
        (watch_id,),
    ).fetchall():
        row_key = (int(row["sheet_id"]), int(row["row_id"]))
        field_key = (int(row["sheet_id"]), int(row["column_id"]))
        if row_key in current_keys and field_key in configured_fields:
            continue
        cur.execute(
            "DELETE FROM watch_row_field_state "
            "WHERE watch_id=? AND sheet_id=? AND row_id=? AND column_id=?",
            (watch_id, row["sheet_id"], row["row_id"], row["column_id"]),
        )

    changes_by_row: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for field in fields:
        field_sheet_id = int(field["sheet_id"])
        column_id = int(field["column_id"])
        row_ids = current_by_sheet.get(field_sheet_id, [])
        if not row_ids:
            continue
        values = project.get_values(field_sheet_id, column_id, row_ids=row_ids)
        for row_id in row_ids:
            key = (field_sheet_id, int(row_id))
            value_json = _watch_value_json(values.get(row_id))
            value_hash = _watch_value_hash(value_json)
            prior = cur.execute(
                "SELECT value_hash, value_json FROM watch_row_field_state "
                "WHERE watch_id=? AND sheet_id=? AND row_id=? AND column_id=?",
                (watch_id, field_sheet_id, row_id, column_id),
            ).fetchone()
            if (
                prior is not None
                and key in previous_members
                and prior["value_hash"] != value_hash
            ):
                changes_by_row.setdefault(key, []).append(
                    {
                        "sheet_id": field_sheet_id,
                        "column_id": column_id,
                        "name": field.get("name"),
                        "before": _decode_watch_value(prior["value_json"]),
                        "after": values.get(row_id),
                    }
                )
            cur.execute(
                "INSERT INTO watch_row_field_state "
                "(watch_id, sheet_id, row_id, column_id, value_hash, "
                "value_json, last_observed_run_id) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(watch_id, sheet_id, row_id, column_id) DO UPDATE SET "
                "value_hash=excluded.value_hash, "
                "value_json=excluded.value_json, "
                "last_observed_run_id=excluded.last_observed_run_id",
                (
                    watch_id,
                    field_sheet_id,
                    row_id,
                    column_id,
                    value_hash,
                    value_json,
                    run_id,
                ),
            )

    events: list[dict[str, Any]] = []
    for key in sorted(changes_by_row, key=lambda item: hit_rank.get(item, 0)):
        events.append(
            row_changed_event(
                run_id=run_id,
                watch_id=watch_id,
                sheet_id=key[0],
                row_id=key[1],
                rank=hit_rank.get(key, len(events) + 1),
                changed_fields=changes_by_row[key],
            )
        )
    return events
