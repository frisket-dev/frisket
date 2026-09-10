"""Datalab hosted document APIs (datalab.to), following the hosted-engine
playbook and mirroring tests/test_translate_hosted_engines.py. Every
provider call replays a fixture through ``httpx.MockTransport``; NO live
network (tests never touch the live network by default). The convert/ocr success bodies come from a
committed recording (tests/fixtures/datalab/*.json, recorded live
2026-07-17 against the free tier, re-recorded the same day for the OCR->
/convert migration — see integrations/datalab.py's module docstring for the
full API writeup).

Also covers real cost propagation, team/org key threading,
model:complete validator consistency (3), cooperative cancellation + a
wall-clock poll deadline (4), fail-closed unknown poll statuses (5), and the
OCR->/convert migration + input-type gating (6)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from deterministic_time import controlled_time

from frisket.ops.integrations.datalab import (
    DATALAB_BASE_URL,
    DatalabEngineError,
    datalab_convert,
    datalab_ocr,
)
from frisket.actions.markdown import ToMarkdownParams
from frisket.ops.ocr_engines import DATALAB_ENGINE as OCR_DATALAB_ENGINE
from frisket.engine.runner import MapRunner
from tests.execution_composition_helpers import open_attempt_authority
from frisket.server.action_catalog_hints import _recipe_engines
from frisket.engine.store import Project

FIXTURES = Path(__file__).parent.parent / "fixtures" / "datalab"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _mock(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run_ocr(project, sheet_id, router=None, *, confirm=True):
    from frisket.engine.executor import run_action_spec

    request = {
        "action_id": "media.ocr",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"source": "page", "engine": "datalab"},
        "output_names": {"text": "ocr_text", "blocks": "ocr_text_blocks"},
        "idempotency_key": "datalab-ocr",
    }
    first = run_action_spec(project, request, project_id="datalab", router=router)
    if confirm:
        assert first.status == "needs_confirmation", first.errors
        request["confirmation"] = first.errors[0].details["promise_set_hash"]
        return run_action_spec(project, request, project_id="datalab", router=router)
    return first


def _run_markdown(project, sheet_id, router):
    from frisket.engine.executor import run_action_spec

    request = {
        "action_id": "media.to_markdown",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"source": "doc", "engine": "datalab"},
        "idempotency_key": "datalab-markdown",
    }
    first = run_action_spec(project, request, project_id="datalab", router=router)
    assert first.status == "needs_confirmation", first.errors
    request["confirmation"] = first.errors[0].details["promise_set_hash"]
    return run_action_spec(project, request, project_id="datalab", router=router)


def _project(tmp_path: Path, rows: list[dict], coltype: str) -> tuple[Project, int]:
    p = Project.create(tmp_path / "t.frisket", name="t")
    sheet = p.add_sheet("data")
    cols = {k: p.add_column(sheet, k, type=coltype) for k in rows[0]}
    if coltype == "image":
        for row in rows:
            for key, value in row.items():
                path = Path(value)
                digest = p.add_blob(
                    path.read_bytes(), filename=path.name, mime="image/png"
                )
                row[key] = {"blob": digest, "filename": path.name, "mime": "image/png"}
    p.add_rows(sheet, rows, cols)
    return p, sheet


# --- engine ids are stable strings shared by conversion and OCR recipes ---


def test_datalab_engine_id_is_consistent_across_recipes() -> None:
    assert OCR_DATALAB_ENGINE == "datalab"
    assert (
        ToMarkdownParams(source="document", engine="datalab").engine.root == "datalab"
    )


# --- client: /convert (recorded fixtures) -----------------------------------


@pytest.mark.asyncio
async def test_datalab_convert_replays_recorded_fixtures_and_extracts_cost() -> None:
    submit = _load("convert_submit.json")
    complete = _load("convert_complete.json")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.method == "POST":
            assert str(request.url) == f"{DATALAB_BASE_URL}/convert"
            assert request.headers.get("x-api-key") == "test-key"
            return httpx.Response(200, json=submit)
        assert str(request.url) == submit["request_check_url"]
        return httpx.Response(200, json=complete)

    async with _mock(handler) as http:
        body, cost_usd = await datalab_convert(
            http, "test-key", b"%PDF-1.4 fake", "doc.pdf", "application/pdf"
        )

    assert body["markdown"] == complete["markdown"]
    assert body["page_count"] == 1
    # ``cost_breakdown.final_cost_cents`` (1.0 cents)
    # extracted as real USD spend, not discarded.
    assert cost_usd == 0.01
    assert len(calls) == 2  # one submit, one poll (fixture is already terminal)


@pytest.mark.asyncio
async def test_datalab_convert_polls_through_a_pending_state() -> None:
    """The recorded live run genuinely polled 'processing' before 'complete'
    — replay that sequence so the poll loop itself, not just a single-shot
    terminal response, is covered."""
    submit = _load("convert_submit.json")
    complete = _load("convert_complete.json")
    poll_bodies = iter([{"status": "processing"}, complete])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=submit)
        return httpx.Response(200, json=next(poll_bodies))

    async with _mock(handler) as http:
        body, cost_usd = await datalab_convert(
            http, "test-key", b"data", "doc.pdf", "application/pdf", poll_interval=0.0
        )
    assert body["status"] == "complete"
    assert cost_usd == 0.01


@pytest.mark.asyncio
async def test_datalab_convert_missing_markdown_on_complete_is_an_error() -> None:
    """A ``complete`` body with no Markdown content must never
    silently become an empty string — a document Datalab couldn't actually
    extract text from (or a response shape drift) fails loudly."""
    submit = _load("convert_submit.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=submit)
        return httpx.Response(
            200, json={"status": "complete", "success": True, "markdown": ""}
        )

    async with _mock(handler) as http:
        with pytest.raises(DatalabEngineError) as exc:
            await datalab_convert(http, "k", b"x", "d.pdf", "application/pdf")
    assert exc.value.code == "bad_request"
    assert "no markdown" in exc.value.message.lower()


# --- client: OCR via /convert(output_format=json) — the migrated path ------


@pytest.mark.asyncio
async def test_datalab_ocr_submits_to_convert_not_the_deprecated_ocr_route() -> None:
    """Datalab's migration guide names ``/convert`` as the
    documented replacement for the deprecated /ocr endpoint — this client
    must submit there, not to /ocr."""
    submit = _load("ocr_submit.json")
    complete = _load("ocr_complete.json")
    posted_to: list[str] = []
    posted_data: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posted_to.append(str(request.url))
            posted_data.update(dict(request.url.params) if request.url.params else {})
            return httpx.Response(200, json=submit)
        return httpx.Response(200, json=complete)

    async with _mock(handler) as http:
        await datalab_ocr(http, "test-key", b"png-bytes", "page.png", "image/png")

    assert posted_to == [f"{DATALAB_BASE_URL}/convert"]


@pytest.mark.asyncio
async def test_datalab_ocr_replays_recorded_fixture_and_normalizes_pages() -> None:
    submit = _load("ocr_submit.json")
    complete = _load("ocr_complete.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert str(request.url) == f"{DATALAB_BASE_URL}/convert"
            return httpx.Response(200, json=submit)
        assert str(request.url) == submit["request_check_url"]
        return httpx.Response(200, json=complete)

    async with _mock(handler) as http:
        pages, cost_usd = await datalab_ocr(
            http, "test-key", b"png-bytes", "page.png", "image/png"
        )

    assert cost_usd == 0.01
    assert len(pages) == 1
    page = pages[0]
    assert page["text"] == "Hello Datalab"
    assert len(page["blocks"]) == 1
    block = page["blocks"][0]
    assert block["text"] == "Hello Datalab"
    assert 0.0 <= block["score"] <= 1.0
    # No page_size supplied: bbox stays in Datalab's raw internal render
    # space (recorded live: a Page-level bbox of [0,0,2352,784] for what was
    # actually a 300x100 source image), un-rescaled, rather than a fabricated
    # scale — matches the raw recorded leaf-block bbox exactly.
    assert block["bbox"] == [[58, 312], [571, 312], [571, 397], [58, 397]]


@pytest.mark.asyncio
async def test_datalab_ocr_rescales_bbox_to_the_source_image_pixel_space() -> None:
    """Datalab renders ``/convert`` JSON internally at its own resolution,
    verified live, a 300x100 input PNG produced a Page-level bbox of
    [0,0,2352,784] (~7.84x). Every other OCR engine (rapidocr/tesseract/
    sidecar/the old deprecated /ocr endpoint) reports bboxes in the SAME
    pixel frame as the input image — page_size makes /convert(json) match
    that convention instead of silently mis-positioning the searchable-PDF
    text layer / grounding evidence."""
    submit = _load("ocr_submit.json")
    complete = _load("ocr_complete.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=submit)
        return httpx.Response(200, json=complete)

    async with _mock(handler) as http:
        pages, _cost = await datalab_ocr(
            http,
            "test-key",
            b"png-bytes",
            "page.png",
            "image/png",
            page_size=(300, 100),
        )

    block = pages[0]["blocks"][0]
    # Matches the OLD /ocr endpoint's recorded line bbox ([8,40]-[73,50])
    # almost exactly once rescaled to the true 300x100 source frame.
    assert block["bbox"] == [[7, 40], [73, 40], [73, 51], [7, 51]]


@pytest.mark.asyncio
async def test_datalab_ocr_missing_json_tree_is_an_error() -> None:
    submit = _load("ocr_submit.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=submit)
        return httpx.Response(
            200, json={"status": "complete", "success": True, "json": None}
        )

    async with _mock(handler) as http:
        with pytest.raises(DatalabEngineError) as exc:
            await datalab_ocr(http, "k", b"x", "p.png", "image/png")
    assert exc.value.code == "bad_request"


@pytest.mark.asyncio
async def test_datalab_ocr_empty_page_blocks_is_an_error() -> None:
    submit = _load("ocr_submit.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=submit)
        return httpx.Response(
            200,
            json={"status": "complete", "success": True, "json": {"children": []}},
        )

    async with _mock(handler) as http:
        with pytest.raises(DatalabEngineError) as exc:
            await datalab_ocr(http, "k", b"x", "p.png", "image/png")
    assert exc.value.code == "bad_request"


# --- client: error taxonomy --------------------------------------------------


@pytest.mark.asyncio
async def test_datalab_auth_failure_is_typed() -> None:
    # Replays the recorded 401 body (bogus-key call, tests/fixtures/datalab).
    recorded = _load("auth_failure.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(recorded["status"], json=recorded["body"])

    async with _mock(handler) as http:
        with pytest.raises(DatalabEngineError) as exc:
            await datalab_ocr(http, "bogus", b"x", "p.png", "image/png")
    assert exc.value.code == "auth"
    assert exc.value.retryable is False
    assert "DATALAB_API_KEY" in exc.value.message


@pytest.mark.asyncio
async def test_datalab_quota_429_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={})

    async with _mock(handler) as http:
        with pytest.raises(DatalabEngineError) as exc:
            await datalab_convert(http, "k", b"x", "d.pdf", "application/pdf")
    assert exc.value.code == "quota"
    assert exc.value.retryable is True


@pytest.mark.asyncio
async def test_datalab_insufficient_credits_402_is_quota_not_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"detail": "insufficient credits"})

    async with _mock(handler) as http:
        with pytest.raises(DatalabEngineError) as exc:
            await datalab_convert(http, "k", b"x", "d.pdf", "application/pdf")
    assert exc.value.code == "quota"
    assert exc.value.retryable is False


@pytest.mark.asyncio
async def test_datalab_validation_422_is_bad_request() -> None:
    body = {
        "detail": [
            {"loc": ["body", "file"], "msg": "field required", "type": "value_error"}
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json=body)

    async with _mock(handler) as http:
        with pytest.raises(DatalabEngineError) as exc:
            await datalab_convert(http, "k", b"x", "d.pdf", "application/pdf")
    assert exc.value.code == "bad_request"
    assert "field required" in exc.value.message


@pytest.mark.asyncio
async def test_datalab_server_error_5xx_is_retryable_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={})

    async with _mock(handler) as http:
        with pytest.raises(DatalabEngineError) as exc:
            await datalab_convert(http, "k", b"x", "d.pdf", "application/pdf")
    assert exc.value.code == "http"
    assert exc.value.retryable is True


@pytest.mark.asyncio
async def test_datalab_transport_error_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async with _mock(handler) as http:
        with pytest.raises(DatalabEngineError) as exc:
            await datalab_convert(http, "k", b"x", "d.pdf", "application/pdf")
    assert exc.value.code == "transport"
    assert exc.value.retryable is True


@pytest.mark.asyncio
async def test_datalab_documented_failed_status_has_a_nice_error_message() -> None:
    submit = _load("convert_submit.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=submit)
        return httpx.Response(200, json={"status": "failed", "error": "corrupt PDF"})

    async with _mock(handler) as http:
        with pytest.raises(DatalabEngineError) as exc:
            await datalab_convert(http, "k", b"x", "d.pdf", "application/pdf")
    assert exc.value.code == "bad_request"
    assert "corrupt PDF" in exc.value.message


@pytest.mark.asyncio
async def test_datalab_unrecognized_poll_status_fails_closed_not_open() -> None:
    """A fabricated ``cancelled`` status (not
    in ANY documented set — pending, complete, or the one documented
    'failed') used to be returned as if it were a successful terminal body,
    silently becoming an empty result. It must now fail loudly instead."""
    submit = _load("convert_submit.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=submit)
        # No markdown field at all — the old fail-open behavior would have
        # returned this body, and the caller would have silently substituted
        # markdown="".
        return httpx.Response(200, json={"status": "cancelled"})

    async with _mock(handler) as http:
        with pytest.raises(DatalabEngineError) as exc:
            await datalab_convert(http, "k", b"x", "d.pdf", "application/pdf")
    assert exc.value.code == "bad_request"
    assert exc.value.retryable is False
    assert "cancelled" in exc.value.message


@pytest.mark.asyncio
async def test_datalab_success_false_at_submit_is_bad_request() -> None:
    """documentation.datalab.to/platform/billing.md describes a 200 with
    success=false for the page-concurrency-limit case — a submission-time
    rejection, not a job to poll."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"success": False, "error": "too many pages in flight"}
        )

    async with _mock(handler) as http:
        with pytest.raises(DatalabEngineError) as exc:
            await datalab_convert(http, "k", b"x", "d.pdf", "application/pdf")
    assert exc.value.code == "bad_request"
    assert "too many pages in flight" in exc.value.message


