"""PDF bytes survive real typed execution, structured completion and HTTP adapters."""

from __future__ import annotations

import base64
from io import BytesIO
import json

import httpx
from jsonschema import Draft202012Validator
import pytest
from pypdf import PdfReader, PdfWriter

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.types import ActionRequest
from frisket.ai.llm import ModelRouter
from frisket.engine.store import Project
from test_model_rows_actions import _run_third_party


@pytest.mark.parametrize(
    "provider,model",
    [
        ("anthropic", "anthropic/claude-haiku-4-5"),
        ("openai", "openai/gpt-5-mini"),
    ],
)
@pytest.mark.parametrize(
    "action,output",
    [
        ("map.extract", "person"),
        ("map.ask", "answer"),
        ("map.summarize", "summary"),
    ],
)
def test_typed_pdf_action_delivers_whole_and_selected_page_and_publishes_only_output(
    tmp_path,
    provider,
    model,
    action,
    output,
):
    stream = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=200)
    writer.add_blank_page(width=300, height=400)
    writer.write(stream)
    original = stream.getvalue()
    seen = []

    def respond(request):
        body = json.loads(request.content)
        seen.append(body)
        result = {output: "Ada"}
        if provider == "openai":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": json.dumps(result)},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 3},
                },
            )
        blocks = [{"type": "text", "text": json.dumps(result)}]
        if body.get("tool_choice", {}).get("name") == "emit":
            blocks = [
                {
                    "type": "tool_use",
                    "id": "fixture-output",
                    "name": "emit",
                    "input": result,
                }
            ]
        return httpx.Response(
            200,
            json={
                "content": blocks,
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 20, "output_tokens": 3},
            },
        )

    project = Project.create(tmp_path / "composed-pdf.frisket")
    try:
        sheet = project.add_sheet("Documents")
        columns = {
            name: project.add_column(sheet, name, type=kind)
            for name, kind in (("document", "file"), ("note", "text"))
        }
        digest = project.add_blob(
            original, filename="filing.pdf", mime="application/pdf"
        )
        cell = {"blob": digest, "filename": "filing.pdf", "mime": "application/pdf"}
        rows = project.add_rows(
            sheet,
            [
                {"document": cell, "note": "whole-document context"},
                {"document": {**cell, "page": 2}, "note": "selected-page context"},
            ],
            columns,
        )
        params = {"source": ["document", "note"], "model": model}
        if action == "map.ask":
            params["question"] = "Who is named?"
        if action == "map.extract":
            params["fields"] = [{"name": "person", "type": "text"}]
            # API-authored optional citations must be effectively off for PDF.
            params["grounding"] = {"enabled": True}
        request = ActionRequest.model_validate(
            {
                "action_id": action,
                "scope": {"kind": "sheet_rows", "sheet_id": sheet},
                "params": params,
                "output_names": {output: "published"},
                "idempotency_key": "pdf-composed@1",
            }
        )
        router = ModelRouter(
            keys={provider: "fixture-only"},
            use_env_keys=False,
            cache=None,
            cache_mode="off",
        )
        router._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        registered = ACTION_REGISTRY.get(action)
        gated = _run_third_party(project, request, router, registered)
        assert gated.status == "needs_confirmation", gated.errors
        assert seen == []
        request = request.model_copy(
            update={
                "confirmation": gated.errors[0].details["promise_set_hash"],
            }
        )
        result = _run_third_party(project, request, router, registered)
        assert result.status == "completed", result.errors
        assert len(seen) == 2
        delivered = {}
        for body in seen:
            parts = [
                part
                for message in body["messages"]
                for part in message["content"]
                if isinstance(part, dict)
            ]
            texts = "\n".join(part.get("text", "") for part in parts)
            documents = [part for part in parts if part["type"] in {"document", "file"}]
            assert len(documents) == 1
            document = documents[0]
            encoded = (
                document["source"]["data"]
                if provider == "anthropic"
                else document["file"]["file_data"].split(",", 1)[1]
            )
            data = base64.b64decode(encoded)
            delivered["selected" if "selected-page context" in texts else "whole"] = (
                data
            )
            assert "context" in texts
            assert encoded not in texts
            if action == "map.extract":
                schema = (
                    body.get("output_config", {}).get("format", {}).get("schema")
                    or body.get("response_format", {})
                    .get("json_schema", {})
                    .get("schema")
                    or body["tools"][0]["input_schema"]
                )
                field = Draft202012Validator(schema["properties"]["person"])
                assert field.is_valid("Ada")
                assert not field.is_valid({"value": "Ada", "evidence": []})
        assert delivered["whole"] == original
        selected = PdfReader(BytesIO(delivered["selected"]))
        assert len(selected.pages) == 1
        assert selected.pages[0].mediabox.width == 300
        stored = project.columns(sheet, include_hidden=True)
        assert {column["name"] for column in stored} == {
            "document",
            "note",
            "published",
        }
        published = next(
            column["id"] for column in stored if column["name"] == "published"
        )
        assert project.get_values(sheet, published, row_ids=rows) == dict.fromkeys(
            rows, "Ada"
        )
        assert len(project.visible_row_ids(sheet)) == 2
    finally:
        project.close()
