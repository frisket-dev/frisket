from __future__ import annotations

import pytest

from frisket.engine.projections.point_backend import GeoProjectionBackend
from frisket.engine.runner.result_generations import _compatibility_key
from frisket.engine.store import Project
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore
from helpers import run_writer_authority_fixture, write_claimed_test_results


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "geo.frisket", name="geo")
    yield p
    p.close()


def _seed_source(project: Project):
    """Source geo_point column with 2 valid points and 1 out-of-range row."""
    sheet = project.add_sheet("places")
    name_col = project.add_column(sheet, "name", type="text")
    geo_col = project.add_column(sheet, "location", type="geo_point")
    rows = project.add_rows(
        sheet,
        [
            {"name": "NYC", "location": {"lat": 40.7128, "lon": -74.0060}},
            {"name": "Tokyo", "location": {"lat": 35.6764, "lon": 139.65}},
            {"name": "Bad", "location": {"lat": 91, "lon": 0}},
        ],
        {"name": name_col, "location": geo_col},
    )
    return sheet, geo_col, rows


def _row_ids_on_map(backend, sheet, col):
    points, _ = backend.query_points(sheet, col)
    return {p.row_id for p in points}


def test_manual_edit_changes_projection(project):
    sheet, geo_col, rows = _seed_source(project)
    backend = GeoProjectionBackend(project)
    assert _row_ids_on_map(backend, sheet, geo_col) == {rows[0], rows[1]}

    # Edit the out-of-range row into a valid point.
    project.apply_edits(
        [{"row_id": rows[2], "column_id": geo_col, "value": {"lat": 1.0, "lon": 2.0}}]
    )
    status = backend.materialize_geo_column(sheet, geo_col)
    assert status["valid_points"] == 3
    assert _row_ids_on_map(backend, sheet, geo_col) == {rows[0], rows[1], rows[2]}


def test_undo_restores_old_point_and_redo_restores_edit(project):
    sheet, geo_col, rows = _seed_source(project)
    backend = GeoProjectionBackend(project)

    # Move NYC to a new location via a manual edit.
    project.apply_edits(
        [{"row_id": rows[0], "column_id": geo_col, "value": {"lat": 0.0, "lon": 0.0}}]
    )
    points, _ = backend.query_points(sheet, geo_col, row_ids=[rows[0]])
    assert points[0].lon == pytest.approx(0.0)
    assert points[0].lat == pytest.approx(0.0)

    # Undo -> the projection must show the ORIGINAL NYC coordinates again.
    project.undo()
    points, _ = backend.query_points(sheet, geo_col, row_ids=[rows[0]])
    assert points[0].lon == pytest.approx(-74.0060)
    assert points[0].lat == pytest.approx(40.7128)

    # Redo -> the edited point returns.
    project.redo()
    points, _ = backend.query_points(sheet, geo_col, row_ids=[rows[0]])
    assert points[0].lon == pytest.approx(0.0)
    assert points[0].lat == pytest.approx(0.0)


def _seed_ai_run(project: Project, values_by_row):
    sheet = project.add_sheet("p")
    name_col = project.add_column(sheet, "n", type="text")
    geo_col = project.add_column(sheet, "loc", type="geo_point", ai_generated=True)
    rows = project.add_rows(
        sheet,
        [{"n": "a"}, {"n": "b"}],
        {"n": name_col},
    )
    op = project.append_op("map", {"action_kind": "map.to_geo_point"})
    run = RunResultStore(project).start_run(
        op, sheet, "map.to_geo_point", total_rows=len(rows)
    )
    batch = [
        {"row_id": rows[i], "column_id": geo_col, "value": v}
        for i, v in values_by_row.items()
    ]
    write_claimed_test_results(project, run, batch)
    return sheet, geo_col, rows, op, run


