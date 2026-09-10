from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

import asyncio
import copy
import json
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

import frisket.ops.media_metadata as metadata
from frisket.contracts.action import (
    ActionResult,
)
from frisket.contracts.action_validation import validate_action_spec
from frisket.engine.executor import ExecutorDeps
from frisket.engine.executor import action_reservations as action_runtime_reservations
from frisket.actions.system import typed_action_for_request
from frisket.engine.executor.map_rows_action import typed_request_hash
from frisket.engine.executor.queued_actions import (
    queued_v1_action_request,
)
from frisket.engine.jobs import RUN_PROJECT_KIND
from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.engine.store.media_blobs import media_cell
from frisket.engine.store.output_families import OutputFamilyStore
from frisket.engine.store.execution_routes import instance_principal
from frisket.execution.consent_coverage import ConsentCoverage
from frisket.actions.registry import ACTION_REGISTRY
from frisket.engine.runner import MapRunner
from frisket.engine.executor.document_convert import _BoundDocumentConverter
from frisket.ops.ocr_engines import OcrEngines
from helpers import replace_test_source_cell
from frisket.sdk.ops import transcribe_engines
from frisket.engine.runner import CostGate
from frisket.sdk.replay import (
    output_column_result_value_hash,
    output_column_value_hash,
)
from frisket.server.app import create_app
from http_test_helpers import (
    drain_queue,
    queued_python_run_spec,
    v1_action_from_canonical_run_spec,
)
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.result_generations import ResultGenerationStore


CSV = "note\nCall 212-555-0123\nNo phone\n"
CLASSIFY_CSV = "story\nBridge repairs delayed\nCouncil approves park\n"
EXTRACT_CSV = "note\nAda Lovelace founded the club\nGrace Hopper led the lab\n"
SUMMARY_CSV = (
    "article\nTariffs pressure aluminum makers\nAvocado importers face delays\n"
)
TRANSLATE_CSV = "statement,speaker\nBonjour le monde,Ada\nGracias por venir,Grace\n"
JUDGE_CSV = (
    "story,source_url\n"
    "City hall awarded a no-bid contract.,https://example.com/contract\n"
    "Road repairs finished under budget.,https://example.com/road\n"
)
NER_CSV = (
    "body,speaker\n"
    "Ada Lovelace wrote notes for the Analytical Engine,host\n"
    "Grace Hopper worked on COBOL,guest\n"
)
WEB_SEARCH_CSV = "country\nCanada\nMexico\n"
RESEARCH_ANSWER_CSV = (
    "company,topic\nAcme Corp,battery recall\nGlobex,export controls\n"
)
PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
    b"\x00\x00\x00\x0bIDATx\xdac\xfc\xff\x1f\x00\x03\x03"
    b"\x02\x00\xef\xbf\xa7\xdb\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _pin_zero_preapproval(client: TestClient) -> None:
    workspace = client.app.state.workspace

    def executor_deps(project_id: str, _request: Any) -> ExecutorDeps:
        return ExecutorDeps(
            consent_coverage=ConsentCoverage(
                instance_principal(workspace.get(project_id)), Decimal("0")
            )
        )

    workspace.executor_deps_factory = executor_deps


def _post_after_exact_confirmation(
    client: TestClient,
    path: str,
    *,
    action: dict[str, Any],
    params: dict[str, str] | None = None,
):
    """Exercise the real HTTP 402 -> exact-token retry protocol."""

    _pin_zero_preapproval(client)
    gated = client.post(path, params=params, json=action)
    assert gated.status_code == 402, gated.text
    gated_result = ActionResult.model_validate(gated.json())
    assert gated_result.status == "needs_confirmation"
    assert gated_result.errors
    details = gated_result.errors[0].details
    assert isinstance(details.get("estimate"), dict)
    promise_set_hash = details.get("promise_set_hash")
    assert isinstance(promise_set_hash, str) and promise_set_hash

    retry = copy.deepcopy(action)
    if "action_id" in retry:
        retry["confirmation"] = promise_set_hash
    else:
        retry["params"]["confirmed"] = True
        retry["params"]["consented_promise_set_hash"] = promise_set_hash
    return gated, client.post(path, params=params, json=retry)


def test_legacy_youtube_download_wire_kind_is_rejected_at_ingress() -> None:
    """The pre-rename wire kind is retired vocabulary: persisted bundles are
    rewritten at open time, and live clients must send the canonical kind."""
    body = {
        "schema_version": "frisket.action.v2",
        "kind": "media.youtube_download",
        "capabilities": ["project:write", "external:media_download"],
        "params": {
            "sheet_id": 1,
            "input_columns": ["url"],
            "media_type": "video",
            "output_name": "media",
        },
        "idempotency_key": "legacy-youtube-download",
    }

    validation = validate_action_spec(body)
    request = queued_v1_action_request(body)

    assert not validation.ok
    assert validation.error is not None
    assert validation.error.code == "unsupported_action_kind"
    assert request is None


def test_legacy_media_capture_url_wire_kind_is_rejected_at_ingress() -> None:
    body = {
        "schema_version": "frisket.action.v2",
        "kind": "media.capture_url",
        "capabilities": ["project:write", "external:url_capture"],
        "params": {
            "sheet_id": 12,
            "input_column": "source_url",
            "row_ids": [101, 102, 103],
            "output_prefix": "capture",
        },
        "idempotency_key": "media_capture_url_pages@sha256:deadbeef",
    }

    validation = validate_action_spec(body)
    request = queued_v1_action_request(body)

    assert not validation.ok
    assert validation.error is not None
    assert validation.error.code == "unsupported_action_kind"
    assert request is None


def test_ytdlp_download_wire_kind_uses_queued_placement(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id = client.post(
        "/api/projects", json={"name": "Download placement"}
    ).json()["id"]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={
            "file": (
                "videos.csv",
                "url\nhttps://example.com/video\n",
                "text/csv",
            )
        },
    )
    assert imported.status_code == 200, imported.text
    sheet_id = imported.json()["sheet_id"]

    action_body = {
        "action_id": "media.ytdlp_download",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": "url",
            "media_type": "video",
        },
        "output_names": {"video": "media"},
        "idempotency_key": "ytdlp-download@queued-placement",
    }
    _, response = _post_after_exact_confirmation(
        client,
        f"/api/projects/{project_id}/actions/v1/run",
        action=action_body,
    )

    assert response.status_code == 200, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "queued"
    assert result.action.kind == "media.ytdlp_download"
    assert result.run_id is not None
    assert result.job_id is not None
    job = client.app.state.workspace.queue.get(result.job_id)
    assert job is not None
    assert job.kind == RUN_PROJECT_KIND
    assert job.action_kind == "media.ytdlp_download"

    bound = typed_action_for_request(action_body)
    receipt_row = (
        client.app.state.workspace.get(project_id)
        .db.execute(
            "SELECT params_hash FROM receipts WHERE id=?",
            (result.receipt_id,),
        )
        .fetchone()
    )
    assert receipt_row is not None
    # Confirmation is outside typed request identity, so an exact-token retry
    # retains the first submission's receipt.
    assert receipt_row["params_hash"] == typed_request_hash(bound)

    replay = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=action_body,
    )
    assert replay.status_code == 200, replay.text
    replay_result = ActionResult.model_validate(replay.json())
    assert replay_result.status == "queued"
    assert replay_result.receipt_id == result.receipt_id
    assert replay_result.job_id == result.job_id


def test_v1_queued_action_dispatch_registry_matches_server_and_worker() -> None:

    # Migrated programs use the same request/worker dispatcher, reconstructed
    # from their canonical request rather than a second static recipe owner.
    model_actions = {
        "map.classify": _classify_action(1),
        "map.extract": _extract_action(1),
        "map.translate": _translate_action(1),
        "map.ner": _ner_action(1),
        "research.answer": _research_answer_action(1),
        "map.mcp_extract": {
            "action_id": "map.mcp_extract",
            "scope": {"kind": "sheet_rows", "sheet_id": 1},
            "params": {
                "source": ["company"],
                "model": "openai/gpt-5-mini",
                "fields": [{"name": "company_name", "type": "text"}],
                "mcp_server_ids": ["local-crm"],
            },
            "idempotency_key": "queued-registry:mcp",
        },
    }
    for kind in (
        *model_actions,
        "enrich.geocode",
        "enrich.census_demographics",
        "map.python",
        "media.fetch_url",
        "media.ytdlp_download",
        "web.capture_screenshot",
        "media.video_frames",
        "media.extract_faces",
        "media.to_markdown",
        "media.ocr",
        "media.transcribe",
    ):
        queued = queued_v1_action_request(
            model_actions[kind]
            if kind in model_actions
            else v1_action_from_canonical_run_spec(
                queued_python_run_spec(1, "source", "computed")
            )
            if kind == "map.python"
            else {
                "action_id": kind,
                "scope": {"kind": "sheet_rows", "sheet_id": 1},
                "params": {"source": "source"},
                "idempotency_key": f"queued-registry:{kind}",
            }
        )
        assert queued is not None
        entry = queued.entry
        assert entry.kind == kind
        assert (
            entry.params_model is ACTION_REGISTRY.get(kind).definition.run.params_model
        )
        assert entry.reserve_action is not None
        assert entry.cleanup_reservation is not None
        assert entry.mark_enqueued is not None
        assert entry.finalize_action is not None
        assert entry.pre_run_guard is not None
        assert queued.program is not None
        assert queued.program.consumes_resolution is (
            kind.startswith("enrich.")
            or kind in {"media.to_markdown", "media.ocr", "media.transcribe"}
        )
        assert queued.expected_params_hash
        assert entry.payload_keys == (
            "input_column_ids",
            "input_column_types",
            "output_names",
            "output_target_preconditions",
        ) + (
            ("row_source_snapshot",)
            if kind in {"media.to_markdown", "media.ocr", "media.transcribe"}
            else ()
        )

    semantic = queued_v1_action_request(
        {
            "action_id": "join.semantic",
            "scope": {"kind": "sheet_rows", "sheet_id": 1},
            "params": {"source": "name", "target": {"sheet_id": 2, "column": "name"}},
            "sheet_name": "Matches",
            "idempotency_key": "queued-registry:join.semantic",
        }
    )
    assert semantic is not None
    assert semantic.entry.kind == "join.semantic"
    assert (
        semantic.entry.params_model
        is ACTION_REGISTRY.get("join.semantic").definition.run.params_model
    )
    assert semantic.entry.pre_run_guard is not None
    assert semantic.entry.finalize_action is not None
    assert semantic.program.defer_generation_seal is True
    assert semantic.entry.payload_keys == (
        "semantic_join",
        "input_column_ids",
        "input_column_types",
        "output_names",
        "output_target_preconditions",
    )


def test_v1_queued_prepare_failures_use_action_specific_error_policy(
    tmp_path,
    monkeypatch,
) -> None:
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id, sheet_id, row_ids, _blobs = _seed_media_ocr_project(client)
    body = _media_ocr_action(
        sheet_id,
        row_ids,
        idempotency_key="v1-queued-prepare-error-policy@sha256:ocr",
    )
    ocr_request = queued_v1_action_request(body)
    assert ocr_request is not None
    entry = ocr_request.entry
    assert entry.map_error_code == "external_rows_failed"
    assert (
        entry.prepare_run_error(
            ocr_request.action, RuntimeError("prepare exploded")
        ).code
        == "external_rows_failed"
    )

    secret_error = entry.prepare_run_error(
        ocr_request.action,
        RuntimeError("api_key=checkpoint1b-queued-secret"),
    )
    assert secret_error.code == "external_rows_failed"
    assert "checkpoint1b-queued-secret" not in secret_error.message

    markdown_project_id, markdown_sheet_id, markdown_row_ids, _docs = (
        _seed_media_to_markdown_project(client)
    )
    markdown_body = _media_to_markdown_action(
        markdown_sheet_id,
        markdown_row_ids,
        idempotency_key="v1-queued-prepare-error-policy@sha256:markdown",
    )
    markdown_request = queued_v1_action_request(markdown_body)
    assert markdown_request is not None
    markdown_entry = markdown_request.entry
    assert (
        markdown_entry.prepare_cost_gate_error(
            markdown_request.action,
            CostGate(None),
        ).code
        == "external_cost_requires_confirmation"
    )

    # Both kinds under test consume execution resolution, so packet 10 routes
    # their preparation through prepare_admitted_run inside the publication
    # transaction; unrouted kinds (map.ner below) still reach prepare_run.
    # Assert the placement so a future move reds HERE, at the patch target,
    # instead of silently sending the injected failure down a dead method and
    # letting the enqueue succeed.
    assert (
        markdown_entry.requires_atomic_publication(
            markdown_request.params, program=markdown_request.program
        )
        is True
    )
    ocr_request = queued_v1_action_request(body)
    assert ocr_request is not None
    assert (
        entry.requires_atomic_publication(
            ocr_request.params, program=ocr_request.program
        )
        is True
    )

    def _fail_every_prepare(exc_factory) -> None:  # noqa: ANN001
        """Inject a prepare failure on whichever prepare seam the kind uses."""

        def boom(self, spec, **_kwargs):  # noqa: ANN001, ARG001
            raise exc_factory()

        for method in ("prepare_run", "prepare_admitted_run"):
            monkeypatch.setattr(
                f"frisket.server.action_enqueue.MapRunner.{method}",
                boom,
            )

    _fail_every_prepare(lambda: CostGate(None))
    cost_response = client.post(
        f"/api/projects/{markdown_project_id}/actions/v1/run",
        json=markdown_body,
    )
    # Typed provider work preserves the ordinary 402 confirmation envelope.
    assert cost_response.status_code == 402
    cost_payload = cost_response.json()
    assert cost_payload["status"] == "needs_confirmation"
    assert cost_payload["errors"][0]["code"] == "external_cost_requires_confirmation"
    assert cost_payload["errors"][0]["action_kind"] == "media.to_markdown"
    # A cost-gated refusal buys nothing and leaves nothing behind: the
    # publication transaction rolls back its receipt reservation and its
    # output-column claim.
    markdown_project = client.app.state.workspace.get(markdown_project_id)
    assert (
        markdown_project.db.execute(
            "SELECT COUNT(*) FROM receipts WHERE idempotency_key=?",
            (markdown_body["idempotency_key"],),
        ).fetchone()[0]
        == 0
    )
    assert (
        markdown_project.db.execute(
            "SELECT COUNT(*) FROM output_column_claims WHERE status='active'"
        ).fetchone()[0]
        == 0
    )

    _fail_every_prepare(lambda: RuntimeError("prepare exploded"))
    response = client.post(f"/api/projects/{project_id}/actions/v1/run", json=body)
    assert response.status_code == 400
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["errors"][0]["code"] == "external_rows_failed"
    assert payload["errors"][0]["action_kind"] == "media.ocr"


