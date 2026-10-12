from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest

from frisket.ai.llm import ModelRouter
from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig
from frisket.ai.llm.types import LLMResponse
from frisket.authoring.project_ask import default_project_ask_model
from frisket.cloud_connection import CloudConnectionStore
from frisket.cloud_execution import (
    CloudExecutionRefused,
    CloudPayerSubject,
    CloudQuoteAuthority,
)
from frisket.cloud_llm_checkpoint import CloudLLMCheckpointCoordinator
from frisket.contracts.http.cloud_connection import (
    CloudAccount,
    CloudCapabilitiesResponse,
    CloudCapabilityRoute,
    CloudExecutionReceipt,
    CloudExecutionResponse,
    CloudLLMOptionSupport,
    CloudLLMResult,
    CloudLLMToolCall,
    CloudLLMUsage,
    CloudSessionResponse,
    CloudTokenPrice,
    CloudUser,
)
from frisket.contracts.http.project_qa import AskThreadCreate, AskTurnRequest
from frisket.engine.store.effect_checkpoints import EffectCheckpointStore
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.engine.runner.validation import NetworkDisabled
from frisket.execution.pricing_policy import usd_to_micros
from frisket.execution.resolver import Refusal
from frisket.project_settings import ProjectSettings, write_project_settings
from frisket.server.services.project_qa import ProjectQAService
from frisket.server.cloud_project_qa_runtime import SoloCloudProjectQARuntimePort
from frisket.server.workspace import Workspace


MODEL = "anthropic/claude-sonnet-4-5"
FLASH_LITE = "gemini/gemini-3.5-flash-lite"


class SnapshotOnlyClient:
    origin = "https://cloud.example.test"

    def capabilities(self, _token, _account_id):
        raise AssertionError("Ask composition must use the stored capability snapshot")


def _account() -> CloudAccount:
    return CloudAccount(
        id=41,
        name="Personal",
        kind="personal",
        balance_micro=2_000_000,
        available_micro=2_000_000,
        manage_billing=True,
    )


def _capabilities(
    *, ready: bool = True, model: str = MODEL
) -> CloudCapabilitiesResponse:
    return CloudCapabilitiesResponse(
        account_id=41,
        routes=[
            CloudCapabilityRoute(
                capability="llm.complete",
                engine=model,
                provider=model.split("/", 1)[0],
                lane="frisket_credits",
                route_revision="route-1",
                ready=ready,
                blocker=None if ready else "Add credits to this account",
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
                    max_output_tokens=65_536,
                ),
            )
        ],
    )


def test_cloud_first_signed_out_ask_refuses_instead_of_using_local_key(tmp_path):
    class Client:
        def capabilities(self, _token, _account_id):
            raise AssertionError("signed-out Ask must not discover routes")

    workspace = Workspace(
        tmp_path / "workspace",
        router=ModelRouter(
            keys={"anthropic": "must-not-be-used"},
            use_env_keys=False,
            cache_mode="off",
        ),
        cloud_connection_store=CloudConnectionStore(tmp_path / "profile"),
        cloud_connection_client=Client(),
    )
    project_id = workspace.create("Ask")["id"]
    project = workspace.get(project_id)
    write_project_settings(
        project,
        ProjectSettings(
            cloud_user_id=7,
            cloud_account_id=41,
            cloud_execution_preference="cloud_first",
        ),
    )
    qa = ProjectQAStore(project)
    thread = qa.create_thread(title="Cloud payer")
    turn = qa.submit_turn(
        thread["id"],
        request_id="signed-out-cloud-first",
        question="Use the selected payer",
        model=MODEL,
    )

    with pytest.raises(CloudExecutionRefused) as exc_info:
        SoloCloudProjectQARuntimePort(workspace)._prepare_turn(project, turn["id"])

    assert exc_info.value.code == "cloud_route_unavailable"


def test_ask_does_not_bypass_a_selected_own_route_refusal(tmp_path, monkeypatch):
    workspace = Workspace(
        tmp_path / "workspace",
        router=ModelRouter(
            keys={"anthropic": "configured-but-unavailable"},
            use_env_keys=False,
            cache_mode="off",
        ),
    )
    project_id = workspace.create("Ask")["id"]
    project = workspace.get(project_id)
    qa = ProjectQAStore(project)
    thread = qa.create_thread(title="Own route")
    turn = qa.submit_turn(
        thread["id"],
        request_id="own-route-refusal",
        question="Do not bypass the selected route",
        model=MODEL,
    )
    monkeypatch.setattr(
        "frisket.server.cloud_project_qa_runtime.resolve",
        lambda *_args, **_kwargs: Refusal(
            family="no_live_target",
            remedy="The selected provider key is unavailable.",
            engine=MODEL,
            target_id="remote-api:anthropic",
        ),
    )

    try:
        with pytest.raises(CloudExecutionRefused) as exc_info:
            SoloCloudProjectQARuntimePort(workspace)._prepare_turn(project, turn["id"])
    finally:
        project.close()

    assert exc_info.value.code == "execution_route_unavailable"


