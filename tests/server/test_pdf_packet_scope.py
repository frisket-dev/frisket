import io

import pytest
from pypdf import PdfWriter

from frisket.contracts.http.pdf_packet_splits import PdfPacketSheetCreateRequest
from frisket.server.services.pdf_packet_scope import PdfPacketScopeService
from frisket.server.workspace import Workspace


def test_sheet_scope_freezes_selected_occurrences_and_rejects_stale_anchors(tmp_path):
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("scope", project_id="scope")["id"])
    project = workspace.get(project_id)
    sheet = project.add_sheet("PDFs")
    column = project.add_column(sheet, "File", type="file")
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=100, height=100)
    data = io.BytesIO()
    writer.write(data)
    digest = project.add_blob(
        data.getvalue(), filename="same.pdf", mime="application/pdf"
    )
    rows = project.add_rows(
        sheet,
        [
            {"File": {"blob": digest, "page": 2, "filename": "first.pdf"}},
            {"File": None},
            {"File": {"blob": digest}},
        ],
        {"File": column},
    )
    service = PdfPacketScopeService(workspace)
    token = service.admit(
        project_id,
        PdfPacketSheetCreateRequest(
            sheet_id=sheet,
            file_column_id=column,
            organization="pages",
            scope="filtered",
        ),
    ).order_hash
    body = PdfPacketSheetCreateRequest(
        expected_order_hash=token,
        sheet_id=sheet,
        file_column_id=column,
        organization="pages",
        scope="selected",
        selected_index_ranges=[{"start": 0, "end": 2}],
        cached_anchors=[{"index": 0, "row_id": rows[0]}],
    )
    inventory = service.admit(project_id, body)
    assert [(s.source_row_id, s.page_start, s.page_end) for s in inventory.sources] == [
        (rows[0], 2, 2),
        (rows[2], 1, 3),
    ]
    assert inventory.sources[0].key != inventory.sources[1].key
    assert [s.filename for s in inventory.sources] == ["first.pdf", "same.pdf"]
    assert inventory.excluded == ({"row_id": rows[1], "reason": "not_pdf"},)
    assert [p["row_id"] for p in inventory.preview] == [rows[0], rows[2], rows[1]]
    with pytest.raises(ValueError, match="changed"):
        service.admit(
            project_id,
            body.model_copy(
                update={
                    "cached_anchors": [
                        body.cached_anchors[0].model_copy(update={"row_id": rows[2]})
                    ]
                }
            ),
        )


def test_scope_obeys_grid_filter_sort_and_ranked_lens_order(tmp_path):
    import json

    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("scope", project_id="scope")["id"])
    project = workspace.get(project_id)
    sheet = project.add_sheet("PDFs")
    file_column = project.add_column(sheet, "File", type="file")
    rank_column = project.add_column(sheet, "rank", type="number")
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    data = io.BytesIO()
    writer.write(data)
    digest = project.add_blob(data.getvalue(), filename="a.pdf", mime="application/pdf")
    rows = project.add_rows(
        sheet,
        [{"File": {"blob": digest}, "rank": i} for i in (1, 2, 3)],
        {"File": file_column, "rank": rank_column},
    )
    service = PdfPacketScopeService(workspace)
    body = PdfPacketSheetCreateRequest(
        sheet_id=sheet,
        file_column_id=file_column,
        organization="pages",
        scope="filtered",
        filter_=json.dumps({"rank": {"gte": 2}}),
        sort=json.dumps([{"column": "rank", "dir": "desc"}]),
    )
    assert [s.source_row_id for s in service.admit(project_id, body).sources] == [
        rows[2],
        rows[1],
    ]
    ranked = body.model_copy(
        update={
            "ranked_row_ids": [rows[0], rows[2]],
            "filter_": "invalid ignored by lens",
        }
    )
    assert [s.source_row_id for s in service.admit(project_id, ranked).sources] == [
        rows[0],
        rows[2],
    ]
    selected = body.model_copy(
        update={"scope": "selected", "selected_row_ids": [rows[1], rows[2]]}
    )
    assert [s.source_row_id for s in service.admit(project_id, selected).sources] == [
        rows[2],
        rows[1],
    ]


def test_sheet_scope_creates_session_with_preview_and_frozen_source(tmp_path):
    from frisket.server.services.action_preview_jobs import ActionPreviewJobRegistry
    from frisket.server.services.pdf_packet_splits import PdfPacketSplitService

    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("scope", project_id="scope")["id"])
    project = workspace.get(project_id)
    sheet = project.add_sheet("PDFs")
    column = project.add_column(sheet, "File", type="file")
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    data = io.BytesIO()
    writer.write(data)
    digest = project.add_blob(data.getvalue(), filename="a.pdf", mime="application/pdf")
    row = project.add_rows(sheet, [{"File": {"blob": digest}}], {"File": column})[0]
    scope = PdfPacketScopeService(workspace)
    registry = ActionPreviewJobRegistry()
    service = PdfPacketSplitService(
        workspace, registry=registry, preview_runs=None, sheet_sources=scope.admit
    )
    body = PdfPacketSheetCreateRequest(
        sheet_id=sheet, file_column_id=column, organization="pages", scope="all"
    )
    created = service.create_from_sheet(project_id, body)
    assert created["sources"][0]["key"] == f"sheet:{sheet}:row:{row}:column:{column}"
    source_info = created["sources"][0]
    assert (
        source_info["source_sheet_id"],
        source_info["source_row_id"],
        source_info["source_column_id"],
    ) == (sheet, row, column)
    assert source_info["prepared_page_count"] == 0
    assert source_info["has_prepared"] is False
    assert source_info["prepared_complete"] is False
    assert created["sheet_scope"]["preview"] == [
        {"row_id": row, "filename": "a.pdf", "reason": None}
    ]
    assert (
        service.status(project_id, created["split_id"])["sheet_scope"]
        == created["sheet_scope"]
    )
    service.shutdown()


