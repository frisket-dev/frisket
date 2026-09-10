from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from executor_harness import (
    CatalogEntry,
    ExecutorCase,
    Gate,
    case_env,
    operation_action,
)
from frisket.ai.llm import ModelRouter
from frisket.engine.store import Project
from helpers import replace_test_source_cell

VECS = {
    "Acme Corporation": [1.0, 0.0, 0.0],
    "Globex LLC": [0.0, 1.0, 0.0],
    "Initech Inc": [0.0, 0.0, 1.0],
    "ACME Corp": [1.0, 0.0, 0.0],
    "Globex": [0.6258, 0.78, 0.0],
    "Umbrella Holdings": [0.5774, 0.5774, 0.5774],
}


def _join_action(
    source_sheet_id: int,
    target_sheet_id: int,
    *,
    consented_promise_set_hash: str | None = None,
) -> dict[str, Any]:
    action = {
        "action_id": "join.semantic",
        "scope": {"kind": "sheet_rows", "sheet_id": source_sheet_id},
        "sheet_name": "Raw Semantic Links",
        "output_names": {
            "source": "donor",
            "match_value": "company_match_value",
            "match_score": "company_match_score",
            "matched_row_id": "company_match_row_id",
        },
        "params": {
            "source": "donor",
            "target": {"sheet_id": target_sheet_id, "column": "company"},
            "match_threshold": 0.70,
            "confident_threshold": 0.85,
        },
        "idempotency_key": "join_semantic@sha256:derive-link-table",
    }
    if consented_promise_set_hash is not None:
        action["confirmation"] = consented_promise_set_hash
    return action


def _review_action(
    *,
    run_id: int,
    row_id: int,
    column_id: int,
    value: int,
    key: str = "review_join_row_id@sha256:v1",
) -> dict[str, Any]:
    return {
        "action_id": "review.decision",
        "scope": {"kind": "project"},
        "params": {
            "run_id": run_id,
            "row_id": row_id,
            "column_id": column_id,
            "decision": "edit",
            "value": value,
        },
        "idempotency_key": key,
    }


