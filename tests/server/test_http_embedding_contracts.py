"""HTTP/OpenAPI coverage for the five typed embedding operations.

Exact-equality assertions against full producer-shaped payloads are the
no-pruning proof for the typed response declarations; the download and
manifest routes stay raw and must not grow response schemas.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from frisket.contracts.http import embeddings as contracts
from frisket.server import embedding_catalog
from frisket.server.routes.embeddings import register_embedding_routes
from frisket.server.route_errors import register_route_error_handler
from frisket.server.services import embeddings as embedding_services
from frisket.server.services.embeddings import EmbeddingRouteError


CATALOG_PAYLOAD = {
    "schema_version": "frisket.embedding_provider_catalog.v1",
    "modality": "text",
    "source_column_type": "markdown",
    "providers": [
        {
            "provider_id": "fastembed",
            "provider_kind": "local_process",
            "model_id": "BAAI/bge-small-en-v1.5",
            "label": "Local · bge-small",
            "modalities": ["text", "row"],
            "dimensions": [384],
            "distance_metrics": ["cosine"],
            "local": True,
            "available": False,
            "error": "local FastEmbed runtime unavailable; reinstall Frisket",
            "pricing": None,
            "privacy": {"local": True, "note": {"egress": "none"}},
            "size_gb": 0.13,
            "max_input_tokens": 512,
            "recommended": True,
            "dimension_discovery_required": False,
            "disabled_reason": "local FastEmbed runtime unavailable; reinstall Frisket",
            "modality_compatible": True,
        }
    ],
}

INDEXES_PAYLOAD = {
    "schema_version": "frisket.embedding_index_list.v1",
    "sheet_id": 3,
    "indexes": [
        {
            "freshness": {
                "reason": "stale_source",
                "refresh_needed": True,
                "ready": 2,
                "current": 1,
                "missing": 1,
                "stale": 1,
                "error": 0,
                "total": 3,
                "scope_resolved": True,
                "maintenance_mode": "manual",
                "last_refreshed_at": "2026-08-10 12:00:00",
                "last_refresh_job_id": "41",
                "last_refresh_receipt_id": "receipt_9",
                "pending_refresh_job_id": 77,
            },
            "index_id": "idx_1",
            "name": "notes",
            "sheet_id": 3,
            "space_id": "space_1",
            "modality": "text",
            "provider_id": "fastembed",
            "provider_kind": "local_process",
            "model_id": "BAAI/bge-small-en-v1.5",
            "dimension": 384,
            "distance_metric": "cosine",
            "source_columns": ["notes"],
            "status": "idle",
            "total_items": 3,
            "ready_items": 2,
            "stale_items": 0,
            "stale_source_items": 1,
            "missing_source_items": 1,
            "error_items": 0,
            "refresh_needed": True,
            "last_refreshed_at": "2026-08-10 12:00:00",
            "remote": False,
            "provider_policy": {
                "allow_remote": False,
                "allow_remote_automatic_refresh": False,
                "max_cost_usd_per_refresh": None,
            },
            "maintenance": {"mode": "manual", "schedule": None},
        }
    ],
}

EXPORT_PAYLOAD = {
    "schema_version": "frisket.embedding_index_export_result.v1",
    "index_id": "idx_1",
    "receipt_id": "receipt_12",
    "artifacts": [
        {
            "kind": "export_artifact",
            "export_kind": "embedding_index_export",
            "format": "jsonl",
            "path": "exports/idx_1.embeddings.jsonl",
            "byte_count": 42,
            "sha256": "sha256:" + "ab" * 32,
            "row_count": 3,
        }
    ],
}

SIMILARITY_PAYLOAD = {
    "schema_version": "frisket.embedding_similarity_preview.v1",
    "index_id": "idx_1",
    "space_id": "space_1",
    "sheet_id": 3,
    "distance_metric": "cosine",
    "anchor": {"kind": "manual_text_query", "text": "flood buyouts"},
    "limit": 2,
    "hits": [
        {
            "row_id": 9,
            "sheet_id": 3,
            "distance": 0.12,
            "score": 0.88,
            "values": {"notes": "levee failure", "year": 2019, "verified": None},
        },
        {
            "row_id": 4,
            "sheet_id": 3,
            "distance": 0.5,
            "score": None,
            "values": {},
        },
    ],
}

HYBRID_PAYLOAD = {
    "schema_version": "frisket.embedding_hybrid_preview.v1",
    "index_id": "idx_1",
    "sheet_id": 3,
    "distance_metric": "rrf",
    "hits": [
        {
            "row_id": 9,
            "sheet_id": 3,
            "distance": None,
            "score": 0.03252,
            "vector_rank": 1,
            "keyword_rank": None,
            "values": {"notes": "levee failure"},
        }
    ],
}


class _EmbeddingService:
    def __init__(self) -> None:
        self.similarity_query: dict[str, Any] | None = None
        self.export_args: tuple[Any, ...] | None = None
        self.export_error: EmbeddingRouteError | None = None
        self.extra_response_field = False
        self.indexes_payload: dict[str, Any] = INDEXES_PAYLOAD

    def similarity_preview(self, project_id: str, query: dict[str, Any]) -> dict:
        self.similarity_query = query
        if self.extra_response_field:
            return {**SIMILARITY_PAYLOAD, "surprise": True}
        return SIMILARITY_PAYLOAD

    def hybrid_preview(self, project_id: str, query: dict[str, Any]) -> dict:
        return HYBRID_PAYLOAD

    def provider_catalog(
        self,
        project_id: str,
        *,
        modality: str | None = None,
        source_column_type: str | None = None,
    ) -> dict:
        return CATALOG_PAYLOAD

    def indexes(self, project_id: str, sheet_id: int | None = None) -> dict:
        return self.indexes_payload

    def export_index(
        self,
        project_id: str,
        index_id: str,
        *,
        formats: list[str],
        include_vectors: bool,
    ) -> dict:
        if self.export_error is not None:
            raise self.export_error
        self.export_args = (index_id, formats, include_vectors)
        return EXPORT_PAYLOAD


def _wire_bytes(payload: dict[str, Any]) -> bytes:
    """The exact bytes the pre-contract bare-dict routes put on the wire
    (Starlette ``JSONResponse`` rendering). Comparing ``response.content``
    against this — not ``response.json()`` — also pins JSON key order, so a
    model field-declaration-order regression fails here."""

    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        indent=None,
        separators=(",", ":"),
    ).encode("utf-8")


def _client() -> tuple[TestClient, _EmbeddingService]:
    app = FastAPI()
    register_route_error_handler(app)
    service = _EmbeddingService()
    register_embedding_routes(app, service=service)  # type: ignore[arg-type]
    return TestClient(app, raise_server_exceptions=False), service


def test_contract_schema_literals_match_producers() -> None:
    assert (
        contracts.EMBEDDING_SIMILARITY_PREVIEW_SCHEMA_VERSION
        == embedding_services.EMBEDDING_SIMILARITY_PREVIEW_SCHEMA
    )
    assert (
        contracts.EMBEDDING_PROVIDER_CATALOG_SCHEMA_VERSION
        == embedding_catalog.EMBEDDING_PROVIDER_CATALOG_SCHEMA
    )
    assert (
        contracts.EMBEDDING_INDEX_LIST_SCHEMA_VERSION
        == embedding_catalog.EMBEDDING_INDEX_LIST_SCHEMA
    )


def test_similarity_hits_require_a_distance_while_hybrid_hits_remain_nullable() -> None:
    with pytest.raises(ValidationError):
        contracts.EmbeddingSimilarityHit.model_validate(
            {
                "row_id": 9,
                "sheet_id": 3,
                "distance": None,
                "score": None,
                "values": {},
            }
        )
    assert (
        contracts.EmbeddingHybridHit.model_validate(HYBRID_PAYLOAD["hits"][0]).distance
        is None
    )


def test_preview_routes_preserve_transport_shape_and_request_coercion() -> None:
    client, service = _client()

    similarity = client.post(
        "/api/projects/project-1/embeddings/v1/similarity-preview",
        json={
            "query": {"embedding_index_id": "idx_1", "limit": 2},
            "operator_note": "ignored today, ignored still",
        },
    )
    assert similarity.status_code == 200, similarity.text
    assert similarity.content == _wire_bytes(SIMILARITY_PAYLOAD)
    assert service.similarity_query == {"embedding_index_id": "idx_1", "limit": 2}

    hybrid = client.post(
        "/api/projects/project-1/embeddings/v1/hybrid-preview",
        json={"query": {"embedding_index_id": "idx_1"}},
    )
    assert hybrid.status_code == 200, hybrid.text
    assert hybrid.content == _wire_bytes(HYBRID_PAYLOAD)

    missing_query = client.post(
        "/api/projects/project-1/embeddings/v1/similarity-preview", json={}
    )
    assert missing_query.status_code == 422


def test_catalog_indexes_and_export_preserve_transport_shape() -> None:
    client, service = _client()

    catalog = client.get(
        "/api/projects/project-1/embeddings/v1/provider-catalog",
        params={"modality": "text", "source_column_type": "markdown"},
    )
    assert catalog.status_code == 200, catalog.text
    assert catalog.content == _wire_bytes(CATALOG_PAYLOAD)

    indexes = client.get(
        "/api/projects/project-1/embeddings/v1/indexes", params={"sheet_id": 3}
    )
    assert indexes.status_code == 200, indexes.text
    assert indexes.content == _wire_bytes(INDEXES_PAYLOAD)

    exported = client.post(
        "/api/projects/project-1/embeddings/v1/indexes/idx_1/export",
        json={"formats": ["jsonl", "parquet"], "include_vectors": False},
    )
    assert exported.status_code == 200, exported.text
    assert exported.content == _wire_bytes(EXPORT_PAYLOAD)
    assert service.export_args == ("idx_1", ["jsonl", "parquet"], False)

    defaulted = client.post(
        "/api/projects/project-1/embeddings/v1/indexes/idx_1/export", json={}
    )
    assert defaulted.status_code == 200
    assert service.export_args == ("idx_1", ["jsonl"], True)


def test_export_artifact_is_closed_and_has_the_writer_seven_fields() -> None:
    artifact = EXPORT_PAYLOAD["artifacts"][0]
    assert list(artifact) == [
        "kind",
        "export_kind",
        "format",
        "path",
        "byte_count",
        "sha256",
        "row_count",
    ]
    assert (
        contracts.EmbeddingIndexExportArtifact.model_validate(artifact).model_dump()
        == artifact
    )


def test_corrupt_maintenance_policy_leaf_renders_byte_for_byte() -> None:
    """``_safe_obj`` promises a corrupt/legacy row must not 500 the list: a
    well-formed maintenance policy with a wrong-typed leaf flows into BOTH
    ``freshness.maintenance_mode`` and ``maintenance`` and must round-trip."""

    client, service = _client()
    corrupt_index = {
        **INDEXES_PAYLOAD["indexes"][0],
        "freshness": {
            **INDEXES_PAYLOAD["indexes"][0]["freshness"],
            "maintenance_mode": 123,
        },
        "maintenance": {"mode": 123, "cadence": {"unexpected": True}},
    }
    service.indexes_payload = {**INDEXES_PAYLOAD, "indexes": [corrupt_index]}

    response = client.get("/api/projects/project-1/embeddings/v1/indexes")
    assert response.status_code == 200, response.text
    assert response.content == _wire_bytes(service.indexes_payload)


def test_error_envelope_passes_code_and_details_through() -> None:
    client, service = _client()
    service.export_error = EmbeddingRouteError(
        400,
        {
            "code": "embedding_export_failed",
            "message": "the exporter refused",
            "field": "formats",
            "requested_formats": ["csv"],
        },
    )

    refused = client.post(
        "/api/projects/project-1/embeddings/v1/indexes/idx_1/export",
        json={"formats": ["csv"]},
    )
    assert refused.status_code == 400
    assert refused.json() == {
        "detail": {
            "code": "embedding_export_failed",
            "message": "the exporter refused",
            "field": "formats",
            "requested_formats": ["csv"],
        }
    }


def test_unknown_producer_field_errors_loudly_instead_of_pruning() -> None:
    client, service = _client()
    service.extra_response_field = True

    response = client.post(
        "/api/projects/project-1/embeddings/v1/similarity-preview",
        json={"query": {}},
    )
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal Server Error"}


def test_openapi_declares_the_five_operations_and_keeps_raw_routes_raw() -> None:
    client, _service = _client()
    document = client.app.openapi()
    paths = document["paths"]

    similarity = paths["/api/projects/{pid}/embeddings/v1/similarity-preview"]["post"]
    hybrid = paths["/api/projects/{pid}/embeddings/v1/hybrid-preview"]["post"]
    catalog = paths["/api/projects/{pid}/embeddings/v1/provider-catalog"]["get"]
    indexes = paths["/api/projects/{pid}/embeddings/v1/indexes"]["get"]
    export = paths["/api/projects/{pid}/embeddings/v1/indexes/{index_id}/export"][
        "post"
    ]
    for operation, response_ref in (
        (similarity, "EmbeddingSimilarityPreviewResponse"),
        (hybrid, "EmbeddingHybridPreviewResponse"),
        (catalog, "EmbeddingProviderCatalogResponse"),
        (indexes, "EmbeddingIndexListResponse"),
        (export, "EmbeddingIndexExportResponse"),
    ):
        schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{response_ref}"}
    assert set(similarity["responses"]) >= {
        "200",
        "400",
        "401",
        "403",
        "404",
        "422",
        "500",
    }
    assert set(hybrid["responses"]) >= {"200", "400", "401", "403", "404", "422", "500"}
    assert set(export["responses"]) >= {"200", "400", "401", "403", "404", "422", "500"}
    assert set(catalog["responses"]) >= {"200", "401", "403", "404", "422", "500"}
    assert set(indexes["responses"]) >= {"200", "401", "403", "404", "422", "500"}

    download = paths[
        "/api/projects/{pid}/embeddings/v1/indexes/{index_id}/export/{fmt}"
    ]["get"]
    manifest = paths["/api/projects/{pid}/embeddings/v1/indexes/{index_id}/export"][
        "get"
    ]
    for raw_operation in (download, manifest):
        raw_schema = (
            raw_operation["responses"]["200"]
            .get("content", {})
            .get("application/json", {})
            .get("schema", {})
        )
        assert "$ref" not in raw_schema

    artifact = document["components"]["schemas"]["EmbeddingIndexExportArtifact"]
    assert list(artifact["properties"]) == [
        "kind",
        "export_kind",
        "format",
        "path",
        "byte_count",
        "sha256",
        "row_count",
    ]
