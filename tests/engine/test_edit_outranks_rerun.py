"""A hand edit stays current when a later re-run or Reapply replaces its cell.

A re-run's value is offered for replay review instead; a copied value is not
offered. A reject-clear edit yields to a later replacement, a replacement that
changes the column descriptor supersedes every earlier edit, and a later
source write supersedes an edit.
"""

from __future__ import annotations

from contextlib import closing
from types import SimpleNamespace
from typing import Any

import pytest

from frisket.actions.imports import UpdateRowsParams
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.change_schema import canonical_heads_sql
from frisket.engine.store.current_cells import rebuild_current_cells
from frisket.engine.store.schema import SCHEMA_DIGEST
from frisket.server.services.sheet_grid import SheetGridService, sheet_columns_payload
from frisket.upgrades.project.domain_migration import DOMAIN_TO_DIGEST
from frisket.upgrades.project.edit_precedence_migration import (
    EDIT_PRECEDENCE_TO_DIGEST,
)
from tests.engine.test_citations_migration import _downgrade_to_pre_citations_schema
from helpers import make_client
from tests.engine.historical_schema import restore_pre_feeds_tables

_PROJECT_ID = "proj-edit-precedence"


class _Sheet:
    def __init__(self, project: Project) -> None:
        self.project = project
        self.sheet = project.add_sheet("s")
        source = project.add_column(self.sheet, "n", type="text")
        self.rows = project.add_rows(
            self.sheet, [{"n": str(i)} for i in range(6)], {"n": source}
        )
        self._keys = 0
        self.first_run = self.run_id(self.run("A", replace=False))
        self.out = int(
            project.db.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name='out'",
                (self.sheet,),
            ).fetchone()[0]
        )

    def act(self, spec: dict[str, Any]) -> Any:
        self._keys += 1
        result = run_action_spec(
            self.project,
            {**spec, "idempotency_key": f"k{self._keys}"},
            project_id=_PROJECT_ID,
        )
        assert result.status == "completed", result.errors
        return result

    def run(
        self,
        prefix: str,
        *,
        replace: bool = True,
        row_ids: list[int] | None = None,
        column_type: str = "text",
    ) -> int:
        code = (
            "result = len(row['n'])"
            if column_type == "integer"
            else f"result = {prefix!r} + row['n']"
        )
        self.act(
            {
                "action_id": "map.python",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": self.sheet,
                    **({"row_ids": row_ids} if row_ids else {}),
                },
                "params": {
                    "input_columns": ["n"],
                    "code": code,
                    "return_schema": {
                        "type": "integer" if column_type == "integer" else "string"
                    },
                    "output_routes": [
                        {
                            "name": "out",
                            "path": "$",
                            "target": {"kind": "column", "type": column_type},
                        }
                    ],
                },
                "output_names": {"out": "out"},
                **({"replace_existing": True} if replace else {}),
            }
        )
        return int(self.project.db.execute("SELECT MAX(id) FROM changes").fetchone()[0])

    def run_id(self, change_id: int) -> int:
        return int(
            self.project.db.execute(
                "SELECT id FROM runs WHERE change_id=?", (change_id,)
            ).fetchone()[0]
        )

    def reapply(self, source_run_id: int, *, row_ids: list[int] | None = None) -> None:
        self.act(
            {
                "action_id": "run.reapply",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": self.sheet,
                    **({"row_ids": row_ids} if row_ids else {}),
                },
                "params": {"source_run_id": source_run_id},
                "output_names": {"out": "out"},
                "replace_existing": True,
                "expected_generated_columns": {"out": self.out},
            }
        )

    def edit_first_three(self) -> None:
        self.act(
            {
                "action_id": "cell.edit",
                "scope": {"kind": "project"},
                "params": {
                    "edits": [
                        {"row_id": row, "column_id": self.out, "value": f"EDIT{i}"}
                        for i, row in enumerate(self.rows[:3])
                    ]
                },
            }
        )

    def replay_action(self, action_id: str, row_id: int) -> None:
        pending = self.pending()[row_id]
        self.act(
            {
                "action_id": action_id,
                "scope": {"kind": "project"},
                "params": {
                    "sheet_id": self.sheet,
                    "row_id": row_id,
                    "column_id": self.out,
                    "run_id": pending["run_id"],
                    "generated_value_hash": pending["generated_value_hash"],
                },
            }
        )

    def values(self) -> list[Any]:
        current = self.project.get_values(self.sheet, self.out)
        return [current[row] for row in self.rows]

    def origins(self) -> list[str]:
        refs = self.project.get_values_with_refs(
            self.sheet, self.out, row_ids=self.rows
        )[1]
        return [refs[row]["kind"] for row in self.rows]

    def pending(self) -> dict[int, dict[str, Any]]:
        return self.project.pending_replay_values(
            self.sheet, self.out, row_ids=self.rows
        )

    def pending_fresh(self) -> dict[int, Any]:
        return {row: entry["fresh_value"] for row, entry in self.pending().items()}


