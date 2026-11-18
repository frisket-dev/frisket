import pytest

from frisket.engine.store.keyed_updates import (
    ValidatedKeyedInput,
    observation,
    observe,
    purge_baseline_columns,
    require_relationship,
)


def test_failed_enumeration_cannot_be_sealed_or_published():
    def incomplete():
        yield "a", {"value": "first"}
        raise OSError("source unavailable")

    with ValidatedKeyedInput(retain_records=False) as staged:
        with pytest.raises(OSError):
            staged.stage(incomplete())
        with pytest.raises(ValueError, match="failed"):
            staged.seal()
        with pytest.raises(ValueError, match="incomplete"):
            list(staged.records())


def test_column_purge_removes_only_selected_baseline_fields(make_project):
    project = make_project("purge")
    sheet = project.add_sheet("People")
    columns = {
        name: project.add_column(sheet, name, "text")
        for name in ("key", "note", "other")
    }
    [row] = project.add_rows(sheet, [{"key": "a"}], columns)
    with project.db:
        project.db.execute("BEGIN IMMEDIATE")
        require_relationship(
            project.db,
            relationship_id="source:1",
            sheet_id=sheet,
            key_definition=[(columns["key"], "text")],
        )
        observe(
            project.db,
            relationship_id="source:1",
            row_id=row,
            key="a",
            incoming={str(column): name for name, column in columns.items()},
        )
        purge_baseline_columns(project.db, [columns["note"]])
        assert observation(project.db, "source:1", row) == {
            str(columns["key"]): "key",
            str(columns["other"]): "other",
        }
        purge_baseline_columns(project.db, [columns["key"]])
        assert observation(project.db, "source:1", row) is None
        assert (
            project.db.execute("SELECT COUNT(*) FROM keyed_relationships").fetchone()[0]
            == 0
        )
