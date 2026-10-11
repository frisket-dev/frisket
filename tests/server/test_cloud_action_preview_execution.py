from __future__ import annotations

import io
import json
from typing import Any

from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfWriter

from frisket.ai.llm import ModelRouter
from frisket.cloud_connection import CloudConnectionStore
from frisket.contracts.http.cloud_connection import (
    CloudAccount,
    CloudCapabilitiesResponse,
    CloudCapabilityRoute,
    CloudDocumentConvertOptionSupport,
    CloudDocumentConvertResult,
    CloudDocumentConvertUsage,
    CloudExecutionReceipt,
    CloudExecutionResponse,
    CloudLLMOptionSupport,
    CloudLLMResult,
    CloudLLMUsage,
    CloudOCROptionSupport,
    CloudOCRPage,
    CloudOCRResult,
    CloudOCRUsage,
    CloudSessionResponse,
    CloudTokenPrice,
    CloudUnitPrice,
    CloudUser,
)
from frisket.engine.store.media_blobs import (
    media_cell,
    owned_media_metadata_document,
)
from frisket.project_settings import ProjectSettings, write_project_settings
from frisket.server.app import create_app
from tests.deterministic_time import controlled_time


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (12, 8), "white").save(output, format="PNG")
    return output.getvalue()


def _route(capability: str) -> CloudCapabilityRoute:
    if capability == "ocr":
        return CloudCapabilityRoute(
            capability=capability,
            engine="dots.mocr",
            provider="cloud-media",
            lane="frisket_credits",
            route_revision="ocr-route-1",
            ready=True,
            blocker=None,
            price=CloudUnitPrice(kind="unit", unit="page", micro_per_unit=2500),
            options=CloudOCROptionSupport(
                kind="ocr", language=True, geometry=True, searchable_pdf=False
            ),
        )
    if capability == "document.convert":
        return CloudCapabilityRoute(
            capability=capability,
            engine="datalab",
            provider="datalab",
            lane="account_key",
            route_revision="document-route-1",
            ready=True,
            blocker=None,
            price=CloudUnitPrice(kind="unit", unit="page", micro_per_unit=4000),
            options=CloudDocumentConvertOptionSupport(
                kind="document.convert", mime_types=["application/pdf"]
            ),
        )
    return CloudCapabilityRoute(
        capability="llm.complete",
        engine="openai/gpt-5-mini",
        provider="openai",
        lane="frisket_credits",
        route_revision="llm-route-1",
        ready=True,
        blocker=None,
        price=CloudTokenPrice(
            kind="tokens",
            input_micro_per_million_tokens=250_000,
            output_micro_per_million_tokens=2_000_000,
        ),
        options=CloudLLMOptionSupport(
            kind="llm",
            structured_output=True,
            tools=True,
            vision=True,
            max_output_tokens=65_536,
        ),
    )


class _CloudClient:
    origin = "https://cloud.example.test"

    def capabilities(self, token: str, account_id: int) -> CloudCapabilitiesResponse:
        assert token == "installation-token"
        assert account_id == 41
        return CloudCapabilitiesResponse(
            account_id=41,
            routes=[_route("ocr"), _route("document.convert"), _route("llm")],
        )

    def close(self) -> None:
        return None


