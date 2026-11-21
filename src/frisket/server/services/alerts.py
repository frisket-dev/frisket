"""Alert services for local server routes."""

from __future__ import annotations

import json
from typing import Any

from frisket.contracts.http.alerts import (
    Alert,
    AlertCheckEventsPage,
    AlertCheckResult,
    AlertChecksPage,
    AlertDelete,
    AlertList,
)
from frisket.engine.jobs import enqueue_notification_emit_result
from frisket.contracts.http.paging import offset_page_meta
from frisket.server.workspace import Workspace
from frisket.engine.store import Project
from frisket.alerts.events import (
    ALLOWED_EVENT_KINDS,
    ALERT_CHECK_EVENT_SCHEMA_VERSION,
    decode_alert_check_event,
)
from frisket.contracts.query_spec import (
    normalize_detection_policy,
    detection_conditions,
)
from frisket.alerts.service import AlertBindingError, check_alert


class AlertNotFound(ValueError):
    pass


class AlertCheckNotFound(ValueError):
    pass


class AlertRequestError(ValueError):
    def __init__(self, detail: Any, *, status_code: int = 400):
        super().__init__(str(detail))
        self.detail = detail
        self.status_code = status_code


class AlertService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def list_alerts(self, project_id: str) -> AlertList:
        project = self._workspace.get(project_id)
        return AlertList.model_validate(
            [_alert_dict(project, row) for row in project.alerts()]
        )

    def create_alert(
        self,
        project_id: str,
        *,
        name: str,
        view_id: int,
        detection_policy: dict[str, Any] | None,
        enabled: bool,
        schedule: str | None = "@hourly",
    ) -> Alert:
        project = self._workspace.get(project_id)
        alert_name = name.strip()
        if not alert_name:
            raise AlertRequestError("alert name is required")
        view = project.get_view(view_id)
        if view is None:
            raise AlertRequestError("view not found", status_code=404)
        sheet_id = view["sheet_id"]
        normalized_policy = _normalize_alert_detection_policy(
            project,
            sheet_id=sheet_id,
            policy=detection_policy,
        )
        try:
            alert_id = project.add_alert(
                alert_name,
                view_id=view_id,
                detection_policy=normalized_policy,
                enabled=enabled,
                schedule=schedule,
            )
        except ValueError as exc:
            raise AlertRequestError(str(exc)) from exc
        return Alert.model_validate(_alert_dict(project, project.get_alert(alert_id)))

    def check_alert(self, project_id: str, alert_id: int) -> AlertCheckResult:
        project = self._workspace.get(project_id)
        row = project.get_alert(alert_id)
        if row is None:
            raise AlertNotFound("alert not found")
        try:
            evaluation = check_alert(project, row)
        except AlertBindingError as exc:
            if exc.code == "search_index_not_ready":
                schedule = getattr(project, "_frisket_schedule_search_index", None)
                if schedule is not None:
                    schedule()
                raise AlertRequestError(
                    {"code": exc.code, "message": exc.message}, status_code=409
                ) from exc
            raise AlertRequestError(str(exc)) from exc
        except ValueError as exc:
            raise AlertRequestError(str(exc)) from exc
        enqueue_notification_emit_result(
            self._workspace.queue,
            workspace_root=self._workspace.root,
            project_id=project_id,
            storage_org_id=self._workspace.queue_storage_organization_id,
            result=evaluation.get("notification"),
            project_opener=self._workspace.project_opener,
        )
        run = evaluation["run"]
        return AlertCheckResult.model_validate(
            {
                "schema_version": "frisket.alert_check.v1",
                "alert": _alert_dict(project, project.get_alert(alert_id)),
                "run": _alert_check_dict(project, run),
                "hits": evaluation["hits"],
            }
        )

    def patch_alert(
        self,
        project_id: str,
        alert_id: int,
        *,
        fields: dict[str, Any],
    ) -> Alert:
        if not fields:
            raise AlertRequestError("at least one alert field is required")
        unsupported = set(fields) - {"name", "enabled", "detection_policy", "schedule"}
        if unsupported:
            raise AlertRequestError("unsupported alert field")
        name = fields.get("name")
        if "name" in fields:
            if not isinstance(name, str):
                raise AlertRequestError("alert name is required")
            name = name.strip()
            if not name:
                raise AlertRequestError("alert name is required")
        enabled = fields.get("enabled")
        if "enabled" in fields and not isinstance(enabled, bool):
            raise AlertRequestError("enabled must be a boolean")
        project = self._workspace.get(project_id)
        existing = project.get_alert(alert_id)
        if existing is None:
            raise AlertNotFound("alert not found")
        policy = (
            _normalize_alert_detection_policy(
                project,
                sheet_id=existing["sheet_id"],
                policy=fields["detection_policy"],
            )
            if "detection_policy" in fields
            else None
        )
        try:
            row = project.update_alert(
                alert_id,
                name=name if "name" in fields else None,
                enabled=enabled if "enabled" in fields else None,
                detection_policy=policy,
                **({"schedule": fields["schedule"]} if "schedule" in fields else {}),
            )
        except ValueError as exc:
            raise AlertRequestError(str(exc)) from exc
        if row is None:
            raise AlertNotFound("alert not found")
        return Alert.model_validate(_alert_dict(project, row))

    def delete_alert(self, project_id: str, alert_id: int) -> AlertDelete:
        project = self._workspace.get(project_id)
        if not project.delete_alert(alert_id):
            raise AlertNotFound("alert not found")
        return AlertDelete(ok=True, deleted=int(alert_id))

    def list_alert_checks(
        self,
        project_id: str,
        alert_id: int,
        *,
        offset: int,
        limit: int,
        hits_limit: int,
    ) -> AlertChecksPage:
        project = self._workspace.get(project_id)
        if project.get_alert(alert_id) is None:
            raise AlertNotFound("alert not found")
        runs = [
            _alert_check_dict(project, row, include_hits=True, hits_limit=hits_limit)
            for row in project.alert_checks_page(alert_id, offset=offset, limit=limit)
        ]
        total = project.alert_checks_total(alert_id)
        return AlertChecksPage.model_validate(
            {
                **offset_page_meta(
                    schema_version="frisket.alert_checks_page.v1",
                    order="desc",
                    offset=offset,
                    limit=limit,
                    total=total,
                    item_count=len(runs),
                ),
                "hits_limit": hits_limit,
                "runs": runs,
            }
        )

    def list_alert_check_events(
        self,
        project_id: str,
        alert_id: int,
        run_id: int,
        *,
        offset: int,
        limit: int,
        event_kind: str | None,
    ) -> AlertCheckEventsPage:
        project = self._workspace.get(project_id)
        if project.get_alert(alert_id) is None:
            raise AlertNotFound("alert not found")
        run = project.get_alert_check(run_id)
        if run is None or int(run["alert_id"]) != int(alert_id):
            raise AlertCheckNotFound("alert check not found")
        if event_kind is not None and event_kind not in ALLOWED_EVENT_KINDS:
            raise AlertRequestError("unsupported alert check event kind")
        events = [
            decode_alert_check_event(row)
            for row in project.alert_check_events(
                run_id,
                offset=offset,
                limit=limit,
                event_kind=event_kind,
            )
        ]
        total = project.alert_check_events_total(run_id, event_kind=event_kind)
        return AlertCheckEventsPage.model_validate(
            {
                **offset_page_meta(
                    schema_version=ALERT_CHECK_EVENT_SCHEMA_VERSION,
                    order="asc",
                    offset=offset,
                    limit=limit,
                    total=total,
                    item_count=len(events),
                ),
                "events": events,
            }
        )


