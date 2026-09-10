from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from executor_harness import (
    CatalogEntry,
    ExecutorCase,
    Gate,
    case_env,
    operation_action,
)
from frisket.engine.store import Project
from frisket.engine.runner.review import review_bundle_page
from frisket.engine.store.evidence import (
    list_cell_evidence,
    record_evidence_link,
    record_source_artifact,
    record_source_span,
    resolve_evidence_viewer,
)

_UNSET = object()


def _map_reviewable_action(sheet_id: int) -> dict[str, Any]:
    code = "\n".join(
        [
            "risk = 'high' if 'contract' in row['story'] else 'low'",
            "result = {'risk': risk}",
        ]
    )
    return {
        "action_id": "map.python",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "input_columns": ["headline", "story"],
            "code": code,
            "return_schema": {
                "type": "object",
                "required": ["risk"],
                "properties": {"risk": {"type": "string"}},
            },
            "output_routes": [
                {
                    "name": "risk",
                    "path": "$.risk",
                    "target": {
                        "kind": "column",
                        "type": "text",
                    },
                }
            ],
        },
        "idempotency_key": "review_map@sha256:v1",
    }


def _review_action(
    *,
    run_id: int,
    row_id: int,
    column_id: int,
    decision: str,
    key: str,
    value: Any = _UNSET,
    note: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "run_id": run_id,
        "row_id": row_id,
        "column_id": column_id,
        "decision": decision,
    }
    if value is not _UNSET:
        params["value"] = value
    if note is not None:
        params["note"] = note
    return {
        "action_id": "review.decision",
        "scope": {"kind": "project"},
        "params": params,
        "idempotency_key": key,
    }


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del tmp_path
    from frisket.engine.executor import run_action_spec

    sheet_id = project.add_sheet("Stories")
    columns = {
        "headline": project.add_column(sheet_id, "headline", type="text"),
        "story": project.add_column(sheet_id, "story", type="text"),
    }
    project.add_rows(
        sheet_id,
        [
            {
                "headline": "Contract award",
                "story": "City hall awarded a no-bid contract",
            },
            {
                "headline": "Road work",
                "story": "Routine road work finished early",
            },
        ],
        columns,
    )
    mapped = run_action_spec(
        project, _map_reviewable_action(sheet_id), project_id="project-review-decision"
    )
    assert mapped.status == "completed", mapped.errors
    assert mapped.run_id is not None
    return {
        "sheet_id": sheet_id,
        "run_id": mapped.run_id,
        "map_op_id": mapped.op_ids[0],
        "row_ids": [
            int(row["id"])
            for row in project.db.execute(
                "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (sheet_id,)
            ).fetchall()
        ],
        "risk_column_id": int(
            project.db.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name='risk'", (sheet_id,)
            ).fetchone()["id"]
        ),
    }


def _review_state(project: Project, seeded: dict[str, Any], row_id: int) -> str:
    row = project.db.execute(
        "SELECT review_state FROM results WHERE run_id=? AND row_id=? AND column_id=?",
        (seeded["run_id"], row_id, seeded["risk_column_id"]),
    ).fetchone()
    assert row is not None
    return str(row["review_state"])


def _review_metadata(
    project: Project, seeded: dict[str, Any], row_id: int
) -> tuple[str | None, str | None]:
    row = project.db.execute(
        "SELECT review_decision, review_note FROM results "
        "WHERE run_id=? AND row_id=? AND column_id=?",
        (seeded["run_id"], row_id, seeded["risk_column_id"]),
    ).fetchone()
    assert row is not None
    return row["review_decision"], row["review_note"]


def _live_risk(project: Project, seeded: dict[str, Any], row_id: int) -> Any:
    return project.get_values(
        seeded["sheet_id"], seeded["risk_column_id"], row_ids=[row_id]
    )[row_id]


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _review_action(
        run_id=seeded["run_id"],
        row_id=seeded["row_ids"][0],
        column_id=seeded["risk_column_id"],
        decision="accept",
        key="review_accept@sha256:v1",
    )


def _invalid_decision_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _review_action(
        run_id=seeded["run_id"],
        row_id=seeded["row_ids"][0],
        column_id=seeded["risk_column_id"],
        decision="maybe",
        key="review_invalid_decision@sha256:v1",
    )