class _Transport:
    def __init__(self) -> None:
        self.media_calls: list[tuple[Any, str, bytes]] = []
        self.llm_calls: list[tuple[Any, Any]] = []

    async def execute_media(
        self, attempt, *, capability, engine, options, uploads
    ) -> CloudExecutionResponse:
        del options
        body = b"".join(upload.stream.read() for upload in uploads)
        self.media_calls.append((attempt, capability, body))
        if capability == "ocr":
            result = CloudOCRResult(
                pages=[CloudOCRPage(page=1, text="Cloud OCR", blocks=[])]
            )
            usage = CloudOCRUsage(kind="ocr", pages=1)
            charge = 2500
        else:
            result = CloudDocumentConvertResult(
                markdown="# Cloud document", ocr_used=[]
            )
            usage = CloudDocumentConvertUsage(kind="document.convert", pages=1)
            charge = 0
        route = _route(capability)
        return CloudExecutionResponse(
            execution_id=f"cloud-media-{len(self.media_calls)}",
            status="completed",
            route=route,
            result=result,
            receipt=CloudExecutionReceipt(
                user_id=7,
                account_id=41,
                lane=route.lane,
                provider=route.provider,
                engine=engine,
                charge_micro=charge,
                provider_cost_micro=2000,
                usage=usage,
            ),
        )

    async def execute_llm(self, attempt, request) -> CloudExecutionResponse:
        self.llm_calls.append((attempt, request))
        repaired = len(self.llm_calls) > 1
        data = {"value": "Cloud result"} if repaired else {}
        return CloudExecutionResponse(
            execution_id=f"cloud-llm-{len(self.llm_calls)}",
            status="completed",
            route=_route("llm"),
            result=CloudLLMResult(
                content=json.dumps(data),
                data=data,
                tokens_in=12,
                tokens_out=4,
                cost=0.00003,
                model="openai/gpt-5-mini",
                cost_source="cloud_receipt",
                cached=False,
                provider="openai",
                credential_source="platform_key",
                raw={},
                output_limited=False,
            ),
            receipt=CloudExecutionReceipt(
                user_id=7,
                account_id=41,
                lane="frisket_credits",
                provider="openai",
                engine="openai/gpt-5-mini",
                charge_micro=30,
                provider_cost_micro=20,
                usage=CloudLLMUsage(kind="llm.complete", tokens_in=12, tokens_out=4),
            ),
        )


def _client(tmp_path) -> tuple[TestClient, _Transport]:
    store = CloudConnectionStore(tmp_path / "profile")
    cloud_client = _CloudClient()
    store.save_connection(
        "installation-token",
        CloudSessionResponse(
            user=CloudUser(id=7, email="user@example.test", display_name="User"),
            expires_at="2099-01-01T00:00:00Z",
            accounts=[
                CloudAccount(
                    id=41,
                    name="Personal",
                    kind="personal",
                    balance_micro=1_000_000,
                    available_micro=1_000_000,
                    manage_billing=True,
                )
            ],
        ),
    )
    store.save_capabilities(
        cloud_client.capabilities("installation-token", 41), expected_user_id=7
    )
    transport = _Transport()
    app = create_app(
        tmp_path / "workspace",
        router=ModelRouter(use_env_keys=False, cache_mode="off", max_retries=0),
        cloud_connection_store=store,
        cloud_connection_client=cloud_client,
        cloud_execution_transport=transport,
        serve_spa=False,
    )
    return TestClient(app), transport


def _pdf() -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(output)
    return output.getvalue()


def _select_cloud(client: TestClient, project_id: str) -> None:
    write_project_settings(
        client.app.state.workspace.get(project_id),
        ProjectSettings(
            cloud_user_id=7,
            cloud_account_id=41,
            cloud_execution_preference="cloud_first",
        ),
    )


