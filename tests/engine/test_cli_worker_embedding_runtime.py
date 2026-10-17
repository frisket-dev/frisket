"""The local/team CLI worker owns semantic-search runtime composition."""

from __future__ import annotations

from contextlib import closing

import frisket.ai.embeddings.runtime as embedding_runtime_module
from frisket.cli import _run_worker
from frisket.engine.jobs import open_queue
from frisket.engine.jobs.search_index import enqueue_search_index
from frisket.engine.store import Project
from frisket.retrieval.store import RetrievalStore


class _RecordingEmbeddingRuntime:
    instances: list[_RecordingEmbeddingRuntime] = []

    def __init__(self) -> None:
        self.calls = 0
        self.closed = False
        self.instances.append(self)

    def available(self) -> bool:
        return True

    def encode(self, inputs, *, purpose, space, cancelled=None):
        assert cancelled is None or not cancelled()
        self.calls += 1
        return [tuple([1.0] + [0.0] * (space.dimensions - 1)) for _ in inputs]

    def close(self) -> None:
        self.closed = True


def test_local_worker_executes_semantic_index_with_owned_runtime(
    tmp_path, monkeypatch
) -> None:
    # This worker test must not leave a catalog-mutating daemon in the suite.
    monkeypatch.setattr(
        "frisket.ai.llm.pricing_refresh.start_pricing_refresh", lambda: None
    )
    workspace = tmp_path / "workspace"
    project_path = workspace / "documents.frisket"
    with closing(Project.create(project_path)) as project:
        sheet_id = project.add_sheet("Documents")
        column_id = project.add_column(sheet_id, "body")
        project.add_rows(
            sheet_id,
            [{"body": "semantic worker composition"}],
            {"body": column_id},
        )
        with closing(open_queue(workspace=workspace)) as queue:
            assert (
                enqueue_search_index(project, queue, {"project_id": "documents"})
                is not None
            )

    _RecordingEmbeddingRuntime.instances.clear()
    monkeypatch.setattr(
        embedding_runtime_module, "EmbeddingRuntime", _RecordingEmbeddingRuntime
    )

    assert _run_worker(["--drain", str(workspace)], hosted=False) == 0

    [runtime] = _RecordingEmbeddingRuntime.instances
    assert runtime.calls > 0
    assert runtime.closed
    with RetrievalStore(project_path).open() as store:
        pending = store.db.execute(
            "SELECT COUNT(*) FROM preparation WHERE status != 'ready'"
        ).fetchone()[0]
    assert pending == 0
