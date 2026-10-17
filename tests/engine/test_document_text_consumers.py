from __future__ import annotations

from contextlib import closing

import pytest

from frisket.engine.executor.find_plan import FindOperation
from frisket.engine.executor.map_find_source import resolve_find_sources
from frisket.contracts.action import ActionError
from frisket.engine.store import Project
from frisket.engine.store.document_text import ensure_document_text_role
from frisket.engine.store.evidence import (
    record_evidence_link,
    record_source_artifact,
    record_source_span,
)
from frisket.engine.store.prepared_content import (
    PreparedContentStore,
    PreparedPageDraft,
)
from frisket.engine.store.prepared_readiness import prepared_source_readiness_ctes
from frisket.retrieval.source_adapter import resolve_cell_blob_source
from frisket.server.services import search_preparation_planning as planning
from frisket.server.services.project_qa_sources import (
    SourceUnavailableError,
    read_source_text,
    read_source_with_prepared,
    resolve_prepared_source,
)


def _text_output(project, source_cell, text_column, blob, pages, *, page=None):
    with project.db:
        op = project.append_op("test.document_text", commit=False)
        artifact = record_source_artifact(
            project,
            artifact_kind="document",
            media_type="application/pdf",
            blob_hash=blob,
            page_count=len(pages),
            source_sheet_id=source_cell[0],
            source_row_id=source_cell[1],
            source_column_id=source_cell[2],
        )
        ref = PreparedContentStore(project).stage_reference(
            source_artifact_id=artifact["id"],
            producing_op_id=op,
            pages=[
                PreparedPageDraft(index, text) for index, text in enumerate(pages, 1)
            ],
            page_number=page,
        )
    project.apply_edits(
        [{"row_id": source_cell[1], "column_id": text_column, "value": ref}]
    )
    _, refs = project.get_values_with_refs(
        source_cell[0], text_column, [source_cell[1]]
    )
    span = record_source_span(
        project,
        artifact_id=artifact["id"],
        span_kind="page_range",
        page_start=page or 1,
        page_end=page or len(pages),
        quote=pages[page - 1] if page else "\n\n".join(pages),
    )
    link = record_evidence_link(
        project,
        subject_kind="cell_value",
        subject_ref=refs[source_cell[1]],
        spans=[{"span_id": span["id"]}],
        sheet_id=source_cell[0],
        row_id=source_cell[1],
        column_id=text_column,
        link_role="primary_support",
    )
    return ref.ref_id, artifact["id"], span, link


def _document(project, *, page=None, pdf=b"two-page fixture"):
    sheet = project.add_sheet("Documents")
    source = project.add_column(sheet, "File", type="file")
    text = project.add_column(sheet, "Text", type="text")
    other = project.add_column(sheet, "Comparison", type="text")
    blob = project.add_blob(pdf, mime="application/pdf")
    [row] = project.add_rows(
        sheet,
        [{"File": {"blob": blob, **({"page": page} if page else {})}}],
        {"File": source},
    )
    ensure_document_text_role(
        project, source_column_id=source, text_column_id=text, commit=True
    )
    cell = (sheet, row, source)
    chosen = _text_output(
        project, cell, text, blob, ["chosen first", "chosen second"], page=page
    )
    alternative = _text_output(
        project, cell, other, blob, ["newest alternative", "wrong second"], page=page
    )
    return cell, text, other, blob, chosen, alternative


@pytest.mark.parametrize("page", [None, 2])
def test_ask_uses_designated_text_and_matching_page_not_newest_evidence(tmp_path, page):
    with closing(Project.create(tmp_path / "ask-role.frisket")) as project:
        cell, text, _other, _blob, chosen, _alternative = _document(project, page=page)

        target, _source, prepared = read_source_with_prepared(project, cell)

        assert target == (cell[0], cell[1], text)
        assert prepared is not None
        assert prepared["prepared_value_ref"]["prepared_ref_id"] == chosen[0]
        assert read_source_text(project, target)["text"] == (
            "chosen second" if page else "chosen first\n\nchosen second"
        )