# --- Client: cooperative cancellation + wall-clock deadline ----------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "expected_capability"),
    [
        ("convert", "document.convert"),
        ("ocr", "ocr"),
    ],
)
async def test_datalab_accepted_submission_cancelled_before_poll_preserves_unknown_accounting(
    operation: str,
    expected_capability: str,
) -> None:
    """A cancellation cannot erase a job Datalab already accepted.

    The submit response proves provider egress and transfers the work to a
    separately running provider job.  Its final meter may be unavailable when
    cancellation wins before the first poll, but that uncertainty is itself
    accounting truth: one provider fact with unknown cost, never confident
    zero and never an empty receipt input.
    """
    submit = _load(
        "convert_submit.json" if operation == "convert" else "ocr_submit.json"
    )
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url)))
        if request.method != "POST":
            pytest.fail(
                "cancellation after acceptance must happen before the first poll"
            )
        return httpx.Response(200, json=submit)

    async with _mock(handler) as http:
        with pytest.raises(DatalabEngineError) as exc:
            if operation == "convert":
                await datalab_convert(
                    http,
                    "k",
                    b"%PDF-1.4 fake",
                    "document.pdf",
                    "application/pdf",
                    should_cancel=lambda: True,
                )
            else:
                await datalab_ocr(
                    http,
                    "k",
                    b"png-bytes",
                    "page.png",
                    "image/png",
                    should_cancel=lambda: True,
                )

    assert exc.value.code == "cancelled"
    assert requests == [("POST", f"{DATALAB_BASE_URL}/convert")]
    accounting = exc.value.accounting
    assert accounting is not None
    assert accounting["cost"] is None
    assert accounting["tokens_in"] is None
    assert accounting["tokens_out"] is None
    assert len(accounting["model_calls"]) == 1
    fact = accounting["model_calls"][0]
    assert fact["fact_version"] == "frisket.model-call-fact.v1"
    assert fact["capability"] == expected_capability
    assert fact["engine"] == "datalab"
    assert fact["provider"] == "datalab"
    assert fact["provider_kind"] == "platform_api"
    assert fact["credential_source"] == "none"
    assert fact["provider_reported_cost_usd"] is None
    assert fact["provider_cost_usd"] is None
    assert fact["cost_source"] == "unknown"
    assert fact["units"] == {"requests": 1}
    assert fact["request_id"] == submit["request_id"]
    assert any("accepted" in warning.lower() for warning in fact["warnings"])


