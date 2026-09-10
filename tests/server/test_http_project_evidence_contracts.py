"""Public HTTP contracts for the JSON evidence reads.

The operational evidence suites own producer behavior.  These focused tests
pin the FastAPI boundary: known response shape, omission behavior, and the
fact that producer-owned JSON/detail fields are passed through rather than
silently pruned by a strict response model.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from frisket.contracts.http.project_evidence import CellEvidenceResponse
from frisket.server.routes.project_evidence import register_project_evidence_routes


class _EvidenceService:
    def cell_evidence(
        self, _pid: str, *, row_id: int, column_id: int, include_stale: bool
    ) -> dict:
        return {
            "schema_version": "frisket.cell_evidence.v1",
            "sheet_id": 7,
            "row_id": row_id,
            "column_id": column_id,
            "current_value_ref": {"kind": "generated", "producer_detail": [1]},
            "links": [
                {
                    "id": 3,
                    "stable_id": "evidence_link:3",
                    "export_ref": "evidence_link:3",
                    "status": "stale" if include_stale else "active",
                    "role": "primary_support",
                    "evidence_kind": "region",
                    "span_count": 1,
                    "artifact_count": 1,
                    "snippet": "contract value",
                    "viewer_href": "/viewer",
                }
            ],
            "stale_count": 2,
        }

    def cell_text_annotations(self, _pid: str, *, row_id: int, column_id: int) -> dict:
        return {
            "sheet_id": 7,
            "row_id": row_id,
            "column_id": column_id,
            "content_hash": "hash",
            "offset_unit": "utf16_code_unit",
            "text": "Ada",
            "layers": [
                {
                    "toggle_key": "7:4:entities",
                    "layer_family": "entities",
                    "producer": {"kind": "map.ner", "engine": "gliner"},
                    "output_column": {"id": 4, "name": "entities"},
                    "positioned": True,
                    "counts": {"shown": 1, "total": 1, "invalid": 0},
                    "spans": [
                        {
                            "occurrence_id": "1:1",
                            "start": 0,
                            "end": 3,
                            "quote": "Ada",
                            "metadata": {
                                "entity_type": "person",
                                "entity_fingerprint": None,
                            },
                        }
                    ],
                }
            ],
        }

    def evidence_viewer(self, _pid: str, _link_id: str) -> dict:
        return {
            "schema_version": "frisket.evidence_viewer.v1",
            "link": {
                "id": 3,
                "stable_id": "evidence_link:3",
                "export_ref": "evidence_link:3",
                "subject_kind": "cell_value",
                "subject_ref": {"kind": "generated"},
                "sheet_id": 7,
                "row_id": 8,
                "column_id": 9,
                "run_id": 10,
                "op_id": 11,
                "receipt_id": "receipt-1",
                "role": "primary_support",
                "status": "active",
                "confidence": 0.9,
                "pinned": False,
                "producer": {"action_kind": "map.extract", "model": "test/model"},
                "stale_reason": None,
                "stale_at": None,
                "created_at": "2026-08-11T00:00:00Z",
                "text_layer_hash_mismatch": False,
            },
            "artifacts": [
                {
                    "id": 2,
                    "stable_id": "artifact:2",
                    "export_ref": "artifact:2",
                    "artifact_kind": "file",
                    "media_type": "application/pdf",
                    "title": "Contract",
                    "filename": "contract.pdf",
                    "page_count": 1,
                    "duration_ms": None,
                    "source_url": None,
                    "canonical_url": None,
                    "source_cell": None,
                    "external_ref": {"provider": "test"},
                    "artifact_ref": {
                        "kind": "source_artifact",
                        "stable_id": "artifact:2",
                        "artifact_kind": "file",
                        "media_type": "application/pdf",
                        "blob": None,
                        "source_url": None,
                        "external_ref": {"provider": "test"},
                    },
                    "metadata": {"page_images": {"1": {"raw": True}}},
                    "spans": [
                        {
                            "id": 6,
                            "stable_id": "span:6",
                            "export_ref": "span:6",
                            "span_kind": "region",
                            "rank": 0,
                            "span_role": "support",
                            "required": True,
                            "note": None,
                            "status": "active",
                            "selector": {"kind": "region", "bbox": [[0, 0, 1, 1]]},
                            "quote": "value",
                            "snippet": "value",
                            "text_layer_hash": None,
                            "preview": {"provider": "viewer"},
                            "raw": {"pixel_box": [1, 2, 3, 4]},
                            "warnings": [],
                            "deep_link_url": None,
                            "clip_url": None,
                            "run_index": None,
                        }
                    ],
                    "pages": [
                        {
                            "page": 1,
                            "image": None,
                            "text": "contract",
                            "regions": [
                                {
                                    "id": 6,
                                    "stable_id": "span:6",
                                    "bbox": [[0, 0, 1, 1]],
                                    "snippet": "value",
                                    "raw": {"pixel_box": [1, 2, 3, 4]},
                                }
                            ],
                        }
                    ],
                    "runs": [],
                }
            ],
            "warnings": [],
        }

    def column_evidence(
        self, _pid: str, *, sheet_id: int, column_id: int, row_ids: str | None
    ) -> dict:
        return {
            "schema_version": "frisket.column_evidence_batch.v1",
            "sheet_id": sheet_id,
            "column_id": column_id,
            "rows": [],
        }


def _app() -> FastAPI:
    app = FastAPI()
    register_project_evidence_routes(app, service=_EvidenceService())  # type: ignore[arg-type]
    return app


def _client() -> TestClient:
    return TestClient(_app())


def test_project_evidence_contracts_preserve_explicit_dynamic_json_leaves() -> None:
    client = _client()

    cell = client.get("/api/projects/project-1/cells/8/9/evidence")
    annotations = client.get("/api/projects/project-1/cells/8/9/annotations")
    viewer = client.get("/api/projects/project-1/evidence/links/evidence_link:3/viewer")
    column = client.get("/api/projects/project-1/sheets/7/columns/9/evidence")

    assert cell.status_code == annotations.status_code == viewer.status_code == 200
    assert column.status_code == 200
    assert cell.json()["current_value_ref"]["producer_detail"] == [1]
    assert annotations.json()["layers"][0]["spans"][0]["metadata"] == {
        "entity_type": "person",
        "entity_fingerprint": None,
    }
    artifact = viewer.json()["artifacts"][0]
    assert artifact["spans"][0]["raw"] == {"pixel_box": [1, 2, 3, 4]}
    assert artifact["pages"][0]["regions"][0]["bbox"] == [[0, 0, 1, 1]]
    assert column.json()["rows"] == []


def test_project_evidence_contracts_reject_unknown_envelope_fields() -> None:
    payload = _EvidenceService().cell_evidence(
        "project-1", row_id=8, column_id=9, include_stale=False
    )
    payload["unexpected"] = True

    try:
        CellEvidenceResponse.model_validate(payload)
    except ValidationError as exc:
        assert exc.errors()[0]["loc"] == ("unexpected",)
        assert exc.errors()[0]["type"] == "extra_forbidden"
    else:
        raise AssertionError("closed HTTP envelope accepted an unexpected field")


def test_project_evidence_openapi_names_the_four_json_read_models() -> None:
    # Ask the actual FastAPI instance for OpenAPI. Reaching through
    # TestClient's compatibility wrapper can expose an empty schema under the
    # complete pytest plugin set despite the routes having response models.
    document = _app().openapi()

    paths = document["paths"]
    assert paths["/api/projects/{pid}/cells/{row_id}/{column_id}/evidence"]["get"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CellEvidenceResponse"
    }
    assert paths["/api/projects/{pid}/cells/{row_id}/{column_id}/annotations"]["get"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CellTextAnnotationsResponse"
    }
    assert paths["/api/projects/{pid}/evidence/links/{evidence_link_id}/viewer"]["get"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/EvidenceViewerResponse"
    }
    assert paths["/api/projects/{pid}/sheets/{sheet_id}/columns/{column_id}/evidence"][
        "get"
    ]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ColumnEvidenceResponse"
    }
    components = document["components"]["schemas"]
    for name in (
        "CellEvidenceResponse",
        "EvidenceProducer",
        "EvidenceSourceCell",
        "EvidenceViewerArtifact",
        "TextAnnotationCounts",
        "TextAnnotationSpanMetadata",
        "TextAnnotationUnpositioned",
    ):
        assert components[name]["additionalProperties"] is False
