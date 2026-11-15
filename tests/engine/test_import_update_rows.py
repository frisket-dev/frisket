from __future__ import annotations

import json
from pathlib import Path

import pytest

from frisket.actions.imports import UpdateRowsParams
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.executor.import_update import plan_import_update
from frisket.engine.store import Project
from frisket.features.temporal_ingress import preflight_typed_rows_for_persistence


def _seed(project: Project) -> tuple[int, dict[str, int], list[int]]:
    sheet_id = project.add_sheet("People")
    columns = {
        "id": project.add_column(sheet_id, "id", "text"),
        "name": project.add_column(sheet_id, "name", "text"),
        "note": project.add_column(sheet_id, "note", "text"),
    }
    row_ids = project.add_rows(
        sheet_id,
        [
            {"id": "a", "name": "Alice", "note": "keep"},
            {"id": "b", "name": "Bob", "note": "old"},
        ],
        columns,
    )
    return sheet_id, columns, row_ids


def _params(rows, *, keep=False) -> UpdateRowsParams:
    return UpdateRowsParams(
        columns=[
            {"name": "id", "type": "text"},
            {"name": "name", "type": "text"},
            {"name": "note", "type": "text"},
        ],
        key_columns=["id"],
        update_policy="incoming_wins",
        keep_existing_on_blank=keep,
        rows=rows,
        source={"kind": "inline", "label": "paste", "fingerprint": "sha256:test"},
    )


def _request(
    sheet_id: int, params: UpdateRowsParams, confirmation: str, key="update-1"
):
    return {
        "action_id": "import.update_rows",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": params.model_dump(mode="json"),
        "idempotency_key": key,
        "confirmation": confirmation,
    }


def test_exact_update_preserves_rows_and_undoes_as_one_edit_op(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "update.frisket", name="Update")
    sheet_id, columns, row_ids = _seed(project)
    params = _params(
        [
            {"id": "a", "name": "Alicia", "note": ""},
            {"id": "missing", "name": "Nobody", "note": "ignored"},
            {"id": "", "name": "Blank", "note": "ignored"},
        ]
    )
    preview = plan_import_update(
        project,
        sheet_id=sheet_id,
        columns=params.columns,
        key_columns=params.key_columns,
        rows=params.rows,
        keep_existing_on_blank=False,
        preflight_rows=preflight_typed_rows_for_persistence,
        update_policy="incoming_wins",
    )
    assert (
        preview.matched,
        preview.unmatched,
        preview.blank_keys,
        preview.ambiguous,
    ) == (1, 2, 1, 0)
    assert (preview.changed_cells, preview.cleared_cells) == (2, 1)

    result = run_action_spec(
        project, _request(sheet_id, params, preview.confirmation), project_id="p"
    )
    assert result.status == "completed", result.errors
    op = project.db.execute(
        "SELECT spec FROM ops WHERE id=?", (result.op_ids[0],)
    ).fetchone()
    op_spec = json.loads(op["spec"])
    assert op_spec["scope"] == {"kind": "sheet_rows", "sheet_id": sheet_id}
    assert op_spec["params"]["row_count"] == 3
    assert "rows" not in op_spec["params"]
    assert [
        row["id"]
        for row in project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY id", (sheet_id,)
        )
    ] == row_ids
    assert project.get_values(sheet_id, columns["name"])[row_ids[0]] == "Alicia"
    assert project.get_values(sheet_id, columns["note"])[row_ids[0]] is None
    assert project.get_values(sheet_id, columns["name"])[row_ids[1]] == "Bob"
    assert project.undo() == result.op_ids[0]
    assert project.get_values(sheet_id, columns["name"])[row_ids[0]] == "Alice"
    assert project.redo() == result.op_ids[0]
    assert project.get_values(sheet_id, columns["name"])[row_ids[0]] == "Alicia"

    # Completed idempotency replay wins even though live target values changed.
    replay = run_action_spec(
        project, _request(sheet_id, params, preview.confirmation), project_id="p"
    )
    assert replay.receipt_id == result.receipt_id


