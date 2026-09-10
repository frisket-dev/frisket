"""Worker result-summary projection contract (kind-bounded, no raw leaks).

Ported from a downstream composition's worker result-summary projection
test so this repo carries its own test of ``frisket.engine.jobs.projection``:
result summaries
are allow-listed per job kind, unknown kinds fall back to the status-only
default, and no raw worker payload (provider output, workspace paths, feed
bodies, media URLs, future private fields) ever reaches a projected summary.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from frisket.engine.jobs.projection import (
    admin_job_payload,
    diagnostic_job_context,
    job_result_summary,
)
from frisket.engine.jobs.queue import Job


def _job(kind: str, result: dict[str, Any]) -> Job:
    now = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)
    return Job(
        id=77,
        kind=kind,
        payload={
            "org_id": 8,
            "project_id": "proj_worker_summary",
            "run_id": 99,
            "source_id": 4,
            "sheet_id": 3,
            "trace_id": "trace-worker-summary",
            "action_kind": "map.extract",
        },
        status="done",
        attempts=1,
        max_attempts=3,
        locked_by="worker-a",
        locked_at=now,
        lease_expires_at=now,
        available_at=now,
        created_at=now,
        started_at=now,
        finished_at=now,
        result=result,
        error=None,
    )


def _assert_no_raw_result(value: object) -> None:
    blob = json.dumps(value, sort_keys=True).lower()
    for forbidden in (
        "raw provider payload",
        "secret-workspace",
        "feed_xml",
        "media",
        "source_url",
        "nested action output",
        "future private field",
    ):
        assert forbidden not in blob


def test_worker_result_summaries_are_kind_bounded() -> None:
    project_run_result = {
        "status": "completed",
        "total": 8,
        "completed": 7,
        "completed_rows": 7,
        "failed": 1,
        "failed_rows": 1,
        "skipped": False,
        "action_kind": "map.extract",
        "receipt_id": "receipt_project_run",
        "action_result": {"outputs": ["nested action output"]},
        "raw_result": "raw provider payload",
        "workspace_root": "/tmp/secret-workspace",
    }
    assert job_result_summary("project.run", project_run_result) == {
        "status": "completed",
        "total": 8,
        "completed": 7,
        "completed_rows": 7,
        "failed": 1,
        "failed_rows": 1,
        "skipped": False,
        "action_kind": "map.extract",
        "receipt_id": "receipt_project_run",
    }

    source_poll_result = {
        "project_id": "proj_worker_summary",
        "source_id": 4,
        "action_kind": "source.poll",
        "receipt_id": "receipt_source_poll",
        "run_id": 44,
        "new_rows": 3,
        "revisions": 2,
        "sheet_id": 9,
        "op_id": 12,
        "enclosure_jobs": 5,
        "feed_xml": "raw provider payload",
        "enclosure_row_ids": [1, 2, 3, 4, 5],
    }
    assert job_result_summary("source.poll", source_poll_result) == {
        "action_kind": "source.poll",
        "receipt_id": "receipt_source_poll",
        "run_id": 44,
        "new_rows": 3,
        "revisions": 2,
        "sheet_id": 9,
        "op_id": 12,
        "enclosure_jobs": 5,
    }

    enclosure_result = {
        "status": "downloaded",
        "row_id": 15,
        "op_id": 22,
        "media": {
            "blob": "sha256:abc",
            "source_url": "https://example.com/private.mp3",
        },
    }
    assert job_result_summary("enclosure.download", enclosure_result) == {
        "status": "downloaded",
        "row_id": 15,
        "op_id": 22,
    }

    unknown_result = {
        "status": "done",
        "completed": 50,
        "future_private_field": "future private field",
    }
    assert job_result_summary("future.worker", unknown_result) == {"status": "done"}

    admin_payload = admin_job_payload(
        _job("project.run", project_run_result),
        now=datetime(2026, 6, 18, 12, 1, tzinfo=UTC),
    )
    diagnostic_payload = diagnostic_job_context(
        _job("source.poll", source_poll_result),
        now=datetime(2026, 6, 18, 12, 1, tzinfo=UTC),
    )
    enclosure_payload = admin_job_payload(
        _job("enclosure.download", enclosure_result),
        now=datetime(2026, 6, 18, 12, 1, tzinfo=UTC),
    )
    assert admin_payload["result_summary"]["receipt_id"] == "receipt_project_run"
    assert diagnostic_payload["result_summary"]["enclosure_jobs"] == 5
    assert enclosure_payload["result_summary"] == {
        "status": "downloaded",
        "row_id": 15,
        "op_id": 22,
    }
    _assert_no_raw_result(admin_payload["result_summary"])
    _assert_no_raw_result(diagnostic_payload["result_summary"])
    _assert_no_raw_result(enclosure_payload["result_summary"])
