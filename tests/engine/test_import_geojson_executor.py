from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from executor_harness import CatalogEntry, ExecutorCase, Gate, case_env
from frisket.engine.store import Project


def _write_geojson(dir_path: Path, name: str, body: dict[str, Any]) -> Path:
    path = dir_path / name
    path.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")
    return path


def _feature_collection() -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": "neighborhood-1",
                "properties": {
                    "name": "Civic Center",
                    "district_id": "D1",
                    "population": 12500,
                    "ignored": "not projected",
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [-73.99, 40.75],
                            [-73.98, 40.75],
                            [-73.98, 40.76],
                            [-73.99, 40.76],
                            [-73.99, 40.75],
                        ]
                    ],
                },
            },
            {
                "type": "Feature",
                "id": 202,
                "properties": {
                    "name": "Waterfront",
                    "district_id": "D2",
                    "population": 8700,
                },
                "geometry": {
                    "type": "MultiPolygon",
                    "coordinates": [
                        [
                            [
                                [-73.97, 40.72],
                                [-73.96, 40.72],
                                [-73.96, 40.73],
                                [-73.97, 40.73],
                                [-73.97, 40.72],
                            ]
                        ]
                    ],
                },
            },
        ],
    }


def _import_geojson_action(
    source_path: Path,
    *,
    sheet_name: str = "Neighborhoods",
    idempotency_key: str | None = "import_geojson@sha256:stable",
    strict_properties: bool = False,
) -> dict[str, Any]:
    return {
        "action_id": "import.geojson",
        "scope": {"kind": "project"},
        "sheet_name": sheet_name,
        "params": {
            "source": {
                "kind": "file",
                "path": str(source_path),
                "label": "neighborhoods.geojson",
            },
            "geometry_column": "geometry",
            "property_columns": [
                {"name": "name", "type": "text"},
                {"name": "district_id", "type": "text"},
                {"name": "population", "type": "integer"},
            ],
            "include_feature_id": True,
            "strict_properties": strict_properties,
        },
        "idempotency_key": idempotency_key,
    }


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del project
    return {
        "dir": tmp_path,
        "path": _write_geojson(
            tmp_path, "neighborhoods.geojson", _feature_collection()
        ),
    }


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _import_geojson_action(seeded["path"])


def _missing_source_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _import_geojson_action(
        seeded["dir"] / "missing.geojson",
        idempotency_key="import_geojson@sha256:missing-source",
    )


def _strict_properties_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # The first feature carries an extra "ignored" property, so strict mode
    # refuses the whole import.
    return _import_geojson_action(
        seeded["path"],
        sheet_name="Strict",
        idempotency_key="import_geojson@sha256:strict",
        strict_properties=True,
    )


def _invalid_geometry_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # "Circle" is not an RFC 7946 geometry type (all seven real types import).
    path = _write_geojson(
        seeded["dir"],
        "circles.geojson",
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "name": "Circle",
                        "district_id": "D3",
                        "population": 10,
                    },
                    "geometry": {"type": "Circle", "coordinates": [-73.0, 40.0, 100]},
                }
            ],
        },
    )
    return _import_geojson_action(
        path, sheet_name="Circles", idempotency_key="import_geojson@sha256:circle"
    )


