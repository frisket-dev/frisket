from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from frisket.actions.semantic_join import SemanticJoinParams, match_one
from frisket.actions.semantic_join_types import SemanticJoinMatch
from frisket.actions.types import ActionParams, ColumnRef, Outcome, Row, SheetColumnRef
from frisket.engine.executor.semantic_join_matcher import AdmittedSemanticMatcher
from frisket.ops.base import OpContext


@pytest.fixture
def matcher(monkeypatch):
    vectors = {
        "Acme": [1.0, 0.0],
        "Globex": [0.0, 1.0],
        "ACME": [1.0, 0.0],
        "gray": [0.6, 0.8],
        "neither": [-1.0, -1.0],
    }
    batches = []

    async def cached(project, docs, embed, model_id, **kwargs):
        texts = [doc["content"] for doc in docs]
        batches.append(texts)
        return [vectors[text] for text in texts]

    monkeypatch.setattr("frisket.semantic._doc_vectors_async", cached)
    target = SheetColumnRef(sheet_id=2, column="company")
    admitted = AdmittedSemanticMatcher(
        target=target,
        values=((10, "Acme"), (11, "Globex")),
        backend=(None, "fastembed/test"),
        estimated_model="fastembed/test",
    )
    return admitted, target, batches


def test_match_preserves_confident_gray_and_unmatched_outcomes(matcher):
    admitted, target, batches = matcher

    async def run():
        results = []
        for value in ("ACME", "gray", "neither", None):
            bound = admitted.bind_row(OpContext(project=SimpleNamespace()))
            result = await bound.match(value, target=target)
            bound.validate_output(result)
            results.append(result)
        return results

    confident, gray, unmatched, empty = asyncio.run(run())
    assert confident.matched_row_id.value == 10
    assert confident.match_score.value == 1
    assert confident.match_value.justification is None
    assert gray.matched_row_id.value == 11
    assert gray.match_value.confidence == 0.8
    assert "gray-band" in gray.match_value.justification
    assert unmatched.matched_row_id.value is None
    assert unmatched.match_score.value == 0
    assert "no match" in unmatched.match_value.justification
    assert empty.match_value.value is None
    assert empty.match_score.value is None
    assert batches.count(["Acme", "Globex"]) == 1


def test_target_mismatch_and_fabricated_result_rejected_before_publication(matcher):
    admitted, target, batches = matcher
    bound = admitted.bind_row(OpContext())
    with pytest.raises(ValueError, match="admitted column"):
        asyncio.run(
            bound.match("ACME", target=target.model_copy(update={"sheet_id": 3}))
        )
    assert batches == []
    result = asyncio.run(bound.match("ACME", target=target))
    with pytest.raises(ValueError, match="admitted match result"):
        bound.validate_output(
            result.model_copy(update={"matched_row_id": Outcome.ok(999)})
        )
    with pytest.raises(ValueError, match="one semantic match"):
        asyncio.run(bound.match("ACME", target=target))


def test_actual_call_supports_renamed_params_without_builtin_field_inspection(matcher):
    admitted, target, _ = matcher

    class Renamed(ActionParams):
        donor: ColumnRef[str]
        registry: SheetColumnRef

    async def custom(params, row, matcher):
        return await matcher.match(params.donor.read(row), target=params.registry)

    params = Renamed(donor=ColumnRef("renamed"), registry=target)
    bound = admitted.bind_row(OpContext())
    result = asyncio.run(custom(params, Row({"renamed": "ACME"}), bound))
    bound.validate_output(result)
    assert result.matched_row_id.value == 10


def test_builtin_reads_only_selected_source_not_nonempty_carry(matcher):
    admitted, target, batches = matcher
    params = SemanticJoinParams(
        source=ColumnRef("empty"),
        target=target,
        carry=[ColumnRef("carry")],
    )
    bound = admitted.bind_row(OpContext())
    result = asyncio.run(
        match_one(params, Row({"empty": None, "carry": "ACME"}), bound)
    )
    assert result.output.matched_row_id.value is None
    assert batches == []


def test_route_change_cannot_upgrade_local_admission():
    with pytest.raises(ValueError, match="backend changed"):
        AdmittedSemanticMatcher(
            target=SheetColumnRef(sheet_id=2, column="company"),
            values=((10, "Acme"),),
            backend=(None, "openai/text-embedding-3-small"),
            estimated_model="fastembed/test",
        )


def test_unissued_output_cannot_create_a_link(matcher):
    admitted, _, _ = matcher
    with pytest.raises(ValueError, match="admitted match result"):
        admitted.bind_row(OpContext()).validate_output(
            SemanticJoinMatch(
                match_value=Outcome.ok("invented"),
                match_score=Outcome.ok(1.0),
                matched_row_id=Outcome.ok(10),
            )
        )
