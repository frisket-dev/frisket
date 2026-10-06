from __future__ import annotations

import io
import json
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from frisket.actions.system import typed_action_for_request
from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.engine.executor import actions, run_action_spec
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.runner.review import review_queue
from frisket.engine.store import Project
from frisket.engine.store.prepared_content import PreparedContentStore
from frisket.engine.store.runs import RunResultStore
from frisket.ops import ocr_engines
from frisket.querysets import resolve_sheet_filter_rows
from frisket.search import drain_index, search_project
from frisket.server.exports.sheet_csv import render_sheet_csv
from helpers import stub_rapidocr_run_scope
from runner_test_helpers import run_action_with_exact_confirmation
from tests.engine.extract_typed_chain_helpers import typed_extract_request
from tests.engine.test_row_effect_checkpoint_boundary_fixes import _age_silent_attempt


PROJECT_ID = "pdf-page-content-flow"


class InterruptedAfterOcrReturn(BaseException):
    pass


class _ExtractAdapter:
    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest, client: Any) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            content=json.dumps(self.reply),
            data=json.loads(json.dumps(self.reply)),
            tokens_in=30,
            tokens_out=12,
            cost=0.001,
            model=request.model,
        )


def _text_pdf() -> bytes:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    for text in (
        "ALPHAONE boundary-left",
        "boundary-right OMEGATWO",
    ):
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(content)
    output = io.BytesIO()
    writer.write(output)
    writer.close()
    return output.getvalue()


def _scan_pdf(*texts: str) -> bytes:
    font = ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 48
    )
    pages = []
    for text in texts:
        page = Image.new("RGB", (1200, 800), "white")
        ImageDraw.Draw(page).text((90, 300), text, font=font, fill="black")
        pages.append(page)
    output = io.BytesIO()
    pages[0].save(output, format="PDF", save_all=True, append_images=pages[1:])
    return output.getvalue()


def _import_pdf(project: Project, source: Path, *, key: str) -> Any:
    return run_action_spec(
        project,
        {
            "action_id": "import.pdf",
            "scope": {"kind": "project"},
            "sheet_name": "Pages",
            "params": {
                "source": {"kind": "file", "path": str(source)},
                "render_pages": False,
            },
            "idempotency_key": key,
        },
        project_id=PROJECT_ID,
    )


def _convert(
    project: Project,
    sheet_id: int,
    *,
    source: str,
    output: str,
    key: str,
    row_ids: list[int] | None = None,
) -> Any:
    scope: dict[str, Any] = {"kind": "sheet_rows", "sheet_id": sheet_id}
    if row_ids is not None:
        scope["row_ids"] = row_ids
    return run_action_spec(
        project,
        {
            "action_id": "media.to_markdown",
            "scope": scope,
            "params": {"source": source, "engine": "markitdown"},
            "output_names": {"markdown": output},
            "idempotency_key": key,
        },
        project_id=PROJECT_ID,
    )


def _column_id(project: Project, sheet_id: int, name: str) -> int:
    return int(
        next(
            column["id"]
            for column in project.columns(sheet_id)
            if column["name"] == name
        )
    )


