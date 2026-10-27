from contextlib import nullcontext

from frisket.server.services.pdf_packet_sources import AdmittedPacketSource
from frisket.server.services.pdf_packet_text import (
    classify_packet_text,
    inspect_packet_source,
    refresh_packet_source,
)


def test_quality_distinguishes_missing_suspicious_and_completed_empty_ocr():
    assert classify_packet_text([""]) == "missing"
    assert (
        classify_packet_text(["\ufffd\ufffd\ufffd\ufffd\ufffd damaged"]) == "suspicious"
    )
    assert classify_packet_text([""], completed_empty_ocr=True) == "text"


def test_stored_pages_reused_without_native_sampling(tmp_path, monkeypatch):
    from frisket.engine.store.evidence import record_source_artifact
    from frisket.engine.store.prepared_content import (
        PreparedContentStore,
        PreparedPageDraft,
    )
    from frisket.server.workspace import Workspace

    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Text", project_id="text")["id"])
    project = workspace.get(project_id)
    blob = project.add_blob(b"%PDF-fake", filename="a.pdf", mime="application/pdf")
    op_id = project.append_op("media.read_pdf_text")
    artifact = record_source_artifact(
        project,
        artifact_kind="document",
        media_type="application/pdf",
        blob_hash=blob,
        page_count=1,
    )
    with project.transaction_scope():
        ref = PreparedContentStore(project).stage_reference(
            source_artifact_id=int(artifact["id"]),
            producing_op_id=op_id,
            pages=[PreparedPageDraft(1, "Stored text here")],
        )
    path = tmp_path / "unread.pdf"
    path.write_bytes(b"not opened")
    source = AdmittedPacketSource(
        key="one",
        sha256=blob,
        filename="a.pdf",
        size=9,
        page_count=1,
        page_start=1,
        page_end=1,
        open_source=lambda: nullcontext(path),
        prepared_ref_id=ref.ref_id,
        prepared_pages=(1,),
    )

    def fail_native(*args, **kwargs):
        raise AssertionError("prepared page must not be re-extracted")

    monkeypatch.setattr(
        "frisket.server.services.pdf_packet_text.sample_pdf_text", fail_native
    )
    result = inspect_packet_source(project, source)
    assert result.category == "text"
    assert result.pages[1].text == "Stored text here"
    assert result.pages[1].source == "prepared"


def test_empty_ocr_page_is_retained_without_resampling(tmp_path, monkeypatch):
    from frisket.engine.store.evidence import record_source_artifact
    from frisket.engine.store.prepared_content import (
        PreparedContentStore,
        PreparedPageDraft,
    )
    from frisket.engine.store.runs import RunResultStore
    from frisket.server.workspace import Workspace

    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Text", project_id="text")["id"])
    project = workspace.get(project_id)
    sheet = project.add_sheet("Rows")
    blob = project.add_blob(b"%PDF-fake", filename="a.pdf", mime="application/pdf")
    op_id = project.append_op("media.ocr")
    RunResultStore(project).start_run(
        op_id, sheet, "media.ocr", row_ids=[], total_rows=0
    )
    artifact = record_source_artifact(
        project,
        artifact_kind="document",
        media_type="application/pdf",
        blob_hash=blob,
        page_count=1,
    )
    with project.transaction_scope():
        ref = PreparedContentStore(project).stage_reference(
            source_artifact_id=int(artifact["id"]),
            producing_op_id=op_id,
            pages=[PreparedPageDraft(1, "")],
        )
    source = AdmittedPacketSource(
        key="one",
        sha256=blob,
        filename="a.pdf",
        size=9,
        page_count=1,
        page_start=1,
        page_end=1,
        open_source=lambda: nullcontext(tmp_path / "unavailable"),
        prepared_ref_id=ref.ref_id,
        prepared_pages=(1,),
    )
    monkeypatch.setattr(
        "frisket.server.services.pdf_packet_text.sample_pdf_text",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("unexpected re-read")),
    )
    result = inspect_packet_source(project, source)
    assert result.category == "text"
    assert result.pages[1].source == "prepared"
    assert result.pages[1].text == ""


