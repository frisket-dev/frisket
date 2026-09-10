"""Queue handler for row-local enclosure downloads."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from frisket.ops.enclosures import ENCLOSURE_DOWNLOAD_KIND, materialize_enclosure_row
from frisket.engine.store import Project

from .project_opener import ProjectOpener, open_payload_project
from .ports import JobHandlerContext
from .worker import HandlerRegistration, HandlerRegistry


def register_enclosure_download_handler(
    registry: HandlerRegistry,
    *,
    workspace_root: str | Path,
    fetch: Callable[[str], tuple[bytes, str, str, str | None]] | None = None,
    workspace_root_storage_org_id: int | None = None,
    project_opener: ProjectOpener | None = None,
) -> HandlerRegistration:
    root = Path(workspace_root)

    def handle(payload: dict, _context: JobHandlerContext) -> dict[str, Any]:
        project_id = str(payload["project_id"])
        sheet_id = int(payload["sheet_id"])
        row_id = int(payload["row_id"])
        if project_opener is None:
            project_root = Path(payload.get("workspace_root") or root)
            project = Project(project_root / f"{project_id}.frisket")
        else:
            project_id, project = open_payload_project(
                payload,
                workspace_root=root,
                workspace_root_storage_org_id=workspace_root_storage_org_id,
                project_opener=project_opener,
            )
        try:
            try:
                result = materialize_enclosure_row(
                    project,
                    sheet_id=sheet_id,
                    row_id=row_id,
                    force=bool(payload.get("force")),
                    fetch=fetch,
                )
            except ValueError as exc:
                result = {"status": "error", "row_id": row_id, "error": str(exc)}
            return {"project_id": project_id, "sheet_id": sheet_id, **result}
        finally:
            project.close()

    return registry.add(
        ENCLOSURE_DOWNLOAD_KIND,
        handle,
        origin="frisket.production.enclosure_download",
    )
