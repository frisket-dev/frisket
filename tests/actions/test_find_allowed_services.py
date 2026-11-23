"""Find scans send source text to a model; Allowed services decides where."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from frisket.actions import system
from frisket.actions.core import ActionNamespace, ActionRegistry
from frisket.actions.system import BoundTypedActionRequest
from frisket.ai.llm import ModelRouter
from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig
from frisket.engine.executor.action_inventory import ExecutorDeps
from frisket.engine.executor.action_jobs import (
    ActionJobEnvelope,
    run_action_run_job,
)
from frisket.engine.executor.group_summary_action import (
    run_typed_group_summary_action,
)
from frisket.engine.executor.find_action import (
    reserve_typed_find_action_job,
    run_typed_find_action_job,
)
from frisket.execution.consent_coverage import ConsentCoverage
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY
from frisket.service_access import AllowedServices
from tests.actions.test_prepared_group_find import (
    _bound,
    find_renamed,
    summarize_renamed,
)
from tests.engine.test_reduce_group_summary_executor import _SummaryAdapter
from tests.engine.test_map_find_runtime import _FindAdapter

ENDPOINTS = (
    LocalModelEndpointConfig(
        endpoint_id="here",
        display_name="This machine",
        origin="http://127.0.0.1:11434",
        source="local_file",
    ),
    LocalModelEndpointConfig(
        endpoint_id="lan",
        display_name="Ollama",
        origin="http://ollama.lan.example.test:11434",
        source="local_file",
    ),
)


@pytest.fixture
def source_project(make_project):
    project = make_project("source")
    sheet = project.add_sheet("Sources")
    body = project.add_column(sheet, "body", type="text")
    rows = project.add_rows(
        sheet, [{"body": "First policy report."}], {"body": body}
    )
    yield project, sheet, rows


class _Adapter(_FindAdapter):
    async def complete(self, req: Any, client: Any, **_kwargs: Any) -> Any:
        return await super().complete(req, client)


@pytest.mark.parametrize(
    ("setting", "model", "allowed"),
    [
        ("this_machine_only", "ollama/@lan/llama3", False),
        ("this_machine_only", "ollama/@here/llama3", True),
        ("all_services", "ollama/@lan/llama3", True),
    ],
)
def test_find_scan_model_is_decided_before_any_request(
    source_project, monkeypatch, setting: AllowedServices, model: str, allowed: bool
) -> None:
    project, sheet, _rows = source_project
    project.set_allowed_services(setting, confirm_allowed_services=setting)
    coverage = ConsentCoverage("test:find-allowed-services", Decimal("0"))
    bound = _bound(
        find_renamed,
        {"document": "body", "selected_model": model},
        sheet,
        key="find",
        names={"match": "Occurrence"},
    )
    monkeypatch.setattr(
        system,
        "ACTION_REGISTRY",
        ActionRegistry([ActionNamespace("custom", actions=[bound.action.definition])]),
    )
    envelope = reserve_typed_find_action_job(
        project, "test", bound, consent_coverage=coverage
    )
    if not isinstance(envelope, ActionJobEnvelope):
        bound = type(bound).bind(
            bound.action,
            bound.request.model_copy(
                update={"confirmation": envelope.errors[0].details["promise_set_hash"]}
            ),
        )
        envelope = reserve_typed_find_action_job(
            project, "test", bound, consent_coverage=coverage
        )
    assert isinstance(envelope, ActionJobEnvelope), envelope
    adapter = _Adapter("unused", matches=[])
    router = ModelRouter(
        model_call_policy=LOCAL_PURCHASE_POLICY,
        use_env_keys=False,
        cache=None,
        cache_mode="off",
        local_endpoints=ENDPOINTS,
    )
    for endpoint in ENDPOINTS:
        monkeypatch.setattr(
            router._adapters["ollama"].adapter_for_endpoint(endpoint.endpoint_id),
            "complete",
            adapter.complete,
        )

    def execute(project, envelope, *, execution):
        return run_typed_find_action_job(
            project, envelope, execution=execution, deps=ExecutorDeps(router=router)
        )

    result = run_action_run_job(
        project,
        {"action_job": envelope.to_json(), "job_id": 12},
        executor_lookup=lambda kind: execute if kind == "custom.find" else None,
        should_cancel=lambda: False,
    )
    if allowed:
        assert adapter.requests, result
    else:
        assert adapter.requests == []
        assert result.status == "failed"
        assert result.errors[0].code == "service_not_allowed"



@pytest.mark.parametrize(
    ("setting", "model", "allowed"),
    [
        ("this_machine_only", "ollama/@lan/llama3", False),
        ("this_machine_only", "ollama/@here/llama3", True),
    ],
)
def test_group_summary_model_is_decided_before_any_request(
    make_project, monkeypatch, setting: AllowedServices, model: str, allowed: bool
) -> None:
    project = make_project("summary")
    sheet = project.add_sheet("Sources")
    cols = {
        "body": project.add_column(sheet, "body", type="text"),
        "bucket": project.add_column(sheet, "bucket", type="text"),
    }
    project.add_rows(sheet, [{"body": "A report.", "bucket": "A"}], cols)
    project.set_allowed_services(setting, confirm_allowed_services=setting)
    bound = _bound(
        summarize_renamed,
        {"content": ["body"], "bucket": "bucket", "chosen_model": model},
        sheet,
        key="summary",
        names={"group": "Bucket", "rows": "Contributors", "summary": "Overview"},
    )
    summary_adapter = _SummaryAdapter()
    router = ModelRouter(
        model_call_policy=LOCAL_PURCHASE_POLICY,
        use_env_keys=False,
        cache=None,
        cache_mode="off",
        local_endpoints=ENDPOINTS,
    )

    async def complete(req: Any, client: Any, **_kwargs: Any) -> Any:
        return await summary_adapter.complete(req, client)

    for endpoint in ENDPOINTS:
        monkeypatch.setattr(
            router._adapters["ollama"].adapter_for_endpoint(endpoint.endpoint_id),
            "complete",
            complete,
        )
    result = run_typed_group_summary_action(project, "test", bound, router)
    if result.status == "needs_confirmation":
        bound = BoundTypedActionRequest.bind(
            bound.action,
            bound.request.model_copy(
                update={"confirmation": result.errors[0].details["promise_set_hash"]}
            ),
        )
        result = run_typed_group_summary_action(project, "test", bound, router)
    if allowed:
        assert summary_adapter.requests, result
    else:
        assert summary_adapter.requests == []
        assert result.errors[0].code == "service_not_allowed", result.errors