def test_v1_queued_llm_ner_uses_shared_cost_confirmation(
    tmp_path,
) -> None:
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    _pin_zero_preapproval(client)
    project_id, sheet_id = _seed_ner_project(client)
    body = _ner_action(
        sheet_id,
        idempotency_key="v1-queued-ner-cost-gate@sha256:stable",
    )
    body["params"].update(
        {
            "engine": "llm",
            "model": "openai/gpt-5-mini",
        }
    )
    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=body,
    )

    assert response.status_code == 402
    payload = response.json()
    assert payload["status"] == "needs_confirmation"
    assert payload["errors"][0]["code"] == "model_cost_requires_confirmation"
    assert payload["errors"][0]["action_kind"] == "map.ner"
    assert payload["errors"][0]["details"]["promise_set_hash"]
    assert payload["errors"][0]["details"]["estimate"]


class _FakeDDGS:
    seen: list[dict[str, object]] = []

    def text(self, query: str, max_results: int) -> list[dict[str, str]]:
        self.seen.append({"query": query, "max_results": max_results})
        return [
            {
                "title": f"{query} result {idx}",
                "href": f"https://example.test/search/{idx}?q={query.replace(' ', '+')}",
                "body": f"Snippet {idx} about {query}",
            }
            for idx in range(1, max_results + 1)
        ]


class _StubAdapter:
    def __init__(self, reply: dict[str, object] | None = None) -> None:
        self.reply = reply or {
            "beat": "accountability",
            "beat_confidence": 0.91,
            "beat_justification": "mentions a public decision",
        }
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        self.requests.append(req)
        return LLMResponse(
            content=json.dumps(self.reply),
            data=dict(self.reply),
            tokens_in=37,
            tokens_out=11,
            cost=0.002,
            model=req.model,
        )


class _SequenceAdapter:
    def __init__(self, replies: list[dict[str, object]]) -> None:
        self.replies = replies
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        reply = dict(self.replies[min(len(self.requests), len(self.replies) - 1)])
        self.requests.append(req)
        return LLMResponse(
            content=json.dumps(reply),
            data=reply,
            tokens_in=41,
            tokens_out=13,
            cost=0.002,
            model=req.model,
        )


def _stub_router(
    reply: dict[str, object] | None = None,
) -> tuple[ModelRouter, _StubAdapter]:
    router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
    adapter = _StubAdapter(reply)
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    return router, adapter


def _judge_router() -> tuple[ModelRouter, _SequenceAdapter]:
    router = ModelRouter(
        keys={"anthropic": "k", "openai": "k"}, cache=None, cache_mode="off"
    )
    adapter = _SequenceAdapter(
        [
            {"risk_score": 8},
            {"risk_score": 3},
            {
                "verdict": True,
                "judge_note": "The score cites a concrete contract.",
            },
            {
                "verdict": True,
                "judge_note": "The score is plausible for the source.",
            },
        ]
    )
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    router._adapters["openai"] = adapter  # noqa: SLF001
    return router, adapter


class _ResearchAdapter:
    """Scripts the agent's two-tool loop (the caller migration, ops/agent.py -- search/fetch as
    @agent.tool_plain functions, a plain-text response is the finish signal):
    odd calls request the ``search`` tool, even calls answer in plain text."""

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        self.requests.append(req)
        call_number = len(self.requests)
        if call_number % 2 == 1:
            query = f"official source {call_number}"
            return LLMResponse(
                content=None,
                data=None,
                tool_calls=[
                    {
                        "name": "search",
                        "args": {"query": query},
                        "id": f"call_{call_number}",
                    }
                ],
                tokens_in=90 + call_number,
                tokens_out=12,
                cost=0.004,
                model=req.model,
            )
        row_number = call_number // 2
        return LLMResponse(
            content=f"Cited answer {row_number}",
            data=None,
            tokens_in=90 + call_number,
            tokens_out=12,
            cost=0.004,
            model=req.model,
        )


def _research_router() -> tuple[ModelRouter, _ResearchAdapter]:
    router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
    adapter = _ResearchAdapter()
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    return router, adapter


class _NerSidecarResponse:
    status_code = 200
    text = "ok"

    def __init__(self, entities: list[dict[str, Any]]) -> None:
        self.entities = entities

    def json(self) -> dict[str, Any]:
        return {"results": [self.entities]}


class _NerSidecarHttp:
    is_closed = False

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, **kwargs: Any) -> _NerSidecarResponse:
        self.calls.append((url, kwargs))
        text = str((kwargs.get("json") or {}).get("texts", [""])[0])
        if "Ada" in text:
            return _NerSidecarResponse(
                [
                    {
                        "text": "Ada Lovelace",
                        "label": "person",
                        "start": 0,
                        "end": 12,
                        "score": 0.98,
                    }
                ]
            )
        return _NerSidecarResponse(
            [
                {
                    "text": "COBOL",
                    "label": "organization",
                    "start": 23,
                    "end": 28,
                    "score": 0.9,
                }
            ]
        )


def _ner_router_with_http(http: Any) -> ModelRouter:
    router = ModelRouter(cache=None, cache_mode="off")
    router._client = http  # noqa: SLF001
    return router


def _seed_project(client: TestClient) -> tuple[str, int]:
    project_id = client.post("/api/projects", json={"name": "V1 queued run"}).json()[
        "id"
    ]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("calls.csv", CSV, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    return project_id, imported.json()["sheet_id"]


def _seed_classify_project(client: TestClient) -> tuple[str, int]:
    project_id = client.post(
        "/api/projects", json={"name": "V1 queued classify"}
    ).json()["id"]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("stories.csv", CLASSIFY_CSV, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    return project_id, imported.json()["sheet_id"]


def _seed_extract_project(client: TestClient) -> tuple[str, int]:
    project_id = client.post(
        "/api/projects", json={"name": "V1 queued extract"}
    ).json()["id"]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("notes.csv", EXTRACT_CSV, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    return project_id, imported.json()["sheet_id"]


def _seed_summarize_project(client: TestClient) -> tuple[str, int]:
    project_id = client.post(
        "/api/projects", json={"name": "V1 queued summarize"}
    ).json()["id"]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("articles.csv", SUMMARY_CSV, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    return project_id, imported.json()["sheet_id"]


def _seed_translate_project(client: TestClient) -> tuple[str, int]:
    project_id = client.post(
        "/api/projects", json={"name": "V1 queued translate"}
    ).json()["id"]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("statements.csv", TRANSLATE_CSV, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    return project_id, imported.json()["sheet_id"]


def _seed_judge_project(client: TestClient) -> tuple[str, int]:
    project_id = client.post("/api/projects", json={"name": "V1 queued judge"}).json()[
        "id"
    ]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("judge.csv", JUDGE_CSV, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    return project_id, imported.json()["sheet_id"]


def _seed_ner_project(client: TestClient) -> tuple[str, int]:
    project_id = client.post("/api/projects", json={"name": "V1 queued NER"}).json()[
        "id"
    ]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("ner.csv", NER_CSV, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    return project_id, imported.json()["sheet_id"]


def _seed_web_search_project(client: TestClient) -> tuple[str, int]:
    project_id = client.post(
        "/api/projects", json={"name": "V1 queued web search"}
    ).json()["id"]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("countries.csv", WEB_SEARCH_CSV, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    return project_id, imported.json()["sheet_id"]


def _seed_research_answer_project(client: TestClient) -> tuple[str, int]:
    project_id = client.post(
        "/api/projects", json={"name": "V1 queued research answer"}
    ).json()["id"]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={
            "file": (
                "research.csv",
                RESEARCH_ANSWER_CSV,
                "text/csv",
            )
        },
    )
    assert imported.status_code == 200, imported.text
    return project_id, imported.json()["sheet_id"]


def _seed_media_transcribe_project(
    client: TestClient,
) -> tuple[str, int, list[int], list[str]]:
    project_id = client.post(
        "/api/projects", json={"name": "V1 queued media transcribe"}
    ).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Episodes")
    columns = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "media": project.add_column(sheet_id, "media", type="audio"),
    }
    blobs = [
        project.add_blob(
            b"RIFF0000WAVEfmt " + label.encode("ascii"),
            filename=f"{label}.wav",
            mime="audio/wav",
            source_url=f"https://cdn.example/{label}.wav",
            metadata=owned_media_metadata_document(
                probe={"duration_seconds": 0.5, "kind": "audio"}
            ),
        )
        for label in ("ep1", "ep2")
    ]
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "title": "Episode 1",
                "media": media_cell(
                    blobs[0],
                    mime="audio/wav",
                    filename="ep1.wav",
                ),
            },
            {
                "title": "Episode 2",
                "media": media_cell(
                    blobs[1],
                    mime="audio/wav",
                    filename="ep2.wav",
                ),
            },
        ],
        columns,
    )
    return project_id, sheet_id, row_ids, blobs


def _seed_media_ocr_project(
    client: TestClient,
) -> tuple[str, int, list[int], list[str]]:
    project_id = client.post(
        "/api/projects", json={"name": "V1 queued media OCR"}
    ).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Scans")
    columns = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "media": project.add_column(sheet_id, "media", type="image"),
    }
    blobs = [
        project.add_blob(
            PNG_1X1 + label.encode("ascii"),
            filename=f"{label}.png",
            mime="image/png",
            source_url=f"https://cdn.example/{label}.png",
            metadata=owned_media_metadata_document(
                probe={"width": 1, "height": 1, "kind": "image"}
            ),
        )
        for label in ("scan1", "scan2")
    ]
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "title": "Scan 1",
                "media": media_cell(
                    blobs[0],
                    mime="image/png",
                    filename="scan1.png",
                ),
            },
            {
                "title": "Scan 2",
                "media": media_cell(
                    blobs[1],
                    mime="image/png",
                    filename="scan2.png",
                ),
            },
        ],
        columns,
    )
    return project_id, sheet_id, row_ids, blobs


def _seed_media_to_markdown_project(
    client: TestClient,
) -> tuple[str, int, list[int], list[str]]:
    project_id = client.post(
        "/api/projects", json={"name": "V1 queued media to_markdown"}
    ).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Documents")
    columns = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "doc": project.add_column(sheet_id, "doc", type="file"),
    }
    blobs = [
        project.add_blob(
            f"<h1>{label}</h1><p>Document body.</p>".encode("utf-8"),
            filename=f"{label}.html",
            mime="text/html",
            source_url=f"https://docs.example/{label}.html",
            metadata=owned_media_metadata_document(probe={"kind": "document"}),
        )
        for label in ("report1", "report2")
    ]
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "title": "Report 1",
                "doc": media_cell(
                    blobs[0],
                    mime="text/html",
                    filename="report1.html",
                ),
            },
            {
                "title": "Report 2",
                "doc": media_cell(
                    blobs[1],
                    mime="text/html",
                    filename="report2.html",
                ),
            },
        ],
        columns,
    )
    return project_id, sheet_id, row_ids, blobs


def _regex_action(
    sheet_id: int,
    *,
    output_name: str = "phone",
    idempotency_key: str = "v1-http-queued-regex@sha256:stable",
) -> dict:
    return {
        "schema_version": "frisket.action.v2",
        "kind": "map.regex_extract",
        "capabilities": ["project:write"],
        "row_scope": {"sheet_id": sheet_id, "selector": {"kind": "all_rows"}},
        "params": {
            "input_columns": ["note"],
            "pattern": r"\d{3}-\d{3}-\d{4}",
            "output_name": output_name,
        },
        "idempotency_key": idempotency_key,
    }


def _classify_action(
    sheet_id: int,
    *,
    idempotency_key: str = "v1-http-queued-classify@sha256:stable",
) -> dict:
    return {
        "action_id": "map.classify",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["story"],
            "engine": "llm",
            "model": "anthropic/claude-haiku-4-5",
            "context": "Classify local-government news.",
            "fields": [
                {
                    "name": "beat",
                    "type": "category",
                    "labels": ["accountability", "infrastructure"],
                    "description": "Primary reporting beat.",
                }
            ],
            "include_justification": True,
            "include_confidence": True,
        },
        "idempotency_key": idempotency_key,
    }


def _extract_action(
    sheet_id: int,
    *,
    idempotency_key: str = "v1-http-queued-extract@sha256:stable",
) -> dict:
    return {
        "action_id": "map.extract",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["note"],
            "model": "anthropic/claude-haiku-4-5",
            "instruction": "Extract the named person and organization.",
            "context": "Rows are short biographical notes.",
            "fields": [
                {
                    "name": "person",
                    "type": "text",
                    "description": "Primary person named in the note.",
                },
                {
                    "name": "organization",
                    "type": "text",
                    "description": "Organization or group in the note.",
                },
            ],
        },
        "idempotency_key": idempotency_key,
    }


def _summarize_action(
    sheet_id: int,
    *,
    idempotency_key: str = "v1-http-queued-summarize@sha256:stable",
) -> dict:
    return {
        "action_id": "map.summarize",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["article"],
            "model": "anthropic/claude-haiku-4-5",
            "preset": "one_line",
            "instruction": "Summarize the article in one sentence.",
            "context": "Rows are short trade news snippets.",
        },
        "output_names": {"summary": "summary"},
        "idempotency_key": idempotency_key,
    }


def _translate_action(
    sheet_id: int,
    *,
    idempotency_key: str = "v1-http-queued-translate@sha256:stable",
) -> dict:
    return {
        "action_id": "map.translate",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["statement", "speaker"],
            "engine": "llm",
            "model": "anthropic/claude-haiku-4-5",
            "target_language": "English",
        },
        "idempotency_key": idempotency_key,
    }


def _judge_source_classify_action(sheet_id: int) -> dict:
    return {
        "action_id": "map.classify",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["story"],
            "engine": "llm",
            "model": "anthropic/claude-haiku-4-5",
            "context": "Score accountability risk for feature-tour judge tests.",
            "fields": [
                {
                    "name": "risk_score",
                    "type": "score",
                    "description": "Public accountability risk from 0 to 10.",
                }
            ],
        },
        "idempotency_key": "v1-http-queued-judge-source@sha256:stable",
    }


