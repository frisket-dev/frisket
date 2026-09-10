from __future__ import annotations

from copy import deepcopy
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from frisket.contracts.http.typescript import (
    openapi_contract_artifact,
    render_typescript_module,
)


ROOT = Path(__file__).resolve().parents[1]
TSC = ROOT / "web" / "node_modules" / ".bin" / "tsc"


def _json_response(schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "description": "fixture response",
        "content": {"application/json": {"schema": schema}},
    }


def _document() -> dict[str, Any]:
    shared_ref = {"$ref": "#/components/schemas/Shared"}
    error_ref = {"$ref": "#/components/schemas/ErrorEnvelope"}
    inline = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    return {
        "openapi": "3.1.0",
        "info": {"title": "projection fixture", "version": "1"},
        "paths": {
            "/items/{item_id}": {
                "get": {
                    "operationId": "fixture.items.get",
                    "parameters": [
                        {
                            "in": "path",
                            "name": "item_id",
                            "required": True,
                            "schema": {"type": "integer", "minimum": 1},
                        },
                        {
                            "in": "query",
                            "name": "tag",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {
                        "200": _json_response(shared_ref),
                        "400": _json_response(error_ref),
                    },
                }
            },
            "/created": {
                "post": {
                    "operationId": "fixture.created.post",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": shared_ref}},
                    },
                    "responses": {
                        "201": _json_response(shared_ref),
                        "422": _json_response(error_ref),
                    },
                }
            },
            "/accepted": {
                "post": {
                    "operationId": "fixture.accepted.post",
                    "responses": {"202": _json_response(shared_ref)},
                }
            },
            "/empty": {
                "delete": {
                    "operationId": "fixture.empty.delete",
                    "responses": {"204": {"description": "no content"}},
                }
            },
            "/inline-a": {
                "get": {
                    "operationId": "fixture.inline_a.get",
                    "responses": {"200": _json_response(inline)},
                }
            },
            "/inline-b": {
                "get": {
                    "operationId": "fixture.inline_b.get",
                    "responses": {"200": _json_response(deepcopy(inline))},
                }
            },
        },
        "components": {
            "schemas": {
                "Shared": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "string"},
                        "leaf": {"$ref": "#/components/schemas/Leaf"},
                    },
                    "required": ["value", "leaf"],
                    "additionalProperties": False,
                },
                "Leaf": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
                "ErrorEnvelope": {
                    "type": "object",
                    "properties": {"detail": {"type": "string"}},
                    "required": ["detail"],
                    "additionalProperties": False,
                },
                "Unused": {
                    "type": "object",
                    "properties": {"never": {"type": "string"}},
                    "required": ["never"],
                    "additionalProperties": False,
                },
            }
        },
    }


def _endpoint(artifact: dict[str, Any], operation_id: str) -> dict[str, Any]:
    return next(
        endpoint for endpoint in artifact["endpoints"] if endpoint["id"] == operation_id
    )


