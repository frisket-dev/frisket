"""Project Ask actions, action host, plugin execution, skill runtime, skill adapter, and skill authoring tests."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from helpers import make_client

from frisket.actions.system import root_action_catalog_payload
from frisket.ai.llm import ModelRouter
from frisket.ai.llm.types import LLMRequest, LLMResponse
from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.contracts.http.project_qa import AskThreadCreate, AskTurnRequest
from frisket.engine.store import Project
from frisket.engine.store.project_qa import ProjectQAConflictError, ProjectQAStore
from frisket.engine.store.project_qa_research import ProjectQAResearchStore
from frisket.execution.pricing_policy import usd_to_micros
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY
from frisket.server.services.action_runs import ActionRunService
from frisket.server.services.project_qa import ProjectQAService
from frisket.server.services.project_qa_action_host import ProjectAskActionHost
from frisket.server.services.project_qa_actions import (
    ProjectAskActionService,
    ProjectAskActionUnpriced,
    save_prepared_project_ask_action,
)
from frisket.server.services.project_qa_child_runs import ProjectQAChildRunService
from frisket.server.services.project_qa_execution import ProjectQAExecutionService
from frisket.server.services.project_qa_research import ResearchSession
from frisket.server.services.project_qa_runner import run_turn
from frisket.server.services.project_qa_skills import (
    ProjectQASkillAdapter,
    ProjectQASkillConflict,
)
from frisket.server.services.skills import SkillLibrary, SkillLibraryError
from frisket.server.services.workbench import WorkbenchService
from frisket.server.workspace import Workspace

# --- actions ---


@dataclass(frozen=True)
class _OutputGrant:
    receipt_id: str
    sheet_id: int
    column_ids: frozenset[int] | None
    row_ids: frozenset[int] | None


def _allows_output_cell(
    grants: tuple[_OutputGrant, ...], *, sheet_id: int, row_id: int, column_id: int
) -> bool:
    return any(
        grant.sheet_id == sheet_id
        and (grant.row_ids is None or row_id in grant.row_ids)
        and (grant.column_ids is None or column_id in grant.column_ids)
        for grant in grants
    )


def _actions_turn(project: Project) -> tuple[ProjectQAStore, dict[str, object], int]:
    sheet_id = project.add_sheet("People")
    project.add_column(sheet_id, "Name")
    store = ProjectQAStore(project)
    thread = store.create_thread(title="Ask")
    return (
        store,
        store.submit_turn(thread["id"], request_id="one", question="Prepare"),
        sheet_id,
    )


def _draft(sheet_id: int) -> dict[str, object]:
    return {
        "action_id": "map.template",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"template": {"text": "Hello {{Name}}"}},
        "output_names": {},
    }


def _estimate(action: dict[str, object]) -> dict[str, object]:
    assert action["idempotency_key"].startswith("ask-action:")
    return {
        "estimate": {
            "billed_cost": 17,
            "policy_id": "normal-policy",
            "promise_set_hash": "confirmation-hash",
            "requires_confirmation": True,
        }
    }


def _approval(authority: dict[str, object]) -> dict[str, object]:
    return {
        "payload_identity": authority["payload_identity"],
        "promise_set_hash": authority["estimate"]["estimate"]["promise_set_hash"],
    }


def test_prepared_ask_event_revalidates_then_uses_budget_and_action_services(
    make_project,
):
    project = make_project("project")
    store, turn, sheet_id = _actions_turn(project)
    catalog = root_action_catalog_payload()
    saved = save_prepared_project_ask_action(
        project,
        turn,
        store,
        title="Greet people",
        draft=_draft(sheet_id),
        catalog_payload=catalog,
        estimate_provider=_estimate,
    )
    assert saved.event["payload"]["proposal"] == saved.proposal
    assert saved.event["payload"]["prepared_action"]["payload_identity"] == (
        saved.payload_identity
    )
    stored = saved.event["payload"]["prepared_action"]
    assert stored["effects"] == {
        "kind": "typed_map_rows",
        "output_names": {"rendered": "rendered"},
        "output_target_preconditions": {"rendered": None},
        "replace_existing": False,
    }
    assert stored["estimate"]["estimate"]["billed_cost"] == 17

    admitted: list[dict[str, object]] = []
    launched: list[tuple[str, dict[str, object]]] = []
    service = ProjectAskActionService(
        catalog_payload_provider=lambda: catalog,
        estimate_provider=_estimate,
        research_for_turn=lambda turn_id: {"id": "research-1", "turn_id": turn_id},
        authorize_dispatch=_approval,
        admit_operation=lambda research_id, **kwargs: admitted.append(
            {"research_id": research_id, **kwargs}
        ),
        run_action=lambda project_id, body: (
            launched.append((project_id, body)) or {"status": "queued"}
        ),
    )
    assert service.launch(
        project,
        store,
        saved.reference,
        project_id="project",
    ) == {"status": "queued"}
    assert admitted == [
        {
            "research_id": "research-1",
            "operation_id": saved.reference.dispatch_id,
            "payload_identity": saved.payload_identity,
            "operation_kind": "action",
            "estimate_micros": 17,
            "metadata": {
                "prepared_event_seq": saved.reference.event_seq,
                "prepared_turn_id": saved.reference.turn_id,
                "action_id": "map.template",
            },
        }
    ]
    assert launched == [
        (
            "project",
            {
                **saved.prepared.request.model_dump(mode="json"),
                "idempotency_key": saved.reference.dispatch_id,
                "expected_generated_columns": {"rendered": None},
                "confirmation": "confirmation-hash",
            },
        )
    ]
    assert launched[0][1]["replace_existing"] is False


def test_ask_action_revalidates_a_file_derived_column_receipt_at_launch(make_project):
    project = make_project("project")
    sheet_id = project.add_sheet("Files")
    file_column = project.add_column(sheet_id, "File")
    markdown_column = project.add_column(sheet_id, "Markdown")
    [row_id] = project.add_rows(
        sheet_id,
        [{"File": "report.pdf", "Markdown": "derived"}],
        {"File": file_column, "Markdown": markdown_column},
    )
    store = ProjectQAStore(project)
    thread = store.create_thread(title="Ask")
    turn = store.submit_turn(
        thread["id"],
        request_id="derived-column",
        question="Use the converted document",
        scope={
            "kind": "sources",
            "sources": [
                {
                    "kind": "file",
                    "sheet_id": sheet_id,
                    "row_id": row_id,
                    "column_id": file_column,
                }
            ],
        },
    )
    grant = _OutputGrant(
        receipt_id="receipt-markdown",
        sheet_id=sheet_id,
        column_ids=frozenset({markdown_column}),
        row_ids=frozenset({row_id}),
    )
    saved = save_prepared_project_ask_action(
        project,
        turn,
        store,
        title="Summarize derived text",
        draft={
            "action_id": "map.template",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {"template": {"text": "{{Markdown}}"}},
            "output_names": {},
        },
        catalog_payload=root_action_catalog_payload(),
        estimate_provider=_estimate,
        output_grants=(grant,),
        output_grant_allows=_allows_output_cell,
    )
    assert saved.event["payload"]["prepared_action"]["owned_output_inputs"] == [
        {
            "receipt_id": "receipt-markdown",
            "sheet_id": sheet_id,
            "row_id": row_id,
            "column_id": markdown_column,
        }
    ]

    current_grants: tuple[_OutputGrant, ...] = (grant,)
    admitted: list[dict[str, object]] = []
    service = ProjectAskActionService(
        catalog_payload_provider=root_action_catalog_payload,
        estimate_provider=_estimate,
        research_for_turn=lambda turn_id: {"id": "research-1", "turn_id": turn_id},
        authorize_dispatch=_approval,
        admit_operation=lambda research_id, **kwargs: admitted.append(
            {"research_id": research_id, **kwargs}
        ),
        run_action=lambda _project_id, _body: {"status": "queued"},
        output_grants_provider=lambda: current_grants,
        output_grant_allows=_allows_output_cell,
    )
    assert service.launch(project, store, saved.reference, project_id="project") == {
        "status": "queued"
    }
    assert len(admitted) == 1

    current_grants = ()
    with pytest.raises(ValueError, match="outside the Ask source scope"):
        service.launch(project, store, saved.reference, project_id="project")
    assert len(admitted) == 1


def test_prepared_ask_action_refuses_catalog_drift_before_budget_admission(
    make_project,
):
    project = make_project("project")
    store, turn, sheet_id = _actions_turn(project)
    saved = save_prepared_project_ask_action(
        project,
        turn,
        store,
        title="Greet people",
        draft=_draft(sheet_id),
        catalog_payload=root_action_catalog_payload(),
        estimate_provider=_estimate,
    )
    service = ProjectAskActionService(
        catalog_payload_provider=lambda: {"actions": []},
        estimate_provider=_estimate,
        research_for_turn=lambda turn_id: {"id": "research-1", "turn_id": turn_id},
        authorize_dispatch=lambda _authority: pytest.fail("must not authorize"),
        admit_operation=lambda *_args, **_kwargs: pytest.fail("must not admit"),
        run_action=lambda *_args, **_kwargs: pytest.fail("must not run"),
    )
    with pytest.raises(ValueError, match="not available"):
        service.launch(
            project,
            store,
            saved.reference,
            project_id="project",
        )


def test_launch_pauses_when_only_the_saved_estimate_becomes_unpriceable(make_project):
    project = make_project("project")
    store, turn, sheet_id = _actions_turn(project)
    estimated = True

    def estimate_or_unknown(action: dict[str, object]) -> dict[str, object]:
        if estimated:
            return _estimate(action)
        return {
            "estimate": {
                "billed_cost": None,
                "policy_id": "normal-policy",
                "requires_confirmation": False,
            }
        }

    saved = save_prepared_project_ask_action(
        project,
        turn,
        store,
        title="Greet people",
        draft=_draft(sheet_id),
        catalog_payload=root_action_catalog_payload(),
        estimate_provider=estimate_or_unknown,
    )
    estimated = False
    service = ProjectAskActionService(
        catalog_payload_provider=root_action_catalog_payload,
        estimate_provider=estimate_or_unknown,
        research_for_turn=lambda turn_id: {"id": "research-1", "turn_id": turn_id},
        authorize_dispatch=lambda _authority: pytest.fail("must not authorize"),
        admit_operation=lambda *_args, **_kwargs: pytest.fail("must not admit"),
        run_action=lambda *_args: pytest.fail("must not run"),
    )

    with pytest.raises(ProjectAskActionUnpriced) as raised:
        service.launch(project, store, saved.reference, project_id="project")
    error = raised.value
    assert error.dispatch_id == saved.reference.dispatch_id
    assert error.action_id == "map.template"
    assert error.prepared_turn_id == saved.reference.turn_id
    assert error.prepared_event_seq == saved.reference.event_seq
    assert error.payload_identity == saved.payload_identity
    assert error.action["idempotency_key"] == saved.reference.dispatch_id
    assert error.reason == "normal action estimate has unknown cost"


def test_launch_uses_the_saved_normal_estimate_not_a_caller_price(make_project):
    project = make_project("project")
    store, turn, sheet_id = _actions_turn(project)
    saved = save_prepared_project_ask_action(
        project,
        turn,
        store,
        title="Greet people",
        draft=_draft(sheet_id),
        catalog_payload=root_action_catalog_payload(),
        estimate_provider=_estimate,
    )
    admitted: list[dict[str, object]] = []
    service = ProjectAskActionService(
        catalog_payload_provider=root_action_catalog_payload,
        estimate_provider=_estimate,
        research_for_turn=lambda turn_id: {"id": "research-1", "turn_id": turn_id},
        authorize_dispatch=_approval,
        admit_operation=lambda research_id, **kwargs: admitted.append(
            {"research_id": research_id, **kwargs}
        ),
        run_action=lambda *_args: {"status": "queued"},
    )

    service.launch(
        project,
        store,
        saved.reference,
        project_id="project",
        confirmation_hash="confirmation-hash",
    )
    assert admitted[0]["estimate_micros"] == 17
    with pytest.raises(TypeError):
        service.launch(
            project,
            store,
            saved.reference,
            project_id="project",
            confirmation_hash="confirmation-hash",
            estimate_micros=0,
        )


def test_launch_requires_its_research_turn_before_repreparing(make_project):
    project = make_project("project")
    store, turn, sheet_id = _actions_turn(project)
    saved = save_prepared_project_ask_action(
        project,
        turn,
        store,
        title="Greet people",
        draft=_draft(sheet_id),
        catalog_payload=root_action_catalog_payload(),
        estimate_provider=_estimate,
    )
    service = ProjectAskActionService(
        catalog_payload_provider=lambda: pytest.fail("must not read catalog"),
        estimate_provider=lambda _action: pytest.fail("must not quote"),
        research_for_turn=lambda turn_id: {"id": "other", "turn_id": "other-turn"},
        authorize_dispatch=lambda _authority: pytest.fail("must not authorize"),
        admit_operation=lambda *_args, **_kwargs: pytest.fail("must not admit"),
        run_action=lambda *_args: pytest.fail("must not run"),
    )
    with pytest.raises(ValueError, match="research turn"):
        service.launch(project, store, saved.reference, project_id="project")


def test_unquoted_proposals_remain_visible_but_cannot_launch(make_project):
    project = make_project("project")
    store, turn, sheet_id = _actions_turn(project)
    saved = save_prepared_project_ask_action(
        project,
        turn,
        store,
        title="Greet people",
        draft=_draft(sheet_id),
        catalog_payload=root_action_catalog_payload(),
    )
    assert saved.event["payload"]["proposal"] == saved.proposal
    service = ProjectAskActionService(
        catalog_payload_provider=root_action_catalog_payload,
        estimate_provider=_estimate,
        research_for_turn=lambda turn_id: {"id": "research-1", "turn_id": turn_id},
        authorize_dispatch=lambda _authority: pytest.fail("must not authorize"),
        admit_operation=lambda *_args, **_kwargs: pytest.fail("must not admit"),
        run_action=lambda *_args: pytest.fail("must not run"),
    )
    with pytest.raises(ValueError, match="changed; reprepare"):
        service.launch(project, store, saved.reference, project_id="project")


def test_current_exact_approval_can_dispatch_a_non_map_action(make_project):
    project = make_project("project")
    store, turn, sheet_id = _actions_turn(project)
    saved = save_prepared_project_ask_action(
        project,
        turn,
        store,
        title="Match people",
        draft={
            "action_id": "join.semantic",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {
                "source": "Name",
                "target": {"sheet_id": sheet_id, "column": "Name"},
            },
            "output_names": {},
            "sheet_name": "Matches",
        },
        catalog_payload=root_action_catalog_payload(),
        estimate_provider=_estimate,
    )
    approved: list[dict[str, object]] = []
    service = ProjectAskActionService(
        catalog_payload_provider=root_action_catalog_payload,
        estimate_provider=_estimate,
        research_for_turn=lambda turn_id: {"id": "research-1", "turn_id": turn_id},
        authorize_dispatch=lambda authority: (
            approved.append(dict(authority)) or _approval(authority)
        ),
        admit_operation=lambda *_args, **_kwargs: None,
        run_action=lambda _project_id, body: {"action_id": body["action_id"]},
    )
    assert service.launch(
        project,
        store,
        saved.reference,
        project_id="project",
        confirmation_hash="confirmation-hash",
    ) == {"action_id": "join.semantic"}
    assert approved[0]["automatic_eligible"] is False
    assert (
        approved[0]["catalog_effects"]
        == saved.event["payload"]["prepared_action"]["catalog_effects"]
    )


# --- action host ---


class _Router:
    local_endpoints = {"local": object()}


class _Workspace:
    executor_deps_factory = object()

    def __init__(self) -> None:
        self.project = object()
        self.router = _Router()
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def get(self, project_id: str) -> object:
        self.calls.append(("get", (project_id,)))
        return self.project

    def router_for(self, project: object) -> _Router:
        self.calls.append(("router_for", (project,)))
        return self.router

    def edition_execution_composition_context_for(self, context: object) -> str:
        self.calls.append(("edition_context", (context,)))
        return f"edition:{context}"

    def execution_composition_for(
        self, project: object, router: _Router, context: str
    ) -> object:
        self.calls.append(("composition", (project, router, context)))
        return {"context": context}

    def organization_provider_keys(self) -> dict[str, str]:
        self.calls.append(("organization_provider_keys", ()))
        return {"openai": "configured"}


def test_catalog_uses_the_live_project_composition_and_sidecar_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _Workspace()
    observed: dict[str, Any] = {}

    def catalog(project: object, **kwargs: Any) -> dict[str, Any]:
        observed["project"] = project
        observed.update(kwargs)
        return {"actions": [{"kind": "plugin.example"}]}

    monkeypatch.setattr(
        "frisket.server.services.project_qa_action_host."
        "project_action_catalog_payload_with_launcher_hints",
        catalog,
    )
    host = ProjectAskActionHost(
        cast(Any, workspace),
        "project-1",
        context_provider=lambda: "admission-1",
        sidecar_capabilities_provider=lambda: {"engines": [{"name": "gliner"}]},
    )

    assert host.catalog() == {"actions": [{"kind": "plugin.example"}]}
    assert observed == {
        "project": workspace.project,
        "sidecar_capabilities": {"engines": [{"name": "gliner"}]},
        "execution_composition": {"context": "edition:admission-1"},
        "organization_provider_keys": {"openai": "configured"},
        "has_local_model_endpoint": True,
        "effective_router": workspace.router,
        "connected_account_resolver_configured": True,
    }


def test_estimate_and_run_refresh_the_trusted_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _Workspace()
    contexts = iter(["quote-context", "run-context"])
    observed: list[tuple[str, str, dict[str, Any], object]] = []

    def estimate(
        _self: object,
        project_id: str,
        action: dict[str, Any],
        *,
        request_context: object,
    ) -> dict[str, Any]:
        observed.append(("quote", project_id, action, request_context))
        return {"estimate": {"billed_cost": 0}}

    def run_action(
        _self: object, project_id: str, body: dict[str, Any], *, request_context: object
    ) -> dict[str, Any]:
        observed.append(("run", project_id, body, request_context))
        return {"status": "queued"}

    monkeypatch.setattr(
        "frisket.server.services.project_qa_action_host.ActionPreviewService.estimate",
        estimate,
    )
    monkeypatch.setattr(
        "frisket.server.services.project_qa_action_host.ActionRunService.run_action",
        run_action,
    )
    host = ProjectAskActionHost(
        cast(Any, workspace),
        "project-1",
        context_provider=lambda: next(contexts),
        sidecar_capabilities_provider=lambda: pytest.fail("catalog was not requested"),
    )

    assert host.estimate({"action_id": "map.template"}) == {
        "estimate": {"billed_cost": 0}
    }
    assert host.run("project-1", {"action_id": "map.template"}) == {"status": "queued"}
    assert observed == [
        ("quote", "project-1", {"action_id": "map.template"}, "quote-context"),
        ("run", "project-1", {"action_id": "map.template"}, "run-context"),
    ]


def test_run_never_redirects_a_bound_host_to_another_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _Workspace()
    monkeypatch.setattr(
        "frisket.server.services.project_qa_action_host.ActionRunService.run_action",
        lambda *_args, **_kwargs: pytest.fail("must not run another project"),
    )
    host = ProjectAskActionHost(
        cast(Any, workspace),
        "project-1",
        context_provider=lambda: pytest.fail("must not read context"),
        sidecar_capabilities_provider=lambda: pytest.fail("catalog was not requested"),
    )

    with pytest.raises(ValueError, match="different project"):
        host.run("project-2", {"action_id": "map.template"})


# --- plugin execution ---

PLUGIN_ID = "demo.receipt_stamp"
ACTION_ID = "demo.receipt_stamp.stamp"
TRUSTED_LOCAL_BACKEND_CAPABILITY = "plugin:trusted_local_backend"
PLUGIN_ROOT = (
    Path(__file__).parents[1] / "fixtures" / "local_plugins" / "demo_receipt_stamp"
)


@pytest.fixture()
def _reset_plugin_registry() -> None:
    _reset_default_registry_for_tests()
    yield
    _reset_default_registry_for_tests()


def _install_and_activate(service: WorkbenchService, project_id: str) -> None:
    status, installed = service.install_local(
        project_id,
        plugin_id=PLUGIN_ID,
        source={"kind": "localPath", "value": str(PLUGIN_ROOT)},
        arbitrary_package_load_allowed=False,
    )
    assert status == 200, installed
    service.activate_manifest(
        project_id,
        plugin_id=PLUGIN_ID,
        receipt_id=str(installed["receiptId"]),
        trust_acknowledged=True,
        permissions_accepted=[TRUSTED_LOCAL_BACKEND_CAPABILITY],
        arbitrary_package_load_allowed=False,
    )
    backend = service.activate_backend(
        project_id,
        plugin_id=PLUGIN_ID,
        trust_acknowledged=True,
        arbitrary_package_load_allowed=False,
        executable_handlers_allowed=True,
    )
    assert backend["registeredRuntimeBindings"]["actions"] == [ACTION_ID]


@pytest.mark.asyncio
async def test_research_executes_installed_plugin_and_grants_its_receipt(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = "plugin-research"
    workspace.create("Plugin research", project_id=project_id)
    _install_and_activate(WorkbenchService(workspace), project_id)
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("People")
    name_column = project.add_column(sheet_id, "Name")
    [row_id] = project.add_rows(sheet_id, [{"Name": "Ada"}], {"Name": name_column})
    ask_store = ProjectQAStore(project)
    thread = ask_store.create_thread(title="Plugin research")
    turn = ask_store.submit_turn(
        thread["id"],
        request_id="plugin-research",
        question="Stamp the selected name.",
        scope={"kind": "project"},
    )
    ledger = ProjectQAResearchStore(project)
    research = ledger.create(
        turn_id=turn["id"],
        actor="researcher",
        budget_micros=1_000_000,
        currency="USD",
        write_mode="full_access",
        max_turns=None,
    )
    session = ResearchSession(
        ledger, research["id"], authorize=lambda: None, model_servers=()
    )
    host = ProjectAskActionHost(
        workspace,
        project_id,
        context_provider=lambda: None,
        sidecar_capabilities_provider=lambda: {},
    )
    child_runs = ProjectQAChildRunService(workspace, poll_interval_seconds=0.001)
    execution = ProjectQAExecutionService(
        project,
        project_id,
        turn,
        ask_store,
        session,
        catalog_payload_provider=host.catalog,
        estimate_provider=host.estimate,
        run_action=host.run,
        wait_child=child_runs.wait,
        rate_actual_cost=usd_to_micros,
    )
    with closing(project):
        prepared = await execution.prepare_action(
            "Stamp Ada",
            {
                "action_id": ACTION_ID,
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet_id,
                    "row_ids": [row_id],
                },
                "params": {"name": "Name"},
                "output_names": {"receipt_stamp": "Plugin receipt stamp"},
            },
        )
        assert prepared["automatic_eligible"] is True
        assert prepared["preparation_reason"] is None

        result = await execution.execute_action(prepared["event_ref"])

        assert result["status"] == "completed"
        assert result["errors"] == []
        assert result["receipt_id"]
        receipt = ActionRunService(workspace).receipt_lookup(
            project_id, result["receipt_id"]
        )
        assert receipt["action_kind"] == ACTION_ID
        output = next(
            item
            for item in receipt["outputs"]
            if item["name"] == "Plugin receipt stamp"
        )
        output_column_id = int(output["ref"]["column_id"])
        assert project.get_values(sheet_id, output_column_id) == {
            row_id: f"Ada|{PLUGIN_ID}|{PLUGIN_ID}:stamp"
        }
        assert ledger.get(research["id"])["output_grants"] == [result["receipt_id"]]


# --- skill runtime ---


def _skill_runtime_turn(make_project) -> tuple[Project, ProjectQAStore, dict[str, Any]]:
    project = make_project("Skills")
    sheet_id = project.add_sheet("Records")
    column_id = project.add_column(sheet_id, "Name")
    project.add_rows(sheet_id, [{"Name": "Selected"}], {"Name": column_id})
    store = ProjectQAStore(project)
    scope = {
        "kind": "sources",
        "sources": [{"kind": "rows", "sheet_id": sheet_id, "row_ids": [1]}],
    }
    thread = store.create_thread(title="Ask", scope=scope)
    turn = store.submit_turn(
        thread["id"],
        request_id="skill-turn",
        question="Use the single-row-review skill on this selection.",
        scope=scope,
        model="anthropic/test",
    )
    return project, store, turn


def _output_tool(request: LLMRequest) -> str:
    return next(
        tool["name"]
        for tool in request.tools or []
        if {"text", "citation_ids"} <= set(tool["parameters"].get("properties", {}))
    )


def _system_text(request: LLMRequest) -> str:
    return "\n".join(
        str(message["content"])
        for message in request.messages
        if message["role"] == "system"
    )


def _router(adapter: Any) -> ModelRouter:
    router = ModelRouter(
        model_call_policy=LOCAL_PURCHASE_POLICY,
        keys={"anthropic": "test-key"},
        cache=None,
        cache_mode="off",
        use_env_keys=False,
    )
    router._adapters["anthropic"] = adapter
    return router


def test_ordinary_turn_loads_admitted_skill_without_authoring_or_action_power(
    make_project,
) -> None:
    marker = "Keep the operation on exactly the selected row."

    class Provider:
        def __init__(self) -> None:
            self.requests: list[LLMRequest] = []

        async def complete(self, request: LLMRequest, client: Any) -> LLMResponse:
            del client
            self.requests.append(request)
            observations = [
                json.loads(message["content"])
                for message in request.messages
                if message["role"] == "tool"
            ]
            if not observations:
                names = {tool["name"] for tool in request.tools or []}
                assert "load_capability" in names
                assert {"list_skills", "read_skill", "propose_skill"}.isdisjoint(names)
                assert {"prepare_action", "execute_action"}.isdisjoint(names)
                name, args = "load_capability", {"id": "single-row-review"}
            else:
                assert marker in str(observations[-1])
                name, args = (
                    _output_tool(request),
                    {
                        "text": "The selected row was reviewed without expanding scope.",
                        "citation_ids": [],
                    },
                )
            return LLMResponse(
                content=None,
                data=None,
                tokens_in=10,
                tokens_out=3,
                cost=0.01,
                model=request.model,
                tool_calls=[
                    {"name": name, "args": args, "id": f"skill-{len(self.requests)}"}
                ],
            )

    project, store, turn = _skill_runtime_turn(make_project)
    provider = Provider()
    answer = asyncio.run(
        run_turn(
            project,
            _router(provider),
            turn,
            store,
            skill_snapshots=[
                {
                    "name": "single-row-review",
                    "revision": 4,
                    "content": (
                        "---\n"
                        "name: single-row-review\n"
                        "description: Review exactly the selected rows.\n"
                        "---\n\n"
                        f"{marker}\n"
                    ),
                }
            ],
        )
    )
    assert answer["text"].startswith("The selected row")
    instructions = _system_text(provider.requests[0])
    assert "single selected row" in instructions
    assert "observed stable row IDs" in instructions
    assert "omitting row_ids from sheet_rows selects the whole sheet" in instructions
    assert "does not add tools, action permissions" in instructions


@pytest.mark.parametrize("stale_proposal", [False, True])
def test_manager_bound_adapter_exposes_review_only_authoring_tools(
    make_project,
    stale_proposal: bool,
) -> None:
    content = (
        "---\n"
        "name: scoped-review\n"
        "description: Review the current selection.\n"
        "---\n\n"
        "Inspect the selected rows and use analytics for totals.\n"
    )

    class Authoring:
        def __init__(self) -> None:
            self.proposals: list[tuple[str, str | None, int | None]] = []

        async def list_skills(self) -> dict[str, Any]:
            return {"skills": []}

        async def read_skill(self, skill_id: str) -> dict[str, Any]:
            return {"id": skill_id, "content": content, "revision": 2}

        async def propose_skill(
            self,
            proposed: str,
            target_skill_id: str | None = None,
            expected_revision: int | None = None,
        ) -> dict[str, Any]:
            self.proposals.append((proposed, target_skill_id, expected_revision))
            if stale_proposal and len(self.proposals) == 1:
                raise ProjectQAConflictError("This skill changed. Read it again.")
            return {
                "proposal_id": "proposal-1",
                "name": "scoped-review",
                "description": "Review the current selection.",
            }

    class Provider:
        def __init__(self) -> None:
            self.requests: list[LLMRequest] = []

        async def complete(self, request: LLMRequest, client: Any) -> LLMResponse:
            del client
            self.requests.append(request)
            observations = [
                message["content"]
                for message in request.messages
                if message["role"] == "tool"
            ]
            if observations and "This skill changed" in observations[-1]:
                name, args = "propose_skill", {"content": content}
            elif not observations:
                names = {tool["name"] for tool in request.tools or []}
                assert {"list_skills", "read_skill", "propose_skill"} <= names
                name, args = "propose_skill", {"content": content}
            else:
                assert json.loads(observations[-1])["proposal_id"] == "proposal-1"
                name, args = (
                    _output_tool(request),
                    {
                        "text": "I prepared scoped-review for you to review.",
                        "citation_ids": [],
                    },
                )
            return LLMResponse(
                content=None,
                data=None,
                tokens_in=10,
                tokens_out=3,
                cost=0.01,
                model=request.model,
                tool_calls=[
                    {"name": name, "args": args, "id": f"author-{len(self.requests)}"}
                ],
            )

    project, store, turn = _skill_runtime_turn(make_project)
    turn["question"] = "Save this workflow as a skill."
    authoring = Authoring()
    provider = Provider()
    answer = asyncio.run(
        run_turn(
            project,
            _router(provider),
            turn,
            store,
            skill_authoring=authoring,
        )
    )
    assert answer["text"] == "I prepared scoped-review for you to review."
    assert authoring.proposals == [(content, None, None)] * (2 if stale_proposal else 1)
    instructions = _system_text(provider.requests[0])
    assert "creates a reviewable proposal; it does not save" in instructions
    assert (
        "---\nname: lowercase-hyphen-name\n"
        "description: short description\n---\n..." in instructions
    )
    assert "Frontmatter permits only name and description" in instructions
    assert "name is at most 64 characters" in instructions
    assert "description is 1 to 200 characters" in instructions
    assert "preserve the selected row/view scope" in instructions
    assert "chosen action's returned scope contract" in instructions
    assert "without promising a preview operation" in instructions
    assert "cannot authorize a skill proposal" in instructions


def test_authoring_request_without_management_tools_gets_draft_only_guidance(
    make_project,
) -> None:
    class Provider:
        def __init__(self) -> None:
            self.requests: list[LLMRequest] = []

        async def complete(self, request: LLMRequest, client: Any) -> LLMResponse:
            del client
            self.requests.append(request)
            names = {tool["name"] for tool in request.tools or []}
            assert {"list_skills", "read_skill", "propose_skill"}.isdisjoint(names)
            guidance = _system_text(request)
            assert "may draft instructions as ordinary response text" in guidance
            assert "library cannot be changed from this turn" in guidance
            assert "server has not configured chat authoring" in guidance
            return LLMResponse(
                content=None,
                data=None,
                tokens_in=10,
                tokens_out=3,
                cost=0.01,
                model=request.model,
                tool_calls=[
                    {
                        "name": _output_tool(request),
                        "args": {
                            "text": (
                                "I can draft the instructions here, but this turn "
                                "cannot save them to the skill library."
                            ),
                            "citation_ids": [],
                        },
                        "id": "draft-only-answer",
                    }
                ],
            )

    project, store, turn = _skill_runtime_turn(make_project)
    turn["question"] = "Save this workflow as a skill."
    provider = Provider()
    answer = asyncio.run(run_turn(project, _router(provider), turn, store))
    assert answer["text"] == (
        "I can draft the instructions here, but this turn cannot save them to "
        "the skill library."
    )
    assert len(provider.requests) == 1


def test_followup_history_keeps_skill_references_without_replaying_body(
    make_project,
    monkeypatch: Any,
) -> None:
    hidden_body = "DO_NOT_REPLAY_THE_SKILL_BODY" * 2_000

    class Provider:
        def __init__(self) -> None:
            self.requests: list[LLMRequest] = []

        async def complete(self, request: LLMRequest, client: Any) -> LLMResponse:
            del client
            self.requests.append(request)
            return LLMResponse(
                content=None,
                data=None,
                tokens_in=10,
                tokens_out=3,
                cost=0.01,
                model=request.model,
                tool_calls=[
                    {
                        "name": _output_tool(request),
                        "args": {
                            "text": "The saved skill is available.",
                            "citation_ids": [],
                        },
                        "id": "history-answer",
                    }
                ],
            )

    project, store, turn = _skill_runtime_turn(make_project)
    provider = Provider()

    def recent_events(thread_id: str, *, limit: int) -> dict[str, Any]:
        assert thread_id == turn["thread_id"]
        assert limit == 100
        return {
            "events": [
                {
                    "turn_id": "earlier-turn",
                    "kind": "skill_proposal",
                    "payload": {
                        "proposal_id": "proposal-9",
                        "target_skill_id": "skill-4",
                        "base_revision": 3,
                        "name": "single-row-review",
                        "description": "Review exactly the selected rows.",
                        "content": hidden_body,
                    },
                },
                {
                    "turn_id": "earlier-turn",
                    "kind": "skill_saved",
                    "payload": {
                        "attempt_id": "attempt-2",
                        "proposal_id": "proposal-9",
                        "skill_id": "skill-4",
                        "name": "single-row-review",
                        "revision": 4,
                        "status": "saved",
                        "content": hidden_body,
                    },
                },
            ]
        }

    monkeypatch.setattr(store, "recent_events", recent_events)
    asyncio.run(run_turn(project, _router(provider), turn, store))
    history = "\n".join(
        str(message["content"])
        for message in provider.requests[0].messages
        if message["role"] == "user"
    )
    assert "proposal-9" in history
    assert '"revision": 4' in history
    assert '"status": "saved"' in history
    assert hidden_body not in history
    assert "DO_NOT_REPLAY_THE_SKILL_BODY" not in history


# --- skill adapter ---


def _content(name: str, instruction: str = "Inspect representative sources.") -> str:
    return (
        f"---\nname: {name}\ndescription: Check representative sources.\n---\n\n"
        f"# Source checks\n\n{instruction}\n"
    )


def test_bound_authoring_rechecks_management_and_keeps_disabled_update_disabled(
    tmp_path,
) -> None:
    async def scenario() -> None:
        project = Project.create(tmp_path / "ask.frisket", name="Ask")
        library = SkillLibrary(tmp_path / "library")
        installed = library.create(_content("source-check"), enabled=False)
        store = ProjectQAStore(project)
        thread = store.create_thread(title="Ask")
        turn = store.submit_turn(thread["id"], request_id="draft", question="Revise it")
        allowed = True
        checks = 0

        def authorize(_project_id, _context) -> None:
            nonlocal checks
            checks += 1
            if not allowed:
                raise PermissionError("Skill manager access is required.")

        adapter = ProjectQASkillAdapter(
            library=lambda: library, authorize=authorize, scope="workspace"
        )
        bound = adapter.bind(
            store=store,
            project_id="project-1",
            thread_id=thread["id"],
            turn_id=turn["id"],
            context=object(),
        )
        listed = await bound.list_skills()
        assert listed["skills"] == [
            {
                "id": installed["id"],
                "name": installed["name"],
                "description": installed["description"],
                "enabled": False,
                "revision": 1,
            }
        ]
        assert "content" not in listed["skills"][0]
        assert (await bound.read_skill(installed["id"]))["content"] == installed[
            "content"
        ]

        revised = _content("source-check", "Compare every cited source.")
        proposal = await bound.propose_skill(
            revised, target_skill_id=installed["id"], expected_revision=1
        )
        store.finish_turn(turn["id"], status="completed")
        result = await adapter.save(
            store=store,
            project_id="project-1",
            thread_id=thread["id"],
            turn_id=turn["id"],
            context=object(),
            proposal_id=proposal["proposal_id"],
            request_id="save-1",
            content=revised,
            expected_revision=1,
        )
        assert result["status"] == "saved"
        assert result["skill"]["revision"] == 2
        assert result["skill"]["enabled"] is False

        allowed = False
        with pytest.raises(PermissionError, match="manager"):
            await bound.read_skill(installed["id"])
        assert checks >= 6
        project.close()

    asyncio.run(scenario())


def test_solo_http_save_projects_current_authority_and_typed_conflict(client) -> None:
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")

    with client:
        pid = client.post("/api/projects", json={"name": "Ask"}).json()["id"]
        thread = client.post(
            f"/api/projects/{pid}/qa/threads",
            json={"scope": {"kind": "project"}},
        ).json()
        detail_path = f"/api/projects/{pid}/qa/threads/{thread['id']}"
        detail = client.get(detail_path)
        assert detail.status_code == 200, detail.text
        assert detail.json()["can_manage_skills"] is True
        assert detail.json()["skill_scope"] == "workspace"

        store = ProjectQAStore(client.app.state.workspace.get(pid))
        turn = store.submit_turn(
            thread["id"], request_id="draft", question="Create the skill"
        )
        content = _content("source-check")
        proposal_id = "proposal-http"
        store.append_skill_proposal(
            turn["id"],
            payload={
                "proposal_id": proposal_id,
                "target_skill_id": None,
                "base_revision": None,
                "name": "source-check",
                "description": "Check representative sources.",
                "content": content,
                "enabled": True,
                "scope": "workspace",
            },
        )
        store.finish_turn(turn["id"], status="completed")
        path = detail_path + f"/turns/{turn['id']}/skill-proposals/{proposal_id}/save"
        saved = client.post(
            path,
            json={
                "request_id": "save-http",
                "content": content,
                "expected_revision": None,
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["status"] == "saved"
        assert saved.json()["skill"]["name"] == "source-check"

        edited_content = _content(
            "source-check", "Use the edited instruction without leaving this dialog."
        )
        edited = client.post(
            path,
            json={
                "request_id": "save-edit",
                "content": edited_content,
                "expected_revision": 1,
            },
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["skill"]["id"] == saved.json()["skill"]["id"]
        assert edited.json()["skill"]["revision"] == 2
        installed = client.get("/api/skills").json()["skills"]
        assert len(installed) == 1
        assert installed[0]["content"] == edited_content

        collision = client.post(
            path,
            json={
                "request_id": "save-http",
                "content": _content(
                    "source-check", "Use a changed instruction on the same request."
                ),
                "expected_revision": None,
            },
        )
        assert collision.status_code == 409
        assert collision.json()["detail"]["code"] == "request_conflict"

        saved_events = [
            event
            for event in client.get(detail_path + "/events").json()["events"]
            if event["kind"] == "skill_saved"
        ]
        assert [event["payload"]["status"] for event in saved_events] == [
            "changed",
            "saved",
        ]
        assert [event["payload"]["current_revision"] for event in saved_events] == [
            2,
            2,
        ]
        assert all("content" not in event["payload"] for event in saved_events)

        skill_id = edited.json()["skill"]["id"]
        disabled = client.patch(
            f"/api/skills/{skill_id}/enabled",
            json={"expectedRevision": 2, "enabled": False},
        )
        assert disabled.status_code == 200, disabled.text
        changed_events = [
            event
            for event in client.get(detail_path + "/events").json()["events"]
            if event["kind"] == "skill_saved"
        ]
        assert all(event["payload"]["status"] == "changed" for event in changed_events)
        assert all(
            event["payload"]["current_revision"] == 3 for event in changed_events
        )

        deleted = client.request(
            "DELETE",
            f"/api/skills/{skill_id}",
            json={"expectedRevision": 3},
        )
        assert deleted.status_code == 204, deleted.text
        deleted_events = [
            event
            for event in client.get(detail_path + "/events").json()["events"]
            if event["kind"] == "skill_saved"
        ]
        assert all(event["payload"]["status"] == "deleted" for event in deleted_events)
        assert all(
            event["payload"]["current_revision"] is None for event in deleted_events
        )


@pytest.mark.parametrize("settings_conflict", [False, True])
def test_save_conflict_rebase_retry_and_explicit_selection(
    tmp_path, monkeypatch, settings_conflict
) -> None:
    async def scenario() -> None:
        project = Project.create(tmp_path / "ask.frisket", name="Ask")
        library = SkillLibrary(tmp_path / "library")
        store = ProjectQAStore(project)
        research = {
            "write_mode": "ask_overwrite",
            "budget_usd": None,
            "max_turns": None,
            "skills": ["existing"],
        }
        thread = store.create_thread(title="Ask", research=research)
        turn = store.submit_turn(
            thread["id"], request_id="draft", question="Create it", research=research
        )
        adapter = ProjectQASkillAdapter(
            library=lambda: library,
            authorize=lambda _project_id, _context: None,
            scope="workspace",
        )
        bound = adapter.bind(
            store=store,
            project_id="project-1",
            thread_id=thread["id"],
            turn_id=turn["id"],
            context=object(),
        )
        content = _content("source-check")
        proposal = await bound.propose_skill(content)
        store.finish_turn(turn["id"], status="completed")

        if settings_conflict:
            original_update = store.update_thread

            def concurrent_settings_change(*args, **kwargs):
                monkeypatch.setattr(store, "update_thread", original_update)
                raise ProjectQAConflictError("Thread changed")

            monkeypatch.setattr(store, "update_thread", concurrent_settings_change)

        saved = await adapter.save(
            store=store,
            project_id="project-1",
            thread_id=thread["id"],
            turn_id=turn["id"],
            context=object(),
            proposal_id=proposal["proposal_id"],
            request_id="save-create",
            content=content,
            expected_revision=None,
        )
        assert saved["skill"]["revision"] == 1
        assert store.latest_skill_saved(turn["id"], proposal["proposal_id"]) is not None
        assert store.get_thread(thread["id"])["research"]["skills"] == (
            ["existing"] if settings_conflict else ["existing", "source-check"]
        )
        assert (
            await adapter.save(
                store=store,
                project_id="project-1",
                thread_id=thread["id"],
                turn_id=turn["id"],
                context=object(),
                proposal_id=proposal["proposal_id"],
                request_id="save-create",
                content=content,
                expected_revision=None,
            )
        )["skill"]["id"] == saved["skill"]["id"]

        update_turn = store.submit_turn(
            thread["id"],
            request_id="update",
            question="Update it",
            research=store.get_thread(thread["id"])["research"],
        )
        update = adapter.bind(
            store=store,
            project_id="project-1",
            thread_id=thread["id"],
            turn_id=update_turn["id"],
            context=object(),
        )
        revised = _content("source-check", "Read the newest source first.")
        update_proposal = await update.propose_skill(
            revised,
            target_skill_id=saved["skill"]["id"],
            expected_revision=1,
        )
        store.finish_turn(update_turn["id"], status="completed")
        library.set_enabled(saved["skill"]["id"], expected_revision=1, enabled=False)
        with pytest.raises(ProjectQASkillConflict) as stale:
            await adapter.save(
                store=store,
                project_id="project-1",
                thread_id=thread["id"],
                turn_id=update_turn["id"],
                context=object(),
                proposal_id=update_proposal["proposal_id"],
                request_id="save-stale",
                content=revised,
                expected_revision=1,
            )
        assert stale.value.detail()["current_revision"] == 2

        rebased = await adapter.save(
            store=store,
            project_id="project-1",
            thread_id=thread["id"],
            turn_id=update_turn["id"],
            context=object(),
            proposal_id=update_proposal["proposal_id"],
            request_id="save-rebased",
            content=revised,
            expected_revision=2,
        )
        assert rebased["skill"]["revision"] == 3
        assert rebased["skill"]["enabled"] is False

        renamed = revised.replace("name: source-check", "name: renamed")
        with pytest.raises(SkillLibraryError, match="keep the installed skill name"):
            await adapter.save(
                store=store,
                project_id="project-1",
                thread_id=thread["id"],
                turn_id=update_turn["id"],
                context=object(),
                proposal_id=update_proposal["proposal_id"],
                request_id="save-renamed",
                content=renamed,
                expected_revision=3,
            )
        project.close()

    asyncio.run(scenario())


def test_save_retry_reconciles_library_write_after_lost_result(tmp_path) -> None:
    async def scenario() -> None:
        project = Project.create(tmp_path / "ask.frisket", name="Ask")
        library = SkillLibrary(tmp_path / "library")
        store = ProjectQAStore(project)
        thread = store.create_thread(title="Ask")
        turn = store.submit_turn(thread["id"], request_id="draft", question="Create it")
        adapter = ProjectQASkillAdapter(
            library=lambda: library,
            authorize=lambda _project_id, _context: None,
            scope="workspace",
        )
        bound = adapter.bind(
            store=store,
            project_id="project-1",
            thread_id=thread["id"],
            turn_id=turn["id"],
            context=object(),
        )
        content = _content("source-check")
        proposal = await bound.propose_skill(content)
        store.finish_turn(turn["id"], status="completed")
        store.record_skill_save_attempt(
            turn["id"],
            proposal_id=proposal["proposal_id"],
            request_id="lost-response",
            payload={
                "target_skill_id": None,
                "expected_revision": None,
                "content": content,
                "name": "source-check",
                "description": "Check representative sources.",
                "enabled": True,
            },
        )
        written = library.create(content, enabled=True)

        recovered = await adapter.save(
            store=store,
            project_id="project-1",
            thread_id=thread["id"],
            turn_id=turn["id"],
            context=object(),
            proposal_id=proposal["proposal_id"],
            request_id="lost-response",
            content=content,
            expected_revision=None,
        )
        assert recovered["status"] == "saved"
        assert recovered["skill"]["id"] == written["id"]
        assert len(library.list()["skills"]) == 1
        assert [
            event["kind"]
            for event in store.events(thread["id"])["events"]
            if event["kind"] in {"skill_save_attempt", "skill_saved"}
        ] == ["skill_save_attempt", "skill_saved"]
        project.close()

    asyncio.run(scenario())


def test_skill_snapshot_failure_terminalizes_durable_admission(tmp_path) -> None:
    async def scenario() -> None:
        workspace = Workspace(tmp_path / "workspace")
        pid = workspace.create("Ask")["id"]

        class BrokenLibrary:
            @staticmethod
            def enabled():
                raise SkillLibraryError("Stored skills cannot be read safely.")

        async def runner(*_args):
            raise AssertionError("runner must not start after snapshot failure")

        service = ProjectQAService(workspace, runner=runner)
        service.configure_research(
            authorize=lambda _project_id, _context: None,
            skills=lambda: BrokenLibrary(),
        )
        thread = await service.create(pid, AskThreadCreate(scope={"kind": "project"}))
        with pytest.raises(SkillLibraryError, match="cannot be read"):
            await service.submit(
                pid,
                thread["id"],
                AskTurnRequest(
                    request_id="snapshot-fails",
                    question="Use a skill",
                    scope={"kind": "project"},
                    model="anthropic/test",
                ),
            )
        store = ProjectQAStore(workspace.get(pid))
        [turn] = [
            event
            for event in store.events(thread["id"])["events"]
            if event["kind"] == "question"
        ]
        saved = store.get_turn(turn["turn_id"])
        assert saved["status"] == "failed"
        assert store.get_active_turn(thread["id"]) is None
        await service.shutdown()

    asyncio.run(scenario())


# --- skill authoring ---

SKILL = """---
name: check-selected-supplier
description: Read only the selected supplier record and report its amount.
---

