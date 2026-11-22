"""Executor, lineage, fan-out guard, refresh, and HTTP tests for derive.spatial_join."""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.actions.registry import ACTION_REGISTRY
from frisket.engine.executor import run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.sheet_lifecycle import dependent_sheets
from frisket.features.graph.sheet_graph import sheet_materialized_kind
from frisket.server.app import create_app

PROJECT_ID = "spatial-join-project"
_KEYGEN = itertools.count(1)

POLY_NORTH = {
    "type": "Polygon",
    "coordinates": [[[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]]],
}
POLY_OVERLAP = {
    "type": "Polygon",
    "coordinates": [[[5.0, 0.0], [15.0, 0.0], [15.0, 10.0], [5.0, 10.0], [5.0, 0.0]]],
}
POLY_WITH_HOLE = {
    "type": "Polygon",
    "coordinates": [
        [[20.0, 0.0], [30.0, 0.0], [30.0, 10.0], [20.0, 10.0], [20.0, 0.0]],
        [[23.0, 3.0], [27.0, 3.0], [27.0, 7.0], [23.0, 7.0], [23.0, 3.0]],
    ],
}


def _seed_spatial_sheets(project: Project) -> dict[str, Any]:
    left_id = project.add_sheet("Incidents")
    left_cols = {
        "name": project.add_column(left_id, "name", type="text"),
        "location": project.add_column(left_id, "location", type="geo_point"),
    }
    left_rows = project.add_rows(
        left_id,
        [
            {"name": "InNorthOnly", "location": {"lat": 5.0, "lon": 2.0}},
            {"name": "InOverlap", "location": {"lat": 5.0, "lon": 7.0}},
            {"name": "InHole", "location": {"lat": 5.0, "lon": 25.0}},
            {"name": "OutsideAll", "location": {"lat": 50.0, "lon": 50.0}},
            {"name": "MissingPoint", "location": None},
        ],
        left_cols,
    )

    right_id = project.add_sheet("Districts")
    right_cols = {
        "name": project.add_column(right_id, "name", type="text"),
        "zone_code": project.add_column(right_id, "zone_code", type="text"),
        "geometry": project.add_column(right_id, "geometry", type="geo_shape"),
    }
    right_rows = project.add_rows(
        right_id,
        [
            {"name": "North District", "zone_code": "N1", "geometry": POLY_NORTH},
            {"name": "Overlap Zone", "zone_code": "O2", "geometry": POLY_OVERLAP},
            {"name": "Donut Zone", "zone_code": "D3", "geometry": POLY_WITH_HOLE},
            {
                "name": "PointOnly",
                "zone_code": "P4",
                "geometry": {"type": "Point", "coordinates": [2.0, 5.0]},
            },
            {"name": "NullGeom", "zone_code": "X5", "geometry": None},
        ],
        right_cols,
    )
    project.db.commit()
    return {
        "left_sheet_id": left_id,
        "right_sheet_id": right_id,
        "left_row_ids": left_rows,
        "right_row_ids": right_rows,
        "left_cols": left_cols,
        "right_cols": right_cols,
    }


def _spatial_action(
    *,
    left_sheet_id: int,
    right_sheet_id: int,
    point_column: str = "location",
    shape_column: str = "geometry",
    how: str = "left",
    match: str = "all",
    right_columns: list[str] | None = None,
    indicator: bool = False,
    target_sheet_name: str = "SpatialJoined",
    row_ids: list[int] | None = None,
    confirmation: str | None = None,
    max_output_rows: int | None = None,
) -> dict[str, Any]:
    scope: dict[str, Any] = {"kind": "sheet_rows", "sheet_id": left_sheet_id}
    if row_ids is not None:
        scope["row_ids"] = row_ids
    params: dict[str, Any] = {
        "point_column": point_column,
        "right_sheet": {"sheet_id": right_sheet_id},
        "shape_column": shape_column,
        "how": how,
        "match": match,
        "indicator": indicator,
    }
    if right_columns is not None:
        params["right_columns"] = right_columns
    if max_output_rows is not None:
        params["max_output_rows"] = max_output_rows
    return {
        "action_id": "derive.spatial_join",
        "scope": scope,
        "sheet_name": target_sheet_name,
        "confirmation": confirmation,
        "params": params,
        "idempotency_key": f"derive_spatial_join@sha256:{next(_KEYGEN)}",
    }