@pytest.mark.asyncio
async def test_datalab_poll_stops_on_should_cancel() -> None:
    submit = _load("convert_submit.json")
    poll_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=submit)
        poll_calls["n"] += 1
        return httpx.Response(200, json={"status": "processing"})

    async with _mock(handler) as http:
        with pytest.raises(DatalabEngineError) as exc:
            await datalab_convert(
                http,
                "k",
                b"x",
                "d.pdf",
                "application/pdf",
                poll_interval=0.0,
                should_cancel=lambda: True,
            )
    assert exc.value.code == "cancelled"
    # Cancelled before ever polling — the FIRST should_cancel check runs
    # before the first GET.
    assert poll_calls["n"] == 0


@pytest.mark.asyncio
async def test_datalab_poll_cancels_mid_loop_not_just_at_the_start() -> None:
    submit = _load("convert_submit.json")
    poll_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=submit)
        poll_calls["n"] += 1
        return httpx.Response(200, json={"status": "processing"})

    def should_cancel() -> bool:
        # Cancel only once the loop has actually polled a couple of times —
        # proves the check runs EVERY iteration, not just before the first.
        return poll_calls["n"] >= 2

    async with _mock(handler) as http:
        with pytest.raises(DatalabEngineError) as exc:
            await datalab_convert(
                http,
                "k",
                b"x",
                "d.pdf",
                "application/pdf",
                poll_interval=0.0,
                should_cancel=should_cancel,
            )
    assert exc.value.code == "cancelled"
    assert poll_calls["n"] == 2