def test_native_pages_real_conversion_and_ordinary_consumers(tmp_path: Path) -> None:
    bundle = tmp_path / "flow.frisket"
    source = tmp_path / "two-pages.pdf"
    raw = _text_pdf()
    source.write_bytes(raw)
    project = Project.create(bundle, name="PDF flow")
    try:
        imported = _import_pdf(project, source, key="flow-import")
        assert imported.status == "completed", imported.errors
        sheet_id = imported.outputs[0].sheet_id
        columns = imported.outputs[0].ref["columns"]
        rows = project.visible_row_ids(sheet_id)
        assert list(columns) == ["page", "text", "source"]
        assert len(rows) == 2
        assert project.get_values(sheet_id, columns["text"], rows) == {
            rows[0]: "ALPHAONE boundary-left",
            rows[1]: "boundary-right OMEGATWO",
        }
        source_values = project.get_values(sheet_id, columns["source"], rows)
        digest = source_values[rows[0]]["blob"]
        assert source_values[rows[1]]["blob"] == digest
        assert [source_values[row]["page"] for row in rows] == [1, 2]
        assert project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 1
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM prepared_page_versions"
            ).fetchone()[0]
            == 2
        )
        assert {
            row[0]
            for row in project.db.execute(
                "SELECT value_kind FROM cells WHERE column_id=?", (columns["text"],)
            )
        } == {"prepared_content_ref"}

        converted = _convert(
            project,
            sheet_id,
            source="source",
            output="page_markdown",
            key="flow-page-markdown",
        )
        assert converted.status == "completed", converted.errors
        markdown_id = _column_id(project, sheet_id, "page_markdown")
        page_markdown = project.get_values(sheet_id, markdown_id, rows)
        assert "ALPHAONE" in page_markdown[rows[0]]
        assert "OMEGATWO" not in page_markdown[rows[0]]
        assert "OMEGATWO" in page_markdown[rows[1]]
        assert "ALPHAONE" not in page_markdown[rows[1]]

        whole_sheet = project.add_sheet("Whole document")
        whole_source = project.add_column(whole_sheet, "document", type="file")
        whole_row = project.add_rows(
            whole_sheet,
            [
                {
                    "document": {
                        "blob": digest,
                        "filename": "two-pages.pdf",
                        "mime": "application/pdf",
                    }
                }
            ],
            {"document": whole_source},
        )[0]
        whole = _convert(
            project,
            whole_sheet,
            source="document",
            output="whole_markdown",
            key="flow-whole-markdown",
        )
        assert whole.status == "completed", whole.errors
        whole_markdown_id = _column_id(project, whole_sheet, "whole_markdown")
        whole_markdown = project.get_values(whole_sheet, whole_markdown_id)[whole_row]
        assert "ALPHAONE" in whole_markdown and "OMEGATWO" in whole_markdown
        stored_whole = project.db.execute(
            "SELECT value_kind FROM results WHERE column_id=?", (whole_markdown_id,)
        ).fetchone()[0]
        assert stored_whole == "text"
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM prepared_page_versions"
            ).fetchone()[0]
            == 2
        )

        filtered = resolve_sheet_filter_rows(
            project,
            sheet_id,
            filter_=json.dumps({"text": {"contains": "OMEGATWO"}}),
        )
        assert filtered.row_ids == [rows[1]]
        drain_index(project)
        hits = search_project(project, "OMEGATWO", rerank="off")
        assert any(
            hit["row_id"] == rows[1] and hit["column_id"] == columns["text"]
            for hit in hits
        )
        _, csv_text = render_sheet_csv(project, sheet_id)
        assert "ALPHAONE boundary-left" in csv_text
        assert "boundary-right OMEGATWO" in csv_text

        adapter = _ExtractAdapter(
            {
                "finding": {
                    "value": "OMEGATWO",
                    "evidence": [
                        {
                            "quote": "boundary-right OMEGATWO",
                            "grounding_method": "quote",
                        }
                    ],
                    "warnings": [],
                }
            }
        )
        router = ModelRouter(keys={"anthropic": "test"}, cache=None, cache_mode="off")
        router._adapters["anthropic"] = adapter  # noqa: SLF001
        extracted = run_action_with_exact_confirmation(
            project,
            typed_extract_request(
                sheet_id,
                source=["text"],
                fields=[{"name": "finding", "type": "text"}],
                instruction="Return the OMEGATWO token.",
                grounding={"enabled": True, "allowed_methods": ["exact_quote"]},
                source_document_columns=["source"],
                evidence_policy={"citation_required": False},
                row_ids=[rows[1]],
                idempotency_key="flow-extract",
            ),
            project_id=PROJECT_ID,
            router=router,
        )
        assert extracted.status == "completed", extracted.errors
        assert len(adapter.requests) == 1
        prompt = json.dumps(adapter.requests[0].messages)
        assert "boundary-right OMEGATWO" in prompt
        assert "ALPHAONE" not in prompt
        finding_id = _column_id(project, sheet_id, "finding")
        assert project.get_values(sheet_id, finding_id)[rows[1]] == "OMEGATWO"
        queued = review_queue(project, run_id=extracted.run_id)
        assert len(queued) == 1 and queued[0]["value"] == "OMEGATWO"

        source.unlink()
        project.close()
        project = Project(bundle)
        assert project.get_values(sheet_id, columns["text"], rows)[rows[1]] == (
            "boundary-right OMEGATWO"
        )
        assert project.get_values(whole_sheet, whole_markdown_id)[whole_row] == (
            whole_markdown
        )
        assert project.undo() == extracted.op_ids[0]
        assert project.redo() == extracted.op_ids[0]
        assert project.get_values(sheet_id, finding_id)[rows[1]] == "OMEGATWO"
        with project.materialize_blob(digest) as stored:
            assert stored.read_bytes() == raw
    finally:
        project.close()


