"""Documented OpenDocRouter HTTP fixtures; no inference or paid network calls."""

from __future__ import annotations

import asyncio
import copy
import json
from uuid import UUID
from pathlib import Path

import httpx
import pytest
from PIL import Image
from pypdf import PdfWriter

from frisket.ops.integrations import opendocrouter as api
from frisket.ops.integrations.hosted_error import HostedEngineError
from frisket.ops.opendocrouter import ocr_page

ENGINE = "opendocrouter/rednote-hilab/dots.mocr"
JOB = str(UUID(int=1))


def response(pages=1, *, status="completed"):
    return {
        "id": JOB,
        "model": "rednote-hilab/dots.mocr",
        "model_version": "1",
        "price_version": "2026-10-06",
        "status": status,
        "page_count": pages,
        "pages_done": pages if status != "processing" else 0,
        "pages": [
            {
                "page": i,
                "status": "ok",
                "markdown": f"# Page {i}\n\nText",
                "cached": False,
            }
            for i in range(1, pages + 1)
        ]
        if status != "processing"
        else [],
        "usage": {"input_tokens": 20, "output_tokens": 10}
        if status != "processing"
        else None,
        "charge_usd": 0.00397 if status != "processing" else None,
        "has_more": False,
        "next_cursor": None,
    }


@pytest.fixture
def png(tmp_path):
    path = tmp_path / "page.png"
    Image.new("RGB", (100, 200), "white").save(path)
    return path


def pdf(tmp_path, count):
    path = tmp_path / f"{count}.pdf"
    writer = PdfWriter()
    for _ in range(count):
        writer.add_blank_page(width=100, height=200)
    writer.write(path)
    return path


def run(path, handler, **kwargs):
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await api.parse_document(
                client,
                path=path,
                engine=ENGINE,
                api_key="test-secret",
                capability="ocr",
                credential_source="project_key",
                **kwargs,
            )

    return asyncio.run(execute())


def test_sync_inline_and_reported_cost(png):
    recorded = []

    def handler(request):
        assert request.headers["authorization"] == "Bearer test-secret"
        payload = json.loads(request.content)
        assert payload["document"]["mime_type"] == "image/png"
        assert payload["model"] == "rednote-hilab/dots.mocr"
        assert payload["cache"] is False and payload["mode"] == "sync"
        return httpx.Response(200, json=response())

    pages, accounting = run(
        png, handler, on_accounting=lambda a: recorded.append(copy.deepcopy(a))
    )
    assert pages[0]["markdown"] == "# Page 1\n\nText"
    fact = accounting["model_calls"][0]
    assert fact["provider_cost_usd"] == 0.00397
    assert fact["credential_source"] == "project_key"
    assert fact["units"]["price_version"] == "2026-10-06"
    assert recorded[0]["model_calls"][0]["id"] == fact["id"]


@pytest.mark.parametrize("bad", ["partial", "missing", "duplicate"])
def test_output_failure_preserves_provider_charge(tmp_path, bad):
    body = response(2)
    if bad == "partial":
        body["status"] = "partial"
        body["pages"][1] = {
            "page": 2,
            "status": "error",
            "error": {"code": "timeout", "message": "secret"},
        }
    elif bad == "missing":
        body["pages"].pop()
    else:
        body["pages"][1]["page"] = 1
    with pytest.raises(HostedEngineError) as error:
        run(pdf(tmp_path, 2), lambda r: httpx.Response(200, json=body))
    assert not error.value.provider_job_accepted
    assert error.value.accounting["cost"] == 0.00397
    assert "secret" not in str(error.value)


def test_async_poll_pagination_and_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "POLL_SECONDS", 0)
    calls = []
    full = response(51)

    def handler(request):
        calls.append((request.method, request.url.path, str(request.url.query)))
        if request.method == "POST":
            payload = json.loads(request.content)
            assert payload["mode"] == "async" and payload["cache"] is True
            return httpx.Response(202, json=response(51, status="processing"))
        if request.method == "DELETE":
            return httpx.Response(200, json={})
        body = copy.deepcopy(full)
        if not request.url.params.get("expand"):
            body["pages"] = []
        elif not request.url.params.get("cursor"):
            body.update(pages=body["pages"][:25], has_more=True, next_cursor=25)
        else:
            assert request.url.params["cursor"] == "25"
            body["pages"] = body["pages"][25:]
        return httpx.Response(200, json=body)

    pages, accounting = run(pdf(tmp_path, 51), handler)
    assert len(pages) == 51 and pages[-1]["page"] == 51
    assert calls[-1][0] == "DELETE"
    assert accounting["model_calls"][0]["units"]["pages"] == 51