def test_actual_manual_edit_is_not_reused_as_a_page(tmp_path, monkeypatch):
    from frisket.engine.store.document_text import ensure_document_text_role
    from frisket.engine.store.evidence import record_source_artifact
    from frisket.engine.store.prepared_content import (
        PreparedContentStore,
        PreparedPageDraft,
    )
    from frisket.engine.store.runs import RunResultStore
    from frisket.server.workspace import Workspace
    from helpers import write_claimed_test_results

    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Text", project_id="text")["id"])
    project = workspace.get(project_id)
    sheet = project.add_sheet("Rows")
    file_column = project.add_column(sheet, "File", type="file")
    text_column = project.add_column(sheet, "Text", type="text", ai_generated=True)
    blob = project.add_blob(b"%PDF-fake", filename="a.pdf", mime="application/pdf")
    value = {"blob": blob}
    row = project.add_rows(sheet, [{"File": value}], {"File": file_column})[0]
    with project.transaction_scope():
        ensure_document_text_role(
            project,
            source_column_id=file_column,
            text_column_id=text_column,
            commit=False,
        )
    op = project.append_op("media.read_pdf_text")
    artifact = record_source_artifact(
        project,
        artifact_kind="document",
        media_type="application/pdf",
        blob_hash=blob,
        page_count=1,
        source_sheet_id=sheet,
        source_row_id=row,
        source_column_id=file_column,
    )
    with project.transaction_scope():
        ref = PreparedContentStore(project).stage_reference(
            source_artifact_id=int(artifact["id"]),
            producing_op_id=op,
            pages=[PreparedPageDraft(1, "native")],
        )
    run = RunResultStore(project).start_run(
        op, sheet, "media.read_pdf_text", row_ids=[row], total_rows=1
    )
    write_claimed_test_results(
        project, run, [{"row_id": row, "column_id": text_column, "value": ref}]
    )
    RunResultStore(project).finish_run(run, "completed")
    project.apply_edits(
        [{"row_id": row, "column_id": text_column, "value": "Corrected document text"}]
    )
    source = AdmittedPacketSource(
        key="row",
        sha256=blob,
        filename="a.pdf",
        size=9,
        page_count=1,
        page_start=1,
        page_end=1,
        open_source=lambda: nullcontext(tmp_path / "unavailable"),
        source_sheet_id=sheet,
        source_row_id=row,
        source_column_id=file_column,
        frozen_value=value,
        prepared_ref_id=ref.ref_id,
        prepared_pages=(1,),
    )

    async def native_sample(*args, **kwargs):
        from types import SimpleNamespace

        return SimpleNamespace(text="Native page text", truncated=False)

    monkeypatch.setattr(
        "frisket.server.services.pdf_packet_text.sample_pdf_text", native_sample
    )
    fresh, identity = refresh_packet_source(project, source)
    assert fresh.prepared_ref_id is None
    assert identity[0] is not None
    result = inspect_packet_source(project, fresh)
    assert result.manual_text == "Corrected document text"
    assert result.pages[1].text == "Native page text"
    assert result.pages[1].source == "native"


def test_known_native_page_is_used_for_classification_without_second_read(
    tmp_path, monkeypatch
):
    from frisket.server.services.pdf_packet_text import PacketTextPage
    from frisket.server.workspace import Workspace

    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Text", project_id="known")["id"])
    project = workspace.get(project_id)
    source = AdmittedPacketSource(
        key="one",
        sha256="a" * 64,
        filename="a.pdf",
        size=0,
        page_count=1,
        page_start=1,
        page_end=1,
        open_source=lambda: nullcontext(tmp_path / "unavailable"),
    )
    monkeypatch.setattr(
        "frisket.server.services.pdf_packet_text.sample_pdf_text",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("unexpected second read")
        ),
    )
    result = inspect_packet_source(
        project,
        source,
        known_pages={1: PacketTextPage("Already read", "native", False)},
    )
    assert result.category == "text"
    assert result.pages[1].text == "Already read"


def test_retained_ocr_from_import_keeps_ocr_provenance(tmp_path):
    from frisket.engine.store.evidence import record_source_artifact
    from frisket.engine.store.prepared_content import (
        PreparedContentStore,
        PreparedPageDraft,
    )
    from frisket.server.workspace import Workspace

    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Text", project_id="imported")["id"])
    project = workspace.get(project_id)
    blob = project.add_blob(b"%PDF-fake", filename="a.pdf", mime="application/pdf")
    op = project.append_op("import.packet_split")
    artifact = record_source_artifact(
        project,
        artifact_kind="document",
        media_type="application/pdf",
        blob_hash=blob,
        page_count=1,
    )
    with project.transaction_scope():
        ref = PreparedContentStore(project).stage_reference(
            source_artifact_id=int(artifact["id"]),
            producing_op_id=op,
            pages=[PreparedPageDraft(1, "", {"engine": "rapidocr", "blocks": []})],
        )
    source = AdmittedPacketSource(
        key="one",
        sha256=blob,
        filename="a.pdf",
        size=9,
        page_count=1,
        page_start=1,
        page_end=1,
        open_source=lambda: nullcontext(tmp_path / "unavailable"),
        prepared_ref_id=ref.ref_id,
        prepared_pages=(1,),
    )
    result = inspect_packet_source(project, source)
    assert result.category == "text"
    assert result.pages[1].completed_ocr is True