@pytest.mark.asyncio
async def test_datalab_poll_timeout_is_wall_clock_not_accumulated_sleep() -> None:
    """A slow poll GET must count against the deadline:
    the OLD accumulated-sleep math only counted the explicit sleeps, so a slow
    network round-trip could silently extend the effective timeout well past
    poll_timeout.

    Deterministic via the injected clock/sleep seams: the manual clock
    advances 0.05s inside each GET handler (the simulated slow round-trip)
    and by each requested sleep. With poll_timeout=0.15, wall-clock
    accounting reaches the deadline after exactly two polls (GET +0.05,
    sleep +0.05, GET +0.05); accumulated-sleep math would only have accrued
    0.05s and kept polling."""
    submit = _load("convert_submit.json")
    poll_calls = {"n": 0}

    with controlled_time() as t:
        sleeper = t.async_sleeper()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json=submit)
            poll_calls["n"] += 1
            t.advance_seconds(0.05)  # simulated slow network round-trip
            return httpx.Response(200, json={"status": "processing"})

        async with _mock(handler) as http:
            with pytest.raises(DatalabEngineError) as exc:
                await datalab_convert(
                    http,
                    "k",
                    b"x",
                    "d.pdf",
                    "application/pdf",
                    poll_interval=0.05,
                    poll_timeout=0.15,
                    clock=t.monotonic,
                    sleep=sleeper,
                )
    assert exc.value.code == "http"
    assert exc.value.retryable is True
    # The deadline fired after the second slow GET, not after accumulated
    # sleeps reached 0.15s — the GET's own duration counted.
    assert poll_calls["n"] == 2
    assert sleeper.sleeps == [pytest.approx(0.05)]


# --- client: submit-to-complete duration bracketing -------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "fixture_prefix"),
    [("convert", "convert"), ("ocr", "ocr")],
)
async def test_datalab_brackets_duration_ms_from_submit_to_complete(
    operation: str, fixture_prefix: str
) -> None:
    """duration_ms is the injected clock's elapsed time from submission
    (before ``_post``) to the completed poll, mutated onto the SAME fact
    ``on_accepted`` already persisted — never a fresh wall-clock read, so
    the number is exactly what the test's manual clock advanced.

    The accepted snapshot proves the honest NULL at acceptance (nothing has
    elapsed yet); the live reference proves the terminal enrichment lands on
    that same fact object.
    """
    submit = _load(f"{fixture_prefix}_submit.json")
    complete = _load(f"{fixture_prefix}_complete.json")
    accepted_snapshot: dict[str, Any] = {}
    live_accounting: dict[str, Any] = {}

    def on_accepted(accounting: dict[str, Any]) -> None:
        accepted_snapshot.update(copy.deepcopy(accounting))
        live_accounting.update(accounting)

    with controlled_time() as t:

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json=submit)
            t.advance_seconds(1.5)
            return httpx.Response(200, json=complete)

        async with _mock(handler) as http:
            kwargs = dict(clock=t.monotonic, on_accepted=on_accepted)
            if operation == "convert":
                await datalab_convert(
                    http, "k", b"%PDF-1.4 fake", "d.pdf", "application/pdf", **kwargs
                )
            else:
                await datalab_ocr(
                    http, "k", b"png-bytes", "p.png", "image/png", **kwargs
                )

    assert accepted_snapshot["model_calls"][0]["duration_ms"] is None
    assert live_accounting["model_calls"][0]["duration_ms"] == 1500


