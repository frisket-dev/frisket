from __future__ import annotations

import io
import json

import pytest
from dataclasses import replace

from pypdf import PdfReader, PdfWriter

from frisket.engine.store.document_text import (
    ensure_document_text_role,
    resolve_document_text_selection,
)
from frisket.engine.store.evidence import record_source_artifact
from frisket.engine.store.pdf_split import PdfSplitSource, PdfSplitWriter
from frisket.engine.store.pdf_split_text import (
    capture_pdf_split_text_identities,
    publish_pdf_split_page,
)
from frisket.engine.store.prepared_content import (
    PreparedContentStore,
    PreparedPageDraft,
)
from frisket.server.services.pdf_split_preparation import PdfSplitChildBinding
from tests.server.test_pdf_split_preparation import _runtime


def _child(tmp_path, monkeypatch):
    adapter, preview, project, provider, read, _op, source = _runtime(
        tmp_path, monkeypatch
    )
    with source.open_source() as path:
        reader = PdfReader(path)
        writer = PdfWriter()
        for page in reader.pages[:2]:
            writer.add_page(page)
        output = io.BytesIO()
        writer.write(output)
    digest = project.blob_store.put(output.getvalue())
    value = {"blob": digest, "filename": "child.pdf", "mime": "application/pdf"}
    writer = PdfSplitWriter.start(
        project,
        session_id="split-text",
        writer_authority="text-generation",
        project_id="pages",
        idempotency_key="split-text",
        params_hash="split-text",
        action_id="split-text",
        receipt_id="split-text-receipt",
        columns=[{"name": "media", "type": "file"}, {"name": "text", "type": "text"}],
        sources=[PdfSplitSource("first", 1)],
    )
    rows = writer.publish_source([{"media": value, "text": ""}], expected_cursor=0)
    columns = {
        column["name"]: int(column["id"]) for column in project.columns(writer.sheet_id)
    }
    session = writer.session
    writer.finalize(expected_cursor=1)
    artifact = record_source_artifact(
        project,
        artifact_kind="file",
        media_type="application/pdf",
        blob_hash=digest,
        filename="child.pdf",
        page_count=2,
        source_sheet_id=session.sheet_id,
        source_row_id=rows[0],
        source_column_id=columns["media"],
    )
    ensure_document_text_role(
        project,
        source_column_id=columns["media"],
        text_column_id=columns["text"],
        commit=True,
    )
    source_identity, text_identity = capture_pdf_split_text_identities(
        project,
        row_id=rows[0],
        source_column_id=columns["media"],
        text_column_id=columns["text"],
        original_artifact_id=read.source_artifact_id,
        original_page_number=1,
    )
    binding = PdfSplitChildBinding(
        session.op_id,
        session.writer_authority,
        session.id,
        "first",
        read.work_key,
        rows[0],
        int(artifact["id"]),
        columns["media"],
        columns["text"],
        1,
        source_identity,
        text_identity,
    )
    refs = []
    with project.db:
        project.db.execute("BEGIN IMMEDIATE")
        for page in [1, 2]:
            refs.append(
                PreparedContentStore(project)
                .stage_reference(
                    source_artifact_id=read.source_artifact_id,
                    producing_op_id=session.op_id,
                    pages=[
                        PreparedPageDraft(
                            page,
                            f"Page {page}",
                            {"blocks": [], "width": 72, "height": 72},
                        )
                    ],
                    page_number=page,
                )
                .ref_id
            )
    return adapter, preview, project, binding, refs


def test_finished_pages_merge_partial_refs_and_one_undo_restores_saved_text(
    tmp_path, monkeypatch
):
    adapter, preview, project, binding, refs = _child(tmp_path, monkeypatch)
    try:
        before = project.op_cursor
        assert publish_pdf_split_page(project, binding, refs[0])
        first = resolve_document_text_selection(
            project,
            (
                project.db.execute(
                    "SELECT sheet_id FROM rows WHERE id=?", (binding.row_id,)
                ).fetchone()[0],
                binding.row_id,
                binding.source_column_id,
            ),
        )
        content = PreparedContentStore(project).resolve(first.prepared_ref_id)
        assert content.covered_pages == (1,)
        assert content.complete is False
        assert content.text == "Page 1"
        second = replace(
            binding,
            page_number=2,
            expected_source_value={
                **binding.expected_source_value,
                "original_page_number": 2,
            },
        )
        assert publish_pdf_split_page(project, second, refs[1])
        selection = resolve_document_text_selection(
            project, (first.source_cell[0], binding.row_id, binding.source_column_id)
        )
        content = PreparedContentStore(project).resolve(selection.prepared_ref_id)
        assert content.covered_pages == (1, 2)
        assert content.complete is True
        assert content.text == "Page 1\n\nPage 2"
        assert content.pins[1].positions["source_page_number"] == 2
        assert content.pins[1].positions["page_number"] == 2
        assert project.op_cursor == before
        assert (
            project.db.execute("SELECT COUNT(*) FROM search_dirty_scopes").fetchone()[0]
            > 0
        )
        project.undo()
        assert project.visible_row_ids(first.source_cell[0]) == []
        project.redo()
        restored = resolve_document_text_selection(project, first.source_cell)
        assert (
            PreparedContentStore(project).resolve(restored.prepared_ref_id).text
            == content.text
        )
        assert not publish_pdf_split_page(project, binding, refs[0])
    finally:
        preview._registry.shutdown()
        project.close()


