"""Embedding route services for local server routes."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frisket.ai.embeddings import EmbeddingGateway, EmbeddingStore
from frisket.ai.embeddings.export import sha256_file
from frisket.ai.embeddings.freshness import (
    embedding_search_freshness_error,
    freshness_error_code,
    freshness_reason,
    index_freshness,
)
from frisket.ai.embeddings.hybrid import resolve_embedding_hybrid
from frisket.ai.embeddings.similarity import (
    SimilarityError,
    resolve_embedding_similarity,
)
from frisket.contracts.action import Receipt
from frisket.engine.executor import run_action_spec
from frisket.engine.executor.embedding_export import (
    embedding_export_dir,
    index_export_replay_error,
)
from frisket.server.embedding_catalog import (
    embedding_index_list_payload,
    embedding_provider_catalog_payload,
)
from frisket.server.workspace import Workspace
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore, StoredReceipt
from frisket.server.route_errors import RouteError


EMBEDDING_SIMILARITY_PREVIEW_SCHEMA = "frisket.embedding_similarity_preview.v1"
EMBEDDING_EXPORT_MEDIA = {
    "jsonl": "application/x-ndjson",
    "parquet": "application/vnd.apache.parquet",
    "arrow": "application/vnd.apache.arrow.file",
}


class EmbeddingRouteError(RouteError):
    pass


@dataclass(frozen=True)
class EmbeddingExportDownload:
    path: Path
    media_type: str
    filename: str


class EmbeddingRouteService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def similarity_preview(self, project_id: str, query: dict[str, Any]) -> dict:
        project = self._workspace.get(project_id)
        self._check_search_freshness(project, query)
        gateway = EmbeddingGateway(router=self._workspace.router_for(project))
        try:
            result = resolve_embedding_similarity(project, query, gateway=gateway)
        except SimilarityError as exc:
            raise _embedding_request_error(exc) from exc
        return _embedding_similarity_preview_payload(project, result)

    def hybrid_preview(self, project_id: str, query: dict[str, Any]) -> dict:
        project = self._workspace.get(project_id)
        self._check_search_freshness(project, query)
        gateway = EmbeddingGateway(router=self._workspace.router_for(project))
        try:
            result = resolve_embedding_hybrid(project, query, gateway=gateway)
        except SimilarityError as exc:
            raise _embedding_request_error(exc) from exc
        return _embedding_hybrid_preview_payload(project, result)

    def provider_catalog(
        self,
        project_id: str,
        *,
        modality: str | None = None,
        source_column_type: str | None = None,
    ) -> dict:
        project = self._workspace.get(project_id)
        return embedding_provider_catalog_payload(
            router=self._workspace.router_for(project),
            modality=modality,
            source_column_type=source_column_type,
        )

    def indexes(self, project_id: str, sheet_id: int | None = None) -> dict:
        project = self._workspace.get(project_id)
        return embedding_index_list_payload(
            project,
            sheet_id,
            queue=self._workspace.queue,
            project_id=project_id,
            storage_org_id=self._workspace.queue_storage_org_id,
        )

    def export_index(
        self,
        project_id: str,
        index_id: str,
        *,
        formats: list[str],
        include_vectors: bool,
    ) -> dict:
        project = self._workspace.get(project_id)
        export_dir = embedding_export_dir(project)
        export_dir.mkdir(parents=True, exist_ok=True)
        result = run_action_spec(
            project,
            {
                "action_id": "embedding.index_export",
                "scope": {"kind": "project"},
                "params": {
                    "index_id": index_id,
                    "destination": {"kind": "local_dir", "path": str(export_dir)},
                    "formats": formats,
                    "include_vectors": include_vectors,
                },
                "idempotency_key": f"http_embedding_index_export:{uuid.uuid4().hex}",
            },
            project_id=project_id,
        )
        if result.status != "completed":
            err = result.errors[0] if result.errors else None
            detail = {
                "code": err.code if err else "embedding_export_failed",
                "message": err.message if err else "export failed",
                "field": getattr(err, "field", None) if err else None,
                **(getattr(err, "details", None) or {}),
            }
            raise EmbeddingRouteError(400, detail)
        return {
            "schema_version": "frisket.embedding_index_export_result.v1",
            "index_id": index_id,
            "receipt_id": result.receipt_id,
            "artifacts": [output.ref for output in result.outputs],
        }

    def export_download(
        self,
        project_id: str,
        index_id: str,
        fmt: str,
    ) -> EmbeddingExportDownload:
        media = EMBEDDING_EXPORT_MEDIA.get(fmt)
        if media is None:
            raise EmbeddingRouteError(404, "unknown export format")
        project = self._workspace.get(project_id)
        index = EmbeddingStore(project).get_index(index_id)
        if index is None:
            raise EmbeddingRouteError(404, "no such embedding index")
        path = embedding_export_dir(project) / f"{index_id}.embeddings.{fmt}"
        if not path.is_file():
            raise EmbeddingRouteError(404, "no export artifact; run an export first")
        reason = freshness_reason(index, index_freshness(project, index))
        code = freshness_error_code(reason)
        if code is not None:
            raise EmbeddingRouteError(
                409,
                {
                    "code": code,
                    "message": (
                        f"the latest export is stale ({reason}); re-export after "
                        "refreshing the index"
                    ),
                    "field": "index_id",
                },
            )
        recorded = _managed_embedding_exports(project, index_id).get(fmt)
        if recorded is None:
            raise EmbeddingRouteError(
                409,
                {
                    "code": "embedding_export_artifact_missing",
                    "message": "export artifact has no matching receipt; re-export the index",
                    "field": "index_id",
                },
            )
        error = index_export_replay_error(project, recorded[1], artifact_path=path)
        if error is not None:
            raise EmbeddingRouteError(
                409,
                {"code": error.code, "message": error.message, "field": "index_id"},
            )
        return EmbeddingExportDownload(
            path=path,
            media_type=media,
            filename=f"{index_id}.embeddings.{fmt}",
        )

    def export_manifest(self, project_id: str, index_id: str) -> dict:
        project = self._workspace.get(project_id)
        index = EmbeddingStore(project).get_index(index_id)
        if index is None:
            raise EmbeddingRouteError(404, "no such embedding index")
        reason = freshness_reason(index, index_freshness(project, index))
        error_code = freshness_error_code(reason)
        recorded = _managed_embedding_exports(project, index_id)
        # Insertion order is receipt order, so this remains the latest managed
        # export's provenance even when different formats came from older runs.
        provenance = next(iter(recorded.values()))[3] if recorded else {}
        export_dir = embedding_export_dir(project)
        artifacts = []
        for fmt in EMBEDDING_EXPORT_MEDIA:
            path = export_dir / f"{index_id}.embeddings.{fmt}"
            if not path.is_file():
                continue
            match = recorded.get(fmt)
            row, receipt, rec, _ = match if match else (None, None, {}, None)
            current = sha256_file(str(path))
            artifacts.append(
                {
                    "format": fmt,
                    "download_url": (
                        f"/api/projects/{project_id}/embeddings/v1/indexes/{index_id}"
                        f"/export/{fmt}"
                    ),
                    "byte_count": path.stat().st_size,
                    "row_count": rec.get("row_count"),
                    "sha256_recorded": rec.get("sha256"),
                    "sha256_current": current,
                    "matches_receipt": (
                        rec.get("sha256") == current if rec.get("sha256") else None
                    ),
                    "exported_at": row.created_at if row else None,
                    "receipt_id": receipt.receipt_id if receipt else None,
                }
            )
        return {
            "schema_version": "frisket.embedding_index_export_manifest.v1",
            "index_id": index_id,
            "freshness": {
                "reason": reason,
                "downloadable": error_code is None,
                "error_code": error_code,
            },
            "provenance": provenance or {},
            "artifacts": artifacts,
        }

    def _check_search_freshness(self, project: Project, query: dict[str, Any]) -> None:
        index_id = query.get("embedding_index_id") if isinstance(query, dict) else None
        if isinstance(index_id, str) and index_id.strip():
            gate = embedding_search_freshness_error(project, index_id)
            if gate is not None:
                code, message = gate
                raise EmbeddingRouteError(
                    400,
                    {"code": code, "message": message, "field": "embedding_index_id"},
                )


def _embedding_request_error(exc: SimilarityError) -> EmbeddingRouteError:
    return EmbeddingRouteError(
        400,
        {
            "code": exc.code,
            "message": exc.message,
            "field": exc.field,
            **exc.details,
        },
    )


def _current_row_values(
    project: Project,
    *,
    sheet_id: int | None,
    row_ids: list[int],
) -> dict[int, dict[str, Any]]:
    values_by_row: dict[int, dict[str, Any]] = {rid: {} for rid in row_ids}
    if sheet_id is not None and row_ids:
        for col in project.columns(sheet_id):
            vals = project.get_values(sheet_id, int(col["id"]), row_ids=row_ids)
            for rid in row_ids:
                if rid in vals:
                    values_by_row[rid][col["name"]] = vals[rid]
    return values_by_row


def _embedding_similarity_preview_payload(project: Project, result: Any) -> dict:
    sheet_id = result.sheet_id
    row_ids = [hit.row_id for hit in result.hits]
    values_by_row = _current_row_values(project, sheet_id=sheet_id, row_ids=row_ids)
    return {
        "schema_version": EMBEDDING_SIMILARITY_PREVIEW_SCHEMA,
        "index_id": result.index_id,
        "space_id": result.space_id,
        "sheet_id": sheet_id,
        "distance_metric": result.distance_metric,
        "anchor": result.anchor,
        "limit": result.limit,
        "hits": [
            {
                "row_id": hit.row_id,
                "sheet_id": hit.sheet_id,
                "distance": hit.distance,
                "score": hit.score,
                "values": values_by_row.get(hit.row_id, {}),
            }
            for hit in result.hits
        ],
    }


def _embedding_hybrid_preview_payload(project: Project, result: Any) -> dict:
    sheet_id = result.sheet_id
    row_ids = [hit.row_id for hit in result.hits]
    values_by_row = _current_row_values(project, sheet_id=sheet_id, row_ids=row_ids)
    return {
        "schema_version": "frisket.embedding_hybrid_preview.v1",
        "index_id": result.index_id,
        "sheet_id": sheet_id,
        "distance_metric": "rrf",
        "hits": [
            {
                "row_id": hit.row_id,
                "sheet_id": sheet_id,
                "distance": None,
                "score": hit.score,
                "vector_rank": hit.vector_rank,
                "keyword_rank": hit.keyword_rank,
                "values": values_by_row.get(hit.row_id, {}),
            }
            for hit in result.hits
        ],
    }


def _managed_embedding_exports(
    project: Project, index_id: str
) -> dict[str, tuple[StoredReceipt, Receipt, dict[str, Any], dict[str, Any]]]:
    """Match each managed file to its last producer, including custom actions."""
    matches = {}
    store = ReceiptStore(project)
    export_dir = embedding_export_dir(project)
    offset = 0
    while rows := store.recent_metadata_page(limit=100, offset=offset):
        offset += len(rows)
        for row in rows:
            if row.status != "completed":
                continue
            try:
                receipt = row.parsed()
            except ValueError:
                continue
            provenance = next(
                (
                    io.ref
                    for io in receipt.evidence
                    if io.ref.get("kind") == "embedding_index_export_provenance"
                    and io.ref.get("index_id") == index_id
                ),
                None,
            )
            if provenance is None:
                continue
            for io in receipt.outputs:
                ref = io.ref
                fmt = ref.get("format")
                if (
                    ref.get("kind") == "export_artifact"
                    and fmt in EMBEDDING_EXPORT_MEDIA
                    and fmt not in matches
                    and ref.get("path")
                    == str(export_dir / f"{index_id}.embeddings.{fmt}")
                ):
                    matches[fmt] = (row, receipt, ref, provenance)
            if len(matches) == len(EMBEDDING_EXPORT_MEDIA):
                return matches
    return matches
