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
        request_context=None,
    ):
        assert media_path.read_bytes().startswith(b"%PDF-")
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
            rows = []
            for index, page in enumerate(plan["pages"], 1):
                failed = page in self.failed_pages
                rows.append(
                    {
                        "page": {"value": page},
                        "text": {"value": "" if failed else f"OCR page {page}"},
                        "blocks": {
                            "value": []
                            if failed
                            else [
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
                ),
            )
        assert invalid_pattern.value.status_code == 422

    estimate = service.estimate_ocr(
        project_id,
        split_id,
        PdfPacketOcrEstimateRequest(engine="test-ocr", scope="sample", pages=[1, 3]),
    )
    assert estimate["pages"] == [1, 3]
    assert estimate["cached_pages"] == []
    sample_request = PdfPacketOcrJobRequest(
        engine="test-ocr", scope="sample", pages=[1, 3]
    )
    preview_runs.failed_pages.update({1, 3})
    failed_sample = service.start_ocr(project_id, split_id, sample_request)
    job = _wait_registry_job(registry, project_id, failed_sample["job_id"], "error")
    assert job is not None and job.status == "error"
    preview_runs.failed_pages.clear()
    sample = service.start_ocr(project_id, split_id, sample_request)
    assert sample["total"] == 2
    _wait_registry_job(registry, project_id, sample["job_id"], "done")
    assert service.page(project_id, split_id, 1)["ocr_text"] is None

    cached_estimate = service.estimate_ocr(
        project_id,
        split_id,
        PdfPacketOcrEstimateRequest(engine="test-ocr", scope="sample", pages=[1, 3]),
    )
    assert cached_estimate["cached_pages"] == [1, 3]
    assert cached_estimate["estimate"] is None
    cached_selected = service.select_text_source(
        project_id,
        split_id,
        PdfPacketTextSourceRequest(kind="ocr", engine="test-ocr"),
    )
    assert cached_selected["ocr_pages"] == [1, 3]

    preview_runs.failed_pages.add(4)
    full = service.start_ocr(
        project_id,
        split_id,
        PdfPacketOcrJobRequest(engine="test-ocr", scope="all"),
    )
    assert full["total"] == 2
    _status, job = _wait_session_job(
        service, project_id, split_id, full["job_id"], "done"
    )
    assert job["progress"]["status"] == "done"

    partial_selected = service.select_text_source(
        project_id,
        split_id,
        PdfPacketTextSourceRequest(kind="ocr", engine="test-ocr"),
    )
    assert partial_selected["ocr_pages"] == [1, 2, 3]
    assert service.page(project_id, split_id, 1)["ocr_text"] == "OCR page 1"
    assert service.page(project_id, split_id, 4)["ocr_text"] is None
    commit_body = PdfPacketCommitRequest(
        idempotency_key="commit-once",
        confirmed_starts=[1, 3],
        destination=PdfPacketDestination(kind="new_sheet", name="Documents"),
        name_pattern="{packet}-{index}-{start}-{end}",
        keep_ocr_text=True,
    )
    with pytest.raises(PdfPacketSplitRouteError) as incomplete:
        service.commit(project_id, split_id, commit_body)
    assert incomplete.value.status_code == 409

    preview_runs.failed_pages.clear()
    retry = service.start_ocr(
        project_id,
        split_id,
        PdfPacketOcrJobRequest(engine="test-ocr", scope="all"),
    )
    assert retry["total"] == 1
    status, job = _wait_session_job(
        service, project_id, split_id, retry["job_id"], "done"
    )
    assert job["progress"]["status"] == "done"
    assert status["ocr_pages"] == [1, 2, 3, 4]

    candidates = service.candidates(
        project_id,
        split_id,
        PdfPacketCandidatesRequest(confirmed_starts=[1, 3]),
    )
    assert [cluster["confirmed_pages"] for cluster in candidates["clusters"]] == [
        [1, 3]
    ]
    assert candidates["accept_all_scope"] == "packet"

    project = workspace.get(project_id)
    project.add_sheet("Documents")
    failed = service.commit(project_id, split_id, commit_body)
    failed_status, failed_job = _wait_session_job(
        service, project_id, split_id, failed["job_id"], "error"
    )
    assert failed_status["status"] == "ready"
    assert failed_job["progress"]["status"] == "error"
    assert failed_job["progress"]["error"] == "A sheet named 'Documents' already exists"
    assert (
        service.commit(project_id, split_id, commit_body)["job_id"] == failed["job_id"]
    )

    successful_body = commit_body.model_copy(
        update={
            "idempotency_key": "commit-retry",
            "destination": PdfPacketDestination(
                kind="new_sheet", name="Imported Documents"
            ),
        }
    )
    started = service.commit(project_id, split_id, successful_body)
    assert started["kind"] == "commit"
    completed = _wait_status(service, project_id, split_id, "completed")
    assert completed["commit_result"]["document_count"] == 2
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
    child_artifacts = project.db.execute(
        "SELECT blob_hash,source_row_id FROM source_artifacts "
        "WHERE source_sheet_id=? AND source_row_id IS NOT NULL ORDER BY source_row_id",
        (sheet_id,),
    ).fetchall()
    assert len(child_artifacts) == 2
    for artifact in child_artifacts:
        stream = resolve_ocr_word_stream(project, artifact["blob_hash"])
        assert stream is not None
        assert stream.engine == "test-ocr"
        assert [token.page for token in stream.stream.tokens] == [1, 2]
        current = resolve_current_ocr_evidence(
            project,
            sheet_id=sheet_id,
            row_id=int(artifact["source_row_id"]),
            column_id=int(ocr_column["id"]),
            blob_hash=artifact["blob_hash"],
        )
        assert len(current) == 1
        assert current[0].value_ref["prepared_ref_id"] > 0

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
        started = service.start_ocr(
            project_id,
            active_split_id,
            PdfPacketOcrJobRequest(engine="test-ocr", scope="all"),
        )
        clock.wait_entered()
        clock.advance_seconds(4)

        create_packet("active-job-reap-trigger.pdf")
        running = registry.get(project_id, started["job_id"])
        assert running is not None and running.status == "running"
        assert active_split_id in service._sessions

        clock.release()
        clock.wait_until(
            lambda: registry.get(project_id, started["job_id"]).status == "done",
            message="OCR job did not finish",
        )
        status = service.status(project_id, active_split_id)
        assert (
            next(job for job in status["jobs"] if job["job_id"] == started["job_id"])[
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
                        PdfPacketOcrJobRequest(engine="test-ocr", scope="all"),
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


def test_packet_commit_rolls_back_sheet_and_lineage_when_publication_fails(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Packet", project_id="packet")["id"])
    project = workspace.get(project_id)
    source = tmp_path / "packet.pdf"
    source.write_bytes(_pdf(2))

    from frisket.engine.store import import_blobs

    publish = import_blobs.publish_import_blobs

    def fail_after_publication(*args, **kwargs):
        publish(*args, **kwargs)
        raise RuntimeError("forced publication failure")

    monkeypatch.setattr(import_blobs, "publish_import_blobs", fail_after_publication)
    with pytest.raises(RuntimeError, match="project write failed"):
        PdfPacketCommitter(workspace).commit(
            project_id,
            source_path=source,
            source_filename="packet.pdf",
            source_page_count=2,
            ranges=[(1, 2)],
            names=["child.pdf"],
            ocr_pages=None,
            ocr_engine=None,
            sheet_name="Documents",
            idempotency_key="rollback",
            request_context=None,
            progress=lambda *_args: None,
            cancelled=lambda: False,
            seal_cancellation=lambda: True,
        )

    assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
    assert (
        project.db.execute("SELECT COUNT(*) FROM source_artifacts").fetchone()[0] == 0
    )
    assert project.db.execute("SELECT COUNT(*) FROM evidence_links").fetchone()[0] == 0
    assert (
        project.db.execute("SELECT COUNT(*) FROM blob_derivations").fetchone()[0] == 0
    )
