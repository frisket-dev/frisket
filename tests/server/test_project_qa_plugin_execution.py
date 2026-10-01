"""A real installed plugin action can complete through the research bridge."""

from __future__ import annotations

from pathlib import Path

import pytest

from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.engine.store.project_qa_research import ProjectQAResearchStore
from frisket.execution.pricing_policy import usd_to_micros
from frisket.server.services.action_runs import ActionRunService
from frisket.server.services.project_qa_action_host import ProjectAskActionHost
from frisket.server.services.project_qa_child_runs import ProjectQAChildRunService
from frisket.server.services.project_qa_execution import ProjectQAExecutionService
from frisket.server.services.project_qa_research import ResearchSession
from frisket.server.services.workbench import WorkbenchService
from frisket.server.workspace import Workspace


PLUGIN_ID = "demo.receipt_stamp"
ACTION_ID = "demo.receipt_stamp.stamp"
TRUSTED_LOCAL_BACKEND_CAPABILITY = "plugin:trusted_local_backend"
PLUGIN_ROOT = (
    Path(__file__).parents[1] / "fixtures" / "local_plugins" / "demo_receipt_stamp"
)


@pytest.fixture(autouse=True)
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
    session = ResearchSession(ledger, research["id"], authorize=lambda: None)
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
        quote_provider=host.quote,
        run_action=host.run,
        wait_child=child_runs.wait,
        rate_actual_cost=usd_to_micros,
    )
    try:
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
    finally:
        project.close()
