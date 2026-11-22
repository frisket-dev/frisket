"""Source materialization triggers for alert evaluation."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, TypedDict

from frisket.contracts.query_spec import (
    EMBEDDING_SIMILARITY_KIND,
    LOCAL_RETRIEVAL_SIMILARITY_KIND,
)
from frisket.engine.jobs.search_index import index_work_marker, process_index_batches
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.alerts.service import (
    AlertBindingError,
    check_alert,
    alert_query,
)


class FeedPollMaterialization(TypedDict):
    kind: str
    project_id: str
    feed_id: int
    feed_check_id: int
    sheet_id: int
    op_id: int
    row_ids: list[int]
    materialized_rows: int


def feed_poll_materialization_from_receipt(
    project: Project,
    *,
    project_id: str,
    receipt_id: str,
) -> FeedPollMaterialization | None:
    body = ReceiptStore(project).body_by_id(receipt_id)
    if body is None:
        return None
    try:
        receipt = json.loads(body)
    except (TypeError, ValueError):
        return None
    if receipt.get("action_kind") != "feed.poll":
        return None
    if receipt.get("status") != "completed":
        return None
    refs = _receipt_refs(receipt)
    feed_check = next(
        (ref for ref in refs if ref.get("kind") == "feed_poll_check"),
        {},
    )
    rows = next(
        (ref for ref in refs if ref.get("kind") == "feed_poll_rows"),
        {},
    )
    if feed_check.get("status") != "ok":
        return None
    materialized_rows = _positive_int_or_none(feed_check.get("materialized_rows"))
    if materialized_rows is None or materialized_rows <= 0:
        return None
    row_ids = [_as_positive_int(row_id) for row_id in rows.get("row_ids") or []]
    row_ids = [row_id for row_id in row_ids if row_id is not None]
    if not row_ids:
        return None
    feed_id = _positive_int_or_none(feed_check.get("feed_id") or rows.get("feed_id"))
    feed_check_id = _positive_int_or_none(
        feed_check.get("feed_check_id") or rows.get("feed_check_id")
    )
    sheet_id = _positive_int_or_none(feed_check.get("sheet_id") or rows.get("sheet_id"))
    op_id = _positive_int_or_none(feed_check.get("op_id") or rows.get("op_id"))
    if feed_id is None or feed_check_id is None or sheet_id is None or op_id is None:
        return None
    return {
        "kind": "feed_poll_materialized",
        "project_id": project_id,
        "feed_id": feed_id,
        "feed_check_id": feed_check_id,
        "sheet_id": sheet_id,
        "op_id": op_id,
        "row_ids": row_ids,
        "materialized_rows": materialized_rows,
    }


def _drain_standalone_local_retrieval(project: Project, embedding_encoder: Any) -> None:
    if embedding_encoder is None or not embedding_encoder.available():
        return
    for _ in range(32):
        processed, complete = process_index_batches(
            project, embedding_encoder=embedding_encoder
        )
        if complete or index_work_marker(project, include_preparation=True) is None:
            break
        if processed == 0:
            break


def _run_alert_check(
    project: Project, alert: Any, embedding_encoder: Any
) -> dict[str, Any]:
    if embedding_encoder is None:
        return check_alert(project, alert)
    return check_alert(project, alert, embedding_encoder=embedding_encoder)


def trigger_source_materialized_alerts(
    project: Project,
    materialization: FeedPollMaterialization,
    *,
    enqueue_keyword_alerts: Callable[[list[Any], FeedPollMaterialization], None]
    | None = None,
    embedding_encoder: Any = None,
) -> list[dict[str, Any]]:
    if (
        materialization.get("kind") != "feed_poll_materialized"
        or materialization.get("op_id") is None
        or not materialization.get("row_ids")
    ):
        return []
    alerts = affected_enabled_alerts(project, materialization)
    indexed_alerts = [
        alert
        for alert in alerts
        if _is_keyword_alert(alert) or _is_local_retrieval_alert(alert)
    ]
    if indexed_alerts and enqueue_keyword_alerts is not None:
        enqueue_keyword_alerts(indexed_alerts, materialization)
        alerts = [
            alert
            for alert in alerts
            if not (_is_keyword_alert(alert) or _is_local_retrieval_alert(alert))
        ]
    elif indexed_alerts:
        # Standalone synchronous callers have no worker that could finish this
        # dependency; preserve their established complete-on-return contract.
        from frisket.search import drain_index

        drain_index(project)
        if any(_is_local_retrieval_alert(alert) for alert in indexed_alerts):
            _drain_standalone_local_retrieval(project, embedding_encoder)
    out: list[dict[str, Any]] = []
    for alert in alerts:
        try:
            evaluation = _run_alert_check(project, alert, embedding_encoder)
        except AlertBindingError as exc:
            if exc.code != "search_index_not_ready" or not _is_keyword_alert(alert):
                out.append(
                    {
                        "alert_id": int(alert["id"]),
                        "status": "error",
                        "error_code": exc.code,
                        "error": str(exc),
                    }
                )
                continue
            # A writer may land after the standalone drain. Retry only this
            # alert once; previous successful alerts must not emit twice.
            from frisket.search import drain_index

            drain_index(project)
            evaluation = _run_alert_check(project, alert, embedding_encoder)
        evaluation.update(
            {
                "trigger_kind": "feed_poll_materialized",
                "feed_id": materialization["feed_id"],
                "feed_check_id": materialization["feed_check_id"],
                "sheet_id": materialization["sheet_id"],
                "op_id": materialization["op_id"],
                "materialized_row_ids": list(materialization["row_ids"]),
            }
        )
        out.append(evaluation)
    return out


def trigger_completed_feed_poll_alerts(
    project: Project,
    *,
    project_id: str,
    receipt_id: str | None,
    enqueue_keyword_alerts: Callable[[list[Any], FeedPollMaterialization], None]
    | None = None,
    embedding_encoder: Any = None,
) -> list[dict[str, Any]]:
    if not receipt_id:
        return []
    materialization = feed_poll_materialization_from_receipt(
        project,
        project_id=project_id,
        receipt_id=receipt_id,
    )
    if materialization is None:
        return []
    return trigger_source_materialized_alerts(
        project,
        materialization,
        enqueue_keyword_alerts=enqueue_keyword_alerts,
        embedding_encoder=embedding_encoder,
    )


def affected_enabled_alerts(
    project: Project,
    materialization: FeedPollMaterialization,
) -> list[Any]:
    sheet_id = int(materialization["sheet_id"])
    out: list[Any] = []
    for alert in project.alerts():
        if not bool(alert["enabled"]) or alert["schedule"] is None:
            continue
        # Named embedding_similarity alerts are evaluated AFTER their index
        # refresh job completes (jobs/alerts.py), not inline on the source poll.
        # Local retrieval and keyword/filter alerts remain affected here.
        if _is_embedding_similarity_alert(alert):
            continue
        if _alert_may_be_affected(alert, sheet_id=sheet_id):
            out.append(alert)
    return out


def _alert_similarity_kind(alert: Any) -> str | None:
    try:
        similarity = alert_query(alert).get("similarity")
    except AlertBindingError:
        return None
    if isinstance(similarity, dict):
        kind = similarity.get("kind")
        return str(kind) if kind is not None else None
    return None


def _is_embedding_similarity_alert(alert: Any) -> bool:
    return _alert_similarity_kind(alert) == EMBEDDING_SIMILARITY_KIND


def _is_local_retrieval_alert(alert: Any) -> bool:
    return _alert_similarity_kind(alert) == LOCAL_RETRIEVAL_SIMILARITY_KIND


def _is_keyword_alert(alert: Any) -> bool:
    try:
        return bool(alert_query(alert).get("search"))
    except AlertBindingError:
        return False


def _alert_may_be_affected(alert: Any, *, sheet_id: int) -> bool:
    try:
        scope = alert_query(alert)["scope"]
        return scope["kind"] == "project" or scope.get("sheet_id") == sheet_id
    except AlertBindingError:
        # Keep polling unavailable definitions; restoration can make them usable.
        return True


def _receipt_refs(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for item in [
        *receipt.get("inputs", []),
        *receipt.get("outputs", []),
        *receipt.get("records", []),
    ]:
        if not isinstance(item, dict):
            continue
        ref = item.get("ref")
        if isinstance(ref, dict):
            refs.append(ref)
    return refs


def _positive_int_or_none(value: Any) -> int | None:
    out = _as_positive_int(value)
    return out if out is not None and out > 0 else None


def _as_positive_int(value: Any) -> int | None:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None