def _materialized_table(project: Project, sheet_id: int) -> dict[str, Any]:
    columns = [
        (str(c["name"]), str(c["type"]), int(c["id"]))
        for c in project.columns(sheet_id)
    ]
    row_ids = project.visible_row_ids(sheet_id)
    values_by_col = {
        name: project.get_values(sheet_id, col_id) for name, _t, col_id in columns
    }
    rows = [
        {name: values_by_col[name].get(rid) for name, _t, _c in columns}
        for rid in row_ids
    ]
    return {
        "column_names": [name for name, _t, _c in columns],
        "column_types": {name: t for name, t, _c in columns},
        "rows": rows,
        "row_ids": row_ids,
    }


def _sheet_id_by_name(project: Project, name: str) -> int:
    row = project.db.execute(
        "SELECT id FROM sheets WHERE name=? AND hidden=0", (name,)
    ).fetchone()
    assert row is not None
    return int(row["id"])


def test_catalog_entry_is_local_zero_cost_with_geo_point_requirement() -> None:
    entry = ACTION_REGISTRY.get("derive.spatial_join").catalog_entry()
    assert entry["title"] == "Spatial join (point in polygon)"
    assert entry["cost_policy"]["kind"] == "none"
    assert entry["cost_policy"]["requires_confirmation"] is False
    assert entry["row_scope_policy"]["kind"] == "sheet_rows"
    assert entry["ui_hints"]["dynamic_outputs"] is True
    source_reqs = {
        req["param"]: req for req in entry["ui_hints"]["source_requirements"]
    }
    assert source_reqs["point_column"]["accepted_column_types"] == ["geo_point"]


