"""Semantic consumers follow admitted producer identity, not a builtin ID."""

import json
from dataclasses import replace

import pytest

from frisket.actions.core import RegisteredAction
from frisket.actions.registry import ACTION_REGISTRY
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore


@pytest.fixture
def completed_custom_join(tmp_path, monkeypatch):
    definition = replace(ACTION_REGISTRY.get("join.semantic").definition, name="match")
    monkeypatch.setattr(
        ACTION_REGISTRY,
        "_actions",
        {
            **ACTION_REGISTRY._actions,
            "custom.match": RegisteredAction("custom.match", definition),
        },
    )
    monkeypatch.setattr(
        "frisket.semantic.resolve_embedder",
        lambda *a, **kw: (lambda texts: [[1.0, 0.0] for _ in texts], "fastembed/test"),
    )
    project = Project.create(tmp_path / "semantic-consumer.frisket")
    try:
        left, right = project.add_sheet("Left"), project.add_sheet("Right")
        source = project.add_column(left, "source", type="text")
        target = project.add_column(right, "target", type="text")
        rows = project.add_rows(left, [{"source": "Acme"}], {"source": source})
        project.add_rows(right, [{"target": "ACME"}], {"target": target})
        project.db.commit()
        request = {
            "action_id": "custom.match",
            "scope": {"kind": "sheet_rows", "sheet_id": left},
            "params": {
                "source": "source",
                "target": {"sheet_id": right, "column": "target"},
            },
            "sheet_name": "Matches",
            "idempotency_key": "match",
        }
        result = run_action_spec(project, request, project_id="test")
        assert result.status == "completed", result.errors
        yield project, result, left, rows
    finally:
        project.close()


def link_request(receipt_id):
    return {
        "action_id": "derive.link_table",
        "scope": {"kind": "project"},
        "params": {"source": {"kind": "semantic_join", "receipt_id": receipt_id}},
        "sheet_name": "Linked",
        "idempotency_key": "link",
    }


def test_custom_semantic_receipt_feeds_link_table(completed_custom_join):
    project, matched, left, rows = completed_custom_join
    result = run_action_spec(
        project, link_request(matched.receipt_id), project_id="test"
    )
    assert result.status == "completed", result.errors
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    fact = next(
        item.ref
        for item in (*receipt.inputs, *receipt.evidence)
        if item.ref.get("kind") == "semantic_join_link_source"
    )
    assert fact["source_action_kind"] == "custom.match"
    assert fact["source_sheet_id"] == left
    assert fact["source_row_ids"] == rows


@pytest.mark.parametrize(
    "replacement_kind", ["map.regex_extract", "unknown.match", "run.backfill"]
)
def test_semantic_refs_alone_do_not_authorize_unrelated_receipt(
    completed_custom_join, replacement_kind
):
    project, matched, _, _ = completed_custom_join
    receipt = ReceiptStore(project).parsed_by_id(matched.receipt_id)
    changed = receipt.model_copy(update={"action_kind": replacement_kind})
    project.db.execute(
        "UPDATE receipts SET body=? WHERE id=?",
        (changed.model_dump_json(), receipt.receipt_id),
    )
    project.db.commit()
    result = run_action_spec(
        project, link_request(matched.receipt_id), project_id="test"
    )
    assert result.status == "failed"
    assert result.errors[0].code == "invalid_input_ref"


def test_semantic_source_run_must_match_receipt_run(completed_custom_join):
    project, matched, _, _ = completed_custom_join
    receipt = ReceiptStore(project).parsed_by_id(matched.receipt_id)
    body = receipt.model_dump(mode="json")
    next(
        item["ref"]
        for item in body["inputs"]
        if item["ref"].get("kind") == "semantic_join_source_sheet"
    )["run_id"] += 1
    project.db.execute(
        "UPDATE receipts SET body=? WHERE id=?", (json.dumps(body), receipt.receipt_id)
    )
    project.db.commit()
    result = run_action_spec(
        project, link_request(matched.receipt_id), project_id="test"
    )
    assert result.status == "failed"
    assert result.errors[0].code == "invalid_input_ref"


def test_managed_semantic_cells_do_not_depend_on_compatibility_column_pointer(
    completed_custom_join,
):
    project, matched, _, _ = completed_custom_join
    receipt = ReceiptStore(project).parsed_by_id(matched.receipt_id)
    columns = [
        item.ref["column_id"]
        for item in receipt.outputs
        if item.ref.get("kind") == "semantic_join_output_column"
    ]
    project.db.executemany(
        "UPDATE columns SET current_run_id=NULL WHERE id=?",
        [(column,) for column in columns],
    )
    project.db.commit()
    result = run_action_spec(
        project, link_request(matched.receipt_id), project_id="test"
    )
    assert result.status == "completed", result.errors


def test_managed_semantic_cells_still_require_actual_generation_heads(
    completed_custom_join,
):
    project, matched, _, _ = completed_custom_join
    receipt = ReceiptStore(project).parsed_by_id(matched.receipt_id)
    column = next(
        item.ref["column_id"]
        for item in receipt.outputs
        if item.ref.get("kind") == "semantic_join_output_column"
    )
    project.db.execute("DELETE FROM cell_result_heads WHERE column_id=?", (column,))
    project.db.commit()
    result = run_action_spec(
        project, link_request(matched.receipt_id), project_id="test"
    )
    assert result.status == "failed"
    assert result.errors[0].code == "invalid_input_ref"
