from __future__ import annotations

import json
import threading
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from PIL import Image

from frisket.contracts.action import Receipt
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.screenshot import ScreenshotParams
from frisket.actions.system import typed_action_for_request, validate_root_action
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.executor.map_rows_action import typed_request_hash
from frisket.engine.store import Project
from frisket.ops.capture import url as capture_url
from frisket.ops.capture.url import BrowserUrlRenderResult
from frisket.server.app import create_app
from frisket.engine.store.receipts import ReceiptStore
from http_test_helpers import drain_queue
from tests.deterministic_time import controlled_time


PROJECT_ID = "project-capture-screenshot"
STORY_URL = "https://example.test/article"
FINAL_URL = "https://example.test/article?rendered=1"


def _png_bytes():
    stream = BytesIO()
    Image.new("RGB", (2, 2), "navy").save(stream, format="PNG")
    return stream.getvalue()


SCREENSHOT_BYTES = _png_bytes()


def _action(
    *,
    sheet_id: int,
    row_ids: list[int],
    full_page: bool = False,
    viewport_width: int = 1280,
    viewport_height: int = 720,
    idempotency_key: str = "web_capture_screenshot@sha256:viewport",
) -> dict[str, Any]:
    return {
        "action_id": "web.capture_screenshot",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": row_ids},
        "output_names": {"screenshot": "image"},
        "params": {
            "source": "url",
            "full_page": full_page,
            "viewport_width": viewport_width,
            "viewport_height": viewport_height,
            "max_bytes": 25_000,
            "timeout_ms": 2345,
        },
        "idempotency_key": idempotency_key,
    }


def _seed_project(tmp_path: Path) -> tuple[Project, int, list[int]]:
    project = Project.create(
        tmp_path / "capture-screenshot.frisket", name="Capture Screenshot"
    )
    sheet_id = project.add_sheet("Links")
    url_column_id = project.add_column(sheet_id, "url", type="link")
    row_ids = project.add_rows(
        sheet_id,
        [{"url": STORY_URL}],
        {"url": url_column_id},
    )
    return project, sheet_id, row_ids


def _columns(project: Project, sheet_id: int) -> dict[str, Any]:
    return {
        str(column["name"]): column
        for column in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=?", (sheet_id,)
        ).fetchall()
    }


def test_capture_screenshot_defaults_and_viewport_bounds() -> None:
    params = ScreenshotParams.model_validate({"source": "url"})

    assert params.full_page is True
    assert params.viewport_width == 1280
    assert params.viewport_height == 720

    for field in ("viewport_width", "viewport_height"):
        for value in (True, 1.5, 0, 4097):
            with pytest.raises(ValidationError):
                ScreenshotParams.model_validate({"source": "url", field: value})


def test_omitted_and_explicit_default_viewports_have_one_canonical_identity() -> None:
    omitted = _action(sheet_id=4, row_ids=[1])
    omitted["params"].pop("viewport_width")
    omitted["params"].pop("viewport_height")
    explicit = _action(sheet_id=4, row_ids=[1])

    omitted_result = typed_action_for_request(omitted)
    explicit_result = typed_action_for_request(explicit)
    assert omitted_result.params.viewport_width == 1280
    assert omitted_result.params.viewport_height == 720
    assert omitted_result.params == explicit_result.params
    assert typed_request_hash(omitted_result) == typed_request_hash(explicit_result)