_EDITED = ["EDIT0", "EDIT1", "EDIT2"]


def test_whole_column_rerun_keeps_edits_and_offers_run_values(make_project):
    sheet = _Sheet(make_project("p"))
    sheet.edit_first_three()
    sheet.run("B")
    assert sheet.values() == [*_EDITED, "B3", "B4", "B5"]
    assert sheet.origins() == ["manual_edit"] * 3 + ["run_result"] * 3
    assert sheet.project.pending_replay_count(sheet.sheet, sheet.out) == 3
    assert sheet.pending_fresh() == dict(zip(sheet.rows[:3], ["B0", "B1", "B2"]))


def _assert_grid_treats_column_as_mixed(sheet: _Sheet) -> None:
    """Every sheet read succeeds and offers no replay value for the column."""
    service = SheetGridService(SimpleNamespace(get=lambda _: sheet.project))
    for mode in ("exact", "bounded"):
        data = service.sheet_data(_PROJECT_ID, sheet.sheet, value_mode=mode)
        [column] = [c for c in data["columns"] if c["id"] == sheet.out]
        assert column["mixed_origins"] is True
        assert not column["replay_pending_count"]
        cells = {row["id"]: row for row in data["rows"]}
        assert [cells[row]["cells"][str(sheet.out)] for row in sheet.rows[:3]] == (
            _EDITED
        )
        assert not any(
            "pending_value" in cells[row]["meta"].get(str(sheet.out), {})
            for row in sheet.rows
        )
    [column] = [
        c
        for c in sheet_columns_payload(sheet.project, sheet.sheet)
        if c["id"] == sheet.out
    ]
    assert column["mixed_origins"] is True


@pytest.mark.parametrize("scope", [[0], [0, 3]], ids=["edited-only", "mixed"])
def test_row_scoped_rerun_keeps_edits_and_reads_as_mixed(make_project, scope):
    sheet = _Sheet(make_project("p"))
    sheet.edit_first_three()
    sheet.run("B", row_ids=[sheet.rows[i] for i in scope])
    assert sheet.values() == [*_EDITED, "B3" if 3 in scope else "A3", "A4", "A5"]
    assert sheet.origins()[:3] == ["manual_edit"] * 3
    _assert_grid_treats_column_as_mixed(sheet)
    refused = run_action_spec(
        sheet.project,
        {
            "action_id": "replay.accept_column",
            "scope": {"kind": "project"},
            "params": {"sheet_id": sheet.sheet, "column_id": sheet.out},
            "idempotency_key": "accept-mixed",
        },
        project_id=_PROJECT_ID,
    )
    assert [error.code for error in refused.errors] == [
        "mixed_origin_column_unsupported"
    ]


