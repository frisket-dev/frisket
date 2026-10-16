from __future__ import annotations

import hashlib
import io
import json

from pypdf import PdfWriter

from frisket.engine.store import Project
from frisket.retrieval.content import (
    TextChunkRecipe,
    TextSurface,
    TranscriptSegment,
    TranscriptSurface,
)
from frisket.retrieval.contracts import BindingRevision, SourceRevision
from frisket.retrieval.encoding import (
    EmbeddingGemma2Space,
    EncodingError,
    EncodingPurpose,
)
from frisket.retrieval.preparation import (
    AUDIO_CHANNEL,
    DEFAULT_TRANSCRIPT_RECIPE,
    DEFAULT_PREPARATION_POLICY,
    OCR_TEXT_CHANNEL,
    PDF_PAGE_CHANNEL,
    PreparationPolicy,
    TRANSCRIPT_CHANNEL,
    UNSUPPORTED_MEDIA_CHANNEL,
    TEXT_CHANNEL,
    dispatch_resolved_blob_source,
    dispatch_resolved_evidence_source,
    dispatch_resolved_text_source,
    preparation_work_marker,
    process_blob_image_preparation_batch,
    process_text_preparation_batch,
    process_evidence_preparation_batch,
    read_preparation_policy,
    write_preparation_policy,
)
from frisket.retrieval.source_adapter import (
    ResolvedCellOcrTextSource,
    ResolvedCellTranscriptSource,
    resolve_cell_blob_source,
    resolve_cell_text_sources,
)
from frisket.retrieval.store import RetrievalStore
from frisket.search_index import drain_index, index_batch, index_needs_work


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


def _text_hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _two_page_pdf() -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_blank_page(width=144, height=72)
    writer.write(output)
    return output.getvalue()


def _drain_dirty(project, *, semantic_available: bool = True) -> None:
    while not index_batch(
        project,
        batch_size=10,
        semantic_available=semantic_available,
    ).complete:
        pass


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
            dispatch_resolved_text_source(store, resolved, recipe=recipe, space=space)
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
            assert state.space_id == space.identity
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


def test_same_source_lexical_dispatch_replay_is_idempotent(tmp_path) -> None:
    project = Project.create(tmp_path / "lexical-replay.frisket")
    try:
        sheet_id = project.add_sheet("Documents")
        column_id = project.add_column(sheet_id, "body")
        row_id = project.add_rows(
            sheet_id, [{"body": "alpha beta"}], {"body": column_id}
        )[0]
        resolved = _source(project, sheet_id, column_id, row_id)

        with RetrievalStore(project).open() as store:
            dispatch_resolved_text_source(store, resolved)
            dispatch_resolved_text_source(store, resolved)
            assert (
                store.db.execute(
                    "SELECT count(*) FROM lexical_sources WHERE source_id=?",
                    (resolved.source.source_id,),
                ).fetchone()[0]
                == 1
            )
    finally:
        project.close()


def test_unsupported_media_is_scoped_unavailable_without_encoder(
    tmp_path,
) -> None:
    project = Project.create(tmp_path / "unsupported-media.frisket")
    try:
        sheet_id = project.add_sheet("Media")
        column_id = project.add_column(sheet_id, "archive", type="file")
        digest = project.add_blob(b"archive", mime="application/zip")
        row_id = project.add_rows(
            sheet_id,
            [{"archive": {"blob": digest, "mime": "application/zip"}}],
            {"archive": column_id},
        )[0]
        with project.read_snapshot() as snapshot:
            resolved = resolve_cell_blob_source(
                snapshot,
                sheet_id=sheet_id,
                row_id=row_id,
                column_id=column_id,
            )
        assert resolved is not None

        with RetrievalStore(project).open() as store:
            dispatch_resolved_blob_source(store, resolved)
            state = store.preparation_state(
                resolved.source.source_id,
                recipe_id="unsupported-media-v1",
                channel=UNSUPPORTED_MEDIA_CHANNEL,
            )
            assert state is not None
            assert state.status == "unavailable"
            assert state.space_id is not None
            assert "application/zip" in (state.error_message or "")
    finally:
        project.close()