# --- recipe dispatch: engine="datalab" routes to the client -----------------


def test_datalab_ocr_dispatch_routes_to_the_client_and_records_real_cost(
    tmp_path, monkeypatch
):
    """The run and the stored per-row
    cost must reflect Datalab's real reported spend, not the silent-zero
    default (runner/map_runner.py's meta fallback)."""
    monkeypatch.setenv("DATALAB_API_KEY", "env-key")
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    complete = _load("ocr_complete.json")
    submit = _load("ocr_submit.json")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.method == "POST":
            assert request.headers.get("x-api-key") == "env-key"
            return httpx.Response(200, json=submit)
        return httpx.Response(200, json=complete)

    class StubRouter:
        client = _mock(handler)

    page = tmp_path / "page.png"
    page.write_bytes(b"\x89PNG\r\n\x1a\n page-bytes")
    p, sheet = _project(tmp_path, [{"page": str(page)}], "image")
    # Echo the typed action's exact server-authored confirmation before egress.
    prog = _run_ocr(p, sheet, StubRouter())
    assert prog.status == "completed", prog.errors
    col = next(c for c in p.columns(sheet) if c["name"] == "ocr_text")
    (text,) = p.get_values(sheet, col["id"]).values()
    assert text == "Hello Datalab"
    # Typed results expose output facts; the persisted run owns aggregate spend.
    run_row = p.db.execute(
        "SELECT cost_actual FROM runs WHERE id=?", (prog.run_id,)
    ).fetchone()
    assert run_row["cost_actual"] == pytest.approx(0.01)
    p.close()
    assert any(url.endswith("/convert") for url in calls)