def test_project_ask_default_uses_the_only_cloud_catalog_model(tmp_path):
    store = CloudConnectionStore(tmp_path / "profile")
    store.save_connection(
        "installation-token",
        CloudSessionResponse(
            user=CloudUser(id=7, email="user@example.test", display_name="User"),
            expires_at="2099-01-01T00:00:00Z",
            accounts=[_account()],
        ),
    )
    store.save_capabilities(_capabilities(model=FLASH_LITE), expected_user_id=7)
    workspace = Workspace(
        tmp_path / "workspace",
        router=ModelRouter(use_env_keys=False, cache_mode="off"),
        cloud_connection_store=store,
        cloud_connection_client=SnapshotOnlyClient(),
    )
    project_id = workspace.create("Ask")["id"]
    project = workspace.get(project_id)
    write_project_settings(
        project,
        ProjectSettings(
            cloud_user_id=7,
            cloud_account_id=41,
            cloud_execution_preference="cloud_first",
        ),
    )
    router = workspace.router_for(project)
    composition = workspace.execution_composition_for(
        project,
        router,
        workspace.edition_execution_composition_context_for(),
    )

    try:
        assert default_project_ask_model(router, composition) == FLASH_LITE
    finally:
        project.close()


def test_ask_blocks_selected_cloud_and_own_key_routes_when_network_is_off(
    tmp_path,
):
    store = CloudConnectionStore(tmp_path / "profile")
    store.save_connection(
        "installation-token",
        CloudSessionResponse(
            user=CloudUser(id=7, email="user@example.test", display_name="User"),
            expires_at="2099-01-01T00:00:00Z",
            accounts=[_account()],
        ),
    )
    store.save_capabilities(_capabilities(), expected_user_id=7)
    cloud_workspace = Workspace(
        tmp_path / "cloud-workspace",
        router=ModelRouter(use_env_keys=False, cache_mode="off"),
        cloud_connection_store=store,
        cloud_connection_client=SnapshotOnlyClient(),
    )
    cloud_project = cloud_workspace.get(cloud_workspace.create("Cloud Ask")["id"])
    write_project_settings(
        cloud_project,
        ProjectSettings(
            cloud_user_id=7,
            cloud_account_id=41,
            cloud_execution_preference="cloud_first",
        ),
    )
    cloud_project.set_network_policy(mode="off")
    cloud_qa = ProjectQAStore(cloud_project)
    cloud_thread = cloud_qa.create_thread(title="Cloud route")
    cloud_turn = cloud_qa.submit_turn(
        cloud_thread["id"],
        request_id="cloud-network-off",
        question="Do not send this prompt",
        model=MODEL,
    )

    own_workspace = Workspace(
        tmp_path / "own-workspace",
        router=ModelRouter(
            keys={"anthropic": "configured"},
            use_env_keys=False,
            cache_mode="off",
        ),
    )
    own_project = own_workspace.get(own_workspace.create("Own Ask")["id"])
    own_project.set_network_policy(mode="off")
    own_qa = ProjectQAStore(own_project)
    own_thread = own_qa.create_thread(title="Own route")
    own_turn = own_qa.submit_turn(
        own_thread["id"],
        request_id="own-network-off",
        question="Do not send this prompt",
        model=MODEL,
    )

    try:
        with pytest.raises(NetworkDisabled, match="execution_route:frisket_shared"):
            SoloCloudProjectQARuntimePort(cloud_workspace)._prepare_turn(
                cloud_project, cloud_turn["id"]
            )
        with pytest.raises(NetworkDisabled, match="execution_route:third_party_api"):
            SoloCloudProjectQARuntimePort(own_workspace)._prepare_turn(
                own_project, own_turn["id"]
            )
    finally:
        cloud_project.close()
        own_project.close()


def test_ask_allows_a_configured_local_endpoint_when_network_is_off(tmp_path):
    endpoint = LocalModelEndpointConfig(
        endpoint_id="test-local",
        display_name="Local test server",
        origin="http://127.0.0.1:11434",
        source="local_file",
    )
    workspace = Workspace(
        tmp_path / "workspace",
        router=ModelRouter(
            use_env_keys=False,
            cache_mode="off",
            local_endpoints=(endpoint,),
        ),
    )
    project = workspace.get(workspace.create("Local Ask")["id"])
    project.set_network_policy(mode="off")
    qa = ProjectQAStore(project)
    thread = qa.create_thread(title="Local route")
    turn = qa.submit_turn(
        thread["id"],
        request_id="local-network-off",
        question="Keep this prompt local",
        model="ollama/@test-local/qwen2.5",
    )

    try:
        runtime = SoloCloudProjectQARuntimePort(workspace)._prepare_turn(
            project, turn["id"]
        )
    finally:
        project.close()

    assert runtime.cloud_llm_coordinator is None


class CloudTransport:
    def __init__(self, on_execute=None) -> None:
        self.calls = []
        self.on_execute = on_execute

    async def execute_llm(self, attempt, request):
        self.calls.append((attempt, request))
        if self.on_execute is not None:
            self.on_execute(len(self.calls))
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


