"""Combined result identity survives removal of sensitive execution payloads."""

from contextlib import closing
import json
import sqlite3

import pytest

from frisket.engine.executor import run_action_spec
from frisket.engine.runner.review import review_runs_page, review_bundle_page
from frisket.engine.store import Project
from frisket.engine.store.execution_groups import execution_runs
from frisket.engine.store.purge import begin_purge, finish_purge
from frisket.engine.store.runs import RunResultStore
import test_execution_lifecycle_recovery as recovery

work = recovery.work


@pytest.mark.parametrize("select_retry", [False, True], ids=["root", "retry"])
def test_partial_purge_preserves_combined_review_and_reapply_identity(
    work, select_retry
):
    project, sheet, _, rows, calls, adapter, run = work
    adapter.fail = "describe"
    original = recovery.start(run)
    assert original.status == "failed"
    adapter.fail = None
    retried = run("run.retry", {"source_run_id": original.run_id})
    assert retried.status == "completed", retried.errors
    root, members = execution_runs(project, retried.run_id)
    assert (root, members) == (original.run_id, [original.run_id, retried.run_id])
    assert all(
        "execution_root_run_id" not in json.loads(row[0])
        for row in project.db.execute("SELECT params FROM runs")
    )
    target = next(
        column["id"] for column in project.columns(sheet) if column["name"] == "Answer"
    )
    retained = project.get_values(sheet, target)[rows[1]]
    call_count = len(calls)
    path = project.path
    project.close()
    with closing(Project(path, maintenance=True)) as maintenance:
        finish_purge(maintenance, begin_purge(maintenance, row_ids=[rows[0]]))
        assert [
            row[0] for row in maintenance.db.execute("SELECT params FROM runs")
        ] == ["{}", "{}"]
        assert execution_runs(maintenance, retried.run_id) == (root, members)
        assert maintenance.db.execute("PRAGMA foreign_key_check").fetchall() == []
        page = review_runs_page(maintenance)
        assert [item["run_id"] for item in page["runs"]] == [root]
        bundles = review_bundle_page(maintenance, run_id=root, include_reviewed=True)
        assert bundles["total"] == 1
        assert bundles["bundles"][0]["fields"][0]["value"] == retained
        copied = run_action_spec(
            maintenance,
            {
                "action_id": "run.reapply",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet,
                    "row_ids": [rows[1]],
                },
                "params": {"source_run_id": retried.run_id if select_retry else root},
                "output_names": {"answer": "Recovered"},
                "idempotency_key": "copy-retained-after-purge",
            },
            project_id="retained-execution",
        )
        assert copied.status == "completed", copied.errors
        column = next(
            column["id"]
            for column in maintenance.columns(sheet)
            if column["name"] == "Recovered"
        )
        assert maintenance.get_values(sheet, column) == {rows[1]: retained}
    assert len(calls) == call_count


def test_execution_identity_is_immutable_and_cannot_attach_to_retry_or_other_action(
    work,
):
    project, sheet, _, rows, _, _, _ = work
    store = RunResultStore(project)
    original = store.start_run(
        project.append_change("map.ask"), sheet, "map.ask", row_ids=rows
    )
    retry = store.start_run(
        project.append_change("map.ask"),
        sheet,
        "map.ask",
        row_ids=rows,
        execution_root_run_id=original,
    )
    with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
        with project.db:
            project.db.execute(
                "UPDATE runs SET execution_root_run_id=NULL WHERE id=?", (retry,)
            )
    for root, action in [(retry, "map.ask"), (original, "map.summarize")]:
        with pytest.raises(sqlite3.IntegrityError, match="original run of this action"):
            store.start_run(
                project.append_change(action), sheet, action, execution_root_run_id=root
            )
    # A payload is not an identity command; old/unrecognized metadata cannot
    # silently attach a separately authored run to someone else's execution.
    independent = store.start_run(
        project.append_change("map.ask"),
        sheet,
        "map.ask",
        params={"execution_root_run_id": original},
    )
    assert execution_runs(project, independent) == (independent, [independent])