@pytest.mark.parametrize("full_page", [False, True])
def test_capture_screenshot_writes_png_image_uses_browser_and_replays(
    tmp_path: Path,
    monkeypatch: Any,
    full_page: bool,
) -> None:
    monkeypatch.setattr(capture_url, "url_is_safe", lambda url: True)
    project, sheet_id, row_ids = _seed_project(tmp_path)
    action = _action(sheet_id=sheet_id, row_ids=row_ids, full_page=full_page)
    browser_calls: list[tuple[str, int, int, bool, bool, int, int]] = []
    static_calls: list[str] = []

    def unexpected_static_fetch(*args: Any, **kwargs: Any) -> None:
        static_calls.append("called")
        raise AssertionError("static fetcher must not be used for screenshots")

    def fake_browser(
        url: str,
        *,
        max_bytes: int,
        timeout_ms: int,
        full_page: bool,
        capture_screenshot: bool,
        viewport_width: int,
        viewport_height: int,
    ) -> BrowserUrlRenderResult:
        browser_calls.append(
            (
                url,
                max_bytes,
                timeout_ms,
                full_page,
                capture_screenshot,
                viewport_width,
                viewport_height,
            )
        )
        return BrowserUrlRenderResult(
            requested_url=url,
            final_url=FINAL_URL,
            status_code=200,
            headers={"content-type": "text/html; charset=utf-8"},
            html="<title>Rendered page</title>",
            screenshot=SCREENSHOT_BYTES,
            screenshot_mime="image/png",
            elapsed_ms=31,
            resources=[],
        )

    try:
        entry = ACTION_REGISTRY.get("web.capture_screenshot").catalog_entry()
        assert "external:browser_render" in entry["required_capabilities"]
        assert validate_root_action(action).ok is True
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
        assert browser_calls == [(STORY_URL, 25_000, 2345, full_page, True, 1280, 720)]
        assert static_calls == []

        columns = _columns(project, sheet_id)
        assert columns["image"]["type"] == "image"
        values = project.get_values(sheet_id, int(columns["image"]["id"]))
        image = values[row_ids[0]]
        assert image["mime"] == "image/png"
        assert project.read_blob(image["blob"]) == SCREENSHOT_BYTES
        receipt_row = project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()
        assert receipt_row is not None
        receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
        assert receipt.action_kind == "web.capture_screenshot"
        artifact = project.db.execute(
            "SELECT * FROM source_artifacts WHERE artifact_kind='capture_screenshot'"
        ).fetchone()
        assert artifact["source_url"] == FINAL_URL
        assert artifact["blob_hash"] == image["blob"]
        capture_facts = json.loads(artifact["metadata"])
        assert capture_facts["render_mode"] == "playwright"
        assert capture_facts["full_page"] is full_page
        assert capture_facts["viewport"] == {"width": 1280, "height": 720}
        assert capture_facts["network_metadata"]["method"] == "GET"

        replay = run_action_spec(
            project,
            action,
            project_id=PROJECT_ID,
            deps=ExecutorDeps(
                url_capture_fetcher=unexpected_static_fetch,
                url_capture_browser=lambda *args, **kwargs: (_ for _ in ()).throw(
                    AssertionError("browser must not run during replay")
                ),
            ),
        )
        assert replay.status == "completed", replay.errors
        assert browser_calls == [(STORY_URL, 25_000, 2345, full_page, True, 1280, 720)]
        assert static_calls == []
    finally:
        project.close()


