"""SDK-backed FollowTheMoney schema and property metadata helpers."""

from __future__ import annotations

from typing import Any

from frisket.features.followthemoney._sdk_import import require_followthemoney_sdk

with require_followthemoney_sdk():
    from followthemoney import model

SUPPORTED_SCHEMA_PRESETS = (
    "Person",
    "Company",
    "Organization",
    "Asset",
    "Payment",
    "Membership",
    "CourtCase",
    "Document",
)


def supported_schema_presets() -> list[dict[str, Any]]:
    """Return first-class FtM preset metadata for schemas present in the SDK."""

    return [
        metadata
        for schema_name in SUPPORTED_SCHEMA_PRESETS
        if (metadata := get_schema_metadata(schema_name)) is not None
    ]


def get_schema_metadata(schema_name: str) -> dict[str, Any] | None:
    """Return SDK-derived metadata for a single schema, if supported."""

    schema = model.get(schema_name)
    if schema is None:
        return None
    source_prop = getattr(schema, "source_prop", None)
    target_prop = getattr(schema, "target_prop", None)
    properties = [
        _property_metadata(prop, selected_schema=schema)
        for prop in _sorted_schema_properties(schema)
    ]
    return {
        "name": schema.name,
        "label": schema.label,
        "plural": schema.plural,
        "description": _clean_description(schema.description),
        "required": list(schema.required or []),
        "featured": list(schema.featured or []),
        "caption": list(schema.caption or []),
        "matchable": bool(schema.matchable),
        "hidden": bool(schema.hidden),
        "edge": bool(schema.edge),
        "edge_label": schema.edge_label,
        "source_property": source_prop.name if source_prop is not None else None,
        "target_property": target_prop.name if target_prop is not None else None,
        "extends": sorted(parent.name for parent in schema.extends),
        "properties": properties,
        "properties_by_name": {prop["name"]: prop for prop in properties},
    }


def get_property_metadata(
    schema_name: str, property_name: str
) -> dict[str, Any] | None:
    """Return SDK-derived property metadata for a schema property."""

    schema = model.get(schema_name)
    if schema is None:
        return None
    prop = schema.get(property_name)
    if prop is None:
        return None
    return _property_metadata(prop, selected_schema=schema)


def _sorted_schema_properties(schema: Any) -> list[Any]:
    properties = list(schema.properties.values())
    featured_order = {name: index for index, name in enumerate(schema.featured or [])}
    required = set(schema.required or [])
    caption_order = {name: index for index, name in enumerate(schema.caption or [])}
    return sorted(
        properties,
        key=lambda prop: (
            0 if prop.name in required else 1,
            featured_order.get(prop.name, 10_000),
            caption_order.get(prop.name, 10_000),
            prop.label.lower(),
            prop.name,
        ),
    )


def _property_metadata(prop: Any, *, selected_schema: Any) -> dict[str, Any]:
    data = prop.to_dict()
    required = set(selected_schema.required or [])
    featured = set(selected_schema.featured or [])
    caption = set(selected_schema.caption or [])
    return {
        "name": prop.name,
        "qname": prop.qname,
        "label": prop.label,
        "description": _clean_description(prop.description),
        "type": prop.type.name,
        "hidden": bool(prop.hidden),
        "matchable": bool(prop.matchable),
        "required": prop.name in required,
        "featured": prop.name in featured,
        "caption": prop.name in caption,
        "schema": prop.schema.name,
        "range": data.get("range"),
        "reverse": data.get("reverse"),
        "stub": bool(data.get("stub", False)),
        "deprecated": bool(data.get("deprecated", False)),
        "max_length": data.get("maxLength"),
        "format": data.get("format"),
    }


def _clean_description(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None
