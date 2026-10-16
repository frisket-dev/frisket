from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from frisket.engine.store import Project
from frisket.engine.store.evidence import record_source_artifact
from frisket.engine.store.prepared_content import (
    PreparedContentStore,
    PreparedPageDraft,
)
from frisket.retrieval.content import PreparedDocumentSurface, TextSurface
from frisket.retrieval.contracts import (
    ScopedBinding,
    SourceLocator,
    SourceRevision,
    TextRange,
)
from frisket.retrieval.source_adapter import (
    UnsupportedLexicalSourceError,
    resolve_cell_blob_source,
    resolve_cell_text_sources,
    resolve_source_text,
)
from frisket.retrieval.sources import ocr_source_revision, transcript_source_revision


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_resolves_current_blob_metadata_and_authored_page(tmp_path) -> None:
    project = Project.create(tmp_path / "blob.frisket")
    try:
        sheet_id = project.add_sheet("Documents")
        column_id = project.add_column(sheet_id, "document", type="file")
        digest = project.add_blob(
            b"pdf fixture",
            filename="report.pdf",
            mime="application/pdf",
        )
        row_id = project.add_rows(
            sheet_id,
            [
                {
                    "document": {
                        "blob": digest,
                        "filename": "client-name.pdf",
                        "mime": "application/octet-stream",
                        "page": 3,
                    }
                }
            ],
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
        assert resolved.sheet_id == sheet_id
        assert resolved.binding.row_id == row_id
        assert resolved.binding.column_id == column_id
        assert resolved.binding.retrieval_revision > 0
        assert resolved.blob_hash == digest
        assert resolved.mime_type == "application/pdf"
        assert resolved.page_number == 3
        assert resolved.source.source_id == f"blob:{digest}"
        assert resolved.source.source_kind == "blob"
        assert resolved.source.version_kind == "blob_digest"
        assert resolved.source.version_value == digest
        assert resolved.source.content_hash == f"sha256:{digest}"
    finally:
        project.close()


def test_blob_resolution_rejects_unowned_or_invalid_media_refs(tmp_path) -> None:
    project = Project.create(tmp_path / "invalid-blob.frisket")
    try:
        sheet_id = project.add_sheet("Media")
        image_column = project.add_column(sheet_id, "image", type="image")
        file_column = project.add_column(sheet_id, "document", type="file")
        no_mime = project.add_blob(b"no mime")
        image = project.add_blob(b"image", mime="image/png")
        rows = project.add_rows(
            sheet_id,
            [
                {
                    "image": {"blob": no_mime, "mime": "image/png"},
                    "document": {"blob": "a" * 64, "mime": "application/pdf"},
                },
                {
                    "image": {"blob": image, "mime": "image/png", "page": 1},
                    "document": None,
                },
            ],
            {"image": image_column, "document": file_column},
        )

        with project.read_snapshot() as snapshot:
            assert (
                resolve_cell_blob_source(
                    snapshot,
                    sheet_id=sheet_id,
                    row_id=rows[0],
                    column_id=image_column,
                )
                is None
            )
            assert (
                resolve_cell_blob_source(
                    snapshot,
                    sheet_id=sheet_id,
                    row_id=rows[0],
                    column_id=file_column,
                )
                is None
            )
            assert (
                resolve_cell_blob_source(
                    snapshot,
                    sheet_id=sheet_id,
                    row_id=rows[1],
                    column_id=image_column,
                )
                is None
            )
    finally:
        project.close()


def test_resolves_plain_text_with_exact_binding_revision(tmp_path) -> None:
    project = Project.create(tmp_path / "plain.frisket")
    try:
        sheet_id = project.add_sheet("Documents")
        column_id = project.add_column(sheet_id, "body")
        row_id = project.add_rows(
            sheet_id, [{"body": "alpha beta"}], {"body": column_id}
        )[0]

        with project.read_snapshot() as snapshot:
            [resolved] = resolve_cell_text_sources(
                snapshot,
                sheet_id=sheet_id,
                column_id=column_id,
                row_ids=[row_id],
            ).values()
            binding = ScopedBinding(
                "binding:plain",
                sheet_id,
                row_id,
                column_id,
                (resolved.binding,),
            )
            locator = SourceLocator(
                "text",
                text=TextRange(
                    resolved.surface.text_surface_id,
                    resolved.surface.content_hash,
                    0,
                    len(resolved.surface.text),
                ),
            )
            assert (
                resolve_source_text(
                    snapshot,
                    source=resolved.source,
                    locator=locator,
                    binding=binding,
                )
                == "alpha beta"
            )
            assert (
                resolve_source_text(
                    snapshot,
                    source=replace(resolved.source, version_value=999),
                    locator=locator,
                    binding=binding,
                )
                is None
            )

        assert resolved.binding.row_id == row_id
        assert resolved.binding.column_id == column_id
        assert resolved.binding.retrieval_revision == resolved.source.version_value
        assert resolved.source.source_id == f"cell:{row_id}:{column_id}"
        assert resolved.source.source_kind == "text"
        assert resolved.source.version_kind == "retrieval_revision"
        assert resolved.source.content_hash == _sha256("alpha beta")
        assert resolved.lexical_text == "alpha beta"
        assert isinstance(resolved.surface, TextSurface)
        assert resolved.surface.text_surface_id == f"cell:{row_id}:{column_id}"
    finally:
        project.close()


def test_prepared_selector_uses_its_ordered_pins_not_newer_set(tmp_path) -> None:
    project = Project.create(tmp_path / "prepared.frisket")
    try:
        sheet_id = project.add_sheet("Documents")
        column_id = project.add_column(sheet_id, "body")
        project.db.execute("BEGIN IMMEDIATE")
        op_id = project.append_op("prepare.test", commit=False)
        artifact = record_source_artifact(
            project,
            artifact_kind="document",
            media_type="application/pdf",
            page_count=2,
        )
        store = PreparedContentStore(project)
        selected = store.stage_reference(
            source_artifact_id=int(artifact["id"]),
            producing_op_id=op_id,
            pages=(
                PreparedPageDraft(1, "selected one"),
                PreparedPageDraft(2, "selected two"),
            ),
        )
        newer = store.stage_reference(
            source_artifact_id=int(artifact["id"]),
            producing_op_id=op_id,
            pages=(
                PreparedPageDraft(1, "newer one"),
                PreparedPageDraft(2, "newer two"),
            ),
        )
        row_id = project.add_rows(
            sheet_id, [{"body": selected}], {"body": column_id}, commit=False
        )[0]
        project.db.commit()

        with project.read_snapshot() as snapshot:
            resolved = resolve_cell_text_sources(
                snapshot,
                sheet_id=sheet_id,
                column_id=column_id,
                row_ids=[row_id],
            )[row_id]
            binding = ScopedBinding(
                "binding:prepared",
                sheet_id,
                row_id,
                column_id,
                (resolved.binding,),
            )
            assert (
                resolve_source_text(
                    snapshot,
                    source=resolved.source,
                    locator=SourceLocator("prepared_text"),
                    binding=binding,
                )
                == "selected one\n\nselected two"
            )

        selected_text = store.resolve(selected.ref_id)
        assert resolved.lexical_text == "selected one\n\nselected two"
        assert newer.ref_id != selected.ref_id
        assert resolved.source.source_id == f"prepared-ref:{selected.ref_id}"
        assert resolved.source.source_kind == "prepared_text"
        assert resolved.source.version_kind == "prepared_page_versions"
        assert isinstance(resolved.surface, PreparedDocumentSurface)
        assert [page.version_id for page in resolved.surface.pages] == [
            pin.version_id for pin in selected_text.pins
        ]
        assert [page.page_number for page in resolved.surface.pages] == [1, 2]
    finally:
        project.close()


def test_snapshot_resolution_stays_old_after_concurrent_edit(tmp_path) -> None:
    project = Project.create(tmp_path / "edit.frisket")
    try:
        sheet_id = project.add_sheet("Documents")
        column_id = project.add_column(sheet_id, "body")
        row_id = project.add_rows(sheet_id, [{"body": "before"}], {"body": column_id})[
            0
        ]

        with project.read_snapshot() as snapshot:
            before = resolve_cell_text_sources(
                snapshot,
                sheet_id=sheet_id,
                column_id=column_id,
                row_ids=[row_id],
            )[row_id]
            project.apply_edits(
                [{"row_id": row_id, "column_id": column_id, "value": "after"}]
            )
            still_before = resolve_cell_text_sources(
                snapshot,
                sheet_id=sheet_id,
                column_id=column_id,
                row_ids=[row_id],
            )[row_id]

        with project.read_snapshot() as snapshot:
            after = resolve_cell_text_sources(
                snapshot,
                sheet_id=sheet_id,
                column_id=column_id,
                row_ids=[row_id],
            )[row_id]

        assert still_before == before
        assert after.lexical_text == "after"
        assert after.binding.retrieval_revision > before.binding.retrieval_revision
        assert after.source != before.source
    finally:
        project.close()


def test_batch_omits_blank_missing_and_nonsearchable_cells(tmp_path) -> None:
    project = Project.create(tmp_path / "eligibility.frisket")
    try:
        sheet_id = project.add_sheet("Documents")
        text_id = project.add_column(sheet_id, "body")
        number_id = project.add_column(sheet_id, "amount", type="number")
        rows = project.add_rows(
            sheet_id,
            [{"body": "kept", "amount": 7}, {"body": "   ", "amount": 8}],
            {"body": text_id, "amount": number_id},
        )

        with project.read_snapshot() as snapshot:
            assert list(
                resolve_cell_text_sources(
                    snapshot,
                    sheet_id=sheet_id,
                    column_id=text_id,
                    row_ids=[*rows, rows[-1] + 999],
                )
            ) == [rows[0]]
            assert (
                resolve_cell_text_sources(
                    snapshot,
                    sheet_id=sheet_id,
                    column_id=number_id,
                    row_ids=rows,
                )
                == {}
            )
    finally:
        project.close()


def test_unknown_lexical_source_kind_fails_explicitly(tmp_path) -> None:
    project = Project.create(tmp_path / "unsupported.frisket")
    try:
        with project.read_snapshot() as snapshot:
            with pytest.raises(UnsupportedLexicalSourceError, match="image"):
                resolve_source_text(
                    snapshot,
                    source=SourceRevision(
                        "blob:one", "image", "blob_digest", "sha256:one"
                    ),
                    locator=SourceLocator("image"),
                    binding=ScopedBinding("binding:image", 1, 1),
                )
    finally:
        project.close()


def test_transcript_lexical_text_requires_exact_snapshot_and_ordered_spans(
    monkeypatch,
) -> None:
    from frisket.engine.executor import temporal_transcripts

    resolved = SimpleNamespace(
        evidence_link_stable_id="evidence-link:transcript",
        snapshot_hash="sha256:transcript-snapshot",
        transcript_value="first segment\nsecond segment",
        spans=(
            {"stable_id": "span:first"},
            {"stable_id": "span:second"},
        ),
    )
    monkeypatch.setattr(
        temporal_transcripts,
        "resolve_timestamped_transcript",
        lambda *_args, **_kwargs: resolved,
    )
    source = transcript_source_revision(
        evidence_link_stable_id=resolved.evidence_link_stable_id,
        snapshot_hash=resolved.snapshot_hash,
        text=resolved.transcript_value,
    )
    binding = ScopedBinding("binding:transcript", 2, 3, 4)
    locator = SourceLocator(
        "transcript",
        source_span_ids=("span:first", "span:second"),
    )

    assert (
        resolve_source_text(object(), source=source, locator=locator, binding=binding)
        == resolved.transcript_value
    )
    assert (
        resolve_source_text(
            object(),
            source=source,
            locator=replace(locator, source_span_ids=("span:second",)),
            binding=binding,
        )
        == resolved.transcript_value
    )
    assert (
        resolve_source_text(
            object(),
            source=source,
            locator=replace(locator, source_span_ids=("span:second", "span:first")),
            binding=binding,
        )
        is None
    )


def test_ocr_lexical_text_requires_exact_current_evidence_link(monkeypatch) -> None:
    from frisket.engine.store import ocr_word_stream

    resolved = SimpleNamespace(
        evidence_link_stable_id="evidence-link:ocr",
        snapshot_hash="sha256:ocr-snapshot",
        text_value="recognized page text",
        spans=({"stable_id": "span:page"},),
    )
    monkeypatch.setattr(
        ocr_word_stream,
        "resolve_current_ocr_evidence",
        lambda *_args, **_kwargs: (resolved,),
    )
    source = ocr_source_revision(
        evidence_link_stable_id=resolved.evidence_link_stable_id,
        snapshot_hash=resolved.snapshot_hash,
        text=resolved.text_value,
    )
    binding = ScopedBinding("binding:ocr", 2, 3, 4)
    locator = SourceLocator("ocr", source_span_ids=("span:page",))

    assert (
        resolve_source_text(object(), source=source, locator=locator, binding=binding)
        == resolved.text_value
    )
    assert (
        resolve_source_text(
            object(),
            source=replace(source, version_value="sha256:stale-snapshot"),
            locator=locator,
            binding=binding,
        )
        is None
    )
