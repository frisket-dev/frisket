"""Embedding route registration.

The five JSON operations carry typed contracts; the export download (binary
artifact) and export manifest (integrity surface, no web consumer) stay raw.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import FileResponse

from frisket.contracts.http.embeddings import (
    EmbeddingHybridPreviewResponse,
    EmbeddingIndexExportRequest,
    EmbeddingIndexExportResponse,
    EmbeddingIndexListResponse,
    EmbeddingProviderCatalogResponse,
    EmbeddingSimilarityPreviewRequest,
    EmbeddingSimilarityPreviewResponse,
)
from frisket.server.route_errors import http_error_responses
from frisket.server.services.embeddings import (
    EmbeddingRouteService,
)


def register_embedding_routes(
    app: FastAPI,
    *,
    service: EmbeddingRouteService,
) -> None:
    @app.post(
        "/api/projects/{pid}/embeddings/v1/similarity-preview",
        response_model=EmbeddingSimilarityPreviewResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 401, 403, 404, 422, 500),
    )
    def embedding_similarity_preview(
        pid: str,
        body: EmbeddingSimilarityPreviewRequest,
    ) -> EmbeddingSimilarityPreviewResponse:
        return EmbeddingSimilarityPreviewResponse.model_validate(
            service.similarity_preview(pid, body.query)
        )

    @app.post(
        "/api/projects/{pid}/embeddings/v1/hybrid-preview",
        response_model=EmbeddingHybridPreviewResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 401, 403, 404, 422, 500),
    )
    def embedding_hybrid_preview(
        pid: str,
        body: EmbeddingSimilarityPreviewRequest,
    ) -> EmbeddingHybridPreviewResponse:
        return EmbeddingHybridPreviewResponse.model_validate(
            service.hybrid_preview(pid, body.query)
        )

    @app.get(
        "/api/projects/{pid}/embeddings/v1/provider-catalog",
        response_model=EmbeddingProviderCatalogResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 404, 422, 500),
    )
    def embedding_provider_catalog(
        pid: str,
        modality: str | None = None,
        source_column_type: str | None = None,
    ) -> EmbeddingProviderCatalogResponse:
        return EmbeddingProviderCatalogResponse.model_validate(
            service.provider_catalog(
                pid,
                modality=modality,
                source_column_type=source_column_type,
            )
        )

    @app.get(
        "/api/projects/{pid}/embeddings/v1/indexes",
        response_model=EmbeddingIndexListResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 404, 422, 500),
    )
    def embedding_indexes(
        pid: str, sheet_id: int | None = None
    ) -> EmbeddingIndexListResponse:
        return EmbeddingIndexListResponse.model_validate(service.indexes(pid, sheet_id))

    @app.post(
        "/api/projects/{pid}/embeddings/v1/indexes/{index_id}/export",
        response_model=EmbeddingIndexExportResponse,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 401, 403, 404, 422, 500),
    )
    def embedding_index_export(
        pid: str,
        index_id: str,
        body: EmbeddingIndexExportRequest,
    ) -> EmbeddingIndexExportResponse:
        return EmbeddingIndexExportResponse.model_validate(
            service.export_index(
                pid,
                index_id,
                formats=body.formats,
                include_vectors=body.include_vectors,
            )
        )

    @app.get("/api/projects/{pid}/embeddings/v1/indexes/{index_id}/export/{fmt}")
    def embedding_index_export_download(pid: str, index_id: str, fmt: str):
        download = service.export_download(pid, index_id, fmt)
        return FileResponse(
            download.path,
            media_type=download.media_type,
            filename=download.filename,
        )

    @app.get("/api/projects/{pid}/embeddings/v1/indexes/{index_id}/export")
    def embedding_index_export_manifest(pid: str, index_id: str) -> dict:
        return service.export_manifest(pid, index_id)
