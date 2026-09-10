from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

import pytest
from fastapi.testclient import TestClient
from pydantic import AfterValidator, BaseModel

from action_test_helpers import run_typed_map_request
from frisket.actions.core import ActionCategory, RegisteredAction, action, model_rows
from frisket.actions.model_rows import RichSource
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    GeoPoint,
    ModelPrompt,
    ModelRef,
    Row,
)
from frisket.ai.llm import LLMError, LLMRequest, LLMResponse, ModelRouter, ResponseCache
from frisket.ai.llm.cache import request_key
from frisket.contracts.action import ActionResult
from frisket.engine.executor import ExecutorDeps
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.store import Project
from frisket.engine.store.execution_routes import instance_principal
from frisket.engine.store.media_blobs import MediaBlobStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.execution.consent_coverage import ConsentCoverage
from frisket.server.app import create_app
from http_test_helpers import drain_queue


PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
    b"\x00\x00\x00\x0bIDATx\xdac\xfc\xff\x1f\x00\x03\x03"
    b"\x02\x00\xef\xbf\xa7\xdb\x00\x00\x00\x00IEND\xaeB`\x82"
)


class _ReplyAdapter:
    def __init__(self, requests: list[LLMRequest]) -> None:
        self.requests = requests

    async def complete(self, request: LLMRequest, client: Any) -> LLMResponse:
        del client
        self.requests.append(request)
        output = next(iter((request.schema or {})["properties"]))
        data = {output: "Ada"}
        return LLMResponse(
            content=json.dumps(data),
            data=data,
            tokens_in=12,
            tokens_out=3,
            cost=0.001,
            model=request.model,
        )


class _FailingAdapter:
    async def complete(self, request: LLMRequest, client: Any) -> LLMResponse:
        del request, client
        raise LLMError("provider unavailable")


class _ValueAdapter:
    def __init__(self, requests: list[LLMRequest], value: str) -> None:
        self.requests = requests
        self.value = value

    async def complete(self, request: LLMRequest, client: Any) -> LLMResponse:
        del client
        self.requests.append(request)
        output = next(iter((request.schema or {})["properties"]))
        data = {output: self.value}
        return LLMResponse(
            content=json.dumps(data),
            data=data,
            tokens_in=12,
            tokens_out=3,
            cost=0.001,
            model=request.model,
        )


class _DataAdapter:
    def __init__(self, requests: list[LLMRequest], data: dict[str, Any]) -> None:
        self.requests = requests
        self.data = data

    async def complete(self, request: LLMRequest, client: Any) -> LLMResponse:
        del client
        self.requests.append(request)
        return LLMResponse(
            content=json.dumps(self.data),
            data=self.data,
            tokens_in=12,
            tokens_out=3,
            cost=0.001,
            model=request.model,
        )


def _no_surrounding_whitespace(value: str) -> str:
    if value != value.strip():
        raise ValueError("answer must not contain surrounding whitespace")
    return value


class _ThirdPartyParams(ActionParams):
    source: RichSource
    model: ModelRef


class _ThirdPartyOutput(BaseModel):
    answer: Annotated[str, AfterValidator(_no_surrounding_whitespace)]


class _StructuredProviderOutput(BaseModel):
    point: GeoPoint
    tags: list[str]
    metadata: dict[str, int]


def _third_party_renderer(
    params: _ThirdPartyParams, row: Row
) -> ModelPrompt[_ThirdPartyOutput]:
    del params, row
    return ModelPrompt(messages=({"role": "user", "content": "Answer."},))


_THIRD_PARTY_ACTION = RegisteredAction(
    "map.third_party_model_rows",
    action(
        name="third_party_model_rows",
        title="Third-party model rows",
        description="Exercise the portable ModelRows contract.",
        category=ActionCategory.TEXT,
        run=model_rows(_third_party_renderer),
    ),
)


def _structured_renderer(
    params: _ThirdPartyParams, row: Row
) -> ModelPrompt[_StructuredProviderOutput]:
    del params, row
    return ModelPrompt(messages=({"role": "user", "content": "Structure."},))


_STRUCTURED_ACTION = RegisteredAction(
    "map.structured_model_rows",
    action(
        name="structured_model_rows",
        title="Structured model rows",
        description="Exercise structured ModelRows outputs.",
        category=ActionCategory.TEXT,
        run=model_rows(_structured_renderer),
    ),
)


def _router(requests: list[LLMRequest]) -> ModelRouter:
    router = ModelRouter(
        keys={"anthropic": "test-only"},
        cache=None,
        cache_mode="off",
        use_env_keys=False,
    )
    router._adapters["anthropic"] = _ReplyAdapter(requests)  # noqa: SLF001
    return router


def _failing_router() -> ModelRouter:
    router = ModelRouter(
        keys={"anthropic": "test-only"},
        cache=None,
        cache_mode="off",
        use_env_keys=False,
    )
    router._adapters["anthropic"] = _FailingAdapter()  # noqa: SLF001
    return router