def _conflict_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # Same idempotency key as the primary import, different sheet name.
    return _import_geojson_action(seeded["path"], sheet_name="Other Neighborhoods")


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt

    assert result.op_ids == [1]
    assert [output.kind for output in result.outputs] == [
        "sheet",
        *(["column"] * 5),
        "rows",
    ]

    sheet = project.db.execute(
        "SELECT * FROM sheets WHERE name='Neighborhoods'"
    ).fetchone()
    assert sheet is not None
    assert project.row_count(int(sheet["id"])) == 2
    columns = {
        row["name"]: row
        for row in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=? ORDER BY position",
            (sheet["id"],),
        ).fetchall()
    }
    assert columns["feature_id"]["type"] == "text"
    assert columns["population"]["type"] == "integer"
    assert columns["geometry"]["type"] == "geo_shape"
    feature_ids = project.get_values(int(sheet["id"]), int(columns["feature_id"]["id"]))
    assert list(feature_ids.values()) == ["neighborhood-1", "202"]
    populations = project.get_values(int(sheet["id"]), int(columns["population"]["id"]))
    assert list(populations.values()) == [12500, 8700]
    geometries = project.get_values(int(sheet["id"]), int(columns["geometry"]["id"]))
    assert list(geometries.values())[0]["type"] == "Polygon"
    assert list(geometries.values())[1]["type"] == "MultiPolygon"

    op = project.db.execute("SELECT * FROM ops WHERE id=1").fetchone()
    assert op is not None
    assert op["kind"] == "import.geojson"
    op_spec = json.loads(op["spec"])
    assert op_spec["action_id"] == "import.geojson"
    assert op_spec["params"]["source"]["kind"] == "file"
    assert op_spec["params"]["geometry_column"] == "geometry"
    assert [column["name"] for column in op_spec["params"]["property_columns"]] == [
        "name",
        "district_id",
        "population",
    ]
    assert op_spec["import_row_count"] == 2
    assert "rows" not in op_spec["params"]

    receipt_row = project.db.execute(
        "SELECT * FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    assert receipt_row["action_kind"] == "import.geojson"
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.schema_version == "frisket.receipt.v1"
    assert receipt.idempotency_key == "import_geojson@sha256:stable"
    assert receipt.op_ids == [1]
    file_read = next(
        item.ref for item in receipt.inputs if item.ref["kind"] == "local_file_read"
    )
    assert file_read["path"] == str(seeded["path"])
    assert (
        file_read["sha256"]
        == "sha256:" + hashlib.sha256(seeded["path"].read_bytes()).hexdigest()
    )
    assert {item.ref["kind"] for item in receipt.evidence} >= {
        "import_source",
        "source_rows",
        "source_cell",
    }
    source_refs = [
        item.ref for item in receipt.evidence if item.ref["kind"] == "import_source"
    ]
    assert source_refs[0]["source_kind"] == "file"
    assert source_refs[0]["importer"] == "geojson-featurecollection"
    assert source_refs[0]["path"] == str(seeded["path"])
    assert source_refs[0]["request_hash"].startswith("sha256:")


CASES = [
    ExecutorCase(
        kind="import.geojson",
        catalog=CatalogEntry(
            execution_mode="whole_project",
            async_mode="sync",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write",),
            side_effects=frozenset(
                {
                    "read_local_file",
                    "create_sheet",
                    "create_columns",
                    "create_rows",
                    "write_op",
                    "write_receipt",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_action_request",
                    "invalid_file_source",
                    "idempotency_conflict",
                }
            ),
            input_schema_properties=("source", "geometry_column", "property_columns"),
            output_schema_properties=(),
        ),
        seed=_seed,
        make_action=_make_action,
        gates=(
            Gate(
                "invalid_file_source",
                _missing_source_action,
                "invalid_file_source",
            ),
            Gate(
                "strict_properties_row_shape",
                _strict_properties_action,
                "row_shape_mismatch",
            ),
            Gate(
                "unsupported_geojson_geometry",
                _invalid_geometry_action,
                "unsupported_geojson_geometry",
            ),
            Gate(
                "idempotency_conflict",
                _conflict_action,
                "idempotency_conflict",
                after_primary_run=True,
            ),
        ),
        expect_counts={"sheets": 1, "columns": 5, "rows": 2, "ops": 1, "receipts": 1},
        check_state=_check_state,
        request_style="typed",
    )
]


def test_import_geojson_replay_survives_source_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        before = env.counts()

        env.seeded["path"].unlink()
        replay = env.run_primary()
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert replay.op_ids == first.op_ids
        assert env.counts() == before


def test_import_geojson_skip_invalid_geometry_quarantines_and_records_evidence(
    tmp_path: Path,
) -> None:
    from frisket.contracts.action import Receipt
    from frisket.engine.executor import run_action_spec

    body = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": "ok-1",
                "properties": {"name": "Good Polygon"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [-73.99, 40.75],
                            [-73.98, 40.75],
                            [-73.98, 40.76],
                            [-73.99, 40.75],
                        ]
                    ],
                },
            },
            {
                "type": "Feature",
                "id": "bad-1",
                "properties": {"name": "Bad Geometry"},
                "geometry": {"type": "Circle", "coordinates": [-73.0, 40.0, 100]},
            },
            {
                "type": "Feature",
                "id": "ok-2",
                "properties": {"name": "Good Point"},
                "geometry": {"type": "Point", "coordinates": [-73.5, 40.5]},
            },
        ],
    }
    geojson_path = _write_geojson(tmp_path, "quarantine.geojson", body)

    def _action(*, key: str, on_invalid: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "source": {
                "kind": "file",
                "path": str(geojson_path),
                "label": "quarantine.geojson",
            },
            "geometry_column": "geometry",
            "property_columns": [{"name": "name", "type": "text"}],
            "include_feature_id": True,
        }
        if on_invalid is not None:
            params["on_invalid_geometry"] = on_invalid
        return {
            "action_id": "import.geojson",
            "scope": {"kind": "project"},
            "sheet_name": "Quarantine",
            "params": params,
            "idempotency_key": key,
        }

    # Default (reject) keeps the shipped fail-fast behavior on a mixed file.
    reject_project = Project.create(tmp_path / "reject.frisket", name="Reject")
    try:
        reject = run_action_spec(
            reject_project,
            _action(key="import_geojson_reject@sha256:stable", on_invalid=None),
            project_id="project-reject",
        )
        assert reject.status == "failed"
        assert reject.errors[0].code == "unsupported_geojson_geometry"
    finally:
        reject_project.close()

    # skip quarantines the bad feature: keeps its row with an empty geometry
    # cell (properties survive) and records the skipped index in the receipt.
    project = Project.create(tmp_path / "skip.frisket", name="Skip")
    try:
        result = run_action_spec(
            project,
            _action(key="import_geojson_skip@sha256:stable", on_invalid="skip"),
            project_id="project-skip",
        )
        assert result.status == "completed", result.errors

        sheet = project.db.execute(
            "SELECT * FROM sheets WHERE name='Quarantine'"
        ).fetchone()
        assert project.row_count(int(sheet["id"])) == 3
        columns = {
            row["name"]: row
            for row in project.db.execute(
                "SELECT * FROM columns WHERE sheet_id=?", (sheet["id"],)
            ).fetchall()
        }
        geometries = list(
            project.get_values(
                int(sheet["id"]), int(columns["geometry"]["id"])
            ).values()
        )
        names = list(
            project.get_values(int(sheet["id"]), int(columns["name"]["id"])).values()
        )
        # The middle (quarantined) feature keeps its row + properties, empty geo.
        assert [g["type"] if g else None for g in geometries] == [
            "Polygon",
            None,
            "Point",
        ]
        assert names == ["Good Polygon", "Bad Geometry", "Good Point"]
        receipt_row = project.db.execute(
            "SELECT * FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()
        receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
        source = next(
            item.ref for item in receipt.inputs if "skipped_features" in item.ref
        )
        assert source["line_count"] == 3
        assert source["skipped_features"] == [1]
        geojson_path.unlink()
        replay = run_action_spec(
            project,
            _action(key="import_geojson_skip@sha256:stable", on_invalid="skip"),
            project_id="project-skip",
        )
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == result.receipt_id
    finally:
        project.close()


def test_import_geojson_accepts_bare_feature_and_geometry(tmp_path: Path) -> None:
    from frisket.contracts.action import Receipt
    from frisket.engine.executor import run_action_spec

    def _run(
        project: Project, body: dict[str, Any], *, name: str, key: str, params: dict
    ):
        geojson_path = _write_geojson(tmp_path, f"{name}.geojson", body)
        result = run_action_spec(
            project,
            {
                "action_id": "import.geojson",
                "scope": {"kind": "project"},
                "sheet_name": name,
                "params": {
                    "source": {
                        "kind": "file",
                        "path": str(geojson_path),
                        "label": f"{name}.geojson",
                    },
                    "geometry_column": "geometry",
                    **params,
                },
                "idempotency_key": key,
            },
            project_id=f"project-{name}",
        )
        assert result.status == "completed", result.errors
        sheet = project.db.execute(
            "SELECT * FROM sheets WHERE name=?", (name,)
        ).fetchone()
        columns = {
            row["name"]: row
            for row in project.db.execute(
                "SELECT * FROM columns WHERE sheet_id=?", (sheet["id"],)
            ).fetchall()
        }
        geometries = list(
            project.get_values(
                int(sheet["id"]), int(columns["geometry"]["id"])
            ).values()
        )
        receipt_row = project.db.execute(
            "SELECT * FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()
        receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
        return sheet, columns, geometries, receipt

    # Bare Feature (not wrapped in a FeatureCollection).
    project = Project.create(tmp_path / "bare.frisket", name="Bare")
    try:
        bare_feature = {
            "type": "Feature",
            "id": "solo-1",
            "properties": {"name": "Solo Park"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [-73.99, 40.75],
                        [-73.98, 40.75],
                        [-73.98, 40.76],
                        [-73.99, 40.75],
                    ]
                ],
            },
        }
        _sheet, _columns, geometries, receipt = _run(
            project,
            bare_feature,
            name="BareFeature",
            key="import_geojson_bare_feature@sha256:stable",
            params={
                "property_columns": [{"name": "name", "type": "text"}],
                "include_feature_id": True,
            },
        )
        assert len(geometries) == 1
        assert geometries[0]["type"] == "Polygon"
        assert any(item.ref.get("row_count") == 1 for item in receipt.outputs)

        # Bare Geometry (a naked GeoJSON geometry object, no Feature wrapper).
        bare_geometry = {"type": "Point", "coordinates": [-73.5, 40.5]}
        sheet, columns, geometries, receipt = _run(
            project,
            bare_geometry,
            name="BareGeometry",
            key="import_geojson_bare_geometry@sha256:stable",
            params={"property_columns": [], "include_feature_id": True},
        )
        assert len(geometries) == 1
        assert geometries[0]["type"] == "Point"
        assert any(item.ref.get("row_count") == 1 for item in receipt.outputs)
        # A bare geometry carries no id → the feature_id cell is empty.
        fid_values = list(
            project.get_values(
                int(sheet["id"]), int(columns["feature_id"]["id"])
            ).values()
        )
        assert fid_values == [None]
    finally:
        project.close()


