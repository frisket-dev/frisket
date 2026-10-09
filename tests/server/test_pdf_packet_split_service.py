from __future__ import annotations

import hashlib
import io
import time
from pathlib import Path
from types import SimpleNamespace

from pypdf import PdfWriter

from frisket.contracts.http.pdf_packet_splits import (
    PdfPacketCandidatesRequest,
    PdfPacketCommitRequest,
    PdfPacketDestination,
    PdfPacketOcrEstimateRequest,
    PdfPacketOcrJobRequest,
    PdfPacketTextSourceRequest,
)
from frisket.engine.pdf_render import PdfRenderResult
from frisket.engine.executor.table_preview import TablePreviewResult
from frisket.server.services.action_preview_runs import ActionPreviewRunResponse
from frisket.server.services.action_preview_jobs import ActionPreviewJobRegistry
from frisket.server.services.import_uploads import AdmittedUpload
from frisket.server.services.pdf_packet_splits import PdfPacketSplitService
from frisket.server.workspace import Workspace


class _PreviewRuns:
    def __init__(self, registry: ActionPreviewJobRegistry) -> None:
        self.registry = registry

    def prepare_ocr_scratch(
        self, project_id, media_bytes, payload, *, request_context=None
    ):
        assert media_bytes.startswith(b"%PDF-")
        return payload, {}, None, None, None

    def scratch_estimate(self, project_id, plan):
        return {"cost": len(plan["pages"]) / 100, "claims": []}

    def start_scratch_preview(
        self,
        project_id,
        plan,
        *,
        confirmation,
        router,
        composition,
        execution_context,
        replacement_key,
        total,
    ):
        def run(progress, _cancelled):
            rows = []
            for index, page in enumerate(plan["pages"], 1):
                rows.append(
                    {
                        "page": {"value": page},
                        "text": {"value": f"OCR page {page}"},
                        "blocks": {
                            "value": [
                                {
                                    "text": f"OCR page {page}",
                                    "bbox": {
                                        "space": "page_normalized",
                                        "x0": 0.1,
                                        "y0": 0.1,
                                        "x1": 0.9,
                                        "y1": 0.2,
                                    },
                                }
                            ]
                        },
                    }
                )
                progress(index, total)
            return TablePreviewResult([], rows, total)

        job = self.registry.start(
            project_id, total, run, replacement_key=replacement_key
        )
        return ActionPreviewRunResponse(
            202,
            {
                "schema_version": "frisket.action_preview.v1",
                "preview_id": job.id,
                "total": total,
            },
        )


def _pdf(page_count: int) -> bytes:
    writer = PdfWriter()
    for _index in range(page_count):
        writer.add_blank_page(width=612, height=792)
    data = io.BytesIO()
    writer.write(data)
    return data.getvalue()


def _wait_status(service, project_id, split_id, expected):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        status = service.status(project_id, split_id)
        if status["status"] == expected:
            return status
        if status["status"] == "error":
            raise AssertionError(status)
        time.sleep(0.01)
    raise AssertionError(f"packet session did not reach {expected}")


