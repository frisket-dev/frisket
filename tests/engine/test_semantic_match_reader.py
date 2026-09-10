from __future__ import annotations

import json

import pytest

from frisket.actions.core import (
    ActionCategory,
    ActionNamespace,
    ActionRegistry,
    action,
    create_sheet,
)
from frisket.actions.semantic_match_types import (
    SemanticJoinSource,
    SemanticMatchReader,
    SemanticMatchValues,
)
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    RowSource,
    TableResult,
    TableRow,
)
from frisket.engine.executor import run_action_spec
from frisket.engine.executor.table_action import run_typed_create_sheet_action
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.sheet_lifecycle import (
    SheetDeleteBlocked,
    delete_sheet,
    dependent_sheets,
)
from frisket.engine.store.staleness import sheet_is_stale
from tests.engine.test_derive_link_table_executor import (
    _derive_link_action,
    _seed,
    operation_action,
)


@pytest.fixture
def semantic_project(tmp_path, monkeypatch):
    project = Project.create(tmp_path / "semantic.frisket")
    try:
        seed = _seed(project, tmp_path)

        def forbidden(*args, **kwargs):
            raise AssertionError(
                "link materialization must not repeat semantic compute"
            )

        monkeypatch.setattr("frisket.semantic.resolve_embedder", forbidden)
        yield project, seed
    finally:
        project.close()


def _run(project, request):
    return run_action_spec(project, request, project_id="links")


def _counts(project):
    return {
        table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "sheets",
            "columns",
            "rows",
            "cells",
            "ops",
            "receipts",
            "materialized_row_sources",
        )
    }


def _receipt(project, result):
    return ReceiptStore(project).parsed_by_id(result.receipt_id)


def _sheet(result):
    return next(output.sheet_id for output in result.outputs if output.kind == "sheet")


def test_exact_selected_membership_output_names_and_replay(semantic_project):
    project, seed = semantic_project
    chosen = [seed["source_row_ids"][2], seed["source_row_ids"][0]]
    request = _derive_link_action(
        seed["join_receipt_id"], row_ids=chosen, source_sheet_id=seed["source_sheet_id"]
    )
    request["output_names"] = {"source_value": "Donor", "target_value": "Company"}
    result = _run(project, request)
    assert result.status == "completed", result.errors
    columns = {row["name"]: row for row in project.columns(_sheet(result))}
    assert list(columns) == [
        "source_row_id",
        "Donor",
        "target_row_id",
        "Company",
        "match_score",
        "match_value",
    ]
    assert list(
        project.get_values(_sheet(result), columns["source_row_id"]["id"]).values()
    ) == sorted(chosen)
    before = _counts(project)
    assert _run(project, request).receipt_id == result.receipt_id
    assert _counts(project) == before


@pytest.mark.parametrize(
    "scope_kind", ["foreign_sheet", "implicit_all", "empty", "foreign_row"]
)
def test_scope_does_not_escape_receipt(semantic_project, scope_kind):
    project, seed = semantic_project
    scope = {
        "kind": "sheet_rows",
        "sheet_id": seed["source_sheet_id"],
        "row_ids": seed["source_row_ids"][:1],
    }
    if scope_kind == "foreign_sheet":
        scope["sheet_id"] = seed["target_sheet_id"]
    elif scope_kind == "implicit_all":
        scope.pop("row_ids")
    elif scope_kind == "empty":
        scope["row_ids"] = []
    else:
        scope["row_ids"] = seed["target_row_ids"][:1]
    request = _derive_link_action(seed["join_receipt_id"])
    request["scope"] = scope
    before = _counts(project)
    result = _run(project, request)
    assert result.status == "failed"
    assert result.errors[0].code in {
        "invalid_input_ref",
        "invalid_action_request",
        "invalid_params",
    }
    assert _counts(project) == before


@pytest.mark.parametrize("include_unmatched", [False, True])
def test_empty_or_unmatched_links_retain_both_sheet_dependencies(
    semantic_project, include_unmatched
):
    project, seed = semantic_project
    target_id_column = next(
        c["id"]
        for c in project.columns(seed["source_sheet_id"])
        if c["name"] == "company_match_row_id"
    )
    project.apply_edits(
        [
            {"row_id": row, "column_id": target_id_column, "value": None}
            for row in seed["source_row_ids"]
        ]
    )
    request = _derive_link_action(seed["join_receipt_id"])
    request["params"]["include_unmatched"] = include_unmatched
    result = _run(project, request)
    assert result.status == "completed", result.errors
    child = _sheet(result)
    rows = next(o.row_ids for o in result.outputs if o.kind == "rows")
    assert len(rows) == (3 if include_unmatched else 0)
    assert not project.db.execute(
        "SELECT 1 FROM materialized_row_sources WHERE op_id=? AND role='edge_target'",
        (result.op_ids[0],),
    ).fetchone()
    for parent in (seed["source_sheet_id"], seed["target_sheet_id"]):
        assert child in {item["id"] for item in dependent_sheets(project, parent)}
        with pytest.raises(SheetDeleteBlocked) as blocked:
            delete_sheet(project, parent)
        assert child in blocked.value.dependent_sheet_ids
    assert not sheet_is_stale(project, child)
    company = next(
        c["id"]
        for c in project.columns(seed["target_sheet_id"])
        if c["name"] == "company"
    )
    project.apply_edits(
        [
            {
                "row_id": seed["target_row_ids"][0],
                "column_id": company,
                "value": "Changed",
            }
        ]
    )
    assert sheet_is_stale(project, child)


