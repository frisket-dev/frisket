"""Alert checks observe the same View membership as the workspace."""

from __future__ import annotations

import json
from contextlib import ExitStack
from typing import Any

from frisket.engine.store import Project
from frisket.engine.store.alerts import get_alert, AlertCheckConflict
from frisket.engine.views.evaluate import open_selection
from frisket.engine.views.specs import (
    stored_selection,
    normalize_selection,
    membership_hash,
    validate_alert_selection,
)
from frisket.retrieval.query import RetrievalQueryError
from frisket.ai.embeddings.similarity import SimilarityError
from frisket.notifications.projections import emit_alert_check_notification


class AlertBindingError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def alert_query(row: Any) -> dict[str, Any]:
    """The View owns selection; the alert never keeps its own copy."""
    try:
        raw = row["view_spec"]
        spec = json.loads(raw) if isinstance(raw, str) else raw
        return stored_selection(spec)
    except (ValueError, TypeError, KeyError) as exc:
        raise AlertBindingError("selection_unavailable", str(exc)) from exc


def alert_match_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["is_new"] = bool(data.get("is_new"))
    return data


def resolve_alert_check(project: Project, row: Any) -> dict[str, Any]:
    query = normalize_selection(project, alert_query(row))
    validate_alert_selection(query)
    return {
        "alert": dict(row),
        "resolved_query": query,
        "resolved_query_hash": membership_hash(query),
    }


def evaluate_alert(
    project: Project, row: Any, *, embedding_encoder: Any = None
) -> list[dict[str, Any]]:
    selection = alert_query(row)
    validate_alert_selection(selection)
    with open_selection(
        project, selection, embedding_encoder=embedding_encoder
    ) as selected:
        return list(selected.hits())


def check_alert(
    project: Project, row: Any, *, embedding_encoder: Any = None
) -> dict[str, Any]:
    for attempt in range(2):
        try:
            return _check_alert(project, row, embedding_encoder=embedding_encoder)
        except AlertCheckConflict as exc:
            row = project.get_alert(int(row["id"]))
            if row is None or attempt:
                raise AlertBindingError("alert_check_conflict", str(exc)) from exc
    raise AssertionError("unreachable")


def _check_alert(
    project: Project, row: Any, *, embedding_encoder: Any = None
) -> dict[str, Any]:
    alert_id = int(row["id"])
    before = int(row["last_evaluated_change"] or 0)
    query, query_hash = {}, None
    error, error_code, status = None, None, "ok"
    with ExitStack() as stack:
        try:
            selection = alert_query(row)
            validate_alert_selection(selection)
            selected = stack.enter_context(
                open_selection(project, selection, embedding_encoder=embedding_encoder)
            )
            snapshot = selected.snapshot
            # Definition, membership and monitored values belong to this same read.
            expected = get_alert(snapshot, alert_id)
            if expected is None or expected["view_spec"] != row["view_spec"]:
                raise AlertBindingError(
                    "selection_changed", "The view changed before its check; retry."
                )
            hits = selected.hits()
            query, query_hash = selected.selection, selected.query_hash
            after = snapshot.change_cursor
        except (ValueError, RetrievalQueryError, SimilarityError) as exc:
            snapshot = stack.enter_context(project.read_snapshot())
            expected = get_alert(snapshot, alert_id)
            if expected is None:
                raise AlertBindingError(
                    "alert_not_found", "The alert was deleted."
                ) from exc
            error, error_code, status = (
                str(exc),
                getattr(exc, "code", "selection_unavailable"),
                "error",
            )
            hits, after = (), before
        run_id = project.record_alert_check(
            alert_id,
            hits=hits,
            status=status,
            error=error,
            change_cursor_before=before,
            change_cursor_after=after,
            resolved_query_hash=query_hash,
            resolved_query=query,
            error_code=error_code,
            advance_cursor=status == "ok",
            observation_project=snapshot,
            expected_alert=expected,
        )
    run = project.get_alert_check(run_id)
    if run is None:
        raise RuntimeError("alert check was not recorded")
    notification = emit_alert_check_notification(
        project, alert_id=alert_id, run_id=run_id
    )
    return {
        "alert_id": alert_id,
        "run_id": run_id,
        "run": dict(run),
        "hits": [
            alert_match_dict(hit) for hit in project.alert_check_matches(run_id, 500)
        ],
        "notification": notification,
    }