def test_datalab_convert_dispatch_routes_to_the_client_and_records_real_cost(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DATALAB_API_KEY", "env-key")
    submit = _load("convert_submit.json")
    complete = _load("convert_complete.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert str(request.url) == f"{DATALAB_BASE_URL}/convert"
            assert request.headers.get("x-api-key") == "env-key"
            return httpx.Response(200, json=submit)
        return httpx.Response(200, json=complete)

    class StubRouter:
        client = _mock(handler)

    # A real local path with a supported (.pdf) extension. The engine gate uses
    # Datalab's actual supported input
    # types (PDF/Office/image), so unlike the OLD version of this test, a
    # plain inline-text/HTML cell is no longer usable here (see the
    # dedicated rejection test below).
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-1.4 fake pdf bytes")
    p, sheet = _project(tmp_path, [{"doc": str(doc)}], "file")
    prog = _run_markdown(p, sheet, StubRouter())
    assert prog.status == "completed", prog.errors
    col = next(c for c in p.columns(sheet) if c["name"] == "markdown")
    (markdown,) = p.get_values(sheet, col["id"]).values()
    assert markdown.strip() == complete["markdown"].strip()
    # Record real spend, not $0.
    run_row = p.db.execute(
        "SELECT cost_actual FROM runs WHERE id=?", (prog.run_id,)
    ).fetchone()
    assert run_row["cost_actual"] == pytest.approx(0.01)
    # The terminal fact's ``cost_source``
    # must agree with what the route binding requires for its lane, not the
    # provider-vocabulary string Datalab's completion callback
    # (_complete_submission_accounting) writes with no route in scope. This
    # drives the REAL production callback path end to end (real
    # AttemptAuthority, no stubbed on_accepted) — the shape the earlier
    # dollars-only assertions above could not catch, because a hardcoded
    # "provider_reported" still sums to the same $0.01.
    from frisket.engine.store.runs import RunResultStore

    [fact] = RunResultStore(p).model_calls(prog.run_id)
    assert fact["capability"] == "document.convert"
    assert fact["cost_source"] == "pricing_data"
    p.close()


def test_datalab_accepted_job_error_stays_reconciliation_required(
    tmp_path, monkeypatch
):
    """An accepted async job is never downgraded to a retryable row failure."""
    monkeypatch.setenv("DATALAB_API_KEY", "env-key")
    submit = _load("convert_submit.json")
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "POST":
            posts += 1
            return httpx.Response(200, json=submit)
        return httpx.Response(503, json={"detail": "poll unavailable"})

    class StubRouter:
        client = _mock(handler)

    doc = tmp_path / "accepted.pdf"
    doc.write_bytes(b"%PDF-1.4 accepted work")
    project, sheet = _project(tmp_path, [{"doc": str(doc)}], "file")
    progress = _run_markdown(project, sheet, StubRouter())

    assert progress.status == "failed", progress.errors
    assert progress.errors[0].code == "external_effect_reconciliation_required"
    assert not progress.outputs
    assert posts == 1
    checkpoint = project.db.execute(
        "SELECT state FROM effect_checkpoints WHERE family='row_effect' "
        "AND group_key=CAST(? AS TEXT)",
        (progress.run_id,),
    ).fetchone()
    assert checkpoint is not None
    assert checkpoint["state"] == "reserved"
    accepted_facts = project.db.execute(
        "SELECT id, request_id FROM model_calls WHERE run_id=?",
        (progress.run_id,),
    ).fetchall()
    assert len(accepted_facts) == 1
    assert accepted_facts[0]["id"].startswith("datalab_accepted_")
    assert accepted_facts[0]["request_id"]
    project.close()


def test_datalab_convert_rejects_unsupported_input_type_before_any_network_call(
    tmp_path, monkeypatch
):
    """The shared ``to_markdown`` source requirements
    advertise text/html columns (for markitdown/trafilatura_html), but
    Datalab's /convert only accepts PDF/Word/PowerPoint/PNG/JPG/WebP. An
    inline-HTML row must fail per-row with a clear message BEFORE any
    network call, not after a confusing remote 422."""
    monkeypatch.setenv("DATALAB_API_KEY", "env-key")
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={})

    class StubRouter:
        client = _mock(handler)

    p, sheet = _project(tmp_path, [{"doc": "<html><body>hi</body></html>"}], "text")
    prog = _run_markdown(p, sheet, StubRouter())
    assert prog.status == "failed", prog.errors
    assert called["n"] == 0  # no wasted network call for a doomed request
    err = p.db.execute(
        "SELECT error, error_code FROM results WHERE run_id=? AND error IS NOT NULL",
        (prog.run_id,),
    ).fetchone()
    assert err["error_code"] == "bad_request"
    assert "does not support" in err["error"]
    p.close()


# --- Estimate when Datalab omits cost fields -------------------------------


def test_datalab_ocr_falls_back_to_external_pricing_estimate_when_cost_missing(
    tmp_path, monkeypatch
):
    """A completed response with no cost_breakdown/total_cost (a plan
    variant, or a future response-shape drift) must not silently record $0
    — it falls back to external_pricing's per-page rate and marks the
    result as estimated, not reported."""
    monkeypatch.setenv("DATALAB_API_KEY", "env-key")
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    submit = _load("ocr_submit.json")
    complete_no_cost = dict(_load("ocr_complete.json"))
    complete_no_cost.pop("cost_breakdown", None)
    complete_no_cost.pop("total_cost", None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=submit)
        return httpx.Response(200, json=complete_no_cost)

    class StubRouter:
        client = _mock(handler)

    page = tmp_path / "page.png"
    page.write_bytes(b"\x89PNG\r\n\x1a\n page-bytes")
    p, sheet = _project(tmp_path, [{"page": str(page)}], "image")
    prog = _run_ocr(p, sheet, StubRouter())
    assert prog.status == "completed", prog.errors
    from frisket.ai.external_pricing import DATALAB_OCR_PAGE, external_unit_price_usd

    expected = external_unit_price_usd(DATALAB_OCR_PAGE)
    assert expected is not None
    assert p.db.execute(
        "SELECT cost_actual FROM runs WHERE id=?", (prog.run_id,)
    ).fetchone()[0] == pytest.approx(expected)
    row = p.db.execute(
        "SELECT value FROM results r JOIN columns c ON r.column_id=c.id "
        "WHERE r.run_id=? AND c.name='ocr_text'",
        (prog.run_id,),
    ).fetchone()
    assert row is not None  # the row still succeeded despite the unknown cost
    p.close()


@pytest.mark.asyncio
async def test_datalab_convert_falls_back_to_external_pricing_estimate_scaled_by_pages(
    tmp_path,
) -> None:
    """Same fallback for /convert, but scaled by the response's own
    page_count (one /convert call can cover a multi-page document, unlike
    OCR's per-page loop) rather than flatly pricing every document as one
    page. Exercises ``ConvertMarkdownRecipe._convert_datalab`` directly —
    the unit under test for this fallback's page-scaling — rather than a
    full MapRunner run."""
    from frisket.ai.external_pricing import (
        DATALAB_CONVERT_PAGE,
        external_unit_price_usd,
    )
    from frisket.ops.base import OpContext
    from tests.document_conversion_helpers import bound_document_converter

    submit = _load("convert_submit.json")
    complete_no_cost = dict(_load("convert_complete.json"))
    complete_no_cost.pop("cost_breakdown", None)
    complete_no_cost.pop("total_cost", None)
    complete_no_cost["page_count"] = 3

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=submit)
        return httpx.Response(200, json=complete_no_cost)

    class _StubProject:
        def secret_plaintext(self, name: str):
            return "env-key" if name == "DATALAB_API_KEY" else None

    doc_path = tmp_path / "doc.pdf"
    doc_path.write_bytes(b"%PDF-1.4 fake")
    async with _mock(handler) as http:
        ctx = OpContext(project=_StubProject(), http=http, extras={})
        markdown, meta = await bound_document_converter(
            ctx, engine="datalab"
        )._convert_datalab(doc_path)

    unit_price = external_unit_price_usd(DATALAB_CONVERT_PAGE)
    assert unit_price is not None
    assert meta["cost"] == pytest.approx(unit_price * 3)
    assert meta["cost_source"] == "estimated"
    assert markdown.strip() == complete_no_cost["markdown"].strip()


# --- Per-row error isolation -----------------------------------------------


