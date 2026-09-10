"""Saved view and saved lens services."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from frisket.preview.query import (
    QueryPreviewError,
    query_preview_payload,
    resolve_query_preview,
)
from frisket.server.workspace import Workspace
from frisket.engine.store import Project
from frisket.features.watchlists.specs import normalize_lens_spec


class ViewNotFound(ValueError):
    pass


class LensNotFound(ValueError):
    pass


class LensRequestError(ValueError):
    def __init__(self, *, code: str, message: str, field: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field

    def detail(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "field": self.field,
        }


class ViewLensService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def list_views(
        self,
        project_id: str,
        *,
        sheet_id: int | None = None,
    ) -> list[dict[str, Any]]:
        project = self._workspace.get(project_id)
        return [_view_dict(project, row) for row in project.views(sheet_id=sheet_id)]

    def create_view(
        self,
        project_id: str,
        *,
        name: str,
        sheet_id: int,
        filter_: dict[str, Any],
        sort: list[Any] | None,
        columns: list[Any] | None,
        column_groups: list[Any] | None,
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        name = _required_name(name)
        if not isinstance(filter_, dict):
            raise ValueError("filter must be an object")
        view_id = project.add_view(
            name,
            spec=_view_spec(
                filter_=filter_,
                sort=sort,
                columns=columns,
                column_groups=column_groups,
            ),
            sheet_id=sheet_id,
        )
        return _view_dict(project, project.get_view(view_id))

    def get_view(self, project_id: str, view_id: int) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        row = project.get_view(view_id)
        if row is None:
            raise ViewNotFound("view not found")
        return _view_dict(project, row)

    def rename_view(
        self,
        project_id: str,
        view_id: int,
        *,
        name: str,
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        if project.get_view(view_id) is None:
            raise ViewNotFound("view not found")
        name = _required_name(name)
        project.update_view(view_id, name=name)
        return _view_dict(project, project.get_view(view_id))

    def replace_view_definition(
        self,
        project_id: str,
        view_id: int,
        *,
        filter_: dict[str, Any],
        sort: list[Any] | None,
        columns: list[Any] | None,
        column_groups: list[Any] | None,
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        if project.get_view(view_id) is None:
            raise ViewNotFound("view not found")
        if not isinstance(filter_, dict):
            raise ValueError("filter must be an object")
        project.update_view(
            view_id,
            spec=_view_spec(
                filter_=filter_,
                sort=sort,
                columns=columns,
                column_groups=column_groups,
            ),
        )
        return _view_dict(project, project.get_view(view_id))

    def delete_view(self, project_id: str, view_id: int) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        if project.get_view(view_id) is None:
            raise ViewNotFound("view not found")
        project.delete_view(view_id)
        return {"ok": True, "deleted": view_id}

    def list_lenses(
        self,
        project_id: str,
        *,
        sheet_id: int | None = None,
    ) -> list[dict[str, Any]]:
        project = self._workspace.get(project_id)
        return [_lens_dict(row) for row in project.lenses(sheet_id=sheet_id)]

    def create_lens(
        self,
        project_id: str,
        *,
        name: str,
        query: dict[str, Any],
        presentation: dict[str, Any] | None,
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        spec = _build_lens_spec(query, presentation)
        lens_id = project.add_lens(name, spec, sheet_id=_lens_sheet_id(spec))
        return _lens_dict(project.get_lens(lens_id))

    def get_lens(self, project_id: str, lens_id: int) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        row = project.get_lens(lens_id)
        if row is None:
            raise LensNotFound("lens not found")
        return _lens_dict(row)

    def patch_lens(
        self,
        project_id: str,
        lens_id: int,
        *,
        fields: Mapping[str, Any],
        provided_fields: Iterable[str],
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        if project.get_lens(lens_id) is None:
            raise LensNotFound("lens not found")
        provided = {key: fields[key] for key in provided_fields}
        spec = None
        if "query" in provided:
            spec = _build_lens_spec(provided["query"], provided.get("presentation"))
        project.update_lens(lens_id, name=provided.get("name"), spec=spec)
        return _lens_dict(project.get_lens(lens_id))

    def delete_lens(self, project_id: str, lens_id: int) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        if project.get_lens(lens_id) is None:
            raise LensNotFound("lens not found")
        project.delete_lens(lens_id)
        return {"ok": True, "deleted": lens_id}

    def resolve_lens(
        self,
        project_id: str,
        lens_id: int,
        *,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        row = project.get_lens(lens_id)
        if row is None:
            raise LensNotFound("lens not found")
        spec = _lens_dict(row)["spec"]
        query = spec.get("query") or {}
        try:
            preview = resolve_query_preview(project, query, limit=limit, offset=offset)
        except QueryPreviewError as exc:
            raise LensRequestError(
                code=exc.code,
                message=exc.message,
                field=exc.field,
            ) from exc
        return {"lens_id": lens_id, **query_preview_payload(preview)}


def _view_dict(project: Project, row) -> dict[str, Any]:
    del project
    data = dict(row)
    try:
        data["spec"] = json.loads(data.get("spec") or "{}")
    except (TypeError, ValueError):
        data["spec"] = {}
    return data


def _required_name(value: str) -> str:
    if not isinstance(value, str) or not (name := value.strip()):
        raise ValueError("name is required")
    return name


def _view_spec(
    *,
    filter_: dict[str, Any],
    sort: list[Any] | None,
    columns: list[Any] | None,
    column_groups: list[Any] | None,
) -> dict[str, Any]:
    spec: dict[str, Any] = {"filter": filter_}
    if sort is not None:
        spec["sort"] = sort
    if columns is not None:
        spec["columns"] = columns
    if column_groups is not None:
        spec["column_groups"] = column_groups
    return spec


def _lens_dict(row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["spec"] = json.loads(data.get("spec") or "{}")
    except (TypeError, ValueError):
        data["spec"] = {}
    return data


def _build_lens_spec(
    query: dict[str, Any],
    presentation: dict[str, Any] | None,
) -> dict[str, Any]:
    try:
        return normalize_lens_spec(query, presentation=presentation)
    except ValueError as exc:
        raise LensRequestError(
            code="invalid_lens_spec",
            message=str(exc),
            field="query",
        ) from exc


def _lens_sheet_id(spec: dict[str, Any]) -> int | None:
    scope = (spec.get("query") or {}).get("scope") or {}
    sheet_id = scope.get("sheet_id")
    return sheet_id if isinstance(sheet_id, int) else None
