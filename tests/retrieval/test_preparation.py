from __future__ import annotations

import hashlib
import io
import json

import pytest
from PIL import Image
from pypdf import PdfWriter

from frisket.engine.jobs import search_index as search_index_job
from frisket.engine.pdf_render import PdfRenderError
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
    ImageEncodingInput,
)
from frisket.retrieval.preparation import (
    AUDIO_CHANNEL,
    DEFAULT_TRANSCRIPT_RECIPE,
    DEFAULT_PREPARATION_POLICY,
    IMAGE_CHANNEL,
    OCR_TEXT_CHANNEL,
    PDF_PAGE_CHANNEL,
    PreparationPolicy,
    PreparationProgress,
    TRANSCRIPT_CHANNEL,
    UNSUPPORTED_MEDIA_CHANNEL,
    TEXT_CHANNEL,
    VIDEO_CHANNEL,
    dispatch_resolved_blob_source,
    dispatch_resolved_evidence_source,
    dispatch_resolved_text_source,
    preparation_work_marker,
    process_blob_image_preparation_batch,
    process_text_preparation_batch,
    process_evidence_preparation_batch,
    process_timed_media_preparation_batch,
    read_preparation_policy,
    write_preparation_policy,
)
from frisket.retrieval.preparation_policy import PREPARATION_POLICY_META_KEY
from frisket.retrieval.media import AudioWindowRecipe
from frisket.retrieval.source_adapter import (
    ResolvedCellOcrTextSource,
    ResolvedCellTranscriptSource,
    resolve_cell_blob_source,
    resolve_cell_text_sources,
)
from frisket.retrieval.store import RetrievalStore
from frisket.search_index import (
    IndexProgress,
    drain_index,
    index_batch,
    index_needs_work,
)


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


class _RejectInvalidImageEncoder(_Encoder):
    def __init__(self) -> None:
        super().__init__()
        self.batch_sizes: list[int] = []

    def encode(self, inputs, *, purpose, space, cancelled=None):
        self.batch_sizes.append(len(inputs))
        if any(
            isinstance(value, ImageEncodingInput)
            and value.image_bytes.startswith(b"<svg")
            for value in inputs
        ):
            self.calls += 1
            raise EncodingError("invalid_input", "the encoded image is invalid")
        return super().encode(inputs, purpose=purpose, space=space, cancelled=cancelled)


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


def _current_binding(project: Project) -> tuple[int, BindingRevision]:
    sheet_id = project.add_sheet("Evidence")
    column_id = project.add_column(sheet_id, "source")
    row_id = project.add_rows(
        sheet_id, [{"source": "authority"}], {"source": column_id}
    )[0]
    revision = project.db.execute(
        "SELECT retrieval_revision FROM current_cells WHERE row_id=? AND column_id=?",
        (row_id, column_id),
    ).fetchone()[0]
    return sheet_id, BindingRevision(row_id, column_id, int(revision))


def _two_page_pdf() -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_blank_page(width=144, height=72)
    writer.write(output)
    return output.getvalue()


def _three_blank_page_pdf() -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=72, height=72)
    writer.write(output)
    return output.getvalue()


def _tiny_png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(output, format="PNG")
    return output.getvalue()


def _drain_dirty(project) -> None:
    while not index_batch(project, batch_size=10).complete:
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
                    "SELECT count(*) FROM lexical_sources lexical "
                    "JOIN sources source ON source.source_key=lexical.source_key "
                    "WHERE source.source_id=?",
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
                "ON source.source_key=binding.source_key "
                "JOIN parts part ON part.source_key=source.source_key "
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
                "ON source.source_key=binding.source_key "
                "JOIN parts part ON part.source_key=source.source_key "
                "WHERE binding.column_id=? AND binding.row_id=?",
                (column_id, page_one_row),
            ).fetchall()
        assert len(active) == 1
        assert active[0]["source_id"] == page_two.source.source_id
        locator = json.loads(active[0]["locator_json"])
        assert locator["pages"] == {"end": 2, "start": 2}
    finally:
        project.close()


