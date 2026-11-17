"""Watchlist store for a project bundle: watch definitions, watch runs and
their hits/events, per-row field state for row-changed detection, and the
event synthesis that ``record_watch_run`` persists in one transaction. Free
functions over the facade's per-thread SQLite connection; ``project`` stays
duck-typed (``Any``) so this leaf never re-imports the facade module."""

from __future__ import annotations

import json
import sqlite3
from typing import Any
from collections.abc import Iterable
from itertools import groupby

from frisket.features.watchlists.events import (
    count_changed_event,
    event_json,
    row_changed_event,
    row_entered_event,
    row_exited_event,
    threshold_crossed_event,
)
from frisket.contracts.query_spec import (
    normalize_detection_policy,
    canonical_json,
    detection_conditions,
)
from frisket.engine.store.transactions import write_transaction


UNCHANGED_SCHEDULE = object()


class WatchCheckConflict(ValueError):
    """A check lost its definition/baseline race and should be retried."""


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
        "SELECT watches.*, views.spec AS view_spec, views.sheet_id FROM watches "
        "JOIN views ON views.id=watches.view_id ORDER BY watches.created_at, watches.id"
    ).fetchall()


def get_watch(project: Any, watch_id: int) -> sqlite3.Row | None:
    return project.db.execute(
        "SELECT watches.*, views.spec AS view_spec, views.sheet_id FROM watches "
        "JOIN views ON views.id=watches.view_id WHERE watches.id=?",
        (watch_id,),
    ).fetchone()


def _validate_schedule(schedule: str | None) -> None:
    from frisket.engine.jobs.schedule import _schedule_interval

    if schedule is not None and (
        not isinstance(schedule, str) or _schedule_interval(schedule) is None
    ):
        raise ValueError(
            "schedule must be a supported cadence or null for manual checks"
        )


