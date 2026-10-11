from __future__ import annotations

import asyncio

import pytest

from frisket.ai.llm import LLMRequest, ModelRouter
from frisket.cloud_execution import (
    CloudExecutionRefused,
    CloudLLMExecutionBinding,
    CloudPayerSubject,
    CloudQuoteAuthority,
    cloud_llm_execution_scope,
)
from frisket.cloud_llm_checkpoint import CloudLLMCheckpointCoordinator
from frisket.contracts.http.cloud_connection import (
    CloudCapabilityRoute,
    CloudExecutionReceipt,
    CloudExecutionResponse,
    CloudLLMOptionSupport,
    CloudLLMRequest,
    CloudLLMResult,
    CloudLLMUsage,
    CloudTokenPrice,
)
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore

MODEL = "anthropic/claude-sonnet-4-5"


def _payer(*, user_id: int = 7) -> CloudPayerSubject:
    return CloudPayerSubject(
        user_id=user_id,
        account_id=41,
        lane="frisket_credits",
        provider="anthropic",
        engine=MODEL,
        route_revision="route-1",
    )


def _request(*, message: str = "hello") -> CloudLLMRequest:
    return CloudLLMRequest(
        model=MODEL,
        messages=[{"role": "user", "content": message}],
        max_tokens=100,
        temperature=0,
    )


def _response(*, execution_id: str, charge_micro: int = 20) -> CloudExecutionResponse:
    return CloudExecutionResponse(
        execution_id=execution_id,
        status="completed",
        route=CloudCapabilityRoute(
            capability="llm.complete",
            engine=MODEL,
            provider="anthropic",
            lane="frisket_credits",
            route_revision="route-1",
            ready=True,
            blocker=None,
            price=CloudTokenPrice(
                kind="tokens",
                input_micro_per_million_tokens=1,
                output_micro_per_million_tokens=1,
            ),
            options=CloudLLMOptionSupport(
                kind="llm",
                structured_output=True,
                tools=True,
                vision=True,
                max_output_tokens=4096,
            ),
        ),
        result=CloudLLMResult(
            content="answer",
            data=None,
            tokens_in=2,
            tokens_out=1,
            cost=charge_micro / 1_000_000,
            model=MODEL,
            cost_source="cloud_receipt",
            cached=False,
            provider="anthropic",
            credential_source="platform_key",
            raw={},
            output_limited=False,
        ),
        receipt=CloudExecutionReceipt(
            user_id=7,
            account_id=41,
            lane="frisket_credits",
            provider="anthropic",
            engine=MODEL,
            charge_micro=charge_micro,
            provider_cost_micro=10,
            usage=CloudLLMUsage(kind="llm.complete", tokens_in=2, tokens_out=1),
        ),
    )


def _account_key_response(*, execution_id: str) -> CloudExecutionResponse:
    response = _response(execution_id=execution_id, charge_micro=0)
    assert response.receipt is not None
    return response.model_copy(
        update={
            "route": response.route.model_copy(update={"lane": "account_key"}),
            "receipt": response.receipt.model_copy(
                update={
                    "lane": "account_key",
                    "charge_micro": 0,
                    "provider_cost_micro": None,
                }
            ),
        }
    )


class _Transport:
    def __init__(
        self,
        responses: list[CloudExecutionResponse | BaseException],
    ) -> None:
        self.responses = list(responses)
        self.calls = []

    async def execute_llm(self, attempt, request):
        self.calls.append((attempt, request))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _coordinator(project, transport, *, payer=None, ceiling=100):
    return CloudLLMCheckpointCoordinator(
        project,
        transport,
        payer=payer or _payer(),
        quote_authority=CloudQuoteAuthority(ceiling),
        group_key="run:1:row:2",
        action_kind="map.extract",
    )


