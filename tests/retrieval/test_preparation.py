from __future__ import annotations

from frisket.engine.store import Project
from frisket.retrieval.content import TextChunkRecipe
from frisket.retrieval.encoding import (
    EmbeddingGemma2Space,
    EncodingError,
    EncodingPurpose,
)
from frisket.retrieval.preparation import (
    TEXT_CHANNEL,
    dispatch_resolved_text_source,
    preparation_work_marker,
    process_text_preparation_batch,
)
from frisket.retrieval.source_adapter import resolve_cell_text_sources
from frisket.retrieval.store import RetrievalStore


class _Encoder:
    def __init__(self) -> None:
        self.calls = 0

    def available(self) -> bool:
        return True

    def encode(self, inputs, *, purpose, space, cancelled=None):
        assert purpose is EncodingPurpose.DOCUMENT
        assert cancelled is None or not cancelled()
        self.calls += 1
        return [tuple([1.0] + [0.0] * (space.dimensions - 1)) for _ in inputs]


class _MissingAtStartEncoder(_Encoder):
    def encode(self, inputs, *, purpose, space, cancelled=None):
        self.calls += 1
        raise EncodingError("model_unavailable", "the installed model disappeared")


def _source(project, sheet_id, column_id, row_id):
    with project.read_snapshot() as snapshot:
        return resolve_cell_text_sources(
            snapshot,
            sheet_id=sheet_id,
            column_id=column_id,
            row_ids=[row_id],
        )[row_id]


def test_lexical_dispatch_and_text_cursor_resume_across_store_opens(tmp_path) -> None:
    project = Project.create(tmp_path / "resume.frisket")
    recipe = TextChunkRecipe(max_codepoints=8, overlap_codepoints=2)
    space = EmbeddingGemma2Space(dimensions=128)
    encoder = _Encoder()
    try:
        sheet_id = project.add_sheet("Documents")
        column_id = project.add_column(sheet_id, "body")
        row_id = project.add_rows(
            sheet_id,
            [{"body": "alpha beta gamma delta"}],
            {"body": column_id},
        )[0]
        resolved = _source(project, sheet_id, column_id, row_id)
        with RetrievalStore(project).open() as store:
            dispatch_resolved_text_source(store, resolved, recipe=recipe)
            assert (
                store.db.execute(
                    "SELECT count(*) FROM lexical_fts WHERE lexical_fts MATCH 'gamma'"
                ).fetchone()[0]
                == 1
            )

        first = process_text_preparation_batch(
            project,
            encoder=encoder,
            recipe=recipe,
            space=space,
            max_parts=1,
        )
        assert first.processed_parts == 1 and first.pending
        with RetrievalStore(project).open() as store:
            state = store.preparation_state(
                resolved.source.source_id,
                recipe_id=recipe.recipe_id,
                channel=TEXT_CHANNEL,
            )
            assert state is not None
            assert state.status == "preparing" and state.cursor_json is not None

        for _ in range(10):
            progress = process_text_preparation_batch(
                project,
                encoder=encoder,
                recipe=recipe,
                space=space,
                max_parts=1,
            )
            if progress.complete:
                break
        else:
            raise AssertionError("bounded text preparation did not finish")

        with RetrievalStore(project).open() as store:
            state = store.preparation_state(
                resolved.source.source_id,
                recipe_id=recipe.recipe_id,
                channel=TEXT_CHANNEL,
            )
            assert state is not None and state.status == "ready"
            assert state.prepared_parts >= 3
            assert store.db.execute("SELECT count(*) FROM parts").fetchone()[0] == (
                state.prepared_parts
            )
            assert (
                store.db.execute(
                    "SELECT count(*) FROM part_representations"
                ).fetchone()[0]
                == state.prepared_parts
            )
        assert preparation_work_marker(project.path) is None
    finally:
        project.close()