def add_watch(
    project: Any,
    name: str,
    *,
    view_id: int,
    detection_policy: dict[str, Any] | None = None,
    enabled: bool = True,
    schedule: str | None = "@hourly",
    commit: bool = True,
) -> int:
    from .saved_views import get_view
    from frisket.engine.views.specs import stored_selection, normalize_selection

    view = get_view(project, view_id)
    if view is None:
        raise ValueError("view not found")
    normalize_selection(project, stored_selection(json.loads(view["spec"])))
    if project.db.execute(
        "SELECT 1 FROM watches WHERE view_id=?", (view_id,)
    ).fetchone():
        raise ValueError("This view already has alert settings.")
    _validate_schedule(schedule)
    policy = detection_conditions(normalize_detection_policy(detection_policy))
    cur = project.db.execute(
        "INSERT INTO watches (name,view_id,detection_policy,enabled,schedule,last_status) VALUES (?,?,?,?,?, 'never')",
        (name, view_id, canonical_json(policy), int(enabled), schedule),
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
    detection_policy: dict[str, Any] | None = None,
    schedule: str | None | object = UNCHANGED_SCHEDULE,
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
    if detection_policy is not None:
        sets.append("detection_policy=?")
        values.append(
            canonical_json(
                detection_conditions(normalize_detection_policy(detection_policy))
            )
        )
    if schedule is not UNCHANGED_SCHEDULE:
        _validate_schedule(schedule)
        sets.append("schedule=?")
        values.append(schedule)
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
    with write_transaction(project.db):
        cur = project.db.cursor()
        if (
            cur.execute("SELECT 1 FROM watches WHERE id=?", (watch_id,)).fetchone()
            is None
        ):
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
        return True


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


def _display_observed(raw: str) -> Any:
    observed = json.loads(raw)
    return (
        observed["value"]
        if observed.get("outcome") in {"completed", "empty"}
        else observed
    )


def _watch_definition(row: Any) -> tuple[Any, ...]:
    return tuple(
        row[key] for key in ("view_id", "view_spec", "detection_policy", "enabled")
    )


def record_watch_run(
    project: Any,
    watch_id: int,
    *,
    hits: Iterable[dict[str, Any]],
    status: str = "ok",
    error: str | None = None,
    op_cursor_before: int,
    op_cursor_after: int,
    resolved_query_hash: str | None = None,
    resolved_query: dict[str, Any] | None = None,
    error_code: str | None = None,
    advance_cursor: bool = True,
    observation_project: Any | None = None,
    expected_watch: Any | None = None,
) -> int:
    """Publish one coherent observation, differences and latest baseline together.

    Full membership lives only in the latest baseline. Historical checks keep
    counts, actual differences and at most 500 preview hits. Failed checks never
    replace that baseline. The caller can stream hits from its read snapshot.
    """
    from contextlib import ExitStack
    from .watch_observation import stage_watch_observation, WatchObservationIncomplete

    watch_row = project.get_watch(watch_id)
    if watch_row is None:
        raise ValueError("watch not found")
    expected = expected_watch if expected_watch is not None else watch_row
    policy = detection_conditions(
        _decode_watch_detection_policy(expected["detection_policy"])
    )
    with ExitStack() as stack:
        source = observation_project or stack.enter_context(project.read_snapshot())
        try:
            staged = stack.enter_context(
                stage_watch_observation(
                    source,
                    hits if status == "ok" else (),
                    policy["fields"] if status == "ok" else [],
                    watch_id=watch_id,
                )
            )
        except WatchObservationIncomplete as exc:
            status, error, error_code, advance_cursor = (
                "error",
                str(exc),
                "watch_inputs_unavailable",
                False,
            )
            staged = stack.enter_context(stage_watch_observation(source, (), []))
        db = project.db
        db.execute("ATTACH DATABASE ? AS watch_observation", (str(staged),))
        try:
            with write_transaction(db):
                current = project.get_watch(watch_id)
                if current is None:
                    raise WatchCheckConflict("watch was deleted during evaluation")
                if _watch_definition(current) != _watch_definition(expected):
                    raise WatchCheckConflict(
                        "watch definition changed during evaluation; retry"
                    )
                if current["last_run_id"] != expected["last_run_id"]:
                    raise WatchCheckConflict(
                        "another watch check completed during evaluation; retry"
                    )
                if (
                    status == "ok"
                    and db.execute(
                        "SELECT 1 FROM watch_observation.members now LEFT JOIN rows ON rows.id=now.row_id AND rows.sheet_id=now.sheet_id WHERE rows.id IS NULL LIMIT 1"
                    ).fetchone()
                ):
                    raise WatchCheckConflict(
                        "source rows were permanently removed during evaluation; retry"
                    )
                previous = db.execute(
                    "SELECT * FROM watch_runs WHERE id=? AND watch_id=? AND status='ok'",
                    (current["baseline_run_id"], watch_id),
                ).fetchone()
                baseline = (
                    previous is not None
                    and previous["resolved_query_hash"] == resolved_query_hash
                )
                previous_policy = (
                    detection_conditions(
                        _decode_watch_detection_policy(previous["detection_policy"])
                    )
                    if baseline
                    else {}
                )
                previous_count = int(previous["matched_rows"]) if baseline else 0
                matched = db.execute(
                    "SELECT COUNT(*) FROM watch_observation.members"
                ).fetchone()[0]
                run_id = db.execute(
                    "INSERT INTO watch_runs (watch_id,status,op_cursor_before,op_cursor_after,"
                    "matched_rows,new_rows,error,resolved_query_hash,resolved_query,detection_policy,error_code,finished_at) "
                    "VALUES (?,?,?,?,?,0,?,?,?,?,?,datetime('now'))",
                    (
                        watch_id,
                        status,
                        op_cursor_before,
                        op_cursor_after if advance_cursor else op_cursor_before,
                        matched,
                        error,
                        resolved_query_hash,
                        canonical_json(resolved_query or {}),
                        canonical_json(policy),
                        error_code,
                    ),
                ).lastrowid
                rank = 0

                def record(event):
                    nonlocal rank
                    rank += 1
                    db.execute(
                        "INSERT INTO watch_run_events (run_id,watch_id,event_kind,subject_kind,subject_ref,"
                        "before_json,after_json,delta_json,severity,rank,snippet) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            run_id,
                            watch_id,
                            event["event_kind"],
                            event["subject_kind"],
                            event_json(event["subject_ref"]),
                            event_json(event["before_json"]),
                            event_json(event["after_json"]),
                            event_json(event["delta_json"]),
                            event["severity"],
                            rank,
                            event["snippet"],
                        ),
                    )

                new_count = 0
                if status == "ok" and baseline:
                    new_count = db.execute(
                        "SELECT COUNT(*) FROM watch_observation.members now WHERE NOT EXISTS "
                        "(SELECT 1 FROM watch_seen_rows old WHERE old.watch_id=? "
                        "AND old.sheet_id=now.sheet_id AND old.row_id=now.row_id)",
                        (watch_id,),
                    ).fetchone()[0]
                    if policy["entered"]:
                        for hit in db.execute(
                            "SELECT now.* FROM watch_observation.members now WHERE NOT EXISTS "
                            "(SELECT 1 FROM watch_seen_rows old WHERE old.watch_id=? "
                            "AND old.sheet_id=now.sheet_id AND old.row_id=now.row_id) ORDER BY now.rank",
                            (watch_id,),
                        ):
                            record(
                                row_entered_event(
                                    run_id=run_id,
                                    watch_id=watch_id,
                                    sheet_id=hit["sheet_id"],
                                    row_id=hit["row_id"],
                                    rank=0,
                                    snippet=hit["snippet"],
                                )
                            )
                    if policy["exited"]:
                        for hit in db.execute(
                            "SELECT old.*,COALESCE(unavailable.reason,'membership_changed') AS exit_reason "
                            "FROM watch_seen_rows old LEFT JOIN watch_observation.unavailable_rows unavailable "
                            "ON unavailable.sheet_id=old.sheet_id AND unavailable.row_id=old.row_id "
                            "WHERE old.watch_id=? AND NOT EXISTS "
                            "(SELECT 1 FROM watch_observation.members now WHERE old.sheet_id=now.sheet_id "
                            "AND old.row_id=now.row_id) ORDER BY old.sheet_id,old.row_id",
                            (watch_id,),
                        ):
                            record(
                                row_exited_event(
                                    run_id=run_id,
                                    watch_id=watch_id,
                                    sheet_id=hit["sheet_id"],
                                    row_id=hit["row_id"],
                                    rank=0,
                                    reason=hit["exit_reason"],
                                )
                            )
                    if (
                        policy["count_changed"]
                        and previous_policy.get("count_changed")
                        and previous_count != matched
                    ):
                        record(
                            count_changed_event(
                                run_id=run_id,
                                watch_id=watch_id,
                                previous_run_id=previous["id"],
                                previous_count=previous_count,
                                current_count=matched,
                            )
                        )
                    threshold = policy.get("threshold")
                    if threshold and threshold == previous_policy.get("threshold"):
                        crossing = _threshold_crossing(
                            previous_count=previous_count,
                            current_count=matched,
                            threshold=threshold["threshold"],
                            direction=threshold["direction"],
                        )
                        if crossing:
                            record(
                                threshold_crossed_event(
                                    run_id=run_id,
                                    watch_id=watch_id,
                                    previous_run_id=previous["id"],
                                    previous_count=previous_count,
                                    current_count=matched,
                                    threshold=threshold["threshold"],
                                    direction=threshold["direction"],
                                    crossing=crossing,
                                )
                            )
                    changed = db.execute(
                        "SELECT now.sheet_id,now.row_id,now.column_id,col.name,old.value_json before_value,now.value_json after_value "
                        "FROM watch_observation.fields now JOIN watch_row_field_state old "
                        "ON old.watch_id=? AND old.sheet_id=now.sheet_id AND old.row_id=now.row_id AND old.column_id=now.column_id "
                        "JOIN watch_seen_rows member ON member.watch_id=old.watch_id AND member.sheet_id=old.sheet_id AND member.row_id=old.row_id "
                        "JOIN columns col ON col.id=now.column_id WHERE old.value_json!=now.value_json "
                        "ORDER BY now.sheet_id,now.row_id,now.column_id",
                        (watch_id,),
                    )
                    for (sheet, row), changes in groupby(
                        changed, key=lambda cell: (cell["sheet_id"], cell["row_id"])
                    ):
                        record(
                            row_changed_event(
                                run_id=run_id,
                                watch_id=watch_id,
                                sheet_id=sheet,
                                row_id=row,
                                rank=0,
                                changed_fields=[
                                    {
                                        "sheet_id": sheet,
                                        "column_id": cell["column_id"],
                                        "name": cell["name"],
                                        "before": _display_observed(
                                            cell["before_value"]
                                        ),
                                        "after": _display_observed(cell["after_value"]),
                                    }
                                    for cell in changes
                                ],
                            )
                        )
                if status == "ok":
                    # Save a bounded preview before replacing baseline membership.
                    db.execute(
                        "INSERT INTO watch_run_hits (run_id,sheet_id,row_id,column_id,rank,snippet,is_new) "
                        "SELECT ?,now.sheet_id,now.row_id,now.column_id,now.rank,now.snippet,"
                        "(? AND NOT EXISTS (SELECT 1 FROM watch_seen_rows old WHERE old.watch_id=? "
                        "AND old.sheet_id=now.sheet_id AND old.row_id=now.row_id)) "
                        "FROM watch_observation.members now JOIN rows ON rows.id=now.row_id AND rows.sheet_id=now.sheet_id ORDER BY now.rank LIMIT 500",
                        (run_id, int(baseline), watch_id),
                    )
                    # Diff-apply: unchanged million-row checks must not rewrite their baseline.
                    db.execute(
                        "DELETE FROM watch_seen_rows WHERE watch_id=? AND NOT EXISTS (SELECT 1 FROM watch_observation.members now WHERE now.sheet_id=watch_seen_rows.sheet_id AND now.row_id=watch_seen_rows.row_id)",
                        (watch_id,),
                    )
                    db.execute(
                        "INSERT INTO watch_seen_rows (watch_id,sheet_id,row_id,first_seen_run_id,last_seen_run_id) "
                        "SELECT ?,sheet_id,row_id,?,? FROM watch_observation.members WHERE 1 "
                        "ON CONFLICT(watch_id,sheet_id,row_id) DO NOTHING",
                        (watch_id, run_id, run_id),
                    )
                    db.execute(
                        "DELETE FROM watch_row_field_state WHERE watch_id=? AND NOT EXISTS (SELECT 1 FROM watch_observation.fields now WHERE now.sheet_id=watch_row_field_state.sheet_id AND now.row_id=watch_row_field_state.row_id AND now.column_id=watch_row_field_state.column_id)",
                        (watch_id,),
                    )
                    db.execute(
                        "INSERT INTO watch_row_field_state (watch_id,sheet_id,row_id,column_id,value_json,value_hash,last_observed_run_id) "
                        "SELECT ?,sheet_id,row_id,column_id,value_json,value_hash,? FROM watch_observation.fields WHERE 1 "
                        "ON CONFLICT(watch_id,sheet_id,row_id,column_id) DO UPDATE SET "
                        "value_json=excluded.value_json,value_hash=excluded.value_hash,last_observed_run_id=excluded.last_observed_run_id "
                        "WHERE watch_row_field_state.value_json!=excluded.value_json",
                        (watch_id, run_id),
                    )
                    db.execute(
                        "UPDATE watches SET baseline_run_id=? WHERE id=?",
                        (run_id, watch_id),
                    )
                db.execute(
                    "UPDATE watch_runs SET new_rows=?,notification_pending=? WHERE id=?",
                    (new_count, int(rank > 0), run_id),
                )
                db.execute(
                    "UPDATE watches SET last_evaluated_op=?,last_run_id=?,last_status=?,updated_at=datetime('now') WHERE id=?",
                    (
                        op_cursor_after if advance_cursor else op_cursor_before,
                        run_id,
                        status,
                        watch_id,
                    ),
                )
                return int(run_id)
        finally:
            db.execute("DETACH DATABASE watch_observation")
