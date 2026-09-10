from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest
from pydantic import ValidationError

from frisket.actions.import_geo import (
    ImportGeojsonParams,
    ImportKmlParams,
    _result,
    import_geojson,
    import_kml,
)
from frisket.actions.imports import FileSource
from frisket.actions.types import DynamicOutput, TableError, TableRow
from frisket.authoring.column_types import register_column_type, unregister_column_type
from frisket.engine.executor import run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore


class MemoryFiles:
    def __init__(self, raw):
        self.raw = raw
        self.reads = []

    def read_bytes(self, path):
        self.reads.append(path)
        return self.raw


def _params(format_name="geojson", **overrides):
    model = ImportGeojsonParams if format_name == "geojson" else ImportKmlParams
    return model.model_validate(
        {"source": {"kind": "file", "path": "input", "label": "places"}, **overrides}
    )


def _feature(geometry=None, properties=None, **extra):
    return {"type": "Feature", "geometry": geometry, "properties": properties, **extra}


def _kml(placemarks):
    return f'<kml xmlns="http://www.opengis.net/kml/2.2"><Document>{placemarks}</Document></kml>'.encode()


def _request(path, format_name="geojson", **params):
    return {
        "action_id": f"import.{format_name}",
        "scope": {"kind": "project"},
        "sheet_name": "Places",
        "idempotency_key": "places-once",
        "params": {
            "source": {"kind": "file", "path": str(path), "label": "places"},
            **params,
        },
    }


POINT = {"type": "Point", "coordinates": [1, 2]}
LINE = {"type": "LineString", "coordinates": [[1, 2], [2, 3]]}
RING = [[0, 0], [3, 0], [3, 3], [0, 0]]


@pytest.mark.parametrize(
    "geometry",
    [
        POINT,
        LINE,
        {"type": "MultiPoint", "coordinates": [[1, 2], [2, 3]]},
        {"type": "MultiLineString", "coordinates": [LINE["coordinates"]]},
        {"type": "Polygon", "coordinates": [RING]},
        {"type": "MultiPolygon", "coordinates": [[RING]]},
        {"type": "GeometryCollection", "geometries": [POINT, LINE]},
    ],
)
def test_all_bare_geojson_geometries_are_materialized_with_missing_properties(geometry):
    files = MemoryFiles(json.dumps(geometry).encode())
    result = import_geojson(
        _params(
            property_columns=[{"name": "absent", "type": "text"}],
            include_feature_id=True,
        ),
        files,
    )
    assert isinstance(result.rows, list)
    assert [row.output.root for row in result.rows] == [
        {"feature_id": None, "absent": None, "geometry": geometry}
    ]
    assert files.reads == ["input"]
    assert dict(result.source) == {
        "kind": "file",
        "label": "places",
        "importer": "geojson-featurecollection",
        "skipped_features": [],
    }


@pytest.mark.parametrize("properties", [False, 0, "", [], ["bad"]])
def test_geojson_refuses_non_object_properties_even_when_falsy(properties):
    with pytest.raises(TableError) as error:
        import_geojson(
            _params(), MemoryFiles(json.dumps(_feature(POINT, properties)).encode())
        )
    assert error.value.code == "geojson_parse_failed"


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b"\xff",
        b"[]",
        b'{"type": []}',
        b'{"type":"FeatureCollection","features":{}}',
        b'{"type":"FeatureCollection","features":[null]}',
    ],
)
def test_geojson_malformed_documents_have_parse_diagnostics(raw):
    with pytest.raises(TableError) as error:
        import_geojson(_params(on_invalid_geometry="skip"), MemoryFiles(raw))
    assert error.value.code == "geojson_parse_failed"


@pytest.mark.parametrize("format_name", ["geojson", "kml"])
def test_strict_properties_and_invalid_property_types_have_diagnostics(format_name):
    raw = (
        json.dumps(_feature(POINT, {"count": "wrong", "extra": "ignored"})).encode()
        if format_name == "geojson"
        else _kml(
            '<Placemark><ExtendedData><Data name="count"><value>wrong</value></Data><Data name="extra"><value>ignored</value></Data></ExtendedData></Placemark>'
        )
    )
    producer = import_geojson if format_name == "geojson" else import_kml
    for strict, code in [
        (True, "row_shape_mismatch"),
        (False, f"invalid_{format_name}_value"),
    ]:
        with pytest.raises(TableError) as error:
            producer(
                _params(
                    format_name,
                    property_columns=[{"name": "count", "type": "integer"}],
                    strict_properties=strict,
                ),
                MemoryFiles(raw),
            )
        assert error.value.code == code
        assert error.value.details["feature"] == 0


