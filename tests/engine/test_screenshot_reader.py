from __future__ import annotations

import asyncio
import threading
from io import BytesIO

import pytest
from pydantic import BaseModel
from PIL import Image

from frisket.actions.core import map_rows
from frisket.actions.screenshot import UrlColumn
from frisket.actions.screenshot_types import Screenshotter
from frisket.actions.types import ActionParams, Row, RowResult, StagedImage
from frisket.engine.executor.row_file_stage import RowFileStager
from frisket.engine.executor.screenshot_read import AdmittedScreenshotter
from frisket.ops.capture import url as capture_url


def _png_bytes():
    stream = BytesIO()
    Image.new("RGB", (2, 2), "navy").save(stream, format="PNG")
    return stream.getvalue()


PNG = _png_bytes()


class AlternateParams(ActionParams):
    website: UrlColumn
    width: int = 999


class AlternateOutput(BaseModel):
    page: StagedImage


async def alternate(
    params: AlternateParams,
    row: Row,
    browser: Screenshotter,
) -> RowResult[AlternateOutput]:
    return RowResult(
        output=AlternateOutput(
            page=await browser.capture(
                params.website.read(row) + "?derived=1",
                full_page=False,
                viewport=(params.width, 444),
                max_bytes=5000,
                timeout_ms=1234,
            )
        )
    )


def rendered(url, **kwargs):
    return capture_url.BrowserUrlRenderResult(
        requested_url=url,
        final_url=url + "&final=1",
        status_code=200,
        html="<title>Screenshot</title>",
        screenshot=PNG,
    )


@pytest.mark.asyncio
async def test_reused_capability_records_actual_derived_arguments_and_owned_source(
    monkeypatch,
):
    monkeypatch.setattr(capture_url, "url_is_safe", lambda url: True)
    calls = []

    def browser(url, **kwargs):
        calls.append((url, kwargs))
        return rendered(url)

    stager = RowFileStager(None)
    owner = AdmittedScreenshotter(stager, browser_renderer=browser)
    row = Row({"url": "https://example.test/page"})
    sources = {
        "url": {
            "column_id": 3,
            "value": row.values["url"],
            "value_ref": {
                "kind": "edit",
                "op_id": 1,
                "row_id": 2,
                "column_id": 3,
            },
        }
    }
    binding = owner.bind_row(row, sheet_id=1, row_id=2, sources=sources)
    sources["url"]["value"] = "changed after admission"
    try:
        assert map_rows(alternate).capabilities == (Screenshotter,)
        result = await alternate(AlternateParams(website="url"), row, binding)
        bound = stager.bind_row(2)
        value = bound.dump_field(StagedImage, result.output.page, "renamed")
        descriptor = bound.descriptors("renamed")[0]
        assert value["mime"] == "image/png"
        assert descriptor["output_key"] == "renamed"
        assert descriptor["facts"]["url"] == "https://example.test/page?derived=1"
        assert descriptor["facts"]["sources"]["url"]["value"] == row.values["url"]
        assert descriptor["facts"]["viewport"] == {"width": 999, "height": 444}
        assert descriptor["facts"]["network_metadata"]["screenshot_byte_count"] == len(
            PNG
        )
        assert calls == [
            (
                "https://example.test/page?derived=1",
                {
                    "max_bytes": 5000,
                    "timeout_ms": 1234,
                    "full_page": False,
                    "capture_screenshot": True,
                    "viewport_width": 999,
                    "viewport_height": 444,
                },
            )
        ]
        assert owner.calls_by_row[2][0]["status"] == "returned"
        assert owner.calls_by_row[2][0]["url"] == "https://example.test/page?derived=1"
        await owner.aclose()
        with pytest.raises(RuntimeError, match="closed"):
            await binding.capture("https://example.test/new")
        assert len(calls) == 1
    finally:
        await owner.aclose()
        stager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options",
    [
        {"viewport": (True, 720)},
        {"viewport": (4097, 720)},
        {"max_bytes": 50_000_001},
        {"timeout_ms": 300_001},
        {"full_page": "yes"},
    ],
)
async def test_alternate_call_cannot_bypass_capture_bounds(options):
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid actual arguments must not reach browser")

    stager = RowFileStager(None)
    owner = AdmittedScreenshotter(stager, browser_renderer=forbidden)
    binding = owner.bind_row(Row({}), sheet_id=1, row_id=2, sources={})
    try:
        with pytest.raises(Exception, match="Invalid screenshot capture options"):
            await binding.capture("https://example.test", **options)
        assert stager._files == {}
        assert owner.calls_by_row == {}
    finally:
        await owner.aclose()
        stager.close()


@pytest.mark.asyncio
async def test_cancel_waits_for_browser_before_staging_or_closing(monkeypatch):
    monkeypatch.setattr(capture_url, "url_is_safe", lambda url: True)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def browser(url, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        finished.set()
        return rendered(url)

    stager = RowFileStager(None)
    owner = AdmittedScreenshotter(stager, browser_renderer=browser)
    binding = owner.bind_row(Row({}), sheet_id=1, row_id=2, sources={})
    task = asyncio.create_task(binding.capture("https://example.test/page"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert stager._files == {}
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert stager._files == {}
        assert owner.calls_by_row[2][0]["status"] == "returned"
    finally:
        release.set()
        await owner.aclose()
        stager.close()


@pytest.mark.asyncio
async def test_failed_browser_dispatch_is_recorded_without_an_image(monkeypatch):
    monkeypatch.setattr(capture_url, "url_is_safe", lambda url: True)

    def browser(*args, **kwargs):
        raise RuntimeError("renderer unavailable")

    stager = RowFileStager(None)
    owner = AdmittedScreenshotter(stager, browser_renderer=browser)
    binding = owner.bind_row(Row({}), sheet_id=1, row_id=2, sources={})
    try:
        with pytest.raises(Exception, match="renderer unavailable"):
            await binding.capture("https://example.test/page")
        assert stager._files == {}
        call = owner.calls_by_row[2][0]
        assert call["status"] == "failed"
        assert call["provider"] == "browser"
        assert call["external_api"] is True
    finally:
        await owner.aclose()
        stager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe_at", ["request", "redirect"])
async def test_url_safety_refuses_before_any_image_is_staged(monkeypatch, unsafe_at):
    safe = "https://example.test/page"
    unsafe = "http://127.0.0.1/private"
    monkeypatch.setattr(capture_url, "url_is_safe", lambda url: url == safe)

    def browser(url, **kwargs):
        assert url == safe
        result = rendered(url)
        result.final_url = unsafe
        return result

    stager = RowFileStager(None)
    owner = AdmittedScreenshotter(stager, browser_renderer=browser)
    binding = owner.bind_row(Row({}), sheet_id=1, row_id=2, sources={})
    try:
        with pytest.raises(Exception, match="blocked"):
            await binding.capture(unsafe if unsafe_at == "request" else safe)
        assert stager._files == {}
        assert len(owner.calls_by_row.get(2, [])) == (unsafe_at == "redirect")
    finally:
        await owner.aclose()
        stager.close()
