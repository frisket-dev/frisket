from pathlib import Path
from contextlib import closing

from frisket.engine.executor import run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.op_log import step_operation
from test_review_decision_executor import (
    _seed,
    _review_action,
    _review_metadata,
    _review_state,
)


def _decide(project, seeded, decision, key, **extra):
    request = _review_action(
        run_id=seeded["run_id"],
        row_id=seeded["row_ids"][0],
        column_id=seeded["risk_column_id"],
        decision=decision,
        key=key,
    )
    request["params"].update(extra)
    result = run_action_spec(project, request, project_id="review-ownership")
    assert result.status == "completed", result.errors
    return result.op_ids[0]


def _transition(project, op_id, direction):
    project.db.execute("BEGIN IMMEDIATE")
    step_operation(project, direction, change_id=op_id)
    project.db.commit()


def test_selective_review_revert_preserves_later_judgment(tmp_path: Path):
    with closing(Project.create(tmp_path / "p")) as project:
        seeded = _seed(project, tmp_path)
        first = _decide(project, seeded, "accept", "first")
        second = _decide(project, seeded, "reject", "second")
        _transition(project, first, "revert")
        assert _review_state(project, seeded, seeded["row_ids"][0]) == "rejected"
        _transition(project, second, "revert")
        assert _review_state(project, seeded, seeded["row_ids"][0]) == "unreviewed"
        _transition(project, first, "restore")
        assert _review_state(project, seeded, seeded["row_ids"][0]) == "verified"
        _transition(project, second, "restore")
        assert _review_state(project, seeded, seeded["row_ids"][0]) == "rejected"


def test_note_does_not_own_unchanged_decision(tmp_path: Path):
    with closing(Project.create(tmp_path / "p")) as project:
        seeded = _seed(project, tmp_path)
        first = _decide(project, seeded, "accept", "first")
        noted = run_action_spec(
            project,
            {
                "action_id": "review.note",
                "scope": {"kind": "project"},
                "params": {
                    "run_id": seeded["run_id"],
                    "row_id": seeded["row_ids"][0],
                    "note": "verified source",
                },
                "idempotency_key": "note",
            },
            project_id="review-ownership",
        )
        assert noted.status == "completed", noted.errors
        _transition(project, first, "revert")
        assert _review_metadata(project, seeded, seeded["row_ids"][0]) == (
            None,
            "verified source",
        )
        assert _review_state(project, seeded, seeded["row_ids"][0]) == "unreviewed"


def test_copied_result_judgment_is_independent(tmp_path: Path):
    from frisket.engine.store.op_log import append_change
    from frisket.engine.store.changes import start_change

    with closing(Project.create(tmp_path / "p")) as project:
        seeded = _seed(project, tmp_path)
        column = project.add_column(seeded["sheet_id"], "saved", type="text")
        row = seeded["row_ids"][0]
        project.db.execute("BEGIN IMMEDIATE")
        copy = append_change(
            project,
            "reapply_results",
            {
                "output_bindings": {
                    "risk": {
                        "column_id": column,
                        "source_column_id": seeded["risk_column_id"],
                        "review_role": "field",
                    }
                }
            },
            commit=False,
        )
        start_change(project.db, copy)
        project.db.execute(
            "INSERT INTO copied_result_outcomes(op_id,row_id,column_id,source_run_id,source_row_id,source_column_id) VALUES (?,?,?,?,?,?)",
            (copy, row, column, seeded["run_id"], row, seeded["risk_column_id"]),
        )
        project.db.commit()
        from frisket.engine.runner.copied_review import copied_review_page
        from frisket.engine.runner.review import review_runs_page
        from frisket.contracts.http.history_review import (
            ReviewBundlesPage,
            ReviewRunsPage,
        )

        page = copied_review_page(project, copy)
        ReviewBundlesPage.model_validate(page)
        assert page["bundles"][0]["fields"][0]["value"] == "high"
        assert page["bundles"][0]["fields"][0]["copied_op_id"] == copy
        assert page["bundles"][0]["run_id"] is None
        sources = review_runs_page(project)
        assert any(source["copied_op_id"] == copy for source in sources["runs"])
        for source in sources["runs"]:
            source.setdefault("action_name", "Test")
        ReviewRunsPage.model_validate(sources)
        request = {
            "action_id": "review.decision",
            "scope": {"kind": "project"},
            "params": {
                "copied_op_id": copy,
                "row_id": row,
                "column_id": column,
                "decision": "accept",
            },
            "idempotency_key": "copied-review",
        }
        result = run_action_spec(project, request, project_id="review-ownership")
        assert result.status == "completed", result.errors
        assert _review_state(project, seeded, row) == "unreviewed"
        _decide(project, seeded, "reject", "original-review")
        assert (
            project.db.execute(
                "SELECT review_state FROM copied_result_outcomes"
            ).fetchone()[0]
            == "verified"
        )
        _transition(project, result.op_ids[0], "revert")
        assert (
            project.db.execute(
                "SELECT review_state FROM copied_result_outcomes"
            ).fetchone()[0]
            == "unreviewed"
        )
        assert _review_state(project, seeded, row) == "rejected"