def _value_router(requests: list[LLMRequest], value: str) -> ModelRouter:
    router = ModelRouter(
        keys={"anthropic": "test-only"},
        cache=None,
        cache_mode="off",
        use_env_keys=False,
    )
    router._adapters["anthropic"] = _ValueAdapter(requests, value)  # noqa: SLF001
    return router


def _data_router(requests: list[LLMRequest], data: dict[str, Any]) -> ModelRouter:
    router = ModelRouter(
        keys={"anthropic": "test-only"},
        cache=None,
        cache_mode="off",
        use_env_keys=False,
    )
    router._adapters["anthropic"] = _DataAdapter(requests, data)  # noqa: SLF001
    return router


def _zero_consent_coverage(project: Project) -> ConsentCoverage:
    return ConsentCoverage(instance_principal(project), Decimal("0"))


def _zero_consent_deps(project: Project) -> ExecutorDeps:
    return ExecutorDeps(consent_coverage=_zero_consent_coverage(project))


def _pin_zero_preapproval(client: TestClient) -> None:
    workspace = client.app.state.workspace

    def executor_deps(project_id: str, _request: Any) -> ExecutorDeps:
        return _zero_consent_deps(workspace.get(project_id))

    workspace.executor_deps_factory = executor_deps


def _seed(project: Project) -> tuple[int, list[int]]:
    sheet_id = project.add_sheet("Filings")
    digest = project.add_blob(PNG_1X1, filename="scan.png", mime="image/png")
    columns = {
        "body": project.add_column(sheet_id, "body", type="text"),
        "scan": project.add_column(sheet_id, "scan", type="image"),
    }
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "body": "The filing names Ada.",
                "scan": MediaBlobStore.media_cell(digest, mime="image/png"),
            },
            {"body": None, "scan": None},
        ],
        columns,
    )
    return sheet_id, row_ids


def _request(
    sheet_id: int,
    *,
    row_ids: list[int] | None = None,
    confirmation: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "action_id": "map.ask",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": {"text": "Document: {{body}} {{scan}} {{body}}"},
            "model": "anthropic/claude-haiku-4-5",
            "question": "Who is named?",
            "context": "Rows are filings.",
        },
        "output_names": {"answer": "person"},
        "idempotency_key": "model-rows@1",
    }
    if row_ids is not None:
        body["scope"]["row_ids"] = row_ids
    if confirmation is not None:
        body["confirmation"] = confirmation
    return body


def _third_party_request(
    sheet_id: int,
    *,
    source: dict[str, str] | list[str],
    row_ids: list[int] | None = None,
    confirmation: str | None = None,
    idempotency_key: str = "third-party@1",
    action: RegisteredAction = _THIRD_PARTY_ACTION,
    output_names: dict[str, str] | None = None,
) -> ActionRequest:
    return ActionRequest.model_validate(
        {
            "action_id": action.action_id,
            "scope": {
                "kind": "sheet_rows",
                "sheet_id": sheet_id,
                **({"row_ids": row_ids} if row_ids is not None else {}),
            },
            "params": {
                "source": source,
                "model": "anthropic/claude-haiku-4-5",
            },
            "output_names": output_names or {"answer": "answer"},
            "idempotency_key": idempotency_key,
            **({"confirmation": confirmation} if confirmation is not None else {}),
        }
    )


def _run_third_party(
    project: Project,
    request: ActionRequest,
    router: ModelRouter,
    action: RegisteredAction = _THIRD_PARTY_ACTION,
) -> ActionResult:
    from frisket.engine.executor.map_rows_action import run_typed_map_rows_action
    from frisket.engine.runner import MapRunner
    from frisket.execution.attempt_authority import UnroutedOnlyAuthority

    return run_typed_map_rows_action(
        project,
        "project-1",
        BoundTypedActionRequest.bind(action, request),
        router,
        lambda target, selected: MapRunner(
            target,
            selected or router,
            authority=UnroutedOnlyAuthority(target),
            consent_coverage=_zero_consent_coverage(target),
        ),
    )


def test_third_party_model_rows_literal_source_refuses_before_paid_dispatch(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "literal.frisket")
    requests: list[LLMRequest] = []
    try:
        sheet_id, _ = _seed(project)
        request = _third_party_request(sheet_id, source={"text": "literal only"})
        try:
            _run_third_party(project, request, _value_router(requests, "Ada"))
        except ValueError as error:
            assert "reference at least one column" in str(error)
        else:
            raise AssertionError("literal-only paid prompt was accepted")
        assert requests == []
    finally:
        project.close()