def _derive_link_action(
    receipt_id: str,
    *,
    target_sheet_name: str = "Reviewed Semantic Links",
    idempotency_key: str = "derive_link_table@sha256:v1",
    row_ids: list[int] | None = None,
    source_sheet_id: int | None = None,
    capabilities: list[str] | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "source": {"kind": "semantic_join", "receipt_id": receipt_id},
        "include_unmatched": False,
    }
    action = {
        "action_id": "derive.link_table",
        "scope": {"kind": "project"}
        if row_ids is None
        else {"kind": "sheet_rows", "sheet_id": source_sheet_id, "row_ids": row_ids},
        "sheet_name": target_sheet_name,
        "params": params,
        "idempotency_key": idempotency_key,
    }
    if capabilities is not None:
        action["capabilities"] = capabilities
    return action


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    """Full upstream dance: a semantic join, an undone review, and one live
    review — the derive must fold in exactly the live applied review."""
    del tmp_path
    from frisket.engine.executor import actions as executor_actions

    source_sheet_id = project.add_sheet("Donors")
    source_columns = {
        "donor": project.add_column(source_sheet_id, "donor", type="text")
    }
    source_row_ids = project.add_rows(
        source_sheet_id,
        [
            {"donor": "ACME Corp"},
            {"donor": "Globex"},
            {"donor": "Umbrella Holdings"},
        ],
        source_columns,
    )
    target_sheet_id = project.add_sheet("Registry")
    target_columns = {
        "company": project.add_column(target_sheet_id, "company", type="text")
    }
    target_row_ids = project.add_rows(
        target_sheet_id,
        [
            {"company": "Acme Corporation"},
            {"company": "Globex LLC"},
            {"company": "Initech Inc"},
        ],
        target_columns,
    )

    def run(action: dict[str, Any]) -> Any:
        return executor_actions.run_action_spec(
            project,
            action,
            project_id="project-derive-link-table",
            router=ModelRouter(cache=None, cache_mode="off"),
        )

    with mock.patch(
        "frisket.semantic.resolve_embedder",
        lambda router=None, **_kw: (
            lambda texts: [VECS[text] for text in texts],
            "stub/derive-link-table",
        ),
    ):
        gated = run(
            _join_action(source_sheet_id, target_sheet_id),
        )
        assert gated.status == "needs_confirmation"
        promise_set_hash = gated.errors[0].details["promise_set_hash"]
        join_result = run(
            _join_action(
                source_sheet_id,
                target_sheet_id,
                consented_promise_set_hash=promise_set_hash,
            ),
        )
    assert join_result.status == "completed", join_result.errors
    assert join_result.run_id is not None

    source_cols = {
        column["name"]: column for column in project.columns(source_sheet_id)
    }
    target_row_id_column = int(source_cols["company_match_row_id"]["id"])
    assert (
        project.get_values(source_sheet_id, target_row_id_column)[source_row_ids[2]]
        is None
    )

    undone_review = run(
        _review_action(
            run_id=join_result.run_id,
            row_id=source_row_ids[2],
            column_id=target_row_id_column,
            value=target_row_ids[2],
            key="undone_review_join_row_id@sha256:v1",
        ),
    )
    assert undone_review.status == "completed", undone_review.errors
    undo_result = run(
        operation_action(
            "operation.undo",
            key="undo_undone_review_join_row_id@sha256:v1",
            expected_op_id=undone_review.op_ids[0],
        ),
    )
    assert undo_result.status == "completed", undo_result.errors

    review_result = run(
        _review_action(
            run_id=join_result.run_id,
            row_id=source_row_ids[2],
            column_id=target_row_id_column,
            value=target_row_ids[2],
        ),
    )
    assert review_result.status == "completed", review_result.errors

    return {
        "source_sheet_id": source_sheet_id,
        "target_sheet_id": target_sheet_id,
        "source_row_ids": source_row_ids,
        "target_row_ids": target_row_ids,
        "join_receipt_id": join_result.receipt_id,
        "review_receipt_id": review_result.receipt_id,
        "undone_review_receipt_id": undone_review.receipt_id,
    }


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _derive_link_action(seeded["join_receipt_id"])


def _missing_capability_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _derive_link_action(
        seeded["join_receipt_id"],
        idempotency_key="derive_link_table@sha256:missing-capability",
        capabilities=[],
    )


def _conflicting_name_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # Same idempotency key as the primary derive, different target name.
    return _derive_link_action(
        seeded["join_receipt_id"], target_sheet_name="Different Link Table"
    )


def _duplicate_sheet_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _derive_link_action(
        seeded["join_receipt_id"],
        idempotency_key="derive_link_table@sha256:duplicate",
    )


def _missing_receipt_action(seeded: dict[str, Any]) -> dict[str, Any]:
    del seeded
    return _derive_link_action(
        "receipt_missing",
        target_sheet_name="Missing Source",
        idempotency_key="derive_link_table@sha256:missing-source",
    )


