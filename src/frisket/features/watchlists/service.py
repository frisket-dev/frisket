"""Alert checks observe the same saved View membership as the workspace."""

from __future__ import annotations

import json
from contextlib import ExitStack
from typing import Any

from frisket.engine.store import Project
from frisket.engine.store.watches import get_watch, WatchCheckConflict
from frisket.engine.views.evaluate import open_selection
from frisket.engine.views.specs import (
    stored_selection,
    normalize_selection,
    membership_hash,
)
from frisket.retrieval.query import RetrievalQueryError
from frisket.ai.embeddings.similarity import SimilarityError
from frisket.server.notifications.projections import emit_watch_run_notification


class WatchBindingError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def watch_query(row: Any) -> dict[str, Any]:
    """The View owns selection; the alert never keeps its own copy."""
    try:
        raw = row["view_spec"]
        spec = json.loads(raw) if isinstance(raw, str) else raw
        return stored_selection(spec)
    except (ValueError, TypeError, KeyError) as exc:
        raise WatchBindingError("selection_unavailable", str(exc)) from exc


def watch_hit_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["is_new"] = bool(data.get("is_new"))
    return data


def resolve_watch_run(project: Project, row: Any) -> dict[str, Any]:
    query = normalize_selection(project, watch_query(row))
    return {
        "watch": dict(row),
        "resolved_query": query,
        "resolved_query_hash": membership_hash(query),
    }


def evaluate_watch(project: Project, row: Any) -> list[dict[str, Any]]:
    with open_selection(project, watch_query(row)) as selected:
        return list(selected.hits())


def run_watch_evaluation(project: Project, row: Any) -> dict[str, Any]:
    for attempt in range(2):
        try:
            return _run_watch_evaluation(project, row)
        except WatchCheckConflict as exc:
            row = project.get_watch(int(row["id"]))
            if row is None or attempt:
                raise WatchBindingError("watch_check_conflict", str(exc)) from exc
    raise AssertionError("unreachable")


def _run_watch_evaluation(project: Project, row: Any) -> dict[str, Any]:
    watch_id = int(row["id"])
    before = int(row["last_evaluated_op"] or 0)
    query, query_hash = {}, None
    error, error_code, status = None, None, "ok"
    with ExitStack() as stack:
        try:
            selected = stack.enter_context(open_selection(project, watch_query(row)))
            snapshot = selected.snapshot
            # Definition, membership and monitored values belong to this same read.
            expected = get_watch(snapshot, watch_id)
            if expected is None or expected["view_spec"] != row["view_spec"]:
                raise WatchBindingError(
                    "selection_changed", "The view changed before its check; retry."
                )
            hits = selected.hits()
            query, query_hash = selected.selection, selected.query_hash
            after = snapshot.op_cursor
        except (ValueError, RetrievalQueryError, SimilarityError) as exc:
            snapshot = stack.enter_context(project.read_snapshot())
            expected = get_watch(snapshot, watch_id)
            if expected is None:
                raise WatchBindingError(
                    "watch_not_found", "The alert was deleted."
                ) from exc
            error, error_code, status = (
                str(exc),
                getattr(exc, "code", "selection_unavailable"),
                "error",
            )
            hits, after = (), before
        run_id = project.record_watch_run(
            watch_id,
            hits=hits,
            status=status,
            error=error,
            op_cursor_before=before,
            op_cursor_after=after,
            resolved_query_hash=query_hash,
            resolved_query=query,
            error_code=error_code,
            advance_cursor=status == "ok",
            observation_project=snapshot,
            expected_watch=expected,
        )
    run = project.get_watch_run(run_id)
    if run is None:
        raise RuntimeError("watch run was not recorded")
    notification = emit_watch_run_notification(
        project, watch_id=watch_id, run_id=run_id
    )
    return {
        "watch_id": watch_id,
        "run_id": run_id,
        "run": dict(run),
        "hits": [watch_hit_dict(hit) for hit in project.watch_run_hits(run_id, 500)],
        "notification": notification,
    }
