from __future__ import annotations

import pytest

from frisket.server.services.pdf_packet_ocr import PdfPacketOcrCoordinator
from frisket.server.services.pdf_packet_sources import AdmittedPacketSource
from tests.server.test_pdf_split_preparation import _runtime


def _quote_fixture(tmp_path, monkeypatch):
    adapter, preview, project, provider, read, _op, source = _runtime(
        tmp_path, monkeypatch
    )
    admitted = AdmittedPacketSource(
        "first", read.blob_hash, "original.pdf", 1, 3, 2, 2, source.open_source
    )
    coordinator = PdfPacketOcrCoordinator(
        adapter.workspace,
        preview_runs=preview,
        inventory_path=tmp_path / "review" / "inventory.db",
    )
    coordinator.inventory_path.parent.mkdir()
    return coordinator, preview, project, provider, admitted


def test_sheet_quote_uses_actual_selected_missing_page_and_exact_approval(
    tmp_path, monkeypatch
):
    coordinator, preview, project, provider, source = _quote_fixture(
        tmp_path, monkeypatch
    )
    selection = {"kind": "engine", "engine": "anthropic/claude-haiku-4-5"}
    try:
        holder = coordinator.quote(
            project_id="pages",
            sources=[source],
            revision=1,
            ocr_policy="missing",
            ocr_selection=selection,
            page_text={("first", 2): ""},
        )
        assert holder.public["pages"] == 1
        with pytest.raises(ValueError, match="exact claims"):
            coordinator.approve(
                holder,
                project_id="pages",
                sources=[source],
                revision=1,
                ocr_policy="missing",
                ocr_selection=selection,
                confirmation=None,
            )
        facts = coordinator.approve(
            holder,
            project_id="pages",
            sources=[source],
            revision=1,
            ocr_policy="missing",
            ocr_selection=selection,
            confirmation=holder.public["estimate"]["promise_set_hash"],
        )
        assert facts["kind"] == "pdf_split_source_pages"
        assert facts["ocr_authorization"]["scope"]["targets"][0]["page_number"] == 2
        with pytest.raises(ValueError, match="scope, OCR policy or engine changed"):
            coordinator.approve(
                holder,
                project_id="pages",
                sources=[source],
                revision=2,
                ocr_policy="missing",
                ocr_selection=selection,
                confirmation=None,
            )
        assert provider.calls == 0
    finally:
        preview._registry.shutdown()
        project.close()


def test_empty_accepted_ocr_and_none_have_no_paid_work(tmp_path, monkeypatch):
    coordinator, preview, project, provider, source = _quote_fixture(
        tmp_path, monkeypatch
    )
    selection = {"kind": "engine", "engine": "anthropic/claude-haiku-4-5"}
    try:
        holder = coordinator.quote(
            project_id="pages",
            sources=[source],
            revision=1,
            ocr_policy="missing",
            ocr_selection=selection,
            page_text={("first", 2): ""},
            accepted_ocr_pages=[("first", 2)],
        )
        assert holder.public["pages"] == 0
        assert (
            coordinator.approve(
                None,
                project_id="pages",
                sources=[source],
                revision=1,
                ocr_policy="none",
                ocr_selection=selection,
                confirmation=None,
            )
            is None
        )
        assert provider.calls == 0
    finally:
        preview._registry.shutdown()
        project.close()


def test_manual_flat_text_does_not_prevent_one_native_read_or_invent_page_text(
    tmp_path, monkeypatch
):
    coordinator, preview, project, provider, source = _quote_fixture(
        tmp_path, monkeypatch
    )
    selection = {"kind": "engine", "engine": "anthropic/claude-haiku-4-5"}
    try:
        holder = coordinator.quote(
            project_id="pages",
            sources=[source],
            revision=1,
            ocr_policy="missing",
            ocr_selection=selection,
            page_text={},
            manual_text_sources=["first"],
        )
        path = holder.native_files["first"][2]
        assert path.is_file()
        holder2 = coordinator.quote(
            project_id="pages",
            sources=[source],
            revision=1,
            ocr_policy="missing",
            ocr_selection=selection,
            page_text={},
            manual_text_sources=["first"],
        )
        assert holder2.native_files == holder.native_files
        saved = holder.snapshot(coordinator.inventory_path.parent)
        assert (
            type(holder).restore(saved, coordinator.inventory_path.parent).native_files
            == holder.native_files
        )
        assert holder2.public["pages"] == 1
        assert provider.calls == 0
    finally:
        preview._registry.shutdown()
        project.close()