def test_missing_encoder_is_unavailable_until_explicit_requeue(tmp_path) -> None:
    project = Project.create(tmp_path / "unavailable.frisket")
    recipe = TextChunkRecipe(max_codepoints=8, overlap_codepoints=2)
    try:
        sheet_id = project.add_sheet("Documents")
        column_id = project.add_column(sheet_id, "body")
        row_id = project.add_rows(
            sheet_id, [{"body": "alpha beta gamma"}], {"body": column_id}
        )[0]
        resolved = _source(project, sheet_id, column_id, row_id)
        with RetrievalStore(project).open() as store:
            dispatch_resolved_text_source(store, resolved, recipe=recipe)

        result = process_text_preparation_batch(
            project,
            encoder=None,
            recipe=recipe,
            space=EmbeddingGemma2Space(dimensions=128),
        )
        assert result.complete and result.processed_parts == 0
        assert preparation_work_marker(project.path) is None
        with RetrievalStore(project).open() as store:
            state = store.preparation_state(
                resolved.source.source_id,
                recipe_id=recipe.recipe_id,
                channel=TEXT_CHANNEL,
            )
            assert state is not None and state.status == "unavailable"
            assert store.requeue_unavailable(channel=TEXT_CHANNEL) == 1
        assert preparation_work_marker(project.path) is not None
    finally:
        project.close()


def test_runtime_startup_model_miss_becomes_retryable_unavailable(tmp_path) -> None:
    project = Project.create(tmp_path / "startup-miss.frisket")
    recipe = TextChunkRecipe(max_codepoints=8, overlap_codepoints=2)
    encoder = _MissingAtStartEncoder()
    try:
        sheet_id = project.add_sheet("Documents")
        column_id = project.add_column(sheet_id, "body")
        row_id = project.add_rows(
            sheet_id, [{"body": "alpha beta"}], {"body": column_id}
        )[0]
        resolved = _source(project, sheet_id, column_id, row_id)
        with RetrievalStore(project).open() as store:
            dispatch_resolved_text_source(store, resolved, recipe=recipe)

        result = process_text_preparation_batch(
            project,
            encoder=encoder,
            recipe=recipe,
            space=EmbeddingGemma2Space(dimensions=128),
        )
        assert result.complete and encoder.calls == 1
        with RetrievalStore(project).open() as store:
            state = store.preparation_state(
                resolved.source.source_id,
                recipe_id=recipe.recipe_id,
                channel=TEXT_CHANNEL,
            )
            assert state is not None
            assert (state.status, state.error_code) == (
                "unavailable",
                "encoder_unavailable",
            )
    finally:
        project.close()


def test_edit_during_preparation_rejects_old_cursor_target(tmp_path) -> None:
    project = Project.create(tmp_path / "edit.frisket")
    recipe = TextChunkRecipe(max_codepoints=8, overlap_codepoints=2)
    space = EmbeddingGemma2Space(dimensions=128)
    try:
        sheet_id = project.add_sheet("Documents")
        column_id = project.add_column(sheet_id, "body")
        row_id = project.add_rows(
            sheet_id,
            [{"body": "alpha beta gamma delta"}],
            {"body": column_id},
        )[0]
        old = _source(project, sheet_id, column_id, row_id)
        with RetrievalStore(project).open() as store:
            dispatch_resolved_text_source(store, old, recipe=recipe)
        assert process_text_preparation_batch(
            project,
            encoder=_Encoder(),
            recipe=recipe,
            space=space,
            max_parts=1,
        ).pending

        project.apply_edits(
            [{"row_id": row_id, "column_id": column_id, "value": "replacement"}]
        )
        result = process_text_preparation_batch(
            project,
            encoder=_Encoder(),
            recipe=recipe,
            space=space,
            max_parts=1,
        )
        assert result.complete and result.processed_parts == 0
        with RetrievalStore(project).open() as store:
            old_state = store.preparation_state(
                old.source.source_id,
                recipe_id=recipe.recipe_id,
                channel=TEXT_CHANNEL,
            )
            assert old_state is not None
            assert (old_state.status, old_state.error_code) == (
                "failed",
                "stale_source",
            )
    finally:
        project.close()