def test_stale_manual_text_is_unavailable_without_alternative_fallback(tmp_path):
    with closing(Project.create(tmp_path / "stale-manual.frisket")) as project:
        cell, text, _other, _blob, _chosen, _alternative = _document(project)
        project.apply_edits(
            [{"row_id": cell[1], "column_id": text, "value": "manual correction"}]
        )
        target, _source, prepared = read_source_with_prepared(project, cell)
        assert prepared is not None
        assert read_source_text(project, target)["text"] == "manual correction"
        source = resolve_cell_blob_source(
            project, sheet_id=cell[0], row_id=cell[1], column_id=cell[2]
        )
        assert planning._has_current_result(project, source, "native_pdf")
        project.apply_edits(
            [
                {
                    "row_id": cell[1],
                    "column_id": cell[2],
                    "value": {
                        "blob": project.add_blob(
                            b"changed file", mime="application/pdf"
                        )
                    },
                }
            ]
        )

        with pytest.raises(SourceUnavailableError):
            read_source_with_prepared(project, cell)
        source = resolve_cell_blob_source(
            project, sheet_id=cell[0], row_id=cell[1], column_id=cell[2]
        )
        assert not planning._has_current_result(project, source, "native_pdf")


@pytest.mark.parametrize("page", [None, 2])
def test_preparation_readiness_and_preview_follow_only_designated_text(tmp_path, page):
    with closing(Project.create(tmp_path / "planning-role.frisket")) as project:
        cell, _text, _other, blob, chosen, _alternative = _document(project, page=page)
        source = resolve_cell_blob_source(
            project, sheet_id=cell[0], row_id=cell[1], column_id=cell[2]
        )
        assert source is not None
        assert planning._has_current_result(project, source, "native_pdf")
        prepared = planning._current_prepared_result(
            project,
            sheet_id=cell[0],
            row_id=cell[1],
            column_id=cell[2],
            blob_hash=blob,
            page_number=page,
        )
        assert prepared["prepared_ref_id"] == chosen[0]
        samples = project.db.execute(
            "WITH scope(sheet_id,row_id) AS (VALUES (?,?)), "
            + prepared_source_readiness_ctes(scope_relation="scope")
            + " SELECT DISTINCT prepared_ref_id FROM current_prepared_sources "
            "WHERE source_column_id=?",
            (*cell[:2], cell[2]),
        ).fetchall()
        assert [row[0] for row in samples] == [chosen[0]]
        item = {
            "row_id": cell[1],
            "column_id": cell[2],
            "blob_hash": blob,
            "page_number": page,
        }
        assert planning.sample_pdf(project, cell[0], item, page or 1)[:3] == (
            "chosen second" if page else "chosen first",
            2,
            "prepared",
        )


@pytest.mark.parametrize("grounded", [False, True])
def test_find_file_uses_selected_text_with_ordinary_text_publication_coordinates(
    tmp_path, grounded
):
    with closing(Project.create(tmp_path / "find-role.frisket")) as project:
        cell, text, _other, _blob, _chosen, _alternative = _document(project, page=2)
        if grounded:
            span = record_source_span(
                project,
                artifact_id=_chosen[1],
                span_kind="region",
                page_start=2,
                page_end=2,
                quote="chosen second",
                bbox=[
                    {
                        "x0": 0.1,
                        "y0": 0.1,
                        "x1": 0.7,
                        "y1": 0.2,
                        "space": "page_normalized",
                    }
                ],
            )
            _, refs = project.get_values_with_refs(cell[0], text, [cell[1]])
            record_evidence_link(
                project,
                subject_kind="cell",
                subject_ref=refs[cell[1]],
                spans=[{"span_id": span["id"]}],
                sheet_id=cell[0],
                row_id=cell[1],
                column_id=text,
                link_role="media_ocr_grounding",
                producer={"action_kind": "media.read_pdf_text"},
            )
            _, alternatives = project.get_values_with_refs(cell[0], _other, [cell[1]])
            record_evidence_link(
                project,
                subject_kind="cell",
                subject_ref=alternatives[cell[1]],
                spans=[{"span_id": _alternative[2]["id"]}],
                sheet_id=cell[0],
                row_id=cell[1],
                column_id=_other,
                link_role="media_ocr_grounding",
                producer={"action_kind": "media.ocr"},
            )
        params = FindOperation(
            action_kind="map.find",
            request={},
            sheet_id=cell[0],
            row_ids=[cell[1]],
            source_column="File",
            model="test/model",
            instruction="Find chosen",
            fields=[],
            target_sheet_name="Findings",
            output_names={"match": "Match"},
        )

        sources = resolve_find_sources(project, params)

        assert isinstance(sources, tuple), sources
        assert len(sources) == 1
        assert sources[0].column_id == text
        assert sources[0].column_type == "text"
        assert [unit.text for unit in sources[0].units] == ["chosen second"]
        assert sources[0].value_ref["prepared_ref_id"] == _chosen[0]
        if grounded:
            assert sources[0].artifact_id == _chosen[1]
            assert sources[0].units[0].span_ids == (span["id"],)
        else:
            assert all(span_id < 0 for span_id in sources[0].units[0].span_ids)

        project.apply_edits(
            [{"row_id": cell[1], "column_id": text, "value": "manual correction"}]
        )
        manual = resolve_find_sources(project, params)
        assert isinstance(manual, tuple), manual
        assert manual[0].artifact_id is None
        assert [unit.text for unit in manual[0].units] == ["manual correction"]
        assert all(span_id < 0 for span_id in manual[0].units[0].span_ids)

        project.apply_edits(
            [
                {
                    "row_id": cell[1],
                    "column_id": cell[2],
                    "value": {
                        "blob": project.add_blob(
                            b"changed file", mime="application/pdf"
                        ),
                        "page": 2,
                    },
                }
            ]
        )
        stale = resolve_find_sources(project, params)
        assert isinstance(stale, ActionError)
        assert stale.code == "source_not_addressable"


