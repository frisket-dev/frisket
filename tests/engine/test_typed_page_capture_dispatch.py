"""The public dispatcher owns one typed capture identity and real dependencies."""

import pytest

from frisket.actions.registry import ACTION_REGISTRY, COPILOT_ACTION_IDS
from frisket.contracts.action import ActionError
from frisket.engine.executor import ExecutorDeps, resolve_map_preview, run_action_spec
from frisket.engine.executor.queue_policy import INTENTIONALLY_DIRECT_V1_ACTIONS
from frisket.engine.store import Project
from frisket.ops.capture.url import BrowserUrlRenderResult, StaticUrlFetchResult


@pytest.mark.parametrize("mode", ["page", "links"])
@pytest.mark.parametrize("render", ["static", "playwright"])
@pytest.mark.parametrize("explicit", [False, True])
def test_typed_capture_dispatch_publishes_and_replays(
    tmp_path, monkeypatch, mode, render, explicit
):
    monkeypatch.setattr("frisket.ops.capture.url.url_is_safe", lambda url: True)
    project = Project.create(tmp_path / "capture.frisket")
    try:
        sheet = project.add_sheet("Source")
        column = project.add_column(sheet, "source_url", "link")
        rows = project.add_rows(
            sheet, [{"source_url": "https://example.test"}], {"source_url": column}
        )
        calls = []

        def capture(url, **kwargs):
            calls.append((url, kwargs))
            common = dict(
                requested_url=url,
                final_url=url,
                status_code=200,
                headers={"content-type": "text/html"},
            )
            html = '<html><a href="/next">Next</a></html>'
            return (
                StaticUrlFetchResult(
                    **common, body=html.encode(), elapsed_ms=1, redirects=[]
                )
                if render == "static"
                else BrowserUrlRenderResult(**common, html=html)
            )

        def unused(*args, **kwargs):
            pytest.fail("explicit capture dependencies must take precedence")

        request = {
            "action_id": "web.capture_page",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet, "row_ids": rows},
            "params": {
                "source": "source_url",
                "output_mode": mode,
                "render_mode": render,
            },
            "output_names": {"page": "Archive"}
            if mode == "page"
            else {"url": "Destination"},
            "idempotency_key": "capture-dispatch",
            **({"sheet_name": "Captured links"} if mode == "links" else {}),
        }
        deps = ExecutorDeps(
            url_capture_fetcher=unused if explicit else capture,
            url_capture_browser=unused if explicit else capture,
        )
        overrides = (
            dict(url_capture_fetcher=capture, url_capture_browser=capture)
            if explicit
            else {}
        )
        first = run_action_spec(
            project, request, project_id="capture", deps=deps, **overrides
        )
        assert first.status == "completed", first
        replay = run_action_spec(
            project, request, project_id="capture", deps=deps, **overrides
        )
        assert replay.receipt_id == first.receipt_id
        assert len(calls) == 1
        output_sheet = (
            sheet
            if mode == "page"
            else next(
                s["id"] for s in project.sheets() if s["name"] == "Captured links"
            )
        )
        assert ("Archive" if mode == "page" else "Destination") in {
            c["name"] for c in project.columns(output_sheet)
        }
    finally:
        project.close()


def test_capture_registry_has_one_owner_and_preview_has_no_effects(tmp_path):
    entry = ACTION_REGISTRY.get("web.capture_page").catalog_entry()
    assert "web.capture_page" in INTENTIONALLY_DIRECT_V1_ACTIONS
    assert "web.capture_page" not in COPILOT_ACTION_IDS
    project = Project.create(tmp_path / "preview.frisket")
    try:
        before = project.db.total_changes
        result = resolve_map_preview(project, entry["examples"][0])
        assert isinstance(result, ActionError)
        assert result.code == "unsupported_action_kind"
        assert project.db.total_changes == before
    finally:
        project.close()