def test_pdf_page_views_prepare_independently_and_follow_selector_edits(
    tmp_path,
) -> None:
    project = Project.create(tmp_path / "pdf-page-views.frisket")
    encoder = _Encoder()
    try:
        sheet_id = project.add_sheet("Documents")
        column_id = project.add_column(sheet_id, "document", type="file")
        digest = project.add_blob(
            _two_page_pdf(), filename="two-pages.pdf", mime="application/pdf"
        )
        whole_row, page_one_row, page_two_row = project.add_rows(
            sheet_id,
            [
                {"document": {"blob": digest, "mime": "application/pdf"}},
                {
                    "document": {
                        "blob": digest,
                        "mime": "application/pdf",
                        "page": 1,
                    }
                },
                {
                    "document": {
                        "blob": digest,
                        "mime": "application/pdf",
                        "page": 2,
                    }
                },
            ],
            {"document": column_id},
        )
        with project.read_snapshot() as snapshot:
            whole = resolve_cell_blob_source(
                snapshot,
                sheet_id=sheet_id,
                row_id=whole_row,
                column_id=column_id,
            )
            page_one = resolve_cell_blob_source(
                snapshot,
                sheet_id=sheet_id,
                row_id=page_one_row,
                column_id=column_id,
            )
            page_two = resolve_cell_blob_source(
                snapshot,
                sheet_id=sheet_id,
                row_id=page_two_row,
                column_id=column_id,
            )
        assert whole is not None and page_one is not None and page_two is not None
        assert (
            len(
                {
                    whole.source.source_id,
                    page_one.source.source_id,
                    page_two.source.source_id,
                }
            )
            == 3
        )
        assert {
            whole.source.content_hash,
            page_one.source.content_hash,
            page_two.source.content_hash,
        } == {f"sha256:{digest}"}

        _drain_dirty(project)
        for _ in range(3):
            progress = process_blob_image_preparation_batch(
                project,
                channel=PDF_PAGE_CHANNEL,
                encoder=encoder,
                max_parts=8,
            )
            assert progress.processed_parts in {1, 2}
        assert progress.complete

        with RetrievalStore(project).open() as store:
            active = store.db.execute(
                "SELECT binding.row_id,source.source_id,part.locator_json "
                "FROM bindings binding JOIN sources source "
                "ON source.source_id=binding.source_id "
                "AND source.version_kind=binding.version_kind "
                "AND source.version_value=binding.version_value "
                "AND source.version_type=binding.version_type "
                "JOIN parts part ON part.source_id=source.source_id "
                "AND part.version_kind=source.version_kind "
                "AND part.version_value=source.version_value "
                "AND part.version_type=source.version_type "
                "WHERE binding.column_id=? ORDER BY binding.row_id,part.part_id",
                (column_id,),
            ).fetchall()
        pages_by_row: dict[int, set[tuple[int, int]]] = {}
        for row in active:
            locator = json.loads(row["locator_json"])
            pages_by_row.setdefault(int(row["row_id"]), set()).add(
                (locator["pages"]["start"], locator["pages"]["end"])
            )
        assert pages_by_row == {
            whole_row: {(1, 1), (2, 2)},
            page_one_row: {(1, 1)},
            page_two_row: {(2, 2)},
        }

        old_source = page_one.source
        old_revision = page_one.binding.retrieval_revision
        project.apply_edits(
            [
                {
                    "row_id": page_one_row,
                    "column_id": column_id,
                    "value": {
                        "blob": digest,
                        "mime": "application/pdf",
                        "page": 2,
                    },
                }
            ]
        )
        with project.read_snapshot() as snapshot:
            edited = resolve_cell_blob_source(
                snapshot,
                sheet_id=sheet_id,
                row_id=page_one_row,
                column_id=column_id,
            )
        assert edited is not None
        assert edited.source.content_hash == old_source.content_hash
        assert edited.source.version_value == old_source.version_value == digest
        assert edited.source.source_id == page_two.source.source_id
        assert edited.source.source_id != old_source.source_id
        assert edited.binding.retrieval_revision > old_revision

        _drain_dirty(project)
        with RetrievalStore(project).open() as store:
            active = store.db.execute(
                "SELECT binding.row_id,source.source_id,part.locator_json "
                "FROM bindings binding JOIN sources source "
                "ON source.source_id=binding.source_id "
                "AND source.version_kind=binding.version_kind "
                "AND source.version_value=binding.version_value "
                "AND source.version_type=binding.version_type "
                "JOIN parts part ON part.source_id=source.source_id "
                "AND part.version_kind=source.version_kind "
                "AND part.version_value=source.version_value "
                "AND part.version_type=source.version_type "
                "WHERE binding.column_id=? AND binding.row_id=?",
                (column_id, page_one_row),
            ).fetchall()
        assert len(active) == 1
        assert active[0]["source_id"] == page_two.source.source_id
        locator = json.loads(active[0]["locator_json"])
        assert locator["pages"] == {"end": 2, "start": 2}
    finally:
        project.close()