def test_pdf_render_failure_marks_target_failed_and_releases_queue(
    tmp_path, monkeypatch
) -> None:
    project = Project.create(tmp_path / "bad-pdf.frisket")

    async def fail_render(*_args, **_kwargs):
        raise PdfRenderError("invalid PDF bytes")

    monkeypatch.setattr("frisket.retrieval.preparation.render_pdf_pages", fail_render)
    try:
        sheet_id = project.add_sheet("Documents")
        column_id = project.add_column(sheet_id, "document", type="file")
        digest = project.add_blob(b"not a PDF", mime="application/pdf")
        row_id = project.add_rows(
            sheet_id,
            [{"document": {"blob": digest, "mime": "application/pdf"}}],
            {"document": column_id},
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

        progress = process_blob_image_preparation_batch(
            project,
            channel=PDF_PAGE_CHANNEL,
            encoder=_Encoder(),
        )

        assert progress.complete and progress.processed_parts == 0
        with RetrievalStore(project).open() as store:
            state = store.preparation_state(
                resolved.source.source_id,
                recipe_id="pdf-page-png-v1:dpi=144:max-edge=1600",
                channel=PDF_PAGE_CHANNEL,
            )
            assert state is not None
            assert (state.status, state.error_code) == (
                "failed",
                "pdf_render_failed",
            )
    finally:
        project.close()


@pytest.mark.parametrize(
    ("mime_type", "column_type", "channel"),
    [
        ("image/png", "image", IMAGE_CHANNEL),
        ("video/mp4", "video", VIDEO_CHANNEL),
    ],
)
def test_missing_canonical_blob_marks_native_preparation_failed(
    tmp_path, mime_type: str, column_type: str, channel: str
) -> None:
    project = Project.create(tmp_path / f"missing-{column_type}.frisket")
    try:
        sheet_id = project.add_sheet("Media")
        column_id = project.add_column(sheet_id, "media", type=column_type)
        digest = project.add_blob(b"placeholder", mime=mime_type)
        row_id = project.add_rows(
            sheet_id,
            [{"media": {"blob": digest, "mime": mime_type}}],
            {"media": column_id},
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
        (project.path / "blobs" / digest[:2] / digest).unlink()

        if channel == IMAGE_CHANNEL:
            progress = process_blob_image_preparation_batch(
                project, channel=channel, encoder=_Encoder()
            )
        else:
            progress = process_timed_media_preparation_batch(
                project, channel=channel, encoder=_Encoder()
            )

        assert progress.complete and progress.processed_parts == 0
        with RetrievalStore(project).open() as store:
            state = store.db.execute(
                "SELECT status,error_code FROM preparation "
                "WHERE source_id=? AND channel=?",
                (resolved.source.source_id, channel),
            ).fetchone()
            assert tuple(state) == ("failed", "missing_blob")

        # Restoration itself does not create a worker retry loop. The next
        # authoritative source event re-registers this exact target.
        assert project.add_blob(b"placeholder", mime=mime_type) == digest
        with RetrievalStore(project).open() as store:
            dispatch_resolved_blob_source(store, resolved)
            state = store.db.execute(
                "SELECT status,error_code FROM preparation "
                "WHERE source_id=? AND channel=?",
                (resolved.source.source_id, channel),
            ).fetchone()
            assert tuple(state) == ("pending", None)
    finally:
        project.close()


def test_repaired_corrupt_blob_requeues_on_next_authoritative_dispatch(
    tmp_path,
) -> None:
    project = Project.create(tmp_path / "repaired-corrupt-image.frisket")
    image_bytes = _tiny_png()
    try:
        sheet_id = project.add_sheet("Media")
        column_id = project.add_column(sheet_id, "image", type="image")
        digest = project.add_blob(image_bytes, mime="image/png")
        row_id = project.add_rows(
            sheet_id,
            [{"image": {"blob": digest, "mime": "image/png"}}],
            {"image": column_id},
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

        canonical = project.path / "blobs" / digest[:2] / digest
        canonical.write_bytes(b"corrupt")
        failed = process_blob_image_preparation_batch(
            project, channel=IMAGE_CHANNEL, encoder=_Encoder()
        )
        assert failed.complete and failed.processed_parts == 0
        with RetrievalStore(project).open() as store:
            state = store.preparation_state(
                resolved.source.source_id,
                recipe_id="image-bytes-v1",
                channel=IMAGE_CHANNEL,
            )
            assert state is not None
            assert (state.status, state.error_code) == (
                "failed",
                "blob_integrity_failed",
            )

        # Repairing storage does not create a hot worker retry. The next
        # authoritative source dispatch is the bounded retry trigger.
        canonical.unlink()
        assert project.add_blob(image_bytes, mime="image/png") == digest
        with RetrievalStore(project).open() as store:
            dispatch_resolved_blob_source(store, resolved)
            state = store.preparation_state(
                resolved.source.source_id,
                recipe_id="image-bytes-v1",
                channel=IMAGE_CHANNEL,
            )
            assert state is not None
            assert (state.status, state.error_code) == ("preparing", None)

        repaired = process_blob_image_preparation_batch(
            project, channel=IMAGE_CHANNEL, encoder=_Encoder()
        )
        assert repaired.complete and repaired.processed_parts == 1
        with RetrievalStore(project).open() as store:
            state = store.preparation_state(
                resolved.source.source_id,
                recipe_id="image-bytes-v1",
                channel=IMAGE_CHANNEL,
            )
            assert state is not None
            assert (state.status, state.error_code) == ("ready", None)
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


def test_preparation_policy_partial_json_inherits_defaults_and_null_disables(
    tmp_path,
) -> None:
    project = Project.create(tmp_path / "partial-preparation-policy.frisket")
    try:
        project.set_meta(PREPARATION_POLICY_META_KEY, '{"image_enabled":false}')
        partial = read_preparation_policy(project)
        assert partial.image_enabled is False
        assert partial.text_recipe == DEFAULT_PREPARATION_POLICY.text_recipe
        assert partial.video_recipe == DEFAULT_PREPARATION_POLICY.video_recipe

        project.set_meta(
            PREPARATION_POLICY_META_KEY,
            '{"text_recipe":null,"video_recipe":null}',
        )
        disabled = read_preparation_policy(project)
        assert disabled.text_recipe is None
        assert disabled.video_recipe is None
        assert (
            disabled.transcript_recipe == DEFAULT_PREPARATION_POLICY.transcript_recipe
        )
    finally:
        project.close()


def test_policy_edit_during_index_claim_is_not_acknowledged_under_stale_policy(
    tmp_path, monkeypatch
) -> None:
    project = Project.create(tmp_path / "policy-edit-during-index.frisket")
    concurrent = Project(project.path)
    old_policy = PreparationPolicy(
        text_recipe=TextChunkRecipe(max_codepoints=8, overlap_codepoints=0)
    )
    current_policy = PreparationPolicy(
        text_recipe=TextChunkRecipe(max_codepoints=16, overlap_codepoints=0)
    )
    original_read_snapshot = Project.read_snapshot
    injected = False

    def read_snapshot_after_policy_edit(self):
        nonlocal injected
        if self is project and not injected:
            injected = True
            write_preparation_policy(concurrent, current_policy)
        return original_read_snapshot(self)

    try:
        sheet_id = project.add_sheet("Documents")
        column_id = project.add_column(sheet_id, "body")
        project.add_rows(
            sheet_id,
            [{"body": "alpha beta gamma delta"}],
            {"body": column_id},
        )
        write_preparation_policy(project, old_policy)
        monkeypatch.setattr(Project, "read_snapshot", read_snapshot_after_policy_edit)

        _processed, complete = search_index_job.process_index_batches(
            project, embedding_encoder=_Encoder()
        )

        assert injected and complete
        assert not index_needs_work(project)
        with RetrievalStore(project).open() as store:
            recipes = store.db.execute(
                "SELECT recipe_id,status FROM preparation WHERE channel=?",
                (TEXT_CHANNEL,),
            ).fetchall()
            assert [tuple(row) for row in recipes] == [
                (current_policy.text_recipe.recipe_id, "ready")
            ]
    finally:
        concurrent.close()
        project.close()


@pytest.mark.parametrize(
    ("payload", "knob"),
    [
        ("not-json", "retrieval_preparation_policy_v1"),
        ('{"image_enabled":"yes"}', "retrieval_preparation_policy_v1.image_enabled"),
    ],
)
def test_preparation_policy_refuses_malformed_named_knob(
    tmp_path, payload: str, knob: str
) -> None:
    project = Project.create(tmp_path / f"malformed-{abs(hash(payload))}.frisket")
    try:
        project.set_meta(PREPARATION_POLICY_META_KEY, payload)
        with pytest.raises(ValueError, match=knob.replace(".", r"\.")):
            read_preparation_policy(project)
    finally:
        project.close()


def test_stored_future_policy_keys_are_ignored_without_enabling_paid_work(
    tmp_path,
) -> None:
    project = Project.create(tmp_path / "future-preparation-policy.frisket")
    try:
        project.set_meta(
            PREPARATION_POLICY_META_KEY,
            json.dumps(
                {
                    "future_top_level": {"remote_provider": "paid"},
                    "text_recipe": {
                        "strategy": "text_codepoint_windows",
                        "max_codepoints": 256,
                        "overlap_codepoints": 32,
                        "future_boundary_mode": "semantic",
                    },
                    "video_recipe": {
                        "strategy": "future_remote_video",
                        "remote_provider": "paid",
                    },
                    "audio_recipe": {
                        "strategy": "future_remote_audio",
                        "remote_provider": "paid",
                    },
                }
            ),
        )

        policy = read_preparation_policy(project)

        assert policy.text_recipe == TextChunkRecipe(
            max_codepoints=256, overlap_codepoints=32
        )
        assert policy.video_recipe is None
        assert policy.audio_recipe is None

        sheet_id = project.add_sheet("Documents")
        column_id = project.add_column(sheet_id, "body")
        project.add_rows(sheet_id, [{"body": "future-safe"}], {"body": column_id})
        assert drain_index(project, batch_size=10) == 1
        assert not index_needs_work(project)
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
                "SELECT status,error_code,error_message FROM preparation "
                "WHERE source_id=? AND channel=?",
                (resolved.source.source_id, AUDIO_CHANNEL),
            ).fetchone()
            assert row["status"] == "unavailable"
            assert row["error_code"] == "policy_disabled"
            assert "disabled by project policy" in row["error_message"]
            assert store.requeue_unavailable() == 0
    finally:
        project.close()


def test_video_audio_opt_in_fans_out_and_policy_disable_retires_recipe(
    tmp_path,
) -> None:
    project = Project.create(tmp_path / "video-audio-policy.frisket")
    try:
        sheet_id = project.add_sheet("Media")
        column_id = project.add_column(sheet_id, "video", type="video")
        digest = project.add_blob(b"video", mime="video/mp4")
        row_id = project.add_rows(
            sheet_id,
            [{"video": {"blob": digest, "mime": "video/mp4"}}],
            {"video": column_id},
        )[0]
        with project.read_snapshot() as snapshot:
            resolved = resolve_cell_blob_source(
                snapshot,
                sheet_id=sheet_id,
                row_id=row_id,
                column_id=column_id,
            )
        assert resolved is not None
        enabled = PreparationPolicy(audio_recipe=AudioWindowRecipe())
        with RetrievalStore(project).open() as store:
            dispatch_resolved_blob_source(store, resolved, policy=enabled)
            channels = {
                row["channel"]
                for row in store.db.execute(
                    "SELECT channel FROM preparation WHERE source_id=?",
                    (resolved.source.source_id,),
                )
            }
            assert channels == {"audio", "video"}

            dispatch_resolved_blob_source(
                store, resolved, policy=DEFAULT_PREPARATION_POLICY
            )
            audio = store.db.execute(
                "SELECT recipe_id,status,error_code FROM preparation "
                "WHERE source_id=? AND channel='audio'",
                (resolved.source.source_id,),
            ).fetchone()
            assert tuple(audio) == (
                "policy-disabled-audio-v1",
                "unavailable",
                "policy_disabled",
            )
    finally:
        project.close()


def test_transcript_preparation_resumes_derived_segments(tmp_path, monkeypatch) -> None:
    project = Project.create(tmp_path / "transcript-preparation.frisket")
    sheet_id, binding = _current_binding(project)
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
        sheet_id=sheet_id,
        binding=binding,
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


def test_dirty_dispatch_prepares_real_timestamped_transcript_column(tmp_path) -> None:
    from tests.engine.test_temporal_transcript_inheritance import _seed_source

    project = Project.create(tmp_path / "typed-transcript-dispatch.frisket")
    try:
        _source, transcript_column_id, _link = _seed_source(project)

        while not index_batch(project, batch_size=10).complete:
            pass

        with RetrievalStore(project).open() as store:
            work = store.next_preparation(channel=TRANSCRIPT_CHANNEL)
            assert work is not None
            binding = store.db.execute(
                "SELECT binding.column_id FROM bindings binding "
                "JOIN sources source ON source.source_key=binding.source_key "
                "WHERE source.source_id=?",
                (work.source_revision.source_id,),
            ).fetchone()
            assert int(binding["column_id"]) == transcript_column_id

        progress = process_evidence_preparation_batch(
            project,
            channel=TRANSCRIPT_CHANNEL,
            encoder=_Encoder(),
        )
        assert progress.processed_parts == 1
        with RetrievalStore(project).open() as store:
            state = store.preparation_state(
                work.source_revision.source_id,
                recipe_id=DEFAULT_TRANSCRIPT_RECIPE.recipe_id,
                channel=TRANSCRIPT_CHANNEL,
            )
            assert state is not None and state.status == "ready"
            locator = json.loads(
                store.db.execute(
                    "SELECT part.locator_json FROM parts part JOIN sources source "
                    "ON source.source_key=part.source_key WHERE source.source_id=?",
                    (work.source_revision.source_id,),
                ).fetchone()[0]
            )
            assert locator["time"] == {"end_seconds": 6.0, "start_seconds": 1.0}
            assert len(locator["source_span_ids"]) == 2
    finally:
        project.close()


def test_ocr_preparation_keeps_authoritative_span_ids(tmp_path, monkeypatch) -> None:
    project = Project.create(tmp_path / "ocr-preparation.frisket")
    sheet_id, binding = _current_binding(project)
    text = "page one words"
    source = SourceRevision(
        "ocr:evidence",
        "ocr_text",
        "evidence_snapshot",
        "sha256:snapshot",
        _text_hash(text),
    )
    resolved = ResolvedCellOcrTextSource(
        sheet_id=sheet_id,
        binding=binding,
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
                "SELECT part.locator_json FROM parts part JOIN sources source "
                "ON source.source_key=part.source_key WHERE source.source_id=?",
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
                "ON source.source_key=binding.source_key "
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


def test_repeated_chunks_share_one_representation_with_distinct_locations(
    tmp_path,
) -> None:
    project = Project.create(tmp_path / "repeated-chunks.frisket")
    recipe = TextChunkRecipe(max_codepoints=8, overlap_codepoints=0)
    space = EmbeddingGemma2Space(dimensions=128)
    encoder = _Encoder()
    try:
        sheet_id = project.add_sheet("Documents")
        column_id = project.add_column(sheet_id, "body")
        row_id = project.add_rows(
            sheet_id,
            [{"body": "repeat\n\nrepeat\n\n"}],
            {"body": column_id},
        )[0]
        resolved = _source(project, sheet_id, column_id, row_id)
        with RetrievalStore(project).open() as store:
            dispatch_resolved_text_source(store, resolved, recipe=recipe, space=space)

        progress = process_text_preparation_batch(
            project,
            encoder=encoder,
            recipe=recipe,
            space=space,
        )

        assert progress.complete and progress.processed_parts == 2
        assert encoder.calls == 1
        with RetrievalStore(project).open() as store:
            assert store.db.execute("SELECT count(*) FROM parts").fetchone()[0] == 2
            assert (
                store.db.execute("SELECT count(*) FROM representations").fetchone()[0]
                == 1
            )
            assert (
                store.db.execute(
                    "SELECT count(*) FROM part_representations"
                ).fetchone()[0]
                == 2
            )
    finally:
        project.close()


def test_invalid_image_does_not_block_later_blank_pdf_in_worker_quantum(
    tmp_path,
) -> None:
    project = Project.create(tmp_path / "isolated-media-failure.frisket")
    encoder = _RejectInvalidImageEncoder()
    try:
        sheet_id = project.add_sheet("Documents")
        image_column = project.add_column(sheet_id, "image", type="image")
        pdf_column = project.add_column(sheet_id, "document", type="file")
        image_hash = project.add_blob(b"<svg>invalid</svg>", mime="image/svg+xml")
        pdf_hash = project.add_blob(_three_blank_page_pdf(), mime="application/pdf")
        image_row, pdf_row = project.add_rows(
            sheet_id,
            [
                {"image": {"blob": image_hash, "mime": "image/svg+xml"}},
                {"document": {"blob": pdf_hash, "mime": "application/pdf"}},
            ],
            {"image": image_column, "document": pdf_column},
        )

        processed, complete = search_index_job.process_index_batches(
            project, embedding_encoder=encoder
        )

        assert complete and processed >= 5
        assert encoder.batch_sizes == [1, 1]
        with RetrievalStore(project).open() as store:
            failures = store.db.execute(
                "SELECT binding.row_id,work.status,work.error_code "
                "FROM preparation work JOIN sources source "
                "ON source.source_id=work.source_id "
                "AND source.version_kind=work.target_version_kind "
                "AND source.version_value=work.target_version_value "
                "AND source.version_type=work.target_version_type "
                "JOIN bindings binding ON binding.source_key=source.source_key "
                "WHERE work.channel=?",
                (IMAGE_CHANNEL,),
            ).fetchall()
            assert [tuple(row) for row in failures] == [
                (image_row, "failed", "invalid_input")
            ]
            pdf_state = store.db.execute(
                "SELECT binding.row_id,work.status,work.error_code "
                "FROM preparation work JOIN sources source "
                "ON source.source_id=work.source_id "
                "AND source.version_kind=work.target_version_kind "
                "AND source.version_value=work.target_version_value "
                "AND source.version_type=work.target_version_type "
                "JOIN bindings binding ON binding.source_key=source.source_key "
                "WHERE work.channel=?",
                (PDF_PAGE_CHANNEL,),
            ).fetchone()
            assert tuple(pdf_state) == (pdf_row, "ready", None)
            assert store.db.execute("SELECT count(*) FROM parts").fetchone()[0] == 3
            assert (
                store.db.execute("SELECT count(*) FROM representations").fetchone()[0]
                == 1
            )
            assert (
                store.db.execute(
                    "SELECT count(*) FROM part_representations"
                ).fetchone()[0]
                == 3
            )
    finally:
        project.close()


def test_lexical_drain_leaves_semantic_preparation_pending(tmp_path) -> None:
    project = Project.create(tmp_path / "lexical-only.frisket")
    try:
        sheet_id = project.add_sheet("Documents")
        column_id = project.add_column(sheet_id, "body")
        project.add_rows(sheet_id, [{"body": "searchable"}], {"body": column_id})

        assert drain_index(project) == 1

        with RetrievalStore(project).open() as store:
            state = store.db.execute(
                "SELECT status,error_code FROM preparation WHERE channel=?",
                (TEXT_CHANNEL,),
            ).fetchone()
            assert tuple(state) == ("pending", None)
        assert preparation_work_marker(project.path) is not None
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


def test_index_claim_runs_one_bounded_preparation_pass(monkeypatch) -> None:
    calls: list[str] = []

    class ProjectStub:
        path = None

    monkeypatch.setattr(
        search_index_job,
        "index_batch",
        lambda *_args, **_kwargs: IndexProgress(0, False, True),
    )
    monkeypatch.setattr(search_index_job, "preparation_work_marker", lambda *_: "work")
    monkeypatch.setattr(
        search_index_job,
        "read_preparation_policy",
        lambda *_: DEFAULT_PREPARATION_POLICY,
    )
    monkeypatch.setattr(
        search_index_job,
        "process_text_preparation_batch",
        lambda *_args, **_kwargs: (
            calls.append("text") or PreparationProgress(1, True, False)
        ),
    )
    monkeypatch.setattr(
        search_index_job,
        "process_evidence_preparation_batch",
        lambda *_args, channel, **_kwargs: (
            calls.append(channel) or PreparationProgress(1, True, False)
        ),
    )
    monkeypatch.setattr(
        search_index_job,
        "process_blob_image_preparation_batch",
        lambda *_args, channel, **_kwargs: (
            calls.append(channel) or PreparationProgress(1, True, False)
        ),
    )
    monkeypatch.setattr(
        search_index_job,
        "process_timed_media_preparation_batch",
        lambda *_args, channel, **_kwargs: (
            calls.append(channel) or PreparationProgress(1, True, False)
        ),
    )

    processed, complete = search_index_job.process_index_batches(
        ProjectStub(),
        embedding_encoder=_Encoder(),
    )

    assert processed == 7
    assert not complete
    assert calls == [
        "text",
        TRANSCRIPT_CHANNEL,
        OCR_TEXT_CHANNEL,
        "image",
        PDF_PAGE_CHANNEL,
        "video",
        AUDIO_CHANNEL,
    ]
