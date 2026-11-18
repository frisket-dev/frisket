from frisket.engine.store.runs import RunResultStore
from frisket.engine.executor.actions import run_action_spec


def test_cancel_after_last_row_before_terminal_transaction_keeps_results(
    make_project, monkeypatch
):
    from frisket.engine.runner import map_runner

    project = make_project("last-row")
    sheet = project.add_sheet("data")
    source = project.add_column(sheet, "text")
    [row] = project.add_rows(sheet, [{"text": "original"}], {"text": source})
    finalize = map_runner.finalize_dispatched_run
    manual_edits = []

    def stop_before_finalize(project, store, **kwargs):
        store = RunResultStore(project)
        assert store.request_cancel(kwargs["run_id"])
        first = store.get_run(kwargs["run_id"])["cancel_requested_at"]
        manual_edits.append(
            project.apply_edits(
                [{"row_id": row, "column_id": source, "value": "manual"}]
            )
        )
        assert store.request_cancel(kwargs["run_id"])
        assert store.get_run(kwargs["run_id"])["cancel_requested_at"] == first
        return finalize(project, store, **kwargs)

    monkeypatch.setattr(map_runner, "finalize_dispatched_run", stop_before_finalize)
    result = run_action_spec(
        project,
        {
            "action_id": "map.template",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"template": {"text": "{{text}}!"}},
            "output_names": {"rendered": "copied"},
            "idempotency_key": "late-stop",
        },
        project_id="test",
    )
    assert result.status == "cancelled", result.errors
    assert RunResultStore(project).get_run(result.run_id)["status"] == "cancelled"
    output = next(
        item["id"] for item in project.columns(sheet) if item["name"] == "copied"
    )
    assert project.get_values(sheet, output)[row] == "original!"
    assert project.get_values(sheet, source)[row] == "manual"
    assert (
        project.db.execute(
            "SELECT status FROM ops WHERE id=?", (manual_edits[0],)
        ).fetchone()[0]
        == "applied"
    )
    assert not RunResultStore(project).request_cancel(result.run_id)
