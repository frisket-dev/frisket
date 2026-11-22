from __future__ import annotations

import json
from pathlib import Path

import pytest

from frisket.actions.imports import UpdateRowsParams
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store import Project


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


def _request(sheet_id: int, params: UpdateRowsParams, key="update-1"):
    return {
        "action_id": "import.update_rows",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": params.model_dump(mode="json"),
        "idempotency_key": key,
    }


def test_exact_update_preserves_rows_and_undoes_as_one_edit_op(make_project) -> None:
    project = make_project("Update")
    sheet_id, columns, row_ids = _seed(project)
    params = _params(
        [
            {"id": "a", "name": "Alicia", "note": ""},
            {"id": "missing", "name": "Nobody", "note": "ignored"},
            {"id": "", "name": "Blank", "note": "ignored"},
        ]
    )

    result = run_action_spec(project, _request(sheet_id, params), project_id="p")
    assert result.status == "completed", result.errors
    op = project.db.execute(
        "SELECT spec FROM changes WHERE id=?", (result.change_ids[0],)
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
    assert project.undo() == result.change_ids[0]
    assert project.get_values(sheet_id, columns["name"])[row_ids[0]] == "Alice"
    assert project.redo() == result.change_ids[0]
    assert project.get_values(sheet_id, columns["name"])[row_ids[0]] == "Alicia"

    # Completed idempotency replay wins even though live target values changed.
    replay = run_action_spec(project, _request(sheet_id, params), project_id="p")
    assert replay.receipt_id == result.receipt_id


def test_updates_use_current_cells_and_late_duplicate_group_is_skipped(
    make_project,
) -> None:
    project = make_project("Stale")
    sheet_id, columns, row_ids = _seed(project)
    params = _params([{"id": "a", "name": "Alicia", "note": "new"}])
    project.apply_edits(
        [{"row_id": row_ids[0], "column_id": columns["note"], "value": "concurrent"}]
    )
    before = project.db.execute("SELECT COUNT(*) FROM edits").fetchone()[0]
    stale = run_action_spec(project, _request(sheet_id, params), project_id="p")
    assert stale.status == "completed"
    assert project.get_values(sheet_id, columns["note"])[row_ids[0]] == "new"
    assert project.db.execute("SELECT COUNT(*) FROM edits").fetchone()[0] == before

    duplicate = _params(
        [
            {"id": "b", "name": "B1", "note": "one"},
            {"id": "b", "name": "B2", "note": "two"},
        ]
    )
    refused = run_action_spec(
        project,
        _request(sheet_id, duplicate, "update-duplicate"),
        project_id="p",
    )
    assert refused.status == "completed"
    assert refused.change_ids == []
    assert project.db.execute("SELECT COUNT(*) FROM edits").fetchone()[0] == before


def test_keep_existing_blank_preserves_current_values(make_project) -> None:
    project = make_project("Blank")
    sheet_id, columns, row_ids = _seed(project)
    params = _params([{"id": "a", "name": "", "note": None}], keep=True)
    result = run_action_spec(project, _request(sheet_id, params), project_id="p")
    assert result.status == "completed"
    assert result.change_ids == []
    assert project.get_values(sheet_id, columns["name"])[row_ids[0]] == "Alice"


@pytest.mark.parametrize(
    ("keep", "changed", "cleared", "after"),
    [
        (False, 3, 2, {"name": None, "note": None}),
        (True, 1, 0, {"name": "Alice", "note": "keep"}),
    ],
)
def test_whitespace_only_imports_are_blank_without_trimming_nonblank_text(
    make_project,
    keep: bool,
    changed: int,
    cleared: int,
    after: dict[str, object],
) -> None:
    project = make_project(f"whitespace-{keep}")
    sheet_id, columns, row_ids = _seed(project)
    params = _params(
        [
            {"id": "a", "name": " \t", "note": "\u00a0"},
            {"id": "\t\u00a0", "name": "ignored", "note": "ignored"},
            {"id": "b", "name": " Bob ", "note": "old"},
        ],
        keep=keep,
    )
    result = run_action_spec(project, _request(sheet_id, params), project_id="p")
    assert result.status == "completed", result.errors
    assert {
        name: project.get_values(sheet_id, columns[name])[row_ids[0]] for name in after
    } == after
    assert project.get_values(sheet_id, columns["name"])[row_ids[1]] == " Bob "


def test_number_keys_use_exact_numeric_semantics_without_text_coercion(
    make_project,
) -> None:
    project = make_project("Numeric")
    sheet_id = project.add_sheet("Numbers")
    columns = {
        "key": project.add_column(sheet_id, "key", "number"),
        "value": project.add_column(sheet_id, "value", "text"),
    }
    row_ids = project.add_rows(sheet_id, [{"key": 1, "value": "old"}], columns)
    params = UpdateRowsParams(
        columns=[{"name": "key", "type": "number"}, {"name": "value", "type": "text"}],
        key_columns=["key"],
        rows=[{"key": 1.0, "value": "new"}],
        update_policy="incoming_wins",
    )
    result = run_action_spec(project, _request(sheet_id, params), project_id="p")
    assert result.status == "completed", result.errors
    assert project.get_values(sheet_id, columns["value"])[row_ids[0]] == "new"


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
        _request(seeded["sheet_id"], params, "temporal-update"),
        project_id="p",
    )
    assert result.status == "failed"
    assert result.errors[0].code == "timeline_not_found"
    assert set(
        project.get_values(seeded["sheet_id"], seeded["columns"]["selection"]).values()
    ) == {None}


