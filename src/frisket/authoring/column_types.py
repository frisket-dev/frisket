"""Pluggable column-type registry.

Column types/presentations are a REGISTRY, not a hardcoded enum: a plugin
registers a content kind (file, object, geo, ...) through
:func:`register_column_type` with its own validation + presentation hints.
The store validates column types against this registry, the server exposes
it over ``GET /api/column-types``, and the frontend resolves grid renderers
from each type's ``presentation.renderer`` instead of a hardcoded switch.

The core types live on the same seam — they are registered below with
``core=True`` — so a plugin type is a first-class citizen, not a bolt-on
beside an enum.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from math import isfinite
from typing import Any, Callable, Iterator

from frisket.calendar_dates import is_valid_calendar_date
from frisket.features.temporal_values import (
    is_valid_temporal_value,
    normalize_temporal_value,
)

__all__ = [
    "ColumnTypeSpec",
    "register_column_type",
    "unregister_column_type",
    "unregister_plugin_column_types",
    "get_column_type",
    "column_types",
    "is_registered",
    "type_names",
    "range_facet_value_kind",
    "parse_value",
    "validate_value",
]


@dataclass(frozen=True)
class ColumnTypeSpec:
    """One registered column type.

    validate
        Optional predicate over a single cell value. ``None`` cell values are
        always allowed (an empty cell has no type). The current-cell projection
        uses it to mark incompatible values invalid without rewriting them;
        recipes/plugins may also use it for their own output checks.
    parse
        Optional converter for newly-entered values. Parsers run before cell
        edit validation, but column.set_type intentionally validates existing
        stored values without parsing so retyping cannot silently rewrite data.
    presentation
        Presentation hints for the frontend; ``renderer`` names the grid
        renderer to resolve (e.g. ``"media"``, ``"map-pin"``). Extra keys ride
        along verbatim (e.g. ``mediaType``, ``align``).
    """

    name: str
    validate: Callable[[Any], bool] | None = None
    parse: Callable[[Any], Any] | None = None
    presentation: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    core: bool = False
    plugin: str = "core"

    def to_public(self) -> dict[str, Any]:
        """Wire shape for /api/column-types (callables don't serialize)."""
        return {
            "name": self.name,
            "core": self.core,
            "plugin": self.plugin,
            "presentation": dict(self.presentation),
            "has_validator": self.validate is not None,
            "has_parser": self.parse is not None,
            "description": self.description,
        }


_lock = threading.Lock()
_registry: dict[str, ColumnTypeSpec] = {}
_RESERVED_COLUMN_TYPES = {"geo_point", "geo_shape"}


def register_column_type(
    name: str,
    *,
    validate: Callable[[Any], bool] | None = None,
    parse: Callable[[Any], Any] | None = None,
    presentation: dict[str, Any] | None = None,
    description: str = "",
    core: bool = False,
    plugin: str | None = None,
) -> ColumnTypeSpec:
    """Register (or re-register) a column type. The public plugin seam.

    Re-registering an existing name replaces it, EXCEPT a non-core
    registration may not shadow a core type — plugins extend the palette,
    they don't redefine `text`.
    """
    if not name or not isinstance(name, str):
        raise ValueError("column type name must be a non-empty string")
    spec = ColumnTypeSpec(
        name=name,
        validate=validate,
        parse=parse,
        presentation=dict(presentation or {}),
        description=description,
        core=core,
        plugin=plugin or ("core" if core else "external"),
    )
    with _lock:
        existing = _registry.get(name)
        if existing is not None and name in _RESERVED_COLUMN_TYPES:
            raise ValueError(f"cannot override reserved column type '{name}'")
        if existing is not None and existing.core and not core:
            raise ValueError(f"cannot override core column type '{name}'")
        _registry[name] = spec
    return spec


def unregister_column_type(name: str) -> None:
    """Remove a plugin type. Core types cannot be unregistered."""
    with _lock:
        spec = _registry.get(name)
        if spec is None:
            return
        if name in _RESERVED_COLUMN_TYPES:
            raise ValueError(f"cannot unregister reserved column type '{name}'")
        if spec.core:
            raise ValueError(f"cannot unregister core column type '{name}'")
        del _registry[name]


def unregister_plugin_column_types(plugin_id: str) -> list[str]:
    """Remove all non-core column types owned by one plugin id."""
    removed: list[str] = []
    with _lock:
        names = [
            name
            for name, spec in _registry.items()
            if spec.plugin == plugin_id and not spec.core
        ]
        for name in names:
            if name in _RESERVED_COLUMN_TYPES:
                continue
            _registry.pop(name, None)
            removed.append(name)
    return sorted(removed)


def get_column_type(name: str) -> ColumnTypeSpec | None:
    with _lock:
        return _registry.get(name)


def column_types() -> list[ColumnTypeSpec]:
    """All registered types, core first, then plugins in registration order."""
    with _lock:
        specs = list(_registry.values())
    return [s for s in specs if s.core] + [s for s in specs if not s.core]


def is_registered(name: str) -> bool:
    with _lock:
        return name in _registry


def type_names() -> set[str]:
    with _lock:
        return set(_registry)


_RANGE_FACET_VALUE_KINDS = frozenset({"number", "integer", "date"})


def range_facet_value_kind(type_name: str) -> str | None:
    """Return the type's closed, end-to-end range-facet capability.

    Preview generation and row filtering both consume this one capability so
    plugin range facets cannot render a distribution the query layer rejects
    (or vice versa). Unknown keys and unsupported value kinds fail closed.
    """
    spec = get_column_type(type_name)
    if spec is None:
        return None
    raw = spec.presentation.get("facet")
    if not isinstance(raw, dict) or set(raw) != {"kind", "valueKind"}:
        return None
    value_kind = raw.get("valueKind")
    if raw.get("kind") != "range" or value_kind not in _RANGE_FACET_VALUE_KINDS:
        return None
    return str(value_kind)


def parse_value(type_name: str, value: Any) -> Any:
    """Return the stored representation for a newly supplied cell value.

    Unknown types and types without parsers are identity functions. Parser
    exceptions are intentionally surfaced to the action layer so it can return
    a structured field error and keep the edit transactional.
    """
    if value is None:
        return None
    with _lock:
        spec = _registry.get(type_name)
    if spec is None or spec.parse is None:
        return value
    return spec.parse(value)


def validate_value(type_name: str, value: Any) -> bool:
    """True iff `value` is acceptable for `type_name`. Empty cells (None)
    always pass; an unknown type or a type without a validator accepts all."""
    if value is None:
        return True
    if value == "" and type_name == "geo_point":
        return True
    with _lock:
        spec = _registry.get(type_name)
    if spec is None or spec.validate is None:
        return True
    try:
        return bool(spec.validate(value))
    except Exception:
        return False


class TypeNamesView:
    """Live, read-only set-like view over the registered type names.

    Kept so `from frisket.engine.store import COLUMN_TYPES` (the pre-registry enum)
    still works — but it now reflects the registry, including plugin types.
    """

    def __contains__(self, name: object) -> bool:
        with _lock:
            return name in _registry

    def __iter__(self) -> Iterator[str]:
        with _lock:
            names = list(_registry)
        return iter(names)

    def __len__(self) -> int:
        with _lock:
            return len(_registry)

    def __repr__(self) -> str:
        with _lock:
            names = sorted(_registry)
        return f"TypeNamesView({names})"


# ---------------------------------------------------------------------------
# Core types — migrated onto the same seam plugins use. Validators are
# intentionally permissive about coercible inputs (the store/recipes coerce);
# they reject only values that can't possibly belong to the type.


def _is_text(v: Any) -> bool:
    return isinstance(v, str)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and isfinite(v)


def _is_integer(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and -(2**63) <= v <= 2**63 - 1


def _is_boolean(v: Any) -> bool:
    return isinstance(v, bool)


def _is_json(v: Any) -> bool:
    # any JSON-representable shape; strings may hold serialized JSON
    if not isinstance(v, (str, int, float, bool, list, dict)):
        return False
    try:
        json.dumps(v, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return False
    return True


def _is_media(v: Any) -> bool:
    # a URL/path string, or the blob envelope {blob, mime, filename}
    if isinstance(v, str):
        return True
    return isinstance(v, dict) and "blob" in v


def _is_geo_point(v: Any) -> bool:
    # The wire shape produced by geocode/to_geo_point and rendered as a map pin.
    if not isinstance(v, dict):
        return False
    lat, lon = v.get("lat"), v.get("lon")
    return (
        isinstance(lat, (int, float))
        and isinstance(lon, (int, float))
        and not isinstance(lat, bool)
        and not isinstance(lon, bool)
        and -90 <= lat <= 90
        and -180 <= lon <= 180
    )


def _is_coordinate(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) >= 2
        and all(
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and isfinite(item)
            for item in value[:2]
        )
    )


def _is_position_list(value: Any, *, minimum: int) -> bool:
    return (
        isinstance(value, list)
        and len(value) >= minimum
        and all(_is_coordinate(item) for item in value)
    )


def _is_linear_ring(value: Any) -> bool:
    # A polygon boundary ring: a non-empty list of positions.
    return _is_position_list(value, minimum=1)


def _is_polygon_coordinates(value: Any) -> bool:
    return (
        isinstance(value, list)
        and value
        and all(_is_linear_ring(item) for item in value)
    )


def _is_geo_shape(v: Any) -> bool:
    # A GeoJSON geometry object (any of the seven RFC 7946 geometry types),
    # accepted by import.geojson / import.kml.
    if not isinstance(v, dict):
        return False
    geometry_type = v.get("type")
    if geometry_type == "GeometryCollection":
        geometries = v.get("geometries")
        return isinstance(geometries, list) and all(
            _is_geo_shape(item) for item in geometries
        )
    coordinates = v.get("coordinates")
    if geometry_type == "Point":
        return _is_coordinate(coordinates)
    if geometry_type == "LineString":
        return _is_position_list(coordinates, minimum=2)
    if geometry_type == "MultiPoint":
        return _is_position_list(coordinates, minimum=1)
    if geometry_type == "Polygon":
        return _is_polygon_coordinates(coordinates)
    if geometry_type == "MultiLineString":
        return (
            isinstance(coordinates, list)
            and coordinates
            and all(_is_position_list(item, minimum=2) for item in coordinates)
        )
    if geometry_type == "MultiPolygon":
        return (
            isinstance(coordinates, list)
            and coordinates
            and all(_is_polygon_coordinates(item) for item in coordinates)
        )
    return False


def _is_date(v: Any) -> bool:
    return is_valid_calendar_date(v)


def _temporal_validator(type_name: str) -> Callable[[Any], bool]:
    return lambda value: is_valid_temporal_value(type_name, value)


def _temporal_parser(type_name: str) -> Callable[[Any], dict[str, Any]]:
    return lambda value: normalize_temporal_value(type_name, value)


register_column_type(
    "text",
    validate=_is_text,
    presentation={
        "renderer": "text",
        "facet": {"kind": "categorical", "operator": "eq"},
    },
    description="Plain text; format='markdown' renders markdown.",
    core=True,
)
register_column_type(
    "timestamped_transcript",
    validate=_is_text,
    presentation={"renderer": "text", "userSelectable": False},
    description=(
        "Transcript text backed by host-written, source-bound temporal evidence."
    ),
    core=True,
)
register_column_type(
    "number",
    validate=_is_number,
    presentation={
        "renderer": "number",
        "align": "right",
        "facet": {"kind": "range", "valueKind": "number"},
    },
    description="Floating-point number.",
    core=True,
)
register_column_type(
    "integer",
    validate=_is_integer,
    presentation={
        "renderer": "integer",
        "align": "right",
        "facet": {"kind": "range", "valueKind": "integer"},
    },
    description="Whole number; format='filesize' renders bytes.",
    core=True,
)
register_column_type(
    "boolean",
    validate=_is_boolean,
    presentation={
        "renderer": "boolean",
        "facet": {"kind": "categorical", "operator": "eq"},
    },
    description="True/false checkbox.",
    core=True,
)
register_column_type(
    "category",
    validate=_is_text,
    presentation={
        "renderer": "category",
        "facet": {
            "kind": "categorical",
            "preferred": True,
            "oneClick": True,
            "operator": "eq",
        },
    },
    description=(
        "One label from a small set; rendered as a clickable exact-value facet."
    ),
    core=True,
)
register_column_type(
    "json",
    validate=_is_json,
    presentation={
        "renderer": "json",
        "facet": {"kind": "collection", "operator": "list_contains_any"},
    },
    description="Structured value; supported arrays can be filtered by contained values.",
    core=True,
)
register_column_type(
    "date",
    validate=_is_date,
    presentation={
        "renderer": "text",
        "facet": {"kind": "range", "valueKind": "date"},
    },
    description="ISO-8601 date or datetime string.",
    core=True,
)
register_column_type(
    "image",
    validate=_is_media,
    presentation={"renderer": "image"},
    description="Image URL or blob envelope; thumbnail in the grid.",
    core=True,
)
register_column_type(
    "audio",
    validate=_is_media,
    presentation={"renderer": "media", "mediaType": "audio"},
    description="Audio URL or blob envelope; badge + player drawer.",
    core=True,
)
register_column_type(
    "video",
    validate=_is_media,
    presentation={"renderer": "media", "mediaType": "video"},
    description="Video URL or blob envelope; badge + player drawer.",
    core=True,
)
register_column_type(
    "file",
    validate=_is_media,
    presentation={"renderer": "media", "mediaType": "file"},
    description="Arbitrary file URL or blob envelope.",
    core=True,
)
register_column_type(
    "link",
    validate=_is_text,
    presentation={
        "renderer": "link",
        "facet": {"kind": "categorical", "operator": "eq"},
    },
    description="Clickable URL.",
    core=True,
)
register_column_type(
    "timeline_point",
    validate=_temporal_validator("timeline_point"),
    parse=_temporal_parser("timeline_point"),
    presentation={
        "renderer": "timeline",
        "geometry": "point",
        "multiple": False,
        "label": "Timestamp",
        "userSelectable": False,
    },
    description="One source-bound timestamp on an artifact-local timeline.",
    core=True,
)
register_column_type(
    "timeline_points",
    validate=_temporal_validator("timeline_points"),
    parse=_temporal_parser("timeline_points"),
    presentation={
        "renderer": "timeline",
        "geometry": "point",
        "multiple": True,
        "label": "Timestamps",
        "userSelectable": False,
    },
    description="An ordered collection of source-bound timestamps.",
    core=True,
)
register_column_type(
    "timeline_range",
    validate=_temporal_validator("timeline_range"),
    parse=_temporal_parser("timeline_range"),
    presentation={
        "renderer": "timeline",
        "geometry": "range",
        "multiple": False,
        "label": "Time range",
        "userSelectable": False,
    },
    description="One source-bound half-open time range.",
    core=True,
)
register_column_type(
    "timeline_ranges",
    validate=_temporal_validator("timeline_ranges"),
    parse=_temporal_parser("timeline_ranges"),
    presentation={
        "renderer": "timeline",
        "geometry": "range",
        "multiple": True,
        "label": "Time ranges",
        "userSelectable": False,
    },
    description="An ordered collection of source-bound half-open time ranges.",
    core=True,
)

# Eager semantic extension, kept non-core to preserve the plugin-seam facts
# used by the geocode/plugin workflow while making validation deterministic.
register_column_type(
    "geo_point",
    validate=_is_geo_point,
    presentation={"renderer": "map-pin"},
    description="A {lat, lon} point; rendered as a map pin.",
)
register_column_type(
    "geo_shape",
    validate=_is_geo_shape,
    presentation={"renderer": "map-overlay"},
    description="A GeoJSON geometry (any of the seven RFC 7946 geometry types).",
)