def test_single_edited_cell_rerun_reads_as_mixed(make_project):
    sheet = _Sheet(make_project("p"))
    sheet.act(
        {
            "action_id": "cell.edit",
            "scope": {"kind": "project"},
            "params": {
                "edits": [
                    {"row_id": sheet.rows[0], "column_id": sheet.out, "value": "E"}
                ]
            },
        }
    )
    sheet.run("B", row_ids=[sheet.rows[0]])
    assert sheet.values() == ["E", "A1", "A2", "A3", "A4", "A5"]
    service = SheetGridService(SimpleNamespace(get=lambda _: sheet.project))
    for mode in ("exact", "bounded"):
        data = service.sheet_data(_PROJECT_ID, sheet.sheet, value_mode=mode)
        [column] = [c for c in data["columns"] if c["id"] == sheet.out]
        assert column["mixed_origins"] is True
        assert not column["replay_pending_count"]


def test_accepted_value_is_an_edit_that_a_later_rerun_keeps(make_project):
    sheet = _Sheet(make_project("p"))
    sheet.edit_first_three()
    sheet.run("B")
    sheet.replay_action("replay.accept", sheet.rows[0])
    assert sheet.values()[0] == "B0"
    sheet.run("C")
    assert sheet.values()[:3] == ["B0", "EDIT1", "EDIT2"]
    assert sheet.origins()[0] == "manual_edit"
    assert sheet.pending_fresh()[sheet.rows[0]] == "C0"


def test_reapply_after_an_edit_keeps_it_and_a_later_rerun_offers_values(
    make_project,
):
    sheet = _Sheet(make_project("p"))
    sheet.edit_first_three()
    sheet.reapply(sheet.first_run)
    assert sheet.values() == [*_EDITED, "A3", "A4", "A5"]
    assert sheet.origins() == ["manual_edit"] * 3 + ["copied_result"] * 3
    assert sheet.pending() == {}
    sheet.run("B")
    assert sheet.values() == [*_EDITED, "B3", "B4", "B5"]
    assert sheet.pending_fresh() == dict(zip(sheet.rows[:3], ["B0", "B1", "B2"]))


def test_reapply_after_a_rerun_keeps_edits_and_offers_no_copied_value(
    make_project,
):
    sheet = _Sheet(make_project("p"))
    sheet.edit_first_three()
    sheet.run("B")
    sheet.reapply(sheet.first_run)
    assert sheet.values() == [*_EDITED, "A3", "A4", "A5"]
    assert sheet.pending() == {}


def test_edit_after_reapply_is_current(make_project):
    sheet = _Sheet(make_project("p"))
    sheet.run("B")
    sheet.reapply(sheet.first_run)
    sheet.edit_first_three()
    assert sheet.values() == [*_EDITED, "A3", "A4", "A5"]
    assert sheet.origins() == ["manual_edit"] * 3 + ["copied_result"] * 3


def _reject_clear(sheet: _Sheet, row_id: int) -> None:
    sheet.act(
        {
            "action_id": "review.decision",
            "scope": {"kind": "project"},
            "params": {
                "run_id": sheet.first_run,
                "row_id": row_id,
                "column_id": sheet.out,
                "decision": "reject_clear",
            },
        }
    )


def test_reject_clear_yields_to_a_later_reapply_but_an_edit_does_not(make_project):
    sheet = _Sheet(make_project("p"))
    sheet.act(
        {
            "action_id": "cell.edit",
            "scope": {"kind": "project"},
            "params": {
                "edits": [
                    {"row_id": sheet.rows[0], "column_id": sheet.out, "value": "E"}
                ]
            },
        }
    )
    _reject_clear(sheet, sheet.rows[1])
    assert sheet.values()[1] is None
    sheet.reapply(sheet.first_run)
    assert sheet.values()[:2] == ["E", "A1"]
    assert sheet.origins()[:2] == ["manual_edit", "copied_result"]