def _claimless_run(project, *, attempt_id: str, state: str = "dispatching"):
    sheet_id = project.db.execute("SELECT id FROM sheets LIMIT 1").fetchone()[0]
    op_id = project.append_op("map", {"recipe": "extract"})
    run_id = RunResultStore(project).start_run(op_id, sheet_id, "map.extract")
    project.db.execute(
        "INSERT INTO execution_attempts "
        "(id, run_id, seq, state, action_identity_hash, scope_json, created_at) "
        "VALUES (?, ?, 0, ?, 'cloud-llm-recovery', '[]', datetime('now'))",
        (attempt_id, run_id, state),
    )
    if state == "dispatching":
        project.db.execute(
            "UPDATE runs SET current_attempt_id=? WHERE id=?",
            (attempt_id, run_id),
        )
    project.db.commit()
    return run_id


def test_returned_request_replays_locally_and_changed_payer_refuses(tmp_path):
    async def scenario() -> None:
        project = Project.create(tmp_path / "project.frisket")
        first_transport = _Transport([_response(execution_id="exec-1")])
        first = _coordinator(project, first_transport)
        attempt, original = await first.execute(_request())

        replay_transport = _Transport([])
        replay = _coordinator(project, replay_transport)
        replay_attempt, recovered = await replay.execute(_request())

        assert replay_transport.calls == []
        assert replay_attempt.request_id == attempt.request_id
        assert recovered == original

        changed_user = _coordinator(project, replay_transport, payer=_payer(user_id=8))
        with pytest.raises(Exception, match="different request or payer"):
            await changed_user.execute(_request())
        assert replay_transport.calls == []
        project.close()

    asyncio.run(scenario())


def test_reserved_request_reposts_identical_id_and_body(tmp_path):
    async def scenario() -> None:
        project = Project.create(tmp_path / "project.frisket")
        uncertain = _Transport([RuntimeError("lost response")])
        with pytest.raises(RuntimeError, match="lost response"):
            await _coordinator(project, uncertain).execute(_request())

        recovered = _Transport([_response(execution_id="exec-1")])
        await _coordinator(project, recovered).execute(_request())

        [(first_attempt, first_request)] = uncertain.calls
        [(second_attempt, second_request)] = recovered.calls
        assert second_attempt.request_id == first_attempt.request_id
        assert second_request == first_request
        project.close()

    asyncio.run(scenario())


def test_reserved_request_is_adopted_by_fresh_run_writer(tmp_path):
    async def scenario() -> None:
        project = Project.create(tmp_path / "fresh-run.frisket")
        project.add_sheet("S")
        first_run = _claimless_run(project, attempt_id="attempt-first")
        uncertain = _Transport([RuntimeError("lost response")])
        first = CloudLLMCheckpointCoordinator(
            project,
            uncertain,
            payer=_payer(),
            quote_authority=CloudQuoteAuthority(100),
            group_key="map:stable-parent-key",
            action_kind="map.extract",
            run_id=first_run,
            writer_attempt_id="attempt-first",
            claimless_direct_effect=True,
        )
        with pytest.raises(RuntimeError, match="lost response"):
            await first.execute(_request())
        project.db.execute(
            "UPDATE execution_attempts SET state='halted' WHERE id='attempt-first'"
        )
        project.db.execute(
            "UPDATE runs SET current_attempt_id=NULL WHERE id=?", (first_run,)
        )
        project.db.commit()

        second_run = _claimless_run(project, attempt_id="attempt-second")
        recovered_transport = _Transport([_response(execution_id="exec-1")])
        recovered = CloudLLMCheckpointCoordinator(
            project,
            recovered_transport,
            payer=_payer(),
            quote_authority=CloudQuoteAuthority(100),
            group_key="map:stable-parent-key",
            action_kind="map.extract",
            run_id=second_run,
            writer_attempt_id="attempt-second",
            claimless_direct_effect=True,
        )
        await recovered.execute(_request())

        [(first_attempt, first_request)] = uncertain.calls
        [(second_attempt, second_request)] = recovered_transport.calls
        assert second_attempt.request_id == first_attempt.request_id
        assert second_request == first_request
        row = project.db.execute(
            "SELECT state, authorized_attempt_id FROM effect_checkpoints WHERE id=?",
            (second_attempt.request_id,),
        ).fetchone()
        assert tuple(row) == ("returned", "attempt-second")
        recovered.retire_returned()
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM effect_checkpoints WHERE family='model_call'"
            ).fetchone()[0]
            == 0
        )
        project.close()

    asyncio.run(scenario())