def test_native_quote_reads_contiguous_pages_in_batches_and_hands_off_saved_pages(
    tmp_path, monkeypatch
):
    from dataclasses import replace
    from frisket.server.services import pdf_packet_ocr

    coordinator, preview, project, provider, source = _quote_fixture(
        tmp_path, monkeypatch
    )
    source = replace(source, page_start=1, page_end=3)
    original = pdf_packet_ocr.extract_pdf_text_batch
    calls = []

    async def observed(*args, **kwargs):
        calls.append((kwargs["start_page"], kwargs["max_pages"]))
        return await original(*args, **kwargs)

    monkeypatch.setattr(pdf_packet_ocr, "extract_pdf_text_batch", observed)
    try:
        holder = coordinator.quote(
            project_id="pages",
            sources=[source],
            revision=1,
            ocr_policy="missing",
            ocr_selection={"kind": "engine", "engine": "anthropic/claude-haiku-4-5"},
            page_text={},
        )
        assert len(calls) < 3
        assert sum(count for _start, count in calls) == 3
        assert set(holder.native_files["first"]) == {1, 2, 3}
        assert all(path.is_file() for path in holder.native_files["first"].values())
        coordinator.quote(
            project_id="pages",
            sources=[source],
            revision=1,
            ocr_policy="missing",
            ocr_selection={"kind": "engine", "engine": "anthropic/claude-haiku-4-5"},
            page_text={},
        )
        assert sum(count for _start, count in calls) == 3
        assert provider.calls == 0
    finally:
        preview._registry.shutdown()
        project.close()


def test_quote_cancellation_stops_before_native_or_paid_work(tmp_path, monkeypatch):
    coordinator, preview, project, provider, source = _quote_fixture(
        tmp_path, monkeypatch
    )
    try:
        with pytest.raises(InterruptedError, match="cancelled"):
            coordinator.quote(
                project_id="pages",
                sources=[source],
                revision=1,
                ocr_policy="missing",
                ocr_selection={
                    "kind": "engine",
                    "engine": "anthropic/claude-haiku-4-5",
                },
                page_text={},
                should_cancel=lambda: True,
            )
        assert provider.calls == 0
    finally:
        preview._registry.shutdown()
        project.close()


def test_image_page_quote_preserves_mime_and_does_not_run_pdf_native_reader(
    tmp_path, monkeypatch
):
    import io
    from contextlib import contextmanager
    from PIL import Image

    coordinator, preview, project, provider, _source = _quote_fixture(
        tmp_path, monkeypatch
    )
    data = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(data, format="PNG")
    raw = data.getvalue()
    digest = project.blob_store.put(raw)

    @contextmanager
    def opened():
        with project.blob_store.materialize(digest) as path:
            yield path

    source = AdmittedPacketSource(
        "image", digest, "image.png", len(raw), 1, 1, 1, opened, mime="image/png"
    )
    try:
        holder = coordinator.quote(
            project_id="pages",
            sources=[source],
            revision=1,
            ocr_policy="missing",
            ocr_selection={"kind": "engine", "engine": "anthropic/claude-haiku-4-5"},
            page_text={},
        )
        assert holder.public["pages"] == 1
        assert holder.native_files == {}
        assert provider.calls == 0
    finally:
        preview._registry.shutdown()
        project.close()


def test_cached_reduction_keeps_old_confirmation_and_transfers_only_remaining_work(
    tmp_path, monkeypatch
):
    from dataclasses import replace
    from frisket.server.services.pdf_imports.inventory import PdfDraftInventory
    from frisket.server.services.pdf_imports.previews import preview_settings

    coordinator, preview, project, provider, source = _quote_fixture(
        tmp_path, monkeypatch
    )
    source = replace(source, page_start=2, page_end=3)
    selection = {"kind": "engine", "engine": "anthropic/claude-haiku-4-5"}
    params = {"engine": selection["engine"]}
    try:
        old = coordinator.quote(
            project_id="pages",
            sources=[source],
            revision=1,
            ocr_policy="missing",
            ocr_selection=selection,
            page_text={("first", 2): "", ("first", 3): ""},
        )
        _settings, digest = preview_settings(params)
        with PdfDraftInventory(coordinator.inventory_path) as inventory:
            inventory.cache_ocr(
                source.sha256.removeprefix("sha256:"),
                2,
                selection["engine"],
                digest,
                {
                    "ocr_action_params": params,
                    "text": "cached sample",
                    "width": 10,
                    "height": 10,
                    "blocks": [],
                },
            )
        current = coordinator.quote(
            project_id="pages",
            sources=[source],
            revision=1,
            ocr_policy="missing",
            ocr_selection=selection,
            page_text={("first", 2): "", ("first", 3): ""},
        )
        assert old.public["pages"] == 2
        assert current.public["pages"] == 1
        assert 2 in current.native_files["first"]
        facts = coordinator.approve(
            old,
            current=current,
            project_id="pages",
            sources=[source],
            revision=1,
            ocr_policy="missing",
            ocr_selection=selection,
            confirmation=old.public["estimate"]["promise_set_hash"],
        )
        assert facts["ocr_authorization"]["scope"]["page_count"] == 1
        assert facts["ocr_authorization"]["scope"]["targets"][0]["page_number"] == 3
        assert (
            facts["ocr_authorization"]["allocation"]["approval_parent_hash"]
            == old.quoted["authorization"]["promise_set_hash"]
        )
        assert provider.calls == 0
    finally:
        preview._registry.shutdown()
        project.close()


