"""Source health's ``downstream_jobs`` must be scoped to the source in SQL.

Two failures, both silent under-delivery on a correctly-gated route:

1. ``SourceService.get_source_health`` read the project's newest 100 jobs of
   ANY kind and narrowed to one source in Python. A quiet source alongside a
   busy one (or ordinary ``action.run`` traffic) pushes the quiet source's
   jobs past the 100-row window, so its health panel reports nothing
   downstream while its poll job sits queued or failed.

2. The obvious fix -- filter on the ``jobs.source_id`` COLUMN -- is only
   correct if the column is populated for every job the panel attributes to a
   source. The downstream jobs the panel exists to show (``watch.evaluate``,
   the ``action.run`` embedding refresh) carry their source id ONLY inside
   ``payload["trigger_ref"]``, which ``_job_refs`` did not read: their column
   was NULL. A column-only filter would have dropped exactly them. The column
   is now minted from the same rule the reader uses.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from frisket.engine.store.sources import SourceStore
from frisket.server.app import create_app
from frisket.server.sources.health import _job_source_id


def _seed(tmp_path: Path) -> tuple[TestClient, str, int, int]:
    client = TestClient(create_app(tmp_path / "ws"))
    project_id = client.post(
        "/api/projects", json={"name": "Downstream job scope"}
    ).json()["id"]
    project = client.app.state.workspace.get(project_id)
    store = SourceStore(project)
    quiet = store.add_source(
        "Quiet feed",
        kind="rss",
        url="https://example.test/quiet.xml",
        schedule="@hourly",
    )
    busy = store.add_source(
        "Busy feed",
        kind="rss",
        url="https://example.test/busy.xml",
        schedule="@hourly",
    )
    return client, project_id, quiet, busy


def _downstream(client: TestClient, project_id: str, source_id: int) -> list[dict]:
    response = client.get(f"/api/projects/{project_id}/sources/{source_id}/health")
    assert response.status_code == 200, response.text
    return response.json()["downstream_jobs"]


def test_quiet_source_keeps_its_jobs_behind_a_busier_neighbour(
    tmp_path: Path,
) -> None:
    """The reported failure: 100 newer jobs bury the quiet source's poll job."""

    client, project_id, quiet, busy = _seed(tmp_path)
    queue = client.app.state.workspace.queue
    workspace_root = str(client.app.state.workspace.root)

    quiet_job = queue.enqueue(
        "source.poll",
        {
            "project_id": project_id,
            "source_id": quiet,
            "workspace_root": workspace_root,
        },
    )
    # Newer traffic: the busy source's polls plus ordinary action runs.
    for index in range(120):
        if index % 2:
            queue.enqueue(
                "source.poll",
                {
                    "project_id": project_id,
                    "source_id": busy,
                    "workspace_root": workspace_root,
                    "dedupe_key": f"busy-{index}",
                },
            )
        else:
            queue.enqueue(
                "action.run",
                {"project_id": project_id, "workspace_root": workspace_root},
            )

    summaries = _downstream(client, project_id, quiet)
    assert [job["job_id"] for job in summaries] == [quiet_job], (
        "the quiet source's poll job fell out of the 100-row window"
    )


def test_trigger_ref_only_jobs_stay_attributed_to_their_source(
    tmp_path: Path,
) -> None:
    """A column-only filter must not drop the jobs the panel exists to show.

    ``watch.evaluate`` and the ``action.run`` embedding refresh name their
    source only inside ``payload["trigger_ref"]``.
    """

    client, project_id, quiet, busy = _seed(tmp_path)
    queue = client.app.state.workspace.queue
    workspace_root = str(client.app.state.workspace.root)

    trigger_ref = {
        "trigger_kind": "source_run_completed",
        "source_id": quiet,
        "source_run_id": 3,
    }
    watch_job = queue.enqueue(
        "watch.evaluate",
        {
            "project_id": project_id,
            "watch_id": 1,
            "index_id": "ix",
            "trigger_ref": trigger_ref,
            "dedupe_key": "watch-1",
            "workspace_root": workspace_root,
        },
    )
    refresh_job = queue.enqueue(
        "action.run",
        {
            "project_id": project_id,
            "index_id": "ix",
            "sheet_id": 1,
            "trigger_ref": trigger_ref,
            "dedupe_key": "refresh-1",
            "workspace_root": workspace_root,
        },
    )

    # The column is the attribution, not a payload re-read at display time.
    for job_id in (watch_job, refresh_job):
        assert queue.get(job_id).source_id == quiet

    # ... and it agrees with the reader that the panel used to rely on.
    for job_id in (watch_job, refresh_job):
        assert _job_source_id(queue.get(job_id)) == quiet

    summaries = _downstream(client, project_id, quiet)
    assert sorted(job["job_id"] for job in summaries) == sorted(
        [watch_job, refresh_job]
    )
    assert _downstream(client, project_id, busy) == []