def test_updates_use_current_cells_and_late_duplicate_group_is_skipped(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "stale.frisket", name="Stale")
    sheet_id, columns, row_ids = _seed(project)
    params = _params([{"id": "a", "name": "Alicia", "note": "new"}])
    preview = plan_import_update(
        project,
        sheet_id=sheet_id,
        columns=params.columns,
        key_columns=params.key_columns,
        rows=params.rows,
        keep_existing_on_blank=False,
        preflight_rows=preflight_typed_rows_for_persistence,
        update_policy="incoming_wins",
    )
    project.apply_edits(
        [{"row_id": row_ids[0], "column_id": columns["note"], "value": "concurrent"}]
    )
    before = project.db.execute("SELECT COUNT(*) FROM edits").fetchone()[0]
    stale = run_action_spec(
        project, _request(sheet_id, params, preview.confirmation), project_id="p"
    )
    assert stale.status == "completed"
    assert project.get_values(sheet_id, columns["note"])[row_ids[0]] == "new"
    assert project.db.execute("SELECT COUNT(*) FROM edits").fetchone()[0] == before

    duplicate = _params(
        [
            {"id": "b", "name": "B1", "note": "one"},
            {"id": "b", "name": "B2", "note": "two"},
        ]
    )
    duplicate_preview = plan_import_update(
        project,
        sheet_id=sheet_id,
        columns=duplicate.columns,
        key_columns=duplicate.key_columns,
        rows=duplicate.rows,
        keep_existing_on_blank=False,
        preflight_rows=preflight_typed_rows_for_persistence,
        update_policy="incoming_wins",
    )
    assert duplicate_preview.ambiguous == 2
    refused = run_action_spec(
        project,
        _request(
            sheet_id, duplicate, duplicate_preview.confirmation, "update-duplicate"
        ),
        project_id="p",
    )
    assert refused.status == "completed"
    assert refused.op_ids == []
    assert project.db.execute("SELECT COUNT(*) FROM edits").fetchone()[0] == before


def test_keep_existing_blank_is_bound_and_previewed_as_retained(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "blank.frisket", name="Blank")
    sheet_id, columns, row_ids = _seed(project)
    params = _params([{"id": "a", "name": "", "note": None}], keep=True)
    preview = plan_import_update(
        project,
        sheet_id=sheet_id,
        columns=params.columns,
        key_columns=params.key_columns,
        rows=params.rows,
        keep_existing_on_blank=True,
        preflight_rows=preflight_typed_rows_for_persistence,
        update_policy="incoming_wins",
    )
    assert preview.changed_cells == 0
    assert preview.samples[0]["after"] == {"name": "Alice", "note": "keep"}
    result = run_action_spec(
        project, _request(sheet_id, params, preview.confirmation), project_id="p"
    )
    assert result.status == "completed"
    assert result.op_ids == []
    assert project.get_values(sheet_id, columns["name"])[row_ids[0]] == "Alice"


@pytest.mark.parametrize(
    ("keep", "changed", "cleared", "after"),
    [
        (False, 3, 2, {"name": None, "note": None}),
        (True, 1, 0, {"name": "Alice", "note": "keep"}),
    ],
)
def test_whitespace_only_imports_are_blank_without_trimming_nonblank_text(
    tmp_path: Path,
    keep: bool,
    changed: int,
    cleared: int,
    after: dict[str, object],
) -> None:
    project = Project.create(tmp_path / f"whitespace-{keep}.frisket", name="Whitespace")
    sheet_id, _columns, _row_ids = _seed(project)
    params = _params(
        [
            {"id": "a", "name": " \t", "note": "\u00a0"},
            {"id": "\t\u00a0", "name": "ignored", "note": "ignored"},
            {"id": "b", "name": " Bob ", "note": "old"},
        ],
        keep=keep,
    )

    preview = plan_import_update(
        project,
        sheet_id=sheet_id,
        columns=params.columns,
        key_columns=params.key_columns,
        rows=params.rows,
        keep_existing_on_blank=keep,
        preflight_rows=preflight_typed_rows_for_persistence,
        update_policy="incoming_wins",
    )

    assert (preview.matched, preview.unmatched, preview.blank_keys) == (2, 1, 1)
    assert (preview.changed_cells, preview.cleared_cells) == (changed, cleared)
    assert preview.samples[0]["after"] == after
    assert preview.samples[2]["after"]["name"] == " Bob "