def _edit_without_value_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _review_action(
        run_id=seeded["run_id"],
        row_id=seeded["row_ids"][1],
        column_id=seeded["risk_column_id"],
        decision="edit",
        key="review_missing_value@sha256:v1",
    )


def _edit_null_value_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _review_action(
        run_id=seeded["run_id"],
        row_id=seeded["row_ids"][1],
        column_id=seeded["risk_column_id"],
        decision="edit",
        value=None,
        key="review_null_edit@sha256:v1",
    )


def _bad_target_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _review_action(
        run_id=seeded["run_id"],
        row_id=999999,
        column_id=seeded["risk_column_id"],
        decision="accept",
        key="review_bad_target@sha256:v1",
    )


def _conflicting_row_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # Same idempotency key as the primary accept, different target row.
    return _review_action(
        run_id=seeded["run_id"],
        row_id=seeded["row_ids"][1],
        column_id=seeded["risk_column_id"],
        decision="accept",
        key="review_accept@sha256:v1",
    )


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt

    assert len(result.op_ids) == 1
    review_output = next(output for output in result.outputs if output.kind == "review")
    assert review_output.ref["kind"] == "review_decision"
    assert review_output.ref["decision"] == "accept"
    assert review_output.ref["review_state_after"] == "verified"
    assert _review_state(project, seeded, seeded["row_ids"][0]) == "verified"

    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.action_kind == "review.decision"
    assert receipt.op_ids == result.op_ids
    assert receipt.review == {
        "decision": "accept",
        "review_state_before": "unreviewed",
        "review_state_after": "verified",
        "note": None,
    }
    assert {item.ref["kind"] for item in receipt.evidence} >= {
        "target_result_cell",
        "review_state_transition",
    }


CASES = [
    ExecutorCase(
        kind="review.decision",
        catalog=CatalogEntry(
            execution_mode="whole_project",
            async_mode="sync",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write",),
            side_effects=frozenset(
                {
                    "read_result_cell",
                    "write_review_state",
                    "write_edit_overlay",
                    "write_review_op",
                    "write_receipt",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_action_request",
                    "review_value_required",
                    "review_target_not_found",
                    "output_column_busy",
                    "idempotency_conflict",
                    "project_write_failed",
                }
            ),
            cost_policy_kind="none",
            input_schema_properties=(
                "run_id",
                "row_id",
                "column_id",
                "decision",
                "value",
                "note",
            ),
            output_schema_properties=(
                "run_id",
                "row_id",
                "column_id",
                "decision",
                "review_state_before",
                "review_state_after",
                "note",
            ),
            description_contains="current generated",
        ),
        seed=_seed,
        make_action=_make_action,
        gates=(
            Gate(
                "invalid_review_decision",
                _invalid_decision_action,
                "invalid_action_request",
            ),
            Gate(
                "review_value_required_missing",
                _edit_without_value_action,
                "review_value_required",
            ),
            Gate(
                "review_target_not_found",
                _bad_target_action,
                "review_target_not_found",
            ),
            Gate(
                "idempotency_conflict",
                _conflicting_row_action,
                "idempotency_conflict",
                after_primary_run=True,
            ),
        ),
        expect_counts={"ops": 1, "edits": 0, "results": 0, "receipts": 1},
        check_state=_check_state,
        request_style="typed",
    )
]