@pytest.mark.parametrize(
    ("source_column", "copied_grounding"),
    [("File", True), ("File", False), ("Text", False)],
)
def test_queued_find_publishes_designated_text_with_exact_page_citation(
    tmp_path, source_column, copied_grounding
):
    from frisket.actions.types import ActionRequest
    from frisket.engine.store.evidence import list_row_evidence, resolve_evidence_viewer
    from frisket.engine.store.receipts import ReceiptStore
    from tests.engine.test_map_find_runtime import (
        _FindAdapter,
        _operation_for_request,
        _queued_find,
        _run_queued_find,
    )
    from tests.engine.test_native_pdf_text_action import _pdf_bytes

    with closing(Project.create(tmp_path / "queued-file-find.frisket")) as project:
        cell, text, _other, blob, chosen, _alternative = _document(
            project, page=2, pdf=_pdf_bytes("chosen first", "chosen second")
        )
        span = chosen[2]
        if copied_grounding:
            _, refs = project.get_values_with_refs(cell[0], text, [cell[1]])
            record_evidence_link(
                project,
                subject_kind="cell",
                subject_ref=refs[cell[1]],
                spans=[{"span_id": span["id"]}],
                sheet_id=cell[0],
                row_id=cell[1],
                column_id=text,
                link_role="media_ocr_grounding",
                producer={"action_kind": "media.read_pdf_text"},
            )
        else:
            # Use the actual producer descriptor: a complete page snippet,
            # with no quote and no copied cell grounding link.
            from frisket.engine.executor.ocr_evidence import _page_spans

            prepared = PreparedContentStore(project).resolve(chosen[0])
            [pin] = prepared.pins
            [descriptor] = _page_spans(
                {"page": 2, "text": "chosen second", "blocks": []},
                {},
                "native_pdf_text",
                prepared_selector={
                    "prepared_ref_id": prepared.ref_id,
                    "prepared_set_id": prepared.set_id,
                    "prepared_version_id": pin.version_id,
                    "page_number": pin.page_number,
                },
            )
            span = record_source_span(project, artifact_id=chosen[1], **descriptor)
        request = ActionRequest(
            action_id="map.find",
            scope={"kind": "sheet_rows", "sheet_id": cell[0], "row_ids": [cell[1]]},
            sheet_name="Findings",
            output_names={"match": "Match"},
            params={
                "source": source_column,
                "instruction": "Find chosen second.",
                "model": "anthropic/claude-haiku-4-5",
            },
            idempotency_key="canonical-file-find",
        )
        sources = resolve_find_sources(project, _operation_for_request(request))
        assert isinstance(sources, tuple), sources
        assert sources[0].artifact_id == chosen[1]
        assert sources[0].units[0].span_ids == (span["id"],)
        envelope = _queued_find(project, request, project_id="canonical-file-find")
        assert envelope.resolved_snapshot["operation"]["source_column"] == source_column
        assert [
            item["column_id"] for item in envelope.resolved_snapshot["sources"]
        ] == [text]
        adapter = _FindAdapter(
            span["stable_id"],
            matches=[
                {
                    "match": "chosen second",
                    "source_unit_ids": [span["stable_id"]],
                    "details": {},
                }
            ],
        )

        result = _run_queued_find(project, envelope, adapter)

        assert result.status == "completed", result.errors
        assert len(adapter.requests) == 1
        receipt = ReceiptStore(project).parsed_by_id(envelope.receipt_id)
        assert receipt is not None and receipt.status == "completed"
        findings = next(
            sheet for sheet in project.sheets() if sheet["name"] == "Findings"
        )
        [row] = project.visible_row_ids(findings["id"])
        [column] = project.columns(findings["id"])
        assert (
            project.get_values(findings["id"], column["id"], [row])[row]
            == "chosen second"
        )
        [link] = list_row_evidence(project, sheet_id=findings["id"], row_id=row)[
            "links"
        ]
        viewer = resolve_evidence_viewer(project, link["stable_id"])
        [artifact] = viewer["artifacts"]
        assert artifact["artifact_ref"]["blob"]["hash"] == blob
        assert artifact["source_cell"] == dict(
            zip(("sheet_id", "row_id", "column_id"), cell, strict=True)
        )
        source_span = span
        [span] = artifact["spans"]
        assert span["stable_id"] == source_span["stable_id"]
        assert span["selector"] == {
            "kind": "page_range",
            "page_start": 2,
            "page_end": 2,
            **({"data": source_span["selector"]} if source_span["selector"] else {}),
        }
        assert span["quote"] == source_span["quote"]
        assert span["snippet"] == "chosen second"


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("blank", ["", " \n\t\r "])
def test_blank_designated_text_is_valid_but_not_preparation_ready(
    tmp_path, manual, blank
):
    with closing(Project.create(tmp_path / "blank-text.frisket")) as project:
        cell, text, _other, blob, _chosen, _alternative = _document(project)
        _text_output(project, cell, text, blob, [blank, blank])
        if manual:
            project.apply_edits(
                [{"row_id": cell[1], "column_id": text, "value": blank}]
            )
        target, _source, selected = read_source_with_prepared(project, cell)
        assert target == (cell[0], cell[1], text)
        assert selected is not None
        assert not read_source_text(project, target)["text"].strip()
        source = resolve_cell_blob_source(
            project, sheet_id=cell[0], row_id=cell[1], column_id=cell[2]
        )
        assert not planning._has_current_result(project, source, "native_pdf")
        # Readiness follows the selected pages, not a nonblank alternative.
        _text_output(project, cell, text, blob, ["", "readable second"])
        assert planning._has_current_result(project, source, "native_pdf")