def test_model_rows_applies_functional_output_validator_before_publication(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "functional-validator.frisket")
    requests: list[LLMRequest] = []
    try:
        sheet_id, row_ids = _seed(project)
        request = _third_party_request(sheet_id, source=["body"], row_ids=[row_ids[0]])
        router = _value_router(requests, " Ada ")
        gated = _run_third_party(project, request, router)
        assert gated.status == "needs_confirmation"
        assert requests == []

        confirmed = _third_party_request(
            sheet_id,
            source=["body"],
            row_ids=[row_ids[0]],
            confirmation=gated.errors[0].details["promise_set_hash"],
        )
        result = _run_third_party(project, confirmed, router)

        assert result.status == "failed"
        assert len(requests) == 1
        assert set((requests[0].schema or {})["properties"]) == {"answer"}
        output = next(
            column
            for column in project.columns(sheet_id, include_hidden=True)
            if column["name"] == "answer"
        )
        assert project.get_values(sheet_id, int(output["id"]), row_ids=row_ids) == {
            row_ids[0]: None,
            row_ids[1]: None,
        }
        receipt = ReceiptStore(project).parsed_by_id(str(result.receipt_id))
        assert receipt is not None
        assert receipt.provider_use[0]["model_call_count"] == 1
        assert receipt.provider_use[0]["cost_actual"] == 0.001
    finally:
        project.close()


def test_model_rows_publishes_structured_and_json_output_fields(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "structured-output.frisket")
    requests: list[LLMRequest] = []
    try:
        sheet_id, row_ids = _seed(project)
        outputs = {"point": "point", "tags": "tags", "metadata": "metadata"}
        request = _third_party_request(
            sheet_id,
            source=["body"],
            row_ids=[row_ids[0]],
            action=_STRUCTURED_ACTION,
            output_names=outputs,
            idempotency_key="structured@1",
        )
        data = {
            "point": {"lat": 40.6405, "lon": -73.9681},
            "tags": ["filing", "person"],
            "metadata": {"mentions": 1},
        }
        router = _data_router(requests, data)
        gated = _run_third_party(project, request, router, _STRUCTURED_ACTION)
        confirmed = _third_party_request(
            sheet_id,
            source=["body"],
            row_ids=[row_ids[0]],
            confirmation=gated.errors[0].details["promise_set_hash"],
            action=_STRUCTURED_ACTION,
            output_names=outputs,
            idempotency_key="structured@1",
        )

        result = _run_third_party(project, confirmed, router, _STRUCTURED_ACTION)

        assert result.status == "completed", result.errors
        assert len(requests) == 1
        columns = {column["name"]: column for column in project.columns(sheet_id)}
        assert (
            project.get_values(
                sheet_id, int(columns["point"]["id"]), row_ids=[row_ids[0]]
            )[row_ids[0]]
            == data["point"]
        )
        assert (
            project.get_values(
                sheet_id, int(columns["tags"]["id"]), row_ids=[row_ids[0]]
            )[row_ids[0]]
            == data["tags"]
        )
        assert (
            project.get_values(
                sheet_id, int(columns["metadata"]["id"]), row_ids=[row_ids[0]]
            )[row_ids[0]]
            == data["metadata"]
        )
    finally:
        project.close()


def test_model_rows_direct_confirmation_multimodal_receipt_and_replay(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "model-rows.frisket")
    requests: list[LLMRequest] = []
    try:
        sheet_id, row_ids = _seed(project)
        body = _request(sheet_id, row_ids=[row_ids[0]])
        body["params"].update(
            {
                "source": {
                    "text": "private-template-marker {{body}} {{scan}} {{body}}"
                },
                "question": "private-question-marker",
                "context": "private-context-marker",
            }
        )
        gated = run_typed_map_request(
            project,
            body,
            project_id="project-1",
            deps=_zero_consent_deps(project),
        )
        assert gated.status == "needs_confirmation"
        assert requests == []
        promise_hash = gated.errors[0].details["promise_set_hash"]

        request = ActionRequest.model_validate({**body, "confirmation": promise_hash})
        from frisket.engine.executor.map_rows_action import run_typed_map_rows_action
        from frisket.engine.runner import MapRunner
        from frisket.execution.attempt_authority import UnroutedOnlyAuthority

        router = _router(requests)
        result = run_typed_map_rows_action(
            project,
            "project-1",
            BoundTypedActionRequest.bind(ACTION_REGISTRY.get("map.ask"), request),
            router,
            lambda target, selected: MapRunner(
                target,
                selected or router,
                authority=UnroutedOnlyAuthority(target),
                consent_coverage=_zero_consent_coverage(target),
            ),
        )
        assert result.status == "completed", result.errors
        assert [output.name for output in result.outputs] == ["person"]
        assert len(requests) == 1
        assert set((requests[0].schema or {})["properties"]) == {"answer"}
        assert [part["type"] for part in requests[0].messages[1]["content"]] == [
            "text",
            "image",
            "text",
        ]
        assert (
            "private-template-marker The filing names Ada."
            in requests[0].messages[1]["content"][0]["text"]
        )

        receipt = ReceiptStore(project).parsed_by_id(str(result.receipt_id))
        assert receipt is not None
        assert receipt.provider_use[0]["provider"] == "anthropic"
        assert receipt.provider_use[0]["model_call_count"] == 1
        assert receipt.outputs[0].ref["role"] == "answer"
        assert receipt.outputs[0].ref["name"] == "person"
        assert [item.name for item in receipt.inputs] == [
            "column.body",
            "column.scan",
        ]
        assert [item.ref["name"] for item in receipt.inputs] == ["body", "scan"]
        durable = receipt.model_dump_json()
        assert "private-template-marker" not in durable
        assert "private-question-marker" not in durable
        assert "private-context-marker" not in durable

        replay = run_typed_map_rows_action(
            project,
            "project-1",
            BoundTypedActionRequest.bind(ACTION_REGISTRY.get("map.ask"), request),
            router,
            lambda target, selected: MapRunner(
                target,
                selected or router,
                authority=UnroutedOnlyAuthority(target),
                consent_coverage=_zero_consent_coverage(target),
            ),
        )
        assert replay.receipt_id == result.receipt_id
        assert len(requests) == 1
    finally:
        project.close()


