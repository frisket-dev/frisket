from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

import ast
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError

from frisket.contracts.http.preview_comparisons import (
    TopicSegmentationCompareScratchResponse,
    TopicSegmentationCompareScratchSource,
)
from frisket.engine.store.media_blobs import media_cell
from frisket.server.app import create_app
from frisket.server.routes import previews as preview_routes


ROOT = Path(__file__).resolve().parents[2]
SERVER_APP = ROOT / "src" / "frisket" / "server" / "app.py"

PREVIEW_ROUTE_PATHS = {
    "/api/projects/{pid}/queries/v1/preview",
    "/api/projects/{pid}/ocr/compare-preview",
    "/api/projects/{pid}/clusters/v1/preview",
}
SCRATCH_COMPARE_ROUTE_PATHS = (
    "/api/projects/{pid}/ocr/compare-scratch",
    "/api/projects/{pid}/ocr/compare-scratch/estimate",
    "/api/projects/{pid}/transcribe/compare-scratch",
    "/api/projects/{pid}/transcribe/compare-scratch/estimate",
    "/api/projects/{pid}/topic-segmentation/compare-scratch",
)
SCRATCH_COMPARE_RESPONSE_MODELS = {
    "/api/projects/{pid}/ocr/compare-scratch": "ActionPreviewStartResponse",
    "/api/projects/{pid}/ocr/compare-scratch/estimate": (
        "OcrCompareScratchEstimateResponse"
    ),
    "/api/projects/{pid}/transcribe/compare-scratch": "ActionPreviewStartResponse",
    "/api/projects/{pid}/transcribe/compare-scratch/estimate": (
        "TranscribeCompareScratchEstimateResponse"
    ),
    "/api/projects/{pid}/topic-segmentation/compare-scratch": (
        "TopicSegmentationCompareScratchResponse"
    ),
}
SCRATCH_COMPARE_RESPONSE_SCHEMAS = {
    "400": "ActionError",
    "401": "HttpError",
    "403": "HttpError",
    "404": "HttpError",
    "422": "HTTPValidationError",
    "500": "HttpError",
}
PDF_BYTES = b"%PDF-1.4\n% preview route fixture\n"


def _valid_scratch_compare_payloads() -> tuple[tuple[type, dict], ...]:
    return (
        (
            TopicSegmentationCompareScratchResponse,
            {
                "schema_version": "frisket.topic_segmentation_compare.v1",
                "source": {
                    "scratch": True,
                    "filename": "sample.txt",
                    "mime": "text/plain",
                    "size": 8,
                    "source_kind": "untimed_transcript",
                    "snapshot_hash": "sha256:test",
                    "language": "en",
                },
                "engines": [
                    {
                        "id": "texttiling",
                        "label": "TextTiling",
                        "description": "Local segmentation",
                        "version": "1",
                        "tier": "local",
                        "available": True,
                        "error": None,
                        "recommended": False,
                    }
                ],
                "units": [
                    {
                        "id": "unit-1",
                        "ordinal": 0,
                        "text": "Ada",
                        "speaker": None,
                        "start_ms": None,
                        "end_ms": None,
                    }
                ],
                "results": [
                    {
                        "variant_id": "balanced",
                        "engine": "texttiling",
                        "engine_version": "1",
                        "settings": {"detail": "balanced"},
                        "status": "completed",
                        "runtime_ms": 4,
                        "boundaries": [],
                        "canonical_boundaries": [],
                        "sections": [{"index": 0, "unit_ids": ["unit-1"]}],
                        "unit_membership": [
                            {"unit_id": "unit-1", "section_indexes": [0]}
                        ],
                        "diagnostics": {"producer": True},
                        "warnings": [],
                        "errors": [],
                    }
                ],
                "warnings": [],
                "errors": [],
            },
        ),
    )


