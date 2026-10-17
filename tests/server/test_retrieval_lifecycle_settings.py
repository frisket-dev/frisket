from __future__ import annotations

from contextlib import closing

from frisket.engine.jobs.queue import SEARCH_INDEX_KIND
from frisket.engine.store import Project
from frisket.retrieval.contracts import SourceRevision
from frisket.retrieval.preparation import DEFAULT_TEXT_SPACE
from frisket.retrieval.store import RetrievalStore
from frisket.server.services import search_preparation as preparation_module
from frisket.server.services.search_preparation import SearchPreparationService
from frisket.server.workspace import Workspace


class _AvailableEncoder:
    def available(self) -> bool:
        return True


class _UnavailableEncoder:
    def available(self) -> bool:
        return False


class _MutableEncoder:
    available_now = False

    def available(self) -> bool:
        return self.available_now


def test_open_schedules_existing_durable_search_work(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    with closing(Project.create(root / "docs.frisket", name="Docs")) as project:
        sheet = project.add_sheet("Notes")
        column = project.add_column(sheet, "text")
        project.add_rows(sheet, [{"text": "pending content"}], {"text": column})

    workspace = Workspace(root, embedding_runtime=_UnavailableEncoder())
    try:
        project = workspace.get("docs")
        jobs = workspace.queue.list_jobs()
        assert [job.kind for job in jobs] == [SEARCH_INDEX_KIND]
    finally:
        project.close()
        workspace.queue.close()


def test_open_and_model_setup_retry_only_exact_encoder_unavailability(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    bundle = root / "docs.frisket"
    with closing(Project.create(bundle, name="Docs")) as created:
        encoder_source = SourceRevision("cell:1:1", "text", "retrieval_revision", 1)
        disabled_source = SourceRevision("cell:2:1", "text", "retrieval_revision", 1)
        with RetrievalStore(created) as store:
            for source in (encoder_source, disabled_source):
                store.begin_preparation(
                    source,
                    recipe_id="text-v1",
                    channel="text",
                    space_id=DEFAULT_TEXT_SPACE.identity,
                )
            assert store.mark_preparation_unavailable(
                encoder_source,
                recipe_id="text-v1",
                channel="text",
                space_id=DEFAULT_TEXT_SPACE.identity,
                error_code="encoder_unavailable",
                reason="install the encoder",
            )
            assert store.mark_preparation_unavailable(
                disabled_source,
                recipe_id="text-v1",
                channel="text",
                space_id=DEFAULT_TEXT_SPACE.identity,
                error_code="policy_disabled",
                reason="disabled by policy",
            )

    workspace = Workspace(root, embedding_runtime=_AvailableEncoder())
    try:
        project = workspace.get("docs")
        with RetrievalStore(project) as store:
            encoder_state = store.preparation_state(
                encoder_source.source_id, recipe_id="text-v1", channel="text"
            )
            disabled_state = store.preparation_state(
                disabled_source.source_id, recipe_id="text-v1", channel="text"
            )
            assert encoder_state is not None and encoder_state.status == "pending"
            assert disabled_state is not None
            assert (disabled_state.status, disabled_state.error_code) == (
                "unavailable",
                "policy_disabled",
            )
            assert store.mark_preparation_unavailable(
                encoder_source,
                recipe_id="text-v1",
                channel="text",
                space_id=DEFAULT_TEXT_SPACE.identity,
                error_code="encoder_unavailable",
                reason="install the encoder",
            )

        workspace._embedding_setup_completed()
        with RetrievalStore(project) as store:
            state = store.preparation_state(
                encoder_source.source_id, recipe_id="text-v1", channel="text"
            )
            assert state is not None and state.status == "pending"
    finally:
        project.close()
        workspace.queue.close()


def test_preparation_status_poll_observes_external_encoder_setup(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(preparation_module, "_requirements", lambda: ())
    encoder = _MutableEncoder()
    workspace = Workspace(tmp_path / "workspace", embedding_runtime=encoder)
    project_id = workspace.create("Docs")["id"]
    project = workspace.get(project_id)
    source = SourceRevision("cell:1:1", "text", "retrieval_revision", 1)
    try:
        with RetrievalStore(project) as store:
            store.begin_preparation(
                source,
                recipe_id="text-v1",
                channel="text",
                space_id=DEFAULT_TEXT_SPACE.identity,
            )
            assert store.mark_preparation_unavailable(
                source,
                recipe_id="text-v1",
                channel="text",
                space_id=DEFAULT_TEXT_SPACE.identity,
                error_code="encoder_unavailable",
                reason="install the encoder",
            )

        encoder.available_now = True
        status = SearchPreparationService(workspace).list(project_id)

        with RetrievalStore(project) as store:
            resumed = store.preparation_state(
                source.source_id, recipe_id="text-v1", channel="text"
            )
        assert resumed is not None and resumed.status == "pending"
        assert status.indexing.status == "queued"
        assert status.indexing.job_id is not None
    finally:
        project.close()
        workspace.queue.close()
