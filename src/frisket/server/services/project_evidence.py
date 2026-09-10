"""Project evidence services for local server routes."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from frisket.contracts.action import ActionError as V1ActionError
from frisket.server.workspace import Workspace
from frisket.engine.store import Project
from frisket.engine.store.blob_backend import BlobNotFoundError
from frisket.engine.store.evidence import (
    list_cell_evidence,
    list_column_evidence,
    resolve_evidence_viewer,
)
from frisket.engine.store.text_annotations import resolve_text_annotations
from frisket.engine.store.media_clip import (
    ClipError,
    clip_error_status,
    clip_filename,
    cut_clip,
    output_suffix,
    padded_range,
    resolve_clip_source,
    resolve_link_run_clip_source,
)
from frisket.server.route_errors import RouteError


# Mirrors sheet_grid.py's MAX_EXPLICIT_ROW_IDS bound on the same
# comma-separated `row_ids` query-param convention — a separate constant
# (not an import) so this module stays independent of sheet_grid's
# internals, matching the "no cross-service private imports" discipline.
MAX_EXPLICIT_ROW_IDS = 1000


class ProjectEvidenceRouteError(RouteError):
    pass


class ProjectEvidenceService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def cell_evidence(
        self,
        project_id: str,
        *,
        row_id: int,
        column_id: int,
        include_stale: bool,
    ) -> Any:
        project = self._project_or_404(project_id)
        sheet_id = _sheet_id_for_project_cell(
            project,
            row_id=row_id,
            column_id=column_id,
        )
        if sheet_id is None:
            raise ProjectEvidenceRouteError(
                404,
                _v1_action_error(
                    code="cell_not_found",
                    message="No cell exists for that row and column in this project.",
                    field="cell",
                    details={"row_id": row_id, "column_id": column_id},
                ),
                bare_json=True,
            )
        return list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=column_id,
            include_stale=include_stale,
            project_id=project_id,
        )

    def cell_text_annotations(
        self,
        project_id: str,
        *,
        row_id: int,
        column_id: int,
    ) -> Any:
        """The annotation layers whose
        coordinate surface IS this cell — what the reader draws marks from.

        The store function is already fail-closed (guard triple, current-ref
        intersection, hash positioning, allowlisted DTO), so this adds only
        project/cell resolution. A cell that simply has no layers is a 200 with
        `layers: []`, not a 404: "nothing annotated this text" is an answer."""
        project = self._project_or_404(project_id)
        sheet_id = _sheet_id_for_project_cell(
            project,
            row_id=row_id,
            column_id=column_id,
        )
        if sheet_id is None:
            raise ProjectEvidenceRouteError(
                404,
                _v1_action_error(
                    code="cell_not_found",
                    message="No cell exists for that row and column in this project.",
                    field="cell",
                    details={"row_id": row_id, "column_id": column_id},
                ),
                bare_json=True,
            )
        return resolve_text_annotations(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=column_id,
        )

    def column_evidence(
        self,
        project_id: str,
        *,
        sheet_id: int,
        column_id: int,
        row_ids: str | None,
    ) -> Any:
        """The Grounded Answers reading view's middle-pane batch — one request per column
        instead of one `cell_evidence` request per row."""
        project = self._project_or_404(project_id)
        if not _column_belongs_to_sheet(
            project, sheet_id=sheet_id, column_id=column_id
        ):
            raise ProjectEvidenceRouteError(
                404,
                _v1_action_error(
                    code="column_not_found",
                    message="No column exists with that id on that sheet in this project.",
                    field="column_id",
                    details={"sheet_id": sheet_id, "column_id": column_id},
                ),
                bare_json=True,
            )
        parsed_row_ids = _parse_row_ids_param(row_ids)
        return list_column_evidence(
            project,
            sheet_id=sheet_id,
            column_id=column_id,
            row_ids=parsed_row_ids,
            project_id=project_id,
        )

    def evidence_viewer(
        self,
        project_id: str,
        evidence_link_id: str,
    ) -> Any:
        project = self._project_or_404(project_id)
        try:
            return resolve_evidence_viewer(
                project,
                evidence_link_id,
                project_id=project_id,
            )
        except KeyError as exc:
            raise ProjectEvidenceRouteError(
                404,
                _v1_action_error(
                    code="evidence_link_not_found",
                    message="No evidence link exists with that id in this project.",
                    field="evidence_link_id",
                    details={"evidence_link_id": evidence_link_id},
                ),
                bare_json=True,
            ) from exc

    async def evidence_span_clip(
        self,
        project_id: str,
        span_stable_id: str,
        *,
        pad_ms: int,
    ) -> FileResponse:
        """provenance-download-clip-v1: ffmpeg-cut a temporal span's own
        blob (no lineage traversal -- W3.2) and stream it back as a
        download. Raises ProjectEvidenceRouteError, mapped from
        ClipError's guard/cap failures, on every rejection path."""
        project = self._project_or_404(project_id)
        try:
            source = resolve_clip_source(project, span_stable_id)
            start_ms, end_ms = padded_range(source, pad_ms=pad_ms)
        except ClipError as exc:
            raise ProjectEvidenceRouteError(
                clip_error_status(exc),
                _v1_action_error(
                    code=exc.code,
                    message=exc.message,
                    field="span_stable_id",
                    details={"span_stable_id": span_stable_id},
                ),
                bare_json=True,
            ) from exc

        scratch = Path(tempfile.mkdtemp(prefix="frisket-clip-"))
        out_path = scratch / f"clip{output_suffix(source)}"
        try:
            with project.materialize_blob(source.blob_hash) as source_path:
                await cut_clip(
                    source,
                    source_path=Path(source_path),
                    start_ms=start_ms,
                    end_ms=end_ms,
                    out_path=out_path,
                )
        except BlobNotFoundError as exc:
            shutil.rmtree(scratch, ignore_errors=True)
            raise ProjectEvidenceRouteError(
                404,
                _v1_action_error(
                    code="blob_missing",
                    message="Source blob is missing from project storage.",
                    field="span_stable_id",
                    details={"span_stable_id": span_stable_id},
                ),
                bare_json=True,
            ) from exc
        except ClipError as exc:
            shutil.rmtree(scratch, ignore_errors=True)
            raise ProjectEvidenceRouteError(
                clip_error_status(exc),
                _v1_action_error(
                    code=exc.code,
                    message=exc.message,
                    field="span_stable_id",
                    details={"span_stable_id": span_stable_id},
                ),
                bare_json=True,
            ) from exc

        return FileResponse(
            out_path,
            media_type=source.mime,
            filename=clip_filename(source, start_ms=start_ms, end_ms=end_ms),
            background=BackgroundTask(shutil.rmtree, scratch, ignore_errors=True),
        )

    async def evidence_link_run_clip(
        self,
        project_id: str,
        evidence_link_id: str,
        run_index: int,
        *,
        pad_ms: int,
    ) -> FileResponse:
        """citation-span-runs-v1: ffmpeg-cut ONE clip spanning a citation's
        whole contiguous run (start of the run's first cited span -> end of
        its last -- store.evidence.citation_temporal_runs, the same grouping
        the viewer payload used to render the run). Link-level (not
        span-level) because "download this run" is what the citation/link
        download affordance offers; per-segment clips stay on the existing
        span route. Mirrors evidence_span_clip's guard/cut/stream shape."""
        project = self._project_or_404(project_id)
        try:
            source = resolve_link_run_clip_source(project, evidence_link_id, run_index)
            start_ms, end_ms = padded_range(source, pad_ms=pad_ms)
        except ClipError as exc:
            raise ProjectEvidenceRouteError(
                clip_error_status(exc),
                _v1_action_error(
                    code=exc.code,
                    message=exc.message,
                    field="evidence_link_id",
                    details={
                        "evidence_link_id": evidence_link_id,
                        "run_index": run_index,
                    },
                ),
                bare_json=True,
            ) from exc

        scratch = Path(tempfile.mkdtemp(prefix="frisket-clip-"))
        out_path = scratch / f"clip{output_suffix(source)}"
        try:
            with project.materialize_blob(source.blob_hash) as source_path:
                await cut_clip(
                    source,
                    source_path=Path(source_path),
                    start_ms=start_ms,
                    end_ms=end_ms,
                    out_path=out_path,
                )
        except BlobNotFoundError as exc:
            shutil.rmtree(scratch, ignore_errors=True)
            raise ProjectEvidenceRouteError(
                404,
                _v1_action_error(
                    code="blob_missing",
                    message="Source blob is missing from project storage.",
                    field="evidence_link_id",
                    details={
                        "evidence_link_id": evidence_link_id,
                        "run_index": run_index,
                    },
                ),
                bare_json=True,
            ) from exc
        except ClipError as exc:
            shutil.rmtree(scratch, ignore_errors=True)
            raise ProjectEvidenceRouteError(
                clip_error_status(exc),
                _v1_action_error(
                    code=exc.code,
                    message=exc.message,
                    field="evidence_link_id",
                    details={
                        "evidence_link_id": evidence_link_id,
                        "run_index": run_index,
                    },
                ),
                bare_json=True,
            ) from exc

        return FileResponse(
            out_path,
            media_type=source.mime,
            filename=clip_filename(source, start_ms=start_ms, end_ms=end_ms),
            background=BackgroundTask(shutil.rmtree, scratch, ignore_errors=True),
        )

    def _project_or_404(self, project_id: str) -> Project:
        try:
            return self._workspace.get(project_id)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            raise ProjectEvidenceRouteError(
                404,
                _v1_action_error(
                    code="project_not_found",
                    message="No project exists with that id.",
                    field="project_id",
                    details={"project_id": project_id},
                ),
            ) from exc