def test_staged_full_rerun_keeps_old_points_until_atomic_seal(project):
    sheet, geo_col, rows, op1, run1 = _seed_ai_run(
        project, {0: {"lat": 1.0, "lon": 2.0}}
    )
    RunResultStore(project).finish_run(run1)
    RunResultStore(project).point_column_at_run(op1, geo_col, run1)

    backend = GeoProjectionBackend(project)
    s1 = backend.materialize_geo_column(sheet, geo_col)
    assert s1["valid_points"] == 1
    gen1 = s1["generation_hash"]

    # A replacement generation is visible as chronology while its values stay
    # staged. The cached projection must keep serving the old exact heads until
    # the whole generation seals atomically.
    op2 = project.append_op("map", {"action_kind": "map.to_geo_point"})
    run2 = RunResultStore(project).start_run(
        op2, sheet, "map.to_geo_point", total_rows=2
    )
    authority = run_writer_authority_fixture(project, run2, output_column_ids={geo_col})
    assert authority.claim_token is not None
    generations = ResultGenerationStore(project)
    base = generations.get_binding(run1, geo_col)
    assert base is not None
    generations.declare(
        run2,
        geo_col,
        output_role=base.output_role,
        compatibility_key=_compatibility_key(
            field={
                "name": "loc",
                "column_type": "geo_point",
                "semantic_type": None,
                "format": None,
            }
        ),
        write_mode="replace_scope",
        claim_token=authority.claim_token,
    )
    RunResultStore(project).write_results(
        run2,
        [
            {
                "row_id": rows[0],
                "column_id": geo_col,
                "value": {"lat": 5.0, "lon": 6.0},
                "publication_effect": "publish_value",
            },
            {
                "row_id": rows[1],
                "column_id": geo_col,
                "value": {"lat": 3.0, "lon": 4.0},
                "publication_effect": "publish_value",
            },
        ],
        **authority.kwargs(),
    )

    staged_points, staged_status = backend.query_points(sheet, geo_col)
    assert [(point.lat, point.lon) for point in staged_points] == [(1.0, 2.0)]
    assert generations.get_binding(run2, geo_col).state == "staged"  # type: ignore[union-attr]

    generations.seal(
        run2,
        {geo_col},
        claim_token=authority.claim_token,
        terminal_disposition="completed",
    )
    RunResultStore(project).finish_run(run2)
    OutputColumnClaimStore(project).finish_current_writer(
        run_id=run2,
        writer_attempt_id=authority.writer_attempt_id,
        claim_token=authority.claim_token,
        attempt_state="effected",
    )

    s2 = backend.materialize_geo_column(sheet, geo_col)
    assert s2["valid_points"] == 2
    assert s2["generation_hash"] not in {gen1, staged_status["generation_hash"]}
    points, _ = backend.query_points(sheet, geo_col)
    assert [(point.lat, point.lon) for point in points] == [(5.0, 6.0), (3.0, 4.0)]
    assert project.get_column(geo_col)["current_run_id"] == run1


def test_running_current_run_is_marked_transient(project):
    # A run pointed at the column but NOT finished -> status 'running'.
    sheet, geo_col, rows, op, run = _seed_ai_run(project, {0: {"lat": 1.0, "lon": 2.0}})
    RunResultStore(project).point_column_at_run(op, geo_col, run)  # no finish_run

    backend = GeoProjectionBackend(project)
    status = backend.materialize_geo_column(sheet, geo_col)
    assert status["transient"] is True

    # Once it finishes, a fresh generation is no longer transient.
    RunResultStore(project).finish_run(run)
    status2 = backend.materialize_geo_column(sheet, geo_col)
    assert status2["transient"] is False
    assert status2["generation_hash"] != status["generation_hash"]


def test_stale_generation_never_served_as_ready(project):
    sheet, geo_col, rows = _seed_source(project)
    backend = GeoProjectionBackend(project)
    s1 = backend.materialize_geo_column(sheet, geo_col)

    # Inject a bogus 'ready' projection for a DIFFERENT generation hash.
    backend.db.execute(
        "INSERT INTO geo_projection_meta "
        "(projection_id, sheet_id, column_id, generation_hash, generation_json, "
        "backend_id, backend_version, status, transient, total_rows, valid_points, "
        "invalid_points) VALUES "
        "('stale-proj', ?, ?, 'sha256:STALE', '{}', 'sqlite-rtree', '1', 'ready', "
        "0, 99, 99, 0)",
        (sheet, geo_col),
    )

    status = backend.materialize_geo_column(sheet, geo_col)
    # Must serve the CURRENT generation, never the injected stale 'ready' row.
    assert status["generation_hash"] == s1["generation_hash"]
    assert status["projection_id"] != "stale-proj"
    assert status["valid_points"] == 2


def test_superseded_generations_are_pruned(project):
    sheet, geo_col, rows = _seed_source(project)
    backend = GeoProjectionBackend(project)
    backend.materialize_geo_column(sheet, geo_col)

    # Change canonical state so a new generation must be built.
    project.apply_edits(
        [{"row_id": rows[2], "column_id": geo_col, "value": {"lat": 5.0, "lon": 6.0}}]
    )
    backend.materialize_geo_column(sheet, geo_col)

    metas = backend.db.execute(
        "SELECT generation_hash, status FROM geo_projection_meta "
        "WHERE sheet_id=? AND column_id=?",
        (sheet, geo_col),
    ).fetchall()
    # Only the current ready generation survives; no stale rows accumulate.
    assert len(metas) == 1
    assert metas[0]["status"] == "ready"
