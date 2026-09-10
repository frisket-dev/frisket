from __future__ import annotations

import copy
import json
from contextlib import closing

import pytest
from pydantic import BaseModel

from frisket.actions.core import (
    ActionCategory,
    ActionNamespace,
    ActionRegistry,
    action,
    map_rows,
)
from frisket.actions.media_types import OcrColumn, OcrText
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionParams, ActionRequest, Row, RowResult, SheetRows
from frisket.ai.llm import ModelRouter
from frisket.engine.executor.map_rows_action import (
    _TypedMapRowsProgram,
    run_typed_map_rows_action,
)
from frisket.engine.executor.ocr_evidence import write_ocr_evidence
from frisket.engine.runner import MapRunner
from frisket.engine.store import Project
from frisket.engine.store.evidence import resolve_evidence_viewer
from frisket.engine.store.media_blobs import MediaBlobStore, media_cell
from frisket.engine.store.ocr_word_stream import resolve_current_ocr_evidence
from frisket.execution.attempt_authority import UnroutedOnlyAuthority


class Params(ActionParams):
    source: OcrColumn
    emitted: str = "Hello world"


class Output(BaseModel):
    text: OcrText
    rewritten: OcrText
    unrelated: str


def recognize(params: Params, row: Row) -> RowResult[Output]:
    return RowResult(
        output=Output(
            text=OcrText(params.emitted),
            rewritten=OcrText("Hello world!"),
            unrelated="Hello world",
        )
    )


REGISTRY = ActionRegistry(
    [
        ActionNamespace(
            "custom",
            actions=[
                action(
                    name="read",
                    title="Read text",
                    description="Publish text without a blocks column.",
                    category=ActionCategory.EXTRACT,
                    run=map_rows(recognize),
                ),
            ],
        )
    ]
)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    with closing(Project.create(tmp_path / "p.frisket")) as project:
        sheet = project.add_sheet("documents")
        source_id = project.add_column(sheet, "document", type="file")
        source_digest = project.add_blob(
            b"PDF input", filename="document.pdf", mime="application/pdf"
        )
        value = media_cell(
            source_digest, mime="application/pdf", filename="document.pdf"
        )
        [row_id] = project.add_rows(
            sheet, [{"document": value}], {"document": source_id}
        )
        image = {
            "blob_hash": project.blob_store.put(b"capped PNG"),
            "mime": "image/png",
            "filename": "page.png",
            "size": 10,
            "width": 600,
            "height": 600,
            "source_width": 1200,
            "source_height": 1200,
            "downscaled": True,
            "source_blob": False,
        }
        read = {
            "kind": "ocr_read",
            "call_id": "ocr-call",
            "engine": "rapidocr",
            "options": {"dpi": 200},
            "text": "Hello world",
            "source": {
                "sheet_id": sheet,
                "row_id": row_id,
                "column_id": source_id,
                "column_type": "file",
                "source_column": "document",
                "blob_hash": source_digest,
                "filename": "document.pdf",
                "mime": "application/pdf",
                "size": 9,
            },
            "blocks": [
                {
                    "page": 1,
                    "engine": "rapidocr",
                    "blocks": [
                        {
                            "text": "Hello",
                            "bbox": [[120, 240], [600, 240], [600, 300], [120, 300]],
                            "score": 0.9,
                        },
                        {"text": "world", "bbox": None},
                    ],
                }
            ],
            "page_images": {"1": image},
        }
        original = _TypedMapRowsProgram.__init__
        writes = []

        def install_writer(self, *args, **kwargs):
            original(self, *args, **kwargs)

            def write(
                current_project, spec, *, batch, run_id, output_columns, **writer_kwargs
            ):
                writes.append(run_id)
                batch[0]["row_file_calls"] = [copy.deepcopy(read)]
                writer = current_project.db.execute(
                    "SELECT current_attempt_id FROM runs WHERE id=?", (run_id,)
                ).fetchone()[0]
                columns = {
                    output_columns[self._output_names[field.key]]
                    for field in self._resolved_output_fields
                    if field.annotation is OcrText
                }
                write_ocr_evidence(
                    current_project,
                    spec,
                    batch=batch,
                    run_id=run_id,
                    output_columns=output_columns,
                    ocr_columns=columns,
                    writer_attempt_id=read.get("writer_override", writer),
                    **writer_kwargs,
                )

            self.write_result_evidence = write

        monkeypatch.setattr(_TypedMapRowsProgram, "__init__", install_writer)

        def run(key="first", replace=False, emitted="Hello world"):
            request = ActionRequest(
                action_id="custom.read",
                scope=SheetRows(sheet_id=sheet),
                params={"source": "document", "emitted": emitted},
                output_names={"text": "recognized"},
                idempotency_key=key,
                replace_existing=replace,
            )
            return run_typed_map_rows_action(
                project,
                "p",
                BoundTypedActionRequest.bind(REGISTRY.get(request.action_id), request),
                None,
                lambda p, router: MapRunner(
                    p,
                    router or ModelRouter(cache=None, cache_mode="off"),
                    authority=UnroutedOnlyAuthority(p),
                ),
            )

        yield project, sheet, row_id, read, writes, run