def test_descriptor_changing_reapply_supersedes_edits(make_project):
    sheet = _Sheet(make_project("p"))
    retyped = sheet.run_id(sheet.run("", column_type="integer"))
    sheet.run("B")
    sheet.edit_first_three()
    sheet.reapply(retyped)
    assert sheet.values() == [1] * 6
    assert sheet.origins() == ["copied_result"] * 6


def test_source_write_after_an_edit_still_supersedes_it(make_project):
    project = make_project("p")
    sheet = project.add_sheet("People")
    columns = {
        "id": project.add_column(sheet, "id", "text"),
        "name": project.add_column(sheet, "name", "text"),
    }
    rows = project.add_rows(
        sheet, [{"id": "a", "name": "Alice"}, {"id": "b", "name": "Bob"}], columns
    )
    edited = run_action_spec(
        project,
        {
            "action_id": "cell.edit",
            "scope": {"kind": "project"},
            "params": {
                "edits": [
                    {"row_id": rows[1], "column_id": columns["name"], "value": "E"}
                ]
            },
            "idempotency_key": "edit",
        },
        project_id=_PROJECT_ID,
    )
    assert edited.status == "completed", edited.errors
    params = UpdateRowsParams(
        columns=[{"name": "id", "type": "text"}, {"name": "name", "type": "text"}],
        key_columns=["id"],
        update_policy="incoming_wins",
        keep_existing_on_blank=False,
        rows=[{"id": "b", "name": "Robert"}],
        source={"kind": "inline", "label": "paste", "fingerprint": "sha256:test"},
    )
    refreshed = run_action_spec(
        project,
        {
            "action_id": "import.update_rows",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": params.model_dump(mode="json"),
            "idempotency_key": "refresh",
        },
        project_id=_PROJECT_ID,
    )
    assert refreshed.status == "completed", refreshed.errors
    refs = project.get_values_with_refs(sheet, columns["name"], row_ids=rows)[1]
    assert project.get_values(sheet, columns["name"])[rows[1]] == "Robert"
    assert refs[rows[1]]["kind"] == "source_cell"


def test_sheet_endpoint_reads_reapplied_copies_beneath_edits(tmp_path):
    client = make_client(tmp_path)
    project_id = client.post("/api/projects", json={"name": "Copies"}).json()["id"]
    sheet = _Sheet(client.app.state.workspace.get(project_id))

    def read() -> tuple[list[Any], list[int]]:
        response = client.get(f"/api/projects/{project_id}/sheets/{sheet.sheet}/data")
        assert response.status_code == 200, response.text
        data = response.json()
        [column] = [c for c in data["columns"] if c["id"] == sheet.out]
        assert column["mixed_origins"] is False
        rows = {row["id"]: row for row in data["rows"]}
        pending = [
            index
            for index, row in enumerate(sheet.rows)
            if "pending_value" in rows[row]["meta"].get(str(sheet.out), {})
        ]
        assert column["replay_pending_count"] == len(pending)
        return [rows[row]["cells"][str(sheet.out)] for row in sheet.rows], pending

    # A copy beneath an edit is not offered; the original run still is.
    sheet.edit_first_three()
    sheet.reapply(sheet.first_run, row_ids=[sheet.rows[0]])
    assert read() == ([*_EDITED, "A3", "A4", "A5"], [1, 2])
    sheet.run("B")
    sheet.reapply(sheet.first_run)
    assert read() == ([*_EDITED, "A3", "A4", "A5"], [])


def test_accept_makes_the_run_value_current(make_project):
    sheet = _Sheet(make_project("p"))
    sheet.edit_first_three()
    sheet.run("B")
    sheet.replay_action("replay.accept", sheet.rows[1])
    assert sheet.values()[:3] == ["EDIT0", "B1", "EDIT2"]
    assert sheet.project.pending_replay_count(sheet.sheet, sheet.out) == 2


