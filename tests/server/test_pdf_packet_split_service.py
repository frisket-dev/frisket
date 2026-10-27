from __future__ import annotations

import hashlib
import io
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
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
from frisket.engine.store.evidence import resolve_evidence_viewer
from frisket.engine.store.ocr_word_stream import (
    resolve_current_ocr_evidence,
    resolve_ocr_word_stream,
)
from frisket.server.services.action_preview_runs import ActionPreviewRunResponse
from frisket.server.services.action_preview_jobs import ActionPreviewJobRegistry
from frisket.server.services.import_uploads import AdmittedUpload
from frisket.server.services.pdf_packet_commit import PdfPacketCommitter
from frisket.server.services.pdf_packet_splits import (
    PdfPacketSplitRouteError,
    PdfPacketSplitService,
)
from frisket.server.workspace import Workspace
from tests.deterministic_time import controlled_time


class _PreviewRuns:
    def __init__(self, registry: ActionPreviewJobRegistry) -> None:
        self.registry = registry
        self.failed_pages: set[int] = set()
        self.text_only_pages: set[int] = {4}
        self.run_gate = None

    def prepare_ocr_scratch_path(
        self,
        project_id,
        media_path,
        *,
        media_digest,
        media_size,
        payload,
        on_page=None,
        max_pages=10,
        request_context=None,
    ):
        assert media_path.read_bytes().startswith(b"%PDF-")
        assert len(payload["pages"]) <= max_pages
        return {**payload, "on_page": on_page}, {}, None, None, None

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
            if self.run_gate is not None:
                self.run_gate({})
                if _cancelled.is_set():
                    raise RuntimeError("OCR was cancelled")
            rows = []
            for index, page in enumerate(plan["pages"], 1):
                failed = page in self.failed_pages
                rows.append(
                    {
                        "page": {"value": page},
                        "text": {"value": "" if failed else f"OCR page {page}"},
                        "blocks": {
                            "value": []
                            if failed or page in self.text_only_pages
                            else [
                                {
                                    "text": f"OCR page {page}",
                                    "bbox": [
                                        [100, 50],
                                        [900, 50],
                                        [900, 100],
                                        [100, 100],
                                    ],
                                }
                            ]
                        },
                        "errors": {
                            "value": ["provider refused page"] if failed else []
                        },
                    }
                )
                if plan["on_page"] is not None and not failed:
                    plan["on_page"](
                        {
                            "page": page,
                            "text": f"OCR page {page}",
                            "blocks": rows[-1]["blocks"]["value"],
                            "width": 1000,
                            "height": 500,
                        }
                    )
                progress(index, total)
            if rows and all(not row["text"]["value"] for row in rows):
                raise RuntimeError("all OCR pages failed")
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


def _install_packet_sample_coordinator(monkeypatch, preview_runs):
    from frisket.server.services.pdf_packet_ocr import (
        PdfPacketOcrCoordinator,
        packet_ocr_params,
    )
    from frisket.server.services.pdf_split_preparation import PdfSplitPageTarget

    completed = set()

    def estimate(
        self, *, sources, source_key, page, ocr_selection, force_rescan=False, **kwargs
    ):
        source = next(item for item in sources if item.key == source_key)
        params = packet_ocr_params(ocr_selection)
        target = PdfSplitPageTarget(
            source.key, None, source.sha256, source.source_column_id, page, params
        )
        cached = (
            source.sha256,
            page,
            params["engine"],
        ) in completed and not force_rescan
        return {
            "target": target,
            "plan_token": "test",
            "pages": 0 if cached else 1,
            "estimate": None
            if cached
            else {
                "rows": 1,
                "cost": 0.01,
                "cost_source": "free_local",
                "billed_cost": 0,
                "policy_id": "local",
            },
            "authorization": None,
        }

    def start(
        self,
        *,
        project_id,
        sources,
        source_key,
        page,
        ocr_selection,
        confirmation=None,
        **kwargs,
    ):
        source = next(item for item in sources if item.key == source_key)
        engine = packet_ocr_params(ocr_selection)["engine"]

        def on_page(value):
            from frisket.server.services.pdf_imports.inventory import PdfDraftInventory
            from frisket.server.services.pdf_imports.previews import preview_settings

            params = packet_ocr_params(ocr_selection)
            _settings, settings_hash = preview_settings(params)
            with PdfDraftInventory(self.inventory_path) as inventory:
                inventory.cache_ocr(
                    source.sha256,
                    page,
                    engine,
                    settings_hash,
                    {**value, "engine": engine, "ocr_action_params": params},
                )
            completed.add((source.sha256, page, engine))

        response = preview_runs.start_scratch_preview(
            project_id,
            {"pages": [page], "on_page": on_page},
            confirmation=confirmation,
            router=None,
            composition=None,
            execution_context=None,
            replacement_key=f"test-{source_key}-{page}",
            total=1,
        )
        return {"jobs": ({"preview_id": response.payload["preview_id"]},)}

    monkeypatch.setattr(PdfPacketOcrCoordinator, "estimate_sample", estimate)
    monkeypatch.setattr(PdfPacketOcrCoordinator, "start_sample", start)


def _pdf(page_count: int) -> bytes:
    writer = PdfWriter()
    for _index in range(page_count):
        writer.add_blank_page(width=612, height=792)
    data = io.BytesIO()
    writer.write(data)
    return data.getvalue()


def _wait_status(service, project_id, split_id, expected):
    status = None

    def reached_expected_status():
        nonlocal status
        status = service.status(project_id, split_id)
        if status["status"] == expected:
            return True
        if status["status"] == "error":
            raise AssertionError(status)
        return False

    with controlled_time(timeout=8) as clock:
        clock.wait_until(
            reached_expected_status,
            message=f"packet session did not reach {expected}",
        )
    return status


def _wait_registry_job(registry, project_id, job_id, expected):
    job = None

    def reached_expected_status():
        nonlocal job
        job = registry.get(project_id, job_id)
        return job is not None and job.status == expected

    with controlled_time(timeout=4) as clock:
        clock.wait_until(
            reached_expected_status,
            message=f"preview job {job_id} did not reach {expected}",
        )
    return job


