from __future__ import annotations

from contextlib import closing

from frisket.engine.jobs.queue import SEARCH_INDEX_KIND
from frisket.engine.store import Project
from frisket.retrieval.preparation import DEFAULT_TEXT_SPACE
from frisket.retrieval.store import RetrievalStore
from frisket.server.workspace import Workspace


class _AvailableEncoder:
    def available(self) -> bool:
        return True


class _UnavailableEncoder:
    def available(self) -> bool:
        return False


def test_open_schedules_existing_durable_search_work(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    with closing(Project.create(root / "docs.frisket", name="Docs")):
        pass

    workspace = Workspace(root, embedding_runtime=_UnavailableEncoder())
    try:
        project = workspace.get("docs")
        jobs = workspace.queue.list_jobs()
        assert [job.kind for job in jobs] == [SEARCH_INDEX_KIND]
    finally:
        project.close()
        workspace.queue.close()


def test_open_and_model_setup_retry_only_exact_encoder_unavailability(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    bundle = root / "docs.frisket"
    with closing(Project.create(bundle, name="Docs")) as created:
        with RetrievalStore(created):
            pass

    calls: list[dict[str, object]] = []

    def requeue(_store, **kwargs) -> int:
        calls.append(kwargs)
        return 1

    monkeypatch.setattr(RetrievalStore, "requeue_unavailable", requeue)
    workspace = Workspace(root, embedding_runtime=_AvailableEncoder())
    try:
        project = workspace.get("docs")
        assert calls == [
            {
                "error_code": "encoder_unavailable",
                "space_id": DEFAULT_TEXT_SPACE.identity,
            }
        ]

        workspace._embedding_setup_completed()
        assert calls == [calls[0], calls[0]]
    finally:
        project.close()
        workspace.queue.close()