def test_model_rows_direct_skips_empty_references_in_labeled_template(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "labeled-empty-direct.frisket")
    requests: list[LLMRequest] = []
    try:
        sheet_id, row_ids = _seed(project)
        body = _request(sheet_id, row_ids=row_ids)
        gated = run_typed_map_request(
            project,
            body,
            project_id="project-1",
            deps=_zero_consent_deps(project),
        )
        request = ActionRequest.model_validate(
            {**body, "confirmation": gated.errors[0].details["promise_set_hash"]}
        )
        router = _router(requests)

        from frisket.engine.executor.map_rows_action import run_typed_map_rows_action
        from frisket.engine.runner import MapRunner
        from frisket.execution.attempt_authority import UnroutedOnlyAuthority

        result = run_typed_map_rows_action(
            project,
            "project-1",
            BoundTypedActionRequest.bind(ACTION_REGISTRY.get("map.ask"), request),
            router,
            lambda target, selected: MapRunner(
                target,
                selected or router,
                authority=UnroutedOnlyAuthority(target),
                consent_coverage=_zero_consent_coverage(target),
            ),
        )
        assert result.status == "completed", result.errors
        assert len(requests) == 1
    finally:
        project.close()


def test_model_rows_template_rejects_incompatible_referenced_type(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "bad-template.frisket")
    try:
        sheet_id = project.add_sheet("Places")
        project.add_column(sheet_id, "point", type="geo_point")
        bound = BoundTypedActionRequest.bind(
            ACTION_REGISTRY.get("map.ask"),
            ActionRequest.model_validate(
                {
                    **_request(sheet_id),
                    "params": {
                        **_request(sheet_id)["params"],
                        "source": {"text": "{{point}}"},
                    },
                }
            ),
        )
        try:
            build_typed_map_rows_plan(project, bound)
        except ValueError as error:
            assert getattr(error, "code", None) == "invalid_input_ref"
            assert getattr(error, "details")["columns"][0]["name"] == "point"
        else:
            raise AssertionError("incompatible template source was accepted")
    finally:
        project.close()


def test_model_rows_all_failed_receipt_keeps_complete_run_counts(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "failed.frisket")
    try:
        sheet_id, row_ids = _seed(project)
        body = _request(sheet_id, row_ids=[row_ids[0]])
        gated = run_typed_map_request(
            project,
            body,
            project_id="project-1",
            deps=_zero_consent_deps(project),
        )
        confirmation = gated.errors[0].details["promise_set_hash"]
        request = ActionRequest.model_validate({**body, "confirmation": confirmation})
        bound = BoundTypedActionRequest.bind(ACTION_REGISTRY.get("map.ask"), request)
        from frisket.engine.executor.map_rows_action import run_typed_map_rows_action
        from frisket.engine.runner import MapRunner
        from frisket.execution.attempt_authority import UnroutedOnlyAuthority

        router = _failing_router()
        result = run_typed_map_rows_action(
            project,
            "project-1",
            bound,
            router,
            lambda target, selected: MapRunner(
                target,
                selected or router,
                authority=UnroutedOnlyAuthority(target),
                consent_coverage=_zero_consent_coverage(target),
            ),
        )

        assert result.status == "failed"
        receipt = ReceiptStore(project).parsed_by_id(str(result.receipt_id))
        assert receipt is not None
        assert receipt.errors[0].details == {
            "total_rows": 1,
            "completed_rows": 1,
            "failed_rows": 1,
        }
    finally:
        project.close()


