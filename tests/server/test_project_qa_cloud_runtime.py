from __future__ import annotations

import asyncio
from contextlib import contextmanager

from frisket.ai.llm import ModelRouter
from frisket.cloud_execution import CloudPayerSubject, CloudQuoteAuthority
from frisket.cloud_llm_checkpoint import CloudLLMCheckpointCoordinator
from frisket.contracts.http.cloud_connection import (
    CloudCapabilityRoute,
    CloudExecutionReceipt,
    CloudExecutionResponse,
    CloudLLMOptionSupport,
    CloudLLMResult,
    CloudLLMToolCall,
    CloudLLMUsage,
    CloudTokenPrice,
)
from frisket.contracts.http.project_qa import AskThreadCreate, AskTurnRequest
from frisket.engine.store.effect_checkpoints import EffectCheckpointStore
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.server.services.project_qa import ProjectQAService
from frisket.server.workspace import Workspace


MODEL = "anthropic/claude-sonnet-4-5"


class CloudTransport:
    def __init__(self) -> None:
        self.calls = []

    async def execute_llm(self, attempt, request):
        self.calls.append((attempt, request))
        tool_calls = (
            [CloudLLMToolCall(name="inspect_sheets", id="inspect", args={})]
            if len(self.calls) == 1
            else [
                CloudLLMToolCall(
                    name="final_result",
                    id="answer",
                    args={
                        "text": "No project sources were needed.",
                        "citation_ids": [],
                    },
                )
            ]
        )
        return CloudExecutionResponse(
            execution_id=attempt.request_id,
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
                content=None,
                data=None,
                tokens_in=2,
                tokens_out=3,
                cost=0.000_025,
                model=MODEL,
                cost_source="cloud_receipt",
                cached=False,
                provider="anthropic",
                credential_source="platform_key",
                raw={},
                tool_calls=tool_calls,
                output_limited=False,
            ),
            receipt=CloudExecutionReceipt(
                user_id=7,
                account_id=41,
                lane="frisket_credits",
                provider="anthropic",
                engine=MODEL,
                charge_micro=25,
                provider_cost_micro=10,
                usage=CloudLLMUsage(kind="llm.complete", tokens_in=2, tokens_out=3),
            ),
        )


def test_ask_uses_cloud_coordinator_and_retires_after_usage_commit(tmp_path):
    async def scenario() -> None:
        router = ModelRouter(
            use_env_keys=False,
            cache_mode="off",
            max_retries=0,
        )
        transport = CloudTransport()
        settled = []
        scope_entries = []
        closed = asyncio.Event()

        class Runtime:
            def __init__(self, project, turn_id):
                self.cloud_llm_coordinator = CloudLLMCheckpointCoordinator(
                    project,
                    transport,
                    payer=CloudPayerSubject(
                        user_id=7,
                        account_id=41,
                        lane="frisket_credits",
                        provider="anthropic",
                        engine=MODEL,
                        route_revision="route-1",
                    ),
                    quote_authority=CloudQuoteAuthority(1_000),
                    group_key=f"ask:{turn_id}",
                    action_kind="project.ask",
                    claimless_direct_effect=True,
                )

            @contextmanager
            def call_scope(self, _router):
                scope_entries.append(True)
                yield

            def settle_call(self, **kwargs):
                settled.append(kwargs)

            async def aclose(self):
                closed.set()

        class Port:
            async def prepare_turn(self, *, project, turn_id, **_kwargs):
                return Runtime(project, turn_id)

        workspace = Workspace(
            tmp_path / "ws", router=router, project_qa_runtime_port=Port()
        )
        project_id = workspace.create("Ask")["id"]
        service = ProjectQAService(workspace)
        thread = await service.create(
            project_id, AskThreadCreate(scope={"kind": "project"})
        )
        turn = await service.submit(
            project_id,
            thread["id"],
            AskTurnRequest(
                request_id="one",
                question="Say hello",
                scope={"kind": "project"},
                model=MODEL,
            ),
        )
        await asyncio.wait_for(closed.wait(), 5)

        project = workspace.get(project_id)
        saved = ProjectQAStore(project).get_turn(turn["id"])
        assert saved["status"] == "completed", saved["error_summary"]
        assert len(transport.calls) == 2
        assert transport.calls[0][0].quote_authority.maximum_charge_micro == 1_000
        assert transport.calls[1][0].quote_authority.maximum_charge_micro == 975
        assert scope_entries == []
        assert settled == []
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM project_qa_usage_calls WHERE turn_id=?",
                (turn["id"],),
            ).fetchone()[0]
            == 2
        )
        assert (
            EffectCheckpointStore(project.db).list_group(
                family="model_call", group_key=f"ask:{turn['id']}"
            )
            == []
        )

        await service.shutdown()
        await router.aclose()

    asyncio.run(scenario())
