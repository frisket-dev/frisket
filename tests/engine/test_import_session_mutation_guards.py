from __future__ import annotations

from pathlib import Path

import pytest

from frisket.engine.store import Project
from frisket.engine.store.cell_writes import (
    EditCellWrite,
    insert_edits,
    remove_base_cells,
    replace_base_cells,
)
from frisket.engine.store.import_sessions import ImportInProgress, ImportSessionStore
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.streaming_import import StreamingSheetWriter


COLUMNS = [{"name": "value", "type": "text"}]


def _session(project: Project) -> StreamingSheetWriter:
    return StreamingSheetWriter.start_session(
        project,
        session_id="import:guard",
        writer_authority="claim:guard",
        sheet_name="Importing",
        columns=COLUMNS,
        project_id="guard-project",
        action_kind="import.files",
        idempotency_key="guard:one",
        params_hash="sha256:guard",
        action_id="act:guard",
        receipt_id="receipt:guard",
        source_ref={"kind": "file_inventory", "inventory_id": "inventory:guard"},
    )


def test_session_producer_can_append_but_base_removal_and_edits_are_blocked(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "writes.frisket", name="writes")
    try:
        writer = _session(project)
        row_id = writer.append_page(
            [{"value": "one"}], expected_cursor=0, next_cursor=1
        )[0]
        session = ImportSessionStore(project).get("import:guard")
        assert session is not None and session.producer_id is not None
        column_id = int(project.columns(writer.sheet_id)[0]["id"])

        project.db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ImportInProgress) as removal:
            remove_base_cells(
                project.db,
                producer_id=session.producer_id,
                column_ids=[column_id],
                row_ids=[row_id],
            )
        project.db.rollback()
        assert removal.value.session_id == "import:guard"

        project.db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ImportInProgress):
            replace_base_cells(
                project.db,
                producer_id=session.producer_id,
                column_ids=[column_id],
                row_ids=[row_id],
                cells=[],
            )
        project.db.rollback()

        project.db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ImportInProgress):
            insert_edits(
                project.db,
                op_id=session.op_id,
                edits=[EditCellWrite(row_id=row_id, column_id=column_id, value="edit")],
            )
        project.db.rollback()
        assert project.get_values(writer.sheet_id, column_id)[row_id] == "one"
    finally:
        project.close()


def test_result_write_sheet_delete_and_undo_are_blocked_during_import(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "boundaries.frisket", name="boundaries")
    try:
        writer = _session(project)
        session = ImportSessionStore(project).get("import:guard")
        assert session is not None
        run_id = RunResultStore(project).start_run(
            session.op_id, writer.sheet_id, "map.classify"
        )

        with pytest.raises(ImportInProgress):
            RunResultStore(project).mark_run_results_verified(run_id)
        with pytest.raises(ImportInProgress):
            project.delete_sheet(writer.sheet_id)
        with pytest.raises(ImportInProgress):
            project.undo()
        assert project.sheets()[0]["id"] == writer.sheet_id
    finally:
        project.close()


@pytest.mark.parametrize("state", ["active", "paused", "cancelled"])
def test_only_importing_sheet_is_frozen(tmp_path: Path, state: str) -> None:
    project = Project.create(tmp_path / f"scope-{state}.frisket", name="scope")
    try:
        writer = _session(project)
        if state == "paused":
            writer.pause(expected_cursor=0)
        elif state == "cancelled":
            writer.cancel(expected_cursor=0)
        other = project.add_sheet("Other")
        project.delete_sheet(other)

        with pytest.raises(ImportInProgress) as caught:
            project.delete_sheet(writer.sheet_id)
        assert caught.value.sheet_id == writer.sheet_id
        assert caught.value.state == state
    finally:
        project.close()