def test_repeated_explicit_accept_keeps_its_own_judgment(tmp_path: Path):
    with closing(Project.create(tmp_path / "p")) as project:
        seeded = _seed(project, tmp_path)
        first = _decide(project, seeded, "accept", "first")
        second = _decide(project, seeded, "accept", "second")
        _transition(project, first, "revert")
        assert _review_state(project, seeded, seeded["row_ids"][0]) == "verified"
        _transition(project, second, "revert")
        assert _review_state(project, seeded, seeded["row_ids"][0]) == "unreviewed"


def test_edit_does_not_judge_or_replace_saved_answer(tmp_path: Path):
    from frisket.engine.runner.review import review_bundle_page, review_runs_page

    with closing(Project.create(tmp_path / "p")) as project:
        seeded = _seed(project, tmp_path)
        row = seeded["row_ids"][0]
        edit = run_action_spec(
            project,
            {
                "action_id": "cell.edit",
                "scope": {"kind": "project"},
                "params": {
                    "edits": [
                        {
                            "row_id": row,
                            "column_id": seeded["risk_column_id"],
                            "value": "manual",
                        }
                    ]
                },
                "idempotency_key": "ordinary-edit",
            },
            project_id="review-ownership",
        )
        assert edit.status == "completed", edit.errors
        assert _review_state(project, seeded, row) == "unreviewed"
        # Ordinary edits to every output do not remove the saved answer from Review.
        extra_edits = [
            {
                "row_id": target["row_id"],
                "column_id": target["column_id"],
                "value": "manual",
            }
            for target in project.db.execute(
                "SELECT row_id,column_id FROM results WHERE run_id=?",
                (seeded["run_id"],),
            )
        ]
        result = run_action_spec(
            project,
            {
                "action_id": "cell.edit",
                "scope": {"kind": "project"},
                "params": {"edits": extra_edits},
                "idempotency_key": "edit-all",
            },
            project_id="review-ownership",
        )
        assert result.status == "completed", result.errors
        assert any(
            source["run_id"] == seeded["run_id"]
            for source in review_runs_page(project)["runs"]
        )
        bundle = next(
            b
            for b in review_bundle_page(project, run_id=seeded["run_id"])["bundles"]
            if b["row_id"] == row
        )
        assert bundle["fields"][0]["value"] == "high"
        assert bundle["fields"][0]["can_edit"] is False
        rejected = _decide(project, seeded, "reject", "historical-reject")
        assert rejected
        assert (
            project.get_values(
                seeded["sheet_id"], seeded["risk_column_id"], row_ids=[row]
            )[row]
            == "manual"
        )


def test_review_edit_is_not_an_implicit_judgment_command(tmp_path: Path):
    with closing(Project.create(tmp_path / "p")) as project:
        seeded = _seed(project, tmp_path)
        request = _review_action(
            run_id=seeded["run_id"],
            row_id=seeded["row_ids"][0],
            column_id=seeded["risk_column_id"],
            decision="edit",
            value="replacement",
            key="old-edit",
        )
        result = run_action_spec(project, request, project_id="review-ownership")
        assert result.status == "failed"
        assert _review_state(project, seeded, seeded["row_ids"][0]) == "unreviewed"