@pytest.mark.parametrize(
    ("format_name", "options"),
    [
        ("geojson", {"include_feature_id": True, "geometry_column": "feature_id"}),
        (
            "geojson",
            {
                "include_feature_id": True,
                "property_columns": [{"name": "feature_id", "type": "text"}],
            },
        ),
        ("geojson", {"property_columns": [{"name": "geometry", "type": "text"}]}),
        ("kml", {"geometry_column": "name"}),
        ("kml", {"geometry_column": "description"}),
        ("kml", {"property_columns": [{"name": "name", "type": "text"}]}),
        ("kml", {"property_columns": [{"name": "description", "type": "text"}]}),
    ],
)
def test_auto_added_and_property_columns_cannot_collide(format_name, options):
    with pytest.raises(ValidationError, match="duplicate_column_name"):
        _params(format_name, **options)


@pytest.mark.parametrize("indices", [[True], [False], [-1], [1], [0.0]])
def test_quarantine_metadata_requires_non_boolean_in_bounds_integers(indices):
    with pytest.raises(ValueError, match="zero-based"):
        _result(
            [TableRow(output=DynamicOutput({"geometry": None}))],
            FileSource(kind="file", path="input"),
            "geojson-featurecollection",
            indices,
        )


@pytest.mark.parametrize("format_name", ["geojson", "kml"])
def test_public_import_normalizes_once_and_preserves_renamed_descriptors(
    tmp_path, format_name
):
    calls = []

    def parse(value):
        calls.append(value)
        if not isinstance(value, str) or value.endswith("!"):
            raise ValueError("already parsed")
        return value + "!"

    register_column_type(
        "geo_once",
        parse=parse,
        validate=lambda value: isinstance(value, str) and value.endswith("!"),
    )
    project = Project.create(tmp_path / "project")
    try:
        raw = (
            json.dumps(_feature(POINT, {"title": "Place"}, id=12)).encode()
            if format_name == "geojson"
            else _kml(
                '<Placemark><name>Site</name><description>Details</description><ExtendedData><Data name="title"><value>Place</value></Data></ExtendedData><Point><coordinates>1,2,40</coordinates></Point></Placemark>'
            )
        )
        path = tmp_path / f"input.{format_name}"
        path.write_bytes(raw)
        request = _request(
            path,
            format_name,
            property_columns=[
                {
                    "name": "title",
                    "type": "geo_once",
                    "hidden": True,
                    "format": "markdown",
                }
            ],
        )
        request["output_names"] = {"geometry": "Map", "title": "Headline"}
        if format_name == "geojson":
            request["params"]["include_feature_id"] = True
            request["output_names"]["feature_id"] = "Identifier"
        else:
            request["output_names"].update(name="Name", description="Description")
        result = run_action_spec(project, request, project_id="p")
        assert result.status == "completed", result.errors
        assert calls == ["Place"]
        sheet = result.outputs[0].ref
        assert list(
            project.get_values(sheet["sheet_id"], sheet["columns"]["Map"]).values()
        ) == [POINT]
        assert list(
            project.get_values(sheet["sheet_id"], sheet["columns"]["Headline"]).values()
        ) == ["Place!"]
        column = project.db.execute(
            "SELECT * FROM columns WHERE id=?", (sheet["columns"]["Headline"],)
        ).fetchone()
        assert column["hidden"] == 1
        assert column["format"] == "markdown"
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        fact = next(
            item.ref for item in receipt.inputs if item.ref["kind"] == "local_file_read"
        )
        assert fact == {
            "kind": "local_file_read",
            "path": str(path),
            "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "byte_count": len(raw),
        }
        path.unlink()
        replay = run_action_spec(project, request, project_id="p")
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == result.receipt_id
        assert calls == ["Place"]
        changed = {
            **request,
            "output_names": {**request["output_names"], "geometry": "Changed"},
        }
        conflict = run_action_spec(project, changed, project_id="p")
        assert conflict.status == "failed"
        assert conflict.errors[0].code == "idempotency_conflict"
    finally:
        project.close()
        unregister_column_type("geo_once")


