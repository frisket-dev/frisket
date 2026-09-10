from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from frisket.actions.system import typed_action_for_request, validate_root_action
from frisket.ops.capture import url as capture_url
from frisket.ops.capture.url import BrowserUrlRenderResult, BrowserUrlResource
from frisket.contracts.action import Receipt
from frisket.engine.store.evidence import list_cell_evidence
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.executor.page_capture_action import prepare_page_capture_action
from frisket.engine.store import Project
from action_test_helpers import counts as _counts


PROJECT_ID = "project-url-capture-browser-warc"
STORY_URL = "https://example.test/browser-start"
FINAL_URL = "https://example.test/rendered"
CANONICAL_URL = "https://example.test/canonical"
RENDERED_HTML = """<!doctype html>
<html>
  <head>
    <title>Rendered Contract Story</title>
    <link rel="canonical" href="https://example.test/canonical">
    <meta name="author" content="Browser Reporter">
    <meta property="article:published_time" content="2026-06-22T12:00:00Z">
  </head>
  <body>
    <article>
      <h1>Rendered Contract Story</h1>
      <p id="after-js">Playwright saw the rendered investigative paragraph.</p>
    </article>
    <script>window.sessionToken = "SECRET_FROM_PAGE";</script>
  </body>
</html>
"""
SCREENSHOT_BYTES = b"\x89PNG\r\nfake screenshot bytes"


def _capture_action(
    *,
    sheet_id: int,
    row_ids: list[int],
    render_mode: str = "playwright",
    include_warc: bool = True,
    idempotency_key: str = "web_capture_page@sha256:browser-warc",
) -> dict[str, Any]:
    return {
        "action_id": "web.capture_page",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": row_ids},
        "output_names": {"page": "page"},
        "params": {
            "source": "url",
            "render_mode": render_mode,
            "include_warc": include_warc,
            "max_bytes": 25_000,
            "timeout_ms": 2345,
        },
        "idempotency_key": idempotency_key,
    }


def _seed_project(tmp_path: Path) -> tuple[Project, int, list[int], int]:
    project = Project.create(
        tmp_path / "url-capture-browser.frisket",
        name="URL Browser Capture",
    )
    sheet_id = project.add_sheet("Links")
    url_column_id = project.add_column(sheet_id, "url", type="link")
    label_column_id = project.add_column(sheet_id, "label", type="text")
    row_ids = project.add_rows(
        sheet_id,
        [{"label": "Story", "url": STORY_URL}],
        {"url": url_column_id, "label": label_column_id},
    )
    return project, sheet_id, row_ids, url_column_id


def _columns(project: Project, sheet_id: int) -> dict[str, Any]:
    return {
        str(column["name"]): column
        for column in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=?",
            (sheet_id,),
        ).fetchall()
    }


def _receipt(project: Project, receipt_id: str) -> Receipt:
    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?",
        (receipt_id,),
    ).fetchone()
    assert row is not None
    return Receipt.model_validate(json.loads(row["body"]))


def _blob_ref(receipt: Receipt, role: str) -> dict[str, Any]:
    for evidence in receipt.evidence:
        ref = evidence.ref
        if (
            ref.get("kind") == "web_capture_page_supplemental_artifact"
            and ref.get("role") == role
        ):
            return ref
    raise AssertionError(f"missing capture blob ref for role {role}")