def test_custom_rows_updater_uses_same_publication_without_outer_transaction(
    make_project, monkeypatch
):
    from dataclasses import replace
    from frisket.actions.core import RegisteredAction
    from frisket.actions.registry import ACTION_REGISTRY

    project = make_project("custom")
    sheet, columns, rows = _seed(project)
    original = ACTION_REGISTRY.get("import.update_rows")
    custom = RegisteredAction("test.update_people", replace(original.definition))
    monkeypatch.setattr(
        ACTION_REGISTRY,
        "_actions",
        {**ACTION_REGISTRY._actions, custom.action_id: custom},
    )
    request = _request(sheet, _params([{"id": "a", "name": "new"}]), "custom")
    request["action_id"] = custom.action_id
    result = run_action_spec(project, request, project_id="p")
    assert result.status == "completed", result.errors
    assert result.action.kind == custom.action_id
    assert project.get_values(sheet, columns["name"])[rows[0]] == "new"
    assert project.get_values(sheet, columns["note"])[rows[0]] == "keep"
    assert project.undo() == result.change_ids[0]
    assert project.get_values(sheet, columns["name"])[rows[0]] == "Alice"
    replay = run_action_spec(project, request, project_id="p")
    assert replay.receipt_id == result.receipt_id


def test_queued_custom_updater_keeps_original_receipt(make_project, monkeypatch):
    from dataclasses import replace
    from frisket.actions.core import RegisteredAction
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.action_jobs import (
        reserve_typed_action_job,
        run_action_run_job,
    )

    project = make_project("queued-custom")
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
        "queued-custom",
    )
    request["action_id"] = custom.action_id
    envelope = reserve_typed_action_job(project, "p", typed_action_for_request(request))
    result = run_action_run_job(project, {"action_job": envelope.to_json()})
    assert result.status == "completed", result.errors
    assert result.receipt_id == envelope.receipt_id
    assert result.action.kind == custom.action_id
    assert project.get_values(sheet, columns["note"])[rows[0]] == "from worker"


def test_rekey_then_delete_cannot_restore_row_under_old_source_key(make_project):
    project = make_project("hidden-rekey")
    sheet, columns, rows = _seed(project)
    params = _params([{"id": "a", "name": "from source"}])
    params.relationship_id = "import:rekey"
    first = run_action_spec(project, _request(sheet, params, "first"), project_id="p")
    assert first.status == "completed"
    project.apply_edits(
        [{"row_id": rows[0], "column_id": columns["id"], "value": "changed-key"}]
    )
    with project.db:
        project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (rows[0],))
    params.rows = [{"id": "a", "name": "later source"}]
    later = run_action_spec(project, _request(sheet, params, "second"), project_id="p")
    assert later.status == "completed", later.errors
    assert (
        project.db.execute("SELECT hidden FROM rows WHERE id=?", (rows[0],)).fetchone()[
            0
        ]
        == 1
    )
    assert later.change_ids == []


def test_custom_updater_failure_after_publication_keeps_work_and_records_failure(
    make_project, monkeypatch
):
    from dataclasses import replace
    from frisket.actions.core import RegisteredAction
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.engine.store.receipts import ReceiptStore

    project = make_project("failed-handler")
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
            "SELECT publication_closed FROM changes WHERE id=?", (result.change_ids[0],)
        ).fetchone()[0]
        == 1
    )
    assert project.undo() == result.change_ids[0]
    assert project.get_values(sheet, columns["name"])[rows[0]] == "Alice"


def test_large_single_record_exceeds_batch_target_without_new_api_size_limit(
    make_project, monkeypatch
):
    import frisket.engine.jobs.import_table as table_job

    monkeypatch.setattr(table_job, "_PAGE_BYTES", 64)
    project = make_project("large-record")
    sheet, columns, rows = _seed(project)
    value = "Long report " * 100
    params = _params([{"id": "a", "name": value}, {"id": "b", "name": "second"}])
    result = run_action_spec(
        project, _request(sheet, params, "large-record"), project_id="p"
    )
    assert result.status == "completed", result.errors
    assert project.get_values(sheet, columns["name"]) == {
        rows[0]: value,
        rows[1]: "second",
    }


