from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from executor_harness import CatalogEntry, ExecutorCase
from frisket.engine.store import Project

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "geo"


def _import_kml_action(
    source_path: Path,
    *,
    sheet_name: str,
    idempotency_key: str,
    property_columns: list[dict[str, str]] | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    return {
        "action_id": "import.kml",
        "scope": {"kind": "project"},
        "sheet_name": sheet_name,
        "params": {
            "source": {
                "kind": "file",
                "path": str(source_path),
                "label": label or source_path.name,
            },
            "geometry_column": "geometry",
            "property_columns": property_columns or [],
            "include_name": True,
            "include_description": True,
        },
        "idempotency_key": idempotency_key,
    }


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del project, tmp_path
    # import.kml declares lxml as a core dependency; if a host lacks it, skip
    # with a clear reason rather than silently passing (dep-less gap-marking,
    # Skipping here scopes it to this case, not the whole harness.
    pytest.importorskip("lxml")
    usgs = FIXTURES / "usgs_earthquakes_m5_2024-01-01.kml"
    assert usgs.exists()
    return {"path": usgs}


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _import_kml_action(
        seeded["path"],
        sheet_name="Earthquakes",
        idempotency_key="import_kml_usgs@sha256:stable",
    )


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt
    from frisket.authoring.plugin_registry import default_registry

    del seeded
    assert result.op_ids == [1]
    importer_names = {item.name for item in default_registry().importer_specs()}
    assert "kml" in importer_names

    # USGS earthquakes: name + description + Point (altitude dropped to 2D).
    sheet = project.db.execute(
        "SELECT * FROM sheets WHERE name='Earthquakes'"
    ).fetchone()
    assert sheet is not None
    assert project.row_count(int(sheet["id"])) == 3
    columns = {
        row["name"]: row
        for row in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=? ORDER BY position",
            (sheet["id"],),
        ).fetchall()
    }
    assert columns["name"]["type"] == "text"
    assert columns["description"]["type"] == "text"
    assert columns["geometry"]["type"] == "geo_shape"
    names = list(
        project.get_values(int(sheet["id"]), int(columns["name"]["id"])).values()
    )
    assert names[0] == "M 5.1 - 73 km WSW of Sado, Japan"
    descriptions = list(
        project.get_values(int(sheet["id"]), int(columns["description"]["id"])).values()
    )
    assert descriptions[0] is not None and "M 5.1" in descriptions[0]
    geometries = list(
        project.get_values(int(sheet["id"]), int(columns["geometry"]["id"])).values()
    )
    assert geometries[0]["type"] == "Point"
    assert len(geometries[0]["coordinates"]) == 2

    # The typed operation preserves the authored request and import identity.
    op = project.db.execute("SELECT * FROM ops WHERE id=1").fetchone()
    assert op["kind"] == "import.kml"
    op_spec = json.loads(op["spec"])
    assert op_spec["action_id"] == "import.kml"
    assert op_spec["import_row_count"] == 3

    receipt_row = project.db.execute(
        "SELECT * FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row["action_kind"] == "import.kml"
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.action_kind == "import.kml"
    assert any(item.ref["kind"] == "local_file_read" for item in receipt.inputs)
    assert any(item.ref.get("row_count") == 3 for item in receipt.outputs)
    source_refs = [
        item.ref for item in receipt.evidence if item.ref["kind"] == "import_source"
    ]
    assert source_refs[0]["importer"] == "kml-placemarks"


CASES = [
    ExecutorCase(
        kind="import.kml",
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
        ),
        seed=_seed,
        make_action=_make_action,
        expect_counts={"sheets": 1, "columns": 3, "rows": 3, "ops": 1, "receipts": 1},
        check_state=_check_state,
        request_style="typed",
    )
]


def test_import_kmz_polygons_and_extended_data_columns(tmp_path: Path) -> None:
    """A zipped KMZ archive imports like its inner doc.kml, ExtendedData maps
    to declared property columns, and placemarks without <name>/<description>
    leave those cells empty."""
    from frisket.engine.executor import run_action_spec

    pytest.importorskip("lxml")
    nps_kml = FIXTURES / "nps_boundaries_trimmed.kml"
    assert nps_kml.exists()
    kmz_path = tmp_path / "nps_boundaries.kmz"
    with zipfile.ZipFile(kmz_path, "w") as archive:
        archive.writestr("doc.kml", nps_kml.read_bytes())

    project = Project.create(tmp_path / "nps.frisket", name="NPS KMZ")
    try:
        result = run_action_spec(
            project,
            _import_kml_action(
                kmz_path,
                sheet_name="Parks",
                idempotency_key="import_kml_nps@sha256:stable",
                property_columns=[
                    {"name": "UNIT_CODE", "type": "text"},
                    {"name": "UNIT_NAME", "type": "text"},
                ],
            ),
            project_id="project-nps",
        )
        assert result.status == "completed", result.errors

        sheet = project.db.execute("SELECT * FROM sheets WHERE name='Parks'").fetchone()
        assert project.row_count(int(sheet["id"])) == 2
        columns = {
            row["name"]: row
            for row in project.db.execute(
                "SELECT * FROM columns WHERE sheet_id=?", (sheet["id"],)
            ).fetchall()
        }
        assert columns["UNIT_CODE"]["type"] == "text"
        assert columns["geometry"]["type"] == "geo_shape"
        unit_codes = list(
            project.get_values(
                int(sheet["id"]), int(columns["UNIT_CODE"]["id"])
            ).values()
        )
        assert unit_codes[0] == "UPDE"
        unit_names = list(
            project.get_values(
                int(sheet["id"]), int(columns["UNIT_NAME"]["id"])
            ).values()
        )
        assert unit_names[0] == "Upper Delaware"
        geometries = list(
            project.get_values(
                int(sheet["id"]), int(columns["geometry"]["id"])
            ).values()
        )
        assert geometries[0]["type"] in {"Polygon", "MultiPolygon"}
        # NPS placemarks carry no <name>/<description>, so those cells are empty.
        names = list(
            project.get_values(int(sheet["id"]), int(columns["name"]["id"])).values()
        )
        assert names == [None, None]
    finally:
        project.close()