def test_model_rows_live_validation_rejects_incompatible_template_type(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = client.post("/api/projects", json={"name": "Places"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Places")
    project.add_column(sheet_id, "point", type="geo_point")

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/validate-params",
        json={
            "action": {
                "action_id": "map.ask",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {
                    "source": {"text": "{{point}}"},
                    "model": "anthropic/claude-haiku-4-5",
                    "question": "Where?",
                },
            }
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["diagnostics"] == {
        "source": {
            "ok": False,
            "message": "referenced input columns have incompatible types",
        }
    }


def test_model_rows_queued_confirmation_and_empty_row_skip(tmp_path: Path) -> None:
    requests: list[LLMRequest] = []
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            router=_router(requests),
            run_status_grace_seconds=3600,
        )
    )
    _pin_zero_preapproval(client)
    project_id = client.post("/api/projects", json={"name": "Model rows"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id, row_ids = _seed(project)

    body = _request(sheet_id, row_ids=row_ids)
    gated_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run", json=body
    )
    assert gated_response.status_code == 402
    gated = ActionResult.model_validate(gated_response.json())
    promise_hash = gated.errors[0].details["promise_set_hash"]

    confirmed = _request(sheet_id, row_ids=row_ids, confirmation=promise_hash)
    response = client.post(f"/api/projects/{project_id}/actions/v1/run", json=confirmed)
    assert response.status_code == 200, response.text
    queued = ActionResult.model_validate(response.json())
    assert queued.status == "queued"
    drain_queue(client)

    receipt = ReceiptStore(project).parsed_by_id(str(queued.receipt_id))
    assert receipt is not None and receipt.status == "completed"
    assert len(requests) == 1
    output = next(
        column for column in project.columns(sheet_id) if column["name"] == "person"
    )
    assert project.get_values(sheet_id, int(output["id"]), row_ids=row_ids) == {
        row_ids[0]: "Ada",
        row_ids[1]: None,
    }


def test_queued_typed_summarize_can_backfill_new_row_into_exact_output(
    tmp_path: Path,
) -> None:
    requests: list[LLMRequest] = []
    client = TestClient(create_app(tmp_path / "workspace", router=_router(requests)))
    _pin_zero_preapproval(client)
    project_id = client.post("/api/projects", json={"name": "Backfill"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id, row_ids = _seed(project)
    body_column = next(
        int(column["id"])
        for column in project.columns(sheet_id)
        if column["name"] == "body"
    )
    body = {
        "action_id": "map.summarize",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["body"],
            "model": "anthropic/claude-haiku-4-5",
            "preset": "paragraph",
        },
        "output_names": {"summary": "digest"},
        "idempotency_key": "queued-summary-before-backfill@1",
    }
    gated = ActionResult.model_validate(
        client.post(f"/api/projects/{project_id}/actions/v1/run", json=body).json()
    )
    body["confirmation"] = gated.errors[0].details["promise_set_hash"]
    queued = ActionResult.model_validate(
        client.post(f"/api/projects/{project_id}/actions/v1/run", json=body).json()
    )
    drain_queue(client)
    assert queued.run_id is not None
    summary = next(
        column for column in project.columns(sheet_id) if column["name"] == "digest"
    )
    summary_id = int(summary["id"])
    project.db.execute(
        "UPDATE columns SET name='renamed_digest' WHERE id=?", (summary_id,)
    )
    project.db.commit()
    [new_row_id] = project.add_rows(
        sheet_id,
        [{"body": "A newly added filing."}],
        {"body": body_column},
    )
    backfill = {
        "action_id": "run.backfill",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"column": "renamed_digest"},
        "idempotency_key": "typed-summary-backfill@1",
    }
    challenge = ActionResult.model_validate(
        client.post(f"/api/projects/{project_id}/actions/v1/run", json=backfill).json()
    )
    assert challenge.status == "needs_confirmation", challenge.errors
    backfill["confirmation"] = challenge.errors[0].details["promise_set_hash"]
    completed = ActionResult.model_validate(
        client.post(f"/api/projects/{project_id}/actions/v1/run", json=backfill).json()
    )

    assert completed.status == "completed", completed.errors
    assert completed.run_id not in {None, queued.run_id}
    assert len(requests) == 2
    assert project.get_values(
        sheet_id,
        summary_id,
        row_ids=[*row_ids, new_row_id],
    ) == {row_ids[0]: "Ada", row_ids[1]: None, new_row_id: "Ada"}
    replay = ActionResult.model_validate(
        client.post(f"/api/projects/{project_id}/actions/v1/run", json=backfill).json()
    )
    assert replay.receipt_id == completed.receipt_id
    assert len(requests) == 2


@pytest.mark.parametrize(
    ("custom_owner", "tamper"),
    [
        (False, None),
        (True, None),
        (False, "params"),
        (False, "fact_run"),
        (False, "fact_hash"),
    ],
)
def test_typed_model_backfill_can_repeat_from_successor_generations(
    tmp_path: Path,
    custom_owner: bool,
    tamper: str | None,
) -> None:
    from frisket.engine.executor.actions import _default_map_runner_factory
    from frisket.engine.executor.run_backfill_action import run_typed_backfill_action
    from tests.actions.test_backfill import _bound

    requests: list[LLMRequest] = []
    client = TestClient(create_app(tmp_path / "workspace", router=_router(requests)))
    _pin_zero_preapproval(client)
    project_id = client.post("/api/projects", json={"name": "Successors"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id, row_ids = _seed(project)

    def zero_preapproval_factory(target, selected):
        runner = _default_map_runner_factory(target, selected)
        coverage = _zero_consent_coverage(target)
        runner.consent_coverage = coverage
        runner.authority = replace(runner.authority, consent_coverage=coverage)
        return runner

    def send(body):
        if custom_owner and body["action_id"] == "run.backfill":
            initial = _bound(sheet_id, key=body["idempotency_key"])
            bound = BoundTypedActionRequest.bind(
                initial.action,
                initial.request.model_copy(
                    update={
                        "scope": ActionRequest.model_validate(body).scope,
                        "params": {"selected": "chosen:" + body["params"]["column"]},
                        "confirmation": body.get("confirmation"),
                    }
                ),
            )
            return run_typed_backfill_action(
                project,
                project_id,
                bound,
                client.app.state.workspace.router_for(project),
                zero_preapproval_factory,
            )
        return ActionResult.model_validate(
            client.post(f"/api/projects/{project_id}/actions/v1/run", json=body).json()
        )

    def execute(body):
        result = send(body)
        assert result.status == "needs_confirmation", result.errors
        body["confirmation"] = result.errors[0].details["promise_set_hash"]
        result = send(body)
        if result.status == "queued":
            drain_queue(client)
        else:
            assert result.status == "completed", result.errors
        return result

    original = execute(_request(sheet_id, row_ids=[row_ids[0]]))
    output = next(
        column for column in project.columns(sheet_id) if column["name"] == "person"
    )
    prior_run_id = original.run_id
    for attempt in range(3):
        body = {
            "action_id": "run.backfill",
            "scope": {
                "kind": "sheet_rows",
                "sheet_id": sheet_id,
                "row_ids": [row_ids[0]],
            },
            "params": {"column": "person"},
            "idempotency_key": f"typed-successor-backfill@{attempt}",
        }
        completed = execute(body)
        assert completed.run_id not in {None, prior_run_id}
        head = ResultGenerationStore(project).read_cell_heads(int(output["id"]))[
            row_ids[0]
        ]
        assert head.run_id == completed.run_id
        assert head.value == "Ada"
        replay = send(body)
        assert replay.receipt_id == completed.receipt_id
        prior_run_id = completed.run_id

    if tamper is not None:
        if tamper == "params":
            stored = json.loads(
                project.db.execute(
                    "SELECT params FROM runs WHERE id=?",
                    (prior_run_id,),
                ).fetchone()[0]
            )
            stored["params"]["question"] = "A different paid question"
            project.db.execute(
                "UPDATE runs SET params=? WHERE id=?",
                (json.dumps(stored), prior_run_id),
            )
        else:
            stored = json.loads(
                project.db.execute(
                    "SELECT body FROM receipts WHERE id=?",
                    (completed.receipt_id,),
                ).fetchone()[0]
            )
            fact = next(
                item["ref"]
                for item in stored["evidence"]
                if item["ref"]["kind"] == "backfill_source_generation"
            )
            if tamper == "fact_run":
                fact["successor_run_id"] = original.run_id
            else:
                fact["successor_request_hash"] = "sha256:forged"
            project.db.execute(
                "UPDATE receipts SET body=? WHERE id=?",
                (json.dumps(stored), completed.receipt_id),
            )
        project.db.commit()
        paid_before = len(requests)
        refused = send(
            {**body, "idempotency_key": "tampered-successor", "confirmation": None}
        )
        assert refused.status == "failed"
        assert refused.errors[0].code == "invalid_run_params"
        assert len(requests) == paid_before


def test_typed_backfill_refuses_tampered_source_run_identity(tmp_path: Path) -> None:
    requests: list[LLMRequest] = []
    client = TestClient(create_app(tmp_path / "workspace", router=_router(requests)))
    _pin_zero_preapproval(client)
    project_id = client.post("/api/projects", json={"name": "Tamper"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id, row_ids = _seed(project)
    body = _request(sheet_id, row_ids=[row_ids[0]])
    challenge = ActionResult.model_validate(
        client.post(f"/api/projects/{project_id}/actions/v1/run", json=body).json()
    )
    body["confirmation"] = challenge.errors[0].details["promise_set_hash"]
    queued = ActionResult.model_validate(
        client.post(f"/api/projects/{project_id}/actions/v1/run", json=body).json()
    )
    drain_queue(client)
    assert queued.run_id is not None
    stored = json.loads(
        project.db.execute(
            "SELECT params FROM runs WHERE id=?", (queued.run_id,)
        ).fetchone()["params"]
    )
    stored["params"]["question"] = "A tampered question"
    project.db.execute(
        "UPDATE runs SET params=? WHERE id=?",
        (json.dumps(stored), queued.run_id),
    )
    project.db.commit()

    refused = ActionResult.model_validate(
        client.post(
            f"/api/projects/{project_id}/actions/v1/run",
            json={
                "action_id": "run.backfill",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet_id,
                    "row_ids": [row_ids[0]],
                },
                "params": {"column": "person"},
                "idempotency_key": "typed-tampered-backfill@1",
            },
        ).json()
    )

    assert refused.status == "failed"
    assert refused.errors[0].code == "invalid_run_params"
    assert len(requests) == 1


def test_model_rows_can_replace_compatible_cross_action_output_by_id(
    tmp_path: Path,
) -> None:
    requests: list[LLMRequest] = []
    client = TestClient(create_app(tmp_path / "workspace", router=_router(requests)))
    _pin_zero_preapproval(client)
    project_id = client.post("/api/projects", json={"name": "Replace"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id, row_ids = _seed(project)
    first = _request(sheet_id, row_ids=[row_ids[0]])
    first_challenge = ActionResult.model_validate(
        client.post(f"/api/projects/{project_id}/actions/v1/run", json=first).json()
    )
    first["confirmation"] = first_challenge.errors[0].details["promise_set_hash"]
    first_run = ActionResult.model_validate(
        client.post(f"/api/projects/{project_id}/actions/v1/run", json=first).json()
    )
    drain_queue(client)
    target = next(
        column for column in project.columns(sheet_id) if column["name"] == "person"
    )
    target_id = int(target["id"])
    replacement = {
        "action_id": "map.summarize",
        "scope": {
            "kind": "sheet_rows",
            "sheet_id": sheet_id,
            "row_ids": [row_ids[0]],
        },
        "params": {
            "source": ["body"],
            "model": "anthropic/claude-haiku-4-5",
            "preset": "one_line",
        },
        "output_names": {"summary": "person"},
        "replace_existing": True,
        "idempotency_key": "cross-action-replace@1",
    }
    challenge = ActionResult.model_validate(
        client.post(
            f"/api/projects/{project_id}/actions/v1/run", json=replacement
        ).json()
    )
    replacement["confirmation"] = challenge.errors[0].details["promise_set_hash"]
    queued = ActionResult.model_validate(
        client.post(
            f"/api/projects/{project_id}/actions/v1/run", json=replacement
        ).json()
    )
    drain_queue(client)

    assert queued.status == "queued"
    assert queued.run_id not in {None, first_run.run_id}
    assert [
        int(column["id"])
        for column in project.columns(sheet_id)
        if column["name"] == "person"
    ] == [target_id]
    persisted = json.loads(
        project.db.execute(
            "SELECT params FROM runs WHERE id=?", (queued.run_id,)
        ).fetchone()["params"]
    )
    assert persisted["output_names"] == {"summary": "person"}
    assert persisted["replace_existing"] is True
    assert persisted["output_target_preconditions"] == {"person": target_id}
    receipt = ReceiptStore(project).parsed_by_id(str(queued.receipt_id))
    assert receipt is not None and receipt.status == "completed"
    assert project.get_values(sheet_id, target_id, row_ids=[row_ids[0]]) == {
        row_ids[0]: "Ada"
    }
    head = ResultGenerationStore(project).read_cell_heads(target_id)[row_ids[0]]
    assert head.run_id == queued.run_id

    invalid = {**replacement, "idempotency_key": "replace-source@1"}
    invalid.pop("confirmation")
    invalid["output_names"] = {"summary": "body"}
    refused = ActionResult.model_validate(
        client.post(f"/api/projects/{project_id}/actions/v1/run", json=invalid).json()
    )
    assert refused.status == "failed"
    assert refused.errors[0].code == "output_column_exists"


def test_queued_model_rows_replacement_refuses_target_rename_before_worker(
    tmp_path: Path,
) -> None:
    requests: list[LLMRequest] = []
    client = TestClient(create_app(tmp_path / "workspace", router=_router(requests)))
    _pin_zero_preapproval(client)
    project_id = client.post("/api/projects", json={"name": "Replace drift"}).json()[
        "id"
    ]
    project = client.app.state.workspace.get(project_id)
    sheet_id, row_ids = _seed(project)
    original = _request(sheet_id, row_ids=[row_ids[0]])
    challenge = ActionResult.model_validate(
        client.post(f"/api/projects/{project_id}/actions/v1/run", json=original).json()
    )
    original["confirmation"] = challenge.errors[0].details["promise_set_hash"]
    first = ActionResult.model_validate(
        client.post(f"/api/projects/{project_id}/actions/v1/run", json=original).json()
    )
    drain_queue(client)
    assert (
        ReceiptStore(project).parsed_by_id(str(first.receipt_id)).status == "completed"
    )
    target_id = next(
        int(column["id"])
        for column in project.columns(sheet_id)
        if column["name"] == "person"
    )
    replacement = {
        "action_id": "map.summarize",
        "scope": {
            "kind": "sheet_rows",
            "sheet_id": sheet_id,
            "row_ids": [row_ids[0]],
        },
        "params": {
            "source": ["body"],
            "model": "anthropic/claude-haiku-4-5",
            "preset": "one_line",
        },
        "output_names": {"summary": "person"},
        "replace_existing": True,
        "idempotency_key": "queued-replace-drift@1",
    }
    challenge = ActionResult.model_validate(
        client.post(
            f"/api/projects/{project_id}/actions/v1/run", json=replacement
        ).json()
    )
    replacement["confirmation"] = challenge.errors[0].details["promise_set_hash"]
    queued = ActionResult.model_validate(
        client.post(
            f"/api/projects/{project_id}/actions/v1/run", json=replacement
        ).json()
    )
    project.db.execute(
        "UPDATE columns SET name='renamed_person' WHERE id=?", (target_id,)
    )
    project.db.commit()

    drain_queue(client)

    receipt = ReceiptStore(project).parsed_by_id(str(queued.receipt_id))
    job = client.app.state.workspace.queue.get(int(queued.job_id))
    assert receipt is not None and receipt.status == "failed", (
        None if job is None else job.error
    )
    assert receipt.errors[0].code == "stale_input"
    assert receipt.errors[0].field == "output_names"
    assert receipt.errors[0].message == (
        "output targets changed after this action was queued"
    )
    assert len(requests) == 1
    head = ResultGenerationStore(project).read_cell_heads(target_id)[row_ids[0]]
    assert head.run_id == first.run_id
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM output_column_claims "
            "WHERE receipt_id=? AND status='active'",
            (queued.receipt_id,),
        ).fetchone()[0]
        == 0
    )


def test_model_rows_metered_preview_requires_consent_before_dispatch(
    tmp_path: Path,
) -> None:
    requests: list[LLMRequest] = []
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            router=_router(requests),
            run_status_grace_seconds=3600,
        )
    )
    _pin_zero_preapproval(client)
    project_id = client.post("/api/projects", json={"name": "Model rows"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id, row_ids = _seed(project)

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/preview",
        json=_request(sheet_id, row_ids=[row_ids[0]]),
    )

    assert response.status_code == 402, response.text
    assert response.json()["error"]["code"] == "cost_gate"
    assert response.json()["error"]["needs_confirmation"] is True
    assert response.json()["error"]["details"]["promise_set_hash"]
    assert requests == []


def test_model_rows_all_empty_reports_source_field_direct_and_queued(
    tmp_path: Path,
) -> None:
    requests: list[LLMRequest] = []
    client = TestClient(create_app(tmp_path / "workspace", router=_router(requests)))
    project_id = client.post("/api/projects", json={"name": "Empty"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Empty")
    body_column = project.add_column(sheet_id, "body", type="text")
    row_ids = project.add_rows(sheet_id, [{}], {"body": body_column})
    body = _request(sheet_id, row_ids=row_ids)
    body["params"]["source"] = ["body"]

    direct = run_typed_map_request(project, body, project_id=project_id)
    assert direct.status == "failed"
    assert direct.errors[0].code == "empty_input_column"
    assert direct.errors[0].field == "params.source"

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json={**body, "idempotency_key": "empty-queued@1"},
    )
    assert response.status_code == 400, response.text
    assert response.json()["errors"][0]["code"] == "empty_input_column"
    assert response.json()["errors"][0]["field"] == "params.source"
    assert requests == []


def test_model_request_exact_replay_ignores_empty_rows(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "replay.frisket")
    cache = ResponseCache(tmp_path / "replay.cache.db")
    try:
        sheet_id, row_ids = _seed(project)
        request = ActionRequest.model_validate(_request(sheet_id, row_ids=row_ids))
        bound = BoundTypedActionRequest.bind(ACTION_REGISTRY.get("map.ask"), request)
        plan = build_typed_map_rows_plan(project, bound)
        spec = plan.spec_dict()
        columns = project.columns(sheet_id)
        column_ids = {str(column["name"]): int(column["id"]) for column in columns}
        column_types = {str(column["name"]): str(column["type"]) for column in columns}

        from frisket.engine.runner.validation import row_values

        values = row_values(
            project,
            plan.program,
            spec,
            column_ids,
            row_ids[0],
            column_types=column_types,
        )
        rendered = plan.program.render(values, spec)
        llm_request = LLMRequest(
            model=spec["model"],
            messages=rendered.messages,
            schema=rendered.schema,
            max_tokens=rendered.max_tokens,
        )
        cache.put(
            request_key(llm_request, plan.program.version),
            LLMResponse(
                content='{"answer":"Ada"}',
                data={"answer": "Ada"},
                tokens_in=1,
                tokens_out=1,
                cost=0.0,
                model=spec["model"],
            ),
        )
        router = ModelRouter(cache=cache, cache_mode="replay", use_env_keys=False)
        from frisket.engine.runner import MapRunner
        from frisket.execution.attempt_authority import UnroutedOnlyAuthority

        assert MapRunner(
            project, router, authority=UnroutedOnlyAuthority(project)
        ).exact_replay_available(spec, program=plan.program)
    finally:
        cache.close()
        project.close()