def _sheet_id_for_project_cell(
    project: Project,
    *,
    row_id: int,
    column_id: int,
) -> int | None:
    row = project.db.execute(
        "SELECT r.sheet_id "
        "FROM rows r "
        "JOIN columns c ON c.sheet_id = r.sheet_id "
        "WHERE r.id=? AND c.id=?",
        (row_id, column_id),
    ).fetchone()
    if row is None:
        return None
    return int(row["sheet_id"])


def _column_belongs_to_sheet(
    project: Project, *, sheet_id: int, column_id: int
) -> bool:
    row = project.db.execute(
        "SELECT 1 FROM columns WHERE id=? AND sheet_id=?",
        (column_id, sheet_id),
    ).fetchone()
    return row is not None


def _parse_row_ids_param(raw: str | None) -> list[int] | None:
    """Comma-separated `row_ids` query param, mirroring sheet_grid.py's
    `_parse_row_ids_param` convention (sheet_grid.py:207-229): `None` means
    "no filter" (every row); an explicit empty/all-blank value parses to `[]`
    (list_column_evidence's own empty-list short-circuit, not "no filter")."""
    if raw is None:
        return None
    out: list[int] = []
    seen: set[int] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            rid = int(token)
        except ValueError as exc:
            raise ProjectEvidenceRouteError(
                400,
                _v1_action_error(
                    code="invalid_row_ids",
                    message=f"invalid row_ids value: {token!r}",
                    field="row_ids",
                    details={"token": token},
                ),
                bare_json=True,
            ) from exc
        if rid <= 0 or rid in seen:
            continue
        seen.add(rid)
        out.append(rid)
        if len(out) > MAX_EXPLICIT_ROW_IDS:
            raise ProjectEvidenceRouteError(
                400,
                _v1_action_error(
                    code="too_many_row_ids",
                    message=f"too many row_ids: at most {MAX_EXPLICIT_ROW_IDS} allowed per request",
                    field="row_ids",
                    details={"limit": MAX_EXPLICIT_ROW_IDS},
                ),
                bare_json=True,
            )
    return out


def _v1_action_error(
    *,
    code: str,
    message: str,
    field: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return V1ActionError(
        code=code,
        message=message,
        field=field,
        details=dict(details or {}),
    ).model_dump(mode="json")