def test_left_all_join_with_collisions_indicator_and_lineage(project: Project) -> None:
    seed = _seed_spatial_sheets(project)
    res = run_action_spec(
        project,
        _spatial_action(
            left_sheet_id=seed["left_sheet_id"],
            right_sheet_id=seed["right_sheet_id"],
            how="left",
            match="all",
            indicator=True,
            target_sheet_name="LeftAll",
        ),
        project_id=PROJECT_ID,
    )
    assert res.status == "completed", res.errors
    child_id = _sheet_id_by_name(project, "LeftAll")
    table = _materialized_table(project, child_id)
    # Left "name" keeps its name; colliding right "name" becomes "name_right".
    assert table["column_names"] == [
        "name",
        "location",
        "name_right",
        "zone_code",
        "geometry",
        "_merge",
    ]
    summary = [
        (r["name"], r["name_right"], r["zone_code"], r["_merge"])
        for r in table["rows"]
    ]
    assert summary == [
        ("InNorthOnly", "North District", "N1", "both"),
        ("InOverlap", "North District", "N1", "both"),
        ("InOverlap", "Overlap Zone", "O2", "both"),
        ("InHole", None, None, "left_only"),
        ("OutsideAll", None, None, "left_only"),
        ("MissingPoint", None, None, "left_only"),
    ]
    # Lineage: both left and right sheets track this derived child as a dependent.
    left_deps = {d["id"] for d in dependent_sheets(project, seed["left_sheet_id"])}
    right_deps = {d["id"] for d in dependent_sheets(project, seed["right_sheet_id"])}
    assert child_id in left_deps
    assert child_id in right_deps
    assert (
        sheet_materialized_kind(project, child_id, op_kind="derive.spatial_join")
        == "join"
    )
    assert res.receipt_id is not None
    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (res.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    receipt_body = json.loads(receipt_row["body"])
    read_kinds = [inp["ref"].get("kind") for inp in receipt_body["inputs"]]
    assert "spatial_join_source" in read_kinds


def test_inner_and_first_match_modes_and_right_columns_selection(
    project: Project,
) -> None:
    seed = _seed_spatial_sheets(project)
    res = run_action_spec(
        project,
        _spatial_action(
            left_sheet_id=seed["left_sheet_id"],
            right_sheet_id=seed["right_sheet_id"],
            how="inner",
            match="first",
            right_columns=["zone_code"],
            target_sheet_name="InnerFirst",
        ),
        project_id=PROJECT_ID,
    )
    assert res.status == "completed", res.errors
    child_id = _sheet_id_by_name(project, "InnerFirst")
    table = _materialized_table(project, child_id)
    assert table["column_names"] == ["name", "location", "zone_code"]
    assert [(r["name"], r["zone_code"]) for r in table["rows"]] == [
        ("InNorthOnly", "N1"),
        ("InOverlap", "N1"),
    ]


def test_collision_suffix_avoids_existing_right_suffixed_left_column(
    project: Project,
) -> None:
    left_id = project.add_sheet("Pts")
    left_cols = {
        "name": project.add_column(left_id, "name", type="text"),
        "name_right": project.add_column(left_id, "name_right", type="text"),
        "loc": project.add_column(left_id, "loc", type="geo_point"),
    }
    project.add_rows(
        left_id,
        [{"name": "P1", "name_right": "existing", "loc": {"lat": 5.0, "lon": 2.0}}],
        left_cols,
    )
    right_id = project.add_sheet("Polys")
    right_cols = {
        "name": project.add_column(right_id, "name", type="text"),
        "geom": project.add_column(right_id, "geom", type="geo_shape"),
    }
    project.add_rows(right_id, [{"name": "North", "geom": POLY_NORTH}], right_cols)
    project.db.commit()

    res = run_action_spec(
        project,
        _spatial_action(
            left_sheet_id=left_id,
            right_sheet_id=right_id,
            point_column="loc",
            shape_column="geom",
            target_sheet_name="CollisionSafe",
        ),
        project_id=PROJECT_ID,
    )
    assert res.status == "completed", res.errors
    table = _materialized_table(project, _sheet_id_by_name(project, "CollisionSafe"))
    assert table["column_names"] == ["name", "name_right", "loc", "name_right_2", "geom"]
    assert table["rows"][0]["name"] == "P1"
    assert table["rows"][0]["name_right"] == "existing"
    assert table["rows"][0]["name_right_2"] == "North"


def test_fanout_guard_refuses_until_confirmation_token_echoed(
    project: Project,
) -> None:
    left_id = project.add_sheet("Pts")
    left_cols = {"location": project.add_column(left_id, "location", type="geo_point")}
    project.add_rows(
        left_id,
        [{"location": {"lat": 5.0, "lon": 7.0}} for _ in range(3)],
        left_cols,
    )
    right_id = project.add_sheet("Polys")
    right_cols = {"geometry": project.add_column(right_id, "geometry", type="geo_shape")}
    project.add_rows(
        right_id,
        [{"geometry": POLY_NORTH}, {"geometry": POLY_OVERLAP}, {"geometry": POLY_NORTH}],
        right_cols,
    )
    project.db.commit()

    # 3 points x 3 overlapping polygons = 9 rows > max_output_rows=5
    spec = _spatial_action(
        left_sheet_id=left_id,
        right_sheet_id=right_id,
        max_output_rows=5,
        target_sheet_name="FanoutResult",
    )
    gated = run_action_spec(project, spec, project_id=PROJECT_ID)
    assert gated.status == "needs_confirmation"
    assert gated.errors[0].code == "join_fanout_requires_confirmation"
    details = gated.errors[0].details or {}
    assert details["estimated_rows"] == 9
    assert details["max_output_rows"] == 5
    token = details["promise_set_hash"]
    assert isinstance(token, str) and len(token) > 10

    # Confirming with the exact token succeeds and materializes all 9 rows.
    confirmed_spec = {**spec, "confirmation": token}
    done = run_action_spec(project, confirmed_spec, project_id=PROJECT_ID)
    assert done.status == "completed", done.errors
    child_id = _sheet_id_by_name(project, "FanoutResult")
    assert len(project.visible_row_ids(child_id)) == 9


def test_invalid_column_types_and_same_sheet_rejected(project: Project) -> None:
    seed = _seed_spatial_sheets(project)

    # Wrong point_column type (text instead of geo_point)
    bad_point = run_action_spec(
        project,
        _spatial_action(
            left_sheet_id=seed["left_sheet_id"],
            right_sheet_id=seed["right_sheet_id"],
            point_column="name",
            target_sheet_name="Bad1",
        ),
        project_id=PROJECT_ID,
    )
    assert bad_point.status == "failed"
    assert bad_point.errors[0].code == "invalid_input_ref"

    # Wrong shape_column type (text instead of geo_shape)
    bad_shape = run_action_spec(
        project,
        _spatial_action(
            left_sheet_id=seed["left_sheet_id"],
            right_sheet_id=seed["right_sheet_id"],
            shape_column="zone_code",
            target_sheet_name="Bad2",
        ),
        project_id=PROJECT_ID,
    )
    assert bad_shape.status == "failed"
    assert bad_shape.errors[0].code == "invalid_input_ref"

    # Same sheet on both sides
    same_sheet = run_action_spec(
        project,
        _spatial_action(
            left_sheet_id=seed["left_sheet_id"],
            right_sheet_id=seed["left_sheet_id"],
            target_sheet_name="Bad3",
        ),
        project_id=PROJECT_ID,
    )
    assert same_sheet.status == "failed"
    assert same_sheet.errors[0].code == "invalid_input_ref"


def test_spatial_join_sheet_refresh_updates_after_source_change(
    project: Project,
) -> None:
    seed = _seed_spatial_sheets(project)
    res = run_action_spec(
        project,
        _spatial_action(
            left_sheet_id=seed["left_sheet_id"],
            right_sheet_id=seed["right_sheet_id"],
            how="inner",
            match="first",
            right_columns=["zone_code"],
            target_sheet_name="RefreshableSpatial",
        ),
        project_id=PROJECT_ID,
    )
    assert res.status == "completed", res.errors
    child_id = _sheet_id_by_name(project, "RefreshableSpatial")
    assert len(project.visible_row_ids(child_id)) == 2

    # Add a new point inside the Donut polygon's solid ring (outside its hole)
    project.add_rows(
        seed["left_sheet_id"],
        [{"name": "InDonutRing", "location": {"lat": 1.0, "lon": 21.0}}],
        seed["left_cols"],
    )
    project.db.commit()

    refreshed = run_action_spec(
        project,
        {
            "action_id": "sheet.refresh",
            "scope": {"kind": "project"},
            "params": {"sheet_id": child_id},
            "idempotency_key": f"sheet_refresh@sha256:{next(_KEYGEN)}",
        },
        project_id=PROJECT_ID,
    )
    assert refreshed.status == "completed", refreshed.errors
    table = _materialized_table(project, child_id)
    assert [(r["name"], r["zone_code"]) for r in table["rows"]] == [
        ("InNorthOnly", "N1"),
        ("InOverlap", "N1"),
        ("InDonutRing", "D3"),
    ]


def test_http_validate_and_execute_spatial_join_end_to_end(tmp_path: Path) -> None:
    app = create_app(tmp_path / "ws")
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={"name": "Spatial HTTP"}).json()["id"]
        proj = app.state.workspace.get(pid)
        seed = _seed_spatial_sheets(proj)

        validate_resp = client.post(
            f"/api/projects/{pid}/actions/v1/validate-params",
            json={
                "action": {
                    "action_id": "derive.spatial_join",
                    "scope": {"kind": "sheet_rows", "sheet_id": seed["left_sheet_id"]},
                    "params": {
                        "point_column": "location",
                        "right_sheet": {"sheet_id": seed["right_sheet_id"]},
                        "shape_column": "geometry",
                        "how": "inner",
                        "match": "first",
                        "right_columns": ["name", "zone_code"],
                        "indicator": True,
                    },
                }
            },
        )
        assert validate_resp.status_code == 200, validate_resp.text
        validated = validate_resp.json()
        assert validated["diagnostics"] == {}
        assert validated["creates_sheet"] is True
        assert [col["key"] for col in validated["logical_outputs"]] == [
            "name",
            "location",
            "name_right",
            "zone_code",
            "_merge",
        ]

        run_resp = client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json=_spatial_action(
                left_sheet_id=seed["left_sheet_id"],
                right_sheet_id=seed["right_sheet_id"],
                how="inner",
                match="first",
                right_columns=["name", "zone_code"],
                indicator=True,
                target_sheet_name="HttpSpatialJoin",
            ),
        )
        assert run_resp.status_code == 200, run_resp.text
        body = run_resp.json()
        assert body["status"] == "completed", body
        child_id = _sheet_id_by_name(proj, "HttpSpatialJoin")
        table = _materialized_table(proj, child_id)
        assert [(r["name"], r["name_right"], r["zone_code"], r["_merge"]) for r in table["rows"]] == [
            ("InNorthOnly", "North District", "N1", "both"),
            ("InOverlap", "North District", "N1", "both"),
        ]