def test_datalab_missing_key_refuses_the_run_with_the_remedy(tmp_path, monkeypatch):
    """routed OCR behavior change, deliberate: an unconfigured Datalab key is now a
    ``no_live_target`` REFUSAL of the whole OCR run, before any row is
    dispatched, carrying the remedy that names the env var (and Settings >
    Secrets).

    Before routed OCR this ran two rows that each failed identically with the ``auth``
    taxonomy code — the shape the seam exists to replace ("a confirmed run
    with a dead target blocks HERE with the refusal's actionable remedy
    instead of queueing rows that all fail identically",
    engine/runner/validation.py). The per-row DatalabEngineError boundary is
    unchanged and still covers a key that disappears mid-run and every
    unrouted caller (previews); what moved is WHEN an absent key is noticed.
    """
    monkeypatch.delenv("DATALAB_API_KEY", raising=False)

    class StubRouter:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
        )

    page1 = tmp_path / "page1.png"
    page2 = tmp_path / "page2.png"
    page1.write_bytes(b"\x89PNG\r\n\x1a\n one")
    page2.write_bytes(b"\x89PNG\r\n\x1a\n two")
    p, sheet = _project(tmp_path, [{"page": str(page1)}, {"page": str(page2)}], "image")
    result = _run_ocr(p, sheet, StubRouter(), confirm=False)
    assert result.status == "failed", result.errors
    assert "DATALAB_API_KEY" in str(result.errors)
    # Nothing was dispatched: no results rows at all, rather than two
    # identical per-row auth failures.
    assert (
        p.db.execute(
            "SELECT COUNT(*) FROM results WHERE error_code IS NOT NULL"
        ).fetchone()[0]
        == 0
    )
    p.close()


# --- estimate(): the cost-gate safety net ------------------------------------


def test_datalab_ocr_estimate_is_unknown_cost_not_free(tmp_path, monkeypatch):
    monkeypatch.setenv("DATALAB_API_KEY", "test-key")
    project = Project.create(tmp_path / "unknown.frisket")
    sheet = project.add_sheet("Documents")
    column = project.add_column(sheet, "page", type="file")
    digest = project.add_blob(
        b"%PDF-1.4 unknown-page-count", filename="unknown.pdf", mime="application/pdf"
    )
    project.add_rows(
        sheet,
        [
            {
                "page": {
                    "blob": digest,
                    "filename": "unknown.pdf",
                    "mime": "application/pdf",
                }
            }
        ],
        {"page": column},
    )
    try:
        result = _run_ocr(project, sheet, confirm=False)
        assert result.status == "needs_confirmation", result.errors
        assert result.errors[0].details["promise_set_hash"]
        assert result.errors[0].details["estimate"]["cost"] is None
        assert project.db.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 0
    finally:
        project.close()


@pytest.mark.parametrize(
    ("engine", "expected"),
    [
        ("markitdown", 0.0),
        ("trafilatura_html", 0.0),
        ("docling", 0.0),
        ("chandra", 0.0),
        ("datalab", None),
    ],
)
def test_document_estimate_uses_operator_or_provider_pricing(
    tmp_path, monkeypatch, engine, expected
):
    from frisket.ai.llm import ModelRouter
    from tests.engine.test_remote_engine_estimate_honesty import _to_markdown_estimate

    monkeypatch.setenv("DATALAB_API_KEY", "test-key")
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models.test:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "test-token")
    project, sheet = _project(
        tmp_path, [{"doc": "<html>unknown page count</html>"}], "text"
    )
    try:
        router = ModelRouter(cache=None, cache_mode="off")
        runner = MapRunner(
            project, router, authority=open_attempt_authority(project, router)
        )
        estimate = _to_markdown_estimate(runner, sheet, engine)
        assert estimate["cost"] == expected
        assert estimate["cost_source"] == (
            "unknown" if expected is None else "free_local"
        )
    finally:
        project.close()


# --- catalog: engine listed + availability flips with key ------------------


def test_datalab_ocr_engine_consults_project_keys_not_just_env(monkeypatch, tmp_path):
    monkeypatch.delenv("DATALAB_API_KEY", raising=False)

    def by_id(project):
        return {e["id"]: e for e in _recipe_engines("media.ocr", {}, project=project)}

    engines = by_id(None)
    assert engines["datalab"]["available"] is False
    assert engines["datalab"]["tier"] == "hosted"
    assert engines["datalab"]["billable"] is True
    assert "license" not in engines["datalab"]  # vendor's own hosted product
    assert "DATALAB_API_KEY" in engines["datalab"]["error"]
    assert engines["datalab"]["pricing"]["provider"] == "Datalab"

    class _ProjectWithSecret:
        path = tmp_path / "proj.frisket"

        def secret_plaintext(self, name: str):
            return "proj-key" if name == "DATALAB_API_KEY" else None

    engines = by_id(_ProjectWithSecret())
    assert engines["datalab"]["available"] is True
    assert "error" not in engines["datalab"]

    monkeypatch.setenv("DATALAB_API_KEY", "env-key")
    engines = by_id(None)
    assert engines["datalab"]["available"] is True


