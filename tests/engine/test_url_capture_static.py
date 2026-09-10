from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from blob_store_helpers import local_blob_path
from frisket.actions.page_capture import CapturePageParams
from frisket.actions.system import (
    root_action_catalog,
    typed_action_for_request,
    validate_root_action,
)
from frisket.ops.capture import url as capture_url
from frisket.contracts.action import Receipt
from frisket.engine.store.evidence import list_cell_evidence
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.executor.page_capture_action import prepare_page_capture_action
from frisket.engine.store import Project
from action_test_helpers import counts as _counts
from helpers import replace_test_source_cell


PROJECT_ID = "project-url-capture-static"
STORY_URL = "https://example.test/start"
FINAL_URL = "https://example.test/story?via=redirect"
CANONICAL_URL = "https://example.test/story"
HTML_BYTES = b"""<!doctype html>
<html>
  <head>
    <title>County Contract Memo</title>
    <link rel="canonical" href="https://example.test/story">
    <meta name="author" content="Jane Reporter">
    <meta property="article:published_time" content="2026-06-20">
  </head>
  <body>
    <article>
      <h1>County Contract Memo</h1>
      <p>County officials approved the river cleanup contract.</p>
    </article>
    <script>window.secret = "not content";</script>
  </body>
</html>
"""


def _capture_action(
    *,
    sheet_id: int,
    row_ids: list[int] | None = None,
    render_mode: str = "static",
    include_warc: bool = False,
    output_name: str = "page",
    output_mode: str = "page",
    links_sheet_name: str = "Links",
    idempotency_key: str = "web_capture_page@sha256:first",
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "source": "url",
        "output_mode": output_mode,
        "render_mode": render_mode,
        "include_warc": include_warc,
        "max_bytes": 4096,
        "timeout_ms": 1234,
    }
    scope: dict[str, Any] = {"kind": "sheet_rows", "sheet_id": sheet_id}
    if row_ids is not None:
        scope["row_ids"] = row_ids
    action = {
        "action_id": "web.capture_page",
        "scope": scope,
        "params": params,
        "idempotency_key": idempotency_key,
    }
    if output_mode == "links":
        action["sheet_name"] = links_sheet_name
    else:
        action["output_names"] = {"page": output_name}
    return action