@pytest.mark.parametrize(
    "mutation",
    [
        "source",
        "target",
        "review_undo",
        "hidden_source",
        "hidden_target",
        "hidden_source_sheet",
        "hidden_target_sheet",
        "upstream_undo",
    ],
)
def test_replay_revalidates_actual_matches_without_provider(semantic_project, mutation):
    project, seed = semantic_project
    request = _derive_link_action(seed["join_receipt_id"])
    result = _run(project, request)
    assert result.status == "completed", result.errors
    if mutation in {"source", "target"}:
        sheet_id = seed[f"{mutation}_sheet_id"]
        column = project.columns(sheet_id)[0]["id"]
        project.apply_edits(
            [
                {
                    "row_id": seed[f"{mutation}_row_ids"][0],
                    "column_id": column,
                    "value": "Changed",
                }
            ]
        )
    elif mutation.endswith("_sheet"):
        parent = mutation.removeprefix("hidden_").removesuffix("_sheet")
        project.db.execute(
            "UPDATE sheets SET hidden=1 WHERE id=?", (seed[f"{parent}_sheet_id"],)
        )
        project.db.commit()
    elif mutation.startswith("hidden_"):
        parent = mutation.removeprefix("hidden_")
        project.db.execute(
            "UPDATE rows SET hidden=1 WHERE id=?", (seed[f"{parent}_row_ids"][0],)
        )
        project.db.commit()
    else:
        receipt = ReceiptStore(project).parsed_by_id(
            seed[
                "review_receipt_id" if mutation == "review_undo" else "join_receipt_id"
            ]
        )
        project.db.execute(
            "UPDATE ops SET status='undone' WHERE id=?", (receipt.op_ids[0],)
        )
        project.db.commit()
    before = _counts(project)
    replay = _run(project, request)
    assert replay.status == "failed"
    assert replay.errors[0].code == "stale_replay"
    assert _counts(project) == before


class CustomMatchParams(ActionParams):
    receipt: str
    mode: str = "valid"


def _custom_action(project):
    def produce(
        params: CustomMatchParams, reader: SemanticMatchReader
    ) -> TableResult[SemanticMatchValues]:
        matches = reader.read(
            SemanticJoinSource(kind="semantic_join", receipt_id=params.receipt)
        )
        match = matches[0]
        source, target, parent = match.source, match.target, match.source
        if params.mode == "forged":
            source = parent = RowSource(sheet_id=source.sheet_id, row_id=source.row_id)
        elif params.mode == "swapped_target":
            target = matches[1].target
        elif params.mode == "target_parent":
            parent = target
        elif params.mode == "missing_target":
            target = None
        elif params.mode == "no_parent":
            parent = None
        elif params.mode == "changed_after_read":
            column = project.columns(source.sheet_id)[0]["id"]
            project.apply_edits(
                [
                    {
                        "row_id": source.row_id,
                        "column_id": column,
                        "value": "Changed during producer",
                    }
                ]
            )
        elif params.mode == "mutated_output":
            match.value.target_row_id = 987654
            match.value.target_value = "Authored output"
        return TableResult(
            rows=[
                TableRow(
                    output=match.value,
                    sources=(source, target) if target else (source,),
                    parent=parent,
                )
            ],
            source={"kind": "file", "path": "forged-authority", "cost_actual": 999},
        )

    return ActionRegistry(
        (
            ActionNamespace(
                "custom",
                actions=(
                    action(
                        name="matches",
                        title="Matches",
                        description="Actual completed match capability",
                        category=ActionCategory.CONVERT,
                        run=create_sheet(produce),
                    ),
                ),
            ),
        )
    ).get("custom.matches")