Inspect the admitted rows. Do not broaden the scope or execute actions.
"""


def _finished(client: TestClient, path: str) -> dict:
    deadline = time.monotonic() + 10  # realtime: actual background Ask task
    while True:
        response = client.get(path + "/events")
        assert response.status_code == 200, response.text
        page = response.json()
        if page["active_turn"] is None:
            assert not any(
                e["kind"] == "status" and e["payload"].get("status") == "failed"
                for e in page["events"]
            ), page
            return page
        assert time.monotonic() < deadline, page  # realtime: detect a stalled task
        time.sleep(0.01)  # realtime: yield to actual worker threads


def test_skill_saved_after_answer_is_used_on_selected_row_in_same_chat(
    tmp_path, monkeypatch
):
    class Provider:
        calls = 0
        marker = "EDITOR-ONLY-REVISION-ONE"
        row_id = 0
        sheet_id = 0
        seen_markers: list[str] = []

        async def complete(self, request, client):
            self.calls += 1
            tools = {tool["name"] for tool in request.tools}
            observations = [
                str(message["content"])
                for message in request.messages
                if message.get("role") == "tool"
            ]
            if self.calls == 1:
                assert "propose_skill" in tools
                name, args = "propose_skill", {"content": SKILL}
            elif self.calls == 2:
                return self.answer(request, "The skill is ready to review.")
            else:
                stage = (self.calls - 3) % 3
                if stage == 0:
                    assert "load_capability" in tools
                    name, args = "load_capability", {"id": "check-selected-supplier"}
                elif stage == 1:
                    assert self.marker in "\n".join(observations), observations
                    self.seen_markers.append(self.marker)
                    name, args = "read_rows", {"sheet_id": self.sheet_id}
                else:
                    observed = json.loads(observations[-1])
                    assert [row["row_id"] for row in observed["rows"]] == [self.row_id]
                    assert "Beta" in observations[-1]
                    assert "Alpha" not in observations[-1]
                    return self.answer(request, f"{self.marker}: Beta has amount 900.")
            return LLMResponse(
                content=None,
                data=None,
                tokens_in=10,
                tokens_out=5,
                cost=0,
                model=request.model,
                tool_calls=[{"name": name, "args": args, "id": f"step-{self.calls}"}],
            )

        @staticmethod
        def answer(request, text):
            return LLMResponse(
                content=text,
                data=None,
                tokens_in=10,
                tokens_out=5,
                cost=0,
                model=request.model,
            )

    router = ModelRouter(
        model_call_policy=LOCAL_PURCHASE_POLICY,
        keys={"anthropic": "test-key"},
        cache=None,
        cache_mode="off",
        use_env_keys=False,
    )
    provider = Provider()
    router._adapters["anthropic"] = provider
    monkeypatch.setattr(Workspace, "router_for", lambda self, project: router)
    with make_client(tmp_path) as client:
        pid = client.post("/api/projects", json={"name": "Skill flow"}).json()["id"]
        project = client.app.state.workspace.get(pid)
        sheet_id = project.add_sheet("Suppliers")
        columns = {
            "supplier": project.add_column(sheet_id, "supplier", type="text"),
            "amount": project.add_column(sheet_id, "amount", type="number"),
        }
        rows = project.add_rows(
            sheet_id,
            [{"supplier": "Alpha", "amount": 10}, {"supplier": "Beta", "amount": 900}],
            columns,
        )
        provider.sheet_id = sheet_id
        provider.row_id = rows[-1]
        scope = {
            "kind": "sources",
            "sources": [{"kind": "rows", "sheet_id": sheet_id, "row_ids": [rows[-1]]}],
        }
        path = f"/api/projects/{pid}/qa/threads"
        thread = client.post(path, json={"scope": scope}).json()
        path += "/" + thread["id"]

        def submit(request_id, question):
            response = client.post(
                path + "/turns",
                json={
                    "request_id": request_id,
                    "question": question,
                    "scope": scope,
                    "model": "anthropic/test",
                },
            )
            assert response.status_code == 200, response.text
            return response.json()

        submit("create", "Create a reusable skill for checking this selected supplier.")
        page = _finished(client, path)
        proposal = next(e for e in page["events"] if e["kind"] == "skill_proposal")
        edited = SKILL + f"\nInclude {provider.marker} in your answer.\n"
        save_path = (
            f"{path}/turns/{proposal['turn_id']}/skill-proposals/"
            f"{proposal['payload']['proposal_id']}/save"
        )
        saved = client.post(
            save_path,
            json={
                "request_id": "save-one",
                "content": edited,
                "expected_revision": None,
            },
        )
        assert saved.status_code == 200, saved.text
        skill = saved.json()["skill"]
        assert skill["content"] == edited
        assert skill["revision"] == 1
        assert provider.calls == 2  # Save itself never starts model/action work.
        submit("use-one", "Use the skill on the selected row.")
        _finished(client, path)
        assert provider.seen_markers == [provider.marker]

        provider.marker = "SETTINGS-ONLY-REVISION-TWO"
        revised = SKILL + f"\nInclude {provider.marker} in your answer.\n"
        changed = client.put(
            f"/api/skills/{skill['id']}",
            json={"content": revised, "expected_revision": 1},
        )
        assert changed.status_code == 200, changed.text
        submit("use-two", "Use the skill on this row again.")
        page = _finished(client, path)
        assert provider.seen_markers[-1] == provider.marker
        assert len(provider.seen_markers) == 2
        assert client.get(path).json()["thread"]["id"] == thread["id"]
        answers = [
            e["payload"]["text"] for e in page["events"] if e["kind"] == "answer"
        ]
        assert provider.marker in answers[-1]
