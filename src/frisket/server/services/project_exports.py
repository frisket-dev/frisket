"""Project export services."""

from __future__ import annotations

import os
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frisket.authoring import actions as action_contract
from frisket.engine.store.bundle_io import UnresolvedImportExportError
from frisket.server.downloads import download_filename
from frisket.server.workspace import Workspace

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProjectExportArtifact:
    path: Path
    filename: str


class ProjectExportError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class ProjectExportService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def action_export(self, project_id: str) -> dict[str, Any]:
        self._workspace.get(project_id)
        return action_contract.export_links(project_id)

    def export_bundle(
        self,
        project_id: str,
        *,
        include_media: bool,
        include_traces: bool = False,
    ) -> ProjectExportArtifact:
        project = self._workspace.get(project_id)
        path = self._temporary_artifact(suffix=".frisket.tar.gz")
        try:
            project.export(
                path,
                include_media=include_media,
                include_traces=include_traces,
            )
        except UnresolvedImportExportError as exc:
            path.unlink(missing_ok=True)
            raise ProjectExportError(409, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            path.unlink(missing_ok=True)
            LOG.exception("Project bundle export failed")
            raise ProjectExportError(
                500,
                "Project export failed. Please try again.",
            ) from exc
        return ProjectExportArtifact(
            path=path,
            filename=self._download_filename(project, project_id, ".frisket.tar.gz"),
        )

    def export_database(self, project_id: str) -> ProjectExportArtifact:
        project = self._workspace.get(project_id)
        path = self._temporary_artifact(suffix=".frisket.db")
        try:
            project.export_database(path)
        except Exception as exc:  # noqa: BLE001
            path.unlink(missing_ok=True)
            LOG.exception("Project database export failed")
            raise ProjectExportError(
                500,
                "Project export failed. Please try again.",
            ) from exc
        return ProjectExportArtifact(
            path=path,
            filename=self._download_filename(project, project_id, ".frisket.db"),
        )

    def _temporary_artifact(self, *, suffix: str) -> Path:
        fd, path = tempfile.mkstemp(prefix="frisket-export-", suffix=suffix)
        os.close(fd)
        return Path(path)

    def _download_filename(self, project: Any, project_id: str, suffix: str) -> str:
        name = str(getattr(project, "name", None) or project_id)
        return download_filename(name, suffix)
