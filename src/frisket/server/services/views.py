"""Saved View commands and their shared read-only selection resolver."""

from __future__ import annotations

import json
from typing import Any

from frisket.contracts.http.views_lenses import (
    SavedView,
    SavedViewDelete,
    SavedViewList,
    SavedViewResolved,
)
from frisket.engine.views.evaluate import open_selection
from frisket.engine.views.specs import normalize_view_spec, stored_selection
from frisket.server.workspace import Workspace


class ViewNotFound(ValueError):
    pass


class ViewRequestError(ValueError):
    def __init__(self, *, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message

    def detail(self):
        return {"code": self.code, "message": self.message}


class ViewService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def list_views(
        self, project_id: str, *, sheet_id: int | None = None
    ) -> SavedViewList:
        project = self._workspace.get(project_id)
        return SavedViewList(
            [_view_dict(row) for row in project.views(sheet_id=sheet_id)]
        )

    def get_view(self, project_id: str, view_id: int) -> SavedView:
        return _view_dict(_require_view(self._workspace.get(project_id), view_id))

    def create_view(
        self,
        project_id: str,
        *,
        name: str,
        selection: dict[str, Any],
        presentation: dict[str, Any] | None = None,
    ) -> SavedView:
        project = self._workspace.get(project_id)
        spec = _normalize_spec(
            project, {"selection": selection, "presentation": presentation or {}}
        )
        view_id = project.add_view(_required_name(name), spec=spec)
        return _view_dict(project.get_view(view_id))

    def rename_view(self, project_id: str, view_id: int, *, name: str) -> SavedView:
        project = self._workspace.get(project_id)
        _require_view(project, view_id)
        project.update_view(view_id, name=_required_name(name))
        return _view_dict(project.get_view(view_id))

    def replace_view_definition(
        self,
        project_id: str,
        view_id: int,
        *,
        selection: dict[str, Any],
        presentation: dict[str, Any] | None = None,
    ) -> SavedView:
        project = self._workspace.get(project_id)
        _require_view(project, view_id)
        project.update_view(
            view_id,
            spec=_normalize_spec(
                project, {"selection": selection, "presentation": presentation or {}}
            ),
        )
        return _view_dict(project.get_view(view_id))

    def delete_view(self, project_id: str, view_id: int) -> SavedViewDelete:
        project = self._workspace.get(project_id)
        _require_view(project, view_id)
        project.delete_view(view_id)
        return SavedViewDelete(ok=True, deleted=view_id)

    def resolve_view(
        self, project_id: str, view_id: int, *, limit: int, offset: int
    ) -> SavedViewResolved:
        from frisket.ai.embeddings.similarity import SimilarityError
        from frisket.retrieval.query import RetrievalQueryError

        project = self._workspace.get(project_id)
        row = _require_view(project, view_id)
        try:
            definition = stored_selection(json.loads(row["spec"]))
            with open_selection(project, definition) as selected:
                refs = [
                    {
                        key: hit[key]
                        for key in (
                            "sheet_id",
                            "row_id",
                            "search_score",
                            "similarity_score",
                            "relevance_score",
                        )
                    }
                    for hit in selected.hits(offset=offset, limit=limit)
                ]
                resolved = {
                    "view_id": view_id,
                    "query_hash": selected.query_hash,
                    "refs": refs,
                    "total": selected.total,
                    "offset": offset,
                    "limit": limit,
                }
        except (ValueError, SimilarityError, RetrievalQueryError) as exc:
            raise ViewRequestError(
                code=getattr(exc, "code", "selection_unavailable"), message=str(exc)
            ) from exc
        return SavedViewResolved.model_validate(resolved)


def _require_view(project: Any, view_id: int):
    row = project.get_view(view_id)
    if row is None:
        raise ViewNotFound("view not found")
    return row


def _required_name(value: str) -> str:
    if not isinstance(value, str) or not (name := value.strip()):
        raise ValueError("name is required")
    return name


def _view_dict(row) -> SavedView:
    data = dict(row)
    data["spec"] = json.loads(data["spec"])
    data["unavailable_reason"] = data["spec"].get("unavailable_reason")
    return SavedView.model_validate(data)


def _normalize_spec(project, spec):
    try:
        return normalize_view_spec(project, spec)
    except ValueError as exc:
        raise ViewRequestError(
            code=getattr(exc, "code", "invalid_selection"), message=str(exc)
        ) from exc