def test_keep_edit_dismisses_until_a_newer_rerun(make_project):
    sheet = _Sheet(make_project("p"))
    sheet.edit_first_three()
    sheet.run("B")
    sheet.replay_action("replay.dismiss", sheet.rows[0])
    assert sheet.values()[:3] == _EDITED
    assert sheet.pending_fresh() == dict(zip(sheet.rows[1:3], ["B1", "B2"]))
    sheet.run("C")
    assert sheet.values() == [*_EDITED, "C3", "C4", "C5"]
    assert sheet.pending_fresh() == dict(zip(sheet.rows[:3], ["C0", "C1", "C2"]))


def test_undo_of_the_rerun_keeps_edits_and_offers_the_earlier_values(make_project):
    sheet = _Sheet(make_project("p"))
    sheet.edit_first_three()
    sheet.run("B")
    sheet.project.undo()
    assert sheet.values() == [*_EDITED, "A3", "A4", "A5"]
    assert sheet.pending_fresh() == dict(zip(sheet.rows[:3], ["A0", "A1", "A2"]))
    sheet.project.redo()
    assert sheet.values() == [*_EDITED, "B3", "B4", "B5"]


def test_reject_clear_yields_to_a_later_rerun_but_an_edit_does_not(make_project):
    sheet = _Sheet(make_project("p"))
    sheet.act(
        {
            "action_id": "cell.edit",
            "scope": {"kind": "project"},
            "params": {
                "edits": [
                    {"row_id": sheet.rows[0], "column_id": sheet.out, "value": "E"}
                ]
            },
        }
    )
    run_id = int(
        sheet.project.db.execute(
            "SELECT run_id FROM results WHERE column_id=? LIMIT 1", (sheet.out,)
        ).fetchone()[0]
    )
    sheet.act(
        {
            "action_id": "review.decision",
            "scope": {"kind": "project"},
            "params": {
                "run_id": run_id,
                "row_id": sheet.rows[1],
                "column_id": sheet.out,
                "decision": "reject_clear",
            },
        }
    )
    assert sheet.values()[1] is None
    sheet.run("B")
    assert sheet.values()[:2] == ["E", "B1"]
    assert sheet.origins()[:2] == ["manual_edit", "run_result"]


def test_descriptor_changing_rerun_supersedes_edits(make_project):
    sheet = _Sheet(make_project("p"))
    sheet.edit_first_three()
    sheet.run("B")
    assert sheet.values()[:3] == _EDITED
    sheet.run("", column_type="integer")
    assert sheet.values() == [1] * 6
    assert sheet.origins() == ["run_result"] * 6


@pytest.mark.parametrize("reapply", [False, True], ids=["rerun", "reapply"])
def test_projection_rebuild_resolves_the_same_heads(make_project, reapply):
    sheet = _Sheet(make_project("p"))
    sheet.edit_first_three()
    sheet.run("B")
    if reapply:
        sheet.reapply(sheet.first_run)
    db = sheet.project.db
    columns = "column_id,row_id,origin_kind,origin_change_id,origin_run_id"
    cached = db.execute(
        f"SELECT {columns} FROM current_cells WHERE column_id=? ORDER BY row_id",
        (sheet.out,),
    ).fetchall()
    canonical = db.execute(
        f"SELECT {columns} FROM canonical_cell_heads WHERE column_id=? ORDER BY row_id",
        (sheet.out,),
    ).fetchall()
    db.execute("BEGIN")
    db.execute("DELETE FROM current_cells")
    rebuild_current_cells(db)
    db.commit()
    rebuilt = db.execute(
        f"SELECT {columns} FROM current_cells WHERE column_id=? ORDER BY row_id",
        (sheet.out,),
    ).fetchall()
    assert [tuple(row) for row in cached] == [tuple(row) for row in canonical]
    assert [tuple(row) for row in rebuilt] == [tuple(row) for row in canonical]
    assert sheet.values() == [
        *_EDITED,
        *(["A3", "A4", "A5"] if reapply else ["B3", "B4", "B5"]),
    ]