def test_datalab_to_markdown_engine_consults_project_keys_not_just_env(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("DATALAB_API_KEY", raising=False)

    def by_id(project):
        return {
            e["id"]: e
            for e in _recipe_engines("media.to_markdown", {}, project=project)
        }

    engines = by_id(None)
    assert engines["datalab"]["available"] is False
    assert engines["datalab"]["tier"] == "hosted"
    assert "license" not in engines["datalab"]
    assert engines["datalab"]["pricing"]["provider"] == "Datalab"

    class _ProjectWithSecret:
        path = tmp_path / "proj.frisket"

        def secret_plaintext(self, name: str):
            return "proj-key" if name == "DATALAB_API_KEY" else None

    engines = by_id(_ProjectWithSecret())
    assert engines["datalab"]["available"] is True


# --- Team/org-injected provider keys reach the catalog


def test_workspace_org_provider_keys_exposes_the_resolver_result(tmp_path):
    from frisket.server.workspace import Workspace

    ws_no_resolver = Workspace(tmp_path / "ws1")
    assert ws_no_resolver.org_provider_keys() == {}

    ws_with_resolver = Workspace(
        tmp_path / "ws2", provider_keys_resolver=lambda: {"openai": "org-key"}
    )
    assert ws_with_resolver.org_provider_keys() == {"openai": "org-key"}


def test_configured_llm_providers_consults_org_provider_keys(monkeypatch):
    """The catalog's LLM-provider availability (map.translate's 'llm' engine,
    media.ocr's openai/gemini remote tier) must see a team-edition
    org-injected key (Workspace.org_provider_keys(), the SAME resolver
    router_for() consults at run time) — not just env/project/workspace-file
    keys. Without this, a key that WORKS at run time reported 'unavailable'
    at catalog time."""
    from frisket.server.action_catalog_hints import _configured_llm_providers

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    providers = _configured_llm_providers(None, org_provider_keys=None)
    assert "openai" not in providers

    providers = _configured_llm_providers(None, org_provider_keys={"openai": "org-key"})
    assert "openai" in providers

    # Only recognized provider keys are unioned in (not arbitrary strings).
    providers = _configured_llm_providers(
        None, org_provider_keys={"not_a_real_provider": "x"}
    )
    assert "not_a_real_provider" not in providers


def test_explicit_local_endpoint_enables_generic_llm_engines(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    engines = {
        engine["id"]: engine
        for engine in _recipe_engines(
            "map.classify",
            {},
            project=None,
            has_local_model_endpoint=True,
        )
    }

    assert engines["llm"]["available"] is True


def test_recipe_engines_ocr_llm_tier_consults_org_provider_keys(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    engines = {
        e["id"]: e
        for e in _recipe_engines(
            "media.ocr", {}, project=None, org_provider_keys={"openai": "org-key"}
        )
    }
    assert engines["openai/gpt-4.1-mini"]["available"] is True


def test_recipe_engines_translate_llm_tier_consults_org_provider_keys(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    engines = {
        e["id"]: e
        for e in _recipe_engines(
            "map.translate", {}, project=None, org_provider_keys={"gemini": "org-key"}
        )
    }
    assert engines["llm"]["available"] is True


def test_classify_catalog_stays_disabled_under_the_general_cloud_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    monkeypatch.delenv("FRISKET_ENABLE_PROVIDERLESS_CLASSIFY", raising=False)

    engines = {engine["id"]: engine for engine in _recipe_engines("map.classify", {})}

    assert engines["local_semantic"]["available"] is False


def test_classify_catalog_honors_only_the_explicit_providerless_opt_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from frisket import semantic as semantic_module
    from frisket.server.action_catalog_hints import (
        project_action_catalog_payload_with_launcher_hints,
    )

    class _StubProject:
        def secret_plaintext(self, name: str):
            return None

        def provider_model_keys(self):
            return {}

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    monkeypatch.setenv("FRISKET_ENABLE_PROVIDERLESS_CLASSIFY", "1")
    monkeypatch.setenv("FRISKET_PROVIDERLESS_CLASSIFY_THREADS", "2")
    model_cache = tmp_path / "fastembed-cache"
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(model_cache))
    monkeypatch.setattr(semantic_module, "_local_models", {})

    payload = project_action_catalog_payload_with_launcher_hints(
        _StubProject(), sidecar_capabilities={}
    )
    classify = next(
        action for action in payload["actions"] if action["kind"] == "map.classify"
    )
    engines = {engine["id"]: engine for engine in classify["ui_hints"]["engines"]}

    assert engines["local_semantic"]["available"] is True
    assert engines["local_semantic"]["models"] == ["BAAI/bge-small-en-v1.5"]
    assert semantic_module._local_models == {}, "catalog discovery must stay lazy"
    assert not model_cache.exists(), "catalog discovery must not download a model"


def test_classify_catalog_reports_a_malformed_thread_bound_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    monkeypatch.setenv("FRISKET_ENABLE_PROVIDERLESS_CLASSIFY", "1")
    monkeypatch.setenv("FRISKET_PROVIDERLESS_CLASSIFY_THREADS", "0")

    engines = {engine["id"]: engine for engine in _recipe_engines("map.classify", {})}

    local = engines["local_semantic"]
    assert local["available"] is False
    assert "FRISKET_PROVIDERLESS_CLASSIFY_THREADS" in local["error"]


def test_project_action_catalog_payload_threads_org_provider_keys(monkeypatch):
    """End-to-end through the payload builder (server/routes/actions.py's
    call shape) — not just the inner _recipe_engines helper."""
    from frisket.server.action_catalog_hints import (
        project_action_catalog_payload_with_launcher_hints,
    )

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    class _StubProject:
        def secret_plaintext(self, name: str):
            return None

        def provider_model_keys(self):
            return {}

    payload = project_action_catalog_payload_with_launcher_hints(
        _StubProject(),
        sidecar_capabilities={},
        org_provider_keys={"openai": "org-key"},
    )
    ocr_entry = next(a for a in payload["actions"] if a["kind"] == "media.ocr")
    engines = {e["id"]: e for e in ocr_entry["ui_hints"]["engines"]}
    assert engines["openai/gpt-4.1-mini"]["available"] is True