def _wait_session_job(service, project_id, split_id, job_id, expected):
    status = None
    job = None

    def reached_expected_status():
        nonlocal job, status
        status = service.status(project_id, split_id)
        job = next((item for item in status["jobs"] if item["job_id"] == job_id), None)
        return job is not None and job["progress"]["status"] == expected

    with controlled_time(timeout=4) as clock:
        clock.wait_until(
            reached_expected_status,
            message=f"packet job {job_id} did not reach {expected}",
        )
    return status, job


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
    _install_packet_sample_coordinator(monkeypatch, preview_runs)
    service = PdfPacketSplitService(
        workspace,
        registry=registry,
        preview_runs=preview_runs,  # type: ignore[arg-type]
        page_signature=lambda path: int(path.stem.rsplit("-", 1)[1]),
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
    assert ready["prepare"]["visual_pages_ready"] == 4
    for pattern in ("{packet.__class__}", "{index:x>50000}"):
        with pytest.raises(PdfPacketSplitRouteError) as invalid_pattern:
            service.commit(
                project_id,
                split_id,
                PdfPacketCommitRequest(
                    idempotency_key=f"invalid-pattern-{pattern}",
                    confirmed_starts=[1],
                    destination=PdfPacketDestination(kind="new_sheet", name="Invalid"),
                    name_pattern=pattern,
                    analysis_revision=ready["analysis_revision"],
                ),
            )
        assert invalid_pattern.value.status_code == 422

    sample_request = PdfPacketOcrJobRequest(
        engine="rapidocr", scope="sample", pages=[1]
    )
    estimate = service.estimate_ocr(project_id, split_id, sample_request)
    assert estimate["pages"] == [1]
    preview_runs.failed_pages.add(1)
    failed_sample = service.start_ocr(project_id, split_id, sample_request)
    assert (
        _wait_registry_job(
            registry, project_id, failed_sample["job_id"], "error"
        ).status
        == "error"
    )
    preview_runs.failed_pages.clear()
    service.estimate_ocr(project_id, split_id, sample_request)
    sample = service.start_ocr(project_id, split_id, sample_request)
    _wait_registry_job(registry, project_id, sample["job_id"], "done")
    assert service.page(project_id, split_id, 1)["ocr_text"] == "OCR page 1"
    cached_estimate = service.estimate_ocr(project_id, split_id, sample_request)
    assert cached_estimate["cached_pages"] == [1]
    assert cached_estimate["estimate"] is None
    cached_selected = service.select_text_source(
        project_id, split_id, PdfPacketTextSourceRequest(kind="ocr", engine="rapidocr")
    )
    assert cached_selected["ocr_pages"] == [1]
    for page in (2, 3, 4):
        request = PdfPacketOcrJobRequest(
            engine="rapidocr", scope="sample", pages=[page]
        )
        service.estimate_ocr(project_id, split_id, request)
        started_sample = service.start_ocr(project_id, split_id, request)
        status, job = _wait_session_job(
            service, project_id, split_id, started_sample["job_id"], "done"
        )
        assert job["progress"]["status"] == "done"
    assert status["ocr_pages"] == [1, 2, 3, 4]

    candidates = service.candidates(
        project_id,
        split_id,
        PdfPacketCandidatesRequest(
            confirmed_starts=[1, 3],
            expected_analysis_revision=service.status(project_id, split_id)[
                "analysis_revision"
            ],
        ),
    )
    assert [cluster["confirmed_pages"] for cluster in candidates["clusters"]] == [
        [1, 3]
    ]
    assert candidates["accept_all_scope"] == "packet"

    from frisket.engine.jobs import HandlerRegistry, Worker
    from frisket.engine.jobs.pdf_split import register_pdf_split_handler

    project = workspace.get(project_id)
    successful_body = PdfPacketCommitRequest(
        idempotency_key="commit-once",
        confirmed_starts=[1, 3],
        destination=PdfPacketDestination(kind="new_sheet", name="Documents"),
        name_pattern="{packet}-{index}-{start}-{end}",
        analysis_revision=service.status(project_id, split_id)["analysis_revision"],
    )
    started = service.commit(project_id, split_id, successful_body)
    assert started["kind"] == "commit"
    replayed = service.commit(project_id, split_id, successful_body)
    assert replayed["job_id"] == started["job_id"]
    with pytest.raises(PdfPacketSplitRouteError) as conflict:
        service.commit(
            project_id,
            split_id,
            successful_body.model_copy(update={"name_pattern": "different-{index}"}),
        )
    assert conflict.value.status_code == 409
    service.close(project_id, split_id)
    handlers = HandlerRegistry()
    register_pdf_split_handler(
        handlers, workspace_root=workspace.root, queue=workspace.queue
    )
    worker = Worker(workspace.queue, handlers)
    for _ in range(8):
        job = workspace.queue.get(started["job_id"])
        if job.status not in {"queued", "running"}:
            break
        assert worker.run_once()
    assert job.status == "done"
    sheet_id = started["sheet_id"]
    assert project.row_count(sheet_id) == 2
    links = project.db.execute(
        "SELECT link_role FROM evidence_links WHERE sheet_id=? ORDER BY id", (sheet_id,)
    ).fetchall()
    assert [row["link_role"] for row in links].count("source_provenance") == 2
    assert [row["link_role"] for row in links].count("media_ocr_grounding") == 2
    ocr_column = next(row for row in project.columns(sheet_id) if row["name"] == "text")
    assert ocr_column["default_hidden"] == 0
    assert sorted(project.get_values(sheet_id, ocr_column["id"]).values()) == [
        "OCR page 1\n\nOCR page 2",
        "OCR page 3\n\nOCR page 4",
    ]
    child_artifacts = project.db.execute(
        "SELECT blob_hash,source_row_id FROM source_artifacts "
        "WHERE source_sheet_id=? AND source_row_id IS NOT NULL ORDER BY source_row_id",
        (sheet_id,),
    ).fetchall()
    assert len(child_artifacts) == 2
    current_evidence = []
    for artifact in child_artifacts:
        stream = resolve_ocr_word_stream(project, artifact["blob_hash"])
        assert stream is not None
        assert stream.engine == "rapidocr"
        assert all(token.box == (0.1, 0.1, 0.9, 0.2) for token in stream.stream.tokens)
        current = resolve_current_ocr_evidence(
            project,
            sheet_id=sheet_id,
            row_id=int(artifact["source_row_id"]),
            column_id=int(ocr_column["id"]),
            blob_hash=artifact["blob_hash"],
        )
        assert len(current) == 1
        assert current[0].value_ref["prepared_ref_id"] > 0
        current_evidence.append(current[0])
    region_pages = {
        evidence.artifact_id: [
            row["page_start"]
            for row in project.db.execute(
                "SELECT page_start FROM source_spans "
                "WHERE artifact_id=? AND span_kind='region' ORDER BY id",
                (evidence.artifact_id,),
            )
        ]
        for evidence in current_evidence
    }
    assert sorted(region_pages.values()) == [
        [1],
        [1, 2],
    ]
    text_only_artifact_id = next(
        artifact_id for artifact_id, pages in region_pages.items() if pages == [1]
    )
    second_summary = project.db.execute(
        "SELECT snippet FROM source_spans WHERE artifact_id=? "
        "AND span_kind='page_range' AND page_start=2 ORDER BY id LIMIT 1",
        (text_only_artifact_id,),
    ).fetchone()
    assert second_summary["snippet"] == "OCR page 4"

    source_links = project.db.execute(
        "SELECT id FROM evidence_links WHERE sheet_id=? "
        "AND link_role='source_provenance' ORDER BY row_id",
        (sheet_id,),
    ).fetchall()
    viewers = [
        resolve_evidence_viewer(project, link["id"], project_id=project_id)
        for link in source_links
    ]
    assert [viewer["artifacts"][0]["filename"] for viewer in viewers] == [
        "packet.pdf",
        "packet.pdf",
    ]
    assert [viewer["artifacts"][0]["page_count"] for viewer in viewers] == [4, 4]
    assert [
        viewer["artifacts"][0]["external_ref"]["sibling_count"] for viewer in viewers
    ] == [2, 2]
    assert [
        (
            viewer["artifacts"][0]["spans"][0]["selector"]["page_start"],
            viewer["artifacts"][0]["spans"][0]["selector"]["page_end"],
        )
        for viewer in viewers
    ] == [(1, 2), (3, 4)]
    derivations = project.db.execute(
        "SELECT op,params_json FROM blob_derivations ORDER BY id"
    ).fetchall()
    assert [row["op"] for row in derivations] == [
        "pdf_packet_split",
        "pdf_packet_split",
    ]

    registry.shutdown()


def test_create_reaps_expired_packet_sessions_and_preserves_active_ones(
    tmp_path: Path, monkeypatch
) -> None:
    async def fake_text(_source, **_kwargs):
        return [SimpleNamespace(page=1, tokens=[SimpleNamespace(text="Page 1")])]

    async def fake_render(_source, scratch, *, pages, **_kwargs):
        rendered = []
        for page in pages:
            path = scratch / f"page-{page}.png"
            path.write_bytes(b"png")
            rendered.append((page, path))
        return PdfRenderResult(page_count=1, pages=tuple(rendered))

    monkeypatch.setattr(
        "frisket.server.services.pdf_packet_splits.extract_pdf_text", fake_text
    )
    monkeypatch.setattr(
        "frisket.server.services.pdf_packet_splits.render_pdf_pages", fake_render
    )
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Packet", project_id="packet")["id"])
    registry = ActionPreviewJobRegistry()
    service = PdfPacketSplitService(
        workspace,
        registry=registry,
        preview_runs=_PreviewRuns(registry),  # type: ignore[arg-type]
        page_signature=lambda _path: 1,
    )
    raw = _pdf(1)

    def create_packet(filename: str):
        return service.create(
            project_id,
            AdmittedUpload(
                filename=filename,
                mime="application/pdf",
                source=io.BytesIO(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
                size=len(raw),
            ),
        )

    expired = create_packet("expired.pdf")
    _wait_status(service, project_id, expired["split_id"], "ready")
    active = create_packet("active.pdf")
    _wait_status(service, project_id, active["split_id"], "ready")
    with service._lock:
        expired_session = service._sessions[expired["split_id"]]
        active_session = service._sessions[active["split_id"]]
        expired_session.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        expired_directory = expired_session.directory
        expired_job_ids = tuple(expired_session.jobs)

    trigger = create_packet("trigger.pdf")

    assert expired["split_id"] not in service._sessions
    assert not expired_directory.exists()
    assert all(registry.get(project_id, job_id) is None for job_id in expired_job_ids)
    assert service._sessions[active["split_id"]] is active_session
    assert active_session.directory.exists()
    assert service.status(project_id, active["split_id"])["status"] == "ready"
    assert trigger["split_id"] in service._sessions

    service.shutdown()
    registry.shutdown()


def test_packet_session_ttl_tracks_access_and_running_jobs(
    tmp_path: Path, monkeypatch
) -> None:
    async def fake_text(_source, **_kwargs):
        return [SimpleNamespace(page=1, tokens=[SimpleNamespace(text="Page 1")])]

    async def fake_render(_source, scratch, *, pages, **_kwargs):
        rendered = []
        for page in pages:
            path = scratch / f"page-{page}.png"
            path.write_bytes(b"png")
            rendered.append((page, path))
        return PdfRenderResult(page_count=1, pages=tuple(rendered))

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
    _install_packet_sample_coordinator(monkeypatch, preview_runs)
    raw = _pdf(1)

    with controlled_time(timeout=5) as clock:
        service = PdfPacketSplitService(
            workspace,
            registry=registry,
            preview_runs=preview_runs,  # type: ignore[arg-type]
            page_signature=lambda _path: 1,
            ttl_seconds=3,
            clock=clock.now,
        )

        def create_packet(filename: str):
            return service.create(
                project_id,
                AdmittedUpload(
                    filename=filename,
                    mime="application/pdf",
                    source=io.BytesIO(raw),
                    sha256=hashlib.sha256(raw).hexdigest(),
                    size=len(raw),
                ),
            )

        created = create_packet("active.pdf")
        split_id = created["split_id"]
        clock.wait_until(
            lambda: service.status(project_id, split_id)["status"] == "ready",
            message="packet preparation did not finish",
        )
        clock.advance_seconds(2.5)
        assert service.status(project_id, split_id)["status"] == "ready"
        clock.advance_seconds(2.5)
        assert service.page(project_id, split_id, 1)["page"] == 1
        clock.advance_seconds(2.5)

        create_packet("reap-trigger.pdf")
        assert split_id in service._sessions

        clock.advance_seconds(3.5)
        create_packet("expiry-trigger.pdf")
        assert split_id not in service._sessions

        active = create_packet("active-ocr.pdf")
        active_split_id = active["split_id"]
        clock.wait_until(
            lambda: service.status(project_id, active_split_id)["status"] == "ready",
            message="packet preparation did not finish",
        )
        preview_runs.run_gate = clock.gate()
        service.estimate_ocr(
            project_id,
            active_split_id,
            PdfPacketOcrEstimateRequest(engine="rapidocr", scope="sample", pages=[1]),
        )
        started = service.start_ocr(
            project_id,
            active_split_id,
            PdfPacketOcrJobRequest(engine="rapidocr", scope="sample", pages=[1]),
        )
        clock.wait_entered()
        clock.advance_seconds(4)

        create_packet("active-job-reap-trigger.pdf")
        running = registry.get(project_id, started["job_id"])
        assert running is not None and running.status == "running"
        assert active_split_id in service._sessions
        for request in (
            PdfPacketOcrJobRequest(engine="rapidocr", scope="sample", pages=[1]),
        ):
            with pytest.raises(PdfPacketSplitRouteError) as overlapping:
                service.start_ocr(project_id, active_split_id, request)
            assert overlapping.value.status_code == 409
            assert "already running" in str(overlapping.value.detail)

        service.cancel_job(project_id, active_split_id, started["job_id"])
        clock.release()
        clock.wait_until(
            lambda: registry.get(project_id, started["job_id"]).status == "cancelled",
            message="cancelled OCR job did not finish",
        )
        preview_runs.run_gate = None
        service.estimate_ocr(
            project_id,
            active_split_id,
            PdfPacketOcrEstimateRequest(engine="rapidocr", scope="sample", pages=[1]),
        )
        retry = service.start_ocr(
            project_id,
            active_split_id,
            PdfPacketOcrJobRequest(engine="rapidocr", scope="sample", pages=[1]),
        )
        clock.wait_until(
            lambda: registry.get(project_id, retry["job_id"]).status == "done",
            message="replacement OCR job did not finish",
        )
        status = service.status(project_id, active_split_id)
        assert (
            next(job for job in status["jobs"] if job["job_id"] == retry["job_id"])[
                "progress"
            ]["status"]
            == "done"
        )
        clock.advance_seconds(4)
        create_packet("active-job-expiry-trigger.pdf")
        assert active_split_id not in service._sessions

    service.shutdown()
    registry.shutdown()


def test_packet_child_names_use_only_client_placeholders_and_fit_one_component():
    child_name = PdfPacketSplitService._child_name
    assert child_name("{packet}", "records.document", 1, 1, 2) == "records.document.pdf"
    assert child_name("{packet}", "records.PDF", 1, 1, 2) == "records.pdf"
    assert (
        child_name("{packet}-{index}-{page}-{start}-{end}", "packet.pdf", 2, 10, 19)
        == "packet-2-10-10-19.pdf"
    )
    for pattern in ("{packet.__class__}", "{index:x>50000}"):
        with pytest.raises(PdfPacketSplitRouteError, match="unknown field"):
            child_name(pattern, "packet.pdf", 2, 10, 19)
    with pytest.raises(PdfPacketSplitRouteError, match="braces"):
        child_name("{packet", "packet.pdf", 2, 10, 19)
    with pytest.raises(PdfPacketSplitRouteError, match="255 bytes"):
        child_name("x" * 252, "packet.pdf", 2, 10, 19)


def test_close_waits_for_ocr_admission_and_releases_the_registered_job(
    tmp_path: Path, monkeypatch
) -> None:
    async def fake_text(_source, **_kwargs):
        return [SimpleNamespace(page=1, tokens=[SimpleNamespace(text="Page 1")])]

    async def fake_render(_source, scratch, *, pages, **_kwargs):
        rendered = []
        for page in pages:
            path = scratch / f"page-{page}.png"
            path.write_bytes(b"png")
            rendered.append((page, path))
        return PdfRenderResult(page_count=1, pages=tuple(rendered))

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
    _install_packet_sample_coordinator(monkeypatch, preview_runs)
    service = PdfPacketSplitService(
        workspace,
        registry=registry,
        preview_runs=preview_runs,  # type: ignore[arg-type]
        page_signature=lambda _path: 1,
    )
    raw = _pdf(1)
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
    _wait_status(service, project_id, split_id, "ready")
    with service._lock:
        directory = service._sessions[split_id].directory

    service.estimate_ocr(
        project_id,
        split_id,
        PdfPacketOcrEstimateRequest(engine="rapidocr", scope="sample", pages=[1]),
    )
    original_start = preview_runs.start_scratch_preview
    original_detach = service._detach_session
    close_entered = threading.Event()
    started: list[dict] = []
    errors: list[BaseException] = []

    def observed_detach(session, *, expired_at=None):
        close_entered.set()
        return original_detach(session, expired_at=expired_at)

    monkeypatch.setattr(service, "_detach_session", observed_detach)

    with controlled_time(timeout=5) as clock:
        gate = clock.gate()

        def blocked_start(*args, **kwargs):
            gate({})
            return original_start(*args, **kwargs)

        monkeypatch.setattr(preview_runs, "start_scratch_preview", blocked_start)

        def start_ocr() -> None:
            try:
                started.append(
                    service.start_ocr(
                        project_id,
                        split_id,
                        PdfPacketOcrJobRequest(
                            engine="rapidocr", scope="sample", pages=[1]
                        ),
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        def close() -> None:
            try:
                service.close(project_id, split_id)
            except BaseException as exc:
                errors.append(exc)

        clock.background(start_ocr)
        clock.wait_entered()
        clock.background(close)
        clock.wait_until(
            close_entered.is_set,
            message="close did not reach the session admission boundary",
        )
        clock.release()

    assert errors == []
    assert len(started) == 1
    assert split_id not in service._sessions
    assert not directory.exists()
    assert registry.get(project_id, started[0]["job_id"]) is None

    service.shutdown()
    registry.shutdown()


def test_packet_commit_rolls_back_source_and_retains_failed_journal(
    tmp_path: Path, monkeypatch
) -> None:
    from contextlib import nullcontext
    from frisket.engine.jobs import HandlerRegistry, Worker
    from frisket.engine.jobs.pdf_split import register_pdf_split_handler
    from frisket.engine.store import pdf_split
    from frisket.pdf_packets import SourceMap, SourceOccurrence
    from frisket.server.services.pdf_packet_sources import AdmittedPacketSource

    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Packet", project_id="packet")["id"])
    project = workspace.get(project_id)
    path = tmp_path / "packet.pdf"
    raw = _pdf(2)
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    source = AdmittedPacketSource(
        key="packet",
        sha256=digest,
        filename="packet.pdf",
        size=len(raw),
        page_count=2,
        page_start=1,
        page_end=2,
        open_source=lambda: nullcontext(path),
    )
    source_map = SourceMap([SourceOccurrence(source.key, digest, 1, 2)])
    started = PdfPacketCommitter(workspace).start(
        project_id,
        sources=[source],
        source_map=source_map,
        ranges=source_map.ranges([1]),
        destination=PdfPacketDestination(kind="new_sheet", name="Documents"),
        idempotency_key="rollback",
    )
    publish = pdf_split.publish_import_blobs

    def fail_after_publication(*args, **kwargs):
        publish(*args, **kwargs)
        raise RuntimeError("forced publication failure")

    monkeypatch.setattr(pdf_split, "publish_import_blobs", fail_after_publication)
    handlers = HandlerRegistry()
    register_pdf_split_handler(
        handlers, workspace_root=workspace.root, queue=workspace.queue
    )
    worker = Worker(workspace.queue, handlers)
    assert worker.run_once()
    assert project.row_count(started["sheet_id"]) == 0
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM receipts WHERE id=?", (started["receipt_id"],)
        ).fetchone()[0]
        == 1
    )
    assert (
        project.db.execute("SELECT COUNT(*) FROM source_artifacts").fetchone()[0] == 0
    )
    assert project.db.execute("SELECT COUNT(*) FROM evidence_links").fetchone()[0] == 0
    assert (
        project.db.execute("SELECT COUNT(*) FROM blob_derivations").fetchone()[0] == 0
    )
    assert (
        project.db.execute("SELECT state FROM import_session_pdf_sources").fetchone()[0]
        == "failed"
    )


def test_multi_source_page_session_preserves_occurrences_and_local_extents(tmp_path):
    from contextlib import nullcontext
    from frisket.server.services.pdf_packet_splits import AdmittedPacketSource

    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Packet", project_id="multi")["id"])
    registry = ActionPreviewJobRegistry()
    service = PdfPacketSplitService(
        workspace, registry=registry, preview_runs=_PreviewRuns(registry)
    )
    raw = _pdf(3)
    path = tmp_path / "source.pdf"
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()

    def source(key, start, end):
        return AdmittedPacketSource(
            key=key,
            sha256=digest,
            filename="same.pdf",
            size=len(raw),
            page_count=3,
            page_start=start,
            page_end=end,
            open_source=lambda: nullcontext(path),
        )

    created = service.create_from_sources(
        project_id, [source("row-a", 2, 3), source("row-b", 1, 1)], organization="pages"
    )
    ready = _wait_status(service, project_id, created["split_id"], "ready")
    assert ready["fixed_starts"] == [1, 3]
    assert [item["key"] for item in ready["sources"]] == ["row-a", "row-b"]
    assert ready["prepare"]["visual_pages_ready"] == 0
    assert (
        service.page(project_id, created["split_id"], 1)["source_key"],
        service.page(project_id, created["split_id"], 1)["local_page"],
    ) == ("row-a", 2)
    assert (
        service.page(project_id, created["split_id"], 3)["source_key"],
        service.page(project_id, created["split_id"], 3)["local_page"],
    ) == ("row-b", 1)
    service.shutdown()


def test_multi_source_review_uses_local_pages_and_explicit_examples(
    tmp_path, monkeypatch
):
    from contextlib import nullcontext
    from frisket.server.services.pdf_packet_splits import AdmittedPacketSource

    async def fake_text(path, **kwargs):
        return [
            SimpleNamespace(page=i, tokens=[SimpleNamespace(text=f"{path.stem} {i}")])
            for i in range(1, 4)
        ]

    async def fake_render(path, scratch, *, pages, **kwargs):
        rendered = []
        for page in pages:
            target = scratch / f"page-{page}.png"
            target.write_bytes(f"{path.stem}:{page}".encode())
            rendered.append((page, target))
        return PdfRenderResult(page_count=3, pages=tuple(rendered))

    monkeypatch.setattr(
        "frisket.server.services.pdf_packet_splits.extract_pdf_text", fake_text
    )

    async def fake_batch(path, *, start_page, max_pages, **kwargs):
        pages = await fake_text(path)
        return SimpleNamespace(
            pages=[
                page
                for page in pages
                if start_page <= page.page < start_page + max_pages
            ],
            next_page=None,
        )

    monkeypatch.setattr(
        "frisket.server.services.pdf_packet_splits.extract_pdf_text_batch", fake_batch
    )
    monkeypatch.setattr(
        "frisket.server.services.pdf_packet_splits.render_pdf_pages", fake_render
    )
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Packet", project_id="multi")["id"])
    registry = ActionPreviewJobRegistry()
    service = PdfPacketSplitService(
        workspace,
        registry=registry,
        preview_runs=_PreviewRuns(registry),
        page_signature=lambda path: 0,
    )
    raw = _pdf(3)
    paths = [tmp_path / "first.pdf", tmp_path / "second.pdf"]
    for path in paths:
        path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    sources = [
        AdmittedPacketSource(
            key=str(i),
            sha256=digest,
            filename=path.name,
            size=len(raw),
            page_count=3,
            page_start=2,
            page_end=3,
            open_source=lambda path=path: nullcontext(path),
        )
        for i, path in enumerate(paths)
    ]
    created = service.create_from_sources(
        project_id, sources, organization="split_documents"
    )
    split_id = created["split_id"]
    _wait_status(service, project_id, split_id, "ready")
    assert service.page(project_id, split_id, 3)["native_text"] == "second 2"
    lease = service.thumbnail(project_id, split_id, 3)
    assert lease.path.read_bytes() == b"second:2"
    lease.close()
    service.select_text_source(
        project_id, split_id, PdfPacketTextSourceRequest(kind="native")
    )
    revision = service.status(project_id, split_id)["analysis_revision"]
    result = service.candidates(
        project_id,
        split_id,
        PdfPacketCandidatesRequest(
            confirmed_starts=[], example_pages=[], expected_analysis_revision=revision
        ),
    )
    assert result["suggested_pages"] == []
    result = service.candidates(
        project_id,
        split_id,
        PdfPacketCandidatesRequest(
            confirmed_starts=[], example_pages=[3], expected_analysis_revision=revision
        ),
    )
    assert result["suggested_pages"] == [2, 4]
    assert result["analysis_revision"] == revision
    assert service.status(project_id, split_id)["example_pages"] == []
    with pytest.raises(PdfPacketSplitRouteError) as stale:
        service.candidates(
            project_id,
            split_id,
            PdfPacketCandidatesRequest(
                confirmed_starts=[],
                example_pages=[],
                expected_analysis_revision=revision - 1,
            ),
        )
    assert stale.value.status_code == 409
    service._record_cached_sample(
        split_id, "rapidocr", {"page": 2, "text": "sampled phrase"}
    )
    revised = service.status(project_id, split_id)["analysis_revision"]
    mixed = service.candidates(
        project_id,
        split_id,
        PdfPacketCandidatesRequest(
            confirmed_starts=[],
            example_pages=[],
            expected_analysis_revision=revised,
            phrases=[{"id": "sampled", "text": "sampled phrase"}],
        ),
    )
    assert mixed["suggested_pages"] == [2]
    service._record_cached_sample(split_id, "rapidocr", {"page": 4, "text": ""})
    assert 4 in service.status(project_id, split_id)["prepare"]["text_pages_read"]
    service.shutdown()


def test_packet_apply_delegates_per_source_ranges_to_durable_committer(tmp_path):
    from contextlib import nullcontext
    from frisket.server.services.pdf_packet_sources import AdmittedPacketSource

    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Packet", project_id="apply")["id"])
    registry = ActionPreviewJobRegistry()
    service = PdfPacketSplitService(
        workspace, registry=registry, preview_runs=_PreviewRuns(registry)
    )
    raw = _pdf(2)
    path = tmp_path / "source.pdf"
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    sources = [
        AdmittedPacketSource(
            key=str(i),
            sha256=digest,
            filename="a.pdf",
            size=len(raw),
            page_count=2,
            page_start=1,
            page_end=2,
            open_source=lambda: nullcontext(path),
        )
        for i in range(2)
    ]
    created = service.create_from_sources(project_id, sources, organization="pages")
    ready = _wait_status(service, project_id, created["split_id"], "ready")

    class Committer:
        def start(self, pid, **kwargs):
            self.pid, self.kwargs = pid, kwargs
            return {
                "job_id": 42,
                "import_ref": "import-1",
                "receipt_id": "receipt-1",
                "sheet_id": 7,
                "source_count": 2,
                "status": "queued",
                "document_count": 4,
            }

    committer = Committer()
    service._committer = committer
    result = service.commit(
        project_id,
        created["split_id"],
        PdfPacketCommitRequest(
            idempotency_key="apply",
            destination=PdfPacketDestination(kind="new_sheet", name="Output"),
            analysis_revision=ready["analysis_revision"],
        ),
    )
    assert result["job_id"] == 42
    assert result["import_ref"] == "import-1"
    assert [(r.occurrence.key, r.start, r.end) for r in committer.kwargs["ranges"]] == [
        ("0", 1, 1),
        ("0", 2, 2),
        ("1", 1, 1),
        ("1", 2, 2),
    ]
    service.shutdown()


def test_batch_ocr_quote_is_bound_to_apply_settings(tmp_path):
    from contextlib import nullcontext
    from frisket.contracts.http.pdf_packet_splits import (
        PdfPacketBatchOcrEstimateRequest,
    )
    from frisket.server.services.pdf_packet_sources import AdmittedPacketSource

    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Packet", project_id="quote")["id"])
    registry = ActionPreviewJobRegistry()
    service = PdfPacketSplitService(
        workspace, registry=registry, preview_runs=_PreviewRuns(registry)
    )
    raw = _pdf(1)
    path = tmp_path / "source.pdf"
    path.write_bytes(raw)
    source = AdmittedPacketSource(
        key="source",
        sha256=hashlib.sha256(raw).hexdigest(),
        filename="a.pdf",
        size=len(raw),
        page_count=1,
        page_start=1,
        page_end=1,
        open_source=lambda: nullcontext(path),
    )
    split_id = service.create_from_sources(project_id, [source], organization="pages")[
        "split_id"
    ]
    ready = _wait_status(service, project_id, split_id, "ready")
    session = service._require(split_id=split_id)
    session.native_pages[1] = {
        "text": "readable native text",
        "positions": {"engine": "native", "blocks": []},
    }
    selection = {"kind": "engine", "engine": "rapidocr"}
    quote = service.estimate_batch_ocr(
        project_id,
        split_id,
        PdfPacketBatchOcrEstimateRequest(
            expected_analysis_revision=ready["analysis_revision"],
            ocr_policy="missing",
            ocr_selection=selection,
        ),
    )
    assert quote["pages"] == 0

    class Committer:
        def start(self, pid, **kwargs):
            self.kwargs = kwargs
            return {
                "job_id": 5,
                "import_ref": "import-1",
                "receipt_id": "receipt-1",
                "sheet_id": 7,
                "source_count": 1,
                "status": "queued",
                "document_count": 1,
            }

    committer = Committer()
    service._committer = committer
    body = PdfPacketCommitRequest(
        idempotency_key="apply",
        destination=PdfPacketDestination(kind="new_sheet", name="Output"),
        analysis_revision=ready["analysis_revision"],
        ocr_policy="missing",
        ocr_selection=selection,
    )
    with pytest.raises(PdfPacketSplitRouteError) as stale:
        service.commit(
            project_id, split_id, body.model_copy(update={"ocr_policy": "all"})
        )
    assert stale.value.status_code == 409
    result = service.commit(project_id, split_id, body)
    assert result["import_ref"] == "import-1"
    assert committer.kwargs["pdf_preparation"] is None
    service.shutdown()


def test_packet_apply_survives_session_close_and_worker_materializes_pages(tmp_path):
    from contextlib import nullcontext
    from frisket.engine.jobs import HandlerRegistry, Worker
    from frisket.engine.jobs.pdf_split import register_pdf_split_handler
    from frisket.server.services.pdf_packet_sources import AdmittedPacketSource

    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Packet", project_id="durable")["id"])
    registry = ActionPreviewJobRegistry()
    service = PdfPacketSplitService(
        workspace, registry=registry, preview_runs=_PreviewRuns(registry)
    )
    raw = _pdf(2)
    path = tmp_path / "source.pdf"
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    sources = [
        AdmittedPacketSource(
            key=str(i),
            sha256=digest,
            filename="a.pdf",
            size=len(raw),
            page_count=2,
            page_start=1,
            page_end=2,
            open_source=lambda: nullcontext(path),
        )
        for i in range(2)
    ]
    split_id = service.create_from_sources(project_id, sources, organization="pages")[
        "split_id"
    ]
    ready = _wait_status(service, project_id, split_id, "ready")
    started = service.commit(
        project_id,
        split_id,
        PdfPacketCommitRequest(
            idempotency_key="apply-durable",
            destination=PdfPacketDestination(kind="new_sheet", name="Output"),
            analysis_revision=ready["analysis_revision"],
        ),
    )
    service.close(project_id, split_id)
    path.unlink()
    handlers = HandlerRegistry()
    register_pdf_split_handler(
        handlers, workspace_root=workspace.root, queue=workspace.queue
    )
    worker = Worker(workspace.queue, handlers)
    for _ in range(8):
        job = workspace.queue.get(started["job_id"])
        if job.status not in {"queued", "running"}:
            break
        assert worker.run_once()
    assert job.status == "done"
    project = workspace.get(project_id)
    assert len(project.visible_row_ids(started["sheet_id"])) == 4
    service.shutdown()


def test_apply_rechecks_document_text_without_blocking_unrelated_edits(
    tmp_path, monkeypatch
):
    from frisket.engine.store.document_text import ensure_document_text_role
    from frisket.server.services.pdf_packet_scope import PdfPacketScopeService
    from frisket.contracts.http.pdf_packet_splits import PdfPacketSheetCreateRequest
    from types import SimpleNamespace

    async def sample(*args, **kwargs):
        return SimpleNamespace(text="Native page", truncated=False)

    monkeypatch.setattr(
        "frisket.server.services.pdf_packet_text.sample_pdf_text", sample
    )
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Packet", project_id="refresh")["id"])
    project = workspace.get(project_id)
    sheet = project.add_sheet("Rows")
    file_col = project.add_column(sheet, "File", type="file")
    text_col = project.add_column(sheet, "Text", type="text")
    other_col = project.add_column(sheet, "Other", type="text")
    raw = _pdf(1)
    digest = project.add_blob(raw, filename="a.pdf", mime="application/pdf")
    row = project.add_rows(
        sheet,
        [{"File": {"blob": digest}, "Text": "Initial", "Other": "a"}],
        {"File": file_col, "Text": text_col, "Other": other_col},
    )[0]
    with project.transaction_scope():
        ensure_document_text_role(
            project, source_column_id=file_col, text_column_id=text_col, commit=False
        )
    scope = PdfPacketScopeService(workspace)
    registry = ActionPreviewJobRegistry()
    service = PdfPacketSplitService(
        workspace,
        registry=registry,
        preview_runs=_PreviewRuns(registry),
        sheet_sources=scope.admit,
    )
    created = service.create_from_sheet(
        project_id,
        PdfPacketSheetCreateRequest(
            sheet_id=sheet, file_column_id=file_col, organization="pages", scope="all"
        ),
    )
    ready = _wait_status(service, project_id, created["split_id"], "ready")
    session = service._require(split_id=created["split_id"])
    project.apply_edits([{"row_id": row, "column_id": other_col, "value": "b"}])
    service._refresh_text_sources(session)
    project.apply_edits([{"row_id": row, "column_id": text_col, "value": "Changed"}])
    with pytest.raises(PdfPacketSplitRouteError) as stale:
        service._refresh_text_sources(session)
    assert stale.value.status_code == 409
    assert (
        service.status(project_id, created["split_id"])["analysis_revision"]
        > ready["analysis_revision"]
    )
    service.shutdown()


@pytest.mark.parametrize(
    "selection",
    [
        {"engine": "rapidocr"},
        {"ocr_selection": {"kind": "engine", "engine": "rapidocr"}},
    ],
)
def test_sample_ocr_estimate_resolves_occurrence_and_local_page(
    tmp_path, monkeypatch, selection
):
    from contextlib import nullcontext
    from frisket.server.services.pdf_packet_sources import AdmittedPacketSource
    from frisket.server.services.pdf_packet_ocr import (
        PdfPacketOcrCoordinator,
        packet_ocr_params,
    )
    from frisket.server.services.pdf_split_preparation import PdfSplitPageTarget

    captured = {}

    def estimate(
        self, *, sources, source_key, page, ocr_selection, force_rescan, **kwargs
    ):
        source = next(item for item in sources if item.key == source_key)
        target = PdfSplitPageTarget(
            source.key,
            None,
            source.sha256,
            source.source_column_id,
            page,
            packet_ocr_params(ocr_selection),
        )
        captured["target"] = target
        captured["force_rescan"] = force_rescan
        return {
            "target": target,
            "plan_token": "token",
            "pages": 1,
            "estimate": {
                "rows": 1,
                "cost": 0.0,
                "cost_source": "free_local",
                "billed_cost": 0,
                "policy_id": "local",
            },
            "authorization": None,
        }

    monkeypatch.setattr(
        PdfPacketOcrCoordinator, "estimate_sample", estimate, raising=False
    )
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Packet", project_id="sample")["id"])
    registry = ActionPreviewJobRegistry()
    service = PdfPacketSplitService(
        workspace, registry=registry, preview_runs=_PreviewRuns(registry)
    )
    raw = _pdf(2)
    path = tmp_path / "source.pdf"
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    sources = [
        AdmittedPacketSource(
            key=str(i),
            sha256=digest,
            filename="a.pdf",
            size=len(raw),
            page_count=2,
            page_start=1,
            page_end=2,
            open_source=lambda: nullcontext(path),
        )
        for i in range(2)
    ]
    split_id = service.create_from_sources(project_id, sources, organization="pages")[
        "split_id"
    ]
    _wait_status(service, project_id, split_id, "ready")
    estimate = service.estimate_ocr(
        project_id,
        split_id,
        PdfPacketOcrEstimateRequest(
            scope="sample",
            pages=[3],
            **selection,
            force_rescan=True,
        ),
    )
    assert estimate["pages"] == [3]
    assert captured["target"].occurrence_key == "1"
    assert captured["target"].page_number == 1
    assert captured["force_rescan"] is True

    def start(self, **kwargs):
        job = registry.start(
            project_id,
            1,
            lambda progress, cancelled: TablePreviewResult(
                [],
                [
                    {
                        "page": {"value": 1},
                        "text": {"value": "sample OCR"},
                        "blocks": {"value": []},
                    }
                ],
                1,
            ),
            replacement_key="test-sample",
        )
        return {"jobs": ({"preview_id": job.id},)}

    monkeypatch.setattr(PdfPacketOcrCoordinator, "start_sample", start, raising=False)
    started = service.start_ocr(
        project_id,
        split_id,
        PdfPacketOcrJobRequest(
            scope="sample",
            pages=[3],
            **selection,
            force_rescan=True,
        ),
    )
    _wait_registry_job(registry, project_id, started["job_id"], "done")
    assert service.page(project_id, split_id, 3)["ocr_text"] == "sample OCR"
    assert service.page(project_id, split_id, 1)["ocr_text"] is None
    service.shutdown()


def test_legacy_upload_preview_does_not_publish_original_blob(tmp_path, monkeypatch):
    async def no_text(*args, **kwargs):
        return []

    async def fake_render(path, scratch, *, pages, **kwargs):
        rendered = []
        for page in pages:
            target = scratch / f"page-{page}.png"
            target.write_bytes(b"image")
            rendered.append((page, target))
        return PdfRenderResult(page_count=1, pages=tuple(rendered))

    monkeypatch.setattr(
        "frisket.server.services.pdf_packet_splits.extract_pdf_text", no_text
    )
    monkeypatch.setattr(
        "frisket.server.services.pdf_packet_splits.render_pdf_pages", fake_render
    )
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Packet", project_id="preview")["id"])
    registry = ActionPreviewJobRegistry()
    service = PdfPacketSplitService(
        workspace,
        registry=registry,
        preview_runs=_PreviewRuns(registry),
        page_signature=lambda path: 0,
    )
    raw = _pdf(1)
    digest = hashlib.sha256(raw).hexdigest()
    created = service.create(
        project_id,
        AdmittedUpload(
            filename="a.pdf",
            mime="application/pdf",
            source=io.BytesIO(raw),
            sha256=digest,
            size=len(raw),
        ),
    )
    _wait_status(service, project_id, created["split_id"], "ready")
    assert (
        workspace.get(project_id)
        .db.execute("SELECT 1 FROM blobs WHERE hash=?", (digest,))
        .fetchone()
        is None
    )
    service.shutdown()


def test_pages_thumbnail_renders_only_requested_source_page(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from frisket.server.services.pdf_packet_sources import AdmittedPacketSource

    rendered = []

    async def render(path, scratch, *, pages, **kwargs):
        rendered.extend(pages)
        paths = []
        for page in pages:
            target = scratch / f"page-{page}.png"
            target.write_bytes(b"page image")
            paths.append((page, target))
        return PdfRenderResult(page_count=3, pages=tuple(paths))

    monkeypatch.setattr(
        "frisket.server.services.pdf_packet_splits.render_pdf_pages", render
    )
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Packet", project_id="thumb")["id"])
    registry = ActionPreviewJobRegistry()
    service = PdfPacketSplitService(
        workspace,
        registry=registry,
        preview_runs=_PreviewRuns(registry),
        page_signature=lambda path: (_ for _ in ()).throw(
            AssertionError("signature not needed")
        ),
    )
    raw = _pdf(3)
    path = tmp_path / "source.pdf"
    path.write_bytes(raw)
    source = AdmittedPacketSource(
        key="one",
        sha256=hashlib.sha256(raw).hexdigest(),
        filename="a.pdf",
        size=len(raw),
        page_count=3,
        page_start=2,
        page_end=3,
        open_source=lambda: nullcontext(path),
    )
    split_id = service.create_from_sources(project_id, [source], organization="pages")[
        "split_id"
    ]
    _wait_status(service, project_id, split_id, "ready")
    assert service.page(project_id, split_id, 1)["thumbnail_ready"] is False
    lease = service.thumbnail(project_id, split_id, 1)
    assert lease.path.read_bytes() == b"page image"
    lease.close()
    assert rendered == [2]
    assert service.status(project_id, split_id)["prepare"]["visual_pages_ready"] == 0
    service.shutdown()


@pytest.mark.parametrize(
    "selection",
    [
        {"engine": "anthropic/claude-haiku-4-5"},
        {
            "ocr_selection": {
                "kind": "engine_model",
                "engine": "llm",
                "model": "anthropic/claude-haiku-4-5",
            }
        },
    ],
)
def test_packet_service_sample_ocr_publishes_original_and_maps_page(
    tmp_path, monkeypatch, selection
):
    import json
    from dataclasses import replace
    from frisket.ai.llm import LLMResponse
    from frisket.engine.store.document_text import resolve_document_text_selection
    from frisket.engine.store.prepared_content import PreparedContentStore
    from frisket.ops.ocr_engines_hosted import ocr_coordinate_profile
    from tests.server.test_pdf_packet_ocr import _quote_fixture

    coordinator, preview, project, provider, source = _quote_fixture(
        tmp_path, monkeypatch
    )
    workspace = coordinator.workspace
    with source.open_source() as path:
        raw = path.read_bytes()
    digest = project.add_blob(raw, filename="original.pdf", mime="application/pdf")
    sheet = project.add_sheet("Originals")
    column = project.add_column(sheet, "File", type="file")
    value = {"blob": digest}
    row = project.add_rows(sheet, [{"File": value}], {"File": column})[0]
    source = replace(
        source,
        sha256=digest,
        size=len(raw),
        source_sheet_id=sheet,
        source_row_id=row,
        source_column_id=column,
        frozen_value=value,
    )

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
    service = PdfPacketSplitService(
        workspace, registry=preview._registry, preview_runs=preview
    )
    split_id = service.create_from_sources("pages", [source], organization="pages")[
        "split_id"
    ]
    _wait_status(service, "pages", split_id, "ready")
    estimate = service.estimate_ocr(
        "pages",
        split_id,
        PdfPacketOcrEstimateRequest(scope="sample", pages=[1], **selection),
    )
    started = service.start_ocr(
        "pages",
        split_id,
        PdfPacketOcrJobRequest(
            scope="sample",
            pages=[1],
            **selection,
            confirmation=estimate["estimate"]["promise_set_hash"],
        ),
    )
    _wait_registry_job(preview._registry, "pages", started["job_id"], "done")
    assert service.page("pages", split_id, 1)["ocr_text"] == "Sample page"
    service.close("pages", split_id)
    selected = resolve_document_text_selection(project, (sheet, row, column))
    assert (
        PreparedContentStore(project).resolve(selected.prepared_ref_id).text
        == "Sample page"
    )
    assert provider.calls == 1
    service.shutdown()
    preview._registry.shutdown()
    project.close()


def test_page_preview_reports_prepared_ocr_provenance(tmp_path):
    from contextlib import nullcontext
    from frisket.server.services.pdf_packet_sources import AdmittedPacketSource
    from frisket.server.services.pdf_packet_text import (
        PacketTextInspection,
        PacketTextPage,
    )

    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Packet", project_id="kind")["id"])
    registry = ActionPreviewJobRegistry()
    service = PdfPacketSplitService(
        workspace, registry=registry, preview_runs=_PreviewRuns(registry)
    )
    raw = _pdf(1)
    path = tmp_path / "source.pdf"
    path.write_bytes(raw)
    source = AdmittedPacketSource(
        key="one",
        sha256=hashlib.sha256(raw).hexdigest(),
        filename="a.pdf",
        size=len(raw),
        page_count=1,
        page_start=1,
        page_end=1,
        open_source=lambda: nullcontext(path),
    )
    split_id = service.create_from_sources(project_id, [source], organization="pages")[
        "split_id"
    ]
    _wait_status(service, project_id, split_id, "ready")
    session = service._require(split_id=split_id)
    session.text_inspections["one"] = PacketTextInspection(
        "text", {1: PacketTextPage("", "prepared", False, completed_ocr=True)}
    )
    result = service.page(project_id, split_id, 1)
    assert result["preview_text"] == ""
    assert result["preview_source"] == "prepared"
    assert result["preview_text_kind"] == "ocr"
    service.shutdown()


def test_sample_ocr_rejects_full_packet_and_ambiguous_engine():
    with pytest.raises(ValueError):
        PdfPacketOcrEstimateRequest(engine="rapidocr", scope="all")
    with pytest.raises(ValueError):
        PdfPacketOcrEstimateRequest(engine="rapidocr", scope="sample", pages=[1, 2])
    with pytest.raises(PdfPacketSplitRouteError) as invalid:
        PdfPacketSplitService._sample_request(
            PdfPacketOcrEstimateRequest(engine="llm", scope="sample", pages=[1])
        )
    assert invalid.value.status_code == 422
    with pytest.raises(PdfPacketSplitRouteError):
        PdfPacketSplitService._sample_request(
            PdfPacketOcrEstimateRequest(
                ocr_selection={"kind": "engine_model", "engine": "llm", "model": None},
                scope="sample",
                pages=[1],
            )
        )
    with pytest.raises(PdfPacketSplitRouteError):
        PdfPacketSplitService._sample_request(
            PdfPacketOcrEstimateRequest(
                engine="not-a-real-engine", scope="sample", pages=[1]
            )
        )


def test_review_and_committer_reject_unsafe_child_names_consistently():
    from frisket.server.services.pdf_packet_commit import _child_filename

    for pattern in ("../{packet}", "nested\\{packet}", "{packet.__class__}", "x" * 252):
        with pytest.raises(ValueError):
            _child_filename(pattern, "packet.pdf", 1, 1, 2)
        with pytest.raises(PdfPacketSplitRouteError):
            PdfPacketSplitService._child_name(pattern, "packet.pdf", 1, 1, 2)
