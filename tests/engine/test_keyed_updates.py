from contextlib import closing

import pytest

from frisket.engine.store import Project
from frisket.engine.store.keyed_updates import (
    ValidatedKeyedInput,
    changed_fields,
    observe,
    observation,
    require_relationship,
    retained_hidden_targets,
    typed_key,
)


def test_full_domain_validation_collapses_identical_and_skips_conflicting_keys():
    with ValidatedKeyedInput() as staged:
        staged.stage(
            [("a", {"value": True}), ("b", {"value": "safe"}), ("a", {"value": True})]
        )
        with pytest.raises(ValueError, match="incomplete"):
            list(staged.records())
        staged.stage(
            [("a", {"value": 1}), ("missing", {}), ("missing", {"value": None})]
        )
        staged.seal()
        records = list(staged.records())
        assert [(item.key, item.conflicting, item.copies) for item in records] == [
            ("a", True, 3),
            ("b", False, 1),
            ("missing", True, 2),
        ]
        assert records[0].record == {"value": True}
        with pytest.raises(ValueError, match="sealed"):
            staged.stage([])


def test_keys_and_fields_are_typed_and_omission_is_not_blank():
    assert (
        len(
            {
                typed_key([("boolean", True)]),
                typed_key([("integer", 1)]),
                typed_key([("text", "1")]),
            }
        )
        == 3
    )
    assert typed_key([("text", "  ")]) is None
    assert changed_fields({"x": 1}, {"x": True}, {"x": 1}, "keep_changes") == {
        "x": True
    }
    assert changed_fields({"x": "edit"}, {}, {"x": "old"}, "incoming_wins") == {}
    assert (
        changed_fields({"x": "edit"}, {"x": None}, {"x": "old"}, "keep_changes") == {}
    )
    assert changed_fields(
        {"x": "edit"}, {"x": None}, {"x": "old"}, "incoming_wins"
    ) == {"x": None}
    assert (
        changed_fields({"x": "edit"}, {"x": "old"}, {"x": "old"}, "incoming_wins") == {}
    )
    assert changed_fields({"x": "existing"}, {"x": "first"}, None, "keep_changes") == {}


def _project(tmp_path):
    project = Project.create(tmp_path / "keyed.frisket")
    sheet = project.add_sheet("People")
    col = project.add_column(sheet, "id", "text")
    [row] = project.add_rows(sheet, [{"id": "a"}], {"id": col})
    op = project.append_op("import.update_rows")
    with project.db:
        project.db.execute("BEGIN IMMEDIATE")
        require_relationship(
            project.db,
            relationship_id="test:baseline",
            sheet_id=sheet,
            key_definition=[(col, "text")],
        )
    return project, sheet, col, row, op


def test_observations_advance_when_kept_and_do_not_rewind_with_op_status(tmp_path):
    project, sheet, col, row, op = _project(tmp_path)
    with closing(project):
        with project.db:
            project.db.execute("BEGIN IMMEDIATE")
            observe(
                project.db,
                relationship_id="test:baseline",
                row_id=row,
                key=typed_key([("text", "a")]),
                incoming={"name": "first", "note": "retained"},
            )
        baseline = observation(project.db, "test:baseline", row)
        assert (
            changed_fields(
                {"name": "manual"}, {"name": "second"}, baseline, "keep_changes"
            )
            == {}
        )
        with project.db:
            project.db.execute("BEGIN IMMEDIATE")
            observe(
                project.db,
                relationship_id="test:baseline",
                row_id=row,
                key=typed_key([("text", "a")]),
                incoming={"name": "second"},
            )
            project.db.execute("UPDATE ops SET status='undone' WHERE id=?", (op,))
        assert observation(project.db, "test:baseline", row) == {
            "name": "second",
            "note": "retained",
        }
        assert (
            changed_fields(
                {"name": "manual"},
                {"name": "second"},
                observation(project.db, "test:baseline", row),
                "incoming_wins",
            )
            == {}
        )
        project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (row,))
        assert retained_hidden_targets(
            project.db, "test:baseline", typed_key([("text", "a")])
        ) == [row]
        project.db.execute("UPDATE rows SET hidden=0 WHERE id=?", (row,))
        assert (
            retained_hidden_targets(
                project.db, "test:baseline", typed_key([("text", "a")])
            )
            == []
        )