def test_exact_text_only_custom_output_publishes_original_geometry_and_page_image(
    setup,
):
    project, sheet, row_id, read, writes, run = setup
    image = read["page_images"]["1"]
    assert MediaBlobStore(project).blob_row(image["blob_hash"]) is None
    result = run()
    assert result.status == "completed", result.errors
    [link] = project.db.execute("SELECT * FROM evidence_links").fetchall()
    assert (
        project.db.execute(
            "SELECT name FROM columns WHERE id=?", (link["column_id"],)
        ).fetchone()[0]
        == "recognized"
    )
    spans = project.db.execute("SELECT * FROM source_spans ORDER BY id").fetchall()
    assert [span["span_kind"] for span in spans] == [
        "page_range",
        "region",
        "page_range",
    ]
    box = json.loads(spans[1]["bbox_json"])[0]
    assert (box["x0"], box["y0"], box["x1"], box["y1"]) == (0.1, 0.2, 0.5, 0.25)
    assert (
        json.loads(spans[1]["selector_json"])["polygon"]
        == read["blocks"][0]["blocks"][0]["bbox"]
    )
    assert spans[2]["quote"] == "world"
    assert MediaBlobStore(project).probe_metadata(image["blob_hash"])["width"] == 600
    viewer = resolve_evidence_viewer(project, link["id"], project_id="p")
    [page] = viewer["artifacts"][0]["pages"]
    assert page["image"]["blob_hash"] == image["blob_hash"]
    assert page["image"]["width"] == 600
    assert (
        len(
            resolve_current_ocr_evidence(
                project, sheet_id=sheet, row_id=row_id, column_id=link["column_id"]
            )
        )
        == 1
    )
    assert run().receipt_id == result.receipt_id
    assert len(writes) == 1


def test_missing_dimensions_keep_quoted_page_fallback_not_invented_regions(setup):
    project, sheet, row_id, read, writes, run = setup
    read["page_images"] = {}
    assert run().status == "completed"
    spans = project.db.execute(
        "SELECT span_kind,quote FROM source_spans ORDER BY id"
    ).fetchall()
    assert [span["span_kind"] for span in spans] == ["page_range"] * 3
    assert [span["quote"] for span in spans[1:]] == ["Hello", "world"]


def test_geometry_free_page_is_preserved_as_page_only_evidence(setup):
    project, sheet, row_id, read, writes, run = setup
    read["blocks"][0]["blocks"] = []
    assert run().status == "completed"
    [span] = project.db.execute("SELECT * FROM source_spans").fetchall()
    assert (span["span_kind"], span["snippet"], span["quote"]) == (
        "page_range",
        "Page 1",
        None,
    )


@pytest.mark.parametrize("failure", ["mismatch", "empty", "stale_writer"])
def test_unpublished_grounding_does_not_register_page_blobs(setup, failure):
    project, sheet, row_id, read, writes, run = setup
    image_hash = read["page_images"]["1"]["blob_hash"]
    if failure == "mismatch":
        read["text"] = "Other text"
    elif failure == "empty":
        read["blocks"] = []
    else:
        read["writer_override"] = "stale-attempt"
    result = run()
    assert result.status == ("failed" if failure == "stale_writer" else "completed")
    assert MediaBlobStore(project).blob_row(image_hash) is None
    assert (
        project.db.execute("SELECT COUNT(*) FROM source_artifacts").fetchone()[0] == 0
    )


def test_reprocessing_replaces_current_grounding_and_retains_stale_link(setup):
    project, sheet, row_id, read, writes, run = setup
    assert run().status == "completed"
    result = run("second", replace=True)
    assert result.status == "completed", result.errors
    assert [
        row[0]
        for row in project.db.execute("SELECT status FROM evidence_links ORDER BY id")
    ] == ["stale", "active"]


def test_blank_ocr_page_retains_its_raster_without_fabricated_text(setup):
    project, sheet, row_id, read, writes, run = setup
    read["text"] = ""
    read["blocks"][0]["blocks"] = []
    assert run(emitted="").status == "completed"
    [span] = project.db.execute("SELECT * FROM source_spans").fetchall()
    assert span["span_kind"] == "page_range" and span["quote"] is None
    assert (
        MediaBlobStore(project).blob_row(read["page_images"]["1"]["blob_hash"])
        is not None
    )


def test_late_evidence_failure_rolls_back_page_registration_and_geometry(
    setup, monkeypatch
):
    project, sheet, row_id, read, writes, run = setup
    image_hash = read["page_images"]["1"]["blob_hash"]

    def fail(*args, **kwargs):
        assert MediaBlobStore(project).blob_row(image_hash) is not None
        raise RuntimeError("evidence publication failed")

    with monkeypatch.context() as patch:
        patch.setattr("frisket.engine.executor.ocr_evidence.record_evidence_link", fail)
        result = run()
    assert result.status == "failed"
    assert result.run_id is not None and result.receipt_id is not None
    assert [error.code for error in result.errors] == ["project_write_failed"]
    assert result.errors[0].message == "project write failed"
    assert writes == [result.run_id]
    assert MediaBlobStore(project).blob_row(image_hash) is None
    for table in (
        "results",
        "cell_result_heads",
        "source_artifacts",
        "source_spans",
        "evidence_links",
    ):
        assert project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    before = tuple(project.db.iterdump())
    replay = run()
    assert replay.model_dump() == result.model_dump()
    assert writes == [result.run_id]
    assert tuple(project.db.iterdump()) == before