def _judge_action(
    sheet_id: int,
    *,
    idempotency_key: str = "v1-http-queued-judge@sha256:stable",
) -> dict:
    return {
        "action_id": "map.judge",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["story", "risk_score"],
            "judged_column": "risk_score",
            "model": "openai/gpt-5-mini",
            "guidelines": (
                "The risk score must be supported by concrete facts in the story."
            ),
        },
        "idempotency_key": idempotency_key,
    }


def _ner_action(
    sheet_id: int,
    *,
    idempotency_key: str = "v1-http-queued-ner@sha256:stable",
) -> dict:
    return {
        "action_id": "map.ner",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["body", "speaker"],
            "labels": ["person", "organization"],
            "threshold": 0.5,
            "engine": "gliner",
        },
        "idempotency_key": idempotency_key,
    }


def _web_search_action(
    sheet_id: int,
    *,
    idempotency_key: str = "v1-http-queued-web-search@sha256:stable",
) -> dict:
    return {
        "action_id": "research.web_search",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "query": {"text": "US tariff impacts on {{country}} economy 2026"},
            "max_results": 2,
        },
        "output_names": {"search_results": "search_results"},
        "idempotency_key": idempotency_key,
    }


def _research_answer_action(
    sheet_id: int,
    *,
    idempotency_key: str = "v1-http-queued-research-answer@sha256:stable",
) -> dict:
    return {
        "action_id": "research.answer",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "output_names": {"answer": "answer", "sources": "answer_sources"},
        "params": {
            "source": ["company", "topic"],
            "question": {"text": "Answer using cited public sources for this row."},
            "model": "anthropic/claude-haiku-4-5",
        },
        "idempotency_key": idempotency_key,
    }


def _media_transcribe_action(
    sheet_id: int,
    row_ids: list[int],
    *,
    idempotency_key: str = "v1-http-queued-media-transcribe@sha256:stable",
) -> dict:
    return {
        "action_id": "media.transcribe",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": row_ids},
        "params": {"source": "media", "engine": "faster_whisper"},
        "output_names": {
            "text": "transcript",
            "segments": "transcript_segments",
            "detected_language": "detected_language",
        },
        "idempotency_key": idempotency_key,
    }


def _media_extract_metadata_action(
    sheet_id: int,
    row_ids: list[int],
    *,
    idempotency_key: str = "v1-http-queued-media-metadata@sha256:stable",
    mode: str = "object",
    replace_existing: bool = False,
) -> dict:
    terminal = ACTION_REGISTRY.get("media.extract_metadata").definition.run
    params = terminal.params_model.model_validate(
        {"source": "media", "output_mode": mode}
    )
    output_names = {
        field.key: "meta" if mode == "object" else f"meta_{field.key}"
        for field in terminal.resolve_output_fields(params)
    }
    return {
        "action_id": "media.extract_metadata",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": row_ids},
        "params": {"source": "media", "output_mode": mode, "refresh": False},
        "output_names": output_names,
        "replace_existing": replace_existing,
        "idempotency_key": idempotency_key,
    }


def _media_ocr_action(
    sheet_id: int,
    row_ids: list[int],
    *,
    idempotency_key: str = "v1-http-queued-media-ocr@sha256:stable",
) -> dict:
    return {
        "action_id": "media.ocr",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": row_ids},
        "output_names": {"text": "ocr_text", "blocks": "ocr_text_blocks"},
        "params": {
            "source": "media",
            "engine": "rapidocr",
            "language": "en",
            "dpi": 180,
        },
        "idempotency_key": idempotency_key,
    }


def _media_to_markdown_action(
    sheet_id: int,
    row_ids: list[int],
    *,
    idempotency_key: str = "v1-http-queued-media-to-markdown@sha256:stable",
) -> dict:
    return {
        "action_id": "media.to_markdown",
        "scope": {
            "kind": "sheet_rows",
            "sheet_id": sheet_id,
            "row_ids": row_ids,
        },
        "params": {"source": "doc", "engine": "markitdown"},
        "output_names": {"markdown": "markdown"},
        "idempotency_key": idempotency_key,
    }


def test_v1_action_run_queues_maprunner_action_and_status_cancel_use_same_job(
    tmp_path,
) -> None:
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id, sheet_id = _seed_project(client)

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        json=v1_action_from_canonical_run_spec(
            queued_python_run_spec(sheet_id, "note", "phone")
        ),
    )
    assert response.status_code == 200, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "queued"
    assert result.action.kind == "map.python"
    assert result.run_id is not None
    assert result.job_id is not None
    assert result.receipt_id is not None

    job = client.app.state.workspace.queue.get(result.job_id)
    assert job is not None
    assert job.kind == RUN_PROJECT_KIND
    assert job.payload["project_id"] == project_id
    assert job.payload["run_id"] == result.run_id
    assert job.payload["spec"]["action_kind"] == "map.python"
    assert "recipe" not in job.payload["spec"]
    assert job.payload["spec"]["params"]["output_routes"][0]["name"] == "phone"
    assert job.payload["spec"]["output_names"] == {"phone": "phone"}
    assert job.payload["v1_receipt_id"] == result.receipt_id

    status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert status.status_code == 200, status.text
    public_status = status.json()["run"]["public_status"]
    assert public_status["status"] == "queued"
    assert public_status["action_kind"] == "map.python"
    assert public_status["queue"]["job_id"] == result.job_id
    assert public_status["queue"]["status"] == "queued"

    receipt_row = (
        client.app.state.workspace.get(project_id)
        .db.execute(
            "SELECT run_id, action_kind, status FROM receipts WHERE id=?",
            (result.receipt_id,),
        )
        .fetchone()
    )
    assert dict(receipt_row) == {
        "run_id": result.run_id,
        "action_kind": "map.python",
        "status": "queued",
    }

    drain_queue(client)
    completed_status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert completed_status.status_code == 200, completed_status.text
    assert completed_status.json()["run"]["public_status"]["status"] == "completed", (
        client.app.state.workspace.queue.get(result.job_id).error
    )
    completed_receipt = (
        client.app.state.workspace.get(project_id)
        .db.execute(
            "SELECT run_id, action_kind, status FROM receipts WHERE id=?",
            (result.receipt_id,),
        )
        .fetchone()
    )
    assert dict(completed_receipt) == {
        "run_id": result.run_id,
        "action_kind": "map.python",
        "status": "completed",
    }

    cancel_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        json=v1_action_from_canonical_run_spec(
            queued_python_run_spec(sheet_id, "note", "cancel_phone"),
            idempotency_key="v1-http-queued-python-cancel@sha256:stable",
        ),
    )
    assert cancel_response.status_code == 200, cancel_response.text
    cancel_result = ActionResult.model_validate(cancel_response.json())
    assert cancel_result.status == "queued"
    assert cancel_result.run_id is not None
    assert cancel_result.job_id is not None

    cancelled = client.post(
        f"/api/projects/{project_id}/actions/runs/{cancel_result.run_id}/cancel"
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["queue_job_id"] == cancel_result.job_id
    assert cancelled.json()["queue_cancelled"] is True


def test_v1_action_run_queues_map_classify_and_worker_finalizes_receipt(
    tmp_path,
) -> None:
    router, adapter = _stub_router()
    client = TestClient(
        create_app(tmp_path / "ws", router=router, run_status_grace_seconds=3600.0)
    )
    project_id, sheet_id = _seed_classify_project(client)

    _, response = _post_after_exact_confirmation(
        client,
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        action=_classify_action(sheet_id),
    )
    assert response.status_code == 200, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "queued"
    assert result.action.kind == "map.classify"
    assert result.run_id is not None
    assert result.job_id is not None
    assert result.receipt_id is not None
    assert adapter.requests == []

    job = client.app.state.workspace.queue.get(result.job_id)
    assert job is not None
    assert job.kind == RUN_PROJECT_KIND
    assert job.payload["project_id"] == project_id
    assert job.payload["run_id"] == result.run_id
    assert job.payload["spec"]["action_kind"] == "map.classify"
    assert "recipe" not in job.payload["spec"]
    assert job.payload["spec"]["params"]["fields"][0]["name"] == "beat"
    assert job.payload["v1_receipt_id"] == result.receipt_id
    assert job.payload["action_kind"] == "map.classify"

    status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert status.status_code == 200, status.text
    public_status = status.json()["run"]["public_status"]
    assert public_status["status"] == "queued"
    assert public_status["action_kind"] == "map.classify"
    assert public_status["action_kind"] == "map.classify"
    assert public_status["queue"]["job_id"] == result.job_id
    assert public_status["queue"]["status"] == "queued"

    receipt_row = (
        client.app.state.workspace.get(project_id)
        .db.execute(
            "SELECT run_id, action_kind, status FROM receipts WHERE id=?",
            (result.receipt_id,),
        )
        .fetchone()
    )
    assert dict(receipt_row) == {
        "run_id": result.run_id,
        "action_kind": "map.classify",
        "status": "queued",
    }

    replay = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        json=_classify_action(sheet_id),
    )
    assert replay.status_code == 200, replay.text
    replay_result = ActionResult.model_validate(replay.json())
    assert replay_result.status == "queued"
    assert replay_result.run_id == result.run_id
    assert replay_result.job_id == result.job_id
    assert replay_result.receipt_id == result.receipt_id
    assert adapter.requests == []

    drain_queue(client)
    assert len(adapter.requests) == 2
    run_row = (
        client.app.state.workspace.get(project_id)
        .db.execute(
            "SELECT params FROM runs WHERE id=?",
            (result.run_id,),
        )
        .fetchone()
    )
    assert json.loads(run_row["params"])["params"]["fields"][0]["name"] == "beat"
    completed_status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert completed_status.status_code == 200, completed_status.text
    assert completed_status.json()["run"]["public_status"]["status"] == "completed"
    completed_receipt = (
        client.app.state.workspace.get(project_id)
        .db.execute(
            "SELECT run_id, action_kind, status FROM receipts WHERE id=?",
            (result.receipt_id,),
        )
        .fetchone()
    )
    assert dict(completed_receipt) == {
        "run_id": result.run_id,
        "action_kind": "map.classify",
        "status": "completed",
    }


def test_v1_action_run_worker_exception_fails_reserved_receipt(
    tmp_path,
    monkeypatch,
) -> None:
    router, adapter = _stub_router()
    client = TestClient(
        create_app(tmp_path / "ws", router=router, run_status_grace_seconds=3600.0)
    )
    project_id, sheet_id = _seed_classify_project(client)
    action = _classify_action(
        sheet_id,
        idempotency_key="v1-http-queued-terminal-failure@sha256:stable",
    )

    _, response = _post_after_exact_confirmation(
        client,
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        action=action,
    )
    assert response.status_code == 200, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "queued"
    assert result.run_id is not None
    assert result.job_id is not None
    assert result.receipt_id is not None

    async def explode_run(self, spec, **_kwargs):  # noqa: ANN001, ARG001
        raise RuntimeError("worker exploded after queue reservation")

    monkeypatch.setattr("frisket.engine.jobs.runs.MapRunner.run", explode_run)

    drain_queue(client)
    assert adapter.requests == []

    failed_job = client.app.state.workspace.queue.get(result.job_id)
    assert failed_job is not None
    assert failed_job.status == "failed"
    assert "worker exploded after queue reservation" in (failed_job.error or "")

    failed_status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert failed_status.status_code == 200, failed_status.text
    public_status = failed_status.json()["run"]["public_status"]
    assert public_status["status"] == "failed"

    project = client.app.state.workspace.get(project_id)
    receipt_row = project.db.execute(
        "SELECT run_id, action_kind, status, body FROM receipts WHERE id=?",
        (result.receipt_id,),
    ).fetchone()
    assert receipt_row is not None
    assert receipt_row["run_id"] == result.run_id
    assert receipt_row["action_kind"] == "map.classify"
    assert receipt_row["status"] == "failed"
    receipt_body = json.loads(receipt_row["body"])
    assert receipt_body["run_id"] == result.run_id
    assert receipt_body["status"] == "failed"
    assert receipt_body["errors"][0]["code"] == "model_run_failed"
    assert receipt_body["errors"][0]["action_kind"] == "map.classify"
    assert (
        "worker exploded after queue reservation"
        in receipt_body["errors"][0]["message"]
    )

    replay = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        json=action,
    )
    assert replay.status_code == 400, replay.text
    replay_result = ActionResult.model_validate(replay.json())
    assert replay_result.status == "failed"
    assert replay_result.run_id == result.run_id
    assert replay_result.receipt_id == result.receipt_id
    assert replay_result.errors[0].code == "model_run_failed"


def test_v1_action_run_queues_map_extract_and_worker_finalizes_receipt(
    tmp_path,
) -> None:
    router, adapter = _stub_router(
        {"person": "Ada Lovelace", "organization": "Analytical Engine Club"}
    )
    client = TestClient(
        create_app(tmp_path / "ws", router=router, run_status_grace_seconds=3600.0)
    )
    project_id, sheet_id = _seed_extract_project(client)

    _, response = _post_after_exact_confirmation(
        client,
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        action=_extract_action(sheet_id),
    )
    assert response.status_code == 200, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "queued"
    assert result.action.kind == "map.extract"
    assert result.run_id is not None
    assert result.job_id is not None
    assert result.receipt_id is not None
    assert adapter.requests == []

    job = client.app.state.workspace.queue.get(result.job_id)
    assert job is not None
    assert job.kind == RUN_PROJECT_KIND
    assert job.payload["project_id"] == project_id
    assert job.payload["run_id"] == result.run_id
    assert job.payload["spec"]["action_kind"] == "map.extract"
    assert "recipe" not in job.payload["spec"]
    assert job.payload["spec"]["params"]["fields"][0]["name"] == "person"
    assert job.payload["v1_receipt_id"] == result.receipt_id
    assert job.payload["action_kind"] == "map.extract"
    assert set(job.payload["v1_input_column_ids"]) == {"note"}

    status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert status.status_code == 200, status.text
    public_status = status.json()["run"]["public_status"]
    assert public_status["status"] == "queued"
    assert public_status["action_kind"] == "map.extract"
    assert public_status["action_kind"] == "map.extract"
    assert public_status["queue"]["job_id"] == result.job_id
    assert public_status["queue"]["status"] == "queued"

    replay = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        json=_extract_action(sheet_id),
    )
    assert replay.status_code == 200, replay.text
    replay_result = ActionResult.model_validate(replay.json())
    assert replay_result.status == "queued"
    assert replay_result.run_id == result.run_id
    assert replay_result.job_id == result.job_id
    assert replay_result.receipt_id == result.receipt_id
    assert adapter.requests == []

    drain_queue(client)
    assert len(adapter.requests) == 2, client.app.state.workspace.queue.get(
        result.job_id
    ).error
    completed_status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert completed_status.status_code == 200, completed_status.text
    assert completed_status.json()["run"]["public_status"]["status"] == "completed"
    completed_receipt = (
        client.app.state.workspace.get(project_id)
        .db.execute(
            "SELECT run_id, action_kind, status FROM receipts WHERE id=?",
            (result.receipt_id,),
        )
        .fetchone()
    )
    assert dict(completed_receipt) == {
        "run_id": result.run_id,
        "action_kind": "map.extract",
        "status": "completed",
    }