@pytest.mark.parametrize("disable_network_after_first_call", [False, True])
def test_solo_workspace_routes_ask_through_selected_cloud_account(
    tmp_path, disable_network_after_first_call
):
    async def scenario() -> None:
        router = ModelRouter(use_env_keys=False, cache_mode="off", max_retries=0)
        transport = CloudTransport()
        store = CloudConnectionStore(tmp_path / "profile")
        store.save_connection(
            "installation-token",
            CloudSessionResponse(
                user=CloudUser(id=7, email="user@example.test", display_name="User"),
                expires_at="2099-01-01T00:00:00Z",
                accounts=[_account()],
            ),
        )
        store.save_capabilities(_capabilities(), expected_user_id=7)

        class Client:
            origin = "https://cloud.example.test"

            def capabilities(self, token, account_id):
                assert token == "installation-token"
                assert account_id == 41
                return _capabilities()

        workspace = Workspace(
            tmp_path / "ws",
            router=router,
            cloud_connection_store=store,
            cloud_connection_client=Client(),
            cloud_execution_transport=transport,
        )
        project_id = workspace.create("Ask")["id"]
        project = workspace.get(project_id)
        write_project_settings(
            project,
            ProjectSettings(
                cloud_user_id=7,
                cloud_account_id=41,
                cloud_execution_preference="cloud_first",
            ),
        )
        transport.on_execute = lambda call_count: (
            project.set_network_policy(mode="off")
            if disable_network_after_first_call and call_count == 1
            else None
        )
        service = ProjectQAService(workspace)
        thread = await service.create(
            project_id, AskThreadCreate(scope={"kind": "project"})
        )
        turn = await service.submit(
            project_id,
            thread["id"],
            AskTurnRequest(
                request_id="solo-cloud",
                question="Inspect the project, then answer.",
                scope={"kind": "project"},
                model=MODEL,
            ),
        )
        run_task = service._tasks[(project, turn["id"])]  # noqa: SLF001

        for _ in range(100):
            saved = ProjectQAStore(project).get_turn(turn["id"])
            if saved["status"] != "running":
                break
            await asyncio.sleep(0.01)
        # ``finalize_turn`` publishes the terminal status before the same task
        # retires its receipt-backed Cloud checkpoints. Join that task so this
        # assertion observes the completed service lifecycle, not event-loop
        # scheduling between those two ordered writes.
        await asyncio.wait_for(asyncio.shield(run_task), 5)
        if disable_network_after_first_call:
            assert saved["status"] == "failed"
            assert len(transport.calls) == 1
        else:
            assert saved["status"] == "completed", saved["error_summary"]
            assert len(transport.calls) == 2
        allowance = usd_to_micros(workspace.cost_preapproval_usd)
        assert transport.calls[0][0].quote_authority.maximum_charge_micro == allowance
        if not disable_network_after_first_call:
            assert (
                transport.calls[1][0].quote_authority.maximum_charge_micro
                == allowance - 25
            )
        assert transport.calls[0][0].payer.account_id == 41
        assert transport.calls[0][0].payer.user_id == 7
        if not disable_network_after_first_call:
            assert transport.calls[0][0].request_id != transport.calls[1][0].request_id
        assert (
            EffectCheckpointStore(project.db).list_group(
                family="model_call", group_key=f"ask:{turn['id']}"
            )
            == []
        )

        await service.shutdown()
        await router.aclose()

    asyncio.run(scenario())


def test_own_key_ask_rechecks_network_policy_before_each_model_call(tmp_path):
    async def scenario() -> None:
        class Adapter:
            calls = 0

            async def complete(self, request, _client):
                self.calls += 1
                project.set_network_policy(mode="off")
                return LLMResponse(
                    content=None,
                    data=None,
                    model=request.model,
                    tokens_in=1,
                    tokens_out=1,
                    cost=0.0,
                    tool_calls=[
                        {
                            "name": "inspect_sheets",
                            "args": {},
                            "id": "inspect",
                        }
                    ],
                )

        adapter = Adapter()
        router = ModelRouter(
            keys={"anthropic": "configured"},
            use_env_keys=False,
            cache_mode="off",
            max_retries=0,
        )
        router._adapters["anthropic"] = adapter
        workspace = Workspace(tmp_path / "workspace", router=router)
        project_id = workspace.create("Ask")["id"]
        project = workspace.get(project_id)
        service = ProjectQAService(workspace)
        thread = await service.create(
            project_id, AskThreadCreate(scope={"kind": "project"})
        )
        turn = await service.submit(
            project_id,
            thread["id"],
            AskTurnRequest(
                request_id="own-key-policy-flip",
                question="Inspect the project, then answer.",
                scope={"kind": "project"},
                model=MODEL,
            ),
        )
        await asyncio.wait_for(
            asyncio.shield(service._tasks[(project, turn["id"])]),  # noqa: SLF001
            5,
        )

        saved = ProjectQAStore(project).get_turn(turn["id"])
        assert saved["status"] == "failed"
        assert adapter.calls == 1

        await service.shutdown()
        await router.aclose()

    asyncio.run(scenario())
