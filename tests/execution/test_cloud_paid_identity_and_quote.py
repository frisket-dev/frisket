from __future__ import annotations

from types import SimpleNamespace

from frisket.cloud_execution import (
    CloudPayerSubject,
    cloud_llm_estimated_spend_micro,
    cloud_quote_authority_for_row,
)
from frisket.contracts.http.cloud_connection import CloudLLMRequest, CloudTokenPrice
from frisket.engine.runner.map_runner import (
    _cloud_row_request_id,
    _row_effect_identity,
)
from frisket.engine.store import Project
from frisket.execution.resolve_for_action import _llm_cost_basis


def _payer() -> CloudPayerSubject:
    return CloudPayerSubject(
        user_id=7,
        account_id=41,
        lane="frisket_credits",
        provider="anthropic",
        engine="anthropic/claude-sonnet-4-5",
        route_revision="route-1",
    )


def _identity(project: Project, *, run_id: int) -> tuple[str, str]:
    return _row_effect_identity(
        project_storage_identity=project.storage_identity,
        run_id=run_id,
        row_id=1,
        recipe=SimpleNamespace(version="1"),
        spec={"action_kind": "media.ocr", "source": "page"},
        values={"page": {"blob": "sha256:source"}},
        out_cols={"text": 2},
        cloud_payer=_payer().to_snapshot(),
    )


def _new_cloud_row_request(
    project: Project, *, checkpoint_id: str, identity: str, run_id: int
) -> str:
    owner, request_id, adopted = _cloud_row_request_id(
        project,
        current_checkpoint_id=checkpoint_id,
        current_group_key=str(run_id),
        current_unit_key="1",
        action_kind="media.ocr",
        identity=identity,
        current_authorized_attempt_id="not-used-without-recovery",
        run_id=run_id,
        writer_attempt_id="not-used-without-recovery",
        claim_token=None,
        claimless_direct_effect=True,
    )
    assert owner == checkpoint_id
    assert adopted is False
    return request_id


def test_cloud_row_request_is_project_unique_and_new_intent_uses_new_key(tmp_path):
    first = Project.create(tmp_path / "first.frisket")
    second = Project.create(tmp_path / "second.frisket")
    try:
        first_checkpoint, first_identity = _identity(first, run_id=1)
        second_checkpoint, second_identity = _identity(second, run_id=1)

        first_request = _new_cloud_row_request(
            first,
            checkpoint_id=first_checkpoint,
            identity=first_identity,
            run_id=1,
        )
        second_request = _new_cloud_row_request(
            second,
            checkpoint_id=second_checkpoint,
            identity=second_identity,
            run_id=1,
        )
        assert first_request != second_request

        fresh_checkpoint, fresh_identity = _identity(first, run_id=2)
        assert fresh_identity == first_identity
        later_request = _new_cloud_row_request(
            first,
            checkpoint_id=fresh_checkpoint,
            identity=fresh_identity,
            run_id=2,
        )
        assert later_request != first_request
    finally:
        first.close()
        second.close()


def test_action_llm_quote_uses_the_cloud_wire_payload_formula():
    price = CloudTokenPrice(
        kind="tokens",
        input_micro_per_million_tokens=100_000_000,
        output_micro_per_million_tokens=100_000_000,
    )
    call = SimpleNamespace(
        messages=[{"role": "user", "content": "héllo"}],
        schema=None,
        max_tokens=200,
    )

    class Provider:
        @staticmethod
        def cloud_token_price(_target_id, _capability, _engine):
            return price

    resolution = SimpleNamespace(
        target=SimpleNamespace(id="frisket-cloud:41"),
        facts=SimpleNamespace(engine=_payer().engine, cloud_payer=_payer()),
    )
    basis, _total = _llm_cost_basis(
        SimpleNamespace(provider=Provider()),
        resolution,
        {1: call},
        recipe=SimpleNamespace(),
    )
    authority = cloud_quote_authority_for_row(basis, row_id=1)
    expected = cloud_llm_estimated_spend_micro(
        CloudLLMRequest(
            model=_payer().engine,
            messages=call.messages,
            schema=call.schema,
            max_tokens=call.max_tokens,
            temperature=0,
        ),
        price,
    )

    assert authority.maximum_charge_micro == expected


def test_structured_and_agent_llm_calls_use_explicit_unknown_quantity():
    price = CloudTokenPrice(
        kind="tokens",
        input_micro_per_million_tokens=100_000_000,
        output_micro_per_million_tokens=100_000_000,
    )

    class Provider:
        @staticmethod
        def cloud_token_price(_target_id, _capability, _engine):
            return price

    resolution = SimpleNamespace(
        target=SimpleNamespace(id="frisket-cloud:41"),
        facts=SimpleNamespace(engine=_payer().engine, cloud_payer=_payer()),
    )
    plain = SimpleNamespace(
        messages=[{"role": "user", "content": "extract"}],
        schema=None,
        max_tokens=200,
    )
    structured = SimpleNamespace(
        messages=plain.messages,
        schema={"type": "object", "properties": {"answer": {"type": "string"}}},
        max_tokens=plain.max_tokens,
    )

    for recipe, call in (
        (SimpleNamespace(), structured),
        (SimpleNamespace(run_agent=lambda: None), plain),
    ):
        basis, quantity = _llm_cost_basis(
            SimpleNamespace(provider=Provider()),
            resolution,
            {1: call},
            recipe=recipe,
        )

        assert quantity is None
        assert basis.estimated_quantity is None
        assert basis.row_quote_quantities is None
        assert basis.pricing_key.endswith(
            ":anthropic:llm.complete:anthropic/claude-sonnet-4-5"
        )
        assert basis.terms_version == "route-1"
        assert basis.charge_authority == "frisket_cloud_receipt"
        assert (
            cloud_quote_authority_for_row(basis, row_id=1).maximum_charge_micro is None
        )


def test_llm_quote_helper_includes_tools_in_the_canonical_wire_payload():
    price = CloudTokenPrice(
        kind="tokens",
        input_micro_per_million_tokens=100_000_000,
        output_micro_per_million_tokens=100_000_000,
    )
    plain = CloudLLMRequest(
        model=_payer().engine,
        messages=[{"role": "user", "content": "find the source"}],
        max_tokens=200,
        temperature=0,
    )
    with_tools = plain.model_copy(
        update={
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "search",
                        "description": "Search project sources",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]
        }
    )

    assert cloud_llm_estimated_spend_micro(
        with_tools, price
    ) > cloud_llm_estimated_spend_micro(plain, price)
