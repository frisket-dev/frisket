"""Fail-closed OpenAPI-to-TypeScript type and route-metadata renderer."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


_ANNOTATION_SCHEMA_KEYS = frozenset(
    {
        "$id",
        "$schema",
        "default",
        "deprecated",
        "description",
        "discriminator",
        "examples",
        "readOnly",
        "title",
        "writeOnly",
    }
)
_ASSERTION_SCHEMA_KEYS = frozenset(
    {
        "$defs",
        "$ref",
        "additionalProperties",
        "allOf",
        "anyOf",
        "const",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "items",
        "maxItems",
        "maxLength",
        "maxProperties",
        "maximum",
        "minItems",
        "minLength",
        "minProperties",
        "minimum",
        "multipleOf",
        "not",
        "oneOf",
        "pattern",
        "prefixItems",
        "properties",
        "propertyNames",
        "required",
        "type",
        "uniqueItems",
    }
)
_SUPPORTED_SCHEMA_KEYS = _ANNOTATION_SCHEMA_KEYS | _ASSERTION_SCHEMA_KEYS
# Admit MCP's runtime key-length constraints without claiming support for
# property-name unions, references, or other key-domain transformations.
_PROPERTY_NAMES_SCHEMA_KEYS = _ANNOTATION_SCHEMA_KEYS | frozenset(
    {"maxLength", "minLength", "type"}
)
_JSON_SCHEMA_TYPES = frozenset(
    {"array", "boolean", "integer", "null", "number", "object", "string"}
)
_TUPLE_UNION_LIMIT = 32
HTTP_CONTRACT_SCHEMA_VERSION = "frisket.http_contract_artifact.v4"
_HTTP_METHODS = frozenset(
    {"delete", "get", "head", "options", "patch", "post", "put", "trace"}
)
_TS_RESERVED = frozenset(
    {
        "any",
        "boolean",
        "break",
        "case",
        "catch",
        "class",
        "const",
        "constructor",
        "continue",
        "debugger",
        "declare",
        "default",
        "delete",
        "do",
        "else",
        "enum",
        "export",
        "extends",
        "false",
        "finally",
        "for",
        "from",
        "function",
        "get",
        "if",
        "implements",
        "import",
        "in",
        "instanceof",
        "interface",
        "keyof",
        "let",
        "module",
        "namespace",
        "never",
        "new",
        "null",
        "number",
        "object",
        "package",
        "private",
        "protected",
        "public",
        "readonly",
        "require",
        "return",
        "set",
        "static",
        "string",
        "super",
        "switch",
        "symbol",
        "this",
        "throw",
        "true",
        "try",
        "type",
        "typeof",
        "undefined",
        "unique",
        "unknown",
        "var",
        "void",
        "while",
        "with",
        "yield",
    }
)


def _identifier(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_$]", "_", value)
    if not cleaned or cleaned[0].isdigit() or cleaned in _TS_RESERVED:
        cleaned = f"Schema_{cleaned}"
    return cleaned


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def _assert_json_value(value: object, *, context: str, seen: set[int]) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{context}: JSON number must be finite")
        return
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            raise ValueError(f"{context}: cyclic JSON value is unsupported")
        seen.add(identity)
        try:
            for key, child in value.items():
                if not isinstance(key, str):
                    raise ValueError(f"{context}: JSON object keys must be strings")
                _assert_json_value(child, context=f"{context}.{key}", seen=seen)
        finally:
            seen.remove(identity)
        return
    if _is_sequence(value):
        identity = id(value)
        if identity in seen:
            raise ValueError(f"{context}: cyclic JSON value is unsupported")
        seen.add(identity)
        try:
            for index, child in enumerate(value):
                _assert_json_value(child, context=f"{context}[{index}]", seen=seen)
        finally:
            seen.remove(identity)
        return
    raise ValueError(f"{context}: value is outside the recursive JSON domain")


def _pointer_segments(ref: str, *, context: str) -> tuple[str, ...]:
    if ref == "#":
        return ()
    if not ref.startswith("#/"):
        raise ValueError(
            f"{context}: unsupported non-local JSON Schema reference {ref!r}"
        )
    if "%" in ref:
        raise ValueError(f"{context}: percent-encoded JSON pointer is unsupported")
    decoded: list[str] = []
    for raw in ref[2:].split("/"):
        if re.search(r"~(?![01])", raw):
            raise ValueError(f"{context}: malformed JSON pointer escape in {ref!r}")
        decoded.append(raw.replace("~1", "/").replace("~0", "~"))
    return tuple(decoded)


def _resolve_pointer(root: object, ref: str, *, context: str) -> object:
    value = root
    for segment in _pointer_segments(ref, context=context):
        if isinstance(value, Mapping):
            if segment not in value:
                raise ValueError(f"{context}: missing JSON Schema reference {ref!r}")
            value = value[segment]
        elif _is_sequence(value) and segment.isdigit():
            index = int(segment)
            if index >= len(value):
                raise ValueError(f"{context}: missing JSON Schema reference {ref!r}")
            value = value[index]
        else:
            raise ValueError(f"{context}: missing JSON Schema reference {ref!r}")
    if not isinstance(value, (Mapping, bool)):
        raise ValueError(f"{context}: reference {ref!r} is not a schema node")
    return value


def _nonnegative_integer(value: object, *, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{context} must be a non-negative integer")
    return value


def _finite_number(value: object, *, context: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{context} must be a finite number")
    return float(value)


def _assert_supported_property_names_schema(
    schema: object, *, root: object, context: str
) -> None:
    if not isinstance(schema, Mapping):
        raise ValueError(f"{context} must be a non-boolean schema object")
    unknown = sorted(set(schema) - _PROPERTY_NAMES_SCHEMA_KEYS)
    if unknown:
        raise ValueError(f"{context}: unsupported propertyNames keywords {unknown}")
    if schema.get("type", "string") != "string":
        raise ValueError(f"{context}.type must be string when present")
    _assert_supported_schema(schema, root=root, context=context)


def _assert_supported_schema(schema: object, *, root: object, context: str) -> None:
    if isinstance(schema, bool):
        return
    if not isinstance(schema, Mapping):
        raise ValueError(f"{context}: JSON Schema node must be an object or boolean")
    unknown = sorted(set(schema) - _SUPPORTED_SCHEMA_KEYS)
    if unknown:
        raise ValueError(f"{context}: unsupported JSON Schema keywords {unknown}")

    if "discriminator" in schema:
        # OpenAPI's discriminator selects a branch; it adds no validation.
        # The union's ordinary oneOf/anyOf, required and const assertions
        # remain authoritative for generated types and schema consumers.
        discriminator = schema["discriminator"]
        if not isinstance(discriminator, Mapping):
            raise ValueError(f"{context}.discriminator must be an object")
        if set(discriminator) - {"propertyName", "mapping"}:
            raise ValueError(f"{context}.discriminator has unsupported keys")
        property_name = discriminator.get("propertyName")
        if not isinstance(property_name, str) or not property_name:
            raise ValueError(
                f"{context}.discriminator.propertyName must be a non-empty string"
            )
        mapping = discriminator.get("mapping", {})
        if not isinstance(mapping, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str) or not value
            for key, value in mapping.items()
        ):
            raise ValueError(
                f"{context}.discriminator.mapping must map strings to non-empty strings"
            )

    for key in ("$id", "$schema", "description", "title"):
        if key in schema and not isinstance(schema[key], str):
            raise ValueError(f"{context}.{key} must be a string")
    for key in ("deprecated", "readOnly", "writeOnly"):
        if key in schema and not isinstance(schema[key], bool):
            raise ValueError(f"{context}.{key} must be a boolean")
    if "default" in schema:
        _assert_json_value(schema["default"], context=f"{context}.default", seen=set())
    if "examples" in schema:
        examples = schema["examples"]
        if not _is_sequence(examples):
            raise ValueError(f"{context}.examples must be an array")
        _assert_json_value(examples, context=f"{context}.examples", seen=set())

    definitions = schema.get("$defs", {})
    if not isinstance(definitions, Mapping):
        raise ValueError(f"{context}.$defs must be an object")
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise ValueError(f"{context}.properties must be an object")
    for container_name, container in (
        ("$defs", definitions),
        ("properties", properties),
    ):
        for name, child in container.items():
            if not isinstance(name, str):
                raise ValueError(f"{context}.{container_name} keys must be strings")
            _assert_supported_schema(
                child, root=root, context=f"{context}.{container_name}.{name}"
            )

    if "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str):
            raise ValueError(f"{context}.$ref must be a string")
        _resolve_pointer(root, ref, context=f"{context}.$ref")

    schema_type = schema.get("type")
    if schema_type is not None:
        if isinstance(schema_type, str):
            types = (schema_type,)
        elif _is_sequence(schema_type):
            types = tuple(schema_type)
            if not types:
                raise ValueError(f"{context}.type must not be empty")
            if not all(isinstance(item, str) for item in types):
                raise ValueError(f"{context}.type entries must be strings")
            if len(set(types)) != len(types):
                raise ValueError(f"{context}.type entries must be unique")
        else:
            raise ValueError(f"{context}.type must be a string or array")
        invalid = sorted(set(types) - _JSON_SCHEMA_TYPES)
        if invalid:
            raise ValueError(f"{context}.type has unsupported values {invalid}")

    required = schema.get("required", ())
    if not _is_sequence(required) or not all(
        isinstance(item, str) for item in required
    ):
        if "required" in schema:
            raise ValueError(f"{context}.required must be an array of strings")
        required = ()
    if len(set(required)) != len(required):
        raise ValueError(f"{context}.required entries must be unique")
    missing_required = sorted(set(required) - set(properties))
    if missing_required:
        raise ValueError(
            f"{context}.required names lack generated properties {missing_required}"
        )

    for key in ("items", "additionalProperties", "not"):
        if key not in schema:
            continue
        child = schema[key]
        if not isinstance(child, (Mapping, bool)):
            raise ValueError(f"{context}.{key} must be a schema object or boolean")
        _assert_supported_schema(child, root=root, context=f"{context}.{key}")
    if "propertyNames" in schema:
        _assert_supported_property_names_schema(
            schema["propertyNames"],
            root=root,
            context=f"{context}.propertyNames",
        )
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        if key not in schema:
            continue
        children = schema[key]
        if not _is_sequence(children):
            raise ValueError(f"{context}.{key} must be an array")
        for index, child in enumerate(children):
            _assert_supported_schema(
                child, root=root, context=f"{context}.{key}[{index}]"
            )

    for key in ("const", "enum"):
        if key not in schema:
            continue
        value = schema[key]
        if key == "enum" and not _is_sequence(value):
            raise ValueError(f"{context}.enum must be an array")
        _assert_json_value(value, context=f"{context}.{key}", seen=set())

    for key in (
        "maximum",
        "minimum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "multipleOf",
    ):
        if key in schema:
            number = _finite_number(schema[key], context=f"{context}.{key}")
            if key == "multipleOf" and number <= 0:
                raise ValueError(f"{context}.multipleOf must be greater than zero")
    for key in (
        "maxItems",
        "maxLength",
        "maxProperties",
        "minItems",
        "minLength",
        "minProperties",
    ):
        if key in schema:
            _nonnegative_integer(schema[key], context=f"{context}.{key}")
    for minimum_key, maximum_key in (
        ("minItems", "maxItems"),
        ("minLength", "maxLength"),
        ("minProperties", "maxProperties"),
    ):
        if minimum_key in schema and maximum_key in schema:
            if int(schema[minimum_key]) > int(schema[maximum_key]):
                raise ValueError(f"{context}.{minimum_key} exceeds {maximum_key}")
    if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
        raise ValueError(f"{context}.uniqueItems must be a boolean")
    if "pattern" in schema:
        pattern = schema["pattern"]
        if not isinstance(pattern, str):
            raise ValueError(f"{context}.pattern must be a string")
    if "format" in schema:
        if not isinstance(schema["format"], str):
            raise ValueError(f"{context}.format must be a string")
        raise ValueError(
            f"{context}.format {schema['format']!r} has no TypeScript representation"
        )


def _walk_schema(schema: object) -> Iterable[object]:
    yield schema
    if not isinstance(schema, Mapping):
        return
    for container_key in ("$defs", "properties"):
        container = schema.get(container_key)
        if isinstance(container, Mapping):
            for child in container.values():
                yield from _walk_schema(child)
    for child_key in ("items", "additionalProperties", "not"):
        child = schema.get(child_key)
        if isinstance(child, (Mapping, bool)):
            yield from _walk_schema(child)
    for children_key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        children = schema.get(children_key)
        if _is_sequence(children):
            for child in children:
                yield from _walk_schema(child)


def _canonical_json(value: object, *, context: str) -> str:
    _assert_json_value(value, context=context, seen=set())
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _openapi_component_name(ref: object, *, context: str) -> str:
    if not isinstance(ref, str):
        raise ValueError(f"{context}: OpenAPI schema $ref must be a string")
    if not ref.startswith("#/"):
        raise ValueError(
            f"{context}: unsupported non-local OpenAPI schema reference {ref!r}"
        )
    segments = _pointer_segments(ref, context=context)
    if len(segments) != 3 or segments[:2] != ("components", "schemas"):
        raise ValueError(f"{context}: unsupported OpenAPI schema reference {ref!r}")
    if not segments[2]:
        raise ValueError(f"{context}: OpenAPI component name must not be empty")
    return segments[2]


def _component_refs(schema: object, *, context: str) -> set[str]:
    return {
        _openapi_component_name(node["$ref"], context=f"{context}.$ref")
        for node in _walk_schema(schema)
        if isinstance(node, Mapping) and "$ref" in node
    }


def _component_type_name(component_name: str) -> str:
    return (
        component_name if component_name.startswith("Http") else f"Http{component_name}"
    )


@dataclass(frozen=True)
class _SchemaBinding:
    type_name: str


class _OpenApiSchemaRecords:
    def __init__(self, components: Mapping[str, object]) -> None:
        self._components: dict[str, object] = {}
        for name, schema in components.items():
            if not isinstance(name, str) or not name:
                raise ValueError(
                    "OpenAPI component schema names must be non-empty strings"
                )
            if not isinstance(schema, (Mapping, bool)):
                raise ValueError(
                    f"OpenAPI component schema {name!r} must be an object or boolean"
                )
            self._components[name] = schema
        self._bindings: dict[tuple[str, str], _SchemaBinding] = {}
        self._records_by_type: dict[str, dict[str, object]] = {}
        self._record_fingerprints: dict[str, str] = {}

    def bind(self, schema: object, *, context: str) -> _SchemaBinding:
        if not isinstance(schema, (Mapping, bool)):
            raise ValueError(f"{context}: OpenAPI schema must be an object or boolean")
        if isinstance(schema, Mapping) and set(schema) == {"$ref"}:
            component_name = _openapi_component_name(
                schema["$ref"], context=f"{context}.$ref"
            )
            return self._component_binding(component_name, context=context)
        fingerprint = hashlib.sha256(
            _canonical_json(schema, context=context).encode("utf-8")
        ).hexdigest()
        key = ("inline", fingerprint)
        existing = self._bindings.get(key)
        if existing is not None:
            return existing
        type_name = f"HttpInline_{fingerprint[:16]}"
        binding = _SchemaBinding(type_name)
        localized = self._localize(schema, root_component=None, context=context)
        self._add_record(binding, localized, fingerprint=fingerprint)
        self._bindings[key] = binding
        return binding

    def _component_binding(
        self, component_name: str, *, context: str
    ) -> _SchemaBinding:
        key = ("component", component_name)
        existing = self._bindings.get(key)
        if existing is not None:
            return existing
        try:
            schema = self._components[component_name]
        except KeyError as exc:
            raise ValueError(
                f"{context}: missing OpenAPI component schema {component_name!r}"
            ) from exc
        type_name = _component_type_name(component_name)
        binding = _SchemaBinding(type_name)
        localized = self._localize(
            schema,
            root_component=component_name,
            context=f"OpenAPI component {component_name}",
        )
        fingerprint = hashlib.sha256(
            _canonical_json(localized, context=f"localized {component_name}").encode(
                "utf-8"
            )
        ).hexdigest()
        self._add_record(binding, localized, fingerprint=fingerprint)
        self._bindings[key] = binding
        return binding

    def _localize(
        self,
        schema: object,
        *,
        root_component: str | None,
        context: str,
    ) -> object:
        _assert_json_value(schema, context=context, seen=set())
        pending = sorted(_component_refs(schema, context=context))
        dependencies: set[str] = set()
        while pending:
            component_name = pending.pop(0)
            if component_name == root_component or component_name in dependencies:
                continue
            try:
                component_schema = self._components[component_name]
            except KeyError as exc:
                raise ValueError(
                    f"{context}: missing OpenAPI component schema {component_name!r}"
                ) from exc
            dependencies.add(component_name)
            for child_name in sorted(
                _component_refs(
                    component_schema,
                    context=f"OpenAPI component {component_name}",
                )
            ):
                if child_name != root_component and child_name not in dependencies:
                    pending.append(child_name)
            pending.sort()

        def rewrite(value: object, location: str) -> object:
            rewritten = json.loads(_canonical_json(value, context=location))
            for node in _walk_schema(rewritten):
                if not isinstance(node, dict) or "$ref" not in node:
                    continue
                component_name = _openapi_component_name(
                    node["$ref"], context=f"{location}.$ref"
                )
                node["$ref"] = (
                    "#"
                    if component_name == root_component
                    else f"#/$defs/{_pointer_token(component_name)}"
                )
            return rewritten

        localized = rewrite(schema, context)
        if dependencies:
            if not isinstance(localized, dict):
                raise ValueError(
                    f"{context}: boolean schema cannot reference components"
                )
            if "$defs" in localized:
                raise ValueError(
                    f"{context}: schema-local $defs cannot be combined with components"
                )
            localized["$defs"] = {
                name: rewrite(
                    self._components[name], f"OpenAPI component dependency {name}"
                )
                for name in sorted(dependencies)
            }
        return localized

    def _add_record(
        self,
        binding: _SchemaBinding,
        schema: object,
        *,
        fingerprint: str,
    ) -> None:
        rendered_type = _identifier(binding.type_name)
        for existing_name in self._records_by_type:
            if _identifier(existing_name) == rendered_type:
                raise ValueError(
                    f"generated OpenAPI types {existing_name!r} and "
                    f"{binding.type_name!r} collide as {rendered_type!r}"
                )
        existing_fingerprint = self._record_fingerprints.get(binding.type_name)
        if existing_fingerprint is not None and existing_fingerprint != fingerprint:
            raise ValueError(
                f"generated OpenAPI type {binding.type_name!r} has conflicting schemas"
            )
        self._records_by_type[binding.type_name] = {
            "typeName": binding.type_name,
            "schema": schema,
        }
        self._record_fingerprints[binding.type_name] = fingerprint

    def records(self) -> list[dict[str, object]]:
        return [self._records_by_type[name] for name in sorted(self._records_by_type)]


def _object_schema_for_parameters(
    parameters: Sequence[Mapping[str, object]], *, location: str
) -> dict[str, object]:
    properties: dict[str, object] = {}
    required: list[str] = []
    for parameter in sorted(parameters, key=lambda item: str(item.get("name", ""))):
        name = parameter.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"OpenAPI {location} parameter name must be non-empty")
        if name in properties:
            raise ValueError(f"duplicate OpenAPI {location} parameter {name!r}")
        schema = parameter.get("schema")
        if not isinstance(schema, (Mapping, bool)):
            raise ValueError(f"OpenAPI {location} parameter {name!r} lacks a schema")
        properties[name] = schema
        is_required = parameter.get("required", False)
        if not isinstance(is_required, bool):
            raise ValueError(
                f"OpenAPI {location} parameter {name!r} required must be boolean"
            )
        if location == "path" and not is_required:
            raise ValueError(f"OpenAPI path parameter {name!r} must be required")
        if is_required:
            required.append(name)
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(required),
        "additionalProperties": False,
    }


def _merged_parameters(
    inherited: object, direct: object, *, context: str
) -> tuple[Mapping[str, object], ...]:
    merged: dict[tuple[str, str], Mapping[str, object]] = {}
    for label, raw_parameters in (("path", inherited), ("operation", direct)):
        if raw_parameters is None:
            continue
        if not _is_sequence(raw_parameters):
            raise ValueError(f"{context}: {label} parameters must be an array")
        seen: set[tuple[str, str]] = set()
        for index, raw in enumerate(raw_parameters):
            if not isinstance(raw, Mapping):
                raise ValueError(
                    f"{context}: {label} parameter {index} must be an object"
                )
            if "$ref" in raw:
                raise ValueError(
                    f"{context}: referenced OpenAPI parameters are unsupported"
                )
            name = raw.get("name")
            location = raw.get("in")
            if not isinstance(name, str) or not name:
                raise ValueError(
                    f"{context}: {label} parameter {index} has invalid name"
                )
            if location not in {"path", "query"}:
                raise ValueError(
                    f"{context}: unsupported parameter location {location!r}"
                )
            key = (name, location)
            if key in seen:
                raise ValueError(f"{context}: duplicate {label} parameter {key!r}")
            seen.add(key)
            merged[key] = raw
    return tuple(merged[key] for key in sorted(merged))


def _json_content_schema(content: object, *, context: str) -> tuple[str, object] | None:
    if content is None:
        return None
    if not isinstance(content, Mapping):
        raise ValueError(f"{context}: content must be an object")
    if not content:
        return None
    if len(content) != 1:
        raise ValueError(f"{context}: exactly one JSON media type is required")
    media_type, media = next(iter(content.items()))
    if not isinstance(media_type, str) or not (
        media_type == "application/json"
        or (media_type.startswith("application/") and media_type.endswith("+json"))
    ):
        raise ValueError(f"{context}: unsupported JSON media type {media_type!r}")
    if not isinstance(media, Mapping):
        raise ValueError(f"{context}: JSON media record must be an object")
    schema = media.get("schema")
    if not isinstance(schema, (Mapping, bool)):
        raise ValueError(f"{context}: JSON media lacks a schema")
    return media_type, schema


def _is_native_form_data_request(operation_id: str, content: object) -> bool:
    """Recognize fixed native upload bodies without compiling file schemas."""

    return (
        operation_id
        in {
            "tenant.import_bulk_plan.post",
            "tenant.import_csv.post",
            "tenant.append_csv.post",
            "tenant.update_csv.post",
            "tenant.import_csv_update_preview.post",
            "tenant.append_xlsx.post",
            "tenant.import_xlsx_preview.post",
            "tenant.import_xlsx_update_preview.post",
            "tenant.update_xlsx.post",
            "tenant.import_csv_preview.post",
            "tenant.import_xlsx.post",
            "tenant.import_pdf.post",
            "tenant.import_files.post",
            "tenant.import_followthemoney.post",
            "tenant.ocr_compare_scratch.post",
            "tenant.ocr_compare_scratch_estimate.post",
            "tenant.transcribe_compare_scratch.post",
            "tenant.transcribe_compare_scratch_estimate.post",
            "tenant.topic_segmentation_compare_scratch.post",
        }
        and isinstance(content, Mapping)
        and set(content) == {"multipart/form-data"}
    )


def _is_native_form_data_operation(operation_id: str, media_type: str) -> bool:
    return media_type == "multipart/form-data" and operation_id in {
        "tenant.import_bulk_plan.post",
        "tenant.import_csv.post",
        "tenant.append_csv.post",
        "tenant.update_csv.post",
        "tenant.import_csv_update_preview.post",
        "tenant.append_xlsx.post",
        "tenant.import_xlsx_preview.post",
        "tenant.import_xlsx_update_preview.post",
        "tenant.update_xlsx.post",
        "tenant.import_csv_preview.post",
        "tenant.import_xlsx.post",
        "tenant.import_pdf.post",
        "tenant.import_files.post",
        "tenant.import_followthemoney.post",
        "tenant.ocr_compare_scratch.post",
        "tenant.ocr_compare_scratch_estimate.post",
        "tenant.transcribe_compare_scratch.post",
        "tenant.transcribe_compare_scratch_estimate.post",
        "tenant.topic_segmentation_compare_scratch.post",
    }


def _response_metadata(
    responses: object,
    *,
    schemas: _OpenApiSchemaRecords,
    context: str,
) -> dict[str, object]:
    if not isinstance(responses, Mapping) or not responses:
        raise ValueError(f"{context}: responses must be a non-empty object")
    projected: dict[str, object] = {}
    for raw_status in sorted(responses, key=str):
        if (
            not isinstance(raw_status, str)
            or re.fullmatch(r"[1-5][0-9]{2}", raw_status) is None
        ):
            raise ValueError(
                f"{context}: response status {raw_status!r} must be a numeric response status"
            )
        response = responses[raw_status]
        if not isinstance(response, Mapping):
            raise ValueError(f"{context}: response {raw_status} must be an object")
        if "$ref" in response:
            raise ValueError(f"{context}: referenced OpenAPI responses are unsupported")
        selected = _json_content_schema(
            response.get("content"), context=f"{context} response {raw_status}"
        )
        if selected is None:
            projected[raw_status] = {
                "hasBody": False,
                "typeName": None,
            }
            continue
        _media_type, schema = selected
        binding = schemas.bind(
            schema, context=f"{context} response {raw_status} schema"
        )
        projected[raw_status] = {
            "hasBody": True,
            "typeName": binding.type_name,
        }
    return projected


def openapi_contract_artifact(document: Mapping[str, Any]) -> dict[str, Any]:
    """Project real OpenAPI into the generated browser contract artifact."""

    _assert_json_value(document, context="OpenAPI document", seen=set())
    version = document.get("openapi")
    if not isinstance(version, str) or not version.startswith("3.1."):
        raise ValueError("OpenAPI projection requires an OpenAPI 3.1 document")
    raw_components = document.get("components", {})
    if not isinstance(raw_components, Mapping):
        raise ValueError("OpenAPI components must be an object")
    component_schemas = raw_components.get("schemas", {})
    if not isinstance(component_schemas, Mapping):
        raise ValueError("OpenAPI component schemas must be an object")
    schemas = _OpenApiSchemaRecords(component_schemas)

    paths = document.get("paths")
    if not isinstance(paths, Mapping):
        raise ValueError("OpenAPI paths must be an object")
    endpoints_by_id: dict[str, dict[str, object]] = {}
    for path in sorted(paths):
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError(f"OpenAPI path {path!r} must start with '/'")
        path_item = paths[path]
        if not isinstance(path_item, Mapping):
            raise ValueError(f"OpenAPI path item {path!r} must be an object")
        for method in sorted(set(path_item) & _HTTP_METHODS):
            operation = path_item[method]
            context = f"OpenAPI {method.upper()} {path}"
            if not isinstance(operation, Mapping):
                raise ValueError(f"{context}: operation must be an object")
            operation_id = operation.get("operationId")
            if not isinstance(operation_id, str) or not operation_id.strip():
                raise ValueError(f"{context}: operationId must be non-empty")
            if operation_id in endpoints_by_id:
                raise ValueError(f"duplicate OpenAPI operationId {operation_id!r}")

            parameters = _merged_parameters(
                path_item.get("parameters"),
                operation.get("parameters"),
                context=context,
            )
            path_parameters = tuple(
                parameter for parameter in parameters if parameter["in"] == "path"
            )
            query_parameters = tuple(
                parameter for parameter in parameters if parameter["in"] == "query"
            )
            placeholders = re.findall(r"\{([^{}]+)\}", path)
            if len(placeholders) != len(set(placeholders)):
                raise ValueError(f"{context}: duplicate path placeholder")
            path_names = {str(parameter["name"]) for parameter in path_parameters}
            if set(placeholders) != path_names:
                raise ValueError(
                    f"{context}: path placeholders and parameters differ: "
                    f"{sorted(set(placeholders) ^ path_names)}"
                )

            path_binding = schemas.bind(
                _object_schema_for_parameters(path_parameters, location="path"),
                context=f"{context} path parameters",
            )
            query_binding = schemas.bind(
                _object_schema_for_parameters(query_parameters, location="query"),
                context=f"{context} query parameters",
            )

            request_metadata: dict[str, object] | None = None
            raw_request = operation.get("requestBody")
            if raw_request is not None:
                if not isinstance(raw_request, Mapping):
                    raise ValueError(f"{context}: requestBody must be an object")
                if "$ref" in raw_request:
                    raise ValueError(
                        f"{context}: referenced OpenAPI request bodies are unsupported"
                    )
                required = raw_request.get("required", False)
                if not isinstance(required, bool):
                    raise ValueError(
                        f"{context}: request body required must be boolean"
                    )
                content = raw_request.get("content")
                if _is_native_form_data_request(operation_id, content):
                    request_metadata = {
                        "required": required,
                        "mediaType": "multipart/form-data",
                    }
                else:
                    selected = _json_content_schema(
                        content, context=f"{context} request body"
                    )
                    if selected is None:
                        raise ValueError(f"{context}: request body lacks JSON media")
                    media_type, schema = selected
                    request_binding = schemas.bind(
                        schema, context=f"{context} request schema"
                    )
                    request_metadata = {
                        "required": required,
                        "mediaType": media_type,
                        "typeName": request_binding.type_name,
                    }

            endpoints_by_id[operation_id] = {
                "id": operation_id,
                "method": method.upper(),
                "path": path,
                "pathParams": {
                    "typeName": path_binding.type_name,
                },
                "query": {
                    "typeName": query_binding.type_name,
                },
                "request": request_metadata,
                "responses": _response_metadata(
                    operation.get("responses"), schemas=schemas, context=context
                ),
            }

    return {
        "schemaVersion": HTTP_CONTRACT_SCHEMA_VERSION,
        "endpoints": [endpoints_by_id[name] for name in sorted(endpoints_by_id)],
        "schemas": schemas.records(),
    }


@dataclass
class _TypeContext:
    type_name: str
    root: object
    ref_names: dict[str, str]

    @classmethod
    def build(cls, type_name: str, root: object) -> _TypeContext:
        refs = sorted(
            {
                str(node["$ref"])
                for node in _walk_schema(root)
                if isinstance(node, Mapping) and "$ref" in node
            }
        )
        used = {_identifier(type_name)}
        names: dict[str, str] = {}
        for ref in refs:
            _resolve_pointer(root, ref, context=f"{type_name}.$ref")
            if ref == "#":
                names[ref] = _identifier(type_name)
                continue
            label_segments = [
                segment
                for segment in _pointer_segments(ref, context=f"{type_name}.$ref")
                if segment != "$defs"
            ]
            label = "_".join(label_segments) or "Reference"
            base = f"{_identifier(type_name)}_{_identifier(label)}"
            candidate = base
            suffix = 2
            while candidate in used:
                candidate = f"{base}_{suffix}"
                suffix += 1
            used.add(candidate)
            names[ref] = candidate
        return cls(type_name=type_name, root=root, ref_names=names)

    def ref_name(self, ref: str) -> str:
        try:
            return self.ref_names[ref]
        except KeyError as exc:
            raise ValueError(
                f"{self.type_name}: unresolved schema reference {ref!r}"
            ) from exc


def _literal(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return json.dumps(value)
    raise ValueError(f"unsupported TypeScript literal {value!r}")


def _parenthesized_union(parts: Sequence[str]) -> str:
    if not parts:
        return "never"
    return " | ".join(f"({part})" for part in parts)


def _tuple_for_length(
    prefix_items: Sequence[object], trailing: object, length: int, context: _TypeContext
) -> str:
    items: list[str] = []
    for index in range(length):
        item_schema = prefix_items[index] if index < len(prefix_items) else trailing
        items.append(_ts_type(item_schema, context=context))
    return "[" + ", ".join(items) + "]"


def _array_type(schema: Mapping[str, Any], *, context: _TypeContext) -> str:
    prefix_items = schema.get("prefixItems")
    if not _is_sequence(prefix_items):
        return f"Array<{_ts_type(schema.get('items', True), context=context)}>"

    prefix = tuple(prefix_items)
    trailing = schema.get("items", True)
    minimum = int(schema.get("minItems", 0))
    raw_maximum = schema.get("maxItems")
    maximum = int(raw_maximum) if raw_maximum is not None else None
    if trailing is False:
        maximum = len(prefix) if maximum is None else min(maximum, len(prefix))

    if maximum is not None:
        if minimum > maximum:
            return "never"
        if maximum - minimum > _TUPLE_UNION_LIMIT:
            raise ValueError(
                f"{context.type_name}: prefixItems tuple range "
                f"{minimum}..{maximum} exceeds {_TUPLE_UNION_LIMIT} generated variants"
            )
        return _parenthesized_union(
            [
                _tuple_for_length(prefix, trailing, length, context)
                for length in range(minimum, maximum + 1)
            ]
        )

    required: list[str] = []
    for index in range(max(minimum, len(prefix))):
        if index < minimum:
            item_schema = prefix[index] if index < len(prefix) else trailing
            required.append(_ts_type(item_schema, context=context))
    optional = [
        f"{_ts_type(prefix[index], context=context)}?"
        for index in range(minimum, len(prefix))
    ]
    trailing_type = _ts_type(trailing, context=context)
    if not required and not optional:
        return f"Array<{trailing_type}>"
    return "[" + ", ".join([*required, *optional, f"...Array<{trailing_type}>"]) + "]"


def _object_type(schema: Mapping[str, Any], *, context: _TypeContext) -> str:
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise ValueError(f"{context.type_name}: object properties must be a mapping")
    required = set(schema.get("required", ()))
    property_types = {
        str(name): _ts_type(child, context=context)
        for name, child in properties.items()
    }
    members = [
        f"  {json.dumps(name)}{'' if name in required else '?'}: {child_type};"
        for name, child_type in property_types.items()
    ]
    additional = schema.get("additionalProperties", True)
    if additional is not False:
        additional_type = _ts_type(additional, context=context)
        index_types = list(dict.fromkeys([additional_type, *property_types.values()]))
        if any(name not in required for name in property_types):
            index_types.append("undefined")
        members.append(f"  [key: string]: {' | '.join(index_types)};")
    if not members:
        if additional is False:
            return "Record<string, never>"
        return f"Record<string, {_ts_type(additional, context=context)}>"
    return "{\n" + "\n".join(members) + "\n}"


def _base_type(schema: Mapping[str, Any], *, context: _TypeContext) -> str | None:
    schema_type = schema.get("type")
    if _is_sequence(schema_type):
        return _parenthesized_union(
            [
                _base_type({**schema, "type": item}, context=context) or "JsonValue"
                for item in schema_type
            ]
        )
    if schema_type == "null":
        return "null"
    if schema_type == "boolean":
        return "boolean"
    if schema_type in {"integer", "number"}:
        return "number"
    if schema_type == "string":
        return "string"
    if schema_type == "array":
        return _array_type(schema, context=context)
    if schema_type == "object":
        return _object_type(schema, context=context)
    return None


def _ts_type(schema: object, *, context: _TypeContext) -> str:
    if schema is True:
        return "JsonValue"
    if schema is False:
        return "never"
    if not isinstance(schema, Mapping):
        raise ValueError(f"invalid JSON Schema node for {context.type_name}")

    parts: list[str] = []
    ref = schema.get("$ref")
    if isinstance(ref, str):
        parts.append(context.ref_name(ref))
    if "const" in schema:
        parts.append(_literal(schema["const"]))
    enum = schema.get("enum")
    if _is_sequence(enum):
        parts.append(
            "never" if not enum else " | ".join(_literal(value) for value in enum)
        )
    all_of = schema.get("allOf")
    if _is_sequence(all_of):
        parts.extend(_ts_type(child, context=context) for child in all_of)
    for union_key in ("anyOf", "oneOf"):
        variants = schema.get(union_key)
        if _is_sequence(variants):
            parts.append(
                _parenthesized_union(
                    [_ts_type(variant, context=context) for variant in variants]
                )
            )
    base = _base_type(schema, context=context)
    if base is not None:
        parts.append(base)
    if not parts:
        return "JsonValue"
    return " & ".join(f"({part})" for part in dict.fromkeys(parts))


def _render_type(record: Mapping[str, Any]) -> str:
    raw_type_name = str(record["typeName"])
    type_name = _identifier(raw_type_name)
    schema = record["schema"]
    if not isinstance(schema, (Mapping, bool)):
        raise ValueError(f"{raw_type_name}: schema must be an object or boolean")
    _assert_supported_schema(schema, root=schema, context=raw_type_name)
    context = _TypeContext.build(raw_type_name, schema)
    aliases: list[str] = []
    for ref, alias in context.ref_names.items():
        if ref == "#":
            continue
        target = _resolve_pointer(schema, ref, context=f"{raw_type_name}.$ref")
        rendered = _ts_type(target, context=context)
        if rendered.strip("()") == alias:
            raise ValueError(f"{raw_type_name}: unproductive reference cycle at {ref}")
        aliases.append(f"type {alias} = {rendered};")
    root_block = f"export type {type_name} = {_ts_type(schema, context=context)};"
    return "\n\n".join([*aliases, root_block])


def render_typescript_declaration(schema: object, *, type_name: str) -> str:
    """Render one named JSON Schema with the canonical TypeScript renderer."""

    return _render_type({"typeName": type_name, "schema": schema})


_TYPE_SUPPORT = (
    "type JsonValue = null | boolean | number | string | JsonValue[] | "
    "{ [key: string]: JsonValue };"
)


def _metadata_binding(
    value: object,
    *,
    records_by_type: Mapping[str, Mapping[str, Any]],
    context: str,
) -> str:
    if not isinstance(value, Mapping):
        raise ValueError(f"{context} must be an object")
    type_name = value.get("typeName")
    if not isinstance(type_name, str):
        raise ValueError(f"{context} must name a generated type")
    record = records_by_type.get(type_name)
    if record is None:
        raise ValueError(f"{context} references unknown type {type_name!r}")
    return _identifier(type_name)


def _render_operation_map(
    artifact: Mapping[str, Any], records: Sequence[Mapping[str, Any]]
) -> str:
    raw_endpoints = artifact.get("endpoints")
    if not _is_sequence(raw_endpoints):
        raise ValueError("HTTP contract artifact endpoints must be an array")
    records_by_type = {str(record["typeName"]): record for record in records}
    endpoint_blocks: list[tuple[str, str]] = []
    used_ids: set[str] = set()
    for index, raw_endpoint in enumerate(raw_endpoints):
        if not isinstance(raw_endpoint, Mapping):
            raise ValueError(f"HTTP contract endpoint {index} must be an object")
        operation_id = raw_endpoint.get("id")
        method = raw_endpoint.get("method")
        path = raw_endpoint.get("path")
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError(f"HTTP contract endpoint {index} has invalid id")
        if operation_id in used_ids:
            raise ValueError(f"duplicate HTTP contract endpoint id {operation_id!r}")
        used_ids.add(operation_id)
        if not isinstance(method, str) or method.lower() not in _HTTP_METHODS:
            raise ValueError(
                f"HTTP contract endpoint {operation_id!r} has invalid method"
            )
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError(
                f"HTTP contract endpoint {operation_id!r} has invalid path"
            )

        path_type = _metadata_binding(
            raw_endpoint.get("pathParams"),
            records_by_type=records_by_type,
            context=f"HTTP contract endpoint {operation_id!r} pathParams",
        )
        query_type = _metadata_binding(
            raw_endpoint.get("query"),
            records_by_type=records_by_type,
            context=f"HTTP contract endpoint {operation_id!r} query",
        )
        request = raw_endpoint.get("request")
        if request is None:
            request_type = "undefined"
        else:
            required = request.get("required") if isinstance(request, Mapping) else None
            media_type = (
                request.get("mediaType") if isinstance(request, Mapping) else None
            )
            if not isinstance(required, bool) or not isinstance(media_type, str):
                raise ValueError(
                    f"HTTP contract endpoint {operation_id!r} request metadata is invalid"
                )
            if _is_native_form_data_operation(operation_id, media_type):
                request_type = "FormData"
            else:
                request_type = _metadata_binding(
                    request,
                    records_by_type=records_by_type,
                    context=f"HTTP contract endpoint {operation_id!r} request",
                )
            if not required:
                request_type = f"{request_type} | undefined"

        raw_responses = raw_endpoint.get("responses")
        if not isinstance(raw_responses, Mapping) or not raw_responses:
            raise ValueError(
                f"HTTP contract endpoint {operation_id!r} responses must be non-empty"
            )
        response_members: list[str] = []
        for status in sorted(raw_responses, key=str):
            if (
                not isinstance(status, str)
                or re.fullmatch(r"[1-5][0-9]{2}", status) is None
            ):
                raise ValueError(
                    f"HTTP contract endpoint {operation_id!r} has invalid status {status!r}"
                )
            metadata = raw_responses[status]
            if not isinstance(metadata, Mapping):
                raise ValueError(
                    f"HTTP contract endpoint {operation_id!r} response {status} is invalid"
                )
            has_body = metadata.get("hasBody")
            if has_body is True:
                response_type = _metadata_binding(
                    metadata,
                    records_by_type=records_by_type,
                    context=(
                        f"HTTP contract endpoint {operation_id!r} response {status}"
                    ),
                )
            elif has_body is False:
                if metadata.get("typeName") is not None:
                    raise ValueError(
                        f"HTTP contract endpoint {operation_id!r} response {status} "
                        "bodyless metadata must be null"
                    )
                response_type = "undefined"
            else:
                raise ValueError(
                    f"HTTP contract endpoint {operation_id!r} response {status} "
                    "has invalid hasBody"
                )
            response_members.append(
                f"      readonly {json.dumps(status)}: {response_type};"
            )

        endpoint_block = "\n".join(
            [
                f"  readonly {json.dumps(operation_id)}: {{",
                f"    readonly pathParams: {path_type};",
                f"    readonly query: {query_type};",
                f"    readonly request: {request_type};",
                "    readonly responses: {",
                *response_members,
                "    };",
                "  };",
            ]
        )
        endpoint_blocks.append((operation_id, endpoint_block))
    if not endpoint_blocks:
        body = "Record<never, never>"
    else:
        body = (
            "{\n"
            + "\n".join(block for _operation_id, block in sorted(endpoint_blocks))
            + "\n}"
        )
    return (
        f"export type HttpContractOperationMap = {body};\n\n"
        "export type HttpContractOperationId = keyof HttpContractOperationMap;"
    )


def render_typescript_module(artifact: Mapping[str, Any]) -> str:
    raw_records = artifact.get("schemas")
    if not _is_sequence(raw_records):
        raise ValueError("HTTP contract artifact schemas must be an array")
    records: list[Mapping[str, Any]] = []
    used_types: dict[str, str] = {}
    for index, raw in enumerate(raw_records):
        if not isinstance(raw, Mapping):
            raise ValueError(f"HTTP contract schema record {index} must be an object")
        for key in ("typeName", "schema"):
            if key not in raw:
                raise ValueError(f"HTTP contract schema record {index} lacks {key}")
        type_name = str(raw["typeName"])
        rendered = _identifier(type_name)
        collision = used_types.get(rendered)
        if collision is not None:
            raise ValueError(
                f"HTTP contract type names {collision!r} and {type_name!r} "
                f"collide as {rendered!r}"
            )
        used_types[rendered] = type_name
        records.append(raw)

    runtime_metadata = {
        "schemaVersion": artifact.get("schemaVersion"),
        "endpoints": [
            {
                "id": endpoint["id"],
                "method": endpoint["method"],
                "path": endpoint["path"],
                "request": (
                    None
                    if endpoint["request"] is None
                    else {
                        "mediaType": endpoint["request"]["mediaType"],
                        "required": endpoint["request"]["required"],
                    }
                ),
            }
            for endpoint in artifact["endpoints"]
        ],
    }
    metadata_json = json.dumps(
        runtime_metadata, indent=2, sort_keys=True, allow_nan=False
    )
    type_blocks = [_render_type(record) for record in records]
    operation_map = _render_operation_map(artifact, records)
    return (
        "\n\n".join(
            [
                "// Generated by scripts/ci/gen_http_contracts.py from real composition OpenAPI.\n"
                "// Do not hand-edit. Run `uv run python scripts/ci/gen_http_contracts.py` instead.",
                f"export const HTTP_CONTRACT_ARTIFACT = {metadata_json} as const;",
                _TYPE_SUPPORT,
                *type_blocks,
                operation_map,
            ]
        )
        + "\n"
    )


__all__ = [
    "HTTP_CONTRACT_SCHEMA_VERSION",
    "openapi_contract_artifact",
    "render_typescript_declaration",
    "render_typescript_module",
]
