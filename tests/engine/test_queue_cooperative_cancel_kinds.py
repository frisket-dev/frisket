"""Queue-level cancel generalization:
``JobQueue.cancel`` used to special-case exactly ``action.run`` as the one
kind cancellable while RUNNING (workers poll the durable job row between
external calls). ``model.pull``'s handler polls the same way, so it needs
the same cooperative-cancel treatment -- generalized to
``COOPERATIVELY_CANCELLABLE_RUNNING_KINDS`` rather than re-special-casing a
second kind. Every other kind (``echo`` stands in for the rest) must stay
uncancellable while running, exactly as before.
"""

from __future__ import annotations

from frisket.engine.jobs import SqliteJobQueue
from frisket.engine.jobs.queue import MODEL_PULL_KIND


def _queue(tmp_path):
    return SqliteJobQueue(tmp_path / "q.db")


def test_running_model_pull_job_is_cancellable(tmp_path) -> None:
    queue = _queue(tmp_path)
    try:
        jid = queue.enqueue(MODEL_PULL_KIND, {"pull_id": 1, "workspace_root": "x"})
        queue.claim("w1")
        assert queue.get(jid).status == "running"

        assert queue.cancel(jid) is True
        assert queue.get(jid).status == "cancelled"
    finally:
        queue.close()


def test_running_action_run_job_is_still_cancellable(tmp_path) -> None:
    queue = _queue(tmp_path)
    try:
        jid = queue.enqueue("action.run", {})
        queue.claim("w1")
        assert queue.get(jid).status == "running"

        assert queue.cancel(jid) is True
        assert queue.get(jid).status == "cancelled"
    finally:
        queue.close()


def test_running_other_kinds_are_still_not_cancellable(tmp_path) -> None:
    queue = _queue(tmp_path)
    try:
        jid = queue.enqueue("echo", {})
        queue.claim("w1")
        assert queue.get(jid).status == "running"

        assert queue.cancel(jid) is False
        assert queue.get(jid).status == "running"
    finally:
        queue.close()


def test_queued_model_pull_job_is_cancellable_before_claim(tmp_path) -> None:
    queue = _queue(tmp_path)
    try:
        jid = queue.enqueue(MODEL_PULL_KIND, {"pull_id": 1, "workspace_root": "x"})
        assert queue.cancel(jid) is True
        assert queue.get(jid).status == "cancelled"
    finally:
        queue.close()
