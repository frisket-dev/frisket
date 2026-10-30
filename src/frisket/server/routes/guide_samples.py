"""Catalog, download and authenticated sample launch endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from frisket.server.services.guide_samples import (
    GuideSampleService,
    download,
    sample_metadata,
)


class GuideSampleFile(BaseModel):
    filename: str
    media_type: str
    size: int
    source_url: str | None = None
    license: str | None = None
    author: str | None = None


class GuideSampleMetadata(BaseModel):
    id: str
    revision: int
    title: str
    description: str
    file_count: int
    total_bytes: int
    files: list[GuideSampleFile]


class GuideSampleCatalog(BaseModel):
    samples: list[GuideSampleMetadata]


class PdfDestination(BaseModel):
    kind: Literal["pdf_import_draft"]
    draft_id: str
    stage: Literal["check_text"]
    state: Literal["ready", "pending"]
    guide_id: str
    step_id: str


class SheetDestination(BaseModel):
    kind: Literal["sheet"]
    sheet_id: int
    guide_id: str
    step_id: str


class GuideSampleLaunchResponse(BaseModel):
    project_id: str
    destination: PdfDestination | SheetDestination


class GuideSampleLaunchRequest(BaseModel):
    revision: int
    operation_id: str = Field(min_length=1, max_length=128)


def register_guide_sample_routes(app: FastAPI, *, service: GuideSampleService) -> None:
    @app.get(
        "/api/guide-samples",
        response_model=GuideSampleCatalog,
        response_model_exclude_none=True,
    )
    def list_guide_samples() -> dict:
        return {
            "samples": [
                sample_metadata("court-filings"),
                sample_metadata("council-recordings"),
            ]
        }

    @app.get(
        "/api/guide-samples/{sample_id}",
        response_model=GuideSampleMetadata,
        response_model_exclude_none=True,
    )
    def get_guide_sample(sample_id: str, revision: int = 1) -> dict:
        return sample_metadata(sample_id, revision)

    @app.get("/api/guide-samples/{sample_id}/download")
    def download_guide_sample(sample_id: str, revision: int = 1) -> Response:
        return Response(
            download(sample_id, revision),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{sample_id}-v{revision}.zip"'
            },
        )

    @app.post(
        "/api/projects/{pid}/guide-samples/{sample_id}/prepare",
        response_model=GuideSampleLaunchResponse,
    )
    def prepare_guide_sample(
        pid: str,
        sample_id: str,
        body: GuideSampleLaunchRequest,
        request: Request,
    ) -> dict:
        user = getattr(request.state, "user", None)
        actor_id = user.get("id") if isinstance(user, dict) else None
        actor = f"user:{int(actor_id)}" if actor_id is not None else "local"
        return service.prepare(pid, sample_id, body.revision, body.operation_id, actor)
