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


def test_stale_preview_and_late_duplicate_write_nothing(tmp_path: Path) -> None:
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
    )
    project.apply_edits(
        [{"row_id": row_ids[0], "column_id": columns["note"], "value": "concurrent"}]
    )
    before = project.db.execute("SELECT COUNT(*) FROM edits").fetchone()[0]
    stale = run_action_spec(
        project, _request(sheet_id, params, preview.confirmation), project_id="p"
    )
    assert stale.status == "failed"
    assert stale.errors[0].code == "stale_import_preview"
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
    )
    assert duplicate_preview.ambiguous == 2
    refused = run_action_spec(
        project,
        _request(
            sheet_id, duplicate, duplicate_preview.confirmation, "update-duplicate"
        ),
        project_id="p",
    )
    assert refused.status == "failed"
    assert refused.errors[0].code == "ambiguous_import_keys"
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
    )
    assert preview.changed_cells == 0
    assert preview.samples[0]["after"] == {"name": "Alice", "note": "keep"}
    wrong_policy = _params(params.rows, keep=False)
    refused = run_action_spec(
        project, _request(sheet_id, wrong_policy, preview.confirmation), project_id="p"
    )
    assert refused.status == "failed"
    assert refused.errors[0].code == "stale_import_preview"
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