def test_v1_action_run_queues_map_summarize_and_worker_finalizes_receipt(
    tmp_path,
) -> None:
    router, adapter = _stub_router({"summary": "The row describes trade disruption."})
    client = TestClient(
        create_app(tmp_path / "ws", router=router, run_status_grace_seconds=3600.0)
    )
    project_id, sheet_id = _seed_summarize_project(client)

    _, response = _post_after_exact_confirmation(
        client,
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        action=_summarize_action(sheet_id),
    )
    assert response.status_code == 200, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "queued"
    assert result.action.kind == "map.summarize"
    assert result.run_id is not None
    assert result.job_id is not None
    assert result.receipt_id is not None
    assert adapter.requests == []

    job = client.app.state.workspace.queue.get(result.job_id)
    assert job is not None
    assert job.kind == RUN_PROJECT_KIND
    assert job.payload["project_id"] == project_id
    assert job.payload["run_id"] == result.run_id
    assert job.payload["spec"]["action_kind"] == "map.summarize"
    assert "recipe" not in job.payload["spec"]
    assert job.payload["spec"]["output_names"] == {"summary": "summary"}
    assert job.payload["v1_receipt_id"] == result.receipt_id
    assert job.payload["action_kind"] == "map.summarize"
    # SDK-migrated: summarize's queued payload now threads input_column_ids (consistent
    # with every other list-input op); the worker re-derives the rich input refs at
    # finalize via capture, so the completed receipt is unchanged.
    assert "article" in job.payload["v1_input_column_ids"]

    status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert status.status_code == 200, status.text
    public_status = status.json()["run"]["public_status"]
    assert public_status["status"] == "queued"
    assert public_status["action_kind"] == "map.summarize"
    assert public_status["action_kind"] == "map.summarize"
    assert public_status["queue"]["job_id"] == result.job_id
    assert public_status["queue"]["status"] == "queued"

    replay = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        json=_summarize_action(sheet_id),
    )
    assert replay.status_code == 200, replay.text
    replay_result = ActionResult.model_validate(replay.json())
    assert replay_result.status == "queued"
    assert replay_result.run_id == result.run_id
    assert replay_result.job_id == result.job_id
    assert replay_result.receipt_id == result.receipt_id
    assert adapter.requests == []

    drain_queue(client)
    assert len(adapter.requests) == 2
    completed_status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert completed_status.status_code == 200, completed_status.text
    assert completed_status.json()["run"]["public_status"]["status"] == "completed"
    completed_receipt = (
        client.app.state.workspace.get(project_id)
        .db.execute(
            "SELECT run_id, action_kind, status FROM receipts WHERE id=?",
            (result.receipt_id,),
        )
        .fetchone()
    )
    assert dict(completed_receipt) == {
        "run_id": result.run_id,
        "action_kind": "map.summarize",
        "status": "completed",
    }


def test_v1_action_run_queues_map_translate_and_worker_finalizes_receipt(
    tmp_path,
) -> None:
    router, adapter = _stub_router(
        {
            "translation": "Hello world",
        }
    )
    client = TestClient(
        create_app(tmp_path / "ws", router=router, run_status_grace_seconds=3600.0)
    )
    project_id, sheet_id = _seed_translate_project(client)

    _, response = _post_after_exact_confirmation(
        client,
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        action=_translate_action(sheet_id),
    )
    assert response.status_code == 200, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "queued"
    assert result.action.kind == "map.translate"
    assert result.run_id is not None
    assert result.job_id is not None
    assert result.receipt_id is not None
    assert adapter.requests == []

    job = client.app.state.workspace.queue.get(result.job_id)
    assert job is not None
    assert job.kind == RUN_PROJECT_KIND
    assert job.payload["project_id"] == project_id
    assert job.payload["run_id"] == result.run_id
    assert job.payload["spec"]["action_kind"] == "map.translate"
    assert "recipe" not in job.payload["spec"]
    assert job.payload["spec"]["output_names"]["translation"] == "translation"
    assert job.payload["v1_receipt_id"] == result.receipt_id
    assert job.payload["action_kind"] == "map.translate"
    assert set(job.payload["v1_input_column_ids"]) == {"statement", "speaker"}

    status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert status.status_code == 200, status.text
    public_status = status.json()["run"]["public_status"]
    assert public_status["status"] == "queued"
    assert public_status["action_kind"] == "map.translate"
    assert public_status["action_kind"] == "map.translate"
    assert public_status["queue"]["job_id"] == result.job_id
    assert public_status["queue"]["status"] == "queued"

    replay = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        json=_translate_action(sheet_id),
    )
    assert replay.status_code == 200, replay.text
    replay_result = ActionResult.model_validate(replay.json())
    assert replay_result.status == "queued"
    assert replay_result.run_id == result.run_id
    assert replay_result.job_id == result.job_id
    assert replay_result.receipt_id == result.receipt_id
    assert adapter.requests == []

    drain_queue(client)
    assert len(adapter.requests) == 2
    completed_status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert completed_status.status_code == 200, completed_status.text
    assert completed_status.json()["run"]["public_status"]["status"] == "completed"
    completed_receipt = (
        client.app.state.workspace.get(project_id)
        .db.execute(
            "SELECT run_id, action_kind, status, body FROM receipts WHERE id=?",
            (result.receipt_id,),
        )
        .fetchone()
    )
    assert completed_receipt is not None
    assert completed_receipt["run_id"] == result.run_id
    assert completed_receipt["action_kind"] == "map.translate"
    assert completed_receipt["status"] == "completed"
    receipt_body = json.loads(completed_receipt["body"])
    input_kinds = {item["ref"]["kind"] for item in receipt_body["inputs"]}
    evidence_kinds = {item["ref"]["kind"] for item in receipt_body["evidence"]}
    assert "model_rows_input_column" in input_kinds
    assert {item["ref"]["name"] for item in receipt_body["inputs"]} == {
        "statement",
        "speaker",
    }
    assert all(item["ref"]["row_ids"] for item in receipt_body["inputs"])
    assert {
        "typed_action_request",
        "model_rows_model_calls",
        "map_rows_run_counts",
    } <= evidence_kinds
    request = next(
        item["ref"]
        for item in receipt_body["evidence"]
        if item["ref"]["kind"] == "typed_action_request"
    )
    assert job.payload["spec"]["params"]["target_language"] == "English"
    assert request["params_hash"] == typed_request_hash(
        typed_action_for_request(_translate_action(sheet_id))
    )


def test_v1_action_run_queues_map_judge_and_worker_finalizes_receipt(
    tmp_path,
) -> None:
    router, adapter = _judge_router()
    client = TestClient(
        create_app(tmp_path / "ws", router=router, run_status_grace_seconds=3600.0)
    )
    project_id, sheet_id = _seed_judge_project(client)

    _, source_response = _post_after_exact_confirmation(
        client,
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        action=_judge_source_classify_action(sheet_id),
    )
    assert source_response.status_code == 200, source_response.text
    source_result = ActionResult.model_validate(source_response.json())
    assert source_result.status == "queued"
    assert source_result.run_id is not None
    assert source_result.receipt_id is not None
    drain_queue(client)
    assert len(adapter.requests) == 2

    _, response = _post_after_exact_confirmation(
        client,
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        action=_judge_action(sheet_id),
    )
    assert response.status_code == 200, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "queued"
    assert result.action.kind == "map.judge"
    assert result.run_id is not None
    assert result.job_id is not None
    assert result.receipt_id is not None
    assert len(adapter.requests) == 2

    project = client.app.state.workspace.get(project_id)
    assert (
        project.db.execute(
            """
        SELECT 1
        FROM results rr
        JOIN columns c ON c.id = rr.column_id
        WHERE rr.run_id=? AND c.name IN ('verdict', 'judge_note')
        """,
            (result.run_id,),
        ).fetchone()
        is None
    )

    job = client.app.state.workspace.queue.get(result.job_id)
    assert job is not None
    assert job.kind == RUN_PROJECT_KIND
    assert job.payload["project_id"] == project_id
    assert job.payload["run_id"] == result.run_id
    assert job.payload["spec"]["action_kind"] == "map.judge"
    assert "recipe" not in job.payload["spec"]
    assert job.payload["spec"]["params"]["judged_column"] == "risk_score"
    assert job.payload["v1_receipt_id"] == result.receipt_id
    assert job.payload["action_kind"] == "map.judge"
    assert set(job.payload["v1_input_column_ids"]) == {"story", "risk_score"}
    assert "v1_evaluation_context" not in job.payload
    assert (
        job.payload["spec"]["evaluation_context"]["source_run_id"]
        == source_result.run_id
    )

    status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert status.status_code == 200, status.text
    public_status = status.json()["run"]["public_status"]
    assert public_status["status"] == "queued"
    assert public_status["action_kind"] == "map.judge"
    assert public_status["queue"]["job_id"] == result.job_id
    assert public_status["queue"]["status"] == "queued"

    replay = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        json=_judge_action(sheet_id),
    )
    assert replay.status_code == 200, replay.text
    replay_result = ActionResult.model_validate(replay.json())
    assert replay_result.status == "queued"
    assert replay_result.run_id == result.run_id
    assert replay_result.job_id == result.job_id
    assert replay_result.receipt_id == result.receipt_id
    assert len(adapter.requests) == 2

    drain_queue(client)
    assert len(adapter.requests) == 4
    completed_status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert completed_status.status_code == 200, completed_status.text
    assert completed_status.json()["run"]["public_status"]["status"] == "completed"
    completed_receipt = project.db.execute(
        "SELECT run_id, action_kind, status, body FROM receipts WHERE id=?",
        (result.receipt_id,),
    ).fetchone()
    assert completed_receipt is not None
    assert completed_receipt["run_id"] == result.run_id
    assert completed_receipt["action_kind"] == "map.judge"
    assert completed_receipt["status"] == "completed"
    receipt_body = json.loads(completed_receipt["body"])
    input_refs = [item["ref"] for item in receipt_body["inputs"]]
    output_kinds = {item["ref"]["kind"] for item in receipt_body["outputs"]}
    output_roles = {item["ref"]["role"] for item in receipt_body["outputs"]}
    evidence_by_kind = {item["ref"]["kind"]: item for item in receipt_body["evidence"]}
    assert {
        ("model_rows_input_column", "source"),
        ("model_rows_input_column", "judged_output"),
    } <= {(ref["kind"], ref.get("role")) for ref in input_refs}
    assert "map_result_column" in output_kinds
    assert {"judge_verdict", "judge_note"} == output_roles
    judged_input = next(ref for ref in input_refs if ref.get("role") == "judged_output")
    assert judged_input["included_original_prompt"] is False
    assert set(evidence_by_kind) == {
        "typed_action_request",
        "model_rows_model_calls",
        "map_rows_run_counts",
        "map_judge_guidelines",
    }
    assert evidence_by_kind["model_rows_model_calls"]["retention"] == "pinned"

    target = project.db.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name='risk_score'",
        (sheet_id,),
    ).fetchone()
    row = project.db.execute(
        "SELECT id FROM rows WHERE sheet_id=? ORDER BY id LIMIT 1", (sheet_id,)
    ).fetchone()
    project.apply_edits(
        [
            {
                "row_id": int(row["id"]),
                "column_id": int(target["id"]),
                "value": 10,
            }
        ],
        label="drift judged input after completion",
    )
    router._adapters.pop("openai")  # noqa: SLF001
    completed_replay_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_judge_action(sheet_id),
    )
    completed_replay = ActionResult.model_validate(completed_replay_response.json())
    assert completed_replay_response.status_code == 200
    assert completed_replay.status == "completed"
    assert completed_replay.run_id == result.run_id
    assert completed_replay.receipt_id == result.receipt_id
    assert len(adapter.requests) == 4


def test_queued_judge_receipt_uses_frozen_subject_after_model_work(
    tmp_path, monkeypatch
) -> None:
    import frisket.engine.executor.map_rows_action as map_rows_action_module

    router, adapter = _judge_router()
    client = TestClient(
        create_app(tmp_path / "ws", router=router, run_status_grace_seconds=3600.0)
    )
    project_id, sheet_id = _seed_judge_project(client)
    _, source_response = _post_after_exact_confirmation(
        client,
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        action=_judge_source_classify_action(sheet_id),
    )
    source = ActionResult.model_validate(source_response.json())
    drain_queue(client)
    project = client.app.state.workspace.get(project_id)
    subject = project.db.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name='risk_score'",
        (sheet_id,),
    ).fetchone()
    row = project.db.execute(
        "SELECT id FROM rows WHERE sheet_id=? ORDER BY id LIMIT 1", (sheet_id,)
    ).fetchone()
    original_capture = map_rows_action_module.capture_maprunner_facts
    mutated = False

    def capture_after_mutation(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal mutated
        if kwargs["runner_spec"].get("action_kind") == "map.judge" and not mutated:
            mutated = True
            project.apply_edits(
                [
                    {
                        "row_id": int(row["id"]),
                        "column_id": int(subject["id"]),
                        "value": 10,
                    }
                ],
                label="mutate judged value after model work",
            )
        return original_capture(*args, **kwargs)

    monkeypatch.setattr(
        map_rows_action_module, "capture_maprunner_facts", capture_after_mutation
    )
    _, response = _post_after_exact_confirmation(
        client,
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        action=_judge_action(
            sheet_id,
            idempotency_key="v1-http-queued-judge@sha256:frozen-receipt",
        ),
    )
    queued = ActionResult.model_validate(response.json())
    drain_queue(client)

    assert mutated is True
    assert len(adapter.requests) == 4
    stored = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (queued.receipt_id,)
    ).fetchone()
    receipt = json.loads(stored["body"])
    assert receipt["status"] == "completed"
    judged = next(
        item["ref"]
        for item in receipt["inputs"]
        if item["ref"].get("role") == "judged_output"
    )
    assert judged["source_run_id"] == source.run_id
    assert judged["source_receipt_id"] == source.receipt_id
    assert {ref["run_id"] for ref in judged["value_refs"]} == {source.run_id}
    assert all(ref["kind"] == "run_result" for ref in judged["value_refs"])