def test_preparation_policy_persists_strategy_and_defaults_raw_audio_off(
    tmp_path,
) -> None:
    project = Project.create(tmp_path / "preparation-policy.frisket")
    try:
        assert DEFAULT_PREPARATION_POLICY.video_recipe is not None
        assert DEFAULT_PREPARATION_POLICY.video_recipe.strategy == (
            "shot_clips_ffmpeg_scene_v1"
        )
        assert DEFAULT_PREPARATION_POLICY.audio_recipe is None
        policy = PreparationPolicy(video_recipe=None, audio_recipe=None)

        write_preparation_policy(project, policy)

        assert read_preparation_policy(project) == policy
        assert index_needs_work(project)
    finally:
        project.close()


def test_default_policy_records_raw_audio_as_scoped_unavailable(tmp_path) -> None:
    project = Project.create(tmp_path / "audio-disabled.frisket")
    try:
        sheet_id = project.add_sheet("Media")
        column_id = project.add_column(sheet_id, "audio", type="audio")
        digest = project.add_blob(b"audio", mime="audio/wav")
        row_id = project.add_rows(
            sheet_id,
            [{"audio": {"blob": digest, "mime": "audio/wav"}}],
            {"audio": column_id},
        )[0]
        with project.read_snapshot() as snapshot:
            resolved = resolve_cell_blob_source(
                snapshot,
                sheet_id=sheet_id,
                row_id=row_id,
                column_id=column_id,
            )
        assert resolved is not None
        with RetrievalStore(project).open() as store:
            dispatch_resolved_blob_source(store, resolved)
            row = store.db.execute(
                "SELECT status,error_message FROM preparation "
                "WHERE source_id=? AND channel=?",
                (resolved.source.source_id, AUDIO_CHANNEL),
            ).fetchone()
            assert row["status"] == "unavailable"
            assert "disabled by project policy" in row["error_message"]
    finally:
        project.close()


def test_transcript_preparation_resumes_derived_segments(tmp_path, monkeypatch) -> None:
    project = Project.create(tmp_path / "transcript-preparation.frisket")
    segment_texts = ("a" * 1200, "b" * 1200, "c" * 1200)
    transcript_text = "\n".join(segment_texts)
    source = SourceRevision(
        "transcript:evidence",
        "transcript",
        "evidence_snapshot",
        "sha256:snapshot",
        _text_hash(transcript_text),
    )
    resolved = ResolvedCellTranscriptSource(
        sheet_id=1,
        binding=BindingRevision(1, 2, 3),
        lexical_text=transcript_text,
        surface=TranscriptSurface(
            source,
            (
                TranscriptSegment("span:1", 0, 1000, segment_texts[0], 0),
                TranscriptSegment("span:2", 1000, 2000, segment_texts[1], 1),
                TranscriptSegment("span:3", 2000, 3000, segment_texts[2], 2),
            ),
        ),
    )
    monkeypatch.setattr(
        "frisket.retrieval.preparation.resolve_cell_transcript_source",
        lambda *_args, **_kwargs: resolved,
    )
    try:
        with RetrievalStore(project).open() as store:
            dispatch_resolved_evidence_source(store, resolved)

        first = process_evidence_preparation_batch(
            project,
            channel=TRANSCRIPT_CHANNEL,
            encoder=_Encoder(),
            max_parts=1,
        )
        assert first.processed_parts == 1 and first.pending
        for _ in range(4):
            progress = process_evidence_preparation_batch(
                project,
                channel=TRANSCRIPT_CHANNEL,
                encoder=_Encoder(),
                max_parts=1,
            )
            if progress.complete:
                break
        assert progress.complete
        with RetrievalStore(project).open() as store:
            state = store.preparation_state(
                source.source_id,
                recipe_id=DEFAULT_TRANSCRIPT_RECIPE.recipe_id,
                channel=TRANSCRIPT_CHANNEL,
            )
            assert state is not None and state.status == "ready"
            assert state.prepared_parts == 3
    finally:
        project.close()


