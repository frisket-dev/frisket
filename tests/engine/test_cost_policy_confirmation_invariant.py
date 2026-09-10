"""Consent closure for registered actions and independently authored SDK plugins.

Typed actions carry the exact confirmation token on ActionRequest, outside domain
Params. Plugin declarations retain their own explicit confirmation wiring.
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.types import ActionRequest


@pytest.fixture(autouse=True)
def _always_challenge_metered_actions(monkeypatch):
    # These tests prove explicit request-level confirmation. Do not let the
    # process-wide standing preapproval threshold make a local fixture run
    # straight through its intended challenge.
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")


def test_published_confirmation_roster_declares_exact_request_echo() -> None:
    """Every confirming typed catalog exposes the shared request-level token."""
    from frisket.actions.core import MapRows, ModelRows

    properties = ActionRequest.model_json_schema()["properties"]
    assert "confirmation" in properties
    assert properties["confirmation"]["default"] is None
    assert {"type": "string", "minLength": 1} in properties["confirmation"]["anyOf"]
    checked = set()
    for registered in ACTION_REGISTRY.actions:
        catalog = registered.catalog_entry()
        if isinstance(registered.definition.run, (MapRows, ModelRows)) and (
            catalog["cost_policy"]["kind"] != "none"
        ):
            assert catalog["cost_policy"]["requires_confirmation"], registered.action_id
        if not catalog["cost_policy"]["requires_confirmation"]:
            continue
        checked.add(registered.action_id)
        assert not (
            {"confirmed", "consented_promise_set_hash", "confirmation"}
            & set(registered.definition.run.params_model.model_fields)
        ), registered.action_id
        assert any(
            item["code"].endswith("_requires_confirmation")
            for item in catalog["errors"]
        ), registered.action_id
    assert checked >= {
        "map.classify",
        "map.ner",
        "map.translate",
        "map.extract",
        "map.mcp_extract",
        "research.answer",
        "map.find",
        "reduce.group_summary",
        "run.backfill",
        "map.api_call",
    }


def _assert_refresh_request_confirmation_reaches_its_host_gate(tmp_path, registered):
    from test_multi_parent_sheet_refresh import _seed_join
    from frisket.engine.executor import run_action_spec

    seeded = _seed_join(tmp_path / "refresh-fanout.frisket")
    project, child_id = seeded["project"], seeded["child_id"]
    try:
        parent_op_id = project.db.execute(
            "SELECT parent_op_id FROM sheets WHERE id=?", (child_id,)
        ).fetchone()[0]
        saved = json.loads(
            project.db.execute(
                "SELECT spec FROM ops WHERE id=?", (parent_op_id,)
            ).fetchone()[0]
        )
        saved["params"]["max_output_rows"] = 1
        project.db.execute(
            "UPDATE ops SET spec=? WHERE id=?", (json.dumps(saved), parent_op_id)
        )
        # Even historical model metadata cannot introduce a second money token.
        project.db.execute(
            "INSERT INTO runs "
            "(op_id, sheet_id, action_kind, status, model, total_rows, cost_estimate) "
            "VALUES (?, ?, 'derive.join', 'completed', 'stub/model', 3, 0.42)",
            (parent_op_id, child_id),
        )
        project.db.commit()
        request = {
            "action_id": registered.action_id,
            "scope": {"kind": "project"},
            "params": {"sheet_id": child_id},
            "idempotency_key": "refresh-confirmation-closure",
        }
        before = tuple(project.db.iterdump())
        challenge = run_action_spec(project, request, project_id="p-ref")
        assert challenge.status == "needs_confirmation"
        assert challenge.errors[0].code == "join_fanout_requires_confirmation"
        assert challenge.errors[0].field == "confirmation"
        assert "estimate" not in challenge.errors[0].details
        assert challenge.errors[0].details["estimated_rows"] == 3
        assert challenge.errors[0].details["max_output_rows"] == 1
        assert tuple(project.db.iterdump()) == before
        token = challenge.errors[0].details["promise_set_hash"]
        assert isinstance(token, str) and token

        wrong = run_action_spec(
            project, {**request, "confirmation": "wrong-token"}, project_id="p-ref"
        )
        assert wrong.status == "needs_confirmation"
        assert wrong.errors[0].details["promise_set_hash"] == token
        assert tuple(project.db.iterdump()) == before

        confirmed = run_action_spec(
            project, {**request, "confirmation": token}, project_id="p-ref"
        )
        assert confirmed.status == "completed", confirmed.errors
        assert confirmed.outputs[0].sheet_id == child_id
        after = tuple(project.db.iterdump())
        replay = run_action_spec(project, request, project_id="p-ref")
        assert replay.receipt_id == confirmed.receipt_id
        assert tuple(project.db.iterdump()) == after
    finally:
        project.close()


def _assert_backfill_request_confirmation_reaches_its_host_gate(tmp_path, registered):
    from http_test_helpers import drain_queue, post_v1_action_with_exact_confirmation
    from tests.server.test_run_backfill_executor import (
        _classify_spec,
        _client,
        _seed_project,
    )
    from frisket.engine.executor import run_action_spec

    with _client(tmp_path) as client:
        pid, sheet_id = _seed_project(client)
        response = post_v1_action_with_exact_confirmation(
            client, pid, _classify_spec(sheet_id)
        )
        assert response.status_code == 200, response.text
        drain_queue(client)
        project = client.app.state.workspace.get(pid)
        column = next(
            col for col in project.columns(sheet_id) if col["name"] == "story"
        )
        project.add_rows(sheet_id, [{"story": "late"}], {"story": column["id"]})
        request = {
            "action_id": registered.action_id,
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {"column": "beat"},
            "idempotency_key": "backfill-confirmation-closure",
        }
        router = client.app.state.workspace.router_for(project)
        before = tuple(project.db.iterdump())
        challenge = run_action_spec(project, request, project_id=pid, router=router)
        assert challenge.status == "needs_confirmation", challenge.errors
        assert challenge.errors[0].code == "model_cost_requires_confirmation"
        assert challenge.errors[0].field == "confirmation"
        assert tuple(project.db.iterdump()) == before
        token = challenge.errors[0].details["promise_set_hash"]
        assert isinstance(token, str) and token
        wrong = run_action_spec(
            project,
            {**request, "confirmation": "wrong-token"},
            project_id=pid,
            router=router,
        )
        assert wrong.status == "needs_confirmation"
        assert wrong.errors[0].details["promise_set_hash"] == token
        assert tuple(project.db.iterdump()) == before
        confirmed = run_action_spec(
            project,
            {**request, "confirmation": token},
            project_id=pid,
            router=router,
        )
        assert confirmed.status == "completed", confirmed.errors
        assert confirmed.outputs[0].ref["filled"] == 1
        after = tuple(project.db.iterdump())
        replay = run_action_spec(project, request, project_id=pid, router=router)
        assert replay.receipt_id == confirmed.receipt_id
        assert tuple(project.db.iterdump()) == after


def _assert_google_request_confirmation_reaches_its_host_gate(tmp_path, registered):
    from frisket.actions.system import BoundTypedActionRequest
    from frisket.actions.types import ActionRequest
    from frisket.engine.executor import ExecutorDeps
    from frisket.engine.executor.action_jobs import (
        ActionJobEnvelope,
        run_action_run_job,
    )
    from frisket.engine.executor.google_sheets_action import (
        prepare_google_sheets_action_job,
        run_google_sheets_action_job,
        run_typed_google_sheets_export,
    )
    from frisket.engine.store import Project

    calls = []

    class Client:
        def export_tabs(self, **kwargs):
            calls.append(kwargs)
            return {"spreadsheet_id": "made", "updated_tabs": []}

    project = Project.create(tmp_path / "google-confirmation.frisket")
    try:
        sheet_id = project.add_sheet("Rows")
        column_id = project.add_column(sheet_id, "name", "text")
        project.add_rows(sheet_id, [{"name": "Ada"}], {"name": column_id})
        deps = ExecutorDeps(
            google_sheets_client=Client(),
            connected_account_resolver=lambda provider, key: {
                "id": key,
                "provider": provider,
                "external_subject": "owner",
                "refresh_token": "secret",
            },
        )
        request = ActionRequest(
            action_id=registered.action_id,
            scope={"kind": "project"},
            params={
                "connection_id": "chosen",
                "source": {"kind": "current_sheet", "sheet_id": sheet_id},
                "destination": {"kind": "google_sheets", "mode": "new_spreadsheet"},
            },
            idempotency_key="google-confirmation-closure",
        )
        bound = BoundTypedActionRequest.bind(registered, request)
        before = tuple(project.db.iterdump())
        challenge = prepare_google_sheets_action_job(project, "p", bound, deps=deps)
        assert challenge.status == "needs_confirmation"
        assert challenge.errors[0].code == "irreversible_external_requires_confirmation"
        assert challenge.errors[0].field == "confirmation"
        token = challenge.errors[0].details["promise_set_hash"]
        assert isinstance(token, str) and token
        assert tuple(project.db.iterdump()) == before
        assert calls == []
        wrong = BoundTypedActionRequest.bind(
            registered, request.model_copy(update={"confirmation": "wrong-token"})
        )
        challenge = prepare_google_sheets_action_job(project, "p", wrong, deps=deps)
        assert challenge.status == "needs_confirmation"
        assert challenge.errors[0].details["promise_set_hash"] == token
        assert tuple(project.db.iterdump()) == before
        assert calls == []
        confirmed = BoundTypedActionRequest.bind(
            registered, request.model_copy(update={"confirmation": token})
        )
        envelope = prepare_google_sheets_action_job(project, "p", confirmed, deps=deps)
        assert isinstance(envelope, ActionJobEnvelope)
        assert calls == []
        result = run_action_run_job(
            project,
            {"action_job": envelope.to_json()},
            executor_lookup=lambda _kind: (
                lambda project_, envelope_: run_google_sheets_action_job(
                    project_, envelope_, deps=deps
                )
            ),
        )
        assert result.status == "completed", result.errors
        assert len(calls) == 1
        after = tuple(project.db.iterdump())
        replay = run_typed_google_sheets_export(
            project, "p", bound, deps=ExecutorDeps()
        )
        assert replay.receipt_id == result.receipt_id
        assert tuple(project.db.iterdump()) == after
        assert len(calls) == 1
    finally:
        project.close()


def _assert_embedding_request_confirmation_reaches_its_host_gate(tmp_path, registered):
    import pytest

    from frisket.actions.system import BoundTypedActionRequest
    from frisket.actions.core import SemanticJoin
    from frisket.actions.types import ActionRequest
    from frisket.engine.executor.actions import _default_map_runner_factory
    from frisket.engine.executor.semantic_join_action import (
        run_typed_semantic_join_action,
        typed_semantic_join_queue_spec,
    )
    from frisket.engine.executor.cluster_action import (
        run_typed_cluster_action,
        typed_cluster_queue_spec,
    )
    from frisket.engine.store import Project

    is_join = isinstance(registered.definition.run, SemanticJoin)
    run_action = run_typed_semantic_join_action if is_join else run_typed_cluster_action
    queue_spec = typed_semantic_join_queue_spec if is_join else typed_cluster_queue_spec
    project = Project.create(tmp_path / f"{registered.action_id}-confirmation.frisket")
    calls = []

    def embed(texts):
        calls.append(list(texts))
        return {
            "vectors": [[1.0, 0.0] for _ in texts],
            "provider_id": "openai",
            "actual_model_id": "text-embedding-3-small",
            "provider_cost_usd": 0.001,
            "cost_source": "provider_reported",
            "usage": {"input_count": len(texts)},
        }

    def counts():
        return tuple(
            project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("runs", "receipts", "columns", "sheets", "model_calls")
        )

    try:
        source = project.add_sheet("Source")
        source_column = project.add_column(source, "body")
        project.add_rows(source, [{"body": "ACME"}], {"body": source_column})
        target = project.add_sheet("Target")
        target_column = project.add_column(target, "company")
        project.add_rows(target, [{"company": "Acme"}], {"company": target_column})
        request = ActionRequest(
            action_id=registered.action_id,
            scope={"kind": "sheet_rows", "sheet_id": source},
            params={
                "source": "body",
                "target": {"sheet_id": target, "column": "company"},
            }
            if is_join
            else {"source": "body", "method": "semantic"},
            sheet_name="Matches" if is_join else None,
            idempotency_key="semantic-confirmation-closure",
        )

        def run(confirmation=None):
            bound = BoundTypedActionRequest.bind(
                registered, request.model_copy(update={"confirmation": confirmation})
            )
            _, spec, _ = queue_spec(bound)
            assert spec.confirmed_fn is not None
            assert spec.confirmed_fn(bound.params) is (confirmation is not None)
            assert spec.cost_gate_error_fn is not None
            return run_action(project, "test", bound, None, _default_map_runner_factory)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(
                "frisket.semantic.resolve_embedder",
                lambda *a, **kw: (embed, "openai/text-embedding-3-small"),
            )
            before = counts()
            challenge = run()
            assert challenge.status == "needs_confirmation", challenge.errors
            assert challenge.errors[0].code == "model_cost_requires_confirmation"
            assert challenge.errors[0].field == "confirmation"
            token = challenge.errors[0].details["promise_set_hash"]
            assert counts() == before
            assert calls == []
            wrong = run("0" * 64)
            assert wrong.status == "needs_confirmation", wrong.errors
            assert wrong.errors[0].details["promise_set_hash"] == token
            assert counts() == before
            assert calls == []
            accepted = run(token)
            assert accepted.status == "completed", accepted.errors
            assert calls
    finally:
        project.close()


def _assert_prepared_model_request_confirmation_reaches_its_host_gate(
    tmp_path, registered
):
    from frisket.ai.llm import LLMResponse, ModelRouter
    from frisket.actions.system import BoundTypedActionRequest
    from frisket.contracts.action import ActionResult
    from frisket.engine.executor.action_jobs import ActionJobEnvelope
    from frisket.engine.executor.find_action import reserve_typed_find_action_job
    from frisket.engine.executor.group_summary_action import (
        run_typed_group_summary_action,
    )
    from frisket.engine.store import Project

    calls = []

    class Adapter:
        async def complete(self, request, client):
            calls.append(request)
            return LLMResponse(
                content="A summary.",
                data=None,
                tokens_in=10,
                tokens_out=3,
                model=request.model,
                cost=0.0,
            )

    router = ModelRouter(keys={"anthropic": "test"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = Adapter()
    project = Project.create(tmp_path / f"{registered.action_id}-consent.frisket")
    try:
        sheet_id = project.add_sheet("Source")
        column_id = project.add_column(sheet_id, "body")
        project.add_rows(sheet_id, [{"body": "A source story."}], {"body": column_id})
        is_find = registered.action_id == "map.find"
        request = ActionRequest(
            action_id=registered.action_id,
            scope={"kind": "sheet_rows", "sheet_id": sheet_id},
            params={
                "source": "body" if is_find else ["body"],
                "model": "anthropic/unpriced-consent-test",
                "instruction": "Find or summarize the story.",
            },
            sheet_name="Results",
            idempotency_key="prepared-model-consent",
        )

        def run(confirmation=None):
            bound = BoundTypedActionRequest.bind(
                registered, request.model_copy(update={"confirmation": confirmation})
            )
            return (
                reserve_typed_find_action_job(project, "test", bound)
                if is_find
                else run_typed_group_summary_action(project, "test", bound, router)
            )

        before = tuple(project.db.iterdump())
        challenge = run()
        assert isinstance(challenge, ActionResult)
        assert challenge.status == "needs_confirmation", challenge.errors
        error = challenge.errors[0]
        assert error.code == "model_cost_requires_confirmation"
        assert error.field == "confirmation"
        token = error.details["promise_set_hash"]
        assert isinstance(token, str) and token
        wrong = run("wrong-token")
        assert isinstance(wrong, ActionResult)
        assert wrong.status == "needs_confirmation"
        assert wrong.errors[0].details["promise_set_hash"] == token
        assert tuple(project.db.iterdump()) == before
        assert calls == []
        accepted = run(token)
        if is_find:
            assert isinstance(accepted, ActionJobEnvelope)
            assert calls == []  # Provider work belongs to the queued worker.
        else:
            assert accepted.status == "completed", accepted.errors
            assert len(calls) == 1
    finally:
        project.close()


def test_registered_metered_actions_publish_and_wire_the_request_confirmation(
    tmp_path,
) -> None:
    """Typed actions keep consent on ActionRequest, outside prompt Params."""

    from frisket.actions.research_types import WebSearcher
    from frisket.actions.core import (
        GoogleSheetsExport,
        MapRows,
        ModelRows,
        SemanticJoin,
        _ProjectAction,
        routed_capability,
    )
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.http_types import HttpRequester
    from frisket.actions.file_types import FileFetcher
    from frisket.actions.media_download_types import MediaDownloader
    from frisket.actions.mcp_types import McpExtractor
    from frisket.actions.research_types import Researcher
    from frisket.actions.find_types import FindScanner
    from frisket.actions.group_summary_types import GroupSummarizer
    from frisket.actions.types import ActionRequest, RunBackfiller, SheetRefresher
    from frisket.actions.cluster_types import ValueClusterer
    from frisket.actions.system import BoundTypedActionRequest
    from frisket.engine.executor.map_rows_action import typed_queued_map_spec

    checked = 0
    for registered in ACTION_REGISTRY.actions:
        catalog = registered.catalog_entry()
        if not catalog["cost_policy"]["requires_confirmation"]:
            continue
        terminal = registered.definition.run
        assert not (
            {"confirmed", "consented_promise_set_hash", "confirmation"}
            & set(terminal.params_model.model_fields)
        )
        if isinstance(terminal, _ProjectAction):
            # A new confirming project capability needs its own actual host
            # admission proof; it must not be silently skipped as "not a row".
            assert terminal.single_capability() in (
                SheetRefresher,
                RunBackfiller,
                ValueClusterer,
                FindScanner,
                GroupSummarizer,
            ), registered.action_id
            errors = {error["code"] for error in catalog["errors"]}
            if terminal.capabilities == (SheetRefresher,):
                assert catalog["cost_policy"] == {
                    "kind": "none",
                    "requires_confirmation": True,
                }
                assert "model_cost_requires_confirmation" not in errors
            else:
                assert catalog["cost_policy"]["kind"] == "model_metered"
                assert "model_cost_requires_confirmation" in errors
            if terminal.single_capability() in (FindScanner, GroupSummarizer):
                _assert_prepared_model_request_confirmation_reaches_its_host_gate(
                    tmp_path, registered
                )
            elif terminal.capabilities == (SheetRefresher,):
                assert "join_fanout_requires_confirmation" in errors
                _assert_refresh_request_confirmation_reaches_its_host_gate(
                    tmp_path, registered
                )
            elif terminal.capabilities == (RunBackfiller,):
                _assert_backfill_request_confirmation_reaches_its_host_gate(
                    tmp_path, registered
                )
            else:
                _assert_embedding_request_confirmation_reaches_its_host_gate(
                    tmp_path, registered
                )
            checked += 1
            continue
        if isinstance(terminal, GoogleSheetsExport):
            assert catalog["cost_policy"]["kind"] == "none"
            assert "irreversible_external_requires_confirmation" in {
                error["code"] for error in catalog["errors"]
            }
            _assert_google_request_confirmation_reaches_its_host_gate(
                tmp_path, registered
            )
            checked += 1
            continue
        if isinstance(terminal, SemanticJoin):
            assert catalog["cost_policy"]["kind"] == "model_metered"
            assert "model_cost_requires_confirmation" in {
                error["code"] for error in catalog["errors"]
            }
            _assert_embedding_request_confirmation_reaches_its_host_gate(
                tmp_path, registered
            )
            checked += 1
            continue
        routed = routed_capability(terminal) is not None
        http = HttpRequester in getattr(terminal, "capabilities", ())
        fetch = any(
            capability in getattr(terminal, "capabilities", ())
            for capability in (FileFetcher, MediaDownloader)
        )
        agent = any(
            capability in getattr(terminal, "capabilities", ())
            for capability in (McpExtractor, Researcher)
        )
        assert (
            isinstance(terminal, ModelRows)
            or WebSearcher in getattr(terminal, "capabilities", ())
            or routed
            or (isinstance(terminal, MapRows) and (http or fetch or agent))
        ), registered.action_id
        external = (
            WebSearcher in getattr(terminal, "capabilities", ())
            or routed
            or http
            or fetch
        )
        assert catalog["cost_policy"]["kind"] == (
            "unknown"
            if http or fetch or agent
            else "external_metered"
            if external
            else "model_metered"
        )
        assert (
            "external_cost_requires_confirmation"
            if external
            else "model_cost_requires_confirmation"
        ) in {error["code"] for error in catalog["errors"]}
        params = dict(catalog["examples"][0]["params"])
        if registered.action_id in {"map.classify", "map.ner", "map.translate"}:
            # The first published example may select a free/local engine.
            # This proof targets the action's paid model branch explicitly.
            params.update(engine="llm", model="anthropic/claude-haiku-4-5")
        request = ActionRequest(
            action_id=registered.action_id,
            scope={"kind": "sheet_rows", "sheet_id": 1},
            params=params,
            output_names={
                field.key: field.key
                for field in terminal.resolve_output_fields(
                    terminal.params_model.model_validate(params)
                )
            },
            idempotency_key=f"{registered.action_id}@confirmation-closure",
        )
        bound = BoundTypedActionRequest.bind(registered, request)
        _action, spec, _program = typed_queued_map_spec(bound)
        assert spec.confirmed_fn is not None
        assert spec.confirmed_fn(bound.params) is False
        confirmed = request.model_copy(update={"confirmation": "0" * 64})
        confirmed_bound = BoundTypedActionRequest.bind(registered, confirmed)
        _action, confirmed_spec, _program = typed_queued_map_spec(confirmed_bound)
        assert confirmed_spec.confirmed_fn is not None
        assert confirmed_spec.confirmed_fn(confirmed_bound.params) is True
        assert spec.cost_gate_error_fn is not None
        checked += 1
    assert checked >= 3


@pytest.mark.parametrize(
    "shape",
    [
        "requires_confirmation",
        "threads_confirmed",
        "needs_confirmation_error",
        "unknown",
    ],
)
def test_plugin_confirmation_shapes_thread_explicit_consent(shape):
    """Plugin SDK support is checked with real declarations, not a retired roster."""
    from frisket.contracts.action import (
        ActionError,
        CostPolicy,
        IdempotencyPolicy,
        RetryPolicy,
    )
    from frisket.sdk.declaration import Op
    from frisket.sdk.maprunner import build_reserved_spec

    class Params(BaseModel):
        confirmed: bool = False
        consented_promise_set_hash: str | None = None

    options = (
        {"cost": CostPolicy(kind="unknown", requires_confirmation=True)}
        if shape == "unknown"
        else {
            "cost": CostPolicy(kind="model_metered", requires_confirmation=True),
            shape: (
                lambda action, params: ActionError(
                    code="external_cost_requires_confirmation",
                    message="Confirm plugin work.",
                    action_kind=action.kind,
                    needs_confirmation=True,
                )
            )
            if shape == "needs_confirmation_error"
            else True,
        }
    )
    declaration = Op(
        kind="plugin.confirmation_probe",
        title="Confirmation probe",
        description="Exercise plugin consent wiring.",
        params_model=Params,
        output_model=BaseModel,
        errors=(),
        side_effects=(),
        idempotency=IdempotencyPolicy(
            supported=True,
            scope="project",
            key_field="idempotency_key",
            behavior="replay",
        ),
        retry=RetryPolicy(supported=True, strategy="idempotency_replay"),
        examples=(),
        primary_fields=(),
        **options,
    )
    spec = build_reserved_spec(declaration)
    assert spec.confirmed_fn is not None
    assert spec.confirmed_fn(Params()) is False
    assert spec.confirmed_fn(Params(confirmed=False)) is False
    assert spec.confirmed_fn(Params(confirmed=True)) is True
    assert spec.params_hash_fn is not None
    if shape != "threads_confirmed":
        assert spec.cost_gate_error_fn is not None


def test_unpriceable_actions_route_their_gate_to_an_explicit_consent():
    """Unknown cost gates name the request token and a published 402 error.

    Both external HTTP work and agent-driven model/tool work must remain
    runnable through explicit consent, never silently priced as zero or sent
    to a confirmation field their Params schema rejects.
    """
    from frisket.engine.runner.validation import CostGate
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.system import BoundTypedActionRequest
    from frisket.actions.types import ActionRequest
    from frisket.engine.executor.map_rows_action import typed_queued_map_spec

    typed_unpriceable = {
        registered.action_id: registered
        for registered in ACTION_REGISTRY.actions
        if registered.catalog_entry()["cost_policy"]["kind"] == "unknown"
    }
    assert set(typed_unpriceable) >= {
        "map.api_call",
        "media.fetch_url",
        "media.ytdlp_download",
    }

    for kind, registered in sorted(typed_unpriceable.items()):
        catalog = registered.catalog_entry()
        assert catalog["cost_policy"]["requires_confirmation"] is True, kind
        request = ActionRequest(
            action_id=kind,
            scope={"kind": "sheet_rows", "sheet_id": 1},
            params=catalog["examples"][0]["params"],
            idempotency_key="typed-unknown-cost",
        )
        bound = BoundTypedActionRequest.bind(registered, request)
        action, spec, _program = typed_queued_map_spec(bound)
        assert spec.params_hash_fn is not None, kind
        assert spec.cost_gate_error_fn is not None, kind
        error = spec.cost_gate_error_fn(action, CostGate(None))
        assert error.code == (
            "model_cost_requires_confirmation"
            if kind in {"map.mcp_extract", "research.answer"}
            else "external_cost_requires_confirmation"
        ), kind
        assert error.field == "confirmation", kind
        assert error.needs_confirmation is True, kind
        assert error.code in {item["code"] for item in catalog["errors"]}, kind


def test_typed_http_unknown_cost_requires_exact_consent_before_effect(
    tmp_path, monkeypatch
):
    import asyncio
    import httpx

    from frisket.ai.llm import ModelRouter
    from frisket.actions.types import ActionRequest
    from frisket.engine.executor import run_action_spec
    from frisket.engine.store import Project

    monkeypatch.setattr(
        "frisket.ops.netguard.safe_pinned_addresses",
        lambda url, **kwargs: ["93.184.216.34"],
    )
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    router = ModelRouter(cache=None, cache_mode="off")
    router._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    project = Project.create(tmp_path / "http-exact-consent.frisket")
    try:
        sheet_id = project.add_sheet("Rows")
        project.add_rows(sheet_id, [{}], {})
        request = ActionRequest(
            action_id="map.api_call",
            scope={"kind": "sheet_rows", "sheet_id": sheet_id},
            params={"request": {"url": "https://api.test.example/items"}},
            idempotency_key="http-exact-consent",
        ).model_dump(mode="json", exclude_none=True)

        def run(body):
            return run_action_spec(project, body, project_id="p", router=router)

        before = tuple(project.db.iterdump())
        challenge = run(request)
        assert challenge.status == "needs_confirmation", challenge.errors
        error = challenge.errors[0]
        assert error.code == "external_cost_requires_confirmation"
        assert error.field == "confirmation"
        assert error.details["estimate"]["cost"] is None
        assert error.details["estimate"]["cost_source"] == "unknown"
        token = error.details["promise_set_hash"]
        assert token != "0" * 64
        assert tuple(project.db.iterdump()) == before
        assert not calls

        wrong = run({**request, "confirmation": "0" * 64})
        assert wrong.status == "needs_confirmation"
        assert wrong.errors[0].details["promise_set_hash"] == token
        assert tuple(project.db.iterdump()) == before
        assert not calls

        confirmed = run({**request, "confirmation": token})
        assert confirmed.status == "completed", confirmed.errors
        assert len(calls) == 1
        after = tuple(project.db.iterdump())
        replay = run(request)
        assert replay.receipt_id == confirmed.receipt_id
        assert tuple(project.db.iterdump()) == after
        assert len(calls) == 1
    finally:
        asyncio.run(router._client.aclose())
        project.close()