def _json_obj(value: Any, *, field: str) -> dict[str, Any]:
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError) as exc:
        raise AlertRequestError(f"stored {field} must be valid JSON") from exc
    if not isinstance(data, dict):
        raise AlertRequestError(f"stored {field} must be an object")
    return data


def _alert_detection_policy(row) -> dict[str, Any]:
    try:
        policy = json.loads(row["detection_policy"] or "{}")
    except (TypeError, ValueError):
        policy = {}
    try:
        return detection_conditions(
            normalize_detection_policy(policy if isinstance(policy, dict) else {})
        )
    except ValueError:
        return detection_conditions({"kind": "new_matches"})


def _alert_check_dict(
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
            _alert_match_dict(hit)
            for hit in project.alert_check_matches(data["id"], hits_limit)
        ]
        data["hits"] = hits
        data["hits_limit"] = hits_limit
        data["hits_truncated"] = int(data.get("matched_rows") or 0) > len(hits)
    return data


def _alert_match_dict(row) -> dict[str, Any]:
    data = dict(row)
    data["is_new"] = bool(data.get("is_new"))
    return data


def _alert_dict(project: Project, row) -> dict[str, Any]:
    data = dict(row)
    data.pop("baseline_run_id", None)
    data["enabled"] = bool(data.get("enabled"))
    data.pop("view_spec", None)
    data["detection_policy"] = _alert_detection_policy(row)
    latest = project.alert_latest_run(data["id"])
    data["latest_run"] = (
        _alert_check_dict(project, latest) if latest is not None else None
    )
    return data


def _normalize_alert_detection_policy(
    project: Project,
    *,
    sheet_id: int | None,
    policy: dict[str, Any] | None,
) -> dict[str, Any]:
    try:
        normalized = normalize_detection_policy(policy)
    except ValueError as exc:
        raise AlertRequestError(str(exc)) from exc
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
                raise AlertRequestError("row_changed field column_id not found")
            if field_sheet_id is not None and int(column["sheet_id"]) != int(
                field_sheet_id
            ):
                raise AlertRequestError(
                    "row_changed field sheet_id must match the referenced column"
                )
            field_sheet_id = int(column["sheet_id"])
        else:
            if field_sheet_id is None:
                raise AlertRequestError(
                    "row_changed project-scoped fields require column_id"
                )
            columns = project.columns(int(field_sheet_id))
            column = next(
                (col for col in columns if str(col["name"]) == str(field.get("name"))),
                None,
            )
            if column is None:
                raise AlertRequestError("row_changed field name not found")
        fields.append(
            {
                "sheet_id": int(field_sheet_id),
                "column_id": int(column["id"]),
                "name": str(field.get("name") or column["name"]),
            }
        )
    return {**normalized, "fields": fields}