def test_ocr_preparation_keeps_authoritative_span_ids(tmp_path, monkeypatch) -> None:
    project = Project.create(tmp_path / "ocr-preparation.frisket")
    text = "page one words"
    source = SourceRevision(
        "ocr:evidence",
        "ocr_text",
        "evidence_snapshot",
        "sha256:snapshot",
        _text_hash(text),
    )
    resolved = ResolvedCellOcrTextSource(
        sheet_id=1,
        binding=BindingRevision(1, 2, 3),
        lexical_text=text,
        surface=TextSurface(
            source,
            "ocr-surface",
            _text_hash(text),
            text,
            ("span:page",),
        ),
    )
    monkeypatch.setattr(
        "frisket.retrieval.preparation.resolve_cell_ocr_text_source",
        lambda *_args, **_kwargs: resolved,
    )
    try:
        with RetrievalStore(project).open() as store:
            dispatch_resolved_evidence_source(store, resolved)
        progress = process_evidence_preparation_batch(
            project,
            channel=OCR_TEXT_CHANNEL,
            encoder=_Encoder(),
        )
        assert progress.complete and progress.processed_parts == 1
        with RetrievalStore(project).open() as store:
            locator = store.db.execute(
                "SELECT locator_json FROM parts WHERE source_id=?",
                (source.source_id,),
            ).fetchone()[0]
            assert "span:page" in locator
    finally:
        project.close()


def test_media_cell_dispatches_native_blob_and_derived_ocr(
    tmp_path, monkeypatch
) -> None:
    project = Project.create(tmp_path / "media-ocr-fanout.frisket")
    try:
        sheet_id = project.add_sheet("Media")
        column_id = project.add_column(sheet_id, "image", type="image")
        digest = project.add_blob(b"image bytes", mime="image/png")
        row_id = project.add_rows(
            sheet_id,
            [{"image": {"blob": digest, "mime": "image/png"}}],
            {"image": column_id},
        )[0]
        with project.read_snapshot() as snapshot:
            blob = resolve_cell_blob_source(
                snapshot,
                sheet_id=sheet_id,
                row_id=row_id,
                column_id=column_id,
            )
        assert blob is not None
        text = "recognized words"
        ocr_source = SourceRevision(
            "ocr:evidence",
            "ocr_text",
            "evidence_snapshot",
            "sha256:snapshot",
            _text_hash(text),
        )
        ocr = ResolvedCellOcrTextSource(
            sheet_id,
            blob.binding,
            text,
            TextSurface(
                ocr_source,
                "ocr-surface",
                _text_hash(text),
                text,
                ("span:page",),
            ),
        )
        monkeypatch.setattr(
            "frisket.search_index.resolve_cell_ocr_text_source",
            lambda *_args, **_kwargs: ocr,
        )

        while not index_batch(project, batch_size=10).complete:
            pass

        with RetrievalStore(project).open() as store:
            rows = store.db.execute(
                "SELECT source.source_kind,binding.binding_id "
                "FROM bindings binding JOIN sources source "
                "ON source.source_id=binding.source_id "
                "AND source.version_kind=binding.version_kind "
                "AND source.version_value=binding.version_value "
                "AND source.version_type=binding.version_type "
                "WHERE binding.sheet_id=? AND binding.row_id=? "
                "AND binding.column_id=? ORDER BY source.source_kind",
                (sheet_id, row_id, column_id),
            ).fetchall()
            assert [row["source_kind"] for row in rows] == ["blob", "ocr_text"]
            assert len({row["binding_id"] for row in rows}) == 2
    finally:
        project.close()


