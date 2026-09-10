"""Org/kind-scoped queue access: ``get_scoped``/``cancel_scoped`` are atomic
WHERE-clause primitives -- ``id`` AND ``org_id`` AND ``kind`` all matched in ONE query --
never a plain ``get()``/``cancel()`` preceded by a separate authorization
read. The team local-model routes are the first caller; this file pins the
primitive itself so a future caller cannot regress to fetch-then-authorize.
"""

from __future__ import annotations

from frisket.engine.jobs import SqliteJobQueue
from frisket.engine.jobs.queue import MODEL_PULL_KIND


def _queue(tmp_path):
    return SqliteJobQueue(tmp_path / "q.db")


def test_get_scoped_returns_job_for_matching_org_and_kind(tmp_path) -> None:
    queue = _queue(tmp_path)
    try:
        jid = queue.enqueue(
            MODEL_PULL_KIND, {"pull_id": 1, "workspace_root": "x", "org_id": "7"}
        )
        job = queue.get_scoped(jid, org_id="7", kind=MODEL_PULL_KIND)
        assert job is not None
        assert job.id == jid
    finally:
        queue.close()


def test_get_scoped_refuses_wrong_org(tmp_path) -> None:
    queue = _queue(tmp_path)
    try:
        jid = queue.enqueue(
            MODEL_PULL_KIND, {"pull_id": 1, "workspace_root": "x", "org_id": "7"}
        )
        assert queue.get_scoped(jid, org_id="9", kind=MODEL_PULL_KIND) is None
        # The job is otherwise completely real -- confirming this isn't a
        # missing-job false negative.
        assert queue.get(jid) is not None
    finally:
        queue.close()


def test_get_scoped_refuses_wrong_kind(tmp_path) -> None:
    queue = _queue(tmp_path)
    try:
        jid = queue.enqueue("echo", {"org_id": "7"})
        assert queue.get_scoped(jid, org_id="7", kind=MODEL_PULL_KIND) is None
    finally:
        queue.close()


def test_get_scoped_refuses_missing_job(tmp_path) -> None:
    queue = _queue(tmp_path)
    try:
        assert queue.get_scoped(999999, org_id="7", kind=MODEL_PULL_KIND) is None
    finally:
        queue.close()


def test_cancel_scoped_cancels_queued_job_for_matching_org_and_kind(tmp_path) -> None:
    queue = _queue(tmp_path)
    try:
        jid = queue.enqueue(
            MODEL_PULL_KIND, {"pull_id": 1, "workspace_root": "x", "org_id": "7"}
        )
        assert queue.cancel_scoped(jid, org_id="7", kind=MODEL_PULL_KIND) is True
        assert queue.get(jid).status == "cancelled"
    finally:
        queue.close()


def test_cancel_scoped_cancels_running_cooperatively_cancellable_kind(tmp_path) -> None:
    queue = _queue(tmp_path)
    try:
        jid = queue.enqueue(
            MODEL_PULL_KIND, {"pull_id": 1, "workspace_root": "x", "org_id": "7"}
        )
        queue.claim("w1")
        assert queue.get(jid).status == "running"
        assert queue.cancel_scoped(jid, org_id="7", kind=MODEL_PULL_KIND) is True
        assert queue.get(jid).status == "cancelled"
    finally:
        queue.close()


def test_cancel_scoped_refuses_wrong_org_and_leaves_job_untouched(tmp_path) -> None:
    queue = _queue(tmp_path)
    try:
        jid = queue.enqueue(
            MODEL_PULL_KIND, {"pull_id": 1, "workspace_root": "x", "org_id": "7"}
        )
        assert queue.cancel_scoped(jid, org_id="9", kind=MODEL_PULL_KIND) is False
        assert queue.get(jid).status == "queued"
    finally:
        queue.close()


def test_cancel_scoped_refuses_wrong_kind_and_leaves_job_untouched(tmp_path) -> None:
    queue = _queue(tmp_path)
    try:
        jid = queue.enqueue(
            MODEL_PULL_KIND, {"pull_id": 1, "workspace_root": "x", "org_id": "7"}
        )
        assert queue.cancel_scoped(jid, org_id="7", kind="echo") is False
        assert queue.get(jid).status == "queued"
    finally:
        queue.close()


def test_cancel_scoped_refuses_missing_job(tmp_path) -> None:
    queue = _queue(tmp_path)
    try:
        assert queue.cancel_scoped(999999, org_id="7", kind=MODEL_PULL_KIND) is False
    finally:
        queue.close()


def test_cancel_scoped_does_not_cancel_running_non_cooperative_kind(tmp_path) -> None:
    queue = _queue(tmp_path)
    try:
        jid = queue.enqueue("echo", {"org_id": "7"})
        queue.claim("w1")
        assert queue.get(jid).status == "running"
        assert queue.cancel_scoped(jid, org_id="7", kind="echo") is False
        assert queue.get(jid).status == "running"
    finally:
        queue.close()