@pytest.mark.parametrize(
    "mode",
    [
        "valid",
        "forged",
        "swapped_target",
        "target_parent",
        "missing_target",
        "no_parent",
        "changed_after_read",
        "mutated_output",
    ],
)
def test_custom_author_values_never_own_lineage_or_receipt_source(
    semantic_project, mode
):
    project, seed = semantic_project
    registered = _custom_action(project)
    request = ActionRequest(
        action_id="custom.matches",
        scope={"kind": "project"},
        params={"receipt": seed["join_receipt_id"], "mode": mode},
        sheet_name="Custom",
        idempotency_key="custom",
    )
    before = _counts(project)
    result = run_typed_create_sheet_action(
        project, "links", BoundTypedActionRequest.bind(registered, request)
    )
    if mode not in {"valid", "mutated_output"}:
        assert result.status == "failed", result.errors
        assert not project.db.execute(
            "SELECT 1 FROM sheets WHERE name='Custom'"
        ).fetchone()
        if mode != "changed_after_read":
            assert _counts(project) == before
        else:
            assert result.errors[0].code == "stale_replay"
        return
    assert result.status == "completed", result.errors
    receipt = _receipt(project, result)
    assert "forged-authority" not in json.dumps(receipt.model_dump(mode="json"))
    edges = next(
        e.ref["edges"]
        for e in receipt.evidence
        if e.ref.get("kind") == "semantic_link_table_edges"
    )
    assert edges[0]["target_row_id"] == seed["target_row_ids"][0]
    assert edges[0]["target_value"] == "Acme Corporation"
    assert receipt.provider_use == []


def test_late_receipt_failure_rolls_back_table_and_both_memberships(
    semantic_project, monkeypatch
):
    project, seed = semantic_project
    before = _counts(project)

    def fail(*args, **kwargs):
        raise RuntimeError("injected receipt write failure")

    monkeypatch.setattr(ReceiptStore, "insert", fail)
    result = _run(project, _derive_link_action(seed["join_receipt_id"]))
    assert result.status == "failed"
    assert _counts(project) == before


def test_empty_result_still_pins_read_source_cells(semantic_project):
    project, seed = semantic_project
    target_id_column = next(
        c["id"]
        for c in project.columns(seed["source_sheet_id"])
        if c["name"] == "company_match_row_id"
    )
    project.apply_edits(
        [
            {"row_id": row, "column_id": target_id_column, "value": None}
            for row in seed["source_row_ids"]
        ]
    )
    request = _derive_link_action(seed["join_receipt_id"])
    result = _run(project, request)
    assert result.status == "completed", result.errors
    source_column = project.columns(seed["source_sheet_id"])[0]["id"]
    project.apply_edits(
        [
            {
                "row_id": seed["source_row_ids"][0],
                "column_id": source_column,
                "value": "Changed unmatched source",
            }
        ]
    )
    replay = _run(project, request)
    assert replay.status == "failed"
    assert replay.errors[0].code == "stale_replay"


def test_materialized_link_undo_redo_and_preview_do_not_repeat_provider(
    semantic_project,
):
    from frisket.engine.executor.actions import resolve_map_preview

    project, seed = semantic_project
    request = _derive_link_action(seed["join_receipt_id"])
    before = _counts(project)
    preview = resolve_map_preview(project, request)
    assert preview.code == "unsupported_action_kind"
    assert _counts(project) == before
    result = _run(project, request)
    assert result.status == "completed", result.errors
    undone = _run(
        project,
        operation_action(
            "operation.undo", key="undo-links", expected_op_id=result.op_ids[0]
        ),
    )
    assert undone.status == "completed", undone.errors
    replay = _run(project, request)
    assert replay.status == "failed"
    assert replay.errors[0].code == "stale_replay"
    redone = _run(
        project,
        operation_action(
            "operation.redo", key="redo-links", expected_op_id=result.op_ids[0]
        ),
    )
    assert redone.status == "completed", redone.errors
    replay = _run(project, request)
    assert replay.status == "completed", replay.errors
    assert replay.receipt_id == result.receipt_id


@pytest.mark.parametrize("damage", ["wrong_run", "nonfeedable", "missing_read_field"])
def test_receipt_admission_and_replay_do_not_trust_damaged_refs(
    semantic_project, damage
):
    project, seed = semantic_project
    request = _derive_link_action(seed["join_receipt_id"])
    if damage == "missing_read_field":
        result = _run(project, request)
        assert result.status == "completed", result.errors
        receipt_id = result.receipt_id
    else:
        receipt_id = seed["join_receipt_id"]
    body = ReceiptStore(project).parsed_by_id(receipt_id).model_dump(mode="json")
    if damage == "missing_read_field":
        body["inputs"][0]["ref"].pop("requested_row_ids")
    else:
        for output in body["outputs"]:
            if output["ref"].get("kind") == "semantic_join_output_column":
                output["ref"]["run_id" if damage == "wrong_run" else "may_feed"] = (
                    99999 if damage == "wrong_run" else []
                )
    project.db.execute(
        "UPDATE receipts SET body=? WHERE id=?", (json.dumps(body), receipt_id)
    )
    project.db.commit()
    before = _counts(project)
    result = _run(project, request)
    assert result.status == "failed"
    assert result.errors[0].code == (
        "stale_replay" if damage == "missing_read_field" else "invalid_input_ref"
    )
    assert _counts(project) == before