def test_find_skips_blank_selected_text_and_keeps_readable_sibling(tmp_path):
    with closing(Project.create(tmp_path / "find-blank.frisket")) as project:
        cell, text, _other, blob, _chosen, _alternative = _document(project)
        [blank] = project.add_rows(
            cell[0], [{"File": {"blob": blob}}], {"File": cell[2]}
        )
        _text_output(project, (cell[0], blank, cell[2]), text, blob, ["", ""])
        params = FindOperation(
            action_kind="map.find",
            request={},
            sheet_id=cell[0],
            row_ids=[blank, cell[1]],
            source_column="File",
            model="test/model",
            instruction="Find chosen",
            fields=[],
            target_sheet_name="Findings",
            output_names={"match": "Match"},
        )
        sources = resolve_find_sources(project, params)
        assert isinstance(sources, tuple), sources
        assert [source.row_id for source in sources] == [cell[1]]
        assert sources[0].column_id == text


def test_ask_requested_link_must_match_selected_text(tmp_path):
    with closing(Project.create(tmp_path / "ask-link.frisket")) as project:
        cell, text, _other, _blob, chosen, alternative = _document(project)
        assert (
            resolve_prepared_source(
                project,
                cell,
                expected_version=read_source_text(project, cell)["version"],
                evidence_link_id=alternative[3]["stable_id"],
            )
            is None
        )
        assert (
            resolve_prepared_source(
                project,
                cell,
                expected_version=read_source_text(project, cell)["version"],
                evidence_link_id="missing-link",
            )
            is None
        )
        selected = resolve_prepared_source(
            project,
            cell,
            expected_version=read_source_text(project, cell)["version"],
            evidence_link_id=chosen[3]["stable_id"],
        )
        assert selected is not None
        assert selected["cell"] == (cell[0], cell[1], text)
        assert selected["evidence_link_id"] == chosen[3]["stable_id"]