def test_queued_map_judge_refuses_subject_provenance_drift_before_model_work(
    tmp_path,
) -> None:
    router, adapter = _judge_router()
    client = TestClient(
        create_app(tmp_path / "ws", router=router, run_status_grace_seconds=3600.0)
    )
    project_id, sheet_id = _seed_judge_project(client)
    _, source_response = _post_after_exact_confirmation(
        client,
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        action=_judge_source_classify_action(sheet_id),
    )
    assert source_response.status_code == 200
    drain_queue(client)
    assert len(adapter.requests) == 2

    _, response = _post_after_exact_confirmation(
        client,
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        action=_judge_action(
            sheet_id,
            idempotency_key="v1-http-queued-judge@sha256:drift",
        ),
    )
    queued = ActionResult.model_validate(response.json())
    assert queued.status == "queued"

    project = client.app.state.workspace.get(project_id)
    target = project.db.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name='risk_score'",
        (sheet_id,),
    ).fetchone()
    row = project.db.execute(
        "SELECT id FROM rows WHERE sheet_id=? ORDER BY id LIMIT 1", (sheet_id,)
    ).fetchone()
    project.apply_edits(
        [
            {
                "row_id": int(row["id"]),
                "column_id": int(target["id"]),
                "value": 9,
            }
        ],
        label="change queued judge subject",
    )

    replay_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_judge_action(
            sheet_id,
            idempotency_key="v1-http-queued-judge@sha256:drift",
        ),
    )
    replay = ActionResult.model_validate(replay_response.json())
    assert replay.status == "queued"
    assert replay.receipt_id == queued.receipt_id
    assert replay.job_id == queued.job_id

    drain_queue(client)
    assert len(adapter.requests) == 2
    receipt = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (queued.receipt_id,)
    ).fetchone()
    body = json.loads(receipt["body"])
    assert body["status"] == "failed"
    assert body["errors"][0]["code"] == "stale_input"
    assert body["errors"][0]["field"] == "params.judged_column"


def test_v1_action_run_queues_map_ner_and_worker_finalizes_receipt(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "secret")
    http = _NerSidecarHttp()
    client = TestClient(
        create_app(
            tmp_path / "ws",
            router=_ner_router_with_http(http),
            run_status_grace_seconds=3600.0,
        )
    )
    project_id, sheet_id = _seed_ner_project(client)

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        json=_ner_action(sheet_id),
    )
    assert response.status_code == 200, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "queued"
    assert result.action.kind == "map.ner"
    assert result.run_id is not None
    assert result.job_id is not None
    assert result.receipt_id is not None
    assert http.calls == []

    job = client.app.state.workspace.queue.get(result.job_id)
    assert job is not None
    assert job.kind == RUN_PROJECT_KIND
    assert job.payload["project_id"] == project_id
    assert job.payload["run_id"] == result.run_id
    assert job.payload["spec"]["action_kind"] == "map.ner"
    assert "recipe" not in job.payload["spec"]
    assert job.payload["spec"]["output_names"]["entities"] == "entities"
    assert job.payload["v1_receipt_id"] == result.receipt_id
    assert job.payload["action_kind"] == "map.ner"
    assert set(job.payload["v1_input_column_ids"]) == {"body", "speaker"}

    status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert status.status_code == 200, status.text
    public_status = status.json()["run"]["public_status"]
    assert public_status["status"] == "queued"
    assert public_status["action_kind"] == "map.ner"
    assert public_status["action_kind"] == "map.ner"
    assert public_status["queue"]["job_id"] == result.job_id
    assert public_status["queue"]["status"] == "queued"

    replay = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        json=_ner_action(sheet_id),
    )
    assert replay.status_code == 200, replay.text
    replay_result = ActionResult.model_validate(replay.json())
    assert replay_result.status == "queued"
    assert replay_result.run_id == result.run_id
    assert replay_result.job_id == result.job_id
    assert replay_result.receipt_id == result.receipt_id
    assert http.calls == []

    drain_queue(client)
    assert len(http.calls) == 2
    assert http.calls[0][0] == "http://models/ner"
    completed_status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert completed_status.status_code == 200, completed_status.text
    assert completed_status.json()["run"]["public_status"]["status"] == "completed"
    completed_receipt = (
        client.app.state.workspace.get(project_id)
        .db.execute(
            "SELECT run_id, action_kind, status, body FROM receipts WHERE id=?",
            (result.receipt_id,),
        )
        .fetchone()
    )
    assert completed_receipt is not None
    assert completed_receipt["run_id"] == result.run_id
    assert completed_receipt["action_kind"] == "map.ner"
    assert completed_receipt["status"] == "completed"
    receipt_body = json.loads(completed_receipt["body"])
    output_kinds = {item["ref"]["kind"] for item in receipt_body["outputs"]}
    evidence_kinds = {item["ref"]["kind"] for item in receipt_body["evidence"]}
    assert "map_result_column" in output_kinds
    assert {
        "typed_action_request",
        "map_rows_run_counts",
    } <= evidence_kinds
    request = next(
        item["ref"]
        for item in receipt_body["evidence"]
        if item["ref"]["kind"] == "typed_action_request"
    )
    assert request["params"]["engine"] == "gliner"
    assert request["params"]["labels"] == ["person", "organization"]


def test_scoped_ner_recovery_executes_admitted_rows_after_ambient_drift(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "secret")
    workspace_path = tmp_path / "ws"
    http = _NerSidecarHttp()

    with TestClient(
        create_app(
            workspace_path,
            router=_ner_router_with_http(http),
            run_status_grace_seconds=3600.0,
        )
    ) as client:
        project_id, sheet_id = _seed_ner_project(client)
        project = client.app.state.workspace.get(project_id)
        row_ids = [
            int(row["id"])
            for row in project.db.execute(
                "SELECT id FROM rows WHERE sheet_id=? ORDER BY position",
                (sheet_id,),
            ).fetchall()
        ]
        admitted_row_ids = [row_ids[0]]

        action = _ner_action(
            sheet_id,
            idempotency_key="action-scope-ner@sha256:stable",
        )
        action["scope"]["row_ids"] = admitted_row_ids

        response = client.post(
            f"/api/projects/{project_id}/actions/v1/run",
            json=action,
        )
        assert response.status_code == 200, response.text
        result = ActionResult.model_validate(response.json())
        assert result.status == "queued"
        assert result.run_id is not None
        assert result.job_id is not None
        assert http.calls == []
        assert RunResultStore(project).run_row_scope(result.run_id) == admitted_row_ids

        columns = {
            str(row["name"]): int(row["id"]) for row in project.columns(sheet_id)
        }
        [ambient_row_id] = project.add_rows(
            sheet_id,
            [{"body": "Katherine Johnson calculated orbital paths", "speaker": "new"}],
            columns,
        )
        assert ambient_row_id not in admitted_row_ids

    # Recover the durable queued job in a fresh app/workspace. The worker must
    # consume the admitted run_rows, not re-resolve the now larger sheet.
    with TestClient(
        create_app(
            workspace_path,
            router=_ner_router_with_http(http),
            run_status_grace_seconds=3600.0,
        )
    ) as recovered_client:
        drain_queue(recovered_client, worker_id="action-scope-recovery-worker")
        assert len(http.calls) == 1
        posted_text = str((http.calls[0][1].get("json") or {})["texts"][0])
        assert "Ada Lovelace" in posted_text
        assert "Katherine Johnson" not in posted_text

        recovered_project = recovered_client.app.state.workspace.get(project_id)
        recovered_runs = RunResultStore(recovered_project)
        assert recovered_runs.run_row_scope(result.run_id) == admitted_row_ids
        published_row_ids = [
            int(row["row_id"])
            for row in recovered_project.db.execute(
                "SELECT row_id FROM results WHERE run_id=? ORDER BY row_id",
                (result.run_id,),
            ).fetchall()
        ]
        assert published_row_ids == admitted_row_ids


def test_v1_action_run_queues_research_web_search_and_worker_finalizes_receipt(
    tmp_path,
    monkeypatch,
) -> None:
    import ddgs

    _FakeDDGS.seen = []
    monkeypatch.setattr(ddgs, "DDGS", _FakeDDGS)
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id, sheet_id = _seed_web_search_project(client)

    _, response = _post_after_exact_confirmation(
        client,
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        action=_web_search_action(sheet_id),
    )
    assert response.status_code == 200, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "queued"
    assert result.action.kind == "research.web_search"
    assert result.run_id is not None
    assert result.job_id is not None
    assert result.receipt_id is not None
    assert _FakeDDGS.seen == []

    job = client.app.state.workspace.queue.get(result.job_id)
    assert job is not None
    assert job.kind == RUN_PROJECT_KIND
    assert job.payload["project_id"] == project_id
    assert job.payload["run_id"] == result.run_id
    assert job.payload["spec"]["action_kind"] == "research.web_search"
    assert "recipe" not in job.payload["spec"]
    assert job.payload["spec"]["output_names"] == {"search_results": "search_results"}
    assert job.payload["v1_receipt_id"] == result.receipt_id
    assert job.payload["action_kind"] == "research.web_search"
    assert set(job.payload["v1_input_column_ids"]) == {"country"}

    status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert status.status_code == 200, status.text
    public_status = status.json()["run"]["public_status"]
    assert public_status["status"] == "queued"
    assert public_status["action_kind"] == "research.web_search"
    assert public_status["action_kind"] == "research.web_search"
    assert public_status["queue"]["job_id"] == result.job_id
    assert public_status["queue"]["status"] == "queued"

    replay = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        json=_web_search_action(sheet_id),
    )
    assert replay.status_code == 200, replay.text
    replay_result = ActionResult.model_validate(replay.json())
    assert replay_result.status == "queued"
    assert replay_result.run_id == result.run_id
    assert replay_result.job_id == result.job_id
    assert replay_result.receipt_id == result.receipt_id
    assert _FakeDDGS.seen == []

    drain_queue(client)
    assert _FakeDDGS.seen == [
        {
            "query": "US tariff impacts on Canada economy 2026",
            "max_results": 2,
        },
        {
            "query": "US tariff impacts on Mexico economy 2026",
            "max_results": 2,
        },
    ]
    completed_status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert completed_status.status_code == 200, completed_status.text
    assert completed_status.json()["run"]["public_status"]["status"] == "completed"
    completed_receipt = (
        client.app.state.workspace.get(project_id)
        .db.execute(
            "SELECT run_id, action_kind, status, body FROM receipts WHERE id=?",
            (result.receipt_id,),
        )
        .fetchone()
    )
    assert completed_receipt is not None
    assert completed_receipt["run_id"] == result.run_id
    assert completed_receipt["action_kind"] == "research.web_search"
    assert completed_receipt["status"] == "completed"
    receipt_body = json.loads(completed_receipt["body"])
    evidence_kinds = {item["ref"]["kind"] for item in receipt_body["evidence"]}
    assert evidence_kinds == {
        "typed_action_request",
        "map_rows_run_counts",
        "web_search_call",
    }

    project = client.app.state.workspace.get(project_id)
    country_id = next(
        int(column["id"])
        for column in project.columns(sheet_id)
        if column["name"] == "country"
    )
    [new_row_id] = project.add_rows(
        sheet_id, [{"country": "Brazil"}], {"country": country_id}
    )
    backfill = {
        "action_id": "run.backfill",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"column": "search_results"},
        "idempotency_key": "v1-web-search-backfill@stable",
    }
    network_off = client.patch(
        f"/api/projects/{project_id}/network", json={"mode": "off"}
    )
    assert network_off.status_code == 200
    blocked = copy.deepcopy(backfill)
    blocked["idempotency_key"] = "v1-web-search-backfill@network-off"
    blocked_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run", json=blocked
    )
    blocked_result = ActionResult.model_validate(blocked_response.json())
    assert blocked_result.errors[0].code == "network_disabled"
    assert len(_FakeDDGS.seen) == 2
    network_on = client.patch(
        f"/api/projects/{project_id}/network", json={"mode": "on"}
    )
    assert network_on.status_code == 200

    backfill_gate, backfill_response = _post_after_exact_confirmation(
        client,
        f"/api/projects/{project_id}/actions/v1/run",
        action=backfill,
    )
    backfill_challenge = ActionResult.model_validate(backfill_gate.json())
    assert backfill_challenge.errors[0].code == "external_cost_requires_confirmation"
    assert backfill_challenge.errors[0].field == "confirmation"
    assert backfill_challenge.errors[0].details["reason"] == "external_metered"
    assert backfill_response.status_code == 200, backfill_response.text
    backfill_result = ActionResult.model_validate(backfill_response.json())
    assert backfill_result.status == "completed", backfill_result.errors
    backfill_receipt = json.loads(
        project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (backfill_result.receipt_id,)
        ).fetchone()[0]
    )
    assert backfill_receipt["provider_use"] == [
        {
            "provider": "ddgs",
            "service": "ddgs.text",
            "external_api": True,
            "selected_row_count": 1,
            "successful_row_count": 1,
            "failed_row_count": 0,
            "max_attempts_per_row": 4,
            "operation_call_count": 1,
            "cost_actual": 0.0,
        }
    ]
    observations = [
        item["ref"]
        for item in backfill_receipt["evidence"]
        if item["ref"].get("kind") == "web_search_call"
    ]
    assert len(observations) == 1
    assert observations[0]["row_id"] == new_row_id
    assert observations[0]["run_id"] == backfill_result.run_id
    assert observations[0]["succeeded"] is True
    calls_after_backfill = list(_FakeDDGS.seen)
    replay = client.post(f"/api/projects/{project_id}/actions/v1/run", json=backfill)
    assert replay.status_code == 200, replay.text
    replay_result = ActionResult.model_validate(replay.json())
    assert replay_result.status == "completed"
    assert replay_result.receipt_id == backfill_result.receipt_id
    assert _FakeDDGS.seen == calls_after_backfill
    assert _FakeDDGS.seen[-1] == {
        "query": "US tariff impacts on Brazil economy 2026",
        "max_results": 2,
    }
    output_id = next(
        int(column["id"])
        for column in project.columns(sheet_id)
        if column["name"] == "search_results"
    )
    assert (
        len(project.get_values(sheet_id, output_id, row_ids=[new_row_id])[new_row_id])
        == 2
    )


