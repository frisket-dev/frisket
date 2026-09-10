"""Grounding-only files are admitted inputs, never model prompt values."""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field, ValidationError

from executor_harness import run_action_with_confirmation
from frisket.ai.llm import LLMResponse, ModelRouter
from frisket.actions.extract import ExtractParams
from frisket.actions.core import model_rows
from frisket.actions.model_rows import AskParams, ask
from frisket.actions.types import ColumnRef, ModelPrompt, Row, discover_references
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.execution_routes import instance_principal
from frisket.engine.store.media_blobs import media_cell, owned_media_metadata_document
from frisket.engine.store.receipts import ReceiptStore
from frisket.execution.consent_coverage import ConsentCoverage
from frisket.server.app import create_app
from http_test_helpers import drain_queue, post_v1_action_with_exact_confirmation


class _Adapter:
    def __init__(self, before_reply=None):
        self.requests = []
        self.before_reply = before_reply

    async def complete(self, request, client):
        del client
        self.requests.append(request)
        if self.before_reply:
            self.before_reply()
        return LLMResponse(
            content=None,
            data={"amount": {"value": "$42", "evidence": [{"page": 1}]}},
            tokens_in=12,
            tokens_out=5,
            cost=0.001,
            model=request.model,
        )


def _router(before_reply=None):
    adapter = _Adapter(before_reply)
    router = ModelRouter(keys={"anthropic": "stub"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = adapter
    return router, adapter


def _seed(project):
    sheet = project.add_sheet("Documents")
    columns = {
        "body": project.add_column(sheet, "body", "text"),
        "document": project.add_column(sheet, "document", "file"),
    }
    files = [
        media_cell(
            project.add_blob(
                f"%PDF-1.4 {name}".encode(),
                filename=f"{name}.pdf",
                mime="application/pdf",
                metadata=owned_media_metadata_document(
                    probe={"kind": "pdf", "pages": 1}
                ),
            ),
            filename=f"{name}.pdf",
            mime="application/pdf",
        )
        for name in ("private-original", "private-replacement")
    ]
    row = project.add_rows(
        sheet, [{"body": "Contract amount: $42.", "document": files[0]}], columns
    )[0]
    return sheet, columns, row, files


def _request(sheet, *, template=False):
    return {
        "action_id": "map.extract",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {
            "source": {"text": "Converted document: {{body}}"}
            if template
            else ["body"],
            "model": "anthropic/claude-haiku-4-5",
            "fields": [{"name": "amount", "type": "text"}],
            "source_document_columns": ["document"],
            "grounding": {
                "enabled": True,
                "citation_required": True,
            },
        },
        "idempotency_key": "grounding-source-capture",
    }


def _replace(project, seeded):
    _, columns, row, files = seeded
    project.apply_edits(
        [{"row_id": row, "column_id": columns["document"], "value": files[1]}]
    )


def test_grounding_columns_have_one_typed_reference_authority():
    request = _request(1)
    params = ExtractParams.model_validate(request["params"])
    assert [ref.column for ref in discover_references(params)] == ["body", "document"]
    assert params.model_dump(mode="json")["source_document_columns"] == ["document"]
    request["params"]["grounding"]["source_document_columns"] = ["document"]
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ExtractParams.model_validate(request["params"])


class _PromptWithEvidence(AskParams):
    documents: list[ColumnRef[Any]] = Field(default_factory=list)


class _Reply(BaseModel):
    answer: str


def _render(params: _PromptWithEvidence, row: Row) -> ModelPrompt[_Reply]:
    return ModelPrompt(messages=({"role": "user", "content": str(row.values)},))


def test_model_rows_can_select_prompt_source_from_other_typed_references():
    assert model_rows(ask).source_param == "source"
    assert model_rows(_render, source_param="source").source_param == "source"
    with pytest.raises(TypeError, match="exactly one typed source"):
        model_rows(_render)
    for invalid in ("missing", "model", "question", ""):
        with pytest.raises(TypeError, match="must name a typed source field"):
            model_rows(_render, source_param=invalid)


@pytest.mark.parametrize("template", [False, True])
def test_custom_model_rows_keeps_auxiliary_references_out_of_provider(
    tmp_path, template
):
    from frisket.actions.core import ActionCategory, RegisteredAction, action
    from frisket.actions.types import ActionRequest
    from test_model_rows_actions import _data_router, _run_third_party

    declaration = RegisteredAction(
        "custom.prompt",
        action(
            name="prompt",
            title="Prompt",
            description="Isolate prompt data from other admitted references.",
            category=ActionCategory.TEXT,
            run=model_rows(_render, source_param="source"),
        ),
    )
    project = Project.create(tmp_path / "custom.frisket")
    try:
        sheet = project.add_sheet("Sources")
        columns = {
            name: project.add_column(sheet, name, "text")
            for name in ("body", "private")
        }
        project.add_rows(
            sheet, [{"body": "public", "private": "not-for-provider"}], columns
        )
        request = ActionRequest(
            action_id="custom.prompt",
            scope={"kind": "sheet_rows", "sheet_id": sheet},
            params={
                "source": {"text": "Source: {{body}}"} if template else ["body"],
                "documents": ["private"],
                "model": "anthropic/claude-haiku-4-5",
                "question": "What?",
            },
            idempotency_key="custom-prompt-isolation",
        )
        requests = []
        router = _data_router(requests, {"answer": "response"})
        gate = _run_third_party(project, request, router, declaration)
        assert gate.status == "needs_confirmation", gate.errors
        assert requests == []
        result = _run_third_party(
            project,
            request.model_copy(
                update={"confirmation": gate.errors[0].details["promise_set_hash"]}
            ),
            router,
            declaration,
        )
        assert result.status == "completed", result.errors
        assert len(requests) == 1
        assert "public" in json.dumps(requests[0].messages)
        assert "not-for-provider" not in json.dumps(requests[0].messages)
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        assert {item.ref["name"] for item in receipt.inputs} == {"body", "private"}
    finally:
        project.close()


def _assert_original_artifact(project, result, seeded, adapter):
    sheet, columns, row, files = seeded
    assert result.status == "completed", result.errors
    assert len(adapter.requests) == 1
    prompt = json.dumps(adapter.requests[0].messages)
    assert "Contract amount: $42." in prompt
    for file in files:
        assert file["blob"] not in prompt
        assert file["filename"] not in prompt
    assert "application/pdf" not in prompt
    artifact = project.db.execute(
        "SELECT a.blob_hash,a.source_column_id,a.source_sheet_id,a.source_row_id,s.page_start "
        "FROM evidence_links l JOIN evidence_link_spans ls ON ls.link_id=l.id "
        "JOIN source_spans s ON s.id=ls.span_id JOIN source_artifacts a ON a.id=s.artifact_id "
        "WHERE l.run_id=?",
        (result.run_id,),
    ).fetchone()
    assert artifact is not None
    assert tuple(artifact) == (files[0]["blob"], columns["document"], sheet, row, 1)
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    inputs = {item.ref["name"]: item.ref for item in receipt.inputs}
    assert set(inputs) == {"body", "document"}
    assert inputs["document"]["column_id"] == columns["document"]


@pytest.mark.parametrize("template", [False, True])
@pytest.mark.parametrize("replace_during_call", [False, True])
def test_grounding_file_is_captured_but_never_rendered(
    tmp_path, template, replace_during_call
):
    project = Project.create(tmp_path / "documents.frisket")
    try:
        seeded = _seed(project)
        router, adapter = _router(
            (lambda: _replace(project, seeded)) if replace_during_call else None
        )
        result = run_action_with_confirmation(
            project,
            _request(seeded[0], template=template),
            project_id="p",
            router=router,
        )
        _assert_original_artifact(project, result, seeded, adapter)
    finally:
        project.close()


def test_grounding_file_change_invalidates_exact_confirmation(tmp_path):
    project = Project.create(tmp_path / "documents.frisket")
    try:
        seeded = _seed(project)
        router, adapter = _router()
        request = _request(seeded[0])
        deps = ExecutorDeps(
            consent_coverage=ConsentCoverage(instance_principal(project), Decimal("0"))
        )
        gate = run_action_spec(
            project, request, project_id="p", router=router, deps=deps
        )
        assert gate.status == "needs_confirmation"
        request["confirmation"] = gate.errors[0].details["promise_set_hash"]
        _replace(project, seeded)
        retry = run_action_spec(
            project, request, project_id="p", router=router, deps=deps
        )
        assert retry.status == "needs_confirmation", retry.errors
        assert retry.errors[0].details["promise_set_hash"] != request["confirmation"]
        assert adapter.requests == []
        assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    finally:
        project.close()


@pytest.mark.parametrize("change_after_queue", [False, True])
def test_queued_grounding_keeps_admitted_file_identity(tmp_path, change_after_queue):
    router, adapter = _router()
    with TestClient(create_app(tmp_path / "workspace", router=router)) as client:
        pid = client.post("/api/projects", json={"name": "Documents"}).json()["id"]
        project = client.app.state.workspace.get(pid)
        seeded = _seed(project)
        launched = post_v1_action_with_exact_confirmation(
            client, pid, _request(seeded[0])
        )
        assert launched.status_code == 200, launched.text
        queued = launched.json()
        assert queued["status"] == "queued"
        if change_after_queue:
            _replace(project, seeded)
        drain_queue(client)
        receipt = ReceiptStore(project).parsed_by_id(queued["receipt_id"])
        assert receipt.status != "queued", client.app.state.workspace.queue.get(
            queued["job_id"]
        ).error
        if change_after_queue:
            assert receipt.status == "failed", receipt.errors
            assert adapter.requests == []
            assert (
                project.db.execute(
                    "SELECT COUNT(*) FROM evidence_links WHERE run_id=?",
                    (queued["run_id"],),
                ).fetchone()[0]
                == 0
            )
        else:
            _assert_original_artifact(project, receipt, seeded, adapter)


@pytest.mark.parametrize("bad_source", ["missing", "model_file"])
def test_grounding_references_are_validated_without_permitting_model_files(
    tmp_path, bad_source
):
    project = Project.create(tmp_path / "documents.frisket")
    try:
        seeded = _seed(project)
        router, adapter = _router()
        request = copy.deepcopy(_request(seeded[0]))
        if bad_source == "missing":
            request["params"]["source_document_columns"] = ["missing"]
        else:
            request["params"]["source"] = ["document"]
        result = run_action_spec(project, request, project_id="p", router=router)
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_input_ref"
        assert adapter.requests == []
        assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    finally:
        project.close()


@pytest.mark.parametrize("template", [False, True])
def test_grounding_only_transcript_never_augments_model_prompt(
    tmp_path, monkeypatch, template
):
    from test_extract_scalar_temporal_anchors import (
        _TRANSCRIPT_SEGMENTS,
        _run_transcribe,
        _seed_transcript_project,
        _stub_router,
    )

    seeded = _seed_transcript_project(tmp_path)
    project = seeded["project"]
    try:
        _run_transcribe(seeded, monkeypatch)
        router, adapter = _stub_router(
            {
                "amount": {
                    "value": "claim",
                    "evidence": [{"segment_indices": [2]}],
                }
            }
        )
        request = _request(seeded["sheet_id"])
        request["params"]["source"] = (
            {"text": "Episode title: {{title}}"} if template else ["title"]
        )
        request["params"]["source_document_columns"] = ["transcript"]
        result = run_action_with_confirmation(
            project, request, project_id="p", router=router
        )
        assert result.status == "completed", result.errors
        assert len(adapter.requests) == 1
        prompt = json.dumps(adapter.requests[0].messages)
        assert "Episode 1" in prompt
        for segment in _TRANSCRIPT_SEGMENTS:
            assert segment["text"] not in prompt
        assert "transcript_segments" not in prompt
        # The hidden-to-model source is still available for authoritative
        # evidence publication, anchored to its captured transcription run.
        artifact = project.db.execute(
            "SELECT a.blob_hash,s.start_ms,s.end_ms FROM evidence_links l "
            "JOIN evidence_link_spans ls ON ls.link_id=l.id "
            "JOIN source_spans s ON s.id=ls.span_id "
            "JOIN source_artifacts a ON a.id=s.artifact_id WHERE l.run_id=?",
            (result.run_id,),
        ).fetchone()
        assert artifact is not None
        assert tuple(artifact) == (seeded["blob"], 4000, 6000)
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        assert {item.ref["name"] for item in receipt.inputs} == {"title", "transcript"}
    finally:
        project.close()