@pytest.mark.parametrize("source_edit", [False, True])
def test_user_text_edit_wins_even_when_equal_to_the_pending_base(
    tmp_path, monkeypatch, source_edit
):
    _adapter, preview, project, binding, refs = _child(tmp_path, monkeypatch)
    try:
        project.apply_edits(
            [
                {
                    "row_id": binding.row_id,
                    "column_id": binding.source_column_id
                    if source_edit
                    else binding.text_column_id,
                    "value": json.loads(binding.expected_source_value["stored_value"])
                    if source_edit
                    else "",
                }
            ]
        )
        assert not publish_pdf_split_page(project, binding, refs[0])
        assert (
            project.db.execute(
                "SELECT value_kind FROM cells WHERE row_id=? AND column_id=?",
                (binding.row_id, binding.text_column_id),
            ).fetchone()[0]
            == "text"
        )
    finally:
        preview._registry.shutdown()
        project.close()


def test_concurrent_page_completions_merge_against_current_immutable_set(
    tmp_path, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    _adapter, preview, project, binding, refs = _child(tmp_path, monkeypatch)
    second = replace(
        binding,
        page_number=2,
        expected_source_value={
            **binding.expected_source_value,
            "original_page_number": 2,
        },
    )
    barrier = threading.Barrier(2)

    def publish(item):
        target, ref = item
        barrier.wait(timeout=5)
        return publish_pdf_split_page(project, target, ref)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(publish, [(binding, refs[0]), (second, refs[1])])) == [
                True,
                True,
            ]
        sheet = project.db.execute(
            "SELECT sheet_id FROM rows WHERE id=?", (binding.row_id,)
        ).fetchone()[0]
        selection = resolve_document_text_selection(
            project, (sheet, binding.row_id, binding.source_column_id)
        )
        prepared = PreparedContentStore(project).resolve(selection.prepared_ref_id)
        assert prepared.covered_pages == (1, 2)
        assert prepared.text == "Page 1\n\nPage 2"
    finally:
        preview._registry.shutdown()
        project.close()


def test_merged_original_cache_is_narrowed_before_real_child_fanout(
    tmp_path, monkeypatch
):
    from frisket.server.services.pdf_split_preparation import (
        PdfSplitPageTarget,
        PdfSplitPreparationStore,
    )

    adapter, preview, project, binding, _refs = _child(tmp_path, monkeypatch)
    artifact = int(binding.expected_source_value["original_artifact_id"])
    digest = project.db.execute(
        "SELECT blob_hash FROM source_artifacts WHERE id=?", (artifact,)
    ).fetchone()[0]
    target = PdfSplitPageTarget(
        "first",
        artifact,
        digest,
        None,
        2,
        {"engine": "anthropic/claude-haiku-4-5", "keep_text_position": False},
    )
    queued = replace(
        binding,
        work_key=target.work_key,
        page_number=2,
        expected_source_value={
            **binding.expected_source_value,
            "original_page_number": 2,
        },
    )
    try:
        quoted = adapter.quote(targets=[target], plan_token="late-original-cache")
        adapter.approve(quoted, confirmation=quoted["estimate"]["promise_set_hash"])
        with PdfSplitPreparationStore(adapter.inventory_path) as store:
            store.admit(
                operation_id=binding.operation_id,
                generation=binding.generation,
                session_id=binding.session_id,
                targets=[target],
                bindings=[queued],
                approved_plan_token="late-original-cache",
            )
        with project.transaction_scope():
            merged = PreparedContentStore(project).stage_reference(
                source_artifact_id=artifact,
                producing_op_id=1,
                pages=[
                    PreparedPageDraft(
                        page,
                        f"Original {page}",
                        {
                            "engine": target.ocr_action_params["engine"],
                            "ocr_action_params": dict(target.ocr_action_params),
                        },
                    )
                    for page in (1, 2)
                ],
                allow_partial=True,
            )
        assert len(PreparedContentStore(project).resolve(merged.ref_id).pins) == 2
        versions = project.db.execute(
            "SELECT COUNT(*) FROM prepared_page_versions WHERE source_artifact_id=?",
            (artifact,),
        ).fetchone()[0]
        adapter.publish_child_page = lambda current, ref: publish_pdf_split_page(
            project, current, ref
        )
        assert adapter.start_pending(generation=binding.generation) == ()
        sheet = project.db.execute(
            "SELECT sheet_id FROM rows WHERE id=?", (binding.row_id,)
        ).fetchone()[0]
        selection = resolve_document_text_selection(
            project, (sheet, binding.row_id, binding.source_column_id)
        )
        child = PreparedContentStore(project).resolve(selection.prepared_ref_id)
        assert child.text == "Original 2"
        assert child.covered_pages == (2,)
        with PdfSplitPreparationStore(adapter.inventory_path) as store:
            selected = PreparedContentStore(project).resolve(store.cached_ref(target))
            assert selected.covered_pages == (2,)
            assert len(selected.pins) == 1
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM prepared_page_versions WHERE source_artifact_id=?",
                (artifact,),
            ).fetchone()[0]
            == versions
        )
        assert project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
    finally:
        preview._registry.shutdown()
        project.close()