@pytest.mark.parametrize(
    ("fail_all", "expected_status", "expected_calls", "successful", "failed"),
    [
        (False, "partial", 5, 1, 1),
        (True, "failed", 8, 0, 2),
    ],
)
def test_queued_web_search_records_partial_and_all_failed_provider_facts(
    tmp_path,
    monkeypatch,
    fail_all,
    expected_status,
    expected_calls,
    successful,
    failed,
) -> None:
    import ddgs

    calls: list[str] = []

    class SelectiveDDGS:
        def text(self, query: str, max_results: int):
            calls.append(query)
            if fail_all or "Canada" in query:
                raise RuntimeError("provider unavailable")
            return [
                {
                    "title": "Mexico result",
                    "href": "https://example.test/mexico",
                    "body": "Mexico snippet",
                }
            ]

    async def fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(ddgs, "DDGS", SelectiveDDGS)
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id, sheet_id = _seed_web_search_project(client)
    _, response = _post_after_exact_confirmation(
        client,
        f"/api/projects/{project_id}/actions/v1/run",
        action=_web_search_action(
            sheet_id, idempotency_key=f"queued-web-search-{expected_status}"
        ),
    )
    queued = ActionResult.model_validate(response.json())

    drain_queue(client)

    project = client.app.state.workspace.get(project_id)
    row = project.db.execute(
        "SELECT status, body FROM receipts WHERE id=?", (queued.receipt_id,)
    ).fetchone()
    assert row["status"] == expected_status
    receipt = json.loads(row["body"])
    assert len(calls) == expected_calls
    assert receipt["provider_use"] == [
        {
            "provider": "ddgs",
            "service": "ddgs.text",
            "external_api": True,
            "selected_row_count": 2,
            "successful_row_count": successful,
            "failed_row_count": failed,
            "max_attempts_per_row": 4,
            "operation_call_count": expected_calls,
            "cost_actual": 0.0,
        }
    ]


def test_v1_action_run_queues_research_answer_and_worker_finalizes_receipt(
    tmp_path,
    monkeypatch,
) -> None:
    from frisket.engine.executor import research_read

    search_calls: list[dict[str, Any]] = []

    async def fake_search(query: str) -> tuple[str, list[str]]:
        search_calls.append({"query": query})
        idx = len(search_calls)
        url = f"https://example.test/research/source-{idx}"
        return f"Source {idx} says the claim is cited. {url}", [url]

    monkeypatch.setattr(research_read, "search_web", fake_search)
    router, adapter = _research_router()
    client = TestClient(
        create_app(tmp_path / "ws", router=router, run_status_grace_seconds=3600.0)
    )
    project_id, sheet_id = _seed_research_answer_project(client)

    _, response = _post_after_exact_confirmation(
        client,
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        action=_research_answer_action(sheet_id),
    )
    assert response.status_code == 200, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "queued"
    assert result.action.kind == "research.answer"
    assert result.run_id is not None
    assert result.job_id is not None
    assert result.receipt_id is not None
    assert adapter.requests == []
    assert search_calls == []

    job = client.app.state.workspace.queue.get(result.job_id)
    assert job is not None
    assert job.kind == RUN_PROJECT_KIND
    assert job.payload["project_id"] == project_id
    assert job.payload["run_id"] == result.run_id
    assert job.payload["spec"]["action_kind"] == "research.answer"
    assert "recipe" not in job.payload["spec"]
    assert job.payload["spec"]["output_names"] == {
        "answer": "answer",
        "sources": "answer_sources",
    }
    assert job.payload["v1_receipt_id"] == result.receipt_id
    assert job.payload["action_kind"] == "research.answer"
    assert set(job.payload["spec"]["input_columns"]) == {
        "company",
        "topic",
    }
    assert (
        client.app.state.workspace.get(project_id)
        .db.execute("SELECT total_rows FROM runs WHERE id=?", (result.run_id,))
        .fetchone()["total_rows"]
        == 2
    )

    status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert status.status_code == 200, status.text
    public_status = status.json()["run"]["public_status"]
    assert public_status["status"] == "queued"
    assert public_status["action_kind"] == "research.answer"
    assert public_status["action_kind"] == "research.answer"
    assert public_status["queue"]["job_id"] == result.job_id
    assert public_status["queue"]["status"] == "queued"

    replay = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        json=_research_answer_action(sheet_id),
    )
    assert replay.status_code == 200, replay.text
    replay_result = ActionResult.model_validate(replay.json())
    assert replay_result.status == "queued"
    assert replay_result.run_id == result.run_id
    assert replay_result.job_id == result.job_id
    assert replay_result.receipt_id == result.receipt_id
    assert adapter.requests == []
    assert search_calls == []

    drain_queue(client)
    # Six requests across the two concurrent rows: with the odd/even script,
    # one row's FIRST request lands on an even call and answers from memory
    # without searching — which trips the grounding guard's one verification
    # nudge (research_answer.py), adding a search + grounded answer for that
    # row. The other row's calls interleave as search/observation/answer.
    assert len(adapter.requests) == 6
    assert search_calls == [
        {"query": "official source 1"},
        {"query": "official source 3"},
        {"query": "official source 5"},
    ]
    completed_status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert completed_status.status_code == 200, completed_status.text
    assert completed_status.json()["run"]["public_status"]["status"] == "completed"
    completed_receipt = (
        client.app.state.workspace.get(project_id)
        .db.execute(
            "SELECT run_id, action_kind, status, body FROM receipts WHERE id=?",
            (result.receipt_id,),
        )
        .fetchone()
    )
    assert completed_receipt is not None
    assert completed_receipt["run_id"] == result.run_id
    assert completed_receipt["action_kind"] == "research.answer"
    assert completed_receipt["status"] == "completed"
    receipt_body = json.loads(completed_receipt["body"])
    evidence_kinds = {item["ref"]["kind"] for item in receipt_body["evidence"]}
    assert {
        "typed_action_request",
        "model_rows_model_calls",
    } <= evidence_kinds
    assert "research_answer_trace" not in evidence_kinds


def test_v1_action_run_queues_media_metadata_and_worker_finalizes_receipt(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(metadata, "_resolve_exiftool_path", lambda: None)
    monkeypatch.setattr(metadata, "_resolve_ffprobe_path", lambda: None)
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id, sheet_id, row_ids, _blobs = _seed_media_ocr_project(client)

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        json=_media_extract_metadata_action(sheet_id, row_ids),
    )
    assert response.status_code == 200, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "queued"
    assert result.action.kind == "media.extract_metadata"
    assert result.run_id is not None
    assert result.job_id is not None
    assert result.receipt_id is not None

    job = client.app.state.workspace.queue.get(result.job_id)
    assert job is not None
    assert job.kind == RUN_PROJECT_KIND
    assert job.payload["project_id"] == project_id
    assert job.payload["run_id"] == result.run_id
    assert job.payload["spec"]["action_kind"] == "media.extract_metadata"
    assert "recipe" not in job.payload["spec"]
    assert job.payload["spec"]["params"] == {
        "source": "media",
        "output_mode": "object",
        "refresh": False,
    }
    assert job.payload["spec"]["output_names"] == {"details": "meta"}
    assert "output_prefix" not in job.payload["spec"]
    assert job.payload["v1_receipt_id"] == result.receipt_id
    assert job.payload["action_kind"] == "media.extract_metadata"
    assert set(job.payload["v1_input_column_ids"]) == {"media"}
    assert job.payload["v1_input_column_types"] == {"media": "image"}

    receipt_row = (
        client.app.state.workspace.get(project_id)
        .db.execute(
            "SELECT run_id,action_kind,status FROM receipts WHERE id=?",
            (result.receipt_id,),
        )
        .fetchone()
    )
    assert dict(receipt_row) == {
        "run_id": result.run_id,
        "action_kind": "media.extract_metadata",
        "status": "queued",
    }

    drain_queue(client)
    completed_status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert completed_status.status_code == 200, completed_status.text
    assert completed_status.json()["run"]["public_status"]["status"] == "completed"
    completed_receipt = (
        client.app.state.workspace.get(project_id)
        .db.execute(
            "SELECT run_id,action_kind,status,body FROM receipts WHERE id=?",
            (result.receipt_id,),
        )
        .fetchone()
    )
    assert completed_receipt is not None
    assert completed_receipt["run_id"] == result.run_id
    assert completed_receipt["action_kind"] == "media.extract_metadata"
    assert completed_receipt["status"] == "completed"
    body = json.loads(completed_receipt["body"])
    assert {item["ref"]["kind"] for item in body["evidence"]} >= {
        "typed_action_request",
        "map_rows_run_counts",
    }
    assert [item["name"] for item in body["outputs"]] == ["meta"]
    counts = next(
        item["ref"]
        for item in body["evidence"]
        if item["ref"]["kind"] == "map_rows_run_counts"
    )
    assert (counts["total_rows"], counts["completed_rows"], counts["failed_rows"]) == (
        2,
        2,
        0,
    )
    project = client.app.state.workspace.get(project_id)
    details_id = body["outputs"][0]["ref"]["column_id"]
    values = project.get_values(sheet_id, details_id, row_ids=row_ids)
    assert set(values) == set(row_ids)
    for value in values.values():
        assert value["normalized"]["probe_status"] == "partial"
        assert any(
            warning["code"] == "missing_dependency" for warning in value["warnings"]
        )


def test_v1_cancelled_metadata_family_can_requeue_before_worker(
    tmp_path,
    monkeypatch,
) -> None:
    """Cancellation releases the reservation without orphaning its family."""

    monkeypatch.setattr(metadata, "_resolve_exiftool_path", lambda: None)
    monkeypatch.setattr(metadata, "_resolve_ffprobe_path", lambda: None)
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id, sheet_id, row_ids, _blobs = _seed_media_ocr_project(client)

    first_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_media_extract_metadata_action(
            sheet_id,
            row_ids,
            idempotency_key="v1-http-metadata-sync-cancel-first",
        ),
    )
    assert first_response.status_code == 200, first_response.text
    first = ActionResult.model_validate(first_response.json())
    assert first.status == "queued"
    assert first.run_id is not None

    cancelled_response = client.post(
        f"/api/projects/{project_id}/actions/runs/{first.run_id}/cancel"
    )
    assert cancelled_response.status_code == 200, cancelled_response.text

    second_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_media_extract_metadata_action(
            sheet_id,
            row_ids,
            idempotency_key="v1-http-metadata-sync-cancel-second",
            replace_existing=True,
        ),
    )
    assert second_response.status_code == 200, second_response.text
    second = ActionResult.model_validate(second_response.json())
    assert second.status == "queued", second.errors
    assert second.run_id is not None and second.run_id != first.run_id


def test_v1_queued_cancelled_metadata_outputs_allow_explicit_new_key_replacement(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(metadata, "_resolve_exiftool_path", lambda: None)
    monkeypatch.setattr(metadata, "_resolve_ffprobe_path", lambda: None)
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id, sheet_id, row_ids, _blobs = _seed_media_ocr_project(client)

    first_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_media_extract_metadata_action(
            sheet_id,
            row_ids,
            idempotency_key="v1-http-metadata-cancel-first",
        ),
    )
    assert first_response.status_code == 200, first_response.text
    first = ActionResult.model_validate(first_response.json())
    assert first.status == "queued"
    assert first.run_id is not None
    assert first.job_id is not None
    assert first.receipt_id is not None

    project = client.app.state.workspace.get(project_id)
    prepared_column = project.db.execute(
        "SELECT id,hidden,current_run_id FROM columns WHERE sheet_id=? AND name='meta'",
        (sheet_id,),
    ).fetchone()
    assert prepared_column is not None
    output_id = int(prepared_column["id"])
    assert int(prepared_column["hidden"]) == 0
    assert int(prepared_column["current_run_id"]) == first.run_id

    cancelled_response = client.post(
        f"/api/projects/{project_id}/actions/runs/{first.run_id}/cancel"
    )
    assert cancelled_response.status_code == 200, cancelled_response.text
    cancelled = cancelled_response.json()
    assert cancelled["status"] == "cancelled"
    assert cancelled["queue_job_id"] == first.job_id
    assert cancelled["queue_cancelled"] is True

    cancelled_receipt_row = project.db.execute(
        "SELECT status,body FROM receipts WHERE id=?", (first.receipt_id,)
    ).fetchone()
    assert cancelled_receipt_row is not None
    assert cancelled_receipt_row["status"] == "cancelled"
    cancelled_receipt = json.loads(cancelled_receipt_row["body"])
    assert cancelled_receipt["outputs"] == []
    assert any(
        evidence["ref"].get("kind") == "queued_action_run_prepared"
        and evidence["ref"].get("run_id") == first.run_id
        for evidence in cancelled_receipt["evidence"]
    )

    second_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_media_extract_metadata_action(
            sheet_id,
            row_ids,
            idempotency_key="v1-http-metadata-cancel-second",
            replace_existing=True,
        ),
    )
    assert second_response.status_code == 200, second_response.text
    second = ActionResult.model_validate(second_response.json())
    assert second.status == "queued", second.errors
    assert second.run_id is not None and second.run_id != first.run_id
    assert (
        int(
            project.db.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name='meta'", (sheet_id,)
            ).fetchone()["id"]
        )
        == output_id
    )

    drain_queue(client)
    completed_status = client.get(
        f"/api/projects/{project_id}/actions/runs/{second.run_id}/status"
    )
    assert completed_status.status_code == 200, completed_status.text
    assert completed_status.json()["run"]["public_status"]["status"] == "completed"
    completed_column = project.db.execute(
        "SELECT id,hidden,current_run_id FROM columns WHERE id=?", (output_id,)
    ).fetchone()
    assert int(completed_column["hidden"]) == 0
    assert int(completed_column["current_run_id"]) == first.run_id