def test_missing_encoder_is_unavailable_until_explicit_requeue(tmp_path) -> None:
    project = Project.create(tmp_path / "unavailable.frisket")
    recipe = TextChunkRecipe(max_codepoints=8, overlap_codepoints=2)
    space = EmbeddingGemma2Space(dimensions=128)
    try:
        sheet_id = project.add_sheet("Documents")
        column_id = project.add_column(sheet_id, "body")
        row_id = project.add_rows(
            sheet_id, [{"body": "alpha beta gamma"}], {"body": column_id}
        )[0]
        resolved = _source(project, sheet_id, column_id, row_id)
        with RetrievalStore(project).open() as store:
            dispatch_resolved_text_source(store, resolved, recipe=recipe, space=space)

        result = process_text_preparation_batch(
            project,
            encoder=None,
            recipe=recipe,
            space=space,
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
    space = EmbeddingGemma2Space(dimensions=128)
    encoder = _MissingAtStartEncoder()
    try:
        sheet_id = project.add_sheet("Documents")
        column_id = project.add_column(sheet_id, "body")
        row_id = project.add_rows(
            sheet_id, [{"body": "alpha beta"}], {"body": column_id}
        )[0]
        resolved = _source(project, sheet_id, column_id, row_id)
        with RetrievalStore(project).open() as store:
            dispatch_resolved_text_source(store, resolved, recipe=recipe, space=space)

        result = process_text_preparation_batch(
            project,
            encoder=encoder,
            recipe=recipe,
            space=space,
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


def test_preparation_reuses_project_representation_cache(tmp_path) -> None:
    project = Project.create(tmp_path / "cache.frisket")
    recipe = TextChunkRecipe(max_codepoints=32, overlap_codepoints=0)
    space = EmbeddingGemma2Space(dimensions=128)
    encoder = _Encoder()
    try:
        sheet_id = project.add_sheet("Documents")
        column_id = project.add_column(sheet_id, "body")
        row_ids = project.add_rows(
            sheet_id,
            [{"body": "same text"}, {"body": "same text"}],
            {"body": column_id},
        )
        with project.read_snapshot() as snapshot:
            resolved = resolve_cell_text_sources(
                snapshot,
                sheet_id=sheet_id,
                column_id=column_id,
                row_ids=row_ids,
            )
        with RetrievalStore(project).open() as store:
            for row_id in row_ids:
                dispatch_resolved_text_source(
                    store, resolved[row_id], recipe=recipe, space=space
                )

        for _ in row_ids:
            process_text_preparation_batch(
                project,
                encoder=encoder,
                recipe=recipe,
                space=space,
            )

        assert encoder.calls == 1
        with RetrievalStore(project).open() as store:
            assert (
                store.db.execute("SELECT count(*) FROM representations").fetchone()[0]
                == 1
            )
            assert store.db.execute("SELECT count(*) FROM parts").fetchone()[0] == 2
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
            dispatch_resolved_text_source(store, old, recipe=recipe, space=space)
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


def test_dirty_dispatch_uses_only_unified_sidecar_and_recovers_deletion(tmp_path):
    project = Project.create(tmp_path / "unified-index.frisket")
    try:
        sheet_id = project.add_sheet("Documents")
        column_id = project.add_column(sheet_id, "body")
        project.add_rows(
            sheet_id,
            [{"body": "alpha one"}, {"body": "alpha two"}],
            {"body": column_id},
        )

        first = index_batch(project, batch_size=1)
        assert first.processed <= 1 and first.pending
        assert drain_index(project, batch_size=1) == 2
        assert not (project.path / "project.search.db").exists()
        with RetrievalStore(project).open() as store:
            assert (
                store.db.execute(
                    "SELECT count(*) FROM lexical_fts WHERE lexical_fts MATCH 'alpha'"
                ).fetchone()[0]
                == 2
            )

        (project.path / "project.retrieval.db").unlink()
        assert index_needs_work(project)
        assert drain_index(project, batch_size=1) == 2
        assert not index_needs_work(project)
    finally:
        project.close()
