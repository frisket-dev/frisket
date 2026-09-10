"""Request and response schemas for the local HTTP server."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class WorkbenchMarketplaceInstallAttemptBody(BaseModel):
    pluginId: str
    version: str | None = None
    source: dict[str, Any]
    arbitraryPackageLoadAllowed: bool = False


class ReadRangeBody(BaseModel):
    sheet_id: int
    offset: int = 0
    limit: int = 50
    columns: list[str] | None = None


class QueryPreviewBody(BaseModel):
    query: dict[str, Any]
    # Keep raw values so bools do not pass FastAPI's int coercion before the
    # shared preview helper returns the route/action-aligned 400 contract.
    limit: Any = 50
    offset: Any = 0


class OcrComparePreviewBody(BaseModel):
    sheet_id: Any = None
    row_id: Any = None
    input_column: Any = None
    pages: Any = None
    engines: Any = None
    language: Any = None
    dpi: Any = None


class BlobMetadataBody(BaseModel):
    force: bool = False
    limit: int | None = None


class PublishActionArtifactBody(BaseModel):
    """Publish a saved/raw action template as a portable registry artifact."""

    name: str | None = None
    saved_action_id: int | None = None
    spec: dict[str, Any] | None = None
    description: str = ""
    publisher: dict[str, Any] = Field(default_factory=dict)
    dataset: dict[str, Any] = Field(default_factory=dict)
    checks: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("spec")
    @classmethod
    def _require_saved_action_contract(
        cls, spec: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if spec is None:
            return None
        from frisket.server.services.saved_actions import require_saved_action_spec

        return require_saved_action_spec(spec)


class ImportActionArtifactBody(BaseModel):
    """Import a registry artifact into the saved action library."""

    artifact_id: str | None = None
    artifact: dict[str, Any] | None = None
    name: str | None = None