@pytest.mark.parametrize("mode", ["object", "columns"])
def test_v1_queued_targeted_metadata_rerun_preserves_untargeted_family_rows(
    tmp_path,
    monkeypatch,
    mode: str,
) -> None:
    monkeypatch.setattr(metadata, "_resolve_exiftool_path", lambda: None)
    monkeypatch.setattr(metadata, "_resolve_ffprobe_path", lambda: None)
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id, sheet_id, row_ids, _blobs = _seed_media_ocr_project(client)
    target_row, untargeted_row = row_ids

    first_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_media_extract_metadata_action(
            sheet_id,
            row_ids,
            idempotency_key=f"v1-http-metadata-targeted-{mode}-first",
            mode=mode,
        ),
    )
    assert first_response.status_code == 200, first_response.text
    first = ActionResult.model_validate(first_response.json())
    assert first.status == "queued"
    drain_queue(client)

    project = client.app.state.workspace.get(project_id)
    output_names = _media_extract_metadata_action(sheet_id, row_ids, mode=mode)[
        "output_names"
    ]
    output_ids = {
        name: int(
            project.db.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name=?",
                (sheet_id, name),
            ).fetchone()["id"]
        )
        for name in output_names.values()
    }
    details_name = "meta" if mode == "object" else "meta_details"
    details_id = output_ids[details_name]
    generations = ResultGenerationStore(project)
    prior_run_id = generations.latest_applied_run_id(details_id)
    assert prior_run_id is not None
    project.db.execute(
        "UPDATE results SET review_state='rejected' "
        "WHERE run_id=? AND row_id=? AND column_id=?",
        (prior_run_id, untargeted_row, details_id),
    )
    project.db.commit()

    preserved_fields = (
        "value",
        "confidence",
        "justification",
        "error",
        "error_code",
        "review_state",
        "outcome",
    )

    def result_snapshot(run_id: int, row_id: int) -> dict[int, dict[str, Any]]:
        return {
            int(row["column_id"]): {field: row[field] for field in preserved_fields}
            for row in project.db.execute(
                "SELECT column_id,value,confidence,justification,error,error_code,"
                "review_state,outcome FROM results WHERE run_id=? AND row_id=?",
                (run_id, row_id),
            ).fetchall()
        }

    prior_untargeted = result_snapshot(prior_run_id, untargeted_row)
    manual_value = {"manual_overlay": "queued keep me"}
    target_manual_value = {"manual_overlay": "queued target correction"}
    project.apply_edits(
        [
            {
                "row_id": untargeted_row,
                "column_id": details_id,
                "value": manual_value,
            },
            {
                "row_id": target_row,
                "column_id": details_id,
                "value": target_manual_value,
            },
        ],
        label="preserve queued metadata overlay",
    )
    input_id = int(
        project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='media'", (sheet_id,)
        ).fetchone()["id"]
    )
    replacement_digest = project.add_blob(
        PNG_1X1 + b"replacement",
        filename="queued-after.png",
        mime="image/png",
        metadata=owned_media_metadata_document(
            probe={"width": 1, "height": 1, "kind": "image"}
        ),
    )
    project.apply_edits(
        [
            {
                "row_id": target_row,
                "column_id": input_id,
                "value": media_cell(
                    replacement_digest,
                    filename="queued-after.png",
                    mime="image/png",
                ),
            }
        ],
        label="replace queued targeted media",
    )

    rerun_action = _media_extract_metadata_action(
        sheet_id,
        [target_row],
        idempotency_key=f"v1-http-metadata-targeted-{mode}-rerun",
        mode=mode,
        replace_existing=True,
    )
    rerun_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=rerun_action,
    )
    assert rerun_response.status_code == 200, rerun_response.text
    rerun = ActionResult.model_validate(rerun_response.json())
    assert rerun.status == "queued"
    drain_queue(client)

    new_run_id = generations.latest_applied_run_id(details_id)
    assert new_run_id is not None
    assert new_run_id == rerun.run_id
    run = project.db.execute("SELECT * FROM runs WHERE id=?", (new_run_id,)).fetchone()
    assert (run["total_rows"], run["completed_rows"], run["failed_rows"]) == (
        1,
        1,
        0,
    )
    assert float(run["cost_actual"]) == 0.0
    assert RunResultStore(project).row_error_summary(new_run_id) is None
    assert result_snapshot(new_run_id, untargeted_row) == {}
    assert result_snapshot(prior_run_id, untargeted_row) == prior_untargeted
    assert (
        project.db.execute(
            "SELECT run_id FROM cell_result_heads WHERE column_id=? AND row_id=?",
            (details_id, untargeted_row),
        ).fetchone()["run_id"]
        == prior_run_id
    )
    assert project.db.execute(
        "SELECT COUNT(*) FROM results WHERE run_id=?", (new_run_id,)
    ).fetchone()[0] == len(output_ids)
    details = project.get_values(
        sheet_id, details_id, row_ids=[target_row, untargeted_row]
    )
    raw_details = project.get_values(
        sheet_id,
        details_id,
        row_ids=[target_row, untargeted_row],
        apply_edits=False,
    )
    assert raw_details[target_row]["normalized"]["filename"] == "queued-after.png"
    assert details[target_row] == target_manual_value
    assert details[untargeted_row] == manual_value

    receipt = json.loads(
        project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (rerun.receipt_id,)
        ).fetchone()["body"]
    )
    input_rows = next(
        item for item in receipt["inputs"] if item["ref"]["kind"] == "source_column"
    )
    assert input_rows["ref"]["row_ids"] == [target_row]
    counts = next(
        item["ref"]
        for item in receipt["evidence"]
        if item["ref"]["kind"] == "map_rows_run_counts"
    )
    assert counts["failed_rows"] == 0
    assert counts["total_rows"] == counts["completed_rows"] == 1
    assert raw_details[target_row]["normalized"]["probe_status"] == "partial"
    details_ref = next(
        item["ref"] for item in receipt["outputs"] if item["name"] == details_name
    )
    assert details_ref["value_hash"] == output_column_result_value_hash(
        project,
        sheet_id=sheet_id,
        column_id=details_id,
        row_ids=[target_row],
    )
    assert details_ref["value_hash"] != output_column_value_hash(
        project,
        sheet_id=sheet_id,
        column_id=details_id,
        row_ids=[target_row],
    )

    later_manual_value = {"manual_overlay": "queued later correction"}
    project.apply_edits(
        [
            {
                "row_id": target_row,
                "column_id": details_id,
                "value": later_manual_value,
            }
        ],
        label="revise queued targeted metadata overlay",
    )
    run_count = project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    replay_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=rerun_action,
    )
    assert replay_response.status_code == 200, replay_response.text
    replay = ActionResult.model_validate(replay_response.json())
    assert replay.status == "completed", replay.errors
    assert replay.run_id == rerun.run_id
    assert replay.receipt_id == rerun.receipt_id
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == run_count
    assert (
        project.get_values(sheet_id, details_id, row_ids=[target_row])[target_row]
        == later_manual_value
    )


def test_v1_queued_metadata_rerun_rechecks_output_identity_after_claim_acquisition(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(metadata, "_resolve_exiftool_path", lambda: None)
    monkeypatch.setattr(metadata, "_resolve_ffprobe_path", lambda: None)
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id, sheet_id, row_ids, _blobs = _seed_media_ocr_project(client)

    first_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_media_extract_metadata_action(
            sheet_id,
            row_ids,
            idempotency_key="v1-http-metadata-race-first",
        ),
    )
    assert first_response.status_code == 200, first_response.text
    first = ActionResult.model_validate(first_response.json())
    assert first.status == "queued"
    drain_queue(client)
    project = client.app.state.workspace.get(project_id)
    run_count = int(project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0])

    acquire = action_runtime_reservations._acquire_output_claims_for_runner

    def mutate_then_claim(*args, **kwargs):  # noqa: ANN002, ANN003
        # Preserve the caller-owned admission transaction while changing the
        # exact admitted output identity between planning and preparation.
        project.db.execute(
            "UPDATE columns SET name='renamed_meta' WHERE sheet_id=? AND name='meta'",
            (sheet_id,),
        )
        assert project.db.in_transaction
        return acquire(*args, **kwargs)

    monkeypatch.setattr(
        action_runtime_reservations,
        "_acquire_output_claims_for_runner",
        mutate_then_claim,
    )
    rerun_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_media_extract_metadata_action(
            sheet_id,
            row_ids,
            idempotency_key="v1-http-metadata-race-rerun",
            replace_existing=True,
        ),
    )

    assert rerun_response.status_code == 400, rerun_response.text
    rerun = ActionResult.model_validate(rerun_response.json())
    assert rerun.status == "failed"
    assert rerun.errors[0].code == "map_rows_failed"
    assert int(project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]) == (
        run_count
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM output_column_claims WHERE status='active'"
        ).fetchone()[0]
        == 0
    )


def test_v1_queued_post_claim_preparation_exception_cleans_claim_and_reservation(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(metadata, "_resolve_exiftool_path", lambda: None)
    monkeypatch.setattr(metadata, "_resolve_ffprobe_path", lambda: None)
    client = TestClient(
        create_app(tmp_path / "ws", run_status_grace_seconds=3600.0),
        raise_server_exceptions=False,
    )
    project_id, sheet_id, row_ids, _blobs = _seed_media_ocr_project(client)
    first_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_media_extract_metadata_action(
            sheet_id,
            row_ids,
            idempotency_key="v1-http-metadata-precheck-error-first",
        ),
    )
    assert first_response.status_code == 200, first_response.text
    drain_queue(client)
    project = client.app.state.workspace.get(project_id)
    run_count = int(project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0])

    armed = False
    acquire = action_runtime_reservations._acquire_output_claims_for_runner

    def claim_then_arm(*args, **kwargs):  # noqa: ANN002, ANN003
        nonlocal armed
        result = acquire(*args, **kwargs)
        armed = result is None
        return result

    def raise_after_claim(*args, **kwargs):  # noqa: ANN002, ANN003
        assert armed
        raise RuntimeError("queued post-claim metadata preparation exploded")

    monkeypatch.setattr(
        action_runtime_reservations, "_acquire_output_claims_for_runner", claim_then_arm
    )
    monkeypatch.setattr(MapRunner, "_prepare", raise_after_claim)
    key = "v1-http-metadata-precheck-error-rerun"
    rerun_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_media_extract_metadata_action(
            sheet_id,
            row_ids,
            idempotency_key=key,
            replace_existing=True,
        ),
    )

    assert rerun_response.status_code == 400, rerun_response.text
    assert armed
    assert (
        ActionResult.model_validate(rerun_response.json()).errors[0].code
        == "map_rows_failed"
    )
    assert int(project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]) == (
        run_count
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM output_column_claims WHERE status='active'"
        ).fetchone()[0]
        == 0
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM receipts WHERE idempotency_key=?", (key,)
        ).fetchone()[0]
        == 0
    )


def test_v1_action_run_queues_media_transcribe_and_worker_finalizes_receipt(
    tmp_path,
    monkeypatch,
) -> None:
    calls: list[str] = []

    async def fake_faster_whisper(
        self: transcribe_engines.FasterWhisperAdapter,
        path: str,
        spec: dict,
        *,
        should_cancel: Any = None,
    ) -> dict[str, object]:
        del self, spec
        digest = path.rsplit("/", 1)[-1]
        calls.append(digest)
        label = "one" if len(calls) == 1 else "two"
        return {
            "text": f"episode {label} transcript",
            "segments": [{"start": 0.0, "end": 0.5, "text": f"episode {label}"}],
            "language": "en",
            "duration": 0.5,
        }

    monkeypatch.setattr(
        transcribe_engines.FasterWhisperAdapter,
        "transcribe",
        fake_faster_whisper,
    )
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id, sheet_id, row_ids, blobs = _seed_media_transcribe_project(client)

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        json=_media_transcribe_action(sheet_id, row_ids),
    )
    assert response.status_code == 200, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "queued"
    assert result.action.kind == "media.transcribe"
    assert result.run_id is not None
    assert result.job_id is not None
    assert result.receipt_id is not None
    assert calls == []

    job = client.app.state.workspace.queue.get(result.job_id)
    assert job is not None
    assert job.kind == RUN_PROJECT_KIND
    assert job.payload["project_id"] == project_id
    assert job.payload["run_id"] == result.run_id
    assert job.payload["spec"]["action_kind"] == "media.transcribe"
    assert "recipe" not in job.payload["spec"]
    assert job.payload["spec"]["output_names"]["text"] == "transcript"
    assert job.payload["v1_receipt_id"] == result.receipt_id
    assert job.payload["action_kind"] == "media.transcribe"
    assert set(job.payload["v1_input_column_ids"]) == {"media"}
    snapshot = job.payload["v1_row_source_snapshot"]
    assert snapshot["row_ids"] == row_ids
    (source,) = snapshot["sources"]
    assert source["column"]["name"] == "media"
    assert {value["blob"]["blob_hash"] for value in source["values"]} == set(blobs)

    status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert status.status_code == 200, status.text
    public_status = status.json()["run"]["public_status"]
    assert public_status["status"] == "queued"
    assert public_status["action_kind"] == "media.transcribe"
    assert public_status["action_kind"] == "media.transcribe"
    assert public_status["queue"]["job_id"] == result.job_id

    replay = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        json=_media_transcribe_action(sheet_id, row_ids),
    )
    assert replay.status_code == 200, replay.text
    replay_result = ActionResult.model_validate(replay.json())
    assert replay_result.status == "queued"
    assert replay_result.run_id == result.run_id
    assert replay_result.job_id == result.job_id
    assert replay_result.receipt_id == result.receipt_id
    assert calls == []

    project = client.app.state.workspace.get(project_id)
    prepared_head = project.db.execute(
        "SELECT id, promise_set_id FROM routes "
        "WHERE subject_kind='run' AND subject_id=? ORDER BY seq DESC LIMIT 1",
        (str(result.run_id),),
    ).fetchone()
    assert prepared_head is not None
    drain_queue(client)
    assert calls == blobs
    completed_heads = project.db.execute(
        "SELECT id, promise_set_id FROM routes "
        "WHERE subject_kind='run' AND subject_id=? ORDER BY seq",
        (str(result.run_id),),
    ).fetchall()
    assert [tuple(row) for row in completed_heads] == [tuple(prepared_head)]
    completed_status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert completed_status.status_code == 200, completed_status.text
    assert completed_status.json()["run"]["public_status"]["status"] == "completed"
    completed_receipt = (
        client.app.state.workspace.get(project_id)
        .db.execute(
            "SELECT run_id, action_kind, status, body FROM receipts WHERE id=?",
            (result.receipt_id,),
        )
        .fetchone()
    )
    assert completed_receipt is not None
    assert completed_receipt["run_id"] == result.run_id
    assert completed_receipt["action_kind"] == "media.transcribe"
    assert completed_receipt["status"] == "completed"
    receipt_body = json.loads(completed_receipt["body"])
    evidence_kinds = {item["ref"]["kind"] for item in receipt_body["evidence"]}
    assert {
        "typed_action_request",
        "map_rows_run_counts",
        "media_transcribe_temporal_evidence_link",
    } <= evidence_kinds


