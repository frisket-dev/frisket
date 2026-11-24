"""Project Ask cloud runtime, routes, service, and research lifecycle, ownership, runtime, and session tests."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from contextlib import closing, contextmanager, nullcontext
from contextvars import ContextVar
from typing import Any

import pytest
from helpers import make_client
from pydantic_ai import ModelRetry
from pydantic_ai.exceptions import UnexpectedModelBehavior

from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig
from frisket.ai.llm.pricing import PRICES
from frisket.ai.llm.router import ModelRouter
from frisket.ai.llm.types import LLMActualAccounting, LLMRequest, LLMResponse
from frisket.authoring.project_ask import default_project_ask_model
from frisket.cloud_connection import CloudConnectionStore
from frisket.cloud_execution import (
    CloudEstimateAuthority,
    CloudExecutionRefused,
    CloudPayerSubject,
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
from frisket.contracts.http.project_qa import (
    AskResearchResume,
    AskThreadCreate,
    AskTurnRequest,
)
from frisket.engine.runner.validation import ServiceRefused
from frisket.engine.store import Project
from frisket.engine.store.citations import (
    record_citation,
    record_source_artifact,
    record_source_span,
)
from frisket.engine.store.effect_checkpoints import EffectCheckpointStore
from frisket.engine.store.project_qa import ProjectQAConflictError, ProjectQAStore
from frisket.engine.store.project_qa_research import (
    ProjectQAResearchConflictError,
    ProjectQAResearchNotFound,
    ProjectQAResearchStore,
)
from frisket.engine.store.transactions import write_transaction
from frisket.engine.store.writer_fence import WriterFence
from frisket.execution import pricing_policy
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY, PurchaseAdmission
from frisket.execution.resolver import Refusal
from frisket.project_settings import ProjectSettings, write_project_settings
from frisket.server.cloud_project_qa_runtime import SoloCloudProjectQARuntimePort
from frisket.server.services.project_qa import ProjectQAService
from frisket.server.services.project_qa_research import (
    ResearchSession,
    ResearchSkillsUnavailable,
)
from frisket.server.services.project_qa_runner import run_turn
from frisket.server.services.project_qa_tools import ProjectQATools
from frisket.server.services.skills import SkillLibrary
from frisket.server.thread_worker import await_thread_worker
from frisket.server.workspace import Workspace
from tests.execution_composition_helpers import RecordingPurchasePolicy

# --- cloud runtime ---

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
            model_call_policy=LOCAL_PURCHASE_POLICY,
            keys={"anthropic": "must-not-be-used"},
            use_env_keys=False,
            cache_mode="off",
        ),
        cloud_connection_store=CloudConnectionStore(tmp_path / "profile"),
        cloud_connection_client=Client(),
    )
    project_id = workspace.create("Ask")["id"]
    project = workspace.get(project_id)
    with project.transaction_scope():
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
            model_call_policy=LOCAL_PURCHASE_POLICY,
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

    with closing(project):
        with pytest.raises(CloudExecutionRefused) as exc_info:
            SoloCloudProjectQARuntimePort(workspace)._prepare_turn(project, turn["id"])

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
        router=ModelRouter(
            model_call_policy=LOCAL_PURCHASE_POLICY,
            use_env_keys=False,
            cache_mode="off",
        ),
        cloud_connection_store=store,
        cloud_connection_client=SnapshotOnlyClient(),
    )
    project_id = workspace.create("Ask")["id"]
    project = workspace.get(project_id)
    with project.transaction_scope():
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

    with closing(project):
        assert default_project_ask_model(router, composition) == FLASH_LITE


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
        router=ModelRouter(
            model_call_policy=LOCAL_PURCHASE_POLICY,
            use_env_keys=False,
            cache_mode="off",
        ),
        cloud_connection_store=store,
        cloud_connection_client=SnapshotOnlyClient(),
    )
    cloud_project = cloud_workspace.get(cloud_workspace.create("Cloud Ask")["id"])
    with cloud_project.transaction_scope():
        write_project_settings(
            cloud_project,
            ProjectSettings(
                cloud_user_id=7,
                cloud_account_id=41,
                cloud_execution_preference="cloud_first",
            ),
        )
    cloud_project.set_allowed_services(
        "approved_services", confirm_allowed_services=None
    )
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
            model_call_policy=LOCAL_PURCHASE_POLICY,
            keys={"anthropic": "configured"},
            use_env_keys=False,
            cache_mode="off",
        ),
    )
    own_project = own_workspace.get(own_workspace.create("Own Ask")["id"])
    own_project.set_allowed_services("approved_services", confirm_allowed_services=None)
    own_qa = ProjectQAStore(own_project)
    own_thread = own_qa.create_thread(title="Own route")
    own_turn = own_qa.submit_turn(
        own_thread["id"],
        request_id="own-network-off",
        question="Do not send this prompt",
        model=MODEL,
    )

    with closing(cloud_project), closing(own_project):
        with pytest.raises(ServiceRefused, match="Frisket Cloud"):
            SoloCloudProjectQARuntimePort(cloud_workspace)._prepare_turn(
                cloud_project, cloud_turn["id"]
            )
        with pytest.raises(ServiceRefused, match="Allowed services setting"):
            SoloCloudProjectQARuntimePort(own_workspace)._prepare_turn(
                own_project, own_turn["id"]
            )


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
            model_call_policy=LOCAL_PURCHASE_POLICY,
            use_env_keys=False,
            cache_mode="off",
            local_endpoints=(endpoint,),
        ),
        cloud_connection_store=CloudConnectionStore(tmp_path / "profile"),
        cloud_connection_client=SnapshotOnlyClient(),
    )
    project = workspace.get(workspace.create("Local Ask")["id"])
    model = "ollama/@test-local/qwen2.5"
    with project.transaction_scope():
        write_project_settings(
            project,
            ProjectSettings(model_execution_routes={model: "own"}),
        )
    project.set_allowed_services("approved_services", confirm_allowed_services=None)
    qa = ProjectQAStore(project)
    thread = qa.create_thread(title="Local route")
    turn = qa.submit_turn(
        thread["id"],
        request_id="local-network-off",
        question="Keep this prompt local",
        model=model,
    )

    with closing(project):
        runtime = SoloCloudProjectQARuntimePort(workspace)._prepare_turn(
            project, turn["id"]
        )

    assert runtime.cloud_llm_coordinator is None


def test_ask_does_not_use_local_endpoint_for_saved_cloud_route(tmp_path):
    endpoint = LocalModelEndpointConfig(
        endpoint_id="test-local",
        display_name="Local test server",
        origin="http://127.0.0.1:11434",
        source="local_file",
    )
    workspace = Workspace(
        tmp_path / "workspace",
        router=ModelRouter(
            model_call_policy=LOCAL_PURCHASE_POLICY,
            use_env_keys=False,
            cache_mode="off",
            local_endpoints=(endpoint,),
        ),
        cloud_connection_store=CloudConnectionStore(tmp_path / "profile"),
        cloud_connection_client=SnapshotOnlyClient(),
    )
    project = workspace.get(workspace.create("Local Ask")["id"])
    model = "ollama/@test-local/qwen2.5"
    with project.transaction_scope():
        write_project_settings(
            project,
            ProjectSettings(model_execution_routes={model: "cloud"}),
        )
    qa = ProjectQAStore(project)
    thread = qa.create_thread(title="Selected Cloud route")
    turn = qa.submit_turn(
        thread["id"],
        request_id="cloud-not-local",
        question="Do not run this through the local endpoint",
        model=model,
    )

    with closing(project):
        with pytest.raises(CloudExecutionRefused) as exc_info:
            SoloCloudProjectQARuntimePort(workspace)._prepare_turn(project, turn["id"])

    assert exc_info.value.code == "cloud_route_unavailable"


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
            model_call_policy=LOCAL_PURCHASE_POLICY,
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
                    estimate_authority=CloudEstimateAuthority(1_000),
                    group_key=f"ask:{turn_id}",
                    action_kind="project.ask",
                    fence=WriterFence(claimless_direct_effect=True),
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
        assert transport.calls[0][0].estimate_authority.maximum_charge_micro == 1_000
        assert transport.calls[1][0].estimate_authority.maximum_charge_micro == 975
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
        router = ModelRouter(
            model_call_policy=LOCAL_PURCHASE_POLICY,
            use_env_keys=False,
            cache_mode="off",
            max_retries=0,
        )
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
        with project.transaction_scope():
            write_project_settings(
                project,
                ProjectSettings(
                    cloud_user_id=7,
                    cloud_account_id=41,
                    cloud_execution_preference="cloud_first",
                ),
            )
        transport.on_execute = lambda call_count: (
            project.set_allowed_services(
                "approved_services", confirm_allowed_services=None
            )
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
        run_task = service._tasks[(project, turn["id"])]

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
        assert transport.calls[0][0].estimate_authority.maximum_charge_micro is None
        if not disable_network_after_first_call:
            assert transport.calls[1][0].estimate_authority.maximum_charge_micro is None
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
                project.set_allowed_services(
                    "approved_services", confirm_allowed_services=None
                )
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
            model_call_policy=LOCAL_PURCHASE_POLICY,
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
            asyncio.shield(service._tasks[(project, turn["id"])]),
            5,
        )

        saved = ProjectQAStore(project).get_turn(turn["id"])
        assert saved["status"] == "failed"
        assert adapter.calls == 1

        await service.shutdown()
        await router.aclose()

    asyncio.run(scenario())


# --- routes ---


def test_shared_history_revision_and_project_boundary(client):
    with client:
        pid = client.post("/api/projects", json={"name": "Ask"}).json()["id"]
        other = client.post("/api/projects", json={"name": "Other"}).json()["id"]
        path = f"/api/projects/{pid}/qa/threads"
        response = client.post(path, json={"scope": {"kind": "project"}})
        assert response.status_code == 200, response.text
        thread = response.json()
        detail = f"{path}/{thread['id']}"
        assert client.get(detail).json()["thread"] == thread
        assert (
            client.get(f"/api/projects/{other}/qa/threads/{thread['id']}").status_code
            == 404
        )
        assert (
            client.patch(
                detail, json={"expected_revision": 1, "title": "New title"}
            ).status_code
            == 200
        )
        assert (
            client.patch(
                detail, json={"expected_revision": 1, "title": "Stale"}
            ).status_code
            == 409
        )
        assert (
            client.get(detail + "/events", params={"after": 1, "before": 2}).status_code
            == 422
        )
        assert (
            client.get(detail + "/report").json()["markdown"].startswith("# New title")
        )
        assert client.get(detail + "/events?typo=1").status_code == 422
        assert client.delete(detail).status_code == 204
        assert client.get(path).json() == []


def test_missing_scope_is_rejected_before_admission(client):
    with client:
        pid = client.post("/api/projects", json={"name": "Ask"}).json()["id"]
        path = f"/api/projects/{pid}/qa/threads"
        missing = {"kind": "sources", "sources": [{"kind": "sheet", "sheet_id": 999}]}
        assert client.post(path, json={"scope": missing}).status_code == 422
        thread = client.post(path, json={"scope": {"kind": "project"}}).json()
        detail = f"{path}/{thread['id']}"
        assert (
            client.patch(
                detail, json={"expected_revision": 1, "scope": missing}
            ).status_code
            == 422
        )
        assert (
            client.post(
                detail + "/turns",
                json={"request_id": "bad", "question": "Read", "scope": missing},
            ).status_code
            == 422
        )
        result = client.get(detail).json()
        assert result["active_turn"] is None
        assert result["history"]["events"] == []


def test_citation_resolves_saved_value_and_refuses_another_thread(client):

    with client:
        pid = client.post("/api/projects", json={"name": "Ask"}).json()["id"]
        project = client.app.state.workspace.get(pid)
        sheet = project.add_sheet("Evidence")
        column = project.add_column(sheet, "Name")
        [row] = project.add_rows(sheet, [{"Name": "Ada"}], {"Name": column})
        store = ProjectQAStore(project)
        thread = store.create_thread(title="Question")
        other = store.create_thread(title="Other")
        turn = store.submit_turn(thread["id"], request_id="once", question="Who?")
        read = ProjectQATools(project, turn, store).read_rows(sheet)
        citation_id = read["rows"][0]["cells"][0]["citation_id"]
        store.finish_turn(turn["id"], status="completed")
        prefix = f"/api/projects/{pid}/qa/threads"
        path = f"{prefix}/{thread['id']}/citations/{citation_id}"
        result = client.get(path)
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "current"
        assert result.json()["target"] == {
            "kind": "cell",
            "sheet_id": sheet,
            "row_id": row,
            "column_id": column,
            "navigation": None,
            "text_range": None,
        }
        assert (
            client.get(f"{prefix}/{other['id']}/citations/{citation_id}").status_code
            == 404
        )
        project.apply_edits([{"row_id": row, "column_id": column, "value": "Grace"}])
        changed = client.get(path).json()
        assert changed["status"] == "changed"
        assert "Ada" in changed["excerpt"]
        project.delete_sheet(sheet)
        assert client.get(path).json()["status"] == "unavailable"


def test_empty_cell_citation_remains_current_until_the_cell_changes(client):

    with client:
        pid = client.post("/api/projects", json={"name": "Ask"}).json()["id"]
        project = client.app.state.workspace.get(pid)
        sheet = project.add_sheet("Evidence")
        column = project.add_column(sheet, "Name")
        [row] = project.add_rows(sheet, [{}], {"Name": column})
        store = ProjectQAStore(project)
        thread = store.create_thread(title="Question")
        turn = store.submit_turn(thread["id"], request_id="once", question="Who?")
        read = ProjectQATools(project, turn, store).read_rows(sheet, [row], [column])
        citation_id = read["rows"][0]["cells"][0]["citation_id"]
        store.finish_turn(turn["id"], status="completed")
        path = f"/api/projects/{pid}/qa/threads/{thread['id']}/citations/{citation_id}"

        current = client.get(path)
        assert current.status_code == 200, current.text
        assert current.json()["status"] == "current"
        assert current.json()["target"]["row_id"] == row

        project.apply_edits([{"row_id": row, "column_id": column, "value": "Ada"}])
        assert client.get(path).json()["status"] == "changed"


def test_query_and_prepared_evidence_citations_reopen_current_sources(client):

    with client:
        pid = client.post("/api/projects", json={"name": "Sources"}).json()["id"]
        project = client.app.state.workspace.get(pid)
        sheet = project.add_sheet("Evidence")
        column = project.add_column(sheet, "Text")
        rows = project.add_rows(
            sheet, [{"Text": "Ada"}, {"Text": "Grace"}], {"Text": column}
        )
        _, refs = project.get_values_with_refs(sheet, column, row_ids=rows)
        with write_transaction(project.db):
            artifact = record_source_artifact(
                project,
                artifact_kind="text",
                media_type="text/plain",
                title="Original document",
            )
        with write_transaction(project.db):
            span = record_source_span(
                project,
                artifact_id=artifact["id"],
                span_kind="text",
                quote="Ada signed the letter.",
            )
        with write_transaction(project.db):
            link = record_citation(
                project,
                subject_kind="cell_value",
                subject_ref=refs[rows[0]],
                spans=[{"span_id": span["id"]}],
                sheet_id=sheet,
                row_id=rows[0],
                column_id=column,
            )
        scope = {
            "kind": "sources",
            "sources": [{"kind": "rows", "sheet_id": sheet, "row_ids": [rows[0]]}],
        }
        store = ProjectQAStore(project)
        thread = store.create_thread(title="Evidence", scope=scope)
        turn = store.submit_turn(
            thread["id"], request_id="one", question="Who signed?", scope=scope
        )
        tools = ProjectQATools(project, turn, store)
        query = tools.query_rows(
            {
                "schema_version": "frisket.query.v1",
                "kind": "sheet.filter",
                "scope": {"kind": "sheet", "sheet_id": sheet},
                "filter": {},
            },
            limit=0,
        )
        read = tools.read_rows(sheet, [rows[0]], [column])
        passages = tools.open_source(read["rows"][0]["cells"][0]["citation_id"])[
            "passages"
        ]
        citation_id = next(
            p["citation_id"] for p in passages if p["kind"] == "prepared_evidence"
        )
        store.append_event(
            turn["id"],
            kind="answer",
            payload={
                "text": "Ada signed.",
                "citation_ids": [citation_id, query["citation_id"]],
            },
        )
        store.finish_turn(turn["id"], status="completed")
        prefix = f"/api/projects/{pid}/qa/threads/{thread['id']}"
        result = client.get(f"{prefix}/citations/{citation_id}")
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "current"
        assert result.json()["target"] == {
            "kind": "cell_citation",
            "sheet_id": sheet,
            "row_id": rows[0],
            "column_id": column,
            "citation_id": link["stable_id"],
            "artifact_id": artifact["stable_id"],
            "span_id": span["stable_id"],
            "span_ids": [],
        }
        result = client.get(f"{prefix}/citations/{query['citation_id']}").json()
        assert result["target"]["total"] == 1
        assert result["target"]["row_ids"] == [rows[0]]
        events = client.get(prefix + "/events").json()["events"]
        suggestion = next(e for e in events if e["kind"] == "result_suggestion")
        assert suggestion["payload"]["sheet_name"] == "Evidence"
        assert suggestion["citations"][0]["target"]["kind"] == "query"
        project.apply_edits(
            [{"row_id": rows[0], "column_id": column, "value": "Changed"}]
        )
        assert (
            client.get(f"{prefix}/citations/{citation_id}").json()["status"]
            == "changed"
        )
        project.delete_sheet(sheet)
        assert (
            client.get(f"{prefix}/citations/{query['citation_id']}").json()["status"]
            == "unavailable"
        )


def test_patch_null_scope_does_not_broaden_and_blank_title_is_friendly(client):
    with client:
        pid = client.post("/api/projects", json={"name": "Ask"}).json()["id"]
        project = client.app.state.workspace.get(pid)
        sheet = project.add_sheet("Scope")
        scope = {"kind": "sources", "sources": [{"kind": "sheet", "sheet_id": sheet}]}
        path = f"/api/projects/{pid}/qa/threads"
        thread = client.post(path, json={"scope": scope}).json()
        path += f"/{thread['id']}"
        response = client.patch(path, json={"expected_revision": 1, "scope": None})
        assert response.status_code == 200, response.text
        assert response.json()["scope"] == scope
        assert (
            client.patch(
                path,
                json={"expected_revision": response.json()["revision"], "title": "   "},
            ).status_code
            == 422
        )


def test_project_deletion_refuses_active_ask(tmp_path):

    router = ModelRouter(
        model_call_policy=LOCAL_PURCHASE_POLICY,
        keys={"anthropic": "test"},
        use_env_keys=False,
    )
    with make_client(tmp_path, router=router) as client:

        async def runner(*args):
            await asyncio.Event().wait()

        client.app.state.project_qa_service._runner = runner
        pid = client.post("/api/projects", json={"name": "Ask"}).json()["id"]
        path = f"/api/projects/{pid}/qa/threads"
        thread = client.post(path, json={"scope": {"kind": "project"}}).json()
        response = client.post(
            f"{path}/{thread['id']}/turns",
            json={
                "request_id": "one",
                "question": "Read",
                "scope": {"kind": "project"},
            },
        )
        assert response.status_code == 200, response.text
        deletion = client.request(
            "DELETE", f"/api/projects/{pid}", json={"confirm_name": "Ask"}
        )
        assert deletion.status_code == 409, deletion.text
        assert "Ask" in deletion.text


# --- service ---


def test_replay_stop_reconnect_and_restart_do_not_repeat_paid_work(tmp_path):
    async def scenario():
        workspace = Workspace(tmp_path / "ws")
        pid = workspace.create("Ask")["id"]
        started = asyncio.Event()
        calls = []

        async def runner(project, router, turn, store):
            calls.append(turn["id"])
            store.append_event(
                turn["id"], kind="assistant", payload={"text": "Reading sources"}
            )
            started.set()
            await asyncio.Event().wait()

        service = ProjectQAService(workspace, runner=runner)
        thread = await service.create(pid, AskThreadCreate(scope={"kind": "project"}))
        request = AskTurnRequest(
            request_id="once", question="What changed?", scope={"kind": "project"}
        )
        first = await service.submit(pid, thread["id"], request)
        await started.wait()
        assert (await service.submit(pid, thread["id"], request))["id"] == first["id"]
        assert (await service.detail(pid, thread["id"]))["active_turn"]["id"] == first[
            "id"
        ]
        with pytest.raises(ProjectQAConflictError):
            await service.delete(pid, thread["id"])
        await service.stop(pid, thread["id"], first["id"])
        await asyncio.gather(*list(service._tasks.values()))
        assert (await service.detail(pid, thread["id"]))["active_turn"] is None
        assert (
            ProjectQAStore(workspace.get(pid)).get_turn(first["id"])["status"]
            == "stopped"
        )
        assert calls == [first["id"]]
        await service.submit(pid, thread["id"], request)
        await asyncio.sleep(0)
        assert calls == [first["id"]]
        await service.shutdown()

        store = ProjectQAStore(workspace.get(pid))
        abandoned = store.submit_turn(
            thread["id"], request_id="crashed", question="Again"
        )
        restarted = ProjectQAService(workspace, runner=runner)
        assert (await restarted.detail(pid, thread["id"]))["active_turn"] is None
        assert store.get_turn(abandoned["id"])["status"] == "interrupted"
        assert calls == [first["id"]]
        await restarted.shutdown()

    asyncio.run(scenario())


def test_shutdown_is_interrupted_and_cross_thread_stop_is_refused(tmp_path):
    async def scenario():
        workspace = Workspace(tmp_path / "ws")
        pid = workspace.create("Ask")["id"]

        async def runner(*args):
            await asyncio.Event().wait()

        service = ProjectQAService(workspace, runner=runner)
        thread = await service.create(pid, AskThreadCreate(scope={"kind": "project"}))
        other = await service.create(pid, AskThreadCreate(scope={"kind": "project"}))
        turn = await service.submit(
            pid,
            thread["id"],
            AskTurnRequest(
                request_id="one",
                question="Read",
                scope={"kind": "project"},
            ),
        )
        with pytest.raises(LookupError):
            await service.stop(pid, other["id"], turn["id"])
        await service.shutdown()
        assert (
            ProjectQAStore(workspace.get(pid)).get_turn(turn["id"])["status"]
            == "interrupted"
        )

    asyncio.run(scenario())


def test_storage_runs_off_event_loop_and_immediate_stop_drains(tmp_path, monkeypatch):

    async def scenario():
        loop_thread = threading.get_ident()
        original = ProjectQAStore.create_thread

        def checked_create(self, *args, **kwargs):
            assert threading.get_ident() != loop_thread
            return original(self, *args, **kwargs)

        monkeypatch.setattr(ProjectQAStore, "create_thread", checked_create)
        workspace = Workspace(tmp_path / "ws")
        pid = workspace.create("Ask")["id"]
        started = asyncio.Event()

        async def runner(*args):
            started.set()
            await asyncio.Event().wait()

        service = ProjectQAService(workspace, runner=runner)
        thread = await service.create(pid, AskThreadCreate(scope={"kind": "project"}))
        turn = await service.submit(
            pid,
            thread["id"],
            AskTurnRequest(
                request_id="one", question="Read", scope={"kind": "project"}
            ),
        )
        await service.stop(pid, thread["id"], turn["id"])
        await asyncio.gather(*list(service._tasks.values()))
        detail = await service.detail(pid, thread["id"])
        assert detail["active_turn"] is None
        assert detail["history"]["events"][-1]["payload"]["status"] == "stopped"
        await service.shutdown()

    asyncio.run(scenario())


def test_cancelled_submit_still_owns_admitted_turn_and_runtime(tmp_path):

    async def scenario():
        workspace = Workspace(tmp_path / "ws")
        pid = workspace.create("Ask")["id"]
        prepared = asyncio.Event()
        release_prepare = asyncio.Event()
        closed = asyncio.Event()
        calls = []

        class Runtime:
            def call_scope(self, router):
                return nullcontext()

            def settle_call(self, **kwargs):
                raise AssertionError("custom runner has no model calls")

            async def aclose(self):
                closed.set()

        class Port:
            async def prepare_turn(self, **kwargs):
                calls.append(kwargs["turn_id"])
                prepared.set()
                await release_prepare.wait()
                return Runtime()

        workspace.project_qa_runtime_port = Port()

        async def runner(*args):
            await asyncio.Event().wait()

        service = ProjectQAService(workspace, runner=runner)
        thread = await service.create(pid, AskThreadCreate(scope={"kind": "project"}))
        request = AskTurnRequest(
            request_id="once", question="Read", scope={"kind": "project"}
        )
        submit = asyncio.create_task(service.submit(pid, thread["id"], request))
        try:
            await asyncio.wait_for(prepared.wait(), 2)
            submit.cancel()
            release_prepare.set()
            with pytest.raises(asyncio.CancelledError):
                await submit
            replay = await service.submit(pid, thread["id"], request)
            assert calls == [replay["id"]]
            await service.shutdown()
            assert closed.is_set()
            assert (
                ProjectQAStore(workspace.get(pid)).get_turn(replay["id"])["status"]
                == "interrupted"
            )
        finally:
            release_prepare.set()
            if not submit.done():
                submit.cancel()
            await asyncio.gather(submit, return_exceptions=True)
            await service.shutdown()

    asyncio.run(scenario())


def test_runtime_release_may_retire_its_hosted_app(tmp_path):

    async def scenario():
        workspace = Workspace(tmp_path / "ws")
        pid = workspace.create("Ask")["id"]
        closed = asyncio.Event()

        class Runtime:
            def call_scope(self, router):
                return nullcontext()

            def settle_call(self, **kwargs):
                pass

            async def aclose(self):
                await service.shutdown()
                closed.set()

        class Port:
            async def prepare_turn(self, **kwargs):
                return Runtime()

        workspace.project_qa_runtime_port = Port()

        async def runner(*args):
            return {}

        service = ProjectQAService(workspace, runner=runner)
        thread = await service.create(pid, AskThreadCreate(scope={"kind": "project"}))
        await service.submit(
            pid,
            thread["id"],
            AskTurnRequest(
                request_id="one", question="Read", scope={"kind": "project"}
            ),
        )
        await asyncio.wait_for(closed.wait(), 2)

    asyncio.run(scenario())


def test_actual_model_requests_keep_turn_authority_and_settle_durable_calls(tmp_path):

    async def scenario():
        authority = ContextVar("ask-authority", default=None)
        settled = []
        closed = asyncio.Event()

        class Adapter:
            async def complete(self, request, client):
                assert authority.get() is asyncio.current_task()
                return LLMResponse(
                    content=None,
                    data=None,
                    model=request.model,
                    tokens_in=2,
                    tokens_out=3,
                    cost=0.02,
                    tool_calls=[
                        {
                            "name": "final_result",
                            "id": "answer",
                            "args": {
                                "text": "No project sources were needed.",
                                "citation_ids": [],
                            },
                        }
                    ],
                )

        router = ModelRouter(
            model_call_policy=RecordingPurchasePolicy(),
            keys={"anthropic": "test"},
            key_sources={"anthropic": "platform_key"},
            cache=None,
            cache_mode="off",
            use_env_keys=False,
        )
        router._adapters["anthropic"] = Adapter()

        class Runtime:
            @contextmanager
            def call_scope(self, _router):
                token = authority.set(asyncio.current_task())
                try:
                    yield
                finally:
                    authority.reset(token)

            def settle_call(self, *, project, turn_id, call_id):
                assert project.db.execute(
                    "SELECT 1 FROM project_qa_usage_calls WHERE turn_id=? AND call_id=?",
                    (turn_id, call_id),
                ).fetchone()
                assert project.db.execute(
                    "SELECT 1 FROM model_calls WHERE id=?", (call_id,)
                ).fetchone()
                settled.append(call_id)

            async def aclose(self):
                closed.set()

        class Port:
            async def prepare_turn(self, **kwargs):
                return Runtime()

        workspace = Workspace(
            tmp_path / "ws", router=router, project_qa_runtime_port=Port()
        )
        pid = workspace.create("Ask")["id"]
        service = ProjectQAService(workspace)
        thread = await service.create(pid, AskThreadCreate(scope={"kind": "project"}))
        turn = await service.submit(
            pid,
            thread["id"],
            AskTurnRequest(
                request_id="one",
                question="Say hello",
                scope={"kind": "project"},
                model="anthropic/test",
            ),
        )
        await asyncio.wait_for(closed.wait(), 5)
        saved = ProjectQAStore(workspace.get(pid)).get_turn(turn["id"])
        assert saved["status"] == "completed", saved["error_summary"]
        assert len(settled) == 1
        assert authority.get() is None
        await service.shutdown()
        await router.aclose()

    asyncio.run(scenario())


def test_conversation_list_pages_without_losing_old_threads(tmp_path):
    async def scenario():
        service = ProjectQAService(Workspace(tmp_path / "ws"))
        pid = service.workspace.create("Ask")["id"]
        first = await service.create(
            pid, AskThreadCreate(title="First", scope={"kind": "project"})
        )
        second = await service.create(
            pid, AskThreadCreate(title="Second", scope={"kind": "project"})
        )
        assert [row["id"] for row in await service.list(pid, limit=1)] == [second["id"]]
        assert [row["id"] for row in await service.list(pid, limit=1, offset=1)] == [
            first["id"]
        ]
        await service.shutdown()

    asyncio.run(scenario())


def test_invalid_model_response_persists_safe_correlated_diagnostic(tmp_path, caplog):
    async def scenario():
        workspace = Workspace(tmp_path / "ws")
        pid = workspace.create("Ask")["id"]

        async def runner(*args):
            try:
                try:
                    raise RuntimeError("api_key=do-not-log-this provider body")
                except RuntimeError as secret_cause:
                    raise ModelRetry(
                        "Use only current-turn citation IDs returned by the tools; "
                        "reopen prior sources first."
                    ) from secret_cause
            except ModelRetry as cause:
                raise UnexpectedModelBehavior(
                    "Exceeded maximum output retries (1)"
                ) from cause

        service = ProjectQAService(workspace, runner=runner)
        thread = await service.create(pid, AskThreadCreate(scope={"kind": "project"}))
        turn = await service.submit(
            pid,
            thread["id"],
            AskTurnRequest(
                request_id="invalid-output",
                question="What is largest?",
                scope={"kind": "project"},
                model="gemini/test",
            ),
        )
        await asyncio.gather(*list(service._tasks.values()))
        detail = await service.detail(pid, thread["id"])
        status = detail["history"]["events"][-1]
        assert status["kind"] == "status"
        assert status["payload"]["status"] == "failed"
        assert status["payload"]["diagnostic"] == {
            "code": "invalid_model_response",
            "reference": turn["id"],
            "reason": "citation_invalid",
            "last_event": "question",
        }
        assert "do-not-log-this" not in str(status)
        [record] = [
            record
            for record in caplog.records
            if getattr(record, "event", None) == "project_qa_turn_failed"
        ]
        assert record.error_code == "invalid_model_response"
        assert record.reason == "citation_invalid"
        assert record.reference == turn["id"]
        assert record.model == "gemini/test"
        assert record.provider == "gemini"
        assert "do-not-log-this" not in str(record.__dict__)
        await service.shutdown()

    asyncio.run(scenario())


# --- research lifecycle ---


@pytest.mark.anyio
async def test_research_does_not_treat_hosted_billing_as_provider_cost(
    tmp_path, monkeypatch
):

    workspace = Workspace(tmp_path / "workspace")
    pid = workspace.create("Research")["id"]
    service = ProjectQAService(workspace)
    service.configure_research(authorize=lambda pid, context: None)
    monkeypatch.setattr(pricing_policy, "default_pricing_policy", lambda: object())
    try:
        options = await service.research_options(pid)
        assert options["available"] is False
        assert "billing" in options["reason"]
        with pytest.raises(PermissionError, match="billing"):
            service._research_setup(pid, workspace.get(pid), {"budget_usd": "1"}, None)
    finally:
        await service.shutdown()


@pytest.mark.anyio
async def test_runtime_admission_failure_interrupts_research(tmp_path):
    class FailingPort:
        async def prepare_turn(self, **kwargs):
            raise ValueError("Runtime unavailable")

    workspace = Workspace(tmp_path / "workspace", project_qa_runtime_port=FailingPort())
    pid = workspace.create("Research")["id"]
    service = ProjectQAService(workspace)
    service.configure_research(authorize=lambda pid, context: None)
    thread = await service.create(pid, AskThreadCreate(scope={"kind": "project"}))
    try:
        with pytest.raises(ValueError, match="Runtime unavailable"):
            await service.submit(
                pid,
                thread["id"],
                AskTurnRequest(
                    scope={"kind": "project"},
                    request_id="failed-runtime",
                    question="Investigate",
                    research={"budget_usd": "1"},
                ),
            )
        detail = await service.detail(pid, thread["id"])
        question = next(
            event
            for event in detail["history"]["events"]
            if event["kind"] == "question"
        )
        project = workspace.get(pid)
        turn = ProjectQAStore(project).get_turn(question["turn_id"])
        assert turn["status"] == "failed"
        assert (
            ProjectQAResearchStore(project).get_for_turn(turn["id"])["state"]
            == "interrupted"
        )
    finally:
        await service.shutdown()


@pytest.mark.anyio
async def test_replaying_interrupted_parent_does_not_create_live_research(tmp_path):

    workspace = Workspace(tmp_path / "workspace")
    pid = workspace.create("Research")["id"]
    service = ProjectQAService(workspace)
    service.configure_research(authorize=lambda pid, context: None)
    thread = await service.create(pid, AskThreadCreate(scope={"kind": "project"}))
    body = AskTurnRequest(
        scope={"kind": "project"},
        request_id="interrupted",
        question="Investigate",
        research={"budget_usd": "1"},
    )
    store = await service.store(pid)
    values = body.model_dump(mode="json")
    values["scope"] = body.scope.model_dump(exclude_none=True)
    parent = store.submit_turn(thread["id"], **values)
    store.finalize_turn(parent["id"], status="interrupted")
    try:
        replay = await service.submit(pid, thread["id"], body)
        assert replay["status"] == "interrupted"
        with pytest.raises(ProjectQAResearchNotFound):
            ProjectQAResearchStore(workspace.get(pid)).get_for_turn(parent["id"])
    finally:
        await service.shutdown()


@pytest.mark.anyio
async def test_research_options_expose_manifest_only_and_allow_no_skills(tmp_path):
    workspace = Workspace(tmp_path / "workspace")
    pid = workspace.create("Research")["id"]
    library = SkillLibrary(tmp_path / "library")
    skill = library.create(
        "---\nname: analyze-records\ndescription: Analyze project records\n---\nPrivate instructions here."
    )
    service = ProjectQAService(workspace)
    service.configure_research(
        authorize=lambda pid, context: None, skills=lambda: library
    )
    try:
        options = await service.research_options(pid)
        assert options["available"] is True
        assert options["skills"] == [
            {"name": "analyze-records", "description": "Analyze project records"}
        ]
        assert "Private instructions" not in str(options)
        base = {"budget_usd": "1", "write_mode": "ask_overwrite", "max_turns": None}
        project = workspace.get(pid)
        assert (
            service._research_setup(pid, project, {**base, "skills": []}, None)[
                "skills"
            ]
            == []
        )
        assert (
            len(
                service._research_setup(pid, project, {**base, "skills": None}, None)[
                    "skills"
                ]
            )
            == 1
        )
        snapshots = service._research_setup(
            pid, project, {**base, "skills": None}, None
        )["skills"]
        library.set_enabled(
            skill["id"], expected_revision=skill["revision"], enabled=False
        )
        with pytest.raises(ResearchSkillsUnavailable):
            service._authorize_run(pid, None, snapshots)
    finally:
        await service.shutdown()


@pytest.mark.anyio
async def test_exact_consent_survives_client_return_and_stop_interrupts_waiter(
    tmp_path,
):
    workspace = Workspace(tmp_path / "workspace")
    pid = workspace.create("Research")["id"]
    published = asyncio.Event()
    answers = []

    async def runner(project, router, turn, store):
        session = service._research_sessions[(project, turn["id"])]
        original = session.store.publish_pause
        loop = asyncio.get_running_loop()

        def publish(*args, **kwargs):
            result = original(*args, **kwargs)
            if result["state"] == "paused":
                loop.call_soon_threadsafe(published.set)
            return result

        session.store.publish_pause = publish
        answers.append(
            await session.pause({"kind": "action", "title": "Extract fields"})
        )
        return {"text": "Done"}

    service = ProjectQAService(workspace, runner=runner)
    service.configure_research(authorize=lambda pid, context: None)
    thread = await service.create(pid, AskThreadCreate(scope={"kind": "project"}))
    request = AskTurnRequest(
        scope={"kind": "project"},
        request_id="research",
        question="Investigate",
        research={"budget_usd": "1.00"},
    )
    try:
        turn = await service.submit(pid, thread["id"], request, actor="reporter")
        await asyncio.wait_for(published.wait(), 5)
        detail = await service.detail(pid, thread["id"])
        state = detail["active_turn"]["research_state"]
        assert state["state"] == "paused"
        assert answers == []
        pause_request = dict(
            expected_revision=state["revision"],
            pause_id=state["pending_pause"]["id"],
        )
        with pytest.raises(PermissionError):
            await service.resume_research(
                pid,
                thread["id"],
                turn["id"],
                AskResearchResume(**pause_request, decision="approve"),
                actor="another-reporter",
                request_context=None,
            )
        with pytest.raises(ValueError, match="Approve or skip"):
            await service.resume_research(
                pid,
                thread["id"],
                turn["id"],
                AskResearchResume(**pause_request, budget_usd="2.00"),
                actor="reporter",
                request_context=None,
            )
        await service.resume_research(
            pid,
            thread["id"],
            turn["id"],
            AskResearchResume(**pause_request, decision="approve"),
            actor="reporter",
            request_context=None,
        )
        await asyncio.gather(*list(service._tasks.values()))
        assert answers == ["approve"]
        assert (
            ProjectQAStore(workspace.get(pid)).get_turn(turn["id"])["status"]
            == "completed"
        )

        published.clear()
        second = await service.submit(
            pid,
            thread["id"],
            request.model_copy(update={"request_id": "second"}),
            actor="reporter",
        )
        await asyncio.wait_for(published.wait(), 5)
        await service.stop(pid, thread["id"], second["id"])
        await asyncio.gather(*list(service._tasks.values()))
        assert answers == ["approve"]
        assert (
            ProjectQAResearchStore(workspace.get(pid)).get_for_turn(second["id"])[
                "state"
            ]
            == "interrupted"
        )
    finally:
        await service.shutdown()


# --- research ownership ---


@pytest.fixture
def owned_research(make_project):
    project = make_project("Research")
    qa = ProjectQAStore(project)
    thread = qa.create_thread(title="Investigation")
    turn = qa.submit_turn(
        thread["id"], request_id="one", question="Investigate", submitted_by="reporter"
    )
    ledger = ProjectQAResearchStore(project)
    run = ledger.create(
        turn_id=turn["id"],
        actor="reporter",
        budget_micros=100,
        currency="USD",
        write_mode="ask_each",
        max_turns=None,
    )
    session = ResearchSession(
        ledger, run["id"], authorize=lambda: None, model_servers=()
    )
    yield project, qa, turn, ledger, run, session


async def _pending(ledger, research_id, *, operation_id=None, kind=None):
    async with asyncio.timeout(5):
        while True:
            snapshot = await await_thread_worker(ledger.get, research_id)
            pause = snapshot["pending_pause"]
            if (
                pause
                and (operation_id is None or pause.get("operation_id") == operation_id)
                and (kind is None or pause.get("kind") == kind)
            ):
                return snapshot
            await asyncio.sleep(0)


async def _resume(
    session, snapshot, decision="approve", *, authorize=lambda: None, **preferences
):
    return await session.resume(
        pause_id=snapshot["pending_pause"]["id"],
        expected_revision=snapshot["revision"],
        decision=decision,
        actor="reporter",
        authorize=authorize,
        context=None,
        **preferences,
    )


def _action(operation_id):
    return {
        "kind": "action",
        "operation_id": operation_id,
        "payload_identity": f"payload:{operation_id}",
        "title": f"Write {operation_id}",
    }


@pytest.mark.anyio
async def test_parallel_operations_never_share_a_pause(owned_research):
    _, _, _, ledger, run, session = owned_research
    first = asyncio.create_task(session.pause(_action("A")))
    second = None
    try:
        approval_a = await _pending(ledger, run["id"], operation_id="A")
        second = asyncio.create_task(session.pause(_action("B")))
        # Let B enter the approval owner while A remains unanswered.
        await asyncio.sleep(0.02)
        assert ledger.get(run["id"])["pending_pause"] == approval_a["pending_pause"]
        await _resume(session, approval_a)
        assert await asyncio.wait_for(first, 5) == "approve"

        approval_b = await _pending(ledger, run["id"], operation_id="B")
        assert not second.done()
        assert approval_b["pending_pause"]["id"] != approval_a["pending_pause"]["id"]
        with pytest.raises(ProjectQAResearchConflictError):
            await _resume(session, approval_a)
        assert not second.done()
        assert ledger.get(run["id"])["pending_pause"] == approval_b["pending_pause"]

        await _resume(session, approval_b, "skip")
        assert await asyncio.wait_for(second, 5) == "skip"
    finally:
        tasks = [first, *([second] if second is not None else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.anyio
async def test_waiting_pause_rechecks_whether_it_is_still_needed(owned_research):
    _, _, _, ledger, run, session = owned_research
    needs_budget = True

    async def budget_still_needed():
        return needs_budget

    first = asyncio.create_task(session.pause(_action("A")))
    second = None
    try:
        approval = await _pending(ledger, run["id"], operation_id="A")
        second = asyncio.create_task(
            session.pause(
                {"kind": "budget", "operation_id": "B", "next_estimate_micros": 200},
                recheck=budget_still_needed,
            )
        )
        needs_budget = False
        await _resume(session, approval)
        assert await asyncio.wait_for(first, 5) == "approve"
        assert await asyncio.wait_for(second, 5) == "continue"
        assert ledger.get(run["id"])["pending_pause"] is None
    finally:
        tasks = [first, *([second] if second is not None else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.anyio
async def test_budget_increase_covers_a_waiting_operations_stale_pause(
    owned_research, monkeypatch
):
    _, _, _, ledger, run, session = owned_research
    published = []
    original_publish = ledger.publish_pause

    def publish(research_id, approval):
        published.append(approval["operation_id"])
        return original_publish(research_id, approval)

    monkeypatch.setattr(ledger, "publish_pause", publish)
    first = asyncio.create_task(
        session.pause(
            {"kind": "budget", "operation_id": "A", "next_estimate_micros": 200}
        )
    )
    second = None
    try:
        approval = await _pending(ledger, run["id"], operation_id="A")
        second = asyncio.create_task(
            session.pause(
                {"kind": "budget", "operation_id": "B", "next_estimate_micros": 150}
            )
        )
        await asyncio.sleep(0)
        await _resume(session, approval, "continue", budget_micros=300)
        assert await asyncio.wait_for(first, 5) == "continue"
        assert await asyncio.wait_for(second, 5) == "continue"
        assert published == ["A"]
        assert ledger.get(run["id"])["pending_pause"] is None
        assert ledger.budget_summary(run["id"])["budget_micros"] == 300
    finally:
        tasks = [first, *([second] if second is not None else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize("stopping", [False, True])
async def test_parent_stop_or_finish_refuses_new_pause_authority(
    owned_research, stopping
):
    _, qa, turn, ledger, run, session = owned_research
    waiter = asyncio.create_task(session.pause(_action("A")))
    try:
        approval = await _pending(ledger, run["id"], operation_id="A")
        if stopping:
            qa.request_stop(turn["id"])
        else:
            qa.finalize_turn(turn["id"], status="completed")
        with pytest.raises(ProjectQAResearchConflictError):
            await _resume(session, approval)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        with pytest.raises(ProjectQAResearchConflictError):
            await asyncio.wait_for(session.pause(_action("B")), 5)
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.anyio
async def test_authority_recheck_can_pause_after_a_decision_without_deadlocking(
    owned_research, monkeypatch
):
    _, _, _, ledger, run, session = owned_research
    skills_enabled = True

    def authorize():
        if not skills_enabled:
            raise ResearchSkillsUnavailable("Selected skill was disabled")

    original_resume = ledger.resume

    def disable_skills_after_consumption(*args, **kwargs):
        nonlocal skills_enabled
        result = original_resume(*args, **kwargs)
        skills_enabled = False
        return result

    monkeypatch.setattr(ledger, "resume", disable_skills_after_consumption)
    waiter = asyncio.create_task(session.pause(_action("A")))
    try:
        approval = await _pending(ledger, run["id"], operation_id="A")
        await _resume(session, approval, authorize=authorize)
        skills = await _pending(ledger, run["id"], kind="skills")
        assert not waiter.done()
        skills_enabled = True
        monkeypatch.setattr(ledger, "resume", original_resume)
        await _resume(session, skills, "continue", authorize=authorize)
        assert await asyncio.wait_for(waiter, 5) == "approve"
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.anyio
async def test_disconnected_resume_commits_and_wakes_once_despite_duplicate_resume(
    owned_research, monkeypatch
):
    _, _, _, ledger, run, session = owned_research
    consuming = threading.Event()
    release = threading.Event()
    original_resume = ledger.resume
    consumptions = []

    def blocked_resume(*args, **kwargs):
        consuming.set()
        assert release.wait(5), "test must release the real consumption transaction"
        result = original_resume(*args, **kwargs)
        consumptions.append(result["revision"])
        return result

    monkeypatch.setattr(ledger, "resume", blocked_resume)
    waiter = asyncio.create_task(session.pause(_action("A")))
    resume = duplicate = None
    try:
        approval = await _pending(ledger, run["id"], operation_id="A")
        resume = asyncio.create_task(_resume(session, approval))
        assert await asyncio.to_thread(consuming.wait, 5)
        resume.cancel()
        duplicate = asyncio.create_task(_resume(session, approval))
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await resume
        with pytest.raises(ProjectQAResearchConflictError):
            await duplicate
        assert await asyncio.wait_for(waiter, 5) == "approve"
        assert consumptions == [approval["revision"] + 1]
        assert ledger.get(run["id"])["pending_pause"] is None
    finally:
        release.set()
        tasks = [
            waiter,
            *([resume] if resume is not None else []),
            *([duplicate] if duplicate is not None else []),
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.anyio
async def test_closing_owner_rejects_both_pending_and_queued_decisions(owned_research):
    _, qa, turn, ledger, run, session = owned_research
    first = asyncio.create_task(session.pause(_action("A")))
    second = None
    try:
        approval = await _pending(ledger, run["id"], operation_id="A")
        second = asyncio.create_task(session.pause(_action("B")))
        await asyncio.sleep(0)
        qa.request_stop(turn["id"])
        session.close()
        outcomes = await asyncio.wait_for(
            asyncio.gather(first, second, return_exceptions=True), 5
        )
        assert isinstance(outcomes[0], asyncio.CancelledError)
        assert isinstance(outcomes[1], ProjectQAResearchConflictError)
        with pytest.raises(ProjectQAResearchConflictError):
            await _resume(session, approval)
        assert ledger.get(run["id"])["pending_pause"] is None
    finally:
        tasks = [first, *([second] if second is not None else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.anyio
async def test_closing_owner_during_authorization_cannot_return_live_authority(
    owned_research,
):
    _, _, _, _, _, session = owned_research
    authorizing = threading.Event()
    release = threading.Event()

    def authorize():
        authorizing.set()
        assert release.wait(5), "test must release the authorization worker"

    session.authorize = authorize
    checking = asyncio.create_task(session.check_authority())
    try:
        assert await asyncio.to_thread(authorizing.wait, 5)
        session.close()
        release.set()
        with pytest.raises(ProjectQAResearchConflictError):
            await asyncio.wait_for(checking, 5)
    finally:
        release.set()
        checking.cancel()
        await asyncio.gather(checking, return_exceptions=True)


def test_finalization_rolls_back_parent_research_and_events_together(owned_research):
    project, qa, turn, ledger, run, _ = owned_research
    before_events = project.db.execute(
        "SELECT COUNT(*) FROM project_qa_events WHERE turn_id=?", (turn["id"],)
    ).fetchone()[0]
    project.db.execute(
        "CREATE TRIGGER refuse_research_terminal BEFORE UPDATE ON project_qa_research_runs "
        "WHEN NEW.state IN ('completed','interrupted') "
        "BEGIN SELECT RAISE(ABORT,'simulated research write failure'); END"
    )
    project.db.commit()
    with pytest.raises(
        sqlite3.IntegrityError, match="simulated research write failure"
    ):
        qa.finalize_turn(turn["id"], status="completed")
    assert qa.get_turn(turn["id"])["status"] == "running"
    assert ledger.get(run["id"])["state"] == "running"
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM project_qa_events WHERE turn_id=?", (turn["id"],)
        ).fetchone()[0]
        == before_events
    )


def test_returned_paid_usage_settles_after_parent_stop(owned_research):
    _, qa, turn, ledger, run, _ = owned_research
    ledger.admit_operation(
        run["id"],
        operation_id="paid-call",
        payload_identity="exact-paid-payload",
        operation_kind="model",
        estimate_micros=70,
    )
    qa.request_stop(turn["id"])
    qa.finalize_turn(turn["id"], status="interrupted")
    ledger.settle_operation(
        run["id"],
        operation_id="paid-call",
        payload_identity="exact-paid-payload",
        actual_micros=90,
    )
    assert qa.get_turn(turn["id"])["status"] == "stopped"
    assert ledger.get(run["id"])["state"] == "interrupted"
    assert ledger.budget_summary(run["id"])["settled_micros"] == 90
    assert ledger.budget_summary(run["id"])["reserved_micros"] == 0
    with pytest.raises(ProjectQAResearchConflictError):
        ledger.admit_operation(
            run["id"],
            operation_id="new-paid-call",
            payload_identity="new-payload",
            operation_kind="model",
            estimate_micros=1,
        )


# --- research runtime ---

MODEL_ID = "gemini/gemini-3.5-flash"


class ScriptedPurchasePolicy:
    """Explicit platform composition for the scripted provider below."""

    def __init__(self):
        self.requests = []
        self.observations = []

    def prepare(self, request, credential_source):
        assert credential_source == "platform_key"
        return request

    def admit_purchase(self, request):
        assert request.credential_source == "platform_key"
        self.requests.append(request)
        return PurchaseAdmission("metered", f"test-operation-{len(self.requests)}")

    def record_purchase(self, request, admission, observation):
        assert admission.operation_id == f"test-operation-{len(self.observations) + 1}"
        self.observations.append(observation)

    def fail_purchase(self, request, admission, error):
        pytest.fail(f"Scripted provider unexpectedly failed: {type(error).__name__}")


class ScriptedAdapter:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = responses
        self.calls = 0
        self.policy = ScriptedPurchasePolicy()

    async def complete(self, request, client) -> LLMResponse:
        assert len(self.policy.requests) == self.calls + 1
        response = self.responses[self.calls]
        self.calls += 1
        return response


def _response(
    *,
    tool_calls: int = 0,
    tool_start: int = 0,
    text: str | None = None,
    cost: float = 0.000_001,
) -> LLMResponse:
    return LLMResponse(
        content=text,
        data=None,
        model=MODEL_ID,
        tokens_in=10,
        tokens_out=10,
        cost=cost,
        tool_calls=[
            {"name": "inspect_sheets", "args": {}, "id": f"inspect-{index}"}
            for index in range(tool_start, tool_start + tool_calls)
        ],
    )


def _runtime(
    make_project, *, name: str, max_turns: int | None, responses: list[LLMResponse]
) -> tuple[
    Project,
    ProjectQAStore,
    ProjectQAResearchStore,
    dict[str, Any],
    ResearchSession,
    ModelRouter,
    ScriptedAdapter,
]:
    project = make_project(name)
    qa = ProjectQAStore(project)
    thread = qa.create_thread(title=name, scope={"kind": "project"})
    turn = qa.submit_turn(
        thread["id"],
        request_id=name,
        question="Inspect until the investigation is complete.",
        scope={"kind": "project"},
        model=MODEL_ID,
        research={"budget_usd": "10", "max_turns": max_turns},
    )
    ledger = ProjectQAResearchStore(project)
    research = ledger.create(
        turn_id=turn["id"],
        actor="researcher",
        budget_micros=10_000_000,
        currency="USD",
        write_mode="ask_overwrite",
        max_turns=max_turns,
    )
    session = ResearchSession(
        ledger, research["id"], authorize=lambda: None, model_servers=()
    )
    adapter = ScriptedAdapter(responses)
    router = ModelRouter(
        keys={"gemini": "test-key"},
        key_sources={"gemini": "platform_key"},
        cache=None,
        cache_mode="off",
        max_retries=0,
        use_env_keys=False,
        model_call_policy=adapter.policy,
    )
    router._adapters["gemini"] = adapter
    return project, qa, ledger, research, session, router, adapter


@pytest.mark.anyio
async def test_research_crosses_ordinary_caps_and_finite_turn_limit_pauses(
    make_project, monkeypatch
) -> None:

    monkeypatch.setitem(PRICES, "gemini-3.5-flash", (1.0, 1.0))
    responses = [_response(tool_calls=25)]
    responses.extend(
        _response(tool_calls=1, tool_start=index) for index in range(25, 33)
    )
    responses.append(_response(text="Investigation complete."))
    project, qa, ledger, research, session, router, adapter = _runtime(
        make_project,
        name="unlimited",
        max_turns=None,
        responses=responses,
    )
    try:
        turn = qa.get_turn(research["turn_id"])
        result = await run_turn(project, router, turn, qa, research=session)
        assert result["text"] == "Investigation complete."
        assert adapter.calls == 10
        assert len(adapter.policy.observations) == adapter.calls
        assert ledger.get(research["id"])["turn_count"] == 10
        completed_tools = [
            event
            for event in qa.events(turn["thread_id"], limit=200)["events"]
            if event["kind"] == "tool_completed"
        ]
        assert len(completed_tools) == 33
    finally:
        await router.aclose()

    project, qa, ledger, research, session, router, adapter = _runtime(
        make_project,
        name="finite",
        max_turns=1,
        responses=[_response(tool_calls=1), _response(text="must not be sent")],
    )
    published = asyncio.Event()
    original_update = ledger.publish_pause
    loop = asyncio.get_running_loop()

    def observe_pause(*args, **kwargs):
        updated = original_update(*args, **kwargs)
        if updated["state"] == "paused":
            loop.call_soon_threadsafe(published.set)
        return updated

    ledger.publish_pause = observe_pause
    task = asyncio.create_task(
        run_turn(
            project, router, qa.get_turn(research["turn_id"]), qa, research=session
        )
    )
    try:
        await asyncio.wait_for(published.wait(), 5)
        current = ledger.get(research["id"])
        assert current["pending_pause"]["kind"] == "turn_limit"
        assert current["turn_count"] == 1
        assert adapter.calls == 1
        assert len(adapter.policy.observations) == adapter.calls
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await router.aclose()


@pytest.mark.anyio
async def test_cancellation_during_usage_recording_still_settles_actual_cost(
    make_project, monkeypatch
) -> None:

    monkeypatch.setitem(PRICES, "gemini-3.5-flash", (1.0, 1.0))
    response = _response(text="Investigation complete.", cost=0.000_321)
    project, qa, ledger, research, session, router, _adapter = _runtime(
        make_project,
        name="cancel-settlement",
        max_turns=None,
        responses=[response],
    )
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_record_usage = qa.record_usage

    def blocked_record_usage(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(5):
            raise TimeoutError("test did not release usage persistence")
        return original_record_usage(*args, **kwargs)

    qa.record_usage = blocked_record_usage
    task = asyncio.create_task(
        run_turn(
            project, router, qa.get_turn(research["turn_id"]), qa, research=session
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        [operation] = ledger.operations(research["id"])
        assert operation["actual_micros"] == 321
        assert ledger.budget_summary(research["id"])["reserved_micros"] == 0
        assert len(_adapter.policy.observations) == _adapter.calls == 1
    finally:
        release.set()
        await router.aclose()


@pytest.mark.anyio
async def test_action_tool_exposes_and_validates_the_canonical_draft(
    make_project,
) -> None:
    """A misplaced setting is corrected before an execution service sees it."""
    draft = {
        "action_id": "media.to_markdown",
        "scope": {"kind": "sheet_rows", "sheet_id": 1},
        "params": {"source": "PDF", "engine": "markitdown"},
        "output_names": {"markdown": "Text"},
    }
    wrong = {**draft, "engine": "markitdown"}
    replies = []
    for index, args in enumerate((wrong, draft)):
        reply = _response()
        reply.tool_calls = [
            {
                "name": "prepare_action",
                "args": {"title": "Convert PDFs", "draft": args},
                "id": f"prepare-{index}",
            }
        ]
        replies.append(reply)
    replies.append(_response(text="Action prepared."))
    project, qa, ledger, research, session, router, adapter = _runtime(
        make_project, name="typed-action", max_turns=None, responses=replies
    )
    prepared = []

    class Execution:
        async def prepare_action(self, title, value):
            prepared.append(value)
            return {
                "proposal": {"title": title, "spec": value},
                "event_ref": {"event_seq": 5, "dispatch_id": "prepared-1"},
            }

    turn = qa.get_turn(research["turn_id"])
    result = await run_turn(
        project, router, turn, qa, research=session, execution=Execution()
    )
    assert result["text"] == "Action prepared."
    assert prepared == [draft]
    assert adapter.calls == 3
    assert len(adapter.policy.observations) == adapter.calls


# --- research session ---


@pytest.mark.anyio
async def test_model_budget_settles_once_and_revocation_blocks_next_request(
    make_project, monkeypatch
):

    monkeypatch.setitem(PRICES, "gpt-4o", (2.5, 10.0))
    project = make_project("Research")
    qa = ProjectQAStore(project)
    thread = qa.create_thread(title="Research")
    turn = qa.submit_turn(thread["id"], request_id="one", question="Analyze")
    ledger = ProjectQAResearchStore(project)
    run = ledger.create(
        turn_id=turn["id"],
        actor="reporter",
        budget_micros=1_000_000,
        currency="USD",
        write_mode="ask_overwrite",
        max_turns=None,
    )
    allowed = True

    def authorize():
        if not allowed:
            raise PermissionError("Project access revoked")

    session = ResearchSession(ledger, run["id"], authorize=authorize, model_servers=())
    request = LLMRequest(
        model="openai/gpt-4o",
        messages=[{"role": "user", "content": "Analyze"}],
        max_tokens=100,
    )
    await session.before_model(request)
    response = LLMResponse(
        content="Done",
        data=None,
        model=request.model,
        tokens_in=10,
        tokens_out=10,
        cost=0.0001,
    )
    await session.after_model(response)
    await session.after_model(response)
    assert ledger.budget_summary(run["id"])["settled_micros"] == 100
    assert ledger.budget_summary(run["id"])["reserved_micros"] == 0
    assert ledger.get(run["id"])["turn_count"] == 1
    allowed = False
    with pytest.raises(PermissionError):
        await session.before_model(request)
    assert ledger.get(run["id"])["turn_count"] == 1


@pytest.mark.anyio
async def test_model_budget_uses_authoritative_cloud_cost_and_zero_for_cache(
    make_project, monkeypatch
):

    monkeypatch.setitem(PRICES, "gpt-4o", (2.5, 10.0))
    project = make_project("Research")
    qa = ProjectQAStore(project)
    thread = qa.create_thread(title="Research")
    turn = qa.submit_turn(thread["id"], request_id="one", question="Analyze")
    ledger = ProjectQAResearchStore(project)
    run = ledger.create(
        turn_id=turn["id"],
        actor="reporter",
        budget_micros=1_000_000,
        currency="USD",
        write_mode="ask_overwrite",
        max_turns=None,
    )
    session = ResearchSession(
        ledger, run["id"], authorize=lambda: None, model_servers=()
    )
    request = LLMRequest(
        model="openai/gpt-4o",
        messages=[{"role": "user", "content": "Analyze"}],
        max_tokens=100,
    )

    await session.before_model(request)
    cloud = LLMResponse(
        content="Done",
        data=None,
        model=request.model,
        tokens_in=10,
        tokens_out=10,
        cost=0.000_001,
        actual_accounting=LLMActualAccounting.one(
            cost=0.000_321,
            model_call={"id": "cloud-call"},
            receipt={"execution_id": "cloud-call"},
        ),
    )
    await session.after_model(cloud)

    await session.before_model(request)
    cached = LLMResponse(
        content="Cached",
        data=None,
        model=request.model,
        tokens_in=10,
        tokens_out=10,
        cost=0.5,
        cached=True,
    )
    await session.after_model(cached)

    assert ledger.budget_summary(run["id"])["settled_micros"] == 321
    assert ledger.budget_summary(run["id"])["reserved_micros"] == 0


@pytest.mark.anyio
async def test_unpriced_model_pauses_without_consuming_budget_or_turn(make_project):
    project = make_project("Research")
    qa = ProjectQAStore(project)
    thread = qa.create_thread(title="Research")
    turn = qa.submit_turn(thread["id"], request_id="one", question="Analyze")
    ledger = ProjectQAResearchStore(project)
    run = ledger.create(
        turn_id=turn["id"],
        actor=None,
        budget_micros=1_000_000,
        currency="USD",
        write_mode="ask_each",
        max_turns=None,
    )
    session = ResearchSession(
        ledger, run["id"], authorize=lambda: None, model_servers=()
    )
    published = asyncio.Event()
    original_update = ledger.publish_pause

    def update(*args, **kwargs):
        result = original_update(*args, **kwargs)
        loop.call_soon_threadsafe(published.set)
        return result

    ledger.publish_pause = update
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(
        session.before_model(LLMRequest(model="openai/unpriced-model", messages=[]))
    )
    await asyncio.wait_for(published.wait(), 5)
    assert ledger.get(run["id"])["pending_pause"]["kind"] == "unknown_cost"
    assert ledger.operations(run["id"]) == []
    assert ledger.get(run["id"])["turn_count"] == 0
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