def test_review_edit_undo_and_two_reject_decisions_walk_exact_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Edit is usable but failed; reject preserves, reject_clear blanks."""
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        project, seeded = env.project, env.seeded
        row_id = seeded["row_ids"][1]

        edited = env.run(
            _review_action(
                run_id=seeded["run_id"],
                row_id=row_id,
                column_id=seeded["risk_column_id"],
                decision="edit",
                value="manual-medium",
                note="Corrected the original category.",
                key="review_edit@sha256:v1",
            )
        )
        assert edited.status == "completed", edited.errors
        assert _review_state(project, seeded, row_id) == "verified"
        assert _review_metadata(project, seeded, row_id) == (
            "edit",
            "Corrected the original category.",
        )
        assert _live_risk(project, seeded, row_id) == "manual-medium"
        from frisket.contracts.action import Receipt

        edit_receipt = Receipt.model_validate(
            json.loads(
                project.db.execute(
                    "SELECT body FROM receipts WHERE id=?", (edited.receipt_id,)
                ).fetchone()["body"]
            )
        )
        assert {item.ref["kind"] for item in edit_receipt.evidence} >= {
            "target_result_cell",
            "review_state_transition",
            "edit_overlay",
        }

        undo = env.run(
            operation_action(
                "operation.undo",
                key="undo_review_edit@sha256:v1",
                expected_op_id=edited.op_ids[0],
            )
        )
        assert undo.status == "completed", undo.errors
        assert _review_state(project, seeded, row_id) == "unreviewed"
        assert _review_metadata(project, seeded, row_id) == (None, None)
        assert _live_risk(project, seeded, row_id) == "low"

        rejected = env.run(
            _review_action(
                run_id=seeded["run_id"],
                row_id=row_id,
                column_id=seeded["risk_column_id"],
                decision="reject",
                note="Wrong, but retain for inspection.",
                key="review_reject@sha256:v1",
            )
        )
        assert rejected.status == "completed", rejected.errors
        assert _review_state(project, seeded, row_id) == "rejected"
        assert _review_metadata(project, seeded, row_id) == (
            "reject",
            "Wrong, but retain for inspection.",
        )
        assert _live_risk(project, seeded, row_id) == "low"

        cleared = env.run(
            _review_action(
                run_id=seeded["run_id"],
                row_id=row_id,
                column_id=seeded["risk_column_id"],
                decision="reject_clear",
                note="Unsafe to leave in the working data.",
                key="review_reject_clear@sha256:v1",
            )
        )
        assert cleared.status == "completed", cleared.errors
        assert _review_state(project, seeded, row_id) == "rejected"
        assert _review_metadata(project, seeded, row_id) == (
            "reject_clear",
            "Unsafe to leave in the working data.",
        )
        assert _live_risk(project, seeded, row_id) is None
        pending = review_bundle_page(project, run_id=seeded["run_id"])
        assert pending["total"] == 1
        complete = review_bundle_page(
            project, run_id=seeded["run_id"], include_reviewed=True
        )
        assert complete["total"] == 2
        reviewed = next(
            bundle for bundle in complete["bundles"] if bundle["row_id"] == row_id
        )
        assert reviewed["fields"][0]["review_decision"] == "reject_clear"
        assert reviewed["fields"][0]["review_note"] == (
            "Unsafe to leave in the working data."
        )
        assert review_bundle_page(project, run_id=seeded["run_id"] + 100)["total"] == 0

        redoable = env.run(
            operation_action(
                "operation.undo",
                key="undo_review_reject_clear@sha256:v1",
                expected_op_id=cleared.op_ids[0],
            )
        )
        assert redoable.status == "completed", redoable.errors
        assert _review_state(project, seeded, row_id) == "rejected"
        assert _review_metadata(project, seeded, row_id) == (
            "reject",
            "Wrong, but retain for inspection.",
        )
        assert _live_risk(project, seeded, row_id) == "low"

        redone = env.run(
            operation_action(
                "operation.redo",
                key="redo_review_reject_clear@sha256:v1",
                expected_op_id=cleared.op_ids[0],
            )
        )
        assert redone.status == "completed", redone.errors
        assert _review_state(project, seeded, row_id) == "rejected"
        assert _review_metadata(project, seeded, row_id) == (
            "reject_clear",
            "Unsafe to leave in the working data.",
        )
        assert _live_risk(project, seeded, row_id) is None


def test_review_explicit_null_refreshes_summary_and_replays_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit null is a real edit value, distinct from an omitted value."""
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        project, seeded = env.project, env.seeded
        row_id = seeded["row_ids"][1]
        request = _edit_null_value_action(seeded)
        project._write_manifest(pending_review_count=99)

        first = env.run(request)
        assert first.status == "completed", first.errors
        assert _live_risk(project, seeded, row_id) is None
        assert (
            json.loads((project.path / "manifest.json").read_text())[
                "pending_review_count"
            ]
            == 1
        )
        assert env.run(request) == first

        omitted = _review_action(
            run_id=seeded["run_id"],
            row_id=row_id,
            column_id=seeded["risk_column_id"],
            decision="edit",
            key=request["idempotency_key"],
        )
        from frisket.engine.executor import run_action_spec

        malformed = run_action_spec(
            project, omitted, project_id="project-review-decision"
        )
        assert [error.code for error in malformed.errors] == ["review_value_required"]
        assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 2

        op = project.db.execute(
            "SELECT spec FROM ops WHERE id=?", (first.op_ids[0],)
        ).fetchone()
        assert json.loads(op["spec"])["params"]["value"] is None


