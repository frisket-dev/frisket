"""Unit contract for the standard receipt envelope (`frisket.sdk.envelope`).

Feeds synthetic `CapturedFacts` through `StandardReceipt` and pins the
audit-verified common core: the two-part inputs block with derived kind slugs,
mechanical provider_use, canonical evidence order (author cites, field_roles,
model_calls/prompt, run_counts), trailing op_id/run_id on every evidence ref,
and the model-status/errors derivation.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from frisket.contracts.action import (
    CostPolicy,
    IdempotencyPolicy,
    RetryPolicy,
)
from frisket.sdk import (
    StandardReceipt,
    deterministic_map,
    media_map,
    model_child_sheet,
    model_map,
)
from frisket.sdk.capture import CapturedFacts, OutputFact
from frisket.sdk.envelope import CiteContext
from frisket.sdk.inputs import Columns


class _SourceParams(BaseModel):
    input_columns: list[str]


class _SourceOutput(BaseModel):
    value: str


def _decl(archetype: Any = deterministic_map, **overrides: Any) -> Any:
    base: dict[str, Any] = dict(
        kind="map.demo",
        title="Demo",
        description="Demo op.",
        params_model=object,
        output_model=object,
        errors=[],
        side_effects=[],
        cost=CostPolicy(kind="none", requires_confirmation=False, notes=""),
        idempotency=IdempotencyPolicy(
            supported=True, scope="project", key_field="idempotency_key", behavior=""
        ),
        retry=RetryPolicy(supported=True, strategy="idempotency_replay", notes=""),
        examples=[],
        primary_fields=["sheet_id"],
    )
    base.update(overrides)
    return archetype(**base)


@pytest.mark.parametrize(
    ("factory", "extra"),
    (
        (model_map, {}),
        (media_map, {"map_error_code": "media_run_failed"}),
        (model_child_sheet, {"execution_mode": "cross_sheet"}),
    ),
)
def test_archetypes_forward_typed_inputs(factory: Any, extra: dict[str, Any]) -> None:
    descriptor = Columns("input_columns")
    declaration = _decl(
        factory,
        kind=f"test.typed_inputs.{factory.__name__}",
        params_model=_SourceParams,
        output_model=_SourceOutput,
        primary_fields=["input_columns"],
        inputs=descriptor,
        **extra,
    )

    assert declaration.inputs is descriptor


def _facts(**overrides: Any) -> CapturedFacts:
    base: dict[str, Any] = dict(
        run={"status": "completed", "cost_actual": 0.0},
        run_id=7,
        params_hash="sha256:params",
        op_id=3,
        sheet_id=1,
        total_rows=2,
        completed_rows=2,
        failed_rows=0,
        cost_actual=0.0,
        model="",
        prompt_hash=None,
        run_status="completed",
        row_ids=[10, 11],
        failed_row_ids=[],
        output_facts=[
            OutputFact(
                field={"name": "out", "schema": {"type": "string"}},
                name="out",
                column_id=42,
                type="text",
                value_hash="sha256:value",
            )
        ],
        missing_outputs=[],
        input_column_ids={"src": 41},
        input_column_types={"src": "text"},
        input_columns_rich=[],
        runner_spec={"recipe": "demo", "action_kind": "map.demo", "sheet_id": 1},
        model_calls=[],
        model_call_ids=[],
    )
    base.update(overrides)
    return CapturedFacts(**base)


def _base_refs(facts: CapturedFacts) -> list[dict[str, Any]]:
    return [
        {
            "kind": "map_demo_output_column",
            "name": fact.name,
            "type": fact.type,
            "sheet_id": facts.sheet_id,
            "column_id": fact.column_id,
            "run_id": facts.run_id,
            "op_id": facts.op_id,
            "row_ids": facts.row_ids,
            "value_hash": fact.value_hash,
        }
        for fact in facts.output_facts
    ]


def test_deterministic_envelope_core() -> None:
    decl = _decl()

    def cites(ctx: CiteContext, facts: CapturedFacts) -> None:
        ctx.cite("policy", rule="upper", params_hash=facts.params_hash)

    receipt = StandardReceipt(cites=cites)
    facts = _facts()
    prov = receipt.bind(decl)(None, facts, _base_refs(facts))

    assert [io.name for io in prov.inputs] == ["input_rows", "input_column.src"]
    assert prov.inputs[0].ref == {
        "kind": "map_demo_input_rows",
        "sheet_id": 1,
        "row_ids": [10, 11],
        "op_id": 3,
        "run_id": 7,
    }
    assert prov.inputs[1].ref == {
        "kind": "map_demo_input_column",
        "sheet_id": 1,
        "column_id": 41,
        "name": "src",
    }
    assert prov.provider_use == [
        {
            "provider": "local",
            "service": "frisket.demo",
            "external_api": False,
            "cost_actual": 0.0,
        }
    ]
    kinds = [item.ref["kind"] for item in prov.evidence]
    assert kinds == ["map_demo_policy", "map_demo_run_counts"]
    assert prov.evidence[0].ref == {
        "kind": "map_demo_policy",
        "rule": "upper",
        "params_hash": "sha256:params",
        "op_id": 3,
        "run_id": 7,
    }
    assert prov.evidence[1].ref == {
        "kind": "map_demo_run_counts",
        "total_rows": 2,
        "completed_rows": 2,
        "failed_rows": 0,
        "failed_row_ids": [],
        "result_count": 2,
        "model_call_count": 0,
        "cost_actual": 0.0,
        "op_id": 3,
        "run_id": 7,
    }
    assert prov.status == "completed"
    assert prov.errors == []


def test_deterministic_partial_status() -> None:
    prov = StandardReceipt().bind(_decl())(
        None, (facts := _facts(failed_rows=1)), _base_refs(facts)
    )
    assert prov.status == "partial"


def test_model_envelope_cites_and_errors(monkeypatch: Any) -> None:
    decl = _decl(model_map, form="demo")
    receipt = StandardReceipt(
        field_roles=lambda name, facts: "demo_role",
        prompt_extra=lambda facts: {"preset": facts.runner_spec.get("preset")},
    )
    facts = _facts(
        run_status="failed",
        total_rows=2,
        completed_rows=0,
        failed_rows=2,
        failed_row_ids=[10, 11],
        model="anthropic/claude-haiku-4-5",
        prompt_hash="sha256:prompt",
        model_calls=[],
        model_call_ids=[5, 6],
        runner_spec={
            "recipe": "demo",
            "action_kind": "map.demo",
            "sheet_id": 1,
            "preset": "one_line",
        },
    )
    from frisket.engine.executor import action_support as action_runtime_support

    monkeypatch.setattr(
        action_runtime_support,
        "_model_call_provider_use",
        lambda calls, *, model, run: [{"provider": "router", "model": model}],
    )
    prov = receipt.bind(decl)(None, facts, _base_refs(facts))

    assert prov.provider_use == [
        {"provider": "router", "model": "anthropic/claude-haiku-4-5"}
    ]
    kinds = [item.ref["kind"] for item in prov.evidence]
    assert kinds == [
        "map_demo_field_roles",
        "map_demo_model_calls",
        "map_demo_prompt",
        "map_demo_run_counts",
    ]
    roles = prov.evidence[0].ref
    assert roles["fields"] == [
        {"name": "out", "role": "demo_role", "column_id": 42, "type": "text"}
    ]
    assert prov.output_refs[0]["role"] == "demo_role"
    assert prov.output_refs[0]["schema"] == {"type": "string"}
    model_calls = prov.evidence[1]
    assert model_calls.retention == "pinned"
    assert model_calls.ref["model_call_ids"] == [5, 6]
    prompt = prov.evidence[2].ref
    assert list(prompt.keys()) == [
        "kind",
        "prompt_hash",
        "model",
        "preset",
        "op_id",
        "run_id",
    ]
    assert prov.status == "failed"
    assert [e.code for e in prov.errors] == ["model_run_failed"]
    assert prov.errors[0].message == "map.demo failed for every target row"


def test_model_envelope_does_not_report_unknown_provider_cost_as_free() -> None:
    facts = _facts(
        model="anthropic/claude-haiku-4-5",
        model_calls=[
            {
                "provider": "anthropic",
                "engine": "anthropic/claude-haiku-4-5",
                "credential_source": "platform_key",
                "provider_cost_usd": None,
                "units": {"tokens_in": 17, "tokens_out": 5},
            }
        ],
        model_call_ids=["call-unknown-cost"],
    )

    prov = StandardReceipt().bind(_decl(model_map, form="demo"))(
        None, facts, _base_refs(facts)
    )

    assert prov.provider_use == [
        {
            "provider": "anthropic",
            "model": "anthropic/claude-haiku-4-5",
            "model_call_count": 1,
            "cost_actual": None,
            "tokens_in": 17,
            "tokens_out": 5,
        }
    ]
    evidence_by_kind = {item.ref["kind"]: item.ref for item in prov.evidence}
    assert evidence_by_kind["map_demo_model_calls"]["cost_actual"] is None
    assert evidence_by_kind["map_demo_run_counts"]["cost_actual"] is None


def test_rich_input_refs_and_extras() -> None:
    decl = _decl(model_map, rich_input_columns=True)
    receipt = StandardReceipt(
        input_ref_extra=lambda name, facts: {"role": "source"},
        run_counts=False,
    )
    facts = _facts(
        model="anthropic/claude-haiku-4-5",
        input_columns_rich=[
            {
                "name": "src",
                "column_id": 41,
                "type": "text",
                "ai_generated": False,
                "source_run_id": None,
                "source_receipt_id": None,
            }
        ],
    )
    prov = receipt.bind(decl)(None, facts, _base_refs(facts))
    ref = prov.inputs[1].ref
    assert ref["type"] == "text"
    assert ref["ai_generated"] is False
    assert ref["role"] == "source"
    kinds = [item.ref["kind"] for item in prov.evidence]
    assert kinds == ["map_demo_model_calls", "map_demo_prompt"]
