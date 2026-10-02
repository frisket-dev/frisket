from contextlib import closing

import pytest

from frisket.actions.join_types import JoinKeyPair
from frisket.actions.types import ListColumnSource, SheetRef, SheetRows, TableError
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.executor.joined_tables_read import AdmittedJoinedTablesReader
from frisket.engine.executor.list_table_read import AdmittedListTableReader
from frisket.engine.store import Project
from frisket.engine.store.streaming_import import StreamingSheetWriter


def _importing_sheet(project: Project, *, state: str = "active") -> int:
    writer = StreamingSheetWriter.start_session(
        project,
        session_id=f"session:{state}",
        writer_authority="worker:one",
        sheet_name=f"Import {state}",
        columns=[
            {"name": "doc", "type": "file"},
            {"name": "k", "type": "text"},
        ],
        project_id="project",
        action_kind="import.files",
        idempotency_key=f"import:{state}",
        params_hash=f"sha256:{state}",
        action_id=f"action:{state}",
        receipt_id=f"receipt:{state}",
        source_ref={"kind": "import_inventory", "ref": f"inventory:{state}"},
    )
    if state != "active":
        project.db.execute(
            "UPDATE import_sessions SET state=? WHERE id=?", (state, f"session:{state}")
        )
        project.db.commit()
    return writer.sheet_id


@pytest.mark.parametrize("state", ["active", "paused", "cancelled"])
def test_sheet_scoped_action_refuses_unfinished_import(tmp_path, state):
    with closing(Project.create(tmp_path / f"{state}.frisket")) as project:
        sheet_id = _importing_sheet(project, state=state)
        result = run_action_spec(
            project,
            {
                "action_id": "media.to_markdown",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {"source": "doc"},
                "idempotency_key": f"blocked:{state}",
            },
            project_id="project",
        )

        assert result.status == "failed"
        assert result.errors[0].code == "import_in_progress"
        assert result.errors[0].details == {
            "sheet_id": sheet_id,
            "session_id": f"session:{state}",
            "state": state,
        }


def test_secondary_and_dynamic_sheet_readers_refuse_importing_sheet(tmp_path):
    with closing(Project.create(tmp_path / "secondary.frisket")) as project:
        importing = _importing_sheet(project)
        left = project.add_sheet("Left")
        left_column = project.add_column(left, "k")
        project.add_rows(left, [{"k": "one"}], {"k": left_column})

        joined = AdmittedJoinedTablesReader(
            project,
            scope=SheetRows(sheet_id=left),
            action_kind="derive.join",
            request_identity={"action_id": "derive.join", "output_names": {}},
            confirmation=None,
        )
        with pytest.raises(TableError) as join_error:
            joined.read(
                SheetRef(sheet_id=importing),
                join_keys=[JoinKeyPair(left_column="k", right_column="k")],
            )
        assert join_error.value.code == "import_in_progress"

        importing_column = next(
            column["id"]
            for column in project.columns(importing)
            if column["name"] == "k"
        )
        lists = AdmittedListTableReader(project)
        with pytest.raises(TableError) as list_error:
            lists.read(
                ListColumnSource(
                    kind="column", sheet_id=importing, column_id=importing_column
                )
            )
        assert list_error.value.code == "import_in_progress"