def test_number_keys_use_exact_numeric_semantics_without_text_coercion(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "numeric.frisket", name="Numeric")
    sheet_id = project.add_sheet("Numbers")
    columns = {
        "key": project.add_column(sheet_id, "key", "number"),
        "value": project.add_column(sheet_id, "value", "text"),
    }
    project.add_rows(sheet_id, [{"key": 1, "value": "old"}], columns)
    params = UpdateRowsParams(
        columns=[{"name": "key", "type": "number"}, {"name": "value", "type": "text"}],
        key_columns=["key"],
        rows=[{"key": 1.0, "value": "new"}],
    )
    preview = plan_import_update(
        project,
        sheet_id=sheet_id,
        columns=params.columns,
        key_columns=params.key_columns,
        rows=params.rows,
        keep_existing_on_blank=False,
        preflight_rows=preflight_typed_rows_for_persistence,
        update_policy="incoming_wins",
    )
    assert preview.matched == 1


def test_update_temporal_value_requires_live_project_anchor(tmp_path: Path) -> None:
    from tests.engine.test_temporal_persistence_ingress import (
        _range_value,
        _seed_project,
    )

    seeded = _seed_project(tmp_path, "update-temporal")
    project = seeded["project"]
    bad = _range_value(seeded["lease"].anchor.wire_value())
    bad["timeline"]["artifact_stable_id"] = (
        "source_artifact:00000000-0000-4000-8000-000000000011"
    )
    params = UpdateRowsParams(
        columns=[
            {"name": "label", "type": "text"},
            {"name": "selection", "type": "timeline_range"},
        ],
        key_columns=["label"],
        rows=[{"label": "first", "selection": bad}],
    )
    result = run_action_spec(
        project,
        _request(seeded["sheet_id"], params, "reviewed-but-invalid", "temporal-update"),
        project_id="p",
    )
    assert result.status == "failed"
    assert result.errors[0].code == "timeline_not_found"
    assert set(
        project.get_values(seeded["sheet_id"], seeded["columns"]["selection"]).values()
    ) == {None}


def test_custom_rows_updater_uses_same_publication_without_outer_transaction(
    tmp_path, monkeypatch
):
    from dataclasses import replace
    from frisket.actions.core import RegisteredAction
    from frisket.actions.registry import ACTION_REGISTRY

    project = Project.create(tmp_path / "custom.frisket")
    sheet, columns, rows = _seed(project)
    original = ACTION_REGISTRY.get("import.update_rows")
    custom = RegisteredAction("test.update_people", replace(original.definition))
    monkeypatch.setattr(
        ACTION_REGISTRY,
        "_actions",
        {**ACTION_REGISTRY._actions, custom.action_id: custom},
    )
    request = _request(
        sheet, _params([{"id": "a", "name": "new"}]), "reviewed", "custom"
    )
    request["action_id"] = custom.action_id
    result = run_action_spec(project, request, project_id="p")
    assert result.status == "completed", result.errors
    assert result.action.kind == custom.action_id
    assert project.get_values(sheet, columns["name"])[rows[0]] == "new"
    assert project.get_values(sheet, columns["note"])[rows[0]] == "keep"
    assert project.undo() == result.op_ids[0]
    assert project.get_values(sheet, columns["name"])[rows[0]] == "Alice"
    replay = run_action_spec(project, request, project_id="p")
    assert replay.receipt_id == result.receipt_id


def test_queued_custom_updater_keeps_original_receipt(tmp_path, monkeypatch):
    from dataclasses import replace
    from frisket.actions.core import RegisteredAction
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.action_jobs import (
        reserve_typed_action_job,
        run_action_run_job,
    )

    project = Project.create(tmp_path / "queued-custom.frisket")
    sheet, columns, rows = _seed(project)
    original = ACTION_REGISTRY.get("import.update_rows")
    custom = RegisteredAction("test.queued_update", replace(original.definition))
    monkeypatch.setattr(
        ACTION_REGISTRY,
        "_actions",
        {**ACTION_REGISTRY._actions, custom.action_id: custom},
    )
    request = _request(
        sheet,
        _params([{"id": "a", "note": "from worker"}]),
        "reviewed",
        "queued-custom",
    )
    request["action_id"] = custom.action_id
    envelope = reserve_typed_action_job(project, "p", typed_action_for_request(request))
    result = run_action_run_job(project, {"action_job": envelope.to_json()})
    assert result.status == "completed", result.errors
    assert result.receipt_id == envelope.receipt_id
    assert result.action.kind == custom.action_id
    assert project.get_values(sheet, columns["note"])[rows[0]] == "from worker"


