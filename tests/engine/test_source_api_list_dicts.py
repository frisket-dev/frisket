from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from frisket.engine.executor import run_action_spec
from frisket.engine.jobs import (
    SqliteJobQueue,
    Worker,
    default_registry,
    enqueue_due_source_polls,
    register_source_poll_handler,
)
from frisket.server.sources.api_list_dicts import (
    API_LIST_DICTS_KIND,
    ApiListDictsHttpResponse,
    ApiListDictsPoller,
)
from frisket.server.sources.runtime import (
    SourcePollContext,
    get_source_poller,
    register_source_poller,
    unregister_source_poller,
)
from frisket.engine.store import Project
from frisket.engine.store.sources import SourceStore


PROJECT_ID = "project-source-api-list-dicts"
API_URL = "https://data.example.gov/api/items"
FIXED_FETCHED_AT = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)


class FakeHttpGet:
    def __init__(self, responses: list[ApiListDictsHttpResponse | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, *, max_bytes: int) -> ApiListDictsHttpResponse:
        self.calls.append({"url": url, "max_bytes": max_bytes})
        if not self._responses:
            raise AssertionError("fake API HTTP client exhausted")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def restore_api_poller() -> Iterator[None]:
    previous = get_source_poller(API_LIST_DICTS_KIND)
    try:
        yield
    finally:
        if previous is None:
            unregister_source_poller(API_LIST_DICTS_KIND)
        else:
            register_source_poller(previous, replace=True)


def _source_poll_action(
    *,
    source_id: int | None = None,
    source: dict[str, Any] | None = None,
    idempotency_key: str = "source_api_list_dicts@sha256:first",
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if source_id is not None:
        params["source"] = source_id
    if source is not None:
        params["source"] = source
    return {
        "action_id": "source.poll",
        "scope": {"kind": "project"},
        "params": params,
        "idempotency_key": idempotency_key,
    }


def _api_source(config: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "kind": API_LIST_DICTS_KIND,
        "name": "Public API",
        "url": API_URL,
        "config": {
            "schema_version": "frisket.source.api_list_dicts.v1",
            "method": "GET",
            "list_path": "/results",
            "item_id_path": "/id",
            "updated_at_path": "/updated",
            "max_bytes": 1_000_000,
            "max_items": 10,
            "schema_policy": "additive",
            **(config or {}),
        },
    }


def _response_json(payload: Any, *, status_code: int = 200) -> ApiListDictsHttpResponse:
    return ApiListDictsHttpResponse(
        status_code=status_code,
        body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        final_url=API_URL,
        headers={"content-type": "application/json"},
    )


def _receipt(project: Project, receipt_id: str) -> dict[str, Any]:
    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?",
        (receipt_id,),
    ).fetchone()
    assert row is not None
    return json.loads(row["body"])


def _columns(project: Project, sheet_id: int) -> list[str]:
    return [
        str(column["name"]) for column in project.columns(sheet_id, include_hidden=True)
    ]


def _rows(project: Project, sheet_id: int) -> list[dict[str, Any]]:
    columns = {
        int(column["id"]): column["name"]
        for column in project.columns(sheet_id, include_hidden=True)
    }
    out: list[dict[str, Any]] = []
    for row in project.db.execute(
        "SELECT id FROM rows WHERE sheet_id=? ORDER BY position",
        (sheet_id,),
    ).fetchall():
        values = project.db.execute(
            "SELECT column_id, value FROM cells WHERE row_id=?",
            (row["id"],),
        ).fetchall()
        out.append(
            {
                columns[int(value["column_id"])]: json.loads(value["value"])
                for value in values
            }
        )
    return out


def _table_count(project: Project, table: str) -> int:
    return int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_api_list_dicts_source_poll_materializes_rows_dedupes_and_drifts(
    tmp_path: Path,
    restore_api_poller: None,
) -> None:
    first_payload = {
        "results": [
            {
                "id": "case-1",
                "name": "Alpha",
                "details": {"score": 1},
                "tags": ["records", "alpha"],
                "updated": "2026-06-22T10:00:00Z",
            },
            {
                "id": "case-2",
                "name": "Beta",
                "status": "open",
                "details": {"score": 2},
                "updated": "2026-06-23T09:00:00Z",
            },
        ]
    }
    third_payload = {
        "results": [
            first_payload["results"][0],
            {
                "id": "case-2",
                "name": "Beta revised",
                "status": "closed",
                "agency": "Records Office",
                "details": {"score": 3},
                "updated": "2026-06-23T11:00:00Z",
            },
            {
                "id": "case-3",
                "name": "Gamma",
                "status": "open",
                "agency": "Records Office",
                "details": {"score": 4},
                "updated": "2026-06-23T11:30:00Z",
            },
        ]
    }
    fake_http = FakeHttpGet(
        [
            _response_json(first_payload),
            _response_json(first_payload),
            _response_json(third_payload),
        ]
    )
    register_source_poller(
        ApiListDictsPoller(http_get=fake_http, now=lambda: FIXED_FETCHED_AT),
        replace=True,
    )

    project = Project.create(tmp_path / "api-list-source.frisket", name="API")
    try:
        first = run_action_spec(
            project,
            _source_poll_action(source=_api_source()),
            project_id=PROJECT_ID,
        )
        assert first.status == "completed"
        assert first.receipt_id is not None
        assert fake_http.calls == [{"url": API_URL, "max_bytes": 1_000_000}]
        source_id = next(
            output.ref["source_id"]
            for output in first.outputs
            if output.kind == "source"
        )
        source_run_id = next(
            output.ref["source_run_id"]
            for output in first.outputs
            if output.kind == "source_run"
        )
        sheet_id = next(
            output.sheet_id for output in first.outputs if output.kind == "sheet"
        )
        assert sheet_id is not None

        rows = _rows(project, sheet_id)
        assert [row["id"] for row in rows] == ["case-1", "case-2"]
        assert rows[0]["source_id"] == source_id
        assert rows[0]["source_run_id"] == source_run_id
        assert rows[0]["source_item_id"] == "case-1"
        assert rows[0]["source_url"] == API_URL
        assert rows[0]["source_fetched_at"] == "2026-06-23T12:00:00Z"
        assert rows[0]["source_updated_at"] == "2026-06-22T10:00:00Z"
        assert rows[0]["source_raw"]["id"] == "case-1"
        assert rows[0]["details"] == {"score": 1}
        assert rows[0]["tags"] == ["records", "alpha"]
        record_columns = [
            name
            for name in _columns(project, sheet_id)
            if not name.startswith("source_") and name not in {"_revision", "_revises"}
        ]
        assert record_columns == ["id", "name", "details", "tags", "updated", "status"]

        first_run = project.db.execute(
            "SELECT * FROM source_runs WHERE id=?",
            (source_run_id,),
        ).fetchone()
        assert first_run is not None
        assert first_run["status"] == "ok"
        assert first_run["new_rows"] == 2
        assert first_run["warning_count"] == 0
        first_cursor = json.loads(first_run["cursor_after"])
        assert first_cursor["schema_version"] == "frisket.api_list_dicts_cursor.v1"
        assert first_cursor["list_path"] == "/results"
        assert first_cursor["items_seen"] == 2
        assert first_cursor["items_materialized"] == 2
        assert first_cursor["record_keys"] == [
            "id",
            "name",
            "details",
            "tags",
            "updated",
            "status",
        ]
        receipt = _receipt(project, first.receipt_id)
        assert receipt["action_kind"] == "source.poll"
        assert receipt["status"] == "completed"
        assert receipt["warnings"] == []
        assert receipt["provider_use"][0]["provider"] == "http"
        assert receipt["provider_use"][0]["service"] == "https_get"
        assert receipt["provider_use"][0]["url_host"] == "data.example.gov"
        assert API_URL not in json.dumps(receipt["provider_use"])
        assert _table_count(project, "source_items") == 2
        assert _table_count(project, "blobs") == 0

        second = run_action_spec(
            project,
            _source_poll_action(
                source_id=source_id,
                idempotency_key="source_api_list_dicts@sha256:second",
            ),
            project_id=PROJECT_ID,
        )
        assert second.status == "completed"
        second_run_id = next(
            output.ref["source_run_id"]
            for output in second.outputs
            if output.kind == "source_run"
        )
        second_run = project.db.execute(
            "SELECT * FROM source_runs WHERE id=?",
            (second_run_id,),
        ).fetchone()
        assert second_run["new_rows"] == 0
        assert second_run["skipped_rows"] == 2
        assert second_run["changed_rows"] == 0
        assert second_run["revisions"] == 0
        assert [row["id"] for row in _rows(project, sheet_id)] == [
            "case-1",
            "case-2",
        ]

        third = run_action_spec(
            project,
            _source_poll_action(
                source_id=source_id,
                idempotency_key="source_api_list_dicts@sha256:third",
            ),
            project_id=PROJECT_ID,
        )
        assert third.status == "completed"
        third_run_id = next(
            output.ref["source_run_id"]
            for output in third.outputs
            if output.kind == "source_run"
        )
        third_run = project.db.execute(
            "SELECT * FROM source_runs WHERE id=?",
            (third_run_id,),
        ).fetchone()
        assert third_run["new_rows"] == 1
        assert third_run["skipped_rows"] == 1
        assert third_run["changed_rows"] == 1
        assert third_run["revisions"] == 1
        rows = _rows(project, sheet_id)
        assert [row["name"] for row in rows] == [
            "Alpha",
            "Beta",
            "Beta revised",
            "Gamma",
        ]
        assert rows[2]["_revises"] == "case-2"
        assert rows[2]["_revision"] == 1
        assert rows[2]["agency"] == "Records Office"
        assert "agency" in _columns(project, sheet_id)
        assert _table_count(project, "source_items") == 3
    finally:
        project.close()


def test_api_list_dicts_skips_non_dict_items_with_warning() -> None:
    poller = ApiListDictsPoller(
        http_get=FakeHttpGet(
            [
                _response_json(
                    {
                        "results": [
                            {"id": "case-1", "name": "Alpha"},
                            "skip me",
                            {"id": "case-2", "name": "Beta"},
                        ]
                    }
                )
            ]
        ),
        now=lambda: FIXED_FETCHED_AT,
    )

    result = poller.poll(
        SourcePollContext(
            source=_api_source({"updated_at_path": None}), cursor_before=None
        )
    )

    assert [item.stable_item_id() for item in result.items] == ["case-1", "case-2"]
    assert result.warnings == ["api_list_dicts skipped non-dict item at /results[1]"]
    assert result.cursor_after["items_seen"] == 3
    assert result.cursor_after["items_materialized"] == 2


def test_api_list_dicts_invalid_identity_falls_back_with_warning() -> None:
    fallback_records = [
        {"id": None, "name": "Null"},
        {"id": [], "name": "List"},
        {"id": {}, "name": "Object"},
    ]
    poller = ApiListDictsPoller(
        http_get=FakeHttpGet([_response_json({"results": fallback_records})]),
        now=lambda: FIXED_FETCHED_AT,
    )

    source = _api_source({"updated_at_path": None})
    result = poller.poll(SourcePollContext(source=source, cursor_before=None))

    item_ids = [item.stable_item_id() for item in result.items]
    assert len(set(item_ids)) == 3
    assert all(isinstance(item_id, str) for item_id in item_ids)
    assert all(item_id.startswith("sha256:") for item_id in item_ids)

    missing_id_poller = ApiListDictsPoller(
        http_get=FakeHttpGet(
            [_response_json({"results": [{"name": "Missing identity"}]})]
        ),
        now=lambda: FIXED_FETCHED_AT,
    )
    missing_result = missing_id_poller.poll(
        SourcePollContext(source=source, cursor_before=None)
    )

    assert len(missing_result.items) == 1
    assert missing_result.items[0].stable_item_id().startswith("sha256:")
    assert missing_result.warnings == [
        "api_list_dicts item_id_path did not resolve to a scalar at "
        "/results[0]; using record hash"
    ]


def test_api_list_dicts_solo_materializes_legacy_item_cap_plus_one(
    tmp_path: Path,
    restore_api_poller: None,
) -> None:
    records = [
        {"id": f"case-{index:05d}", "name": f"Case {index}"} for index in range(10_001)
    ]
    register_source_poller(
        ApiListDictsPoller(
            http_get=FakeHttpGet([_response_json({"results": records})]),
            now=lambda: FIXED_FETCHED_AT,
        ),
        replace=True,
    )
    source = _api_source({"updated_at_path": None})
    source["config"].pop("max_items")

    project = Project.create(tmp_path / "api-list-unbounded.frisket", name="API")
    try:
        result = run_action_spec(
            project,
            _source_poll_action(
                source=source,
                idempotency_key="source_api_list_dicts@sha256:legacy-cap-plus-one",
            ),
            project_id=PROJECT_ID,
        )

        assert result.status == "completed", result.errors
        assert _table_count(project, "source_items") == 10_001
        run_ref = next(
            output.ref for output in result.outputs if output.kind == "source_run"
        )
        source_run = project.db.execute(
            "SELECT cursor_after FROM source_runs WHERE id=?",
            (run_ref["source_run_id"],),
        ).fetchone()
        assert source_run is not None
        cursor = json.loads(source_run["cursor_after"])
        assert cursor["items_seen"] == 10_001
        assert cursor["items_materialized"] == 10_001
        assert cursor["truncated"] is False
    finally:
        project.close()


def test_api_list_dicts_operator_can_raise_remote_response_byte_guard() -> None:
    payload = {"results": [{"id": "large", "payload": "x" * 5_000_000}]}
    response = _response_json(payload)
    assert len(response.body) > 5_000_000
    source = _api_source(
        {
            "updated_at_path": None,
            "max_bytes": len(response.body),
            "max_items": 1,
        }
    )
    poller = ApiListDictsPoller(
        http_get=FakeHttpGet([response]),
        now=lambda: FIXED_FETCHED_AT,
    )

    result = poller.poll(SourcePollContext(source=source, cursor_before=None))

    assert len(result.items) == 1
    assert len(result.items[0].row["payload"]) == 5_000_000
    assert result.cursor_after["response_bytes"] == len(response.body)
    assert result.cursor_after["truncated"] is False


def test_api_list_dicts_validation_root_array_caps_and_failure_receipts(
    tmp_path: Path,
    restore_api_poller: None,
) -> None:
    project = Project.create(tmp_path / "invalid.frisket", name="Invalid")
    try:
        invalid = run_action_spec(
            project,
            _source_poll_action(
                source=_api_source({"method": "POST"}),
                idempotency_key="source_api_list_dicts@sha256:invalid",
            ),
            project_id=PROJECT_ID,
        )
        assert invalid.status == "failed"
        assert invalid.errors[0].code == "unsupported_source_config"
        assert "method must be GET" in invalid.errors[0].message
        assert _table_count(project, "sources") == 0
        assert _table_count(project, "source_runs") == 0
    finally:
        project.close()

    capped_http = FakeHttpGet(
        [
            _response_json(
                [
                    {"id": "one", "name": "One"},
                    {"id": "two", "name": "Two"},
                    {"id": "three", "name": "Three"},
                ]
            )
        ]
    )
    register_source_poller(
        ApiListDictsPoller(http_get=capped_http, now=lambda: FIXED_FETCHED_AT),
        replace=True,
    )
    capped_project = Project.create(tmp_path / "capped.frisket", name="Capped")
    try:
        capped = run_action_spec(
            capped_project,
            _source_poll_action(
                source=_api_source(
                    {
                        "list_path": "/",
                        "updated_at_path": None,
                        "max_items": 2,
                    }
                ),
                idempotency_key="source_api_list_dicts@sha256:capped",
            ),
            project_id=PROJECT_ID,
        )
        assert capped.status == "completed"
        sheet_id = next(
            output.sheet_id for output in capped.outputs if output.kind == "sheet"
        )
        assert sheet_id is not None
        assert [row["id"] for row in _rows(capped_project, sheet_id)] == ["one", "two"]
        run_ref = next(
            output.ref for output in capped.outputs if output.kind == "source_run"
        )
        source_run = capped_project.db.execute(
            "SELECT * FROM source_runs WHERE id=?",
            (run_ref["source_run_id"],),
        ).fetchone()
        cursor = json.loads(source_run["cursor_after"])
        assert cursor["list_path"] == "/"
        assert cursor["truncated"] is True
        assert cursor["max_items"] == 2
        assert _receipt(capped_project, capped.receipt_id)["warnings"] == [
            "api_list_dicts item cap reached; limited to 2 items"
        ]
    finally:
        capped_project.close()

    oversized_http = FakeHttpGet(
        [ApiListDictsHttpResponse(status_code=200, body=b'{"rows": []}xxxxx')]
    )
    register_source_poller(
        ApiListDictsPoller(http_get=oversized_http, now=lambda: FIXED_FETCHED_AT),
        replace=True,
    )
    failed_project = Project.create(tmp_path / "oversized.frisket", name="Oversized")
    try:
        source_id = SourceStore(failed_project).add_source(
            name="Too large",
            kind=API_LIST_DICTS_KIND,
            url=API_URL,
            config={"list_path": "/rows", "max_bytes": 10},
        )
        failed = run_action_spec(
            failed_project,
            _source_poll_action(
                source_id=source_id,
                idempotency_key="source_api_list_dicts@sha256:oversized",
            ),
            project_id=PROJECT_ID,
        )
        assert failed.status == "failed"
        assert failed.receipt_id is not None
        assert failed.errors[0].code == "source_poll_failed"
        assert "max_bytes=10" in failed.errors[0].message
        source_run = SourceStore(failed_project).source_runs(source_id, limit=1)[0]
        assert source_run["status"] == "error"
        assert _table_count(failed_project, "source_items") == 0
        assert _receipt(failed_project, failed.receipt_id)["status"] == "failed"
    finally:
        failed_project.close()


def test_api_list_dicts_provider_failure_redacts_and_scheduled_dispatches(
    tmp_path: Path,
    restore_api_poller: None,
) -> None:
    failing_http = FakeHttpGet(
        [
            RuntimeError(
                "quota failed api_key=SECRET token=SECRET authorization: Bearer abc123"
            )
        ]
    )
    register_source_poller(
        ApiListDictsPoller(http_get=failing_http, now=lambda: FIXED_FETCHED_AT),
        replace=True,
    )
    failed_project = Project.create(tmp_path / "failed.frisket", name="Failed")
    try:
        source_id = SourceStore(failed_project).add_source(
            name="Failing API",
            kind=API_LIST_DICTS_KIND,
            url=API_URL,
            config={"list_path": "/results"},
        )
        failed = run_action_spec(
            failed_project,
            _source_poll_action(
                source_id=source_id,
                idempotency_key="source_api_list_dicts@sha256:provider-failed",
            ),
            project_id=PROJECT_ID,
        )
        assert failed.status == "failed"
        assert failed.receipt_id is not None
        assert failed.errors[0].code == "source_poll_failed"
        assert "SECRET" not in failed.errors[0].message
        assert "abc123" not in failed.errors[0].message
        source_run = SourceStore(failed_project).source_runs(source_id, limit=1)[0]
        assert source_run["status"] == "error"
        assert "SECRET" not in source_run["error"]
        assert "abc123" not in source_run["error"]
        receipt = _receipt(failed_project, failed.receipt_id)
        assert receipt["status"] == "failed"
        assert "SECRET" not in receipt["errors"][0]["message"]
        assert "abc123" not in receipt["errors"][0]["message"]
    finally:
        failed_project.close()

    scheduled_http = FakeHttpGet(
        [_response_json({"results": [{"id": "sched-1", "name": "Scheduled"}]})]
    )
    register_source_poller(
        ApiListDictsPoller(http_get=scheduled_http, now=lambda: FIXED_FETCHED_AT),
        replace=True,
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    project = Project.create(workspace / "news.frisket", name="news")
    try:
        api_source_id = SourceStore(project).add_source(
            name="Scheduled API",
            kind=API_LIST_DICTS_KIND,
            url=API_URL,
            schedule="@hourly",
            config={
                "list_path": "/results",
                "item_id_path": "/id",
                "max_items": 5,
            },
        )
        SourceStore(project).add_source(
            name="Unsupported API",
            kind="api",
            url="https://example.com/api",
            schedule="@hourly",
        )
    finally:
        project.close()

    queue = SqliteJobQueue(workspace / ".queue.db")
    try:
        jobs = enqueue_due_source_polls(workspace_root=workspace, queue=queue)
        assert len(jobs) == 1
        assert jobs[0]["source_id"] == api_source_id
        job_id = int(jobs[0]["job_id"])

        def unexpected_rss_fetch(_url: str) -> str:
            raise AssertionError("api_list_dicts must not use RSS fetch")

        registry = default_registry()
        register_source_poll_handler(
            registry,
            workspace_root=workspace,
            queue=queue,
            fetch=unexpected_rss_fetch,
        )
        assert Worker(queue, registry).run_once()
        job = queue.get(job_id)
        assert job.status == "done"
        assert job.result["action_kind"] == "source.poll"
        assert job.result["source_id"] == api_source_id
        assert job.result["new_rows"] == 1
        assert scheduled_http.calls == [{"url": API_URL, "max_bytes": 5_000_000}]

        reopened = Project(workspace / "news.frisket")
        try:
            runs = [
                dict(row) for row in SourceStore(reopened).source_runs(api_source_id)
            ]
            assert len(runs) == 1
            assert runs[0]["status"] == "ok"
            receipt = _receipt(reopened, job.result["receipt_id"])
            assert receipt["action_kind"] == "source.poll"
            assert receipt["provider_use"][0]["provider"] == "http"
        finally:
            reopened.close()
    finally:
        queue.close()