def test_v1_action_run_queues_media_ocr_and_worker_finalizes_receipt(
    tmp_path,
    monkeypatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_page_images(
        self: OcrEngines,
        path: Any,
        media: Any,
        spec: dict[str, Any],
        scratch: Any,
    ) -> list[Any]:
        del self, media, scratch
        calls.append({"stage": "pages", "path": path.name, "dpi": spec.get("dpi")})
        return [path]

    async def fake_rapidocr(
        self: OcrEngines,
        page_paths: list[Any],
        scratch: Any,
        language: str | None,
    ) -> list[dict[str, Any]]:
        del self, scratch
        calls.append(
            {
                "stage": "ocr",
                "paths": [path.name for path in page_paths],
                "language": language,
            }
        )
        label = (
            "one"
            if len([call for call in calls if call["stage"] == "ocr"]) == 1
            else "two"
        )
        return [
            {
                "text": f"scan {label} visible text",
                "blocks": [
                    {
                        "text": f"scan {label}",
                        "bbox": [[0, 0], [1, 0], [1, 1], [0, 1]],
                        "score": 0.99,
                    }
                ],
            }
        ]

    monkeypatch.setattr(OcrEngines, "_page_images", fake_page_images)
    monkeypatch.setattr(OcrEngines, "_ocr_rapidocr", fake_rapidocr)
    monkeypatch.setattr(
        "frisket.ops.ocr_engines_local._rapidocr_model_root_dir",
        lambda language=None: None,
    )
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id, sheet_id, row_ids, blobs = _seed_media_ocr_project(client)

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        json=_media_ocr_action(sheet_id, row_ids),
    )
    assert response.status_code == 200, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "queued"
    assert result.action.kind == "media.ocr"
    assert result.run_id is not None
    assert result.job_id is not None
    assert result.receipt_id is not None
    assert calls == []

    project = client.app.state.workspace.get(project_id)
    assert (
        project.db.execute(
            """
        SELECT 1
        FROM results rr
        JOIN columns c ON c.id = rr.column_id
        WHERE rr.run_id=? AND c.name IN ('ocr_text', 'ocr_text_blocks')
        """,
            (result.run_id,),
        ).fetchone()
        is None
    )

    job = client.app.state.workspace.queue.get(result.job_id)
    assert job is not None
    assert job.kind == RUN_PROJECT_KIND
    assert job.payload["project_id"] == project_id
    assert job.payload["run_id"] == result.run_id
    assert job.payload["spec"]["action_kind"] == "media.ocr"
    assert "recipe" not in job.payload["spec"]
    assert job.payload["spec"]["output_names"]["text"] == "ocr_text"
    assert job.payload["spec"]["dpi"] == 180
    assert job.payload["v1_receipt_id"] == result.receipt_id
    assert job.payload["action_kind"] == "media.ocr"
    assert set(job.payload["v1_input_column_ids"]) == {"media"}
    snapshot = job.payload["v1_row_source_snapshot"]
    assert snapshot["row_ids"] == row_ids
    (source,) = snapshot["sources"]
    assert source["column"]["name"] == "media"
    assert {value["blob"]["blob_hash"] for value in source["values"]} == set(blobs)

    status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert status.status_code == 200, status.text
    public_status = status.json()["run"]["public_status"]
    assert public_status["status"] == "queued"
    assert public_status["action_kind"] == "media.ocr"
    assert public_status["action_kind"] == "media.ocr"
    assert public_status["queue"]["job_id"] == result.job_id

    replay = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        json=_media_ocr_action(sheet_id, row_ids),
    )
    assert replay.status_code == 200, replay.text
    replay_result = ActionResult.model_validate(replay.json())
    assert replay_result.status == "queued"
    assert replay_result.run_id == result.run_id
    assert replay_result.job_id == result.job_id
    assert replay_result.receipt_id == result.receipt_id
    assert calls == []

    drain_queue(client)
    assert [call["stage"] for call in calls] == ["pages", "ocr", "pages", "ocr"]
    completed_status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert completed_status.status_code == 200, completed_status.text
    assert completed_status.json()["run"]["public_status"]["status"] == "completed"
    completed_receipt = project.db.execute(
        "SELECT run_id, action_kind, status, body FROM receipts WHERE id=?",
        (result.receipt_id,),
    ).fetchone()
    assert completed_receipt is not None
    assert completed_receipt["run_id"] == result.run_id
    assert completed_receipt["action_kind"] == "media.ocr"
    assert completed_receipt["status"] == "completed"
    receipt_body = json.loads(completed_receipt["body"])
    output_kinds = {item["ref"]["kind"] for item in receipt_body["outputs"]}
    evidence_kinds = {item["ref"]["kind"] for item in receipt_body["evidence"]}
    assert "map_result_column" in output_kinds
    assert {
        "typed_action_request",
        "map_rows_run_counts",
        "media_ocr_grounding_evidence_link",
    } <= evidence_kinds


def test_v1_action_run_queues_media_to_markdown_and_worker_finalizes_receipt(
    tmp_path,
    monkeypatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_markitdown(
        self: _BoundDocumentConverter,
        path: Any,
        scratch: Any,
    ) -> str:
        del self, scratch
        calls.append({"path": path.name})
        return f"# Converted {path.name}\n\nDocument body"

    monkeypatch.setattr(
        _BoundDocumentConverter,
        "_convert_markitdown",
        fake_markitdown,
    )
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id, sheet_id, row_ids, blobs = _seed_media_to_markdown_project(client)

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        json=_media_to_markdown_action(sheet_id, row_ids),
    )
    assert response.status_code == 200, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "queued"
    assert result.action.kind == "media.to_markdown"
    assert result.run_id is not None
    assert result.job_id is not None
    assert result.receipt_id is not None
    assert calls == []

    project = client.app.state.workspace.get(project_id)
    assert (
        project.db.execute(
            """
        SELECT 1
        FROM results rr
        JOIN columns c ON c.id = rr.column_id
        WHERE rr.run_id=? AND c.name IN ('markdown', 'markdown_ocr_used')
        """,
            (result.run_id,),
        ).fetchone()
        is None
    )

    job = client.app.state.workspace.queue.get(result.job_id)
    assert job is not None
    assert job.kind == RUN_PROJECT_KIND
    assert job.payload["project_id"] == project_id
    assert job.payload["run_id"] == result.run_id
    assert job.payload["spec"]["action_kind"] == "media.to_markdown"
    assert "recipe" not in job.payload["spec"]
    assert job.payload["spec"]["output_names"] == {"markdown": "markdown"}
    assert job.payload["spec"]["engine"] == "markitdown"
    assert job.payload["v1_receipt_id"] == result.receipt_id
    assert job.payload["action_kind"] == "media.to_markdown"
    assert set(job.payload["v1_input_column_ids"]) == {"doc"}
    snapshot = job.payload["v1_row_source_snapshot"]
    assert snapshot["row_ids"] == row_ids
    (source,) = snapshot["sources"]
    assert source["column"]["name"] == "doc"
    assert {value["blob"]["blob_hash"] for value in source["values"]} == set(blobs)

    status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert status.status_code == 200, status.text
    public_status = status.json()["run"]["public_status"]
    assert public_status["status"] == "queued"
    assert public_status["action_kind"] == "media.to_markdown"
    assert public_status["action_kind"] == "media.to_markdown"
    assert public_status["queue"]["job_id"] == result.job_id

    replay = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        json=_media_to_markdown_action(sheet_id, row_ids),
    )
    assert replay.status_code == 200, replay.text
    replay_result = ActionResult.model_validate(replay.json())
    assert replay_result.status == "queued"
    assert replay_result.run_id == result.run_id
    assert replay_result.job_id == result.job_id
    assert replay_result.receipt_id == result.receipt_id
    assert calls == []

    drain_queue(client)
    assert [call["path"] for call in calls] == ["doc.html", "doc.html"]
    completed_status = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    )
    assert completed_status.status_code == 200, completed_status.text
    assert completed_status.json()["run"]["public_status"]["status"] == "completed"
    completed_receipt = project.db.execute(
        "SELECT run_id, action_kind, status, body FROM receipts WHERE id=?",
        (result.receipt_id,),
    ).fetchone()
    assert completed_receipt is not None
    assert completed_receipt["run_id"] == result.run_id
    assert completed_receipt["action_kind"] == "media.to_markdown"
    assert completed_receipt["status"] == "completed"
    receipt_body = json.loads(completed_receipt["body"])
    output_refs = [item["ref"] for item in receipt_body["outputs"]]
    evidence_kinds = {item["ref"]["kind"] for item in receipt_body["evidence"]}
    assert output_refs[0]["value_hash"].startswith("sha256:")
    assert output_refs == [
        {
            "kind": "map_result_column",
            "name": "markdown",
            "sheet_id": sheet_id,
            "column_id": output_refs[0]["column_id"],
            "run_id": result.run_id,
            "op_id": output_refs[0]["op_id"],
            "row_ids": row_ids,
            "type": "text",
            "format": "markdown",
            "value_hash": output_refs[0]["value_hash"],
        }
    ]
    assert {
        "typed_action_request",
        "map_rows_run_counts",
    } <= evidence_kinds
    request_evidence = next(
        item["ref"]
        for item in receipt_body["evidence"]
        if item["ref"]["kind"] == "typed_action_request"
    )
    assert request_evidence["params"] == {"source": "doc", "engine": "markitdown"}
    assert request_evidence["scope"]["row_ids"] == row_ids
    assert project.get_values(sheet_id, output_refs[0]["column_id"]) == {
        row_id: "# Converted doc.html\n\nDocument body" for row_id in row_ids
    }


def test_v1_action_run_queued_media_to_markdown_rejects_stale_input_before_conversion(
    tmp_path,
    monkeypatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_markitdown(
        self: _BoundDocumentConverter,
        path: Any,
        scratch: Any,
    ) -> str:
        del self, scratch
        calls.append({"path": path.name})
        return f"# Converted {path.name}\n\nDocument body"

    monkeypatch.setattr(
        _BoundDocumentConverter,
        "_convert_markitdown",
        fake_markitdown,
    )
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id, sheet_id, row_ids, _blobs = _seed_media_to_markdown_project(client)

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        params={},
        json=_media_to_markdown_action(
            sheet_id,
            row_ids,
            idempotency_key="v1-http-queued-to-markdown-stale@sha256:stable",
        ),
    )
    assert response.status_code == 200, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "queued"
    assert result.run_id is not None
    assert result.job_id is not None
    assert result.receipt_id is not None

    project = client.app.state.workspace.get(project_id)
    doc_column = project.db.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name='doc' AND hidden=0",
        (sheet_id,),
    ).fetchone()
    assert doc_column is not None
    replace_test_source_cell(
        project,
        row_id=row_ids[0],
        column_id=int(doc_column["id"]),
        value="changed document body after queue reservation",
    )

    drain_queue(client)
    assert calls == []
    failed_run = project.db.execute(
        "SELECT status FROM runs WHERE id=?", (result.run_id,)
    ).fetchone()
    assert failed_run is not None
    assert failed_run["status"] == "failed"
    receipt_row = project.db.execute(
        "SELECT run_id, action_kind, status, body FROM receipts WHERE id=?",
        (result.receipt_id,),
    ).fetchone()
    assert receipt_row is not None
    assert receipt_row["run_id"] == result.run_id
    assert receipt_row["action_kind"] == "media.to_markdown"
    assert receipt_row["status"] == "failed"
    receipt_body = json.loads(receipt_row["body"])
    assert receipt_body["errors"][0]["code"] == "stale_input"
    assert (
        project.db.execute(
            """
        SELECT 1
        FROM results rr
        JOIN columns c ON c.id = rr.column_id
        WHERE rr.run_id=? AND c.name IN ('markdown', 'markdown_ocr_used')
        """,
            (result.run_id,),
        ).fetchone()
        is None
    )


def _materialize_reserved_output_family(
    project: Any,
    *,
    sheet_id: int,
    reserved: dict[str, Any],
) -> None:
    """Mirror prepare_run's output-family step before testing enqueue replay."""

    OutputFamilyStore(project).create_or_reuse(
        sheet_id=sheet_id,
        fields=reserved["output_fields"],
    )


def test_v1_media_queued_adapter_replay_before_enqueue_is_retryable_then_pollable(
    tmp_path,
) -> None:
    client = TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))
    project_id, sheet_id, row_ids, _blobs = _seed_media_to_markdown_project(client)
    project = client.app.state.workspace.get(project_id)
    body = _media_to_markdown_action(
        sheet_id,
        row_ids,
        idempotency_key="v1-media-queued-adapter-window@sha256:stable",
    )
    queued = queued_v1_action_request(body)
    assert queued is not None
    action, params, entry = queued.action, queued.params, queued.entry

    reserved = entry.reserve_action(
        project,
        action,
        params,
        project_id=project_id,
        program=queued.program,
    )
    assert isinstance(reserved, dict)

    duplicate_before_enqueue = entry.reserve_action(
        project,
        action,
        params,
        project_id=project_id,
        program=queued.program,
    )
    assert isinstance(duplicate_before_enqueue, ActionResult)
    assert duplicate_before_enqueue.status == "failed"
    assert duplicate_before_enqueue.run_id is None
    assert duplicate_before_enqueue.job_id is None
    assert duplicate_before_enqueue.receipt_id is None
    assert duplicate_before_enqueue.errors[0].code == "idempotency_in_progress"
    assert duplicate_before_enqueue.errors[0].details == {
        "receipt_id": reserved["receipt_id"],
        "retryable": True,
    }

    op_id = project.append_op(
        "test.queue_adapter_window",
        {"reason": "simulate prepare_run before enqueue evidence"},
    )
    _materialize_reserved_output_family(
        project,
        sheet_id=sheet_id,
        reserved=reserved,
    )
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "media.to_markdown",
        model="markitdown",
        total_rows=len(row_ids),
    )
    entry.mark_enqueued(
        project,
        receipt_id=reserved["receipt_id"],
        run_id=run_id,
        job_id=84,
    )
    duplicate_after_enqueue = entry.reserve_action(
        project,
        action,
        params,
        project_id=project_id,
        program=queued.program,
    )
    assert isinstance(duplicate_after_enqueue, ActionResult)
    assert duplicate_after_enqueue.status == "queued"
    assert duplicate_after_enqueue.run_id == run_id
    assert duplicate_after_enqueue.job_id == 84
    assert duplicate_after_enqueue.receipt_id == reserved["receipt_id"]