def _module_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _route_decorator_paths(tree: ast.Module) -> set[str]:
    paths: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            call = decorator if isinstance(decorator, ast.Call) else None
            if call is None or not call.args:
                continue
            target = call.func
            if (
                isinstance(target, ast.Attribute)
                and target.attr in {"get", "post", "patch", "delete", "api_route"}
                and isinstance(call.args[0], ast.Constant)
                and isinstance(call.args[0].value, str)
            ):
                paths.add(call.args[0].value)
    return paths


def _add_pdf_row(project) -> tuple[int, int]:
    sheet_id = project.add_sheet("Documents")
    cols = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "pdf": project.add_column(sheet_id, "pdf", type="file"),
    }
    digest = project.add_blob(
        PDF_BYTES,
        filename="docket.pdf",
        mime="application/pdf",
        metadata=owned_media_metadata_document(probe={"kind": "pdf", "pages": 4}),
    )
    row_id = project.add_rows(
        sheet_id,
        [
            {
                "title": "Docket",
                "pdf": media_cell(
                    digest,
                    mime="application/pdf",
                    filename="docket.pdf",
                ),
            }
        ],
        cols,
    )[0]
    return sheet_id, row_id


def test_ocr_preview_route_preserves_bare_v1_action_error_shape(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "OCR Error"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id, row_id = _add_pdf_row(project)

    response = client.post(
        f"/api/projects/{pid}/ocr/compare-preview",
        json={
            "sheet_id": sheet_id,
            "row_id": row_id + 999,
            "input_column": "pdf",
            "pages": [1],
            "engines": ["rapidocr", "dots.mocr"],
            "language": "en",
            "dpi": 180,
            "allow_remote": False,
        },
    )

    assert response.status_code == 400
    body = response.json()
    assert "detail" not in body
    assert body["code"] == "invalid_input_ref"
    assert body["field"] == "row_id"
    assert body["details"] == {"row_id": row_id + 999}


def test_scratch_compare_upload_routes_declare_only_file_and_json_payload_parts(
    tmp_path,
) -> None:
    app = create_app(tmp_path / "workspace")
    document = app.openapi()
    runtime_routes = {
        route.path: route for route in app.routes if isinstance(route, APIRoute)
    }
    for path in SCRATCH_COMPARE_ROUTE_PATHS:
        assert runtime_routes[path].response_model_exclude_unset is True
        operation = document["paths"][path]["post"]
        content = operation["requestBody"]["content"]
        assert set(content) == {"multipart/form-data"}
        schema = content["multipart/form-data"]["schema"]
        if "$ref" in schema:
            schema = document["components"]["schemas"][
                schema["$ref"].rsplit("/", 1)[-1]
            ]
        properties = schema.get("properties", {})
        assert set(properties) == {"file", "payload"}
        success = (
            "202"
            if path
            in {
                "/api/projects/{pid}/ocr/compare-scratch",
                "/api/projects/{pid}/transcribe/compare-scratch",
            }
            else "200"
        )
        extra = {"402"} if success == "202" else set()
        assert set(operation["responses"]) == {
            success,
            *SCRATCH_COMPARE_RESPONSE_SCHEMAS,
            *extra,
        }
        assert operation["responses"][success]["content"]["application/json"][
            "schema"
        ] == {"$ref": (f"#/components/schemas/{SCRATCH_COMPARE_RESPONSE_MODELS[path]}")}
        for status, schema_name in SCRATCH_COMPARE_RESPONSE_SCHEMAS.items():
            if success == "202" and status == "400":
                schema_name = "ActionPreviewErrorResponse"
            if "/ocr/compare-scratch" in path or "/transcribe/compare-scratch" in path:
                if status == "422":
                    schema_name = "HttpError"
            assert operation["responses"][status]["content"]["application/json"][
                "schema"
            ] == {"$ref": f"#/components/schemas/{schema_name}"}


def test_scratch_compare_response_models_keep_exact_schema_versions_and_nulls() -> None:
    for response, source, schema_version, nullable, non_nullable in (
        (
            TopicSegmentationCompareScratchResponse,
            TopicSegmentationCompareScratchSource,
            "frisket.topic_segmentation_compare.v1",
            {"mime", "language"},
            {"filename", "size", "source_kind", "snapshot_hash"},
        ),
    ):
        response_schema = response.model_json_schema()
        assert set(response_schema["required"]) == set(response.model_fields)
        schema_version_schema = response_schema["properties"]["schema_version"]
        assert schema_version_schema["const"] == schema_version
        assert schema_version_schema["type"] == "string"

        source_schema = source.model_json_schema()
        assert set(source_schema["required"]) == set(source.model_fields)
        for field in nullable:
            assert {"type": "null"} in source_schema["properties"][field]["anyOf"]
        for field in non_nullable:
            variants = source_schema["properties"][field].get("anyOf", [])
            assert {"type": "null"} not in variants


def test_scratch_compare_response_models_keep_typed_core_and_json_extensions() -> None:
    for model, payload in _valid_scratch_compare_payloads():
        assert model.model_validate(payload).model_dump(exclude_unset=True) == payload


@pytest.mark.parametrize(
    ("payload_index", "record", "required_field"),
    ((0, ("results", 0), "status"),),
)
def test_scratch_compare_response_models_reject_missing_consumer_core(
    payload_index: int,
    record: tuple[str | int, ...],
    required_field: str,
) -> None:
    model, original = _valid_scratch_compare_payloads()[payload_index]
    payload = deepcopy(original)
    target = payload
    for key in record:
        target = target[key]
    del target[required_field]

    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_scratch_compare_upload_checks_size_before_parsing_payload() -> None:
    tree = _module_tree(ROOT / "src" / "frisket" / "server" / "routes" / "previews.py")
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
    }
    route_names = {
        "ocr_compare_scratch",
        "transcribe_compare_scratch",
        "topic_segmentation_compare_scratch",
    }
    assert route_names <= functions.keys()
    for route_name in route_names:
        assert any(
            isinstance(node.func, ast.Name) and node.func.id == "_scratch_input"
            for node in ast.walk(functions[route_name])
            if isinstance(node, ast.Call)
        )

    calls = [
        node
        for node in ast.walk(functions["_scratch_input"])
        if isinstance(node, ast.Call)
    ]
    read = next(
        node.lineno
        for node in calls
        if isinstance(node.func, ast.Attribute) and node.func.attr == "read"
    )
    loads = next(
        node.lineno
        for node in calls
        if isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "json"
        and node.func.attr == "loads"
    )
    assert read < loads