def _bad_scope_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _derive_link_action(
        seeded["join_receipt_id"],
        target_sheet_name="Bad Scope",
        row_ids=[999999],
        source_sheet_id=seeded["source_sheet_id"],
        idempotency_key="derive_link_table@sha256:bad-scope",
    )


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt

    source_row_ids = seeded["source_row_ids"]
    target_row_ids = seeded["target_row_ids"]
    assert len(result.op_ids) == 1
    assert {output.kind for output in result.outputs} == {"sheet", "column", "rows"}
    child_sheet_id = next(
        output.sheet_id for output in result.outputs if output.kind == "sheet"
    )
    child_row_ids = next(
        output.row_ids for output in result.outputs if output.kind == "rows"
    )
    assert len(child_row_ids) == 3

    child = project.db.execute(
        "SELECT * FROM sheets WHERE id=?", (child_sheet_id,)
    ).fetchone()
    assert child["name"] == "Reviewed Semantic Links"
    assert child["parent_sheet_id"] == seeded["source_sheet_id"]
    assert child["parent_op_id"] == result.op_ids[0]

    child_columns = {
        column["name"]: column for column in project.columns(child_sheet_id)
    }
    assert list(child_columns) == [
        "source_row_id",
        "source_value",
        "target_row_id",
        "target_value",
        "match_score",
        "match_value",
    ]
    assert child_columns["source_row_id"]["type"] == "integer"
    assert child_columns["target_row_id"]["type"] == "integer"
    assert child_columns["match_score"]["type"] == "number"
    child_rows = project.db.execute(
        "SELECT id, parent_row_id FROM rows WHERE sheet_id=? ORDER BY position",
        (child_sheet_id,),
    ).fetchall()
    assert [int(row["id"]) for row in child_rows] == child_row_ids
    assert [int(row["parent_row_id"]) for row in child_rows] == source_row_ids

    source_values = project.get_values(
        child_sheet_id, int(child_columns["source_value"]["id"])
    )
    target_values = project.get_values(
        child_sheet_id, int(child_columns["target_value"]["id"])
    )
    derived_target_row_ids = project.get_values(
        child_sheet_id, int(child_columns["target_row_id"]["id"])
    )
    assert list(source_values.values()) == [
        "ACME Corp",
        "Globex",
        "Umbrella Holdings",
    ]
    assert list(target_values.values()) == [
        "Acme Corporation",
        "Globex LLC",
        "Initech Inc",
    ]
    # the third edge exists only because the live review filled the match in
    assert list(derived_target_row_ids.values()) == target_row_ids

    receipt_row = project.db.execute(
        "SELECT * FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row["run_id"] is None
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.action_kind == "derive.link_table"
    assert receipt.op_ids == result.op_ids
    input_ref = next(
        item.ref
        for item in receipt.inputs
        if item.ref["kind"] == "semantic_join_link_source"
    )
    assert input_ref["source_receipt_id"] == seeded["join_receipt_id"]
    assert input_ref["source_sheet_id"] == seeded["source_sheet_id"]
    assert input_ref["target_sheet_id"] == seeded["target_sheet_id"]
    assert input_ref["source_row_ids"] == source_row_ids
    assert input_ref["requested_row_ids"] == source_row_ids
    assert input_ref["materialized_source_row_ids"] == source_row_ids
    assert input_ref["target_row_ids"] == target_row_ids
    assert input_ref["may_feed"] == ["export.work_log"]
    refs = [
        item.ref
        for section in (receipt.inputs, receipt.outputs, receipt.evidence)
        for item in section
    ]
    assert {
        "semantic_join_output_cells",
        "semantic_link_table_edges",
        "semantic_join_review_decisions",
        "materialized_sheet",
        "materialized_rows",
    } <= {ref["kind"] for ref in refs}
    edge_ref = next(ref for ref in refs if ref["kind"] == "semantic_link_table_edges")
    assert edge_ref["source_row_ids"] == source_row_ids
    assert edge_ref["target_row_ids"] == target_row_ids
    assert edge_ref["child_row_ids"] == child_row_ids
    assert edge_ref["edges"][2]["target_value"] == "Initech Inc"
    # only the live review is folded in; the undone review does not reach the
    # link table's provenance
    review_ref = next(
        ref for ref in refs if ref["kind"] == "semantic_join_review_decisions"
    )
    assert review_ref["receipt_ids"] == [seeded["review_receipt_id"]]
    assert review_ref["reviewed_row_ids"] == [source_row_ids[2]]
    assert seeded["undone_review_receipt_id"] not in review_ref["receipt_ids"]


CASES = [
    ExecutorCase(
        kind="derive.link_table",
        request_style="typed",
        catalog=CatalogEntry(
            execution_mode="whole_project",
            async_mode="sync",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write",),
            side_effects=frozenset(
                {
                    "read_semantic_join_receipt",
                    "read_current_join_outputs",
                    "create_sheet",
                    "create_columns",
                    "create_rows",
                    "write_op",
                    "write_receipt",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_action_request",
                    "invalid_input_ref",
                    "duplicate_sheet_name",
                    "stale_replay",
                    "idempotency_conflict",
                }
            ),
            cost_policy_kind="none",
        ),
        seed=_seed,
        make_action=_make_action,
        gates=(
            Gate(
                "request_capabilities_are_not_authority",
                _missing_capability_action,
                "invalid_action_request",
            ),
            Gate(
                "invalid_input_ref_missing_receipt",
                _missing_receipt_action,
                "invalid_input_ref",
            ),
            Gate(
                "invalid_input_ref_bad_scope",
                _bad_scope_action,
                "invalid_input_ref",
            ),
            Gate(
                "idempotency_conflict",
                _conflicting_name_action,
                "idempotency_conflict",
                after_primary_run=True,
            ),
            Gate(
                "duplicate_sheet_name",
                _duplicate_sheet_action,
                "duplicate_sheet_name",
                after_primary_run=True,
            ),
        ),
        expect_counts={
            "sheets": 1,
            "columns": 6,
            "rows": 3,
            "edits": 0,
            "ops": 1,
            "receipts": 1,
        },
        check_state=_check_state,
    )
]


def test_link_table_replay_rejects_stale_materialized_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tampering with a materialized edge cell invalidates the recorded link
    table: replaying the same spec is refused."""
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        child_sheet_id = next(
            output.sheet_id for output in first.outputs if output.kind == "sheet"
        )
        child_row_ids = next(
            output.row_ids for output in first.outputs if output.kind == "rows"
        )
        target_value_column = env.project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='target_value'",
            (child_sheet_id,),
        ).fetchone()
        replace_test_source_cell(
            env.project,
            row_id=child_row_ids[0],
            column_id=int(target_value_column["id"]),
            value="tampered target value",
        )
        replay = env.run_primary()
        assert replay.status == "failed"
        assert replay.errors[0].code == "stale_replay"


def test_link_table_review_selection_uses_exact_result_and_applied_op(
    tmp_path: Path,
) -> None:
    from frisket.contracts.action import Receipt, ReceiptIO
    from frisket.engine.executor.semantic_match_read import (
        _derive_link_table_review_refs,
    )
    from frisket.engine.store.receipts import ReceiptStore

    project = Project.create(tmp_path / "review-selection.frisket")
    try:

        def add_review(
            receipt_id: str,
            *,
            run_id: int,
            row_id: int,
            column_id: int,
        ) -> int:
            op_id = project.append_op("review_decision")
            ReceiptStore(project).insert(
                Receipt(
                    receipt_id=receipt_id,
                    project_id="review-selection",
                    action_id=f"action-{receipt_id}",
                    action_kind="review.decision",
                    op_ids=[op_id],
                    status="completed",
                    inputs=[
                        ReceiptIO(
                            name="target",
                            ref={
                                "kind": "target_result_cell",
                                "run_id": run_id,
                                "row_id": row_id,
                                "column_id": column_id,
                            },
                        )
                    ],
                    review={"decision": "accept"},
                )
            )
            return op_id

        exact_op = add_review("exact", run_id=7, row_id=100, column_id=10)
        add_review("same-row-wrong-run", run_id=8, row_id=100, column_id=10)
        add_review("same-row-wrong-column", run_id=7, row_id=100, column_id=99)
        add_review("wrong-row", run_id=7, row_id=101, column_id=10)
        undone_op = add_review("undone-exact", run_id=7, row_id=100, column_id=11)
        assert project.undo() == undone_op

        review_refs = _derive_link_table_review_refs(
            project,
            source_run_id=7,
            output_column_ids={10, 11},
            selected_row_ids=[100],
        )
        assert review_refs["receipt_ids"] == ["exact"]
        assert review_refs["reviewed_row_ids"] == [100]
        assert review_refs["decisions"] == [
            {
                "receipt_id": "exact",
                "row_id": 100,
                "column_id": 10,
                "decision": "accept",
                "op_ids": [exact_op],
            }
        ]
    finally:
        project.close()