def test_render_playwright_url_sets_viewport_before_navigation(
    monkeypatch: Any,
) -> None:
    events: list[tuple[str, Any]] = []
    page = MagicMock()
    page.url = FINAL_URL
    page.content.return_value = "<title>Viewport page</title>"
    page.goto.side_effect = lambda url, **kwargs: (
        events.append(("goto", {"url": url, **kwargs}))
        or SimpleNamespace(status=200, headers={"content-type": "text/html"})
    )
    page.screenshot.side_effect = lambda **kwargs: (
        events.append(("screenshot", kwargs)) or SCREENSHOT_BYTES
    )
    context = MagicMock()
    context.new_page.return_value = page
    browser = MagicMock()
    browser.new_context.side_effect = lambda **kwargs: (
        events.append(("new_context", kwargs)) or context
    )
    chromium = MagicMock()
    chromium.launch.return_value = browser
    session_context = MagicMock()
    session_context.__enter__.return_value = SimpleNamespace(chromium=chromium)
    sync_api = ModuleType("playwright.sync_api")
    sync_api.sync_playwright = MagicMock(return_value=session_context)
    playwright = ModuleType("playwright")
    playwright.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", playwright)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    monkeypatch.setattr(capture_url, "url_is_safe", lambda url: True)

    rendered = capture_url.render_playwright_url(
        STORY_URL,
        capture_screenshot=True,
        max_bytes=25_000,
        timeout_ms=2345,
        full_page=False,
        viewport_width=1365,
        viewport_height=777,
    )

    assert rendered.screenshot == SCREENSHOT_BYTES
    context_index = next(
        i for i, event in enumerate(events) if event[0] == "new_context"
    )
    goto_index = next(i for i, event in enumerate(events) if event[0] == "goto")
    screenshot_index = next(
        i for i, event in enumerate(events) if event[0] == "screenshot"
    )
    assert context_index < goto_index < screenshot_index
    assert events[context_index][1]["viewport"] == {"width": 1365, "height": 777}
    assert events[screenshot_index][1] == {"full_page": False, "type": "png"}