def _seed_project(tmp_path: Path) -> tuple[Project, int, list[int], int]:
    project = Project.create(tmp_path / "url-capture.frisket", name="URL Capture")
    sheet_id = project.add_sheet("Links")
    url_column_id = project.add_column(sheet_id, "url", type="link")
    title_column_id = project.add_column(sheet_id, "label", type="text")
    row_ids = project.add_rows(
        sheet_id,
        [
            {"label": "Story", "url": STORY_URL},
            {"label": "Bad", "url": "ftp://example.test/not-http"},
        ],
        {"url": url_column_id, "label": title_column_id},
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
        "SELECT body FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    assert row is not None
    return Receipt.model_validate(json.loads(row["body"]))


def _staged_blob_files(project: Project) -> list[Path]:
    stage_dir = Path(project.path) / "tmp" / "blob-staging"
    if not stage_dir.exists():
        return []
    return [path for path in stage_dir.iterdir() if path.is_file()]


def test_extract_html_links_uses_first_valid_base_for_every_anchor() -> None:
    links = capture_url.extract_html_links(
        """
        <a href="before">Before base</a>
        <base href="/first/">
        <a href="after">After first base</a>
        <base href="/second/">
        <a href="last">After second base</a>
        """,
        base_url="https://example.test/original/page",
    )

    assert links == [
        {"url": "https://example.test/first/before", "anchor_text": "Before base"},
        {
            "url": "https://example.test/first/after",
            "anchor_text": "After first base",
        },
        {
            "url": "https://example.test/first/last",
            "anchor_text": "After second base",
        },
    ]


def test_media_capture_url_static_contract_and_replay(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(capture_url, "url_is_safe", lambda url: True)
    catalog = root_action_catalog()
    entry = next(item for item in catalog.actions if item.kind == "web.capture_page")
    assert entry.execution_mode == "per_row"
    assert entry.async_mode == "sync"
    assert entry.writes_project is True
    assert entry.required_capabilities == ["project:write", "external:url_capture"]
    assert {error.code for error in entry.errors} >= {
        "invalid_input_ref",
        "invalid_params",
        "network_disabled",
        "stale_input",
        "project_write_failed",
        "url_capture_failed",
        "stale_replay",
        "idempotency_conflict",
        "idempotency_in_progress",
    }

    project, sheet_id, row_ids, url_column_id = _seed_project(tmp_path)
    action = _capture_action(sheet_id=sheet_id, row_ids=row_ids)
    params = CapturePageParams.model_validate(action["params"])
    assert params.source.name == "url"
    assert action["output_names"] == {"page": "page"}
    assert params.render_mode == "static"
    assert params.output_mode == "page"

    validation = validate_root_action(action)
    assert validation.ok is True
    prepared = prepare_page_capture_action(project, typed_action_for_request(action))
    assert prepared.required_capabilities == ("project:write", "external:url_capture")
    authored_grant = validate_root_action(action | {"capabilities": ["project:write"]})
    assert authored_grant.ok is False
    assert authored_grant.error is not None
    assert "capabilities" in authored_grant.error.message

    from frisket.ops.capture.url import StaticUrlFetchResult

    calls: list[tuple[str, int, int]] = []

    def fake_fetch(
        url: str,
        *,
        max_bytes: int,
        timeout_ms: int,
    ) -> StaticUrlFetchResult:
        calls.append((url, max_bytes, timeout_ms))
        return StaticUrlFetchResult(
            requested_url=url,
            final_url=FINAL_URL,
            status_code=200,
            headers={
                "content-type": "text/html; charset=utf-8",
                "set-cookie": "session=SECRET",
                "x-debug": "debug-header",
            },
            body=HTML_BYTES,
            elapsed_ms=17,
            redirects=[
                {
                    "status_code": 302,
                    "url_hash": "sha256:redirect-source",
                    "location_hash": "sha256:redirect-target",
                }
            ],
        )

    try:
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
            deps=ExecutorDeps(url_capture_fetcher=fake_fetch),
        )
        assert result.status == "partial", result.errors
        assert result.action.kind == "web.capture_page"
        assert result.receipt_id is not None
        assert calls == [(STORY_URL, 4096, 1234)]

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
        }:
            assert old_name not in columns

        values = project.get_values(sheet_id, int(columns["page"]["id"]))
        good_row, bad_row = row_ids
        html_ref = values[good_row]
        assert values.get(bad_row) is None
        assert html_ref["mime"] == "text/html"
        assert html_ref["filename"] == "page-row-1.html"
        assert html_ref["kind"] == "web_page_capture"
        assert html_ref["final_url"] == FINAL_URL
        assert html_ref["canonical_url"] == CANONICAL_URL
        assert html_ref["title"] == "County Contract Memo"
        assert html_ref["author"] == "Jane Reporter"
        assert html_ref["published_at"] == "2026-06-20"
        assert html_ref["captured_at"].endswith("Z")
        assert project.read_blob(html_ref["blob"]) == HTML_BYTES
        assert "markdown_preview" not in html_ref

        network = html_ref["network"]
        assert network["method"] == "GET"
        assert network["render_mode"] == "static"
        assert network["status_code"] == 200
        assert network["byte_count"] == len(HTML_BYTES)
        assert network["content_type"] == "text/html"
        assert network["redirects"] == [
            {
                "status_code": 302,
                "url_hash": "sha256:redirect-source",
                "location_hash": "sha256:redirect-target",
            }
        ]
        assert "headers" not in network

        receipt = _receipt(project, result.receipt_id)
        assert receipt.status == "partial"
        assert receipt.provider_use
        provider = receipt.provider_use[0]
        assert provider["provider"] == "url_capture"
        assert provider["method"] == "GET"
        assert provider["render_mode"] == "static"
        assert provider["request_count"] == 1
        provider_row = provider["rows"][0]
        assert provider_row["host"] == "example.test"
        assert provider_row["url_hash"].startswith("sha256:")
        assert provider_row["final_url_hash"].startswith("sha256:")
        assert provider_row["status_code"] == 200
        assert provider_row["byte_count"] == len(HTML_BYTES)
        assert provider_row["html_blob_hash"] == html_ref["blob"]
        assert "markdown_blob_hash" not in provider_row
        receipt_json = json.dumps(receipt.model_dump(mode="json"), sort_keys=True)
        assert "<html" not in receipt_json
        assert "river cleanup contract" not in receipt_json
        assert "SECRET" not in receipt_json
        assert "set-cookie" not in receipt_json.lower()

        cell_evidence = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=good_row,
            column_id=url_column_id,
            project_id=PROJECT_ID,
        )
        assert cell_evidence["links"]
        assert cell_evidence["links"][0]["artifact_count"] == 1

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
        assert after_first["receipts"] == before["receipts"] + 1
        assert after_first["blobs"] == before["blobs"] + 1
        assert after_first["source_artifacts"] == before["source_artifacts"] + 1
        assert after_first["source_spans"] == before["source_spans"] + 1
        assert after_first["evidence_links"] == before["evidence_links"] + 1
        assert _staged_blob_files(project) == []

        calls.clear()
        replay = run_action_spec(
            project,
            action,
            project_id=PROJECT_ID,
            deps=ExecutorDeps(url_capture_fetcher=fake_fetch),
        )
        assert replay.status == "partial"
        assert replay.receipt_id == result.receipt_id
        assert calls == []
        assert (
            _counts(
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
            == after_first
        )

        conflict = run_action_spec(
            project,
            _capture_action(
                sheet_id=sheet_id,
                row_ids=row_ids,
                idempotency_key="web_capture_page@sha256:first",
                output_name="other_page",
            ),
            project_id=PROJECT_ID,
        )
        assert conflict.status == "failed"
        assert conflict.errors[0].code == "idempotency_conflict"

        local_blob_path(project, html_ref["blob"]).unlink()
        stale = run_action_spec(project, action, project_id=PROJECT_ID)
        assert stale.status == "failed"
        assert stale.errors[0].code == "stale_replay"
    finally:
        project.close()


def test_capture_page_links_mode_materializes_absolute_link_rows_with_lineage(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(capture_url, "url_is_safe", lambda url: True)
    project, sheet_id, row_ids, _url_column_id = _seed_project(tmp_path)
    action = _capture_action(
        sheet_id=sheet_id,
        row_ids=[row_ids[0]],
        output_mode="links",
        links_sheet_name="Harvested Links",
        idempotency_key="web_capture_page@sha256:links",
    )

    from frisket.ops.capture.url import StaticUrlFetchResult

    html = b"""<html><head><base href="/reports/"></head><body>
      <a href="contract.pdf#page=1"> Contract <strong>PDF</strong> </a>
      <a href="contract.pdf#page=2">duplicate fragment</a>
      <a href="../agenda">Agenda 2026</a>
      <a href="mailto:tips@example.test">Email tips</a>
      <a onclick="loadMore()">Load more</a>
      <a href="https://records.example.org/index">External records</a>
    </body></html>"""

    def fake_fetch(url: str, **_kwargs: Any) -> StaticUrlFetchResult:
        return StaticUrlFetchResult(
            requested_url=url,
            final_url=FINAL_URL,
            status_code=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=html,
        )

    try:
        result = run_action_spec(
            project,
            action,
            project_id=PROJECT_ID,
            deps=ExecutorDeps(url_capture_fetcher=fake_fetch),
        )
        assert result.status == "completed", result.errors
        assert not project.db.execute(
            "SELECT 1 FROM columns WHERE sheet_id=? AND name='page' AND hidden=0",
            (sheet_id,),
        ).fetchone()
        sheet = project.db.execute(
            "SELECT id, parent_sheet_id FROM sheets WHERE name='Harvested Links' AND hidden=0"
        ).fetchone()
        assert sheet is not None
        assert int(sheet["parent_sheet_id"]) == sheet_id
        child_sheet_id = int(sheet["id"])
        columns = _columns(project, child_sheet_id)
        assert {name: column["type"] for name, column in columns.items()} == {
            "url": "link",
            "anchor_text": "text",
            "source_url": "link",
        }
        child_rows = project.db.execute(
            "SELECT id, parent_row_id FROM rows WHERE sheet_id=? ORDER BY position",
            (child_sheet_id,),
        ).fetchall()
        assert [int(row["parent_row_id"]) for row in child_rows] == [row_ids[0]] * 3
        child_row_ids = [int(row["id"]) for row in child_rows]
        urls = project.get_values(
            child_sheet_id, int(columns["url"]["id"]), row_ids=child_row_ids
        )
        labels = project.get_values(
            child_sheet_id,
            int(columns["anchor_text"]["id"]),
            row_ids=child_row_ids,
        )
        sources = project.get_values(
            child_sheet_id,
            int(columns["source_url"]["id"]),
            row_ids=child_row_ids,
        )
        assert list(urls.values()) == [
            "https://example.test/reports/contract.pdf",
            "https://example.test/agenda",
            "https://records.example.org/index",
        ]
        assert list(labels.values()) == [
            "Contract PDF",
            "Agenda 2026",
            "External records",
        ]
        assert set(sources.values()) == {FINAL_URL}
        assert project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 0

        replay = run_action_spec(
            project,
            action,
            project_id=PROJECT_ID,
            deps=ExecutorDeps(url_capture_fetcher=fake_fetch),
        )
        assert replay.status == "completed"
        assert replay.receipt_id == result.receipt_id
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM sheets WHERE name='Harvested Links'"
            ).fetchone()[0]
            == 1
        )

        first_child_row_id = child_row_ids[0]
        url_column_id = int(columns["url"]["id"])
        stored_url = project.db.execute(
            "SELECT value FROM cells WHERE row_id=? AND column_id=?",
            (first_child_row_id, url_column_id),
        ).fetchone()["value"]
        replace_test_source_cell(
            project,
            row_id=first_child_row_id,
            column_id=url_column_id,
            value="https://changed.example.test/",
        )
        changed_value = run_action_spec(project, action, project_id=PROJECT_ID)
        assert changed_value.status == "failed"
        assert changed_value.errors[0].code == "stale_replay"
        replace_test_source_cell(
            project,
            row_id=first_child_row_id,
            column_id=url_column_id,
            value=json.loads(stored_url),
        )
        assert run_action_spec(project, action, project_id=PROJECT_ID).status == (
            "completed"
        )

        project.db.execute(
            "UPDATE rows SET parent_row_id=? WHERE id=?",
            (row_ids[1], first_child_row_id),
        )
        project.db.commit()
        changed_parent = run_action_spec(project, action, project_id=PROJECT_ID)
        assert changed_parent.status == "failed"
        assert changed_parent.errors[0].code == "stale_replay"
        project.db.execute(
            "UPDATE rows SET parent_row_id=? WHERE id=?",
            (row_ids[0], first_child_row_id),
        )
        project.db.commit()
        assert run_action_spec(project, action, project_id=PROJECT_ID).status == (
            "completed"
        )

        project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (first_child_row_id,))
        project.db.commit()
        deleted_row = run_action_spec(project, action, project_id=PROJECT_ID)
        assert deleted_row.status == "failed"
        assert deleted_row.errors[0].code == "stale_replay"
        project.db.execute("UPDATE rows SET hidden=0 WHERE id=?", (first_child_row_id,))
        project.db.commit()

        project.db.execute(
            "UPDATE sheets SET name='Renamed Links' WHERE id=?", (child_sheet_id,)
        )
        project.db.commit()
        stale = run_action_spec(project, action, project_id=PROJECT_ID)
        assert stale.status == "failed"
        assert stale.errors[0].code == "stale_replay"
    finally:
        project.close()