def test_async_cancel_deletes_and_preserves_settled_cost(tmp_path):
    cancelled = False
    calls = []

    def handler(request):
        nonlocal cancelled
        calls.append(request.method)
        if request.method == "POST":
            cancelled = True
            return httpx.Response(202, json=response(51, status="processing"))
        return httpx.Response(200, json=response(51, status="partial"))

    with pytest.raises(HostedEngineError) as error:
        run(pdf(tmp_path, 51), handler, should_cancel=lambda: cancelled)
    assert calls == ["POST", "DELETE", "GET"]
    assert error.value.accounting["cost"] == 0.00397


def test_async_recorded_job_replay(tmp_path, monkeypatch):
    from dataclasses import replace
    from frisket.opendocrouter_catalog import current_catalog, DocumentCatalog

    captured = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/opendocrouter/async_job.json"
        ).read_text()
    )
    catalog = current_catalog()
    # Exercise async on the recorded two-page document without manufacturing
    # extra provider pages; production chooses async from the model's limit.
    monkeypatch.setattr(
        "frisket.opendocrouter_catalog._catalog",
        DocumentCatalog(
            catalog.price_version,
            tuple(replace(m, max_sync_pages=1) for m in catalog.models),
        ),
    )
    monkeypatch.setattr(api, "POLL_SECONDS", 0)
    steps = []

    def handler(request):
        if request.method == "POST":
            assert json.loads(request.content)["mode"] == "async"
            step = "submit"
        elif request.method == "DELETE":
            step = "delete"
        else:
            step = (
                "result_expanded"
                if request.url.params.get("expand")
                else "poll_completed"
            )
        steps.append(step)
        return httpx.Response(captured[step]["status"], json=captured[step]["body"])

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await api.parse_document(
                client,
                path=pdf(tmp_path, 2),
                engine="opendocrouter/anthropic/claude-haiku-5-5",
                api_key="test-secret",
                capability="ocr",
                credential_source="project_key",
                layout=True,
            )

    pages, accounting = asyncio.run(execute())
    assert steps == ["submit", "poll_completed", "result_expanded", "delete"]
    assert pages == captured["result_expanded"]["body"]["pages"]
    assert accounting["cost"] == captured["poll_completed"]["body"]["charge_usd"]


def test_upload_strips_client_secrets_and_blocks_private_destination(png, monkeypatch):
    monkeypatch.setattr(api, "INLINE_BYTES", 1)
    import frisket.ops.netguard as guard

    monkeypatch.setattr(
        guard, "safe_pinned_addresses", lambda url, **kw: ["93.184.216.34"]
    )
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("uploads"):
            return httpx.Response(
                200,
                json={
                    "upload_id": JOB,
                    "upload_url": "https://uploads.example.test/file?signature=private",
                    "max_bytes": api.MAX_BYTES,
                },
            )
        if request.method == "PUT":
            assert (
                "authorization" not in request.headers
                and "cookie" not in request.headers
            )
            return httpx.Response(200)
        return httpx.Response(200, json=response())

    async def execute():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "default-secret"},
            cookies={"secret": "cookie"},
        ) as client:
            return await api.parse_document(
                client,
                path=png,
                engine=ENGINE,
                api_key="test-secret",
                capability="ocr",
                credential_source="project_key",
            )

    asyncio.run(execute())
    assert [r.method for r in calls] == ["POST", "PUT", "POST"]
    monkeypatch.setattr(guard, "safe_pinned_addresses", lambda url, **kw: None)
    with pytest.raises(HostedEngineError):
        asyncio.run(execute())
    assert len(calls) == 4  # refused before any second upload PUT


@pytest.mark.parametrize("count", [50, 500, 501])
def test_pdf_input_limits(tmp_path, count):
    if count == 501:
        with pytest.raises(HostedEngineError, match="1–500"):
            api.input_details(pdf(tmp_path, count))
    else:
        assert api.input_details(pdf(tmp_path, count)) == ("application/pdf", count)


