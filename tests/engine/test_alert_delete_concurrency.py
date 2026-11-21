from __future__ import annotations

from tests.alert_helpers import add_alert

import threading


def test_alert_delete_serializes_a_concurrent_different_row_update(
    make_project,
) -> None:
    project = make_project("Watches")
    delete_id = add_alert(
        project, "Delete me", scope="project", query={"kind": "fts", "q": "one"}
    )
    update_id = add_alert(
        project, "Update me", scope="project", query={"kind": "fts", "q": "two"}
    )
    delete_entered = threading.Event()
    release_delete = threading.Event()
    update_attempted = threading.Event()
    results: dict[str, object] = {}

    def delete() -> None:
        def trace(statement: str) -> None:
            if statement == "BEGIN IMMEDIATE":
                delete_entered.set()
                assert release_delete.wait(timeout=5)

        project.db.set_trace_callback(trace)
        results["deleted"] = project.delete_alert(delete_id)

    def update() -> None:
        update_attempted.set()
        results["updated"] = project.update_alert(update_id, name="Updated")

    # realtime: Event-gated SQLite concurrency is the behavior under test.
    deleting = threading.Thread(target=delete)
    # realtime: see the Event-gated concurrency above.
    updating = threading.Thread(target=update)
    try:
        deleting.start()
        assert delete_entered.wait(timeout=5)
        updating.start()
        assert update_attempted.wait(timeout=5)
        release_delete.set()
        deleting.join(timeout=10)
        updating.join(timeout=10)

        assert not deleting.is_alive()
        assert not updating.is_alive()
        assert results["deleted"] is True
        assert results["updated"] is not None
        updated = project.get_alert(update_id)
        assert updated is not None
        assert updated["name"] == "Updated"
        assert project.get_alert(delete_id) is None
    finally:
        release_delete.set()
        deleting.join(timeout=1)
        updating.join(timeout=1)