def test_capture_page_playwright_links_does_not_capture_a_screenshot(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(capture_url, "url_is_safe", lambda url: True)
    project, sheet_id, row_ids, _url_column_id = _seed_project(tmp_path)
    action = _capture_action(
        sheet_id=sheet_id,
        row_ids=[row_ids[0]],
        output_mode="links",
        links_sheet_name="Rendered Links",
        render_mode="playwright",
        idempotency_key="web_capture_page@sha256:rendered-links",
    )
    calls: list[bool] = []

    from frisket.ops.capture.url import BrowserUrlRenderResult

    def fake_browser(
        url: str,
        *,
        max_bytes: int,
        timeout_ms: int,
        full_page: bool,
        capture_screenshot: bool,
    ) -> BrowserUrlRenderResult:
        del max_bytes, timeout_ms, full_page
        calls.append(capture_screenshot)
        return BrowserUrlRenderResult(
            requested_url=url,
            final_url=FINAL_URL,
            status_code=200,
            headers={"content-type": "text/html"},
            html='<a href="/records">Records</a>',
        )

    try:
        result = run_action_spec(
            project,
            action,
            project_id=PROJECT_ID,
            deps=ExecutorDeps(url_capture_browser=fake_browser),
        )

        assert result.status == "completed", result.errors
        assert calls == [False]
        assert project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 0
    finally:
        project.close()


def test_capture_page_links_mode_rejects_warc_output() -> None:
    action = _capture_action(
        sheet_id=1,
        output_mode="links",
        include_warc=True,
    )

    with pytest.raises(
        ValidationError, match="WARC requires page output with browser rendering"
    ):
        CapturePageParams.model_validate(action["params"])


def test_capture_page_rejects_static_warc_and_network_off_before_dispatch(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(capture_url, "url_is_safe", lambda url: True)
    project, sheet_id, row_ids, _url_column_id = _seed_project(tmp_path)
    calls: list[str] = []

    def unexpected_fetch(url: str, **kwargs: Any) -> Any:
        calls.append(url)
        raise AssertionError(f"unexpected fetch for {url}")

    try:
        project.set_network_policy(mode="off")
        before = _counts(project, ("columns", "ops", "receipts", "blobs"))
        for render_mode in ("static", "playwright"):
            action = _capture_action(
                sheet_id=sheet_id,
                row_ids=[row_ids[0]],
                render_mode=render_mode,
                idempotency_key=f"capture-network-off-{render_mode}",
            )
            assert validate_root_action(action).ok is True
            result = run_action_spec(
                project,
                action,
                project_id=PROJECT_ID,
                deps=ExecutorDeps(
                    url_capture_fetcher=unexpected_fetch,
                    url_capture_browser=unexpected_fetch,
                ),
            )
            assert result.status == "failed"
            assert result.errors[0].code == "network_disabled"
        assert calls == []
        assert _counts(project, ("columns", "ops", "receipts", "blobs")) == before
        project.set_network_policy(mode="on")

        warc = _capture_action(
            sheet_id=sheet_id,
            row_ids=[row_ids[0]],
            include_warc=True,
            idempotency_key="media_capture_url@sha256:warc",
        )
        with pytest.raises(
            ValidationError, match="WARC requires page output with browser rendering"
        ):
            CapturePageParams.model_validate(warc["params"])
        warc_validation = validate_root_action(warc)
        assert warc_validation.ok is False
        assert warc_validation.error is not None
        assert (
            "WARC requires page output with browser rendering"
            in warc_validation.error.message
        )
        warc_result = run_action_spec(
            project,
            warc,
            project_id=PROJECT_ID,
            deps=ExecutorDeps(url_capture_fetcher=unexpected_fetch),
        )
        assert warc_result.status == "failed"
        assert (
            "WARC requires page output with browser rendering"
            in warc_result.errors[0].message
        )
        assert calls == []
    finally:
        project.close()


def test_web_capture_page_finalize_failure_does_not_refetch_or_commit_blob_metadata(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(capture_url, "url_is_safe", lambda url: True)
    project, sheet_id, row_ids, _url_column_id = _seed_project(tmp_path)
    action = _capture_action(
        sheet_id=sheet_id,
        row_ids=[row_ids[0]],
        idempotency_key="web_capture_page@sha256:finalize-crash",
    )
    calls: list[str] = []

    from frisket.ops.capture.url import StaticUrlFetchResult
    from frisket.engine.executor import page_capture as executor_capture

    def fake_fetch(
        url: str,
        *,
        max_bytes: int,
        timeout_ms: int,
    ) -> StaticUrlFetchResult:
        calls.append(url)
        return StaticUrlFetchResult(
            requested_url=url,
            final_url=FINAL_URL,
            status_code=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=HTML_BYTES,
            elapsed_ms=17,
            redirects=[],
        )

    monkeypatch.setattr(
        executor_capture,
        "_WEB_CAPTURE_PAGE_FAIL_AFTER_STAGING_FOR_TEST",
        True,
    )
    try:
        failed = run_action_spec(
            project,
            action,
            project_id=PROJECT_ID,
            deps=ExecutorDeps(url_capture_fetcher=fake_fetch),
        )
        assert failed.status == "failed"
        assert failed.errors[0].code == "project_write_failed"
        assert calls == [STORY_URL]
        assert int(project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]) == 0
        assert _staged_blob_files(project) == []

        monkeypatch.setattr(
            executor_capture,
            "_WEB_CAPTURE_PAGE_FAIL_AFTER_STAGING_FOR_TEST",
            False,
        )
        replay = run_action_spec(
            project,
            action,
            project_id=PROJECT_ID,
            deps=ExecutorDeps(
                url_capture_fetcher=lambda *args, **kwargs: (_ for _ in ()).throw(
                    AssertionError(
                        "capture must not refetch after staged finalize failure"
                    )
                )
            ),
        )
        assert replay.status == "failed"
        assert replay.receipt_id == failed.receipt_id
        assert calls == [STORY_URL]
        assert int(project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]) == 0
        assert _staged_blob_files(project) == []
    finally:
        project.close()


def test_capture_static_url_rejects_unsafe_url_before_injected_fetch(
    monkeypatch: Any,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(capture_url, "url_is_safe", lambda url: False)

    def unexpected_fetch(url: str, **kwargs: Any) -> Any:
        calls.append(url)
        raise AssertionError(f"unexpected fetch for {url}")

    result = capture_url.capture_static_url(
        "http://127.0.0.1/internal",
        fetch=unexpected_fetch,
    )

    assert result.status == "error"
    assert result.reason == "unsafe_url"
    assert calls == []