def _stamp_pre_citations_digest(db, digest: str) -> None:
    _downgrade_to_pre_citations_schema(db)
    db.execute("BEGIN")
    db.execute("UPDATE meta SET value=? WHERE key='schema_digest'", (digest,))
    db.commit()


def test_upgrade_restores_edits_hidden_by_latest_change_wins(make_project):
    sheet = _Sheet(make_project("p"))
    sheet.edit_first_three()
    sheet.run("B")
    db = sheet.project.db
    # Recreate the cache the previous schema wrote: the latest run won.
    run_heads = canonical_heads_sql(
        "cell_contributions contribution WHERE contribution.origin_kind!='manual_edit'"
    )
    restore_pre_feeds_tables(db)
    db.execute("BEGIN")
    db.execute(
        "INSERT OR REPLACE INTO current_cells SELECT * FROM "
        f"({run_heads}) WHERE column_id=?",
        (sheet.out,),
    )
    db.execute("ALTER TABLE consents RENAME COLUMN estimate_json TO quote_json")
    db.execute(
        "ALTER TABLE project_qa_research_runs "
        "RENAME COLUMN pending_pause_json TO pending_approval_json"
    )
    db.commit()
    _stamp_pre_citations_digest(db, DOMAIN_TO_DIGEST)
    assert sheet.values()[:3] == ["B0", "B1", "B2"]
    path = sheet.project.path
    sheet.project.close()

    with closing(Project(path)) as reopened:
        assert (
            reopened.db.execute(
                "SELECT value FROM meta WHERE key='schema_digest'"
            ).fetchone()[0]
            == SCHEMA_DIGEST
        )
        values = reopened.get_values(sheet.sheet, sheet.out)
        assert [values[row] for row in sheet.rows] == [*_EDITED, "B3", "B4", "B5"]
        assert reopened.pending_replay_count(sheet.sheet, sheet.out) == 3


def test_upgrade_restores_edits_hidden_by_a_later_reapply(make_project):
    sheet = _Sheet(make_project("p"))
    sheet.edit_first_three()
    sheet.reapply(sheet.first_run)
    db = sheet.project.db
    # Recreate the cache the previous schema wrote: the later copy won.
    copy_heads = canonical_heads_sql(
        "cell_contributions contribution WHERE contribution.origin_kind!='manual_edit'"
    )
    restore_pre_feeds_tables(db)
    db.execute("BEGIN")
    db.execute(
        "INSERT OR REPLACE INTO current_cells SELECT * FROM "
        f"({copy_heads}) WHERE column_id=?",
        (sheet.out,),
    )
    db.execute("ALTER TABLE consents RENAME COLUMN estimate_json TO quote_json")
    db.execute(
        "ALTER TABLE project_qa_research_runs "
        "RENAME COLUMN pending_pause_json TO pending_approval_json"
    )
    db.commit()
    _stamp_pre_citations_digest(db, EDIT_PRECEDENCE_TO_DIGEST)
    assert sheet.values()[:3] == ["A0", "A1", "A2"]
    path = sheet.project.path
    sheet.project.close()

    with closing(Project(path)) as reopened:
        assert (
            reopened.db.execute(
                "SELECT value FROM meta WHERE key='schema_digest'"
            ).fetchone()[0]
            == SCHEMA_DIGEST
        )
        values = reopened.get_values(sheet.sheet, sheet.out)
        assert [values[row] for row in sheet.rows] == [*_EDITED, "A3", "A4", "A5"]
        refs = reopened.get_values_with_refs(sheet.sheet, sheet.out, row_ids=sheet.rows)
        assert [refs[1][row]["kind"] for row in sheet.rows] == (
            ["manual_edit"] * 3 + ["copied_result"] * 3
        )