def _poll(client: TestClient, project_id: str, preview_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {}

    def finished() -> bool:
        nonlocal result
        response = client.get(
            f"/api/projects/{project_id}/actions/v1/preview/{preview_id}"
        )
        assert response.status_code == 200, response.text
        result = response.json()
        return result["status"] != "running"

    with controlled_time(timeout=10) as clock:
        clock.wait_until(finished, message="Cloud preview did not finish")
    return result


def test_scratch_ocr_http_uses_admitted_cloud_binding(tmp_path) -> None:
    client, transport = _client(tmp_path)
    with client:
        project_id = client.post("/api/projects", json={"name": "OCR"}).json()["id"]
        _select_cloud(client, project_id)
        endpoint = f"/api/projects/{project_id}/ocr/compare-scratch"
        upload = {
            "files": {"file": ("scan.png", _png(), "image/png")},
            "data": {"payload": json.dumps({"engine": "dots.mocr"})},
        }
        quote = client.post(f"{endpoint}/estimate", **upload)
        assert quote.status_code == 200, quote.text
        confirmation = quote.json()["estimate"]["promise_set_hash"]
        upload["data"]["payload"] = json.dumps(
            {"engine": "dots.mocr", "confirmation": confirmation}
        )
        started = client.post(endpoint, **upload)
        assert started.status_code == 202, started.text
        result = _poll(client, project_id, started.json()["preview_id"])

    assert result["status"] == "done", result
    assert result["result"]["rows"][0]["text"]["value"] == "Cloud OCR"
    assert len(transport.media_calls) == 1
    attempt, capability, body = transport.media_calls[0]
    assert capability == "ocr" and body
    assert attempt.request_id.startswith("preview:")
    assert attempt.request_id.endswith(":row:1")
    assert attempt.payer.account_id == 41


def test_markdown_and_llm_http_previews_bind_cloud_per_row(tmp_path) -> None:
    client, transport = _client(tmp_path)
    with client:
        project_id = client.post(
            "/api/projects", json={"name": "Cloud previews"}
        ).json()["id"]
        _select_cloud(client, project_id)
        project = client.app.state.workspace.get(project_id)
        sheet_id = project.add_sheet("Documents")
        doc_column = project.add_column(sheet_id, "doc", type="file")
        text_column = project.add_column(sheet_id, "text", type="text")
        digests = [
            project.add_blob(
                _pdf(),
                filename=f"report-{index}.pdf",
                mime="application/pdf",
                metadata=owned_media_metadata_document(
                    probe={"kind": "pdf", "page_count": 1}
                ),
            )
            for index in (1, 2)
        ]
        row_ids = project.add_rows(
            sheet_id,
            [
                {
                    "doc": media_cell(
                        digest,
                        filename=f"report-{index}.pdf",
                        mime="application/pdf",
                    ),
                    "text": f"Extract this {index}",
                }
                for index, digest in enumerate(digests, 1)
            ],
            {"doc": doc_column, "text": text_column},
        )
        row_id = row_ids[0]

        markdown = {
            "action_id": "media.to_markdown",
            "scope": {
                "kind": "sheet_rows",
                "sheet_id": sheet_id,
                "row_ids": row_ids,
            },
            "params": {"source": "doc", "engine": "datalab"},
            "output_names": {"markdown": "converted"},
            "idempotency_key": "cloud-markdown-preview",
        }
        challenge = client.post(
            f"/api/projects/{project_id}/actions/v1/preview", json=markdown
        )
        assert challenge.status_code == 402, challenge.text
        markdown["confirmation"] = challenge.json()["error"]["details"][
            "promise_set_hash"
        ]
        started = client.post(
            f"/api/projects/{project_id}/actions/v1/preview", json=markdown
        )
        assert started.status_code == 202, started.text
        document_result = _poll(client, project_id, started.json()["preview_id"])

        extract = {
            "action_id": "map.extract",
            "scope": {
                "kind": "sheet_rows",
                "sheet_id": sheet_id,
                "row_ids": [row_id],
            },
            "params": {
                "source": ["text"],
                "model": "openai/gpt-5-mini",
                "instruction": "Extract the value",
                "fields": [{"name": "value", "type": "text"}],
                "grounding": {"enabled": False},
            },
            "idempotency_key": "cloud-llm-preview",
        }
        challenge = client.post(
            f"/api/projects/{project_id}/actions/v1/preview", json=extract
        )
        assert challenge.status_code == 402, challenge.text
        extract["confirmation"] = challenge.json()["error"]["details"][
            "promise_set_hash"
        ]
        started = client.post(
            f"/api/projects/{project_id}/actions/v1/preview", json=extract
        )
        assert started.status_code == 202, started.text
        llm_result = _poll(client, project_id, started.json()["preview_id"])

        call_ids = {
            row["id"]
            for row in project.db.execute(
                "SELECT id FROM model_calls WHERE id LIKE 'cloud-%'"
            ).fetchall()
        }
        child_count = project.db.execute(
            "SELECT COUNT(*) FROM effect_checkpoints WHERE family='model_call'"
        ).fetchone()[0]

    assert llm_result["status"] == "done", llm_result
    assert len(transport.llm_calls) == 2
    assert document_result["status"] == "done", document_result
    assert all(
        document_result["result"]["rows"][str(item)]["converted"]["value"]
        == "# Cloud document"
        for item in row_ids
    )
    assert len(transport.media_calls) == 2
    media_request_ids = [call[0].request_id for call in transport.media_calls]
    assert len(set(media_request_ids)) == 2
    assert {request_id.rsplit(":", 1)[-1] for request_id in media_request_ids} == {
        str(item) for item in row_ids
    }
    request_ids = [call[0].request_id for call in transport.llm_calls]
    assert len(set(request_ids)) == 2
    assert all(request_id.startswith("cloud_llm_") for request_id in request_ids)
    assert call_ids == {
        "cloud-media-1",
        "cloud-media-2",
        "cloud-llm-1",
        "cloud-llm-2",
    }
    assert child_count == 0