def test_original_sample_runs_accounted_preview_then_binds_cached_page_to_second_occurrence(
    tmp_path, monkeypatch
):
    import json
    from dataclasses import replace
    from frisket.ai.llm import LLMResponse
    from frisket.engine.store.document_text import resolve_document_text_selection
    from frisket.engine.store.prepared_content import PreparedContentStore
    from frisket.ops.ocr_engines_hosted import ocr_coordinate_profile
    from tests.server.test_pdf_split_preparation import _wait_preview

    coordinator, preview, project, provider, source = _quote_fixture(
        tmp_path, monkeypatch
    )
    with source.open_source() as path:
        raw = path.read_bytes()
    digest = project.add_blob(raw, filename="original.pdf", mime="application/pdf")
    sheet = project.add_sheet("Originals")
    column = project.add_column(sheet, "File", type="file")
    value = {"blob": digest}
    rows = project.add_rows(sheet, [{"File": value}, {"File": value}], {"File": column})
    source = replace(
        source,
        sha256=digest,
        source_sheet_id=sheet,
        source_row_id=rows[0],
        source_column_id=column,
        frozen_value=value,
    )
    selection = {"kind": "engine", "engine": "anthropic/claude-haiku-4-5"}

    async def complete(request, client):
        provider.calls += 1
        profile = ocr_coordinate_profile(request.model)
        data = {"lines": [{"text": "Sample page", profile.bbox_field: [1, 1, 20, 10]}]}
        return LLMResponse(
            content=json.dumps(data),
            data=data,
            tokens_in=20,
            tokens_out=5,
            cost=0.01,
            model=request.model,
            cost_source="model_metadata",
        )

    provider.complete = complete
    try:
        before = project.op_cursor
        estimated = coordinator.estimate_sample(
            project_id="pages",
            sources=[source],
            source_key="first",
            page=2,
            revision=1,
            ocr_selection=selection,
        )
        with pytest.raises(ValueError, match="exact claims"):
            coordinator.start_sample(
                project_id="pages",
                sources=[source],
                source_key="first",
                page=2,
                revision=1,
                ocr_selection=selection,
                confirmation=None,
            )
        assert project.op_cursor == before
        started = coordinator.start_sample(
            project_id="pages",
            sources=[source],
            source_key="first",
            page=2,
            revision=1,
            ocr_selection=selection,
            confirmation=estimated["estimate"]["promise_set_hash"],
        )
        result = _wait_preview(preview, started["jobs"])
        assert result[0]["status"] == "done", result
        selected = resolve_document_text_selection(project, (sheet, rows[0], column))
        prepared = PreparedContentStore(project).resolve(selected.prepared_ref_id)
        assert prepared.text == "Sample page"
        assert prepared.covered_pages == (2,)
        assert prepared.complete is False
        assert provider.calls == 1
        assert (
            project.db.execute(
                "SELECT run_id FROM receipts WHERE action_kind='media.ocr' ORDER BY rowid DESC LIMIT 1"
            ).fetchone()[0]
            is not None
        )
        second = replace(source, key="second", source_row_id=rows[1])
        again = coordinator.start_sample(
            project_id="pages",
            sources=[second],
            source_key="second",
            page=2,
            revision=1,
            ocr_selection=selection,
            confirmation=None,
        )
        assert again["jobs"] == ()
        assert again["cached"] is True
        second_selection = resolve_document_text_selection(
            project, (sheet, rows[1], column)
        )
        assert second_selection.source_artifact_id != selected.source_artifact_id
        assert (
            PreparedContentStore(project).resolve(second_selection.prepared_ref_id).text
            == "Sample page"
        )
        assert provider.calls == 1
        project.undo()
        project.redo()
        assert provider.calls == 1
    finally:
        preview._registry.shutdown()
        project.close()
