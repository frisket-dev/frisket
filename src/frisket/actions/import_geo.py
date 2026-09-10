"""Declared-schema GeoJSON and KML/KMZ imports over admitted local bytes."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import Field, model_validator

from frisket.actions.core import ActionCategory, action, create_sheet
from frisket.actions.imports import FileSource
from frisket.actions.types import (
    ActionParams,
    DynamicOutput,
    LocalFileReader,
    TableColumn,
    TableError,
    TableResult,
    TableRow,
)
from frisket.authoring import column_types
from frisket.contracts.action import MAX_IMPORT_ROWS_COLUMNS
from frisket.contracts.actions.schemas.imports import (
    ValidatedImportColumns,
    normalize_import_value,
)


class ImportGeojsonParams(ActionParams):
    source: FileSource
    geometry_column: str = Field(default="geometry", min_length=1)
    property_columns: ValidatedImportColumns = Field(
        default_factory=list, max_length=MAX_IMPORT_ROWS_COLUMNS
    )
    include_feature_id: bool = False
    strict_properties: bool = False
    on_invalid_geometry: Literal["reject", "skip"] = "reject"

    @model_validator(mode="after")
    def _projected_columns(self) -> ImportGeojsonParams:
        names = [column.name for column in self.property_columns]
        names.append(self.geometry_column)
        if self.include_feature_id:
            names.append("feature_id")
        if len(names) != len(set(names)):
            raise ValueError("duplicate_column_name")
        return self


class ImportKmlParams(ActionParams):
    source: FileSource
    geometry_column: str = Field(default="geometry", min_length=1)
    property_columns: ValidatedImportColumns = Field(
        default_factory=list, max_length=MAX_IMPORT_ROWS_COLUMNS
    )
    include_name: bool = True
    include_description: bool = True
    strict_properties: bool = False
    on_invalid_geometry: Literal["reject", "skip"] = "reject"

    @model_validator(mode="after")
    def _projected_columns(self) -> ImportKmlParams:
        names = [column.name for column in self.property_columns]
        names.append(self.geometry_column)
        if self.include_name:
            names.append("name")
        if self.include_description:
            names.append("description")
        if len(names) != len(set(names)):
            raise ValueError("duplicate_column_name")
        return self


def _columns(params: ImportGeojsonParams | ImportKmlParams) -> tuple[TableColumn, ...]:
    columns = []
    if isinstance(params, ImportGeojsonParams):
        if params.include_feature_id:
            columns.append(TableColumn(key="feature_id", type="text"))
    else:
        if params.include_name:
            columns.append(TableColumn(key="name", type="text"))
        if params.include_description:
            columns.append(TableColumn(key="description", type="text"))
    columns.extend(
        TableColumn(
            key=column.name,
            type=column.type,
            format=column.format,
            hidden=column.hidden,
        )
        for column in params.property_columns
    )
    columns.append(TableColumn(key=params.geometry_column, type="geo_shape"))
    return tuple(columns)


def _geometry(
    value: Any,
    *,
    index: int,
    format_name: str,
    on_invalid: str,
    skipped: list[int],
) -> Any:
    if column_types.validate_value("geo_shape", value):
        return value
    if on_invalid == "skip":
        skipped.append(index)
        return None
    raise TableError(
        f"unsupported_{format_name}_geometry",
        "Feature geometry must map to a valid GeoJSON geometry",
        details={
            "feature": index,
            "geometry_type": value.get("type") if isinstance(value, dict) else None,
        },
    )


def _properties(
    properties: dict[str, Any],
    params: ImportGeojsonParams | ImportKmlParams,
    *,
    index: int,
    format_name: str,
) -> dict[str, Any]:
    extra = sorted(
        set(properties) - {column.name for column in params.property_columns}
    )
    if params.strict_properties and extra:
        raise TableError(
            "row_shape_mismatch",
            "Feature properties include undeclared keys",
            details={"feature": index, "extra": extra},
        )
    row = {}
    for column in params.property_columns:
        try:
            row[column.name] = normalize_import_value(
                column.type, properties.get(column.name)
            )
        except ValueError as exc:
            raise TableError(
                f"invalid_{format_name}_value",
                "Feature property does not match the declared column type",
                details={
                    "feature": index,
                    "property": column.name,
                    "type": column.type,
                },
            ) from exc
    return row


def _result(
    rows: list[TableRow[DynamicOutput]],
    source: FileSource,
    importer: str,
    skipped: list[int],
) -> TableResult[DynamicOutput]:
    if any(type(index) is not int or not 0 <= index < len(rows) for index in skipped):
        raise ValueError("skipped_features must contain zero-based row indices")
    return TableResult(
        rows=rows,
        source={
            "kind": "file",
            "label": source.label,
            "importer": importer,
            "skipped_features": skipped,
        },
    )


_GEOMETRY_TYPES = frozenset(
    {
        "Point",
        "LineString",
        "Polygon",
        "MultiPoint",
        "MultiLineString",
        "MultiPolygon",
        "GeometryCollection",
    }
)


def import_geojson(
    params: ImportGeojsonParams, files: LocalFileReader
) -> TableResult[DynamicOutput]:
    raw = files.read_bytes(params.source.path)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TableError(
            "geojson_parse_failed", "GeoJSON file could not be parsed"
        ) from exc
    data_type = data.get("type") if isinstance(data, dict) else None
    if data_type == "FeatureCollection":
        features = data.get("features")
        if not isinstance(features, list):
            raise TableError(
                "geojson_parse_failed",
                "GeoJSON FeatureCollection must contain a features array",
            )
    elif data_type == "Feature":
        features = [data]
    elif isinstance(data_type, str) and data_type in _GEOMETRY_TYPES:
        features = [{"type": "Feature", "geometry": data}]
    else:
        raise TableError(
            "geojson_parse_failed",
            "GeoJSON source must be a FeatureCollection, Feature, or Geometry",
        )
    rows = []
    skipped: list[int] = []
    for index, feature in enumerate(features):
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise TableError(
                "geojson_parse_failed",
                "GeoJSON features must be Feature objects",
                details={"feature": index},
            )
        geometry = _geometry(
            feature.get("geometry"),
            index=index,
            format_name="geojson",
            on_invalid=params.on_invalid_geometry,
            skipped=skipped,
        )
        properties = feature.get("properties")
        if properties is None:
            properties = {}
        if not isinstance(properties, dict):
            raise TableError(
                "geojson_parse_failed",
                "GeoJSON feature properties must be an object or null",
                details={"feature": index},
            )
        row = _properties(properties, params, index=index, format_name="geojson")
        if params.include_feature_id:
            feature_id = feature.get("id")
            row["feature_id"] = None if feature_id is None else str(feature_id)
        row[params.geometry_column] = geometry
        rows.append(TableRow(output=DynamicOutput(row)))
    return _result(rows, params.source, "geojson-featurecollection", skipped)


def import_kml(
    params: ImportKmlParams, files: LocalFileReader
) -> TableResult[DynamicOutput]:
    from frisket.features.geometry.kml import KmlParseError, read_placemarks

    raw = files.read_bytes(params.source.path)
    try:
        placemarks = read_placemarks(raw)
    except KmlParseError as exc:
        raise TableError(
            "kml_parse_failed",
            "KML/KMZ file could not be parsed",
            details={"error": str(exc)},
        ) from exc
    rows = []
    skipped: list[int] = []
    for index, placemark in enumerate(placemarks):
        geometry = _geometry(
            placemark.geometry,
            index=index,
            format_name="kml",
            on_invalid=params.on_invalid_geometry,
            skipped=skipped,
        )
        row = _properties(
            placemark.extended_data, params, index=index, format_name="kml"
        )
        if params.include_name:
            row["name"] = placemark.name
        if params.include_description:
            row["description"] = placemark.description
        row[params.geometry_column] = geometry
        rows.append(TableRow(output=DynamicOutput(row)))
    return _result(rows, params.source, "kml-placemarks", skipped)


GEOJSON = action(
    examples=(
        ImportGeojsonParams(
            source=FileSource(kind="file", path="examples/places.geojson"),
            property_columns=[{"name": "name", "type": "text"}],
            include_feature_id=True,
        ),
    ),
    name="geojson",
    title="Import GeoJSON",
    description="Import GeoJSON features and geometries with selected typed properties.",
    category=ActionCategory.CONVERT,
    run=create_sheet(import_geojson, columns_from=_columns),
)

KML = action(
    examples=(
        ImportKmlParams(source=FileSource(kind="file", path="examples/places.kml")),
    ),
    name="kml",
    title="Import KML",
    description="Import KML or KMZ placemarks with selected typed properties.",
    category=ActionCategory.CONVERT,
    run=create_sheet(import_kml, columns_from=_columns),
)