def test_direct_update_publication_failure_closes_its_reserved_receipt(
    make_project, monkeypatch
):
    import frisket.engine.jobs.import_table as table_job

    project = make_project("Failed update")
    sheet_id, columns, row_ids = _seed(project)
    actual = table_job.run_table_import

    def fail_after_committed_page(*args, **kwargs):
        def fail():
            raise OSError("synthetic failure after committed update")

        return actual(*args, **kwargs, on_commit=fail)

    with monkeypatch.context() as patch:
        patch.setattr(table_job, "run_table_import", fail_after_committed_page)
        result = run_action_spec(
            project,
            _request(sheet_id, _params([{"id": "a", "name": "Alicia", "note": "new"}])),
            project_id="p",
        )
    receipt = project.db.execute(
        "SELECT status,body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt is not None and receipt["status"] == result.status == "partial"
    assert "synthetic failure" in result.errors[0].message
    assert project.get_values(sheet_id, columns["name"])[row_ids[0]] == "Alicia"
    assert (
        project.db.execute("SELECT state FROM import_sessions").fetchone()[0] == "kept"
    )
    assert (
        project.db.execute(
            "SELECT publication_closed FROM changes WHERE id=?", (result.change_ids[0],)
        ).fetchone()[0]
        == 1
    )
    replay = run_action_spec(
        project,
        _request(sheet_id, _params([{"id": "a", "name": "Alicia", "note": "new"}])),
        project_id="p",
    )
    assert replay.receipt_id == result.receipt_id and replay.status == "partial"
    next_result = run_action_spec(
        project,
        _request(
            sheet_id, _params([{"id": "a", "name": "Next", "note": "new"}]), key="next"
        ),
        project_id="p",
    )
    assert next_result.status == "completed"


@pytest.mark.parametrize("policy", ["keep_changes", "incoming_wins"])
def test_keyed_import_restores_only_changed_source_and_undo_hides_again(
    make_project, policy
):
    from frisket.engine.store.keyed_updates import observation

    project = make_project("restore")
    sheet, columns, rows = _seed(project)
    params = _params([{"id": "a", "name": "first"}])
    params.relationship_id = "import:restore"
    first = run_action_spec(project, _request(sheet, params, "first"), project_id="p")
    assert first.status == "completed"
    deleted = run_action_spec(
        project,
        {
            "action_id": "row.delete",
            "scope": {"kind": "project"},
            "params": {"sheet_id": sheet, "row_ids": [rows[0]]},
            "idempotency_key": "delete",
        },
        project_id="p",
    )
    assert deleted.status == "completed"
    repeated = run_action_spec(
        project, _request(sheet, params, "repeat"), project_id="p"
    )
    assert repeated.status == "completed" and repeated.change_ids == []
    params.rows = [{"id": "a", "name": "changed"}]
    params.update_policy = policy
    result = run_action_spec(
        project, _request(sheet, params, "changed"), project_id="p"
    )
    assert result.status == "completed", result.errors
    assert (
        observation(project.db, "import:restore", rows[0])[str(columns["name"])]
        == "changed"
    )
    hidden = project.db.execute(
        "SELECT hidden FROM rows WHERE id=?", (rows[0],)
    ).fetchone()[0]
    assert hidden == (policy == "keep_changes")
    if policy == "incoming_wins":
        assert project.get_values(sheet, columns["name"])[rows[0]] == "changed"
        assert project.undo() == result.change_ids[0]
        assert (
            project.db.execute(
                "SELECT hidden FROM rows WHERE id=?", (rows[0],)
            ).fetchone()[0]
            == 1
        )
    else:
        assert result.change_ids == []


def test_keyed_import_rechecks_target_identity_after_indexing(
    make_project, monkeypatch
):
    import frisket.engine.jobs.import_table as table_job

    project = make_project("rekey")
    sheet, columns, rows = _seed(project)
    original = table_job.publish_keyed_page

    def rekey_then_publish(*args, **kwargs):
        project.apply_edits(
            [{"row_id": rows[0], "column_id": columns["id"], "value": "renamed"}]
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(table_job, "publish_keyed_page", rekey_then_publish)
    result = run_action_spec(
        project,
        _request(sheet, _params([{"id": "a", "name": "must not land"}])),
        project_id="p",
    )
    assert result.status == "completed", result.errors
    assert project.get_values(sheet, columns["name"])[rows[0]] == "Alice"
    assert (
        project.db.execute("SELECT COUNT(*) FROM keyed_observations").fetchone()[0] == 0
    )
