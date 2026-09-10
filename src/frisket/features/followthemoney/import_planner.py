"""Read-only FollowTheMoney entity stream import planner."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from io import TextIOBase
from typing import Any

from frisket.features.followthemoney._sdk_import import require_followthemoney_sdk

with require_followthemoney_sdk():
    from followthemoney import model

from frisket.features.followthemoney.adapter import validate_entity
from frisket.features.followthemoney.schema_catalog import (
    SUPPORTED_SCHEMA_PRESETS,
    get_schema_metadata,
)

FOLLOWTHEMONEY_IMPORT_PLAN_SCHEMA_VERSION = "frisket.followthemoney.import_plan.v1"

TECHNICAL_IMPORT_COLUMNS = (
    "_ftm_id",
    "_ftm_schema",
    "_ftm_caption",
    "_ftm_properties_json",
    "_ftm_source_refs_json",
    "_ftm_raw_json",
    "_ftm_dataset",
)

_TECHNICAL_COLUMN_TYPES = {
    "_ftm_id": "text",
    "_ftm_schema": "text",
    "_ftm_caption": "text",
    "_ftm_properties_json": "json",
    "_ftm_source_refs_json": "json",
    "_ftm_raw_json": "json",
    "_ftm_dataset": "json",
}

_FTM_TYPE_TO_COLUMN_TYPE = {
    "date": "date",
    "number": "number",
    "float": "number",
    "integer": "number",
    "amount": "number",
    "url": "link",
}


@dataclass(frozen=True)
class FtmImportColumnPlan:
    name: str
    type: str
    hidden: bool = False
    ftm_property: str | None = None
    ftm_type: str | None = None
    label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "hidden": self.hidden,
        }
        if self.ftm_property is not None:
            data["ftm_property"] = self.ftm_property
        if self.ftm_type is not None:
            data["ftm_type"] = self.ftm_type
        if self.label is not None:
            data["label"] = self.label
        return data


@dataclass(frozen=True)
class FtmImportRowPlan:
    entity_id: str
    schema: str
    values: dict[str, Any]
    source_ftm_id: str | None = None
    target_ftm_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "entity_id": self.entity_id,
            "schema": self.schema,
            "values": self.values,
        }
        if self.source_ftm_id is not None:
            data["source_ftm_id"] = self.source_ftm_id
        if self.target_ftm_id is not None:
            data["target_ftm_id"] = self.target_ftm_id
        return data


@dataclass(frozen=True)
class FtmImportSheetPlan:
    schema: str
    kind: str
    sheet_name: str
    columns: list[FtmImportColumnPlan]
    rows: list[FtmImportRowPlan]
    supported: bool = True
    source_property: str | None = None
    target_property: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema": self.schema,
            "kind": self.kind,
            "sheet_name": self.sheet_name,
            "supported": self.supported,
            "columns": [column.to_dict() for column in self.columns],
            "rows": [row.to_dict() for row in self.rows],
            "row_count": len(self.rows),
        }
        if self.source_property is not None:
            data["source_property"] = self.source_property
        if self.target_property is not None:
            data["target_property"] = self.target_property
        return data


@dataclass(frozen=True)
class _PlannableEntity:
    raw: dict[str, Any]
    entity: dict[str, Any]
    line_number: int | None = None
    entity_index: int | None = None


class FollowTheMoneyImportLimitError(ValueError):
    """The entity stream exceeded a caller-owned planning ceiling."""

    def __init__(self, max_entities: int) -> None:
        self.max_entities = max_entities
        super().__init__(f"FtM import exceeds the {max_entities}-row limit")


def plan_followthemoney_import(
    source: str | bytes | TextIOBase | Mapping[str, Any] | Iterable[Any],
    *,
    dataset_name: str | None = None,
    supported_schemas: Iterable[str] | None = None,
    max_entities: int | None = None,
    max_diagnostics: int | None = None,
    max_diagnostic_chars: int | None = None,
) -> dict[str, Any]:
    """Plan a deterministic schema-per-sheet import for an FtM entity stream.

    The planner is deliberately read-only: it validates and groups entities, but
    it does not create sheets, fetch documents, or canonicalize FtM ids.
    """

    if max_entities is not None and max_entities < 0:
        raise ValueError("max_entities must be non-negative or None")
    if max_diagnostics is not None and max_diagnostics < 1:
        raise ValueError("max_diagnostics must be positive or None")
    if max_diagnostic_chars is not None and max_diagnostic_chars < 1:
        raise ValueError("max_diagnostic_chars must be positive or None")
    supported = frozenset(supported_schemas or SUPPORTED_SCHEMA_PRESETS)
    parsed = _parse_entity_stream(
        source,
        max_entities=max_entities,
        max_diagnostics=max_diagnostics,
        max_diagnostic_chars=max_diagnostic_chars,
    )
    diagnostics = list(parsed["diagnostics"])
    diagnostic_count = int(parsed["diagnostic_count"])
    entity_items: list[_PlannableEntity] = parsed["entities"]

    def record_diagnostic(diagnostic: dict[str, Any]) -> None:
        nonlocal diagnostic_count
        diagnostic_count += 1
        if max_diagnostics is None or len(diagnostics) < max_diagnostics:
            diagnostics.append(
                _bounded_diagnostic(diagnostic, max_chars=max_diagnostic_chars)
            )

    entity_groups: dict[str, list[_PlannableEntity]] = {}
    relationship_groups: dict[str, list[_PlannableEntity]] = {}
    unsupported_groups: dict[str, list[_PlannableEntity]] = {}

    for item in entity_items:
        schema_name = str(item.entity["schema"])
        metadata = get_schema_metadata(schema_name)
        source_property, target_property = _relationship_properties(
            schema_name, metadata
        )
        is_relationship = _is_relationship_schema(schema_name, metadata)
        if schema_name not in supported:
            record_diagnostic(
                _diagnostic(
                    "unsupported_schema",
                    "warning",
                    f"unsupported FtM schema for import planner: {schema_name}",
                    schema=schema_name,
                    entity_id=item.entity.get("id"),
                    line_number=item.line_number,
                    entity_index=item.entity_index,
                )
            )
            if is_relationship:
                record_diagnostic(
                    _diagnostic(
                        "unsupported_relationship_schema",
                        "warning",
                        f"unsupported relationship schema for import planner: {schema_name}",
                        schema=schema_name,
                        entity_id=item.entity.get("id"),
                        line_number=item.line_number,
                        entity_index=item.entity_index,
                    )
                )
            unsupported_groups.setdefault(schema_name, []).append(item)
            continue
        if is_relationship:
            if source_property is None or target_property is None:
                record_diagnostic(
                    _diagnostic(
                        "relationship_props_missing",
                        "warning",
                        f"relationship schema has no discoverable endpoint properties: {schema_name}",
                        schema=schema_name,
                        entity_id=item.entity.get("id"),
                        line_number=item.line_number,
                        entity_index=item.entity_index,
                    )
                )
            relationship_groups.setdefault(schema_name, []).append(item)
        else:
            entity_groups.setdefault(schema_name, []).append(item)

    sheets = [
        _build_sheet(
            schema_name,
            entity_groups[schema_name],
            kind="entity",
            supported=True,
            dataset_name=dataset_name,
        ).to_dict()
        for schema_name in sorted(entity_groups)
    ]
    relationship_sheets = [
        _build_sheet(
            schema_name,
            relationship_groups[schema_name],
            kind="relationship",
            supported=True,
            dataset_name=dataset_name,
        ).to_dict()
        for schema_name in sorted(relationship_groups)
    ]
    unsupported_sheets = [
        _build_sheet(
            schema_name,
            unsupported_groups[schema_name],
            kind="unsupported",
            supported=False,
            dataset_name=dataset_name,
        ).to_dict()
        for schema_name in sorted(unsupported_groups)
    ]
    diagnostics = sorted(diagnostics, key=_diagnostic_sort_key)
    if max_diagnostics is not None and diagnostic_count > len(diagnostics):
        retained_count = max_diagnostics - 1
        diagnostics = diagnostics[:retained_count]
        diagnostics.append(
            _diagnostic(
                "diagnostics_truncated",
                "warning",
                "Additional FtM import diagnostics were omitted.",
                omitted_count=diagnostic_count - retained_count,
            )
        )
    planned_row_count = sum(
        sheet["row_count"]
        for sheet in (*sheets, *unsupported_sheets, *relationship_sheets)
    )
    return {
        "schema_version": FOLLOWTHEMONEY_IMPORT_PLAN_SCHEMA_VERSION,
        "dataset": dataset_name,
        "sheets": sheets,
        "relationship_sheets": relationship_sheets,
        "unsupported_sheets": unsupported_sheets,
        "diagnostics": diagnostics,
        "entity_count": len(entity_items),
        "planned_row_count": planned_row_count,
        "relationship_count": sum(sheet["row_count"] for sheet in relationship_sheets),
        "unsupported_count": sum(sheet["row_count"] for sheet in unsupported_sheets),
        "invalid_count": parsed["invalid_count"],
        "diagnostic_count": diagnostic_count,
    }


def _parse_entity_stream(
    source: str | bytes | TextIOBase | Mapping[str, Any] | Iterable[Any],
    *,
    max_entities: int | None,
    max_diagnostics: int | None,
    max_diagnostic_chars: int | None,
) -> dict[str, Any]:
    entities: list[_PlannableEntity] = []
    diagnostics: list[dict[str, Any]] = []
    invalid_count = 0
    diagnostic_count = 0

    def record_diagnostics(items: Iterable[dict[str, Any]]) -> None:
        nonlocal diagnostic_count
        for diagnostic in items:
            diagnostic_count += 1
            if max_diagnostics is None or len(diagnostics) < max_diagnostics:
                diagnostics.append(
                    _bounded_diagnostic(diagnostic, max_chars=max_diagnostic_chars)
                )

    def record_entity(item: _PlannableEntity) -> None:
        if max_entities is not None and len(entities) >= max_entities:
            raise FollowTheMoneyImportLimitError(max_entities)
        entities.append(item)

    if isinstance(source, bytes):
        try:
            source = source.decode("utf-8")
        except UnicodeDecodeError as exc:
            return {
                "entities": entities,
                "diagnostics": [
                    _diagnostic(
                        "invalid_encoding",
                        "error",
                        f"FtM entity stream is not valid UTF-8: {exc.reason}",
                    )
                ],
                "invalid_count": 1,
                "diagnostic_count": 1,
            }
    if isinstance(source, (str, TextIOBase)):
        lines = source.splitlines() if isinstance(source, str) else source
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                invalid_count += 1
                record_diagnostics(
                    [
                        _diagnostic(
                            "invalid_json",
                            "error",
                            f"line {line_number} is not valid JSON: {exc.msg}",
                            line_number=line_number,
                        )
                    ]
                )
                continue
            item = _validate_raw_entity(raw, line_number=line_number)
            if item["entity"] is not None:
                record_entity(item["entity"])
            else:
                invalid_count += 1
                record_diagnostics(item["diagnostics"])
        return {
            "entities": entities,
            "diagnostics": diagnostics,
            "invalid_count": invalid_count,
            "diagnostic_count": diagnostic_count,
        }

    if isinstance(source, Mapping):
        iterable: Iterable[Any] = [source]
    else:
        iterable = source
    entity_index = 0
    try:
        for entity_index, raw in enumerate(iterable, start=1):
            item = _validate_raw_entity(raw, entity_index=entity_index)
            if item["entity"] is not None:
                record_entity(item["entity"])
            else:
                invalid_count += 1
                record_diagnostics(item["diagnostics"])
    except FollowTheMoneyImportLimitError:
        raise
    except Exception as exc:  # noqa: BLE001 - preserve planner diagnostics.
        invalid_count += 1
        record_diagnostics(
            [
                _diagnostic(
                    "stream_read_failed",
                    "error",
                    f"FtM entity stream could not be read: {exc}",
                    entity_index=entity_index + 1,
                )
            ]
        )
    return {
        "entities": entities,
        "diagnostics": diagnostics,
        "invalid_count": invalid_count,
        "diagnostic_count": diagnostic_count,
    }


def _json_normalized_raw(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    try:
        return _json_safe(dict(raw))
    except TypeError:
        return None


def _json_normalized_entity(entity: Mapping[str, Any]) -> dict[str, Any] | None:
    try:
        return _json_safe(dict(entity))
    except TypeError:
        return None


def _validate_raw_entity(
    raw: Any,
    *,
    line_number: int | None = None,
    entity_index: int | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {
            "entity": None,
            "diagnostics": [
                _diagnostic(
                    "invalid_entity",
                    "error",
                    "FtM stream item is not a JSON object",
                    line_number=line_number,
                    entity_index=entity_index,
                )
            ],
        }
    raw_dict = _json_normalized_raw(raw)
    if raw_dict is None:
        return {
            "entity": None,
            "diagnostics": [
                _diagnostic(
                    "non_json_entity",
                    "error",
                    "FtM stream item contains values that cannot be represented as JSON",
                    line_number=line_number,
                    entity_index=entity_index,
                )
            ],
        }
    validation = validate_entity(raw_dict)
    if validation["valid"]:
        entity = _json_normalized_entity(validation["entity"])
        if entity is None:
            return {
                "entity": None,
                "diagnostics": [
                    _diagnostic(
                        "non_json_validated_entity",
                        "error",
                        "validated FtM entity contains values that cannot be represented as JSON",
                        line_number=line_number,
                        entity_index=entity_index,
                    )
                ],
            }
        return {
            "entity": _PlannableEntity(
                raw=raw_dict,
                entity=entity,
                line_number=line_number,
                entity_index=entity_index,
            ),
            "diagnostics": [],
        }
    diagnostics = [
        {
            **item,
            **{
                key: value
                for key, value in {
                    "line_number": line_number,
                    "entity_index": entity_index,
                }.items()
                if value is not None
            },
        }
        for item in validation["diagnostics"]
    ]
    return {"entity": None, "diagnostics": diagnostics}


def _build_sheet(
    schema_name: str,
    items: list[_PlannableEntity],
    *,
    kind: str,
    supported: bool,
    dataset_name: str | None,
) -> FtmImportSheetPlan:
    metadata = get_schema_metadata(schema_name)
    source_property, target_property = _relationship_properties(schema_name, metadata)
    columns = _columns_for_schema(schema_name, items, metadata)
    rows = [
        _row_for_entity(
            item,
            columns=columns,
            source_property=source_property,
            target_property=target_property,
            dataset_name=dataset_name,
        )
        for item in sorted(items, key=_entity_sort_key)
    ]
    return FtmImportSheetPlan(
        schema=schema_name,
        kind=kind,
        sheet_name=_sheet_name(schema_name, kind=kind, metadata=metadata),
        supported=supported,
        columns=columns,
        rows=rows,
        source_property=source_property if kind == "relationship" else None,
        target_property=target_property if kind == "relationship" else None,
    )


def _columns_for_schema(
    schema_name: str,
    items: list[_PlannableEntity],
    metadata: Mapping[str, Any] | None,
) -> list[FtmImportColumnPlan]:
    observed = {
        prop_name
        for item in items
        for prop_name in (item.entity.get("properties") or {})
        if isinstance(prop_name, str)
    }
    columns: list[FtmImportColumnPlan] = []
    emitted: set[str] = set()
    if metadata is not None:
        for prop in metadata.get("properties") or []:
            if not isinstance(prop, Mapping):
                continue
            prop_name = prop.get("name")
            if not isinstance(prop_name, str) or not prop_name:
                continue
            if prop_name not in observed or prop.get("hidden"):
                continue
            columns.append(
                FtmImportColumnPlan(
                    name=prop_name,
                    type=_column_type_for_ftm_type(prop.get("type")),
                    ftm_property=prop_name,
                    ftm_type=prop.get("type"),
                    label=prop.get("label"),
                )
            )
            emitted.add(prop_name)
    schema = model.get(schema_name)
    for prop_name in sorted(observed - emitted):
        prop = schema.get(prop_name) if schema is not None else None
        columns.append(
            FtmImportColumnPlan(
                name=prop_name,
                type=_column_type_for_ftm_type(
                    prop.type.name if prop is not None else None
                ),
                ftm_property=prop_name,
                ftm_type=prop.type.name if prop is not None else None,
                label=prop.label if prop is not None else None,
            )
        )
    columns.extend(
        FtmImportColumnPlan(
            name=name,
            type=_TECHNICAL_COLUMN_TYPES[name],
            hidden=True,
        )
        for name in TECHNICAL_IMPORT_COLUMNS
    )
    return columns


def _row_for_entity(
    item: _PlannableEntity,
    *,
    columns: list[FtmImportColumnPlan],
    source_property: str | None,
    target_property: str | None,
    dataset_name: str | None,
) -> FtmImportRowPlan:
    entity = item.entity
    properties = entity.get("properties") or {}
    values: dict[str, Any] = {}
    for column in columns:
        if column.hidden or column.ftm_property is None:
            continue
        values[column.name] = _visible_property_value(properties.get(column.name))
    source_refs = _source_refs(item.raw, entity)
    values.update(
        {
            "_ftm_id": entity["id"],
            "_ftm_schema": entity["schema"],
            "_ftm_caption": _caption(entity),
            "_ftm_properties_json": _json_safe(properties),
            "_ftm_source_refs_json": source_refs,
            "_ftm_raw_json": _json_safe(item.raw),
            "_ftm_dataset": _dataset_metadata(item.raw, entity, dataset_name),
        }
    )
    return FtmImportRowPlan(
        entity_id=str(entity["id"]),
        schema=str(entity["schema"]),
        values=values,
        source_ftm_id=_first_property_value(properties, source_property),
        target_ftm_id=_first_property_value(properties, target_property),
    )


def _visible_property_value(value: Any) -> Any:
    """Expose the first visible value; full FtM arrays stay in properties JSON."""

    if isinstance(value, list):
        if not value:
            return None
        return _json_safe(value[0])
    return _json_safe(value)


def _first_property_value(
    properties: Mapping[str, Any], property_name: str | None
) -> str | None:
    if property_name is None:
        return None
    value = _visible_property_value(properties.get(property_name))
    if value is None:
        return None
    return str(value)


def _caption(entity: Mapping[str, Any]) -> str | None:
    caption = entity.get("caption")
    if isinstance(caption, str) and caption.strip():
        return caption
    try:
        proxy = model.get_proxy(dict(entity))
        proxy_caption = proxy.caption
    except Exception:  # noqa: BLE001 - caption is non-critical plan metadata
        return None
    return proxy_caption or None


def _source_refs(raw: Mapping[str, Any], entity: Mapping[str, Any]) -> Any:
    for item in (raw, entity):
        for key in (
            "source_refs",
            "sourceRefs",
            "source_refs_json",
            "_ftm_source_refs_json",
        ):
            if key in item:
                return _json_safe(item[key])
    return []


def _dataset_metadata(
    raw: Mapping[str, Any],
    entity: Mapping[str, Any],
    dataset_name: str | None,
) -> Any:
    for item in (raw, entity):
        for key in ("dataset", "datasets", "_ftm_dataset"):
            if key in item:
                return _json_safe(item[key])
    return dataset_name


def _relationship_properties(
    schema_name: str, metadata: Mapping[str, Any] | None
) -> tuple[str | None, str | None]:
    if metadata is not None:
        source_property = metadata.get("source_property")
        target_property = metadata.get("target_property")
        if isinstance(source_property, str) and isinstance(target_property, str):
            return source_property, target_property
    schema = model.get(schema_name)
    if schema is None:
        return None, None
    source_prop = getattr(schema, "source_prop", None)
    target_prop = getattr(schema, "target_prop", None)
    source_name = getattr(source_prop, "name", None)
    target_name = getattr(target_prop, "name", None)
    if isinstance(source_name, str) and isinstance(target_name, str):
        return source_name, target_name
    return None, None


def _is_relationship_schema(
    schema_name: str, metadata: Mapping[str, Any] | None
) -> bool:
    if metadata is not None and metadata.get("edge") is not None:
        return bool(metadata.get("edge"))
    schema = model.get(schema_name)
    return bool(getattr(schema, "edge", False)) if schema is not None else False


def _sheet_name(
    schema_name: str,
    *,
    kind: str,
    metadata: Mapping[str, Any] | None,
) -> str:
    label = metadata.get("plural") if metadata else schema_name
    suffix = "_links" if kind == "relationship" else ""
    if kind == "unsupported":
        suffix = "_raw"
    return f"ftm_{_slug(label or schema_name)}{suffix}"


def _slug(value: str) -> str:
    chars = [char.lower() if char.isalnum() else "_" for char in value]
    return "_".join(part for part in "".join(chars).split("_") if part)


def _column_type_for_ftm_type(ftm_type: Any) -> str:
    if not isinstance(ftm_type, str):
        return "text"
    return _FTM_TYPE_TO_COLUMN_TYPE.get(ftm_type, "text")


def _entity_sort_key(item: _PlannableEntity) -> tuple[str, int, int]:
    return (
        str(item.entity.get("id") or ""),
        item.line_number or 0,
        item.entity_index or 0,
    )


def _diagnostic_sort_key(diagnostic: Mapping[str, Any]) -> tuple[int, int, str]:
    line_number = diagnostic.get("line_number")
    entity_index = diagnostic.get("entity_index")
    position = line_number if isinstance(line_number, int) else 1_000_000
    index = entity_index if isinstance(entity_index, int) else 0
    return (position, index, str(diagnostic.get("code") or ""))


def _bounded_diagnostic(
    diagnostic: dict[str, Any], *, max_chars: int | None
) -> dict[str, Any]:
    if max_chars is None:
        return diagnostic
    return {
        key: value
        if not isinstance(value, str) or len(value) <= max_chars
        else value[: max_chars - 1] + "…"
        for key, value in diagnostic.items()
    }


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return json.loads(json.dumps(value, sort_keys=True))


def _diagnostic(
    code: str,
    severity: str,
    message: str,
    **extra: Any,
) -> dict[str, Any]:
    diagnostic = {
        "code": code,
        "severity": severity,
        "message": message,
    }
    diagnostic.update({key: value for key, value in extra.items() if value is not None})
    return diagnostic