def test_review_success_survives_pending_summary_manifest_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        project, seeded = env.project, env.seeded

        def fail_manifest(**_updates: Any) -> None:
            raise OSError("manifest unavailable")

        monkeypatch.setattr(project, "_write_manifest", fail_manifest)
        result = env.run_primary()

        assert result.status == "completed", result.errors
        assert _review_state(project, seeded, seeded["row_ids"][0]) == "verified"
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM receipts WHERE id=?", (result.receipt_id,)
            ).fetchone()[0]
            == 1
        )


def _attach_cell_evidence(
    project: Project, seeded: dict[str, Any], row_id: int
) -> dict[str, Any]:
    _values, refs = project.get_values_with_refs(
        seeded["sheet_id"], seeded["risk_column_id"], row_ids=[row_id]
    )
    artifact = record_source_artifact(
        project,
        artifact_kind="row",
        media_type="application/vnd.frisket.row+json",
        source_sheet_id=seeded["sheet_id"],
        source_row_id=row_id,
    )
    span = record_source_span(
        project,
        artifact_id=int(artifact["id"]),
        span_kind="whole",
        snippet="reviewable map result evidence",
    )
    return record_evidence_link(
        project,
        subject_kind="cell_value",
        subject_ref=refs[row_id],
        spans=[{"span_id": span["id"]}],
        sheet_id=seeded["sheet_id"],
        row_id=row_id,
        column_id=seeded["risk_column_id"],
        run_id=seeded["run_id"],
        op_id=seeded["map_op_id"],
        producer={
            "source_action_kind": "map.python",
            "field": "risk",
        },
    )


def test_review_decision_edit_and_reject_clear_stale_active_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accept keeps attached cell evidence active; edit and reject-clear mark it
    stale with a decision-specific stale_reason."""
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        project, seeded = env.project, env.seeded
        rows = seeded["row_ids"]
        column_id = seeded["risk_column_id"]

        accepted_link = _attach_cell_evidence(project, seeded, rows[0])
        edited_link = _attach_cell_evidence(project, seeded, rows[1])
        assert accepted_link["status"] == "active"
        assert edited_link["status"] == "active"

        accepted = env.run_primary()
        assert accepted.status == "completed", accepted.errors
        accepted_evidence = list_cell_evidence(
            project,
            sheet_id=seeded["sheet_id"],
            row_id=rows[0],
            column_id=column_id,
            include_stale=True,
        )
        assert accepted_evidence["links"][0]["status"] == "active"
        assert accepted_evidence["stale_count"] == 0

        edited = env.run(
            _review_action(
                run_id=seeded["run_id"],
                row_id=rows[1],
                column_id=column_id,
                decision="edit",
                value="manual-medium",
                key="review_edit_evidence@sha256:v1",
            )
        )
        assert edited.status == "completed", edited.errors
        edited_evidence = list_cell_evidence(
            project,
            sheet_id=seeded["sheet_id"],
            row_id=rows[1],
            column_id=column_id,
            include_stale=True,
        )
        assert edited_evidence["links"][0]["status"] == "stale"
        edited_viewer = resolve_evidence_viewer(
            project, edited_evidence["links"][0]["stable_id"]
        )
        assert edited_viewer["link"]["stale_reason"] == "review_decision_edit"
        assert edited_evidence["stale_count"] == 1

        _attach_cell_evidence(project, seeded, rows[0])
        rejected = env.run(
            _review_action(
                run_id=seeded["run_id"],
                row_id=rows[0],
                column_id=column_id,
                decision="reject_clear",
                key="review_reject_evidence@sha256:v1",
            )
        )
        assert rejected.status == "completed", rejected.errors
        rejected_evidence = list_cell_evidence(
            project,
            sheet_id=seeded["sheet_id"],
            row_id=rows[0],
            column_id=column_id,
            include_stale=True,
        )
        assert {link["status"] for link in rejected_evidence["links"]} == {"stale"}
        reject_reasons = {
            resolve_evidence_viewer(project, link["stable_id"])["link"]["stale_reason"]
            for link in rejected_evidence["links"]
        }
        assert reject_reasons == {"review_decision_reject_clear"}
        assert rejected_evidence["stale_count"] == 2