def test_layout_lines_and_block_coordinates():
    page = {
        "markdown": "# Heading\n\nTwo lines\ncontinued",
        "layout": {
            "status": "ok",
            "elements": [
                {
                    "type": "text",
                    "lines": [2, 3],
                    "confidence": 0.9,
                    "boxes": [
                        {"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.1},
                        {"x": 0.1, "y": 0.3, "w": 0.6, "h": 0.1},
                    ],
                },
                {
                    "type": "picture",
                    "lines": None,
                    "boxes": [{"x": 0, "y": 0, "w": 1, "h": 1}],
                },
            ],
        },
    }
    out = ocr_page(page, (100, 200))
    assert out["text"] == "Heading\n\nTwo lines\ncontinued"
    assert len(out["blocks"]) == 1
    assert out["blocks"][0] == {
        "text": "Two lines\ncontinued",
        "bbox": [[10, 40], [70, 40], [70, 80], [10, 80]],
        "score": 0.9,
    }
    page["layout"] = {"status": "error", "code": "timeout"}
    assert ocr_page(page, (100, 200)) == {
        "text": "Heading\n\nTwo lines\ncontinued",
        "blocks": [],
    }


def test_http_error_never_echoes_credentials_or_provider_text(png):
    with pytest.raises(HostedEngineError) as error:
        run(
            png,
            lambda r: httpx.Response(
                401, json={"message": "test-secret document contents"}
            ),
        )
    assert str(error.value) == "Check your OpenDocRouter API key."
    assert not error.value.retryable


def test_task_cancellation_drains_sync_response_and_records_charge(png):
    recorded = []

    async def execute():
        submitted = asyncio.Event()
        finish = asyncio.Event()

        async def handler(request):
            submitted.set()
            await finish.wait()
            return httpx.Response(200, json=response())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            task = asyncio.create_task(
                api.parse_document(
                    client,
                    path=png,
                    engine=ENGINE,
                    api_key="key",
                    capability="ocr",
                    credential_source="project_key",
                    on_accounting=lambda a: recorded.append(copy.deepcopy(a)),
                )
            )
            await submitted.wait()
            task.cancel()
            finish.set()
            with pytest.raises(HostedEngineError) as error:
                await task
            assert error.value.code == "cancelled"
            assert error.value.accounting["cost"] == 0.00397

    asyncio.run(execute())
    assert recorded[-1]["cost"] == 0.00397


def test_async_repeated_cursor_retains_paid_results(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "POLL_SECONDS", 0)
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == "DELETE":
            return httpx.Response(200, json={})
        if request.method == "POST":
            return httpx.Response(202, json=response(51, status="processing"))
        body = response(51)
        if request.url.params.get("expand"):
            body.update(pages=body["pages"][:1], has_more=True, next_cursor=1)
        return httpx.Response(200, json=body)

    with pytest.raises(HostedEngineError, match="pagination") as error:
        run(pdf(tmp_path, 51), handler)
    assert "DELETE" not in calls
    assert error.value.provider_job_accepted
    assert error.value.accounting["cost"] == 0.00397


@pytest.mark.parametrize("failure", ["poll", "results", "invalid_pages"])
def test_async_failure_keeps_job_identity_charge_and_results(
    tmp_path, monkeypatch, failure
):
    monkeypatch.setattr(api, "POLL_SECONDS", 0)
    monkeypatch.setattr(api, "READ_RETRY_SECONDS", 0, raising=False)
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(202, json=response(51, status="processing"))
        if request.method == "DELETE":
            return httpx.Response(200, json={})
        expanded = bool(request.url.params.get("expand"))
        if failure == "poll" or (failure == "results" and expanded):
            return httpx.Response(503)
        body = response(51)
        body["pages"] = []
        return httpx.Response(200, json=body)

    with pytest.raises(HostedEngineError) as error:
        run(pdf(tmp_path, 51), handler)
    assert calls.count("POST") == 1
    assert "DELETE" not in calls
    assert error.value.provider_job_accepted
    assert error.value.accounting["model_calls"][0]["request_id"] == JOB
    assert error.value.accounting["cost"] == (None if failure == "poll" else 0.00397)


@pytest.mark.parametrize("failure", ["transport", "rate_limit", "unavailable"])
def test_async_read_retries_do_not_resubmit_paid_work(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(api, "POLL_SECONDS", 0)
    monkeypatch.setattr(api, "READ_RETRY_SECONDS", 0, raising=False)
    calls = []
    failures = set()

    def handler(request):
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(202, json=response(51, status="processing"))
        if request.method == "DELETE":
            return httpx.Response(200, json={})
        phase = "results" if request.url.params.get("expand") else "poll"
        if phase not in failures:
            failures.add(phase)
            if failure == "transport":
                raise httpx.ReadError("connection interrupted")
            return httpx.Response(
                429 if failure == "rate_limit" else 503,
                headers={"Retry-After": "0"},
            )
        body = response(51)
        if phase == "poll":
            body["pages"] = []
        else:
            # A result page need not repeat the settled job meters.
            body.pop("charge_usd")
        return httpx.Response(200, json=body)

    pages, accounting = run(pdf(tmp_path, 51), handler)
    assert len(pages) == 51
    assert calls == ["POST", "GET", "GET", "GET", "GET", "DELETE"]
    assert accounting["cost"] == 0.00397


@pytest.mark.parametrize("retry_after,expected_reads", [("7", 2), ("240", 1)])
def test_read_retry_after_respects_provider_wait(
    monkeypatch, retry_after, expected_reads
):
    waits = []
    reads = []

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(api.asyncio, "sleep", sleep)

    def handler(request):
        reads.append(request.method)
        if len(reads) == 1:
            return httpx.Response(429, headers={"Retry-After": retry_after})
        return httpx.Response(200, json=response())

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await api._request(client, "GET", f"{api.BASE_URL}/parse/{JOB}")

    if expected_reads == 1:
        with pytest.raises(HostedEngineError):
            asyncio.run(execute())
        assert waits == []
    else:
        assert asyncio.run(execute())["status"] == "completed"
        assert waits == [7]
    assert len(reads) == expected_reads


def test_cancel_during_read_retry_stops_job_and_settles(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "POLL_SECONDS", 0)
    monkeypatch.setattr(api, "READ_RETRY_SECONDS", 0)
    cancelled = False
    calls = []

    def handler(request):
        nonlocal cancelled
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(202, json=response(51, status="processing"))
        if not cancelled:
            cancelled = True
            return httpx.Response(503)
        return httpx.Response(200, json=response(51, status="partial"))

    with pytest.raises(HostedEngineError) as error:
        run(pdf(tmp_path, 51), handler, should_cancel=lambda: cancelled)
    assert error.value.code == "cancelled"
    assert error.value.provider_job_accepted
    assert error.value.accounting["cost"] == 0.00397
    assert calls == ["POST", "GET", "DELETE", "GET"]


def test_acceptance_callback_failure_carries_accounting(png):
    def fail(accounting):
        raise RuntimeError("database unavailable")

    with pytest.raises(HostedEngineError) as error:
        run(png, lambda r: httpx.Response(200, json=response()), on_accounting=fail)
    assert error.value.code == "accounting"
    assert error.value.provider_job_accepted
    assert error.value.accounting["model_calls"][0]["request_id"] == JOB


def test_native_pdf_live_response_replay(tmp_path):
    captured = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/opendocrouter/native_pdf.json"
        ).read_text()
    )
    pages, accounting = run(
        pdf(tmp_path, 2), lambda request: httpx.Response(200, json=captured)
    )
    assert len(pages) == 2
    assert all("INV-1042" in page["markdown"] for page in pages)
    assert accounting["cost"] == captured["charge_usd"]


