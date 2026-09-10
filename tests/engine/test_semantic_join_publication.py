from types import SimpleNamespace
from dataclasses import replace

import pytest

from frisket.actions.semantic_match_types import SemanticJoinSource
from frisket.actions.types import ProjectScope
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.executor.semantic_match_read import AdmittedSemanticMatchReader
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.actions.core import RegisteredAction, semantic_join
from frisket.actions.types import (
    ActionParams,
    ColumnRef,
    Row,
    RowResult,
    SheetColumnRef,
)
from frisket.actions.semantic_join_types import SemanticJoinMatch, SemanticMatcher
from frisket.actions.registry import ACTION_REGISTRY


class RenamedJoinParams(ActionParams):
    donor: ColumnRef[str]
    registry: SheetColumnRef


async def renamed_match(
    params: RenamedJoinParams, row: Row, matcher: SemanticMatcher
) -> RowResult[SemanticJoinMatch]:
    return RowResult(
        output=await matcher.match(params.donor.read(row), target=params.registry)
    )


def test_custom_semantic_action_uses_declared_refs_not_builtin_names(case, monkeypatch):
    original = ACTION_REGISTRY.get("join.semantic")
    custom = RegisteredAction(
        "custom.match_donors",
        replace(
            original.definition, run=semantic_join(renamed_match), _example_params=()
        ),
    )
    monkeypatch.setattr(
        ACTION_REGISTRY,
        "_actions",
        {**ACTION_REGISTRY._actions, custom.action_id: custom},
    )
    result = case.run(
        action_id=custom.action_id,
        params={
            "donor": "donor",
            "registry": {"sheet_id": case.right, "column": "company"},
        },
    )
    assert result.status == "completed", result
    assert result.action.kind == custom.action_id
    receipt = ReceiptStore(case.project).parsed_by_id(result.receipt_id)
    assert (
        len(
            [
                item
                for item in receipt.outputs
                if item.ref.get("kind") == "semantic_join_output_column"
            ]
        )
        == 3
    )


@pytest.fixture
def case(tmp_path, monkeypatch):
    calls = []

    def embed(texts):
        calls.append(list(texts))
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(
        "frisket.semantic.resolve_embedder", lambda *a, **kw: (embed, "fastembed/test")
    )
    project = Project.create(tmp_path / "review.frisket")
    left = project.add_sheet("Donors")
    donor = project.add_column(left, "donor")
    [left_row] = project.add_rows(left, [{"donor": "ACME"}], {"donor": donor})
    right = project.add_sheet("Companies")
    company = project.add_column(right, "company")
    [right_row] = project.add_rows(right, [{"company": "Acme"}], {"company": company})
    request = dict(
        action_id="join.semantic",
        scope={"kind": "sheet_rows", "sheet_id": left},
        params={"source": "donor", "target": {"sheet_id": right, "column": "company"}},
        sheet_name="Matches",
        idempotency_key="review-one",
    )

    def run(**changes):
        return run_action_spec(project, {**request, **changes}, project_id="review")

    try:
        yield SimpleNamespace(
            project=project,
            run=run,
            left=left,
            donor=donor,
            left_row=left_row,
            right=right,
            company=company,
            right_row=right_row,
            calls=calls,
        )
    finally:
        project.close()


def test_actual_receipt_can_feed_match_reader(case):
    result = case.run()
    assert result.status == "completed", result
    reader = AdmittedSemanticMatchReader(
        case.project, scope=ProjectScope(), action_kind="derive.link_table"
    )
    matches = reader.read(
        SemanticJoinSource(kind="semantic_join", receipt_id=result.receipt_id)
    )
    assert matches[0].value.source_value == "ACME"
    assert matches[0].value.target_value == "Acme"
    receipt = ReceiptStore(case.project).parsed_by_id(result.receipt_id)
    thresholds = next(
        item.ref
        for item in receipt.evidence
        if item.ref.get("kind") == "semantic_join_thresholds"
    )
    assert thresholds["match_threshold"] == 0.7
    assert thresholds["confident_threshold"] == 0.85
    backend = next(
        item.ref
        for item in receipt.evidence
        if item.ref.get("kind") == "semantic_join_embedding_backend"
    )
    assert backend["model"] == "fastembed/test"