def test_packet_service_prepares_matches_and_commits_confirmed_ranges(
    tmp_path: Path, monkeypatch
) -> None:
    async def fake_text(_source, **_kwargs):
        return [
            SimpleNamespace(
                page=page,
                tokens=[SimpleNamespace(text=f"Page {page}")],
            )
            for page in range(1, 5)
        ]

    async def fake_render(_source, scratch, *, pages, **_kwargs):
        rendered = []
        for page in pages:
            path = scratch / f"page-{page}.png"
            path.write_bytes(f"png-{page}".encode())
            rendered.append((page, path))
        return PdfRenderResult(page_count=4, pages=tuple(rendered))

    monkeypatch.setattr(
        "frisket.server.services.pdf_packet_splits.extract_pdf_text", fake_text
    )
    monkeypatch.setattr(
        "frisket.server.services.pdf_packet_splits.render_pdf_pages", fake_render
    )
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Packet", project_id="packet")["id"])
    registry = ActionPreviewJobRegistry()
    preview_runs = _PreviewRuns(registry)
    service = PdfPacketSplitService(
        workspace,
        registry=registry,
        preview_runs=preview_runs,  # type: ignore[arg-type]
        image_vectorizer=lambda paths: [
            [1.0, float(path.stem.rsplit("-", 1)[1]) / 100] for path in paths
        ],
    )
    raw = _pdf(4)
    created = service.create(
        project_id,
        AdmittedUpload(
            filename="packet.pdf",
            mime="application/pdf",
            source=io.BytesIO(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            size=len(raw),
        ),
    )
    split_id = created["split_id"]
    ready = _wait_status(service, project_id, split_id, "ready")
    assert ready["prepare"]["pages_ready"] == [1, 2, 3, 4]
    assert ready["prepare"]["embeddings_ready"] == 4

    estimate = service.estimate_ocr(
        project_id,
        split_id,
        PdfPacketOcrEstimateRequest(engine="test-ocr", scope="sample", pages=[1, 3]),
    )
    assert estimate["pages"] == [1, 3]
    assert estimate["cached_pages"] == []
    sample = service.start_ocr(
        project_id,
        split_id,
        PdfPacketOcrJobRequest(engine="test-ocr", scope="sample", pages=[1, 3]),
    )
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        status = service.status(project_id, split_id)
        job = next(job for job in status["jobs"] if job["job_id"] == sample["job_id"])
        if job["progress"]["status"] == "done":
            break
        time.sleep(0.01)
    assert service.page(project_id, split_id, 1)["ocr_text"] is None

    full = service.start_ocr(
        project_id,
        split_id,
        PdfPacketOcrJobRequest(engine="test-ocr", scope="all"),
    )
    assert full["total"] == 2
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        status = service.status(project_id, split_id)
        job = next(job for job in status["jobs"] if job["job_id"] == full["job_id"])
        if job["progress"]["status"] == "done":
            break
        time.sleep(0.01)

    service.select_text_source(
        project_id,
        split_id,
        PdfPacketTextSourceRequest(kind="ocr", engine="test-ocr"),
    )
    assert service.page(project_id, split_id, 1)["ocr_text"] == "OCR page 1"
    candidates = service.candidates(
        project_id,
        split_id,
        PdfPacketCandidatesRequest(confirmed_starts=[1, 3]),
    )
    assert [cluster["confirmed_pages"] for cluster in candidates["clusters"]] == [
        [1, 3]
    ]
    assert candidates["accept_all_scope"] == "packet"

    started = service.commit(
        project_id,
        split_id,
        PdfPacketCommitRequest(
            idempotency_key="commit-once",
            confirmed_starts=[1, 3],
            destination=PdfPacketDestination(kind="new_sheet", name="Documents"),
            name_pattern="{packet}-{index}-{start}-{end}",
            keep_ocr_text=True,
        ),
    )
    assert started["kind"] == "commit"
    completed = _wait_status(service, project_id, split_id, "completed")
    assert completed["commit_result"]["document_count"] == 2

    project = workspace.get(project_id)
    sheet_id = completed["commit_result"]["sheet_id"]
    assert project.row_count(sheet_id) == 2
    links = project.db.execute(
        "SELECT link_role FROM evidence_links WHERE sheet_id=? ORDER BY id", (sheet_id,)
    ).fetchall()
    assert [row["link_role"] for row in links].count("source_provenance") == 2
    assert [row["link_role"] for row in links].count("media_ocr_grounding") == 2
    ocr_column = next(
        row for row in project.columns(sheet_id) if row["name"] == "OCR text"
    )
    assert ocr_column["default_hidden"] == 1
    assert sorted(project.get_values(sheet_id, ocr_column["id"]).values()) == [
        "OCR page 1\n\nOCR page 2",
        "OCR page 3\n\nOCR page 4",
    ]
    derivations = project.db.execute(
        "SELECT op,params_json FROM blob_derivations ORDER BY id"
    ).fetchall()
    assert [row["op"] for row in derivations] == [
        "pdf_packet_split",
        "pdf_packet_split",
    ]

    service.close(project_id, split_id)
    registry.shutdown()