@pytest.mark.parametrize(
    ("route", "context"),
    (
        ("ocr", "OCR compare scratch"),
        ("transcribe", "Transcribe compare scratch"),
        ("topic-segmentation", "Topic Compare scratch"),
    ),
)
@pytest.mark.parametrize(
    ("content", "payload", "limit", "code", "message_suffix", "field"),
    (
        (b"xx", "{", 1, "invalid_input_ref", "upload is too large", "file"),
        (b"x", "{", 1024, "invalid_params", "payload must be valid JSON", "payload"),
        (
            b"x",
            "[]",
            1024,
            "invalid_params",
            "payload must be a JSON object",
            "payload",
        ),
    ),
)
def test_scratch_compare_input_errors_keep_exact_bare_action_error(
    tmp_path,
    monkeypatch,
    route: str,
    context: str,
    content: bytes,
    payload: str,
    limit: int,
    code: str,
    message_suffix: str,
    field: str,
) -> None:
    monkeypatch.setattr(preview_routes, "_SCRATCH_MAX_UPLOAD_BYTES", limit)
    client = TestClient(create_app(tmp_path / "workspace"))

    response = client.post(
        f"/api/projects/missing/{route}/compare-scratch",
        files={"file": ("sample.bin", content, "application/octet-stream")},
        data={"payload": payload},
    )

    assert response.status_code == 400
    assert response.json() == {
        "schema_version": "frisket.action_error.v1",
        "code": code,
        "message": f"{context} {message_suffix}",
        "action_kind": None,
        "field": field,
        "details": {},
        "needs_confirmation": False,
    }