@pytest.mark.parametrize("format_name", ["geojson", "kml"])
def test_late_geometry_rejection_publishes_nothing_and_skip_retains_properties(
    tmp_path, format_name
):
    path = tmp_path / f"input.{format_name}"
    raw = (
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    _feature(POINT, {"title": "Good"}),
                    _feature({"type": "Circle"}, {"title": "Bad"}),
                    _feature(None, None),
                ],
            }
        ).encode()
        if format_name == "geojson"
        else _kml(
            '<Placemark><ExtendedData><Data name="title"><value>Good</value></Data></ExtendedData><Point><coordinates>1,2</coordinates></Point></Placemark><Placemark><ExtendedData><Data name="title"><value>Bad</value></Data></ExtendedData><LineString><coordinates>1,2</coordinates></LineString></Placemark><Placemark/>'
        )
    )
    path.write_bytes(raw)
    project = Project.create(tmp_path / "project")
    try:
        request = _request(
            path, format_name, property_columns=[{"name": "title", "type": "text"}]
        )
        rejected = run_action_spec(project, request, project_id="p")
        assert rejected.status == "failed"
        assert rejected.errors[0].code == f"unsupported_{format_name}_geometry"
        for table in ("sheets", "columns", "rows", "ops", "receipts"):
            assert (
                project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            )
        request["params"]["on_invalid_geometry"] = "skip"
        imported = run_action_spec(project, request, project_id="p")
        assert imported.status == "completed", imported.errors
        sheet = imported.outputs[0].ref
        assert sheet["row_count"] == 3
        assert list(
            project.get_values(sheet["sheet_id"], sheet["columns"]["geometry"]).values()
        ) == [POINT, None, None]
        assert list(
            project.get_values(sheet["sheet_id"], sheet["columns"]["title"]).values()
        ) == ["Good", "Bad", None]
        receipt = ReceiptStore(project).parsed_by_id(imported.receipt_id)
        source = next(
            item.ref for item in receipt.inputs if "skipped_features" in item.ref
        )
        assert source["skipped_features"] == [1]
        assert source["label"] == "places"
        assert source["fingerprint"] == "sha256:" + hashlib.sha256(raw).hexdigest()
        path.unlink()
        replay = run_action_spec(project, request, project_id="p")
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == imported.receipt_id
        assert ReceiptStore(project).parsed_by_id(replay.receipt_id) == receipt
    finally:
        project.close()


@pytest.mark.parametrize(
    "raw",
    [
        b"<invalid",
        _kml("<Placemark><Point><coordinates>x,2</coordinates></Point></Placemark>"),
        _kml("<Placemark><Point><coordinates>123</coordinates></Point></Placemark>"),
        b"PKbroken",
    ],
)
def test_kml_skip_does_not_swallow_xml_coordinate_or_archive_parse_failures(raw):
    with pytest.raises(TableError) as error:
        import_kml(_params("kml", on_invalid_geometry="skip"), MemoryFiles(raw))
    assert error.value.code == "kml_parse_failed"


@pytest.mark.parametrize("canonical_entry", [False, True])
def test_kmz_entry_selection_polygon_holes_and_mixed_geometry(canonical_entry):
    polygon = "<Polygon><outerBoundaryIs><LinearRing><coordinates>0,0,9 3,0,9 3,3,9 0,0,9</coordinates></LinearRing></outerBoundaryIs><innerBoundaryIs><LinearRing><coordinates>1,1,9 2,1,9 2,2,9 1,1,9</coordinates></LinearRing></innerBoundaryIs></Polygon>"
    raw = _kml(
        f"<Placemark><MultiGeometry>{polygon}<Point><coordinates>1,2,9</coordinates></Point></MultiGeometry></Placemark>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "ignored")
        if canonical_entry:
            archive.writestr("first.kml", _kml(""))
        archive.writestr("doc.kml" if canonical_entry else "folder/features.KML", raw)
    result = import_kml(_params("kml"), MemoryFiles(buffer.getvalue()))
    assert isinstance(result.rows, list)
    row = result.rows[0].output.root
    assert row["name"] is None and row["description"] is None
    geometry = row["geometry"]
    assert geometry["type"] == "GeometryCollection"
    assert geometry["geometries"][0] == {
        "type": "Polygon",
        "coordinates": [RING, [[1, 1], [2, 1], [2, 2], [1, 1]]],
    }
    assert geometry["geometries"][1] == POINT