@pytest.mark.parametrize(
    "error_type,ambiguous", [(httpx.ReadTimeout, True), (httpx.ConnectError, False)]
)
def test_transport_uncertainty_is_recorded_without_resubmitting(
    png, error_type, ambiguous
):
    calls, facts = [], []

    def handler(request):
        calls.append(request)
        raise error_type("private response", request=request)

    with pytest.raises(HostedEngineError) as caught:
        run(png, handler, on_accounting=lambda fact: facts.append(copy.deepcopy(fact)))
    assert caught.value.post_egress_ambiguous is ambiguous
    assert len(calls) == 1
    assert bool(facts) is ambiguous
    if ambiguous:
        assert facts[0]["cost"] is None
        assert facts[0]["model_calls"][0]["request_id"] is None
    assert "private response" not in str(caught.value)


@pytest.mark.parametrize(
    "invalid",
    [
        {"upload_id": "bad"},
        {"upload_url": "https://example.test:bad/"},
        {"max_bytes": True},
    ],
)
def test_malformed_upload_metadata_refuses_before_put(png, monkeypatch, invalid):
    monkeypatch.setattr(api, "INLINE_BYTES", 1)
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(
            200,
            json={
                "upload_id": JOB,
                "upload_url": "https://example.test/file",
                "max_bytes": api.MAX_BYTES,
                **invalid,
            },
        )

    with pytest.raises(HostedEngineError):
        run(png, handler)
    assert calls == ["POST"]


def test_async_uses_model_specific_page_limit(tmp_path, monkeypatch):
    from dataclasses import replace
    from frisket.opendocrouter_catalog import current_catalog, DocumentCatalog

    catalog = current_catalog()
    monkeypatch.setattr(
        "frisket.opendocrouter_catalog._catalog",
        DocumentCatalog(
            catalog.price_version,
            tuple(
                replace(m, max_sync_pages=1) if m.engine == ENGINE else m
                for m in catalog.models
            ),
        ),
    )
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == "POST":
            assert json.loads(request.content)["mode"] == "async"
        return httpx.Response(200, json=response(2))

    pages, _ = run(pdf(tmp_path, 2), handler)
    assert len(pages) == 2
    assert calls == ["POST", "GET", "DELETE"]
