"""Egress gate dispatch tests: ``off`` rejects
remote engines with a typed ``NetworkDisabled`` at validate/enqueue time, and
the gate binds work that already ran — a stored remote-engine spec cannot be
re-executed live after the project flips ``off``."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import httpx
import pytest

from frisket.ai.llm import ModelRouter
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest
from frisket.engine.executor import ExecutorDeps, http_request, run_action_spec
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.runner import validation
from frisket.engine.runner.map_runner import MapRunner, NetworkDisabled
from frisket.engine.runner.network_policy import remote_capability_for_spec
from frisket.engine.executor.queued_actions import queued_v1_action_request
from frisket.engine.store import Project
from frisket.engine.store.execution_routes import instance_principal
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.execution.consent_coverage import ConsentCoverage


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "p.frisket")
    yield p
    p.close()


def _seed_statement_sheet(p: Project) -> int:
    sheet = p.add_sheet("data")
    cols = {"statement": p.add_column(sheet, "statement")}
    p.add_rows(sheet, [{"statement": "Hello"}], cols)
    return sheet


def _translate_request(sheet_id: int, engine: str) -> dict[str, Any]:
    return {
        "action_id": "map.translate",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["statement"],
            "engine": engine,
            "target_language": "Spanish",
        },
        "output_names": {"translation": "es"},
        "idempotency_key": "network-translation",
    }


def _plan(project, body):
    request = ActionRequest.model_validate(body)
    return build_typed_map_rows_plan(
        project,
        BoundTypedActionRequest.bind(ACTION_REGISTRY.get(request.action_id), request),
    )


def _router() -> ModelRouter:
    return ModelRouter(cache=None, cache_mode="off")


def test_off_rejects_deepl_translate_at_validate(project) -> None:
    sheet = _seed_statement_sheet(project)
    project.set_network_policy(mode="off")
    runner = MapRunner(project, _router(), authority=UnroutedOnlyAuthority(project))
    plan = _plan(project, _translate_request(sheet, "deepl"))
    with pytest.raises(NetworkDisabled) as excinfo:
        validation.validate_spec(
            runner.project,
            runner.router,
            runner.run_store,
            plan.spec_dict(),
            program=plan.program,
            confirmed=True,
            resume_run_id=None,
            pricing_policy=runner.pricing_policy,
            composition=runner.execution_composition,
        )
    assert excinfo.value.capability == "engine:deepl"


def test_off_rejects_remote_llm_provider_at_validate(project) -> None:
    sheet = _seed_statement_sheet(project)
    project.set_network_policy(mode="off")
    runner = MapRunner(project, _router(), authority=UnroutedOnlyAuthority(project))
    plan = _plan(
        project,
        {
            "action_id": "map.classify",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {
                "source": ["statement"],
                "engine": "llm",
                "model": "anthropic/claude-haiku-4-5",
                "fields": [
                    {
                        "name": "relevance",
                        "type": "score",
                        "description": "0-10 relevance",
                    }
                ],
            },
            "idempotency_key": "network-classify",
        },
    )
    with pytest.raises(NetworkDisabled) as excinfo:
        validation.validate_spec(
            runner.project,
            runner.router,
            runner.run_store,
            plan.spec_dict(),
            program=plan.program,
            confirmed=True,
            resume_run_id=None,
            pricing_policy=runner.pricing_policy,
            composition=runner.execution_composition,
        )
    assert excinfo.value.capability == "provider:anthropic"


def test_off_rejects_typed_api_call_at_validate_and_dispatch(
    project, monkeypatch
) -> None:
    """The admitted typed program closes the gate before any HTTP effect."""

    async def forbidden(*_args, **_kwargs):
        pytest.fail("network-off API dispatch reached the HTTP provider")

    monkeypatch.setattr(http_request, "safe_request", forbidden)

    sheet = _seed_statement_sheet(project)
    before = {
        table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("runs", "ops", "results", "receipts")
    }
    project.set_network_policy(mode="off")
    runner = MapRunner(project, _router(), authority=UnroutedOnlyAuthority(project))
    request = ActionRequest(
        action_id="map.api_call",
        scope={"kind": "sheet_rows", "sheet_id": sheet},
        params={"request": {"url": "https://api.example.test/items"}},
        idempotency_key="api-network-off",
    )
    plan = build_typed_map_rows_plan(
        project,
        BoundTypedActionRequest.bind(ACTION_REGISTRY.get("map.api_call"), request),
    )
    with pytest.raises(NetworkDisabled) as excinfo:
        validation.validate_spec(
            runner.project,
            runner.router,
            runner.run_store,
            plan.spec_dict(),
            program=plan.program,
            confirmed=True,
            resume_run_id=None,
            pricing_policy=runner.pricing_policy,
            composition=runner.execution_composition,
        )
    assert excinfo.value.capability == "external:api_call"
    result = run_action_spec(
        project, request.model_dump(mode="json"), project_id="api-network-off"
    )
    assert result.status == "failed", result.errors
    assert result.errors[0].code == "network_disabled"
    assert result.errors[0].details["capability"] == "external:api_call"
    for table in ("runs", "ops", "results", "receipts"):
        assert (
            project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            == before[table]
        )


def test_canonical_action_kinds_drive_catalog_classification() -> None:

    for kind, capability, params in (
        (
            "map.api_call",
            "external:api_call",
            {"request": {"url": "https://api.example.test/items"}},
        ),
        ("enrich.geocode", "external:geocode", {"source": "source"}),
        (
            "enrich.census_demographics",
            "external:us_census_acs",
            {"source": "source"},
        ),
    ):
        queued = queued_v1_action_request(
            {
                "action_id": kind,
                "scope": {"kind": "sheet_rows", "sheet_id": 1},
                "params": params,
                "idempotency_key": f"egress:{kind}",
            }
        )
        assert queued is not None and queued.program is not None
        assert (
            remote_capability_for_spec(
                queued.program, dict(queued.expected_runner_spec)
            )
            == capability
        )


def test_private_runner_name_cannot_change_egress_policy() -> None:
    import copy

    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.map_rows_action import _typed_map_rows_plan

    plan = _typed_map_rows_plan(
        typed_action_for_request(
            {
                "action_id": "media.transcribe",
                "scope": {"kind": "sheet_rows", "sheet_id": 1},
                "params": {"source": "audio", "engine": "openai/whisper-1"},
                "idempotency_key": "network-policy",
            }
        )
    )
    canonical = plan.program
    renamed = copy.copy(canonical)
    renamed.name = "private_runner_name_changed"
    spec = plan.spec_dict()

    assert remote_capability_for_spec(canonical, spec) == "provider:openai"
    assert remote_capability_for_spec(renamed, spec) == "provider:openai"


def test_prepare_run_is_gated_too(project) -> None:
    """The queued enqueue path goes through prepare_run -> _prepare ->
    _validate_spec; the gate must fire before any op/run row exists."""
    sheet = _seed_statement_sheet(project)
    project.set_network_policy(mode="off")
    runner = MapRunner(
        project,
        _router(),
        authority=UnroutedOnlyAuthority(project),
        allow_action_lifecycle_only_recipes=True,
    )
    plan = _plan(project, _translate_request(sheet, "deepl"))
    with pytest.raises(NetworkDisabled):
        runner.prepare_run(plan.spec_dict(), program=plan.program, confirmed=True)
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_reexecution_of_a_stored_run_is_gated_after_policy_flip(
    project, monkeypatch
) -> None:
    """The policy flip binds work that ALREADY ran: run DeepL under ``on``
    (mocked transport, results stored), flip the project ``off``, then
    re-execute the very same spec through the preview path. It must raise —
    and DeepL must not be called a second time."""
    monkeypatch.setenv("DEEPL_API_KEY", "k:fx")
    sheet = _seed_statement_sheet(project)
    request_body = _translate_request(sheet, "deepl")
    plan = _plan(project, request_body)
    spec = plan.spec_dict()

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json={"translations": [{"text": "Hola", "detected_source_language": "EN"}]},
        )

    live_router = _router()
    live_router._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    # Explicit confirmation establishes the original run before the policy flip.
    deps = ExecutorDeps(
        consent_coverage=ConsentCoverage(instance_principal(project), Decimal("0"))
    )
    challenge = run_action_spec(
        project, request_body, project_id="network-test", router=live_router, deps=deps
    )
    assert challenge.status == "needs_confirmation"
    result = run_action_spec(
        project,
        {
            **request_body,
            "confirmation": challenge.errors[0].details["promise_set_hash"],
        },
        project_id="network-test",
        router=live_router,
        deps=deps,
    )
    assert result.status == "completed", result
    assert calls["n"] == 1  # the original run really egressed (mock)

    project.set_network_policy(mode="off")
    replayer = MapRunner(project, _router(), authority=UnroutedOnlyAuthority(project))
    with pytest.raises(NetworkDisabled):
        asyncio.run(
            replayer.preview(
                {**spec, "row_ids": list(project.visible_row_ids(sheet))},
                program=plan.program,
            )
        )
    assert calls["n"] == 1  # and the re-execution never called out again
