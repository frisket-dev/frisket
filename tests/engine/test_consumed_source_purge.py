"""Purge removes consumed input material without deleting independent results."""

from frisket.engine.store import Project
from frisket.engine.store.changes import start_change
from frisket.engine.store.consumed_sources import (
    CONSUMED_SOURCE_SCHEMA_SQL,
    bundle_state,
    purge_consumed_sources,
    read_consumed_source,
    store_capture,
    verify_bundle,
)
from frisket.engine.store.column_descriptors import DESCRIPTOR_SCHEMA_SQL
from frisket.engine.store.runs import RunResultStore


def test_purge_forgets_capture_but_retains_independent_output(tmp_path):
    project = Project.create(tmp_path / "capture.frisket")
    try:
        # Also executable as a dependency test before composed schema wiring.
        project.db.executescript(CONSUMED_SOURCE_SCHEMA_SQL + DESCRIPTOR_SCHEMA_SQL)
        source_sheet = project.add_sheet("source")
        source_column = project.add_column(source_sheet, "text")
        [source_row] = project.add_rows(
            source_sheet, [{"text": "private input"}], {"text": source_column}
        )
        output_sheet = project.add_sheet("output")
        output_column = project.add_column(output_sheet, "saved")
        [output_row] = project.add_rows(
            output_sheet, [{"saved": "independent answer"}], {"saved": output_column}
        )
        _, _, capture = read_consumed_source(project, source_sheet, [source_column])
        owner = project.append_op("test.capture", {})
        run_id = RunResultStore(project).start_run(owner, output_sheet, "test.capture")
        project.db.execute("BEGIN IMMEDIATE")
        start_change(project.db, owner)
        bundle_id = store_capture(
            project,
            capture,
            owner_op_id=owner,
            output_sheet_id=output_sheet,
            run_id=run_id,
            row_id=output_row,
        )
        project.db.commit()
        assert verify_bundle(project, bundle_id) == "fresh"

        project.db.execute("BEGIN IMMEDIATE")
        affected_runs, changed = purge_consumed_sources(
            project.db, row_ids=[source_row], limit=1
        )
        project.db.commit()
        assert affected_runs == [run_id]
        assert changed == 1
        assert bundle_state(project, bundle_id) == "unknown"
        assert verify_bundle(project, bundle_id) == "unknown"
        assert (
            project.get_values(output_sheet, output_column)[output_row]
            == "independent answer"
        )
        saved = project.db.execute(
            "SELECT capture_json FROM consumed_source_bundles WHERE id=?", (bundle_id,)
        ).fetchone()[0]
        assert saved == '{"kind":"purged","schema_version":1}'
        assert (
            project.db.execute(
                "SELECT count(*) FROM consumed_source_columns WHERE bundle_id=?",
                (bundle_id,),
            ).fetchone()[0]
            == 0
        )
        project.db.execute("BEGIN IMMEDIATE")
        assert purge_consumed_sources(
            project.db, column_ids=[source_column], limit=1
        ) == ([], 0)
        project.db.commit()
    finally:
        project.close()