def test_import_update_uses_baseline_and_skips_whole_conflicting_key(tmp_path):
    from frisket.actions.imports import UpdateRowsParams
    from frisket.engine.executor.import_update import plan_import_update
    from frisket.engine.executor.actions import run_action_spec
    from frisket.features.temporal_ingress import preflight_typed_rows_for_persistence

    project, sheet, key_col, row, op = _project(tmp_path)
    with closing(project):
        value_col = project.add_column(sheet, "value", "text")
        project.apply_edits(
            [{"row_id": row, "column_id": value_col, "value": "original"}]
        )

        def run(records, policy="keep_changes", relationship=None):
            params = UpdateRowsParams(
                columns=[
                    {"name": "id", "type": "text"},
                    {"name": "value", "type": "text"},
                ],
                key_columns=["id"],
                rows=records,
                update_policy=policy,
                relationship_id=relationship,
            )
            plan = plan_import_update(
                project,
                sheet_id=sheet,
                columns=params.columns,
                key_columns=params.key_columns,
                rows=params.rows,
                keep_existing_on_blank=False,
                preflight_rows=preflight_typed_rows_for_persistence,
                update_policy=policy,
                relationship_id=relationship,
            )
            result = run_action_spec(
                project,
                {
                    "action_id": "import.update_rows",
                    "scope": {"kind": "sheet_rows", "sheet_id": sheet},
                    "params": params.model_dump(mode="json"),
                    "idempotency_key": f"test-{project.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0]}",
                    "confirmation": plan.confirmation,
                },
                project_id="p",
            )
            assert result.status == "completed", result.errors
            return plan, result

        first, result = run([{"id": "a", "value": "first"}], relationship="import:test")
        assert first.changed_cells == 0
        assert project.get_values(sheet, value_col, [row])[row] == "original"
        plan, result = run(
            [{"id": "a", "value": "second"}],
            policy="incoming_wins",
            relationship="import:test",
        )
        assert plan.changed_cells == 1
        assert project.get_values(sheet, value_col, [row])[row] == "second"
        project.apply_edits(
            [{"row_id": row, "column_id": value_col, "value": "my edit"}]
        )
        plan, result = run([{"id": "a", "value": "third"}], relationship="import:test")
        assert plan.changed_cells == 0
        assert observation(project.db, "import:test", row)[str(value_col)] == "third"
        plan, result = run(
            [{"id": "a", "value": "third"}],
            policy="incoming_wins",
            relationship="import:test",
        )
        assert plan.changed_cells == 0
        plan, result = run(
            [{"id": "a", "value": "fourth"}, {"id": "a", "value": "conflict"}],
            policy="incoming_wins",
            relationship="import:test",
        )
        assert plan.ambiguous == 2
        assert observation(project.db, "import:test", row)[str(value_col)] == "third"
        assert project.get_values(sheet, value_col, [row])[row] == "my edit"


def test_source_poll_updates_same_row_and_preserves_manual_changes(tmp_path):
    from frisket.engine.executor.source_poll_action import (
        _materialize_registered_source_poll_in_txn,
    )
    from frisket.engine.store.sources import SourceStore
    from frisket.server.sources.runtime import SourcePollItem, SourcePollResult

    project, sheet, col, row, op = _project(tmp_path)
    with closing(project):
        sources = SourceStore(project)
        source = sources.add_source("Test", kind="rss", sheet_id=sheet)

        def poll(items):
            run = sources.start_source_run(source)
            with project.db:
                project.db.execute("BEGIN IMMEDIATE")
                return _materialize_registered_source_poll_in_txn(
                    project,
                    project.db.cursor(),
                    source_before=dict(sources.get_source(source)),
                    source_run_id=run,
                    poll_result=SourcePollResult(items=items),
                )

        first = poll([SourcePollItem("item-a", {"title": "first"})])
        [target] = first["row_ids"]
        second = poll([SourcePollItem("item-a", {"title": "second"})])
        assert second["new_rows"] == 0
        assert second["row_ids"] == [target]
        title = int(
            project.db.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name='title'", (sheet,)
            ).fetchone()[0]
        )
        assert project.get_values(sheet, title, [target])[target] == "second"
        project.apply_edits(
            [{"row_id": target, "column_id": title, "value": "my correction"}]
        )
        third = poll([SourcePollItem("item-a", {"title": "third"})])
        assert third["row_ids"] == []
        assert project.get_values(sheet, title, [target])[target] == "my correction"
        baseline = observation(project.db, f"source:{source}", target)
        assert baseline[str(title)] == "third"
        duplicate = poll(
            [
                SourcePollItem("item-a", {"title": "fourth"}),
                SourcePollItem("item-a", {"title": "conflict"}),
            ]
        )
        assert duplicate["row_ids"] == []
        assert duplicate["skipped_rows"] == 2
        assert observation(project.db, f"source:{source}", target) == baseline
        sources.update_source(source, config={"update_policy": "incoming_wins"})
        latest = poll([SourcePollItem("item-a", {"title": "latest"})])
        assert latest["row_ids"] == [target]
        assert project.get_values(sheet, title, [target])[target] == "latest"
        assert project.undo() == latest["op_id"]
        assert project.get_values(sheet, title, [target])[target] == "my correction"
        repeated = poll([SourcePollItem("item-a", {"title": "latest"})])
        assert repeated["row_ids"] == []
        assert repeated["op_id"] is None
        sources.update_source(source, config={"update_policy": "keep_changes"})
        # A hidden association cannot steal a newly visible current-key row.
        project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (target,))
        project.db.commit()
        cols = {
            str(c["name"]): int(c["id"])
            for c in project.db.execute(
                "SELECT id,name FROM columns WHERE sheet_id=?", (sheet,)
            )
        }
        [replacement] = project.add_rows(
            sheet,
            [{"source_id": source, "source_item_id": "item-a", "title": "new target"}],
            cols,
        )
        replaced = poll([SourcePollItem("item-a", {"title": "third"})])
        assert replaced["new_rows"] == 0
        assert (
            observation(project.db, f"source:{source}", replacement)[str(title)]
            == "third"
        )
        assert (
            project.get_values(sheet, title, [replacement])[replacement] == "new target"
        )
