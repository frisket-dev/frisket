"""Project blob services for local server routes."""

from __future__ import annotations

import mimetypes
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frisket.engine.store.media_blobs import backfill_blob_metadata
from frisket.server.workspace import Workspace
from frisket.server.route_errors import RouteError
from frisket.engine.store.blob_backend import BlobNotFoundError, validate_blob_digest


@dataclass
class BlobDownload:
    path: Path
    media_type: str
    materialization: AbstractContextManager[Path]

    def close(self) -> None:
        self.materialization.__exit__(None, None, None)


class ProjectBlobRouteError(RouteError):
    pass


class ProjectBlobService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def blob_download(self, project_id: str, digest: str) -> BlobDownload:
        try:
            validate_blob_digest(digest)
        except ValueError as exc:
            raise ProjectBlobRouteError(404, "no such blob") from exc
        project = self._workspace.get(project_id)
        row = project.db.execute(
            "SELECT mime, filename FROM blobs WHERE hash=?",
            (digest,),
        ).fetchone()
        if row is None:
            raise ProjectBlobRouteError(404, "no such blob")
        media_type = (
            (row["mime"] if row else None)
            or mimetypes.guess_type(row["filename"] if row else "")[0]
            or "application/octet-stream"
        )
        materialization = project.materialize_blob(digest)
        try:
            path = Path(materialization.__enter__())
        except BlobNotFoundError as exc:
            raise ProjectBlobRouteError(404, "no such blob") from exc
        return BlobDownload(
            path=path,
            media_type=media_type,
            materialization=materialization,
        )

    def backfill_metadata(
        self,
        project_id: str,
        *,
        force: bool,
        limit: int | None,
    ) -> dict[str, Any]:
        """Recover probe facts only; acquisition title/duration are not recoverable."""

        return backfill_blob_metadata(
            self._workspace.get(project_id),
            force=force,
            limit=limit,
        )