def test_ask_manual_text_without_grounding_link_searches_and_resolves_citation(
    tmp_path,
):
    from hashlib import sha256

    from frisket.engine.store.project_qa import ProjectQAStore
    from frisket.search import drain_index
    from frisket.server.services.project_qa_citations import resolve_citation
    from frisket.server.services.project_qa_tools import ProjectQATools

    with closing(Project.create(tmp_path / "ask-manual.frisket")) as project:
        cell, text, _other, _blob, _chosen, _alternative = _document(project)
        project.apply_edits(
            [
                {
                    "row_id": cell[1],
                    "column_id": text,
                    "value": "manual NEEDLE correction",
                }
            ]
        )
        selected = resolve_prepared_source(
            project, cell, expected_version=read_source_text(project, cell)["version"]
        )
        assert selected is not None
        assert selected["evidence_link_id"] is None
        store = ProjectQAStore(project)
        thread = store.create_thread(title="Ask")
        turn = store.submit_turn(
            thread["id"],
            request_id="manual",
            question="Read the document",
            scope={
                "kind": "sources",
                "sources": [
                    {
                        "kind": "file",
                        "sheet_id": cell[0],
                        "row_id": cell[1],
                        "column_id": cell[2],
                    }
                ],
            },
        )
        drain_index(project)
        tools = ProjectQATools(project, turn, store)
        [hit] = tools.search_cells("NEEDLE", cell[0])["hits"]
        assert hit["column_id"] == cell[2]
        opened = tools.open_source(hit["citation_id"])
        assert "NEEDLE correction" in opened["passages"][0]["text"]
        resolved = resolve_citation(project, thread["id"], hit["citation_id"])
        assert resolved["status"] == "current"
        assert resolved["target"] == {
            "kind": "cell",
            "sheet_id": cell[0],
            "row_id": cell[1],
            "column_id": text,
            "text_range": {
                "text_surface_id": f"cell:{cell[1]}:{text}",
                "content_hash": "sha256:"
                + sha256(b"manual NEEDLE correction").hexdigest(),
                "start": 7,
                "end": 13,
            },
        }


def test_find_ignores_later_partial_quote_and_newline_only_prepared_page(tmp_path):
    from frisket.actions.types import ActionRequest
    from frisket.engine.executor.ocr_evidence import _page_spans
    from tests.engine.test_map_find_runtime import _operation_for_request

    with closing(Project.create(tmp_path / "find-full-page.frisket")) as project:
        cell, text, other, blob, _chosen, _alternative = _document(project)
        full_page = "Opening evidence. chosen second. Closing evidence."
        chosen = _text_output(project, cell, text, blob, ["\n", full_page])
        prepared = PreparedContentStore(project).resolve(chosen[0])
        anchors = {}
        for pin in prepared.pins:
            selector = {
                "prepared_ref_id": prepared.ref_id,
                "prepared_set_id": prepared.set_id,
                "prepared_version_id": pin.version_id,
                "page_number": pin.page_number,
            }
            [descriptor] = _page_spans(
                {
                    "page": pin.page_number,
                    "text": prepared.text[pin.start : pin.end],
                    "blocks": [],
                },
                {},
                "native_pdf_text",
                prepared_selector=selector,
            )
            anchors[pin.page_number] = record_source_span(
                project, artifact_id=chosen[1], **descriptor
            )
        # A later Extract citation correctly pins the same immutable page,
        # but its partial quote is not the producer's complete text surface.
        partial = record_source_span(
            project,
            artifact_id=chosen[1],
            span_kind="region",
            page_start=2,
            page_end=2,
            quote="chosen second",
            bbox=[
                {"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.2, "space": "page_normalized"}
            ],
            selector=anchors[2]["selector"],
        )
        _, refs = project.get_values_with_refs(cell[0], other, [cell[1]])
        record_evidence_link(
            project,
            subject_kind="cell_value",
            subject_ref=refs[cell[1]],
            spans=[{"span_id": partial["id"]}],
            sheet_id=cell[0],
            row_id=cell[1],
            column_id=other,
            producer={"action_kind": "map.extract"},
        )
        request = ActionRequest(
            action_id="map.find",
            scope={"kind": "sheet_rows", "sheet_id": cell[0], "row_ids": [cell[1]]},
            sheet_name="Findings",
            output_names={"match": "Match"},
            params={
                "source": "File",
                "instruction": "Find Closing evidence.",
                "model": "anthropic/claude-haiku-4-5",
            },
            idempotency_key="full-page-find",
        )
        span_count = project.db.execute("SELECT count(*) FROM source_spans").fetchone()[
            0
        ]
        sources = resolve_find_sources(project, _operation_for_request(request))
        assert isinstance(sources, tuple), sources
        assert sources[0].artifact_id == chosen[1]
        assert [unit.text for unit in sources[0].units] == [full_page]
        assert sources[0].units[0].span_ids == (anchors[2]["id"],)
        assert (
            project.db.execute("SELECT count(*) FROM source_spans").fetchone()[0]
            == span_count
        )