def test_media_capture_url_playwright_warc_records_artifacts_and_replays(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(capture_url, "url_is_safe", lambda url: True)
    project, sheet_id, row_ids, url_column_id = _seed_project(tmp_path)
    action = _capture_action(sheet_id=sheet_id, row_ids=row_ids)
    calls: list[tuple[str, int, int, bool, bool]] = []
    static_calls: list[str] = []

    def unexpected_static_fetch(*args: Any, **kwargs: Any) -> None:
        static_calls.append("called")
        raise AssertionError("static fetcher must not be used for browser mode")

    def fake_browser(
        url: str,
        *,
        max_bytes: int,
        timeout_ms: int,
        full_page: bool,
        capture_screenshot: bool,
    ) -> BrowserUrlRenderResult:
        calls.append((url, max_bytes, timeout_ms, full_page, capture_screenshot))
        return BrowserUrlRenderResult(
            requested_url=url,
            final_url=FINAL_URL,
            status_code=200,
            headers={
                "content-type": "text/html; charset=utf-8",
                "set-cookie": "session=SECRET",
                "x-secret-token": "SECRET",
            },
            html=RENDERED_HTML,
            elapsed_ms=31,
            resources=[
                BrowserUrlResource(
                    url="https://example.test/assets/story.css",
                    method="GET",
                    status_code=200,
                    headers={
                        "content-type": "text/css",
                        "authorization": "Bearer SECRET",
                    },
                    body=b"body { color: black; }",
                    elapsed_ms=5,
                ),
                BrowserUrlResource(
                    url="https://example.test/assets/pixel.png",
                    method="GET",
                    status_code=200,
                    headers={"content-type": "image/png"},
                    body=b"PNGDATA",
                    elapsed_ms=7,
                ),
            ],
        )

    try:
        assert validate_root_action(action).ok is True
        prepared = prepare_page_capture_action(
            project, typed_action_for_request(action)
        )
        assert prepared.required_capabilities == (
            "project:write",
            "external:url_capture",
            "external:browser_render",
        )
        before = _counts(
            project,
            (
                "columns",
                "ops",
                "receipts",
                "blobs",
                "edits",
                "source_artifacts",
                "source_spans",
                "evidence_links",
            ),
        )
        result = run_action_spec(
            project,
            action,
            project_id=PROJECT_ID,
            deps=ExecutorDeps(
                url_capture_fetcher=unexpected_static_fetch,
                url_capture_browser=fake_browser,
            ),
        )
        assert result.status == "completed", result.errors
        assert calls == [(STORY_URL, 25_000, 2345, True, False)]
        assert static_calls == []

        columns = _columns(project, sheet_id)
        assert columns["page"]["type"] == "file"
        for old_name in {
            "capture_status",
            "capture_error",
            "capture_final_url",
            "capture_canonical_url",
            "capture_title",
            "capture_author",
            "capture_published_at",
            "capture_captured_at",
            "capture_html_blob",
            "capture_markdown_blob",
            "capture_network_metadata",
            "capture_screenshot_blob",
            "capture_warc_blob",
        }:
            assert old_name not in columns
        values = project.get_values(sheet_id, int(columns["page"]["id"]))
        row_id = row_ids[0]

        html_ref = values[row_id]
        html = project.read_blob(html_ref["blob"]).decode("utf-8")
        assert "after-js" in html
        assert html_ref["kind"] == "web_page_capture"
        assert html_ref["final_url"] == FINAL_URL
        assert html_ref["canonical_url"] == CANONICAL_URL
        assert html_ref["title"] == "Rendered Contract Story"
        assert html_ref["author"] == "Browser Reporter"
        assert html_ref["published_at"] == "2026-06-22"
        assert "markdown_preview" not in html_ref

        network = html_ref["network"]
        assert network["render_mode"] == "playwright"
        assert network["include_warc"] is True
        assert network["request_count"] == 2
        assert network["resource_count"] == 2
        assert network["warc_record_count"] == 3
        assert "headers" not in network
        assert "SECRET" not in json.dumps(network)

        receipt = _receipt(project, result.receipt_id or "")
        request_ref = next(
            ref.ref
            for ref in receipt.evidence
            if ref.ref.get("kind") == "web_capture_page_request_counts"
        )
        assert request_ref["render_mode"] == "playwright"
        assert request_ref["include_warc"] is True
        assert request_ref["http_downloader"] == (
            "frisket.capture.url.render_playwright_url"
        )
        provider = receipt.provider_use[0]
        assert provider["service"] == "playwright"
        assert provider["render_mode"] == "playwright"
        provider_row = provider["rows"][0]
        assert provider_row["html_blob_hash"] == html_ref["blob"]
        assert "markdown_blob_hash" not in provider_row
        assert "screenshot_blob_hash" not in provider_row
        assert provider_row["warc_blob_hash"]

        warc_ref = _blob_ref(receipt, "warc")
        warc = project.read_blob(warc_ref["blob_hash"])
        assert warc.count(b"WARC/1.0\r\n") == 3
        assert b"WARC-Target-URI: https://example.test/rendered" in warc
        assert b"Rendered Contract Story" in warc
        assert b"PNGDATA" in warc
        assert b"set-cookie" not in warc.lower()
        assert b"authorization" not in warc.lower()
        assert b"x-secret-token" not in warc.lower()
        assert b"Bearer SECRET" not in warc

        cell_evidence = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=url_column_id,
            project_id=PROJECT_ID,
        )
        assert cell_evidence["links"][0]["artifact_count"] == 2

        after_first = _counts(
            project,
            (
                "columns",
                "ops",
                "receipts",
                "blobs",
                "edits",
                "source_artifacts",
                "source_spans",
                "evidence_links",
            ),
        )
        assert after_first["columns"] == before["columns"] + 1
        assert after_first["blobs"] == before["blobs"] + 2
        assert after_first["source_artifacts"] == before["source_artifacts"] + 2
        assert after_first["source_spans"] == before["source_spans"] + 2

        replay = run_action_spec(
            project,
            action,
            project_id=PROJECT_ID,
            deps=ExecutorDeps(
                url_capture_browser=lambda *args, **kwargs: (_ for _ in ()).throw(
                    AssertionError("browser must not run during replay")
                )
            ),
        )
        assert replay.status == "completed"
        assert calls == [(STORY_URL, 25_000, 2345, True, False)]
    finally:
        project.close()