def test_ocr_checkpoint_crash_replays_compact_prepared_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "scan.pdf"
    source.write_bytes(_scan_pdf("CHECKPOINT OCR"))
    project = Project.create(tmp_path / "ocr-replay.frisket", name="OCR replay")
    calls = 0
    try:
        imported = _import_pdf(project, source, key="ocr-import")
        assert imported.status == "completed", imported.errors
        sheet_id = imported.outputs[0].sheet_id
        columns = imported.outputs[0].ref["columns"]
        [row_id] = project.visible_row_ids(sheet_id)

        async def deterministic_engine(self, engine, page_paths, ctx, **kwargs):
            nonlocal calls
            calls += 1
            assert len(page_paths) == 1
            with Image.open(page_paths[0]) as page:
                assert page.width > 100 and page.height > 100
            return [
                {
                    "text": "CHECKPOINT OCR",
                    "blocks": [
                        {
                            "text": "CHECKPOINT OCR",
                            "bbox": [[10, 10], [300, 10], [300, 80], [10, 80]],
                            "score": 0.99,
                        }
                    ],
                }
            ]

        monkeypatch.setattr(
            ocr_engines.OcrEngines,
            "run_engine_on_pages",
            deterministic_engine,
        )
        stub_rapidocr_run_scope(monkeypatch)
        monkeypatch.setattr(
            "frisket.engine.runner.map_runner.row_effect_spends_or_meters",
            lambda recipe, spec, router: True,
        )
        original_consume = RunResultStore.consume_returned_row_effect_checkpoint

        def interrupt(*args: Any, **kwargs: Any) -> None:
            raise InterruptedAfterOcrReturn()

        monkeypatch.setattr(
            RunResultStore,
            "consume_returned_row_effect_checkpoint",
            interrupt,
        )
        action = {
            "action_id": "media.ocr",
            "scope": {
                "kind": "sheet_rows",
                "sheet_id": sheet_id,
                "row_ids": [row_id],
            },
            "params": {"source": "source", "engine": "rapidocr", "dpi": 150},
            "output_names": {"text": "ocr_text", "blocks": "ocr_blocks"},
            "idempotency_key": "ocr-checkpoint-replay",
        }
        with pytest.raises(InterruptedAfterOcrReturn):
            run_action_spec(project, action, project_id=PROJECT_ID)
        checkpoint = project.db.execute(
            "SELECT * FROM effect_checkpoints WHERE family='row_effect'"
        ).fetchone()
        assert checkpoint is not None and checkpoint["state"] == "returned"
        payload = json.loads(checkpoint["payload"])
        assert set(payload) == {"ocr_text", "ocr_blocks"}
        text_payload = payload["ocr_text"]
        assert text_payload["value"] is None
        assert type(text_payload["prepared_ref_id"]) is int
        (fact,) = text_payload["row_file_calls"]
        assert not {"text", "pages", "blocks"} & fact.keys()
        prepared = PreparedContentStore(project).resolve(
            text_payload["prepared_ref_id"]
        )
        assert prepared.text == "CHECKPOINT OCR"
        assert prepared.page_number == 1
        assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0

        monkeypatch.setattr(
            RunResultStore,
            "consume_returned_row_effect_checkpoint",
            original_consume,
        )
        run_id = int(checkpoint["group_key"])
        _age_silent_attempt(project, checkpoint["authorized_attempt_id"])
        claim = project.db.execute(
            "SELECT claim_token FROM output_column_claims WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        plan = build_typed_map_rows_plan(
            project,
            typed_action_for_request(action),
            _allow_existing_outputs=True,
        )
        runner = actions._default_map_runner_factory(project, ModelRouter())
        progress = __import__("asyncio").run(
            runner.run(
                plan.spec_dict(),
                program=plan.program,
                confirmed=True,
                resume_run_id=run_id,
                claim_token=claim,
            )
        )
        assert progress.halted_code is None and progress.completed == 1
        assert calls == 1
        assert RunResultStore(project).row_effect_checkpoint(run_id, row_id) is None
        ocr_text_id = _column_id(project, sheet_id, "ocr_text")
        assert project.get_values(sheet_id, ocr_text_id)[row_id] == "CHECKPOINT OCR"
        raw_result = project.db.execute(
            "SELECT value_kind,value FROM results WHERE run_id=? AND row_id=? "
            "AND column_id=?",
            (run_id, row_id, ocr_text_id),
        ).fetchone()
        assert raw_result["value_kind"] == "prepared_content_ref"
        assert type(raw_result["value"]) is int
        assert project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 1
        assert project.get_values(sheet_id, columns["source"])[row_id]["page"] == 1
    finally:
        project.close()


def test_real_local_ocr_when_installed(tmp_path: Path) -> None:
    tesseract_ok, tesseract_reason = ocr_engines.tesseract_available()
    rapid_ok, rapid_reason = ocr_engines.rapidocr_models_present()
    if tesseract_ok:
        engine = "tesseract"
    elif rapid_ok:
        engine = "rapidocr"
    else:
        pytest.skip(f"local OCR unavailable: {tesseract_reason}; {rapid_reason}")

    source = tmp_path / "actual-ocr.pdf"
    source.write_bytes(_scan_pdf("EVIDENCE 7429"))
    with closing(Project.create(tmp_path / "actual-ocr.frisket")) as project:
        imported = _import_pdf(project, source, key="actual-ocr-import")
        assert imported.status == "completed", imported.errors
        sheet_id = imported.outputs[0].sheet_id
        [row_id] = project.visible_row_ids(sheet_id)
        result = run_action_spec(
            project,
            {
                "action_id": "media.ocr",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet_id,
                    "row_ids": [row_id],
                },
                "params": {"source": "source", "engine": engine, "dpi": 200},
                "output_names": {"text": "ocr_text", "blocks": "ocr_blocks"},
                "idempotency_key": "actual-local-ocr",
            },
            project_id=PROJECT_ID,
        )
        assert result.status == "completed", result.errors
        text_id = _column_id(project, sheet_id, "ocr_text")
        assert "7429" in project.get_values(sheet_id, text_id)[row_id]