def test_import_geojson_accepts_point_line_and_collection_geometries(
    tmp_path: Path,
) -> None:
    from frisket.authoring import column_types
    from frisket.engine.executor import run_action_spec

    spec = column_types.get_column_type("geo_shape")
    assert spec is not None
    assert spec.presentation["renderer"] == "map-overlay"

    # All seven RFC 7946 geometry types validate as geo_shape.
    point = {"type": "Point", "coordinates": [-73.99, 40.75]}
    line = {"type": "LineString", "coordinates": [[-73.99, 40.75], [-73.98, 40.76]]}
    multipoint = {
        "type": "MultiPoint",
        "coordinates": [[-73.99, 40.75], [-73.98, 40.76]],
    }
    multiline = {
        "type": "MultiLineString",
        "coordinates": [[[-73.99, 40.75], [-73.98, 40.76]]],
    }
    polygon = {
        "type": "Polygon",
        "coordinates": [
            [[-73.99, 40.75], [-73.98, 40.75], [-73.98, 40.76], [-73.99, 40.75]]
        ],
    }
    multipolygon = {
        "type": "MultiPolygon",
        "coordinates": [
            [[[-73.97, 40.72], [-73.96, 40.72], [-73.96, 40.73], [-73.97, 40.72]]]
        ],
    }
    collection = {"type": "GeometryCollection", "geometries": [point, line, polygon]}
    for geometry in (
        point,
        line,
        multipoint,
        multiline,
        polygon,
        multipolygon,
        collection,
    ):
        assert column_types.validate_value("geo_shape", geometry), geometry["type"]

    # Malformed coordinates are still rejected honestly.
    assert not column_types.validate_value(
        "geo_shape", {"type": "Point", "coordinates": ["x", "y"]}
    )
    assert not column_types.validate_value(
        "geo_shape", {"type": "LineString", "coordinates": [[-73.99, 40.75]]}
    )
    assert not column_types.validate_value(
        "geo_shape", {"type": "GeometryCollection", "geometries": [{"type": "Nope"}]}
    )
    assert not column_types.validate_value(
        "geo_shape", {"type": "Circle", "coordinates": [-73.99, 40.75, 100]}
    )

    body = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "id": "pt", "properties": {}, "geometry": point},
            {"type": "Feature", "id": "ln", "properties": {}, "geometry": line},
            {"type": "Feature", "id": "gc", "properties": {}, "geometry": collection},
        ],
    }
    geojson_path = _write_geojson(tmp_path, "shapes.geojson", body)
    project = Project.create(tmp_path / "shapes.frisket", name="Shapes GeoJSON")
    try:
        result = run_action_spec(
            project,
            {
                "action_id": "import.geojson",
                "scope": {"kind": "project"},
                "sheet_name": "Shapes",
                "params": {
                    "source": {
                        "kind": "file",
                        "path": str(geojson_path),
                        "label": "shapes.geojson",
                    },
                    "geometry_column": "geometry",
                    "property_columns": [],
                    "include_feature_id": True,
                    "strict_properties": False,
                },
                "idempotency_key": "import_geojson_shapes@sha256:stable",
            },
            project_id="project-shapes",
        )
        assert result.status == "completed", result.errors

        sheet = project.db.execute(
            "SELECT * FROM sheets WHERE name='Shapes'"
        ).fetchone()
        assert sheet is not None
        assert project.row_count(int(sheet["id"])) == 3
        columns = {
            row["name"]: row
            for row in project.db.execute(
                "SELECT * FROM columns WHERE sheet_id=?", (sheet["id"],)
            ).fetchall()
        }
        assert columns["geometry"]["type"] == "geo_shape"
        geometries = list(
            project.get_values(
                int(sheet["id"]), int(columns["geometry"]["id"])
            ).values()
        )
        assert [g["type"] for g in geometries] == [
            "Point",
            "LineString",
            "GeometryCollection",
        ]
    finally:
        project.close()
