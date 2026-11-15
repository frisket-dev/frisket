"""Watchlist services for local server routes."""

from __future__ import annotations

import json
from typing import Any

from frisket.engine.jobs import enqueue_notification_emit_result
from frisket.server.paging import offset_page_meta
from frisket.server.workspace import Workspace
from frisket.engine.store import Project
from frisket.features.watchlists.events import (
    ALLOWED_EVENT_KINDS,
    WATCH_RUN_EVENT_SCHEMA_VERSION,
    decode_watch_run_event,
)
from frisket.contracts.query_spec import (
    normalize_detection_policy,
    detection_conditions,
)
from frisket.features.watchlists.service import WatchBindingError, run_watch_evaluation


class WatchNotFound(ValueError):
    pass


class WatchRunNotFound(ValueError):
    pass


class WatchRequestError(ValueError):
    def __init__(self, detail: Any, *, status_code: int = 400):
        super().__init__(str(detail))
        self.detail = detail
        self.status_code = status_code


class WatchService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def list_watches(self, project_id: str) -> list[dict[str, Any]]:
        project = self._workspace.get(project_id)
        return [_watch_dict(project, row) for row in project.watches()]

    def create_watch(
        self,
        project_id: str,
        *,
        name: str,
        view_id: int,
        detection_policy: dict[str, Any] | None,
        enabled: bool,
        schedule: str | None = "@hourly",
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        watch_name = name.strip()
        if not watch_name:
            raise WatchRequestError("watch name is required")
        view = project.get_view(view_id)
        if view is None:
            raise WatchRequestError("view not found", status_code=404)
        sheet_id = view["sheet_id"]
        normalized_policy = _normalize_watch_detection_policy(
            project,
            sheet_id=sheet_id,
            policy=detection_policy,
        )
        try:
            watch_id = project.add_watch(
                watch_name,
                view_id=view_id,
                detection_policy=normalized_policy,
                enabled=enabled,
                schedule=schedule,
            )
        except ValueError as exc:
            raise WatchRequestError(str(exc)) from exc
        return _watch_dict(project, project.get_watch(watch_id))

    def run_watch(self, project_id: str, watch_id: int) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        row = project.get_watch(watch_id)
        if row is None:
            raise WatchNotFound("watch not found")
        try:
            evaluation = run_watch_evaluation(project, row)
        except WatchBindingError as exc:
            if exc.code == "search_index_not_ready":
                schedule = getattr(project, "_frisket_schedule_search_index", None)
                if schedule is not None:
                    schedule()
                raise WatchRequestError(
                    {"code": exc.code, "message": exc.message}, status_code=409
                ) from exc
            raise WatchRequestError(str(exc)) from exc
        except ValueError as exc:
            raise WatchRequestError(str(exc)) from exc
        enqueue_notification_emit_result(
            self._workspace.queue,
            workspace_root=self._workspace.root,
            project_id=project_id,
            storage_org_id=self._workspace.queue_storage_org_id,
            result=evaluation.get("notification"),
            project_opener=self._workspace.project_opener,
        )
        run = evaluation["run"]
        return {
            "schema_version": "frisket.watch_run.v1",
            "watch": _watch_dict(project, project.get_watch(watch_id)),
            "run": _watch_run_dict(project, run),
            "hits": evaluation["hits"],
        }

    def patch_watch(
        self,
        project_id: str,
        watch_id: int,
        *,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        if not fields:
            raise WatchRequestError("at least one watch field is required")
        unsupported = set(fields) - {"name", "enabled", "detection_policy", "schedule"}
        if unsupported:
            raise WatchRequestError("unsupported watch field")
        name = fields.get("name")
        if "name" in fields:
            if not isinstance(name, str):
                raise WatchRequestError("watch name is required")
            name = name.strip()
            if not name:
                raise WatchRequestError("watch name is required")
        enabled = fields.get("enabled")
        if "enabled" in fields and not isinstance(enabled, bool):
            raise WatchRequestError("enabled must be a boolean")
        project = self._workspace.get(project_id)
        existing = project.get_watch(watch_id)
        if existing is None:
            raise WatchNotFound("watch not found")
        policy = (
            _normalize_watch_detection_policy(
                project,
                sheet_id=existing["sheet_id"],
                policy=fields["detection_policy"],
            )
            if "detection_policy" in fields
            else None
        )
        try:
            row = project.update_watch(
                watch_id,
                name=name if "name" in fields else None,
                enabled=enabled if "enabled" in fields else None,
                detection_policy=policy,
                **({"schedule": fields["schedule"]} if "schedule" in fields else {}),
            )
        except ValueError as exc:
            raise WatchRequestError(str(exc)) from exc
        if row is None:
            raise WatchNotFound("watch not found")
        return _watch_dict(project, row)

    def delete_watch(self, project_id: str, watch_id: int) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        if not project.delete_watch(watch_id):
            raise WatchNotFound("watch not found")
        return {"ok": True, "deleted": int(watch_id)}

    def list_watch_runs(
        self,
        project_id: str,
        watch_id: int,
        *,
        offset: int,
        limit: int,
        hits_limit: int,
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        if project.get_watch(watch_id) is None:
            raise WatchNotFound("watch not found")
        runs = [
            _watch_run_dict(project, row, include_hits=True, hits_limit=hits_limit)
            for row in project.watch_runs_page(watch_id, offset=offset, limit=limit)
        ]
        total = project.watch_runs_total(watch_id)
        return {
            **offset_page_meta(
                schema_version="frisket.watch_runs_page.v1",
                order="desc",
                offset=offset,
                limit=limit,
                total=total,
                item_count=len(runs),
            ),
            "hits_limit": hits_limit,
            "runs": runs,
        }

    def list_watch_run_events(
        self,
        project_id: str,
        watch_id: int,
        run_id: int,
        *,
        offset: int,
        limit: int,
        event_kind: str | None,
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        if project.get_watch(watch_id) is None:
            raise WatchNotFound("watch not found")
        run = project.get_watch_run(run_id)
        if run is None or int(run["watch_id"]) != int(watch_id):
            raise WatchRunNotFound("watch run not found")
        if event_kind is not None and event_kind not in ALLOWED_EVENT_KINDS:
            raise WatchRequestError("unsupported watch run event kind")
        events = [
            decode_watch_run_event(row)
            for row in project.watch_run_events(
                run_id,
                offset=offset,
                limit=limit,
                event_kind=event_kind,
            )
        ]
        total = project.watch_run_events_total(run_id, event_kind=event_kind)
        return {
            **offset_page_meta(
                schema_version=WATCH_RUN_EVENT_SCHEMA_VERSION,
                order="asc",
                offset=offset,
                limit=limit,
                total=total,
                item_count=len(events),
            ),
            "events": events,
        }


def _json_obj(value: Any, *, field: str) -> dict[str, Any]:
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError) as exc:
        raise WatchRequestError(f"stored {field} must be valid JSON") from exc
    if not isinstance(data, dict):
        raise WatchRequestError(f"stored {field} must be an object")
    return data


def _watch_detection_policy(row) -> dict[str, Any]:
    try:
        policy = json.loads(row["detection_policy"] or "{}")
    except (TypeError, ValueError):
        policy = {}
    try:
        return detection_conditions(normalize_detection_policy(policy if isinstance(policy, dict) else {}))
    except ValueError:
        return detection_conditions({"kind": "new_matches"})


def _watch_run_dict(
    project: Project,
    row,
    *,
    include_hits: bool = False,
    hits_limit: int = 20,
) -> dict[str, Any]:
    data = dict(row)
    data.pop("detection_policy", None)
    data.pop("notification_pending", None)
    data["resolved_query"] = _json_obj(
        data.get("resolved_query"), field="resolved query"
    )
    if include_hits:
        hits = [
            _watch_hit_dict(hit)
            for hit in project.watch_run_hits(data["id"], hits_limit)
        ]
        data["hits"] = hits
        data["hits_limit"] = hits_limit
        data["hits_truncated"] = int(data.get("matched_rows") or 0) > len(hits)
    return data


def _watch_hit_dict(row) -> dict[str, Any]:
    data = dict(row)
    data["is_new"] = bool(data.get("is_new"))
    return data


def _watch_dict(project: Project, row) -> dict[str, Any]:
    data = dict(row)
    data.pop("baseline_run_id", None)
    data["enabled"] = bool(data.get("enabled"))
    data.pop("view_spec", None)
    data["detection_policy"] = _watch_detection_policy(row)
    latest = project.watch_latest_run(data["id"])
    data["latest_run"] = (
        _watch_run_dict(project, latest) if latest is not None else None
    )
    return data


def _normalize_watch_detection_policy(
    project: Project,
    *,
    sheet_id: int | None,
    policy: dict[str, Any] | None,
) -> dict[str, Any]:
    try:
        normalized = normalize_detection_policy(policy)
    except ValueError as exc:
        raise WatchRequestError(str(exc)) from exc
    if normalized["kind"] not in {"row_changed", "conditions"}:
        return normalized

    fields: list[dict[str, Any]] = []
    for field in normalized["fields"]:
        field_sheet_id = field.get("sheet_id") or sheet_id
        column = None
        if field.get("column_id") is not None:
            column = project.db.execute(
                "SELECT id, sheet_id, name FROM columns WHERE id=? AND active=1",
                (int(field["column_id"]),),
            ).fetchone()
            if column is None:
                raise WatchRequestError("row_changed field column_id not found")
            if field_sheet_id is not None and int(column["sheet_id"]) != int(
                field_sheet_id
            ):
                raise WatchRequestError(
                    "row_changed field sheet_id must match the referenced column"
                )
            field_sheet_id = int(column["sheet_id"])
        else:
            if field_sheet_id is None:
                raise WatchRequestError(
                    "row_changed project-scoped fields require column_id"
                )
            columns = project.columns(int(field_sheet_id))
            column = next(
                (col for col in columns if str(col["name"]) == str(field.get("name"))),
                None,
            )
            if column is None:
                raise WatchRequestError("row_changed field name not found")
        fields.append(
            {
                "sheet_id": int(field_sheet_id),
                "column_id": int(column["id"]),
                "name": str(field.get("name") or column["name"]),
            }
        )
    return {**normalized, "fields": fields}