def test_openapi_projection_emits_status_specific_metadata_and_operation_types(
    tmp_path: Path,
) -> None:
    artifact = openapi_contract_artifact(_document())

    assert artifact["schemaVersion"] == "frisket.http_contract_artifact.v4"
    assert [endpoint["id"] for endpoint in artifact["endpoints"]] == sorted(
        endpoint["id"] for endpoint in artifact["endpoints"]
    )
    item = _endpoint(artifact, "fixture.items.get")
    assert item["method"] == "GET"
    assert item["path"] == "/items/{item_id}"
    assert item["request"] is None
    assert item["responses"]["200"] == {
        "hasBody": True,
        "typeName": "HttpShared",
    }
    assert item["responses"]["400"]["typeName"] == "HttpErrorEnvelope"

    created = _endpoint(artifact, "fixture.created.post")
    assert created["request"] == {
        "required": True,
        "mediaType": "application/json",
        "typeName": "HttpShared",
    }
    assert set(created["responses"]) == {"201", "422"}
    assert set(_endpoint(artifact, "fixture.accepted.post")["responses"]) == {"202"}
    assert _endpoint(artifact, "fixture.empty.delete")["responses"]["204"] == {
        "hasBody": False,
        "typeName": None,
    }

    rendered = render_typescript_module(artifact)
    runtime_metadata_block = rendered.split("\n\nexport type ", 1)[0]
    assert "export type HttpContractOperationMap =" in rendered
    assert 'readonly "fixture.items.get"' in rendered
    assert 'readonly "200": HttpShared;' in rendered
    assert 'readonly "204": undefined;' in rendered
    assert (
        '"schemaVersion": "frisket.http_contract_artifact.v4"' in runtime_metadata_block
    )
    assert '"request": {' in runtime_metadata_block
    assert '"mediaType": "application/json"' in runtime_metadata_block
    assert '"responses":' not in runtime_metadata_block
    assert '"schemas":' not in runtime_metadata_block
    assert '"pathParams":' not in runtime_metadata_block
    assert '"query":' not in runtime_metadata_block
    assert "validatorName" not in rendered
    assert "validateGeneratedHttpSchema" not in rendered
    assert "HttpContractValidatorMap" not in rendered

    generated = tmp_path / "generated.ts"
    generated.write_text(rendered, encoding="utf-8")
    probe = tmp_path / "probe.ts"
    probe.write_text(
        """
import {
  HTTP_CONTRACT_ARTIFACT,
  type HttpContractOperationMap,
} from './generated';

const path: HttpContractOperationMap['fixture.items.get']['pathParams'] = { item_id: 1 };
const query: HttpContractOperationMap['fixture.items.get']['query'] = {};
const created: HttpContractOperationMap['fixture.created.post']['responses']['201'] = {
  value: 'ok', leaf: { name: 'leaf' },
};
const accepted: HttpContractOperationMap['fixture.accepted.post']['responses']['202'] = created;
const empty: HttpContractOperationMap['fixture.empty.delete']['responses']['204'] = undefined;
void query; void accepted; void empty;

const endpoint = HTTP_CONTRACT_ARTIFACT.endpoints.find((item) => item.id === 'fixture.items.get');
if (!endpoint) throw new Error('missing endpoint');
const createdEndpoint = HTTP_CONTRACT_ARTIFACT.endpoints.find((item) => item.id === 'fixture.created.post');
if (!createdEndpoint?.request) throw new Error('missing request metadata');
if (createdEndpoint.request.required !== true) throw new Error('request must stay required');
if (createdEndpoint.request.mediaType !== 'application/json') throw new Error('wrong media type');
if (Object.keys(endpoint).sort().join(',') !== 'id,method,path,request') {
  throw new Error('runtime endpoint metadata is not slim');
}
void path; void created;
""".strip()
        + "\n",
        encoding="utf-8",
    )
    compiled = subprocess.run(
        [
            str(TSC),
            "--strict",
            "--target",
            "ES2022",
            "--module",
            "commonjs",
            "--moduleResolution",
            "node",
            "--ignoreDeprecations",
            "6.0",
            "--skipLibCheck",
            "--outDir",
            str(tmp_path / "dist"),
            str(generated),
            str(probe),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    runtime = subprocess.run(
        ["node", str(tmp_path / "dist" / "probe.js")],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert runtime.returncode == 0, runtime.stdout + runtime.stderr


@pytest.mark.parametrize(
    "operation_id",
    (
        "tenant.import_csv.post",
        "tenant.import_csv_preview.post",
        "tenant.import_xlsx.post",
        "tenant.import_pdf.post",
        "tenant.import_files.post",
        "tenant.ocr_compare_scratch.post",
        "tenant.ocr_compare_scratch_estimate.post",
        "tenant.transcribe_compare_scratch.post",
        "tenant.transcribe_compare_scratch_estimate.post",
        "tenant.topic_segmentation_compare_scratch.post",
    ),
)
def test_openapi_projection_types_the_fixed_multipart_bodies_as_native_form_data(
    operation_id: str,
) -> None:
    document = _document()
    document["paths"][
        f"/api/projects/{{pid}}/{operation_id.split('.')[1].replace('_', '/')}"
    ] = {
        "post": {
            "operationId": operation_id,
            "parameters": [
                {
                    "in": "path",
                    "name": "pid",
                    "required": True,
                    "schema": {"type": "string"},
                }
            ],
            "requestBody": {
                "required": True,
                "content": {"multipart/form-data": {"schema": {"type": "object"}}},
            },
            "responses": {
                "200": _json_response({"$ref": "#/components/schemas/Shared"})
            },
        }
    }

    artifact = openapi_contract_artifact(document)
    imported = _endpoint(artifact, operation_id)
    assert imported["request"] == {
        "required": True,
        "mediaType": "multipart/form-data",
    }
    rendered = render_typescript_module(artifact)
    assert f'readonly "{operation_id}"' in rendered
    assert "readonly request: FormData;" in rendered


def test_openapi_projection_emits_only_unique_reachable_roots_deterministically() -> (
    None
):
    document = _document()
    artifact = openapi_contract_artifact(document)
    records = artifact["schemas"]
    by_type = {record["typeName"]: record for record in records}

    assert len(records) == 6
    assert "HttpShared" in by_type
    assert "HttpErrorEnvelope" in by_type
    assert "HttpLeaf" not in by_type
    assert "HttpUnused" not in by_type
    assert "Unused" not in json.dumps(records, sort_keys=True)
    assert "Leaf" in by_type["HttpShared"]["schema"]["$defs"]

    shared_types = {
        _endpoint(artifact, operation_id)["responses"][status]["typeName"]
        for operation_id, status in (
            ("fixture.items.get", "200"),
            ("fixture.created.post", "201"),
            ("fixture.accepted.post", "202"),
        )
    }
    assert shared_types == {"HttpShared"}
    assert (
        _endpoint(artifact, "fixture.inline_a.get")["responses"]["200"]["typeName"]
        == _endpoint(artifact, "fixture.inline_b.get")["responses"]["200"]["typeName"]
    )
    assert (
        _endpoint(artifact, "fixture.created.post")["pathParams"]["typeName"]
        == _endpoint(artifact, "fixture.created.post")["query"]["typeName"]
    )
    assert "validatorName" not in json.dumps(artifact, sort_keys=True)

    reordered = deepcopy(document)
    reordered["paths"] = dict(reversed(list(reordered["paths"].items())))
    reordered["components"]["schemas"] = dict(
        reversed(list(reordered["components"]["schemas"].items()))
    )
    assert openapi_contract_artifact(reordered) == artifact
    assert render_typescript_module(openapi_contract_artifact(reordered)) == (
        render_typescript_module(artifact)
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("header", "parameter location"),
        ("external-ref", "non-local OpenAPI schema reference"),
        ("text-media", "JSON media"),
        ("default-status", "numeric response status"),
    ],
)
def test_openapi_projection_rejects_unrepresented_contract_shapes(
    mutation: str,
    message: str,
) -> None:
    document = _document()
    operation = document["paths"]["/items/{item_id}"]["get"]
    if mutation == "header":
        operation["parameters"].append(
            {
                "in": "header",
                "name": "x-token",
                "required": True,
                "schema": {"type": "string"},
            }
        )
    elif mutation == "external-ref":
        operation["responses"]["200"] = _json_response(
            {"$ref": "https://example.invalid/schema"}
        )
    elif mutation == "text-media":
        operation["responses"]["200"] = {
            "description": "text",
            "content": {"text/plain": {"schema": {"type": "string"}}},
        }
    else:
        operation["responses"]["default"] = _json_response(
            {"$ref": "#/components/schemas/ErrorEnvelope"}
        )

    with pytest.raises(ValueError, match=message):
        openapi_contract_artifact(document)