def test_capture_screenshot_custom_viewport_is_provenanced_and_idempotent(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(capture_url, "url_is_safe", lambda url: True)
    project, sheet_id, row_ids = _seed_project(tmp_path)
    action = _action(
        sheet_id=sheet_id,
        row_ids=row_ids,
        viewport_width=1365,
        viewport_height=777,
        idempotency_key="web_capture_screenshot@sha256:custom-viewport",
    )
    browser_calls: list[tuple[int, int]] = []

    def fake_browser(
        url: str,
        *,
        max_bytes: int,
        timeout_ms: int,
        full_page: bool,
        capture_screenshot: bool,
        viewport_width: int,
        viewport_height: int,
    ) -> BrowserUrlRenderResult:
        del url, max_bytes, timeout_ms, full_page, capture_screenshot
        browser_calls.append((viewport_width, viewport_height))
        return BrowserUrlRenderResult(
            requested_url=STORY_URL,
            final_url=FINAL_URL,
            status_code=200,
            headers={"content-type": "text/html; charset=utf-8"},
            html="<title>Rendered page</title>",
            screenshot=SCREENSHOT_BYTES,
            screenshot_mime="image/png",
            elapsed_ms=31,
            resources=[],
        )

    try:
        result = run_action_spec(
            project,
            action,
            project_id=PROJECT_ID,
            deps=ExecutorDeps(url_capture_browser=fake_browser),
        )
        assert result.status == "completed", result.errors
        assert browser_calls == [(1365, 777)]

        artifact = project.db.execute(
            "SELECT metadata FROM source_artifacts WHERE artifact_kind='capture_screenshot'"
        ).fetchone()
        assert artifact is not None
        metadata = json.loads(artifact["metadata"])
        assert metadata["viewport"] == {"width": 1365, "height": 777}
        assert metadata["full_page"] is False

        conflict_action = _action(
            sheet_id=sheet_id,
            row_ids=row_ids,
            viewport_width=1366,
            viewport_height=777,
            idempotency_key=action["idempotency_key"],
        )
        conflict = run_action_spec(
            project,
            conflict_action,
            project_id=PROJECT_ID,
            deps=ExecutorDeps(url_capture_browser=fake_browser),
        )
        assert conflict.status == "failed"
        assert conflict.errors[0].code == "idempotency_conflict"
        assert browser_calls == [(1365, 777)]
    finally:
        project.close()


@pytest.fixture
def screenshot_client(tmp_path, monkeypatch):
    monkeypatch.setattr(capture_url, "url_is_safe", lambda url: True)
    calls = []

    def browser(url, **kwargs):
        calls.append((url, kwargs))
        return BrowserUrlRenderResult(
            requested_url=url,
            final_url=url,
            status_code=200,
            html="<title>Captured</title>",
            screenshot=SCREENSHOT_BYTES,
        )

    monkeypatch.setattr(capture_url, "render_playwright_url", browser)
    with TestClient(create_app(tmp_path / "workspace")) as client:
        pid = client.post("/api/projects", json={"name": "Screenshots"}).json()["id"]
        project = client.app.state.workspace.get(pid)
        sheet = project.add_sheet("URLs")
        source = project.add_column(sheet, "url", type="link")
        rows = project.add_rows(
            sheet, [{"url": STORY_URL}, {"url": FINAL_URL}], {"url": source}
        )
        yield client, pid, project, sheet, rows, calls


def test_queued_screenshot_same_bytes_keep_occurrence_evidence_and_one_undo(
    screenshot_client,
):
    client, pid, project, sheet, rows, calls = screenshot_client
    body = _action(sheet_id=sheet, row_ids=rows)
    response = client.post(f"/api/projects/{pid}/actions/v1/run", json=body)
    assert response.status_code == 200, response.text
    queued = response.json()
    assert queued["status"] == "queued"
    assert calls == []
    drain_queue(client)
    receipt = ReceiptStore(project).parsed_by_id(queued["receipt_id"])
    assert receipt.status == "completed", receipt.errors
    assert len(calls) == 2
    column = _columns(project, sheet)["image"]
    values = project.get_values(sheet, column["id"])
    assert values[rows[0]]["blob"] == values[rows[1]]["blob"]
    artifacts = project.db.execute(
        "SELECT source_row_id, blob_hash FROM source_artifacts WHERE artifact_kind='capture_screenshot'"
    ).fetchall()
    assert {item["source_row_id"] for item in artifacts} == set(rows)
    assert len(artifacts) == 2
    assert project.db.execute("SELECT COUNT(*) FROM source_spans").fetchone()[0] == 2
    assert len(receipt.op_ids) == 1
    replay = client.post(f"/api/projects/{pid}/actions/v1/run", json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["receipt_id"] == queued["receipt_id"]
    assert len(calls) == 2
    assert project.undo() == receipt.op_ids[0]
    assert not any(project.get_values(sheet, column["id"]).values())


def test_screenshot_preview_is_ephemeral_and_network_off_refuses(screenshot_client):
    client, pid, project, sheet, rows, calls = screenshot_client
    body = _action(sheet_id=sheet, row_ids=rows)
    preview = client.post(f"/api/projects/{pid}/actions/v1/preview", json=body)
    assert preview.status_code == 202, preview.text
    preview_id = preview.json()["preview_id"]
    polled = None

    def finished() -> bool:
        nonlocal polled
        polled = client.get(f"/api/projects/{pid}/actions/v1/preview/{preview_id}")
        assert polled.status_code == 200, polled.text
        return polled.json()["status"] != "running"

    with controlled_time(timeout=5) as clock:
        clock.wait_until(finished, message="screenshot preview did not finish")
    assert polled is not None
    assert polled.json()["status"] == "done", polled.text
    assert len(polled.json()["result"]["rows"]) == len(rows)
    assert len(calls) == len(rows)
    assert set(_columns(project, sheet)) == {"url"}
    project.set_network_policy(mode="off")
    refused = client.post(f"/api/projects/{pid}/actions/v1/run", json=body)
    assert refused.status_code >= 400, refused.text
    assert "network_disabled" in refused.text
    assert len(calls) == len(rows)
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert (
        project.db.execute("SELECT COUNT(*) FROM source_artifacts").fetchone()[0] == 0
    )


def test_late_evidence_failure_rolls_back_only_its_row(screenshot_client, monkeypatch):
    from frisket.engine.executor import screenshot_read

    client, pid, project, sheet, rows, calls = screenshot_client
    real_publish = screenshot_read.publish_row_file_evidence
    real_browser = capture_url.render_playwright_url
    first_published = threading.Event()

    def ordered_browser(url, **kwargs):
        if url == FINAL_URL:
            assert first_published.wait(timeout=5)
        return real_browser(url, **kwargs)

    def fail_second(project, descriptor, **kwargs):
        publication = real_publish(project, descriptor, **kwargs)
        if kwargs["row_id"] == rows[1]:
            raise RuntimeError("injected post-evidence SQL failure")
        first_published.set()
        return publication

    monkeypatch.setattr(capture_url, "render_playwright_url", ordered_browser)
    monkeypatch.setattr(screenshot_read, "publish_row_file_evidence", fail_second)
    response = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=_action(sheet_id=sheet, row_ids=rows),
    )
    assert response.status_code == 200, response.text
    drain_queue(client)
    receipt = ReceiptStore(project).parsed_by_id(response.json()["receipt_id"])
    # Unexpected SQL failure retains the live writer for reconciliation. It
    # must not release authority and permit the returned browser call again.
    assert receipt.status == "queued"
    job = client.app.state.workspace.queue.get(response.json()["job_id"])
    assert job.status == "failed"
    run = project.db.execute("SELECT id, op_id, status FROM runs").fetchone()
    assert run["status"] == "running"
    checkpoints = project.db.execute(
        "SELECT state FROM effect_checkpoints WHERE family='row_effect' AND group_key=?",
        (str(run["id"]),),
    ).fetchall()
    assert [item["state"] for item in checkpoints] == ["returned"]
    assert len(calls) == 2
    image = _columns(project, sheet)["image"]
    values = project.get_values(sheet, image["id"])
    assert values[rows[0]]["mime"] == "image/png"
    assert project.read_blob(values[rows[0]]["blob"]) == SCREENSHOT_BYTES
    assert values[rows[1]] is None
    assert [
        item[0]
        for item in project.db.execute(
            "SELECT source_row_id FROM source_artifacts WHERE artifact_kind='capture_screenshot'"
        )
    ] == [rows[0]]
    assert project.db.execute("SELECT COUNT(*) FROM source_spans").fetchone()[0] == 1
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    _, refs = project.get_values_with_refs(sheet, image["id"])
    assert refs[rows[0]]["op_id"] == run["op_id"]


@pytest.mark.parametrize(
    "table",
    ["source_artifacts", "source_spans", "evidence_links", "evidence_link_spans"],
)
def test_screenshot_replay_refuses_missing_published_evidence(screenshot_client, table):
    client, pid, project, sheet, rows, calls = screenshot_client
    body = _action(sheet_id=sheet, row_ids=rows[:1])
    endpoint = f"/api/projects/{pid}/actions/v1/run"
    queued = client.post(endpoint, json=body)
    assert queued.status_code == 200, queued.text
    drain_queue(client)
    receipt = ReceiptStore(project).parsed_by_id(queued.json()["receipt_id"])
    assert receipt.status == "completed", receipt.errors
    assert len(calls) == 1
    project.db.execute(f"DELETE FROM {table}")
    project.db.commit()
    replay = client.post(endpoint, json=body)
    assert replay.status_code == 409, replay.text
    assert replay.json()["errors"][0]["code"] == "stale_replay"
    assert len(calls) == 1
