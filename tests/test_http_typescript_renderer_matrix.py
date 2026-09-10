from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from frisket.contracts.http.project_mcp import McpServerCreateRequest
from frisket.contracts.http.typescript import (
    render_typescript_declaration,
    render_typescript_module,
)


ROOT = Path(__file__).resolve().parents[1]
TSC = ROOT / "web" / "node_modules" / ".bin" / "tsc"


def _record(type_name: str, schema: object) -> dict[str, object]:
    return {"typeName": type_name, "schema": schema}


def _artifact(records: Sequence[Mapping[str, object]]) -> dict[str, object]:
    return {
        "schemaVersion": "renderer.matrix.v1",
        "endpoints": [],
        "schemas": list(records),
    }


def _compile(
    tmp_path: Path,
    records: Sequence[Mapping[str, object]],
    probe: str,
) -> None:
    rendered = render_typescript_module(_artifact(records))
    assert '"schemas":' not in rendered
    assert "validatorName" not in rendered
    assert "validateGeneratedHttpSchema" not in rendered
    assert "HttpContractValidatorMap" not in rendered

    generated = tmp_path / "generated.ts"
    generated.write_text(rendered, encoding="utf-8")
    probe_path = tmp_path / "probe.ts"
    probe_path.write_text(probe, encoding="utf-8")
    compiled = subprocess.run(
        [
            str(TSC),
            "--strict",
            "--noEmit",
            "--target",
            "ES2022",
            "--module",
            "commonjs",
            "--moduleResolution",
            "node",
            "--ignoreDeprecations",
            "6.0",
            "--skipLibCheck",
            str(generated),
            str(probe_path),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr


def test_refs_combinators_recursion_and_pointer_names_compile_as_types(
    tmp_path: Path,
) -> None:
    schema = {
        "$defs": {
            "node/part": {
                "type": "object",
                "properties": {
                    "value": {"type": "string"},
                    "next": {
                        "anyOf": [
                            {"$ref": "#/$defs/node~1part"},
                            {"type": "null"},
                        ]
                    },
                },
                "required": ["value"],
                "additionalProperties": True,
            },
            "node_part": {"type": "integer"},
        },
        "$ref": "#/$defs/node~1part",
        "type": "object",
        "properties": {
            "tag": {"const": "ok"},
            "choice": {
                "anyOf": [{"type": "string"}, {"type": "number"}],
                "enum": ["x", 1],
            },
            "exact": {
                "oneOf": [{"type": "integer"}, {"type": "string"}],
                "enum": [1, "x"],
            },
            "leaf": {"$ref": "#/$defs/node~1part/properties/value"},
            "other": {"$ref": "#/$defs/node_part"},
        },
        "required": ["tag", "choice", "exact", "leaf", "other"],
        "additionalProperties": True,
    }
    probe = """
import type { RecursiveMatrix } from './generated';
const valid: RecursiveMatrix = {
  value: 'root', tag: 'ok', choice: 'x', exact: 1, leaf: 'leaf', other: 2,
  next: { value: 'child', next: null },
};
// @ts-expect-error tag is required
const missingTag: RecursiveMatrix = {
  value: 'root', choice: 'x', exact: 1, leaf: 'leaf', other: 2,
};
const badChoice: RecursiveMatrix = {
  value: 'root', tag: 'ok',
  // @ts-expect-error choice is the literal union 'x' | 1
  choice: 'bad', exact: 1, leaf: 'leaf', other: 2,
};
void valid; void missingTag; void badChoice;
"""
    _compile(tmp_path, [_record("RecursiveMatrix", schema)], probe)


def test_prefix_items_render_closed_optional_and_trailing_tuple_types(
    tmp_path: Path,
) -> None:
    records = [
        _record(
            "ClosedTuple",
            {
                "type": "array",
                "prefixItems": [{"type": "string"}, {"type": "integer"}],
                "items": False,
                "minItems": 1,
                "maxItems": 2,
            },
        ),
        _record(
            "OptionalTuple",
            {
                "type": "array",
                "prefixItems": [{"type": "string"}, {"type": "integer"}],
                "items": False,
            },
        ),
        _record(
            "TrailingTuple",
            {
                "type": "array",
                "prefixItems": [{"type": "string"}],
                "items": {"type": "integer"},
                "minItems": 1,
            },
        ),
    ]
    probe = """
import type { ClosedTuple, OptionalTuple, TrailingTuple } from './generated';
const closedOne: ClosedTuple = ['x'];
const closedTwo: ClosedTuple = ['x', 1];
const optionalEmpty: OptionalTuple = [];
const trailing: TrailingTuple = ['x', 1, 2];
// @ts-expect-error ClosedTuple requires its first item
const closedEmpty: ClosedTuple = [];
// @ts-expect-error ClosedTuple has at most two items
const closedLong: ClosedTuple = ['x', 1, 2];
// @ts-expect-error trailing items are integers
const trailingBad: TrailingTuple = ['x', 'bad'];
void closedOne; void closedTwo; void optionalEmpty; void trailing;
void closedEmpty; void closedLong; void trailingBad;
"""
    _compile(tmp_path, records, probe)


def test_additional_properties_and_recursive_json_render_compile_time_types(
    tmp_path: Path,
) -> None:
    records = [
        _record(
            "TypedExtras",
            {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": {"type": "number"},
            },
        ),
        _record(
            "OpenWithField",
            {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": True,
            },
        ),
        _record("OpenJson", True),
    ]
    probe = """
import type { TypedExtras, OpenWithField, OpenJson } from './generated';
const typed: TypedExtras = { name: 'Ada', score: 3 };
const open: OpenWithField = { name: 'Ada', nested: { ok: [1, null, true] } };
const json: OpenJson = { rows: [{ value: 1 }] };
// @ts-expect-error recursive JSON excludes functions
const invalidJson: OpenJson = () => 1;
void typed; void open; void json; void invalidJson;
"""
    _compile(tmp_path, records, probe)


def test_runtime_only_constraints_do_not_emit_runtime_machinery(tmp_path: Path) -> None:
    records = [
        _record("DecimalStep", {"type": "number", "multipleOf": 0.1}),
        _record(
            "AnnotatedString",
            {
                "$id": "urn:matrix:annotated",
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "string",
                "title": "Annotated",
                "description": "annotation-only keyword coverage",
                "default": "x",
                "deprecated": False,
                "examples": ["x"],
                "readOnly": False,
                "writeOnly": False,
            },
        ),
        _record(
            "PatternedString",
            {"type": "string", "pattern": "^[A-Z][a-z]+$"},
        ),
        _record(
            "ConstrainedKeys",
            {
                "type": "object",
                "propertyNames": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                },
                "additionalProperties": {"type": "string"},
            },
        ),
    ]
    probe = """
import type {
  AnnotatedString,
  ConstrainedKeys,
  DecimalStep,
  PatternedString,
} from './generated';
const decimal: DecimalStep = 0.31;
const annotated: AnnotatedString = 'ok';
const patterned: PatternedString = 'server-validates-this-constraint';
const constrained: ConstrainedKeys = { KEY: 'value' };
void decimal; void annotated; void patterned; void constrained;
"""
    _compile(tmp_path, records, probe)


def test_actual_mcp_property_name_constraints_are_admitted(tmp_path: Path) -> None:
    schema = McpServerCreateRequest.model_json_schema()
    assert schema["properties"]["env"]["propertyNames"] == {
        "maxLength": 256,
        "minLength": 1,
    }
    probe = """
import type { ActualMcpCreateRequest } from './generated';
const request: ActualMcpCreateRequest = {
  name: 'local', command: 'uvx', env: { API_KEY: { value: 'literal' } },
};
void request;
"""
    _compile(tmp_path, [_record("ActualMcpCreateRequest", schema)], probe)


def test_actual_list_source_and_copilot_scope_discriminators_compile(tmp_path):
    from frisket.actions.list_table import ListTableParams
    from frisket.contracts.http.copilot import CopilotRegisteredActionDraft

    models = [
        ("ActualListTableParams", ListTableParams, "source"),
        ("ActualCopilotDraft", CopilotRegisteredActionDraft, "scope"),
    ]
    records = []
    for name, model, field in models:
        schema = model.model_json_schema()
        assert schema["properties"][field]["discriminator"]["propertyName"] == "kind"
        without_annotation = deepcopy(schema)
        del without_annotation["properties"][field]["discriminator"]
        assert render_typescript_declaration(
            schema, type_name=name
        ) == render_typescript_declaration(without_annotation, type_name=name)
        records.append(_record(name, schema))

    probe = """
import type { ActualListTableParams, ActualCopilotDraft } from './generated';
const list: ActualListTableParams = {
  source: { kind: 'column', sheet_id: 1, column_id: 2 },
};
const draft: ActualCopilotDraft = {
  action_id: 'derive.table_from_list', scope: { kind: 'project' },
  sheet_name: 'People', params: { source: { kind: 'column', sheet_id: 1, column_id: 2 } },
};
// @ts-expect-error list sources require their kind
const missingSourceKind: ActualListTableParams = { source: { sheet_id: 1, column_id: 2 } };
// @ts-expect-error an unknown source kind does not match either union branch
const wrongSourceKind: ActualListTableParams = { source: { kind: 'other', sheet_id: 1, column_id: 2 } };
// @ts-expect-error draft scopes require their kind
const missingScopeKind: ActualCopilotDraft = { action_id: 'derive.table_from_list', scope: {}, sheet_name: 'People', params: {} };
// @ts-expect-error an unknown scope kind does not match either union branch
const wrongScopeKind: ActualCopilotDraft = { action_id: 'derive.table_from_list', scope: { kind: 'other' }, sheet_name: 'People', params: {} };
void list; void draft; void missingSourceKind; void wrongSourceKind;
void missingScopeKind; void wrongScopeKind;
"""
    _compile(tmp_path, records, probe)


def test_actual_discriminated_sources_and_scopes_keep_python_schema_kind_parity():
    from frisket.actions.list_table import ListTableParams
    from frisket.contracts.http.copilot import CopilotRegisteredActionDraft

    cases = [
        (
            ListTableParams,
            "source",
            {"source": {"kind": "column", "sheet_id": 1, "column_id": 2}},
        ),
        (
            CopilotRegisteredActionDraft,
            "scope",
            {
                "action_id": "derive.table_from_list",
                "scope": {"kind": "project"},
                "sheet_name": "People",
                "params": {},
            },
        ),
    ]
    for model, field, value in cases:
        schema = model.model_json_schema()
        validator = Draft202012Validator(schema)
        model.model_validate(value)
        assert validator.is_valid(value)
        for kind in (None, "other"):
            malformed = deepcopy(value)
            if kind is None:
                del malformed[field]["kind"]
            else:
                malformed[field]["kind"] = kind
            with pytest.raises(ValidationError):
                model.model_validate(malformed)
            assert not validator.is_valid(malformed)


@pytest.mark.parametrize(
    "annotation",
    [
        None,
        {},
        {"propertyName": "kind", "mapping": {"column": 1}},
        {"propertyName": "kind", "fallback": "Column"},
    ],
)
def test_malformed_discriminator_annotations_fail_closed(annotation):
    schema = {"type": "object", "discriminator": annotation}
    with pytest.raises(ValueError, match="discriminator"):
        render_typescript_declaration(schema, type_name="MalformedDiscriminator")


@pytest.mark.parametrize(
    "property_names",
    [
        False,
        True,
        {"const": "ONLY"},
        {"enum": ["ONLY"]},
        {"$ref": "#"},
        {"allOf": [{"type": "string"}]},
        {"anyOf": [{"type": "string"}]},
        {"oneOf": [{"type": "string"}]},
        {"not": {"const": "FORBIDDEN"}},
        {"type": "object", "properties": {}},
        {"type": "array", "items": {"type": "string"}},
        {"type": "string", "pattern": "^[A-Z_]+$"},
        {"minLength": -1},
        {"maxLength": True},
        {"minLength": 3, "maxLength": 2},
        {"type": ["string", "null"]},
    ],
)
def test_unrepresented_property_name_constraints_fail_closed(
    property_names: object,
) -> None:
    schema = {
        "type": "object",
        "propertyNames": property_names,
        "additionalProperties": {"type": "string"},
    }
    with pytest.raises(ValueError, match="propertyNames"):
        render_typescript_module(_artifact([_record("BrokenKeys", schema)]))


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        ({"type": "mystery"}, "type"),
        ({"type": "string", "mystery": True}, "keywords"),
        ({"$ref": "https://example.test/schema"}, "non-local"),
        ({"$ref": "#/$defs/missing", "$defs": {}}, "missing"),
        ({"$ref": "#/$defs/bad~2escape", "$defs": {}}, "escape"),
        ({"type": "array", "minItems": -1}, "minItems"),
        ({"type": "number", "multipleOf": 0}, "multipleOf"),
        ({"type": "array", "uniqueItems": "yes"}, "uniqueItems"),
        ({"type": "object", "required": "name"}, "required"),
        ({"type": "object", "propertyNames": "name"}, "propertyNames"),
        ({"type": "string", "pattern": 3}, "pattern"),
        ({"type": "string", "format": "date-time"}, "format"),
    ],
)
def test_invalid_or_unrepresented_schema_domains_fail_generation_with_context(
    schema: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        render_typescript_module(_artifact([_record("BrokenMatrix", schema)]))