def test_scope_order_token_refuses_membership_change_even_when_anchor_survives(
    tmp_path,
):
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("scope", project_id="scope")["id"])
    project = workspace.get(project_id)
    sheet = project.add_sheet("PDFs")
    column = project.add_column(sheet, "File", type="file")
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    data = io.BytesIO()
    writer.write(data)
    digest = project.add_blob(data.getvalue(), filename="a.pdf", mime="application/pdf")
    rows = project.add_rows(
        sheet, [{"File": {"blob": digest}} for _ in range(4)], {"File": column}
    )
    service = PdfPacketScopeService(workspace)
    base = dict(sheet_id=sheet, file_column_id=column, organization="pages")
    token = service.admit(
        project_id, PdfPacketSheetCreateRequest(**base, scope="filtered")
    ).order_hash
    selected = PdfPacketSheetCreateRequest(
        **base,
        scope="selected",
        expected_order_hash=token,
        selected_index_ranges=[{"start": 0, "end": 3}],
        cached_anchors=[{"index": 0, "row_id": rows[0]}],
    )
    assert len(service.admit(project_id, selected).sources) == 4
    project.add_rows(sheet, [{"File": {"blob": digest}}], {"File": column})
    with pytest.raises(ValueError, match="changed"):
        service.admit(project_id, selected)


def test_scope_preview_prefers_valid_rows_and_bounds_exclusions(tmp_path):
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("scope", project_id="scope")["id"])
    project = workspace.get(project_id)
    sheet = project.add_sheet("PDFs")
    column = project.add_column(sheet, "File", type="file")
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    data = io.BytesIO()
    writer.write(data)
    digest = project.add_blob(data.getvalue(), filename="a.pdf", mime="application/pdf")
    rows = project.add_rows(
        sheet,
        [{"File": None} for _ in range(12)] + [{"File": {"blob": digest}}],
        {"File": column},
    )
    inventory = PdfPacketScopeService(workspace).admit(
        project_id,
        PdfPacketSheetCreateRequest(
            sheet_id=sheet, file_column_id=column, organization="pages", scope="all"
        ),
    )
    assert inventory.excluded_count == 12
    assert len(inventory.excluded) <= 9
    assert len(inventory.preview) == 9
    assert inventory.preview[0]["row_id"] == rows[-1]


def test_scope_token_never_opens_pdf_and_admits_small_selection_from_large_view(
    tmp_path, monkeypatch
):
    from frisket.contracts.http.pdf_packet_splits import PdfPacketSheetScopeRequest
    from frisket.server.services import pdf_packet_scope

    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("scope", project_id="token")["id"])
    project = workspace.get(project_id)
    sheet = project.add_sheet("PDFs")
    column = project.add_column(sheet, "File", type="file")

    def pdf(count):
        writer = PdfWriter()
        for _ in range(count):
            writer.add_blank_page(width=100, height=100)
        data = io.BytesIO()
        writer.write(data)
        return data.getvalue()

    short = project.add_blob(pdf(1), filename="short.pdf", mime="application/pdf")
    large = project.add_blob(pdf(2001), filename="large.pdf", mime="application/pdf")
    rows = project.add_rows(
        sheet, [{"File": {"blob": short}}, {"File": {"blob": large}}], {"File": column}
    )
    service = PdfPacketScopeService(workspace)
    real_reader = pdf_packet_scope.PdfReader
    monkeypatch.setattr(
        pdf_packet_scope,
        "PdfReader",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("token must not read PDFs")
        ),
    )
    token = service.order_token(
        project_id, PdfPacketSheetScopeRequest(sheet_id=sheet, scope="selected")
    )["order_hash"]
    monkeypatch.setattr(pdf_packet_scope, "PdfReader", real_reader)
    inventory = service.admit(
        project_id,
        PdfPacketSheetCreateRequest(
            sheet_id=sheet,
            file_column_id=column,
            organization="split_documents",
            scope="selected",
            expected_order_hash=token,
            selected_index_ranges=[{"start": 0, "end": 0}],
            cached_anchors=[{"index": 0, "row_id": rows[0]}],
        ),
    )
    assert len(inventory.sources) == 1
    assert inventory.sources[0].page_end == 1