def test_changed_input_during_matching_aborts_without_permanent_claim(
    case, monkeypatch
):
    changed = False

    def embed(texts):
        nonlocal changed
        if not changed:
            changed = True
            case.project.apply_edits(
                [{"row_id": case.left_row, "column_id": case.donor, "value": "Changed"}]
            )
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(
        "frisket.semantic.resolve_embedder", lambda *a, **kw: (embed, "fastembed/test")
    )
    result = case.run()
    assert result.status == "failed", result
    assert result.errors[0].code == "stale_input"
    assert case.run().receipt_id == result.receipt_id
    assert (
        case.project.db.execute(
            "SELECT COUNT(*) FROM output_column_claims WHERE status='active'"
        ).fetchone()[0]
        == 0
    )
    assert (
        case.project.db.execute("SELECT COUNT(*) FROM cell_result_heads").fetchone()[0]
        == 0
    )
    assert {col["name"] for col in case.project.columns(case.left)} == {"donor"}


def test_source_pin_refusal_at_execution_start_closes_receipt(case, monkeypatch):
    from frisket.engine.executor.semantic_join_program import SemanticJoinSourceChanged

    def changed(*args, **kwargs):
        raise SemanticJoinSourceChanged("source changed before execution")

    monkeypatch.setattr(
        "frisket.engine.executor.semantic_join_program.validate_semantic_join_pins",
        changed,
    )
    result = case.run()
    assert result.status == "failed", result
    assert result.errors[0].code == "promise_violation"
    assert case.run().receipt_id == result.receipt_id
    assert not case.project.db.execute(
        "SELECT 1 FROM output_column_claims WHERE status='active'"
    ).fetchone()
    assert not case.project.db.execute("SELECT 1 FROM cell_result_heads").fetchone()
    assert not any(sheet["name"] == "Matches" for sheet in case.project.sheets())


@pytest.mark.parametrize("side", ["source", "target"])
def test_replay_refuses_changed_matching_inputs(case, side):
    first = case.run()
    assert first.status == "completed", first
    row, column = (
        (case.left_row, case.donor)
        if side == "source"
        else (case.right_row, case.company)
    )
    case.project.apply_edits(
        [{"row_id": row, "column_id": column, "value": "Changed"}],
        label="change join input",
    )
    replay = case.run()
    assert replay.status == "failed", replay
    assert replay.errors[0].code == "stale_replay"


def test_source_replacement_preserves_original_in_child(case):
    first = case.run()
    assert first.status == "completed", first
    case.project.apply_edits(
        [{"row_id": case.right_row, "column_id": case.company, "value": "Acme2"}],
        label="change target spelling",
    )
    result = case.run(
        params={
            "source": "match_value",
            "target": {"sheet_id": case.right, "column": "company"},
        },
        sheet_name="Matches2",
        idempotency_key="review-two",
        replace_existing=True,
    )
    assert result.status == "completed", result
    child = next(
        sheet for sheet in case.project.sheets() if sheet["name"] == "Matches2"
    )
    original = next(
        column
        for column in case.project.columns(child["id"])
        if column["name"] == "source"
    )
    assert list(case.project.get_values(child["id"], original["id"]).values()) == [
        "Acme"
    ]


def test_final_child_failure_publishes_no_source_heads(case, monkeypatch):
    from frisket.engine.executor.semantic_join_action import _materialize

    def fail(*args, **kwargs):
        raise RuntimeError("publication failed")

    monkeypatch.setattr(
        "frisket.engine.executor.semantic_join_action._materialize", fail
    )
    with pytest.raises(RuntimeError, match="publication failed"):
        case.run()
    assert (
        case.project.db.execute("SELECT COUNT(*) FROM cell_result_heads").fetchone()[0]
        == 0
    )
    assert {column["name"] for column in case.project.columns(case.left)} == {"donor"}
    assert not any(sheet["name"] == "Matches" for sheet in case.project.sheets())
    calls = list(case.calls)
    monkeypatch.setattr(
        "frisket.engine.executor.semantic_join_action._materialize", _materialize
    )
    retried = case.run()
    assert retried.status == "completed", retried
    assert case.calls == calls
    assert case.project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


@pytest.mark.parametrize("matches", [False, True])
def test_child_retains_target_dependency_even_without_matches(
    case, monkeypatch, matches
):
    from frisket.engine.store.sheet_lifecycle import (
        dependent_sheets,
        materialized_source_sheets,
    )

    if not matches:
        monkeypatch.setattr(
            "frisket.semantic.resolve_embedder",
            lambda *a, **kw: (
                lambda texts: [
                    [1.0, 0.0] if text == "ACME" else [0.0, 1.0] for text in texts
                ],
                "fastembed/test",
            ),
        )
    result = case.run()
    assert result.status == "completed", result
    child = next(sheet for sheet in case.project.sheets() if sheet["name"] == "Matches")
    assert bool(case.project.visible_row_ids(child["id"])) == matches
    assert child["id"] in {
        item["id"] for item in dependent_sheets(case.project, case.right)
    }
    assert materialized_source_sheets(case.project)[child["id"]] == {
        case.left,
        case.right,
    }