def test_rekey_then_delete_cannot_restore_row_under_old_source_key(tmp_path):
    project = Project.create(tmp_path / "hidden-rekey.frisket")
    sheet, columns, rows = _seed(project)
    params = _params([{"id": "a", "name": "from source"}])
    params.relationship_id = "import:rekey"
    first = run_action_spec(
        project, _request(sheet, params, "reviewed", "first"), project_id="p"
    )
    assert first.status == "completed"
    project.apply_edits(
        [{"row_id": rows[0], "column_id": columns["id"], "value": "changed-key"}]
    )
    with project.db:
        project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (rows[0],))
    params.rows = [{"id": "a", "name": "later source"}]
    later = run_action_spec(
        project, _request(sheet, params, "reviewed", "second"), project_id="p"
    )
    assert later.status == "completed", later.errors
    assert (
        project.db.execute("SELECT hidden FROM rows WHERE id=?", (rows[0],)).fetchone()[
            0
        ]
        == 1
    )
    assert later.op_ids == []


def test_custom_updater_failure_after_publication_keeps_work_and_records_failure(
    tmp_path, monkeypatch
):
    from dataclasses import replace
    from frisket.actions.core import RegisteredAction
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.engine.store.receipts import ReceiptStore

    project = Project.create(tmp_path / "failed-handler.frisket")
    sheet, columns, rows = _seed(project)
    original = ACTION_REGISTRY.get("import.update_rows")

    def fail_after_update(params, updater):
        original.definition.run.handler(params, updater)
        stored = ReceiptStore(project).find_by_idempotency_key("fail-after-update")
        assert stored.status == "running"
        from filelock import Timeout
        from frisket.engine.store.import_intake import (
            import_intake_dir,
            import_worker_lock,
        )

        session = project.db.execute(
            "SELECT id FROM import_sessions WHERE receipt_id=?", (stored.id,)
        ).fetchone()[0]
        with (
            pytest.raises(Timeout),
            import_worker_lock(import_intake_dir(project.path, session), timeout=0),
        ):
            pass
        raise RuntimeError("plugin postprocessing failed")

    custom = RegisteredAction(
        "test.failed_update",
        replace(
            original.definition,
            run=replace(original.definition.run, handler=fail_after_update),
        ),
    )
    monkeypatch.setattr(
        ACTION_REGISTRY,
        "_actions",
        {**ACTION_REGISTRY._actions, custom.action_id: custom},
    )
    request = _request(
        sheet,
        _params([{"id": "a", "name": "published"}]),
        "reviewed",
        "fail-after-update",
    )
    request["action_id"] = custom.action_id
    result = run_action_spec(project, request, project_id="p")
    assert result.status == "partial", result.errors
    assert "plugin postprocessing failed" in result.errors[0].message
    assert project.get_values(sheet, columns["name"])[rows[0]] == "published"
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert receipt.status == "partial"
    assert (
        project.db.execute(
            "SELECT publication_closed FROM ops WHERE id=?", (result.op_ids[0],)
        ).fetchone()[0]
        == 1
    )
    assert project.undo() == result.op_ids[0]
    assert project.get_values(sheet, columns["name"])[rows[0]] == "Alice"


def test_large_single_record_exceeds_batch_target_without_new_api_size_limit(
    tmp_path, monkeypatch
):
    import frisket.engine.jobs.import_table as table_job

    monkeypatch.setattr(table_job, "_PAGE_BYTES", 64)
    project = Project.create(tmp_path / "large-record.frisket")
    sheet, columns, rows = _seed(project)
    value = "Long report " * 100
    params = _params([{"id": "a", "name": value}, {"id": "b", "name": "second"}])
    result = run_action_spec(
        project, _request(sheet, params, "reviewed", "large-record"), project_id="p"
    )
    assert result.status == "completed", result.errors
    assert project.get_values(sheet, columns["name"]) == {
        rows[0]: value,
        rows[1]: "second",
    }