def test_changed_request_gets_new_child_and_only_remaining_allowance(tmp_path):
    async def scenario() -> None:
        project = Project.create(tmp_path / "project.frisket")
        transport = _Transport(
            [
                _response(execution_id="exec-1", charge_micro=20),
                _response(execution_id="exec-2", charge_micro=30),
            ]
        )
        coordinator = _coordinator(project, transport, ceiling=100)

        await coordinator.execute(_request(message="first"))
        await coordinator.execute(_request(message="second"))

        first_attempt, second_attempt = [call[0] for call in transport.calls]
        assert first_attempt.request_id != second_attempt.request_id
        assert first_attempt.quote_authority.maximum_charge_micro == 100
        assert second_attempt.quote_authority.maximum_charge_micro == 80
        assert len(coordinator.returned_checkpoints) == 2
        project.close()

    asyncio.run(scenario())


def test_explicit_unknown_quantity_covers_distinct_tool_and_repair_calls(tmp_path):
    async def scenario() -> None:
        project = Project.create(tmp_path / "project.frisket")
        transport = _Transport(
            [
                _response(execution_id="exec-1", charge_micro=20),
                _response(execution_id="exec-2", charge_micro=30),
            ]
        )
        coordinator = _coordinator(project, transport, ceiling=None)
        initial = _request(message="find a source").model_copy(
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
        repair = initial.model_copy(
            update={
                "messages": [
                    *initial.messages,
                    {"role": "assistant", "content": "invalid result"},
                    {
                        "role": "user",
                        "content": "Return a corrected result with the required field.",
                    },
                ]
            }
        )

        await coordinator.execute(initial)
        await coordinator.execute(repair)

        first_attempt, second_attempt = [call[0] for call in transport.calls]
        assert first_attempt.request_id != second_attempt.request_id
        assert first_attempt.quote_authority.maximum_charge_micro is None
        assert second_attempt.quote_authority.maximum_charge_micro is None
        assert len(coordinator.returned_checkpoints) == 2
        project.close()

    asyncio.run(scenario())


def test_account_key_unknown_receipt_cost_remains_fail_closed_for_later_call(tmp_path):
    async def scenario() -> None:
        project = Project.create(tmp_path / "project.frisket")
        payer = CloudPayerSubject(**{**_payer().to_snapshot(), "lane": "account_key"})
        transport = _Transport(
            [
                _account_key_response(execution_id="exec-1"),
                _account_key_response(execution_id="exec-2"),
            ]
        )
        coordinator = _coordinator(project, transport, payer=payer, ceiling=100)

        await coordinator.execute(_request(message="first"))
        with pytest.raises(
            CloudExecutionRefused,
            match="Cloud LLM checkpoint has invalid spend accounting",
        ):
            await coordinator.execute(_request(message="second"))

        [first_attempt] = [call[0] for call in transport.calls]
        assert first_attempt.quote_authority.maximum_charge_micro == 100
        project.close()

    asyncio.run(scenario())


def test_router_selects_cloud_only_inside_exact_bound_model_scope(tmp_path):
    async def scenario() -> None:
        project = Project.create(tmp_path / "project.frisket")
        transport = _Transport([_response(execution_id="exec-1")])
        coordinator = _coordinator(project, transport)
        router = ModelRouter(use_env_keys=False, cache_mode="off", max_retries=0)
        try:
            with cloud_llm_execution_scope(
                CloudLLMExecutionBinding(coordinator=coordinator)
            ):
                response = await router.complete_transport(
                    LLMRequest(
                        model=MODEL,
                        messages=[{"role": "user", "content": "hello"}],
                        max_tokens=100,
                    )
                )
        finally:
            await router.aclose()
            project.close()

        assert response.content == "answer"
        assert len(transport.calls) == 1

    asyncio.run(scenario())
