"""Public table actions must retain their actual consumed source capture."""

from contextlib import closing

from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.staleness import sheet_freshness
from frisket.engine.store.op_log import step_operation


def _assert_captured(project, result):
    assert result.status == "completed", result.errors
    sheet = next(output.sheet_id for output in result.outputs if output.kind == "sheet")
    count = project.db.execute(
        "SELECT count(*) FROM consumed_source_bundles WHERE output_sheet_id=?", (sheet,)
    ).fetchone()[0]
    assert count > 0, "successful source-backed table has no consumed input capture"
    assert sheet_freshness(project, sheet) == "synced"
    return sheet


def _assert_equal_contribution_roundtrip(project, output, sheet, column, row):
    unrelated = project.add_column(sheet, "unrelated")
    project.apply_edits([{"row_id": row, "column_id": unrelated, "value": "unrelated"}])
    assert sheet_freshness(project, output) == "synced"
    value = project.get_values(sheet, column)[row]
    edit = project.apply_edits([{"row_id": row, "column_id": column, "value": value}])
    assert sheet_freshness(project, output) == "stale"
    with project.transaction_scope():
        step_operation(project, "revert", op_id=edit)
    assert sheet_freshness(project, output) == "synced"


def test_transcript_segments_capture_actual_transcript_source(tmp_path):
    from tests.engine.test_transcript_segments_action import (
        _seed_timestamped_transcript,
        _run,
    )

    with closing(Project.create(tmp_path / "transcript.frisket")) as project:
        source = _seed_timestamped_transcript(project)
        result = _run(
            project,
            source,
            selection={"kind": "draft_points", "items": [{"at_ms": 2500}]},
            target="Segments",
            key="segments-capture",
        )
        output = _assert_captured(project, result)
        _assert_equal_contribution_roundtrip(
            project,
            output,
            source["sheet_id"],
            source["transcript_column_id"],
            source["row_id"],
        )


def test_positioned_document_table_keeps_nested_sheet_reader_capture(tmp_path):
    from tests.engine.test_visual_document_extraction_action import seed, request

    with closing(Project.create(tmp_path / "documents.frisket")) as project:
        sheet, column, rows, template = seed(project, count=1)
        result = run_action_spec(
            project, request(sheet, template), project_id="capture"
        )
        output = _assert_captured(project, result)
        _assert_equal_contribution_roundtrip(project, output, sheet, column, rows[0])


def test_semantic_link_table_captures_both_actual_sides(tmp_path):
    from tests.engine.test_derive_link_table_executor import _seed, _derive_link_action

    with closing(Project.create(tmp_path / "semantic.frisket")) as project:
        source = _seed(project, tmp_path)
        result = run_action_spec(
            project,
            _derive_link_action(source["join_receipt_id"]),
            project_id="capture",
        )
        output = _assert_captured(project, result)
        company = next(
            c["id"]
            for c in project.columns(source["target_sheet_id"])
            if c["name"] == "company"
        )
        _assert_equal_contribution_roundtrip(
            project,
            output,
            source["target_sheet_id"],
            company,
            source["target_row_ids"][0],
        )


def test_entity_table_captures_actual_cluster_source(tmp_path):
    from tests.engine.test_cluster_receipt_reader import _request

    with closing(Project.create(tmp_path / "entities.frisket")) as project:
        sheet = project.add_sheet("Names")
        column = project.add_column(sheet, "name")
        rows = project.add_rows(
            sheet, [{"name": "Jon Smith"}, {"name": "Smith Jon"}], {"name": column}
        )
        clusters = run_action_spec(
            project,
            {
                "action_id": "cluster.values",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet},
                "params": {"source": "name", "method": "fingerprint"},
                "idempotency_key": "capture-clusters",
            },
            project_id="capture",
        )
        assert clusters.status == "completed", clusters.errors
        result = run_action_spec(
            project, _request(clusters.receipt_id), project_id="capture"
        )
        output = _assert_captured(project, result)
        _assert_equal_contribution_roundtrip(project, output, sheet, column, rows[0])


def test_semantic_capture_tracks_scoped_effective_judgment_but_value_only_capture_does_not(
    tmp_path,
):
    from tests.engine.test_derive_link_table_executor import _seed, _derive_link_action
    from frisket.engine.store.consumed_sources import (
        read_consumed_source,
        store_capture,
        bundle_state,
    )

    with closing(Project.create(tmp_path / "judgments.frisket")) as project:
        source = _seed(project, tmp_path)
        sheet, row, outside = (
            source["source_sheet_id"],
            source["source_row_ids"][0],
            source["source_row_ids"][1],
        )
        column = next(
            c["id"]
            for c in project.columns(sheet)
            if c["name"] == "company_match_row_id"
        )
        run = project.db.execute(
            "SELECT run_id FROM receipts WHERE id=?", (source["join_receipt_id"],)
        ).fetchone()[0]

        def decide(row_id, key):
            result = run_action_spec(
                project,
                {
                    "action_id": "review.decision",
                    "scope": {"kind": "project"},
                    "params": {
                        "run_id": run,
                        "row_id": row_id,
                        "column_id": column,
                        "decision": "accept",
                    },
                    "idempotency_key": key,
                },
                project_id="judgments",
            )
            assert result.status == "completed", result.errors
            return result.op_ids[0]

        first = decide(row, "first")
        request = _derive_link_action(
            source["join_receipt_id"], row_ids=[row], source_sheet_id=sheet
        )
        result = run_action_spec(project, request, project_id="capture")
        output = _assert_captured(project, result)
        _, _, value_capture = read_consumed_source(
            project, sheet, [column], row_ids=[row], scope_intent="selected"
        )
        with project.transaction_scope():
            value_bundle = store_capture(
                project,
                value_capture,
                owner_op_id=result.op_ids[0],
                output_sheet_id=sheet,
            )
        decide(outside, "outside")
        assert sheet_freshness(project, output) == "synced"
        second = decide(row, "same-decision-new-owner")
        assert sheet_freshness(project, output) == "stale"
        assert bundle_state(project, value_bundle) == "fresh"
        with project.transaction_scope():
            step_operation(project, "revert", op_id=second)
        assert sheet_freshness(project, output) == "synced"
        with project.transaction_scope():
            step_operation(project, "revert", op_id=first)
        assert sheet_freshness(project, output) == "stale"
        with project.transaction_scope():
            step_operation(project, "restore", op_id=first)
        assert sheet_freshness(project, output) == "synced"
