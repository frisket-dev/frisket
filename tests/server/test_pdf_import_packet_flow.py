"""PDF draft imports through the packet pipeline: documents, page text and consent."""

from __future__ import annotations

import io
from contextlib import nullcontext
from types import SimpleNamespace
import sqlite3
import json
import threading

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from frisket.ai.llm import LLMResponse, ModelRouter
from frisket.engine.jobs import Worker
from frisket.engine.jobs import pdf_split as pdf_split_job
from frisket.engine.store.local_readings import local_reading_status
from frisket.engine.store.import_inventory import ImportInventory
from frisket.execution import pricing_policy
from frisket.execution.pricing_policy import RatedEstimate
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY
from frisket.server.app import create_app
from frisket.server.services.action_preview_runs import ActionPreviewRunService
from frisket.server.services import pdf_split_preparation_jobs
from frisket.server.services.pdf_split_preparation import (
    PdfSplitPreparationAdapter,
    PdfSplitPreparationStore,
)
from frisket.server.services.search_preparation import SearchPreparationService
from tests.deterministic_time import controlled_time

PAID = "anthropic/claude-haiku-4-5"


def _blank_pdf(width: int) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=width, height=792)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


class _Provider:
    """Positioned OCR answers; optionally parked or failing per call."""

    def __init__(self) -> None:
        self.calls = 0
        self.failures = 0
        self.gate: threading.Semaphore | None = None
        self.router: ModelRouter | None = None

    async def complete(self, request, client):
        self.calls += 1
        if self.gate is not None:
            assert self.gate.acquire(timeout=30)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("OCR provider is unavailable")
        item = request.schema["properties"]["lines"]["items"]["properties"]
        field = next(name for name in item if name != "text")
        data = {"lines": [{"text": "Read page", field: [10, 10, 200, 40]}]}
        return LLMResponse(
            content=json.dumps(data),
            data=data,
            tokens_in=20,
            tokens_out=5,
            cost=0.01,
            model=request.model,
            cost_source="model_metadata",
        )


@pytest.fixture
def flow(tmp_path, monkeypatch):
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    provider = _Provider()
    router = ModelRouter(
        model_call_policy=LOCAL_PURCHASE_POLICY,
        keys={"anthropic": "test-key"},
        cache=None,
        cache_mode="off",
    )
    router._adapters["anthropic"] = provider
    provider.router = router
    client = TestClient(create_app(tmp_path / "workspace", router=router))
    pid = client.post("/api/projects", json={"name": "Scans"}).json()["id"]
    return client, pid, provider


def _draft(client, pid, files):
    """Create, upload and analyse one draft; files are (name, bytes[, mime])."""
    base = f"/api/projects/{pid}/import/pdf-imports"
    draft = client.post(
        base,
        json={
            "expected_files": len(files),
            "expected_bytes": sum(len(item[1]) for item in files),
            "source_label": "Scans",
        },
    ).json()["draft_id"]
    entries = [
        {
            "client_file_id": f"f{index}",
            "logical_path": item[0],
            "filename": item[0],
            "size": len(item[1]),
        }
        for index, item in enumerate(files)
    ]
    assert (
        client.post(
            f"{base}/{draft}/manifest", json={"batch_id": "m", "files": entries}
        ).status_code
        == 200
    )
    assert client.post(f"{base}/{draft}/manifest/seal").status_code == 200
    parts = [("batch_id", (None, "u"))]
    for index, item in enumerate(files):
        mime = item[2] if len(item) > 2 else "application/pdf"
        parts += [
            ("files", (item[0], item[1], mime)),
            ("file_ids", (None, f"f{index}")),
            ("logical_paths", (None, item[0])),
        ]
    status = client.post(f"{base}/{draft}/uploads", files=parts).json()
    while status["analysis"]["files_done"] < len(files):
        status = client.post(f"{base}/{draft}/analyze").json()
    return f"{base}/{draft}"


def _choose(
    client,
    draft,
    *,
    organization="pages",
    policy="missing",
    selection=None,
    destination=None,
):
    status = client.get(draft).json()
    response = client.patch(
        f"{draft}/choices",
        json={
            "expected_revision": status["revision"],
            "organization": organization,
            "split_starts": {},
            "ocr_policy": policy,
            "ocr_selection": selection or {"kind": "engine", "engine": PAID},
            "semantic_search": False,
            "semantic_setup_confirmation": None,
            "destination": destination or {"kind": "new", "sheet_name": "Scans"},
        },
    )
    assert response.status_code == 200, response.text
    if (destination or {}).get("kind") == "existing":
        assert client.post(f"{draft}/match").status_code == 200


def _estimate(client, draft):
    status = client.get(draft).json()
    return client.post(
        f"{draft}/ocr-estimate",
        json={
            "expected_revision": status["revision"],
            "ocr_selection": status["choices"]["ocr_selection"],
            "ocr_policy": status["choices"]["ocr_policy"],
        },
    )


def _commit(client, draft, confirmation, *, key="scans"):
    body = {
        "idempotency_key": key,
        "expected_revision": client.get(draft).json()["revision"],
    }
    if confirmation is not None:
        body["ocr_confirmation"] = confirmation
    return client.post(f"{draft}/commit", json=body)


def _publish(client):
    workspace = client.app.state.workspace
    worker = Worker(workspace.queue, workspace.registry)
    while worker.run_once():
        pass


def _import(client, pid, files, *, organization="pages"):
    """Review, estimate, consent to and commit one draft; publish its rows."""
    draft = _draft(client, pid, files)
    _choose(client, draft, organization=organization)
    estimate = _estimate(client, draft).json()
    committed = _commit(client, draft, estimate["estimate"]["promise_set_hash"])
    assert committed.status_code == 202, committed.text
    _publish(client)
    job = committed.json()["job"]
    return job["import_ref"], job["sheet_id"], estimate


def _session(client, pid, ref):
    response = client.get(f"/api/projects/{pid}/import/files/sessions/{ref}")
    assert response.status_code == 200, response.text
    return response.json()


def _settled(session):
    return (
        not session["reading_row_ids"]
        and not session["waiting_row_ids"]
        and (session["ocr_completed"] + session["failed_pages"] == session["ocr_total"])
    )


def _media_column(client, pid, sheet_id):
    project = client.app.state.workspace.get(pid)
    return next(
        int(column["id"])
        for column in project.columns(sheet_id)
        if column["name"] == "media"
    )


def test_opened_document_is_read_first_and_rows_show_reading_or_waiting(flow):
    client, pid, provider = flow
    provider.gate = threading.Semaphore(0)
    files = [(f"scan-{index}.pdf", _blank_pdf(300 + index)) for index in range(6)]
    ref, sheet_id, _estimate = _import(client, pid, files)
    rows = client.app.state.workspace.get(pid).visible_row_ids(sheet_id)
    try:
        with controlled_time() as clock:
            session = clock.wait_until(
                lambda: (
                    lambda value: value if len(value["reading_row_ids"]) == 4 else None
                )(_session(client, pid, ref)),
                message="four pages did not start",
            )
            assert session["reading_row_ids"] == rows[:4]
            assert session["waiting_row_ids"] == rows[4:]

            promoted = client.post(
                f"/api/projects/{pid}/search-preparation/document-priority",
                json={
                    "sheet_id": sheet_id,
                    "row_id": rows[5],
                    "column_id": _media_column(client, pid, sheet_id),
                },
            )
            assert promoted.status_code == 200, promoted.text
            assert promoted.json()["promoted"] is True
            assert promoted.json()["import_ref"] == ref
            assert _session(client, pid, ref)["waiting_row_ids"] == [rows[5], rows[4]]

            provider.gate.release()
            session = clock.wait_until(
                lambda: (
                    lambda value: value if rows[5] in value["reading_row_ids"] else None
                )(_session(client, pid, ref)),
                message="the opened document was not read next",
            )
            assert session["waiting_row_ids"] == [rows[4]]
            provider.gate.release(5)
            session = clock.wait_until(
                lambda: (lambda value: value if _settled(value) else None)(
                    _session(client, pid, ref)
                ),
                message="page text did not finish",
            )
    finally:
        provider.gate.release(16)
    assert session["ocr_completed"] == 6 and session["failed_pages"] == 0
    other_sheet_row = client.post(
        f"/api/projects/{pid}/search-preparation/document-priority",
        json={
            "sheet_id": sheet_id,
            "row_id": rows[0],
            "column_id": _media_column(client, pid, sheet_id),
        },
    )
    assert other_sheet_row.json() == {
        "schemaVersion": "frisket.document_priority.v1",
        "promoted": False,
        "import_ref": None,
    }


def _notifications(client, pid):
    project = client.app.state.workspace.get(pid)
    return [
        dict(row)
        for row in project.db.execute(
            "SELECT source_kind,title,severity,deep_link,payload FROM notification_items "
            "ORDER BY id"
        )
    ]


def test_failed_pages_warn_and_retry_with_consent_to_their_own_estimate(flow):
    client, pid, provider = flow
    provider.failures = 1
    files = [("first.pdf", _blank_pdf(300)), ("second.pdf", _blank_pdf(301))]
    ref, sheet_id, estimate = _import(client, pid, files)
    assert estimate["known_pages"] == 2
    with controlled_time() as clock:
        session = clock.wait_until(
            lambda: (lambda value: value if _settled(value) else None)(
                _session(client, pid, ref)
            ),
            message="page text did not settle",
        )
    assert session["failed_pages"] == 1 and session["ocr_completed"] == 1
    assert provider.calls == 2
    _session(client, pid, ref)
    (warning,) = _notifications(client, pid)
    assert warning["source_kind"] == "import"
    assert warning["severity"] == "warning"
    assert warning["title"] == "Import finished with 1 page not read"
    assert json.loads(warning["deep_link"]) == {
        "kind": "sheet",
        "project_id": pid,
        "sheet_id": sheet_id,
    }
    _session(client, pid, ref)
    assert len(_notifications(client, pid)) == 1

    sessions = f"/api/projects/{pid}/import/files/sessions/{ref}"
    retry_estimate = client.post(f"{sessions}/preparation/retry/estimate")
    assert retry_estimate.status_code == 200, retry_estimate.text
    retry = retry_estimate.json()
    assert retry["failed_pages"] == 1 and retry["pages"] == 1
    assert retry["estimate"]["requires_confirmation"] is True
    assert retry["estimate"]["billed_cost"] * 2 == estimate["estimate"]["billed_cost"]

    refused = client.post(
        f"{sessions}/preparation/retry",
        json={"plan_token": retry["plan_token"], "confirmation": None},
    )
    assert refused.status_code == 409, refused.text
    original = client.post(
        f"{sessions}/preparation/retry",
        json={
            "plan_token": retry["plan_token"],
            "confirmation": estimate["estimate"]["promise_set_hash"],
        },
    )
    assert original.status_code == 409, original.text
    foreign = client.post(
        f"{sessions}/preparation/retry",
        json={
            "plan_token": "retry:" + "0" * 64,
            "confirmation": retry["estimate"]["promise_set_hash"],
        },
    )
    assert foreign.status_code == 409, foreign.text
    assert provider.calls == 2

    retried = client.post(
        f"{sessions}/preparation/retry",
        json={
            "plan_token": retry["plan_token"],
            "confirmation": retry["estimate"]["promise_set_hash"],
        },
    )
    assert retried.status_code == 200, retried.text
    with controlled_time() as clock:
        session = clock.wait_until(
            lambda: (
                lambda value: (
                    value if _settled(value) and value["ocr_completed"] == 2 else None
                )
            )(_session(client, pid, ref)),
            message="the retried page was not read",
        )
    assert session["failed_pages"] == 0
    assert provider.calls == 3
    project = client.app.state.workspace.get(pid)
    charged = project.db.execute(
        "SELECT COUNT(*), SUM(provider_cost_usd) FROM model_calls"
    ).fetchone()
    assert tuple(charged) == (2, 0.02)
    # A clean completion is opt-in: no route asked for it, so none is stored.
    assert len(_notifications(client, pid)) == 1
    nothing_failed = client.post(f"{sessions}/preparation/retry/estimate")
    assert nothing_failed.status_code == 409, nothing_failed.text


def _route(client, pid, *, source_kind):
    channel = client.post(
        f"/api/projects/{pid}/notification-channels",
        json={
            "kind": "slack",
            "name": f"{source_kind or 'all'} info",
            "webhook_secret_ref": "env:FRISKET_TEST_UNSET_SLACK_WEBHOOK",
            "channel_label": "#imports",
            "enabled": True,
        },
    )
    assert channel.status_code == 200, channel.text
    route = client.post(
        f"/api/projects/{pid}/notification-routes",
        json={
            "name": f"{source_kind or 'all'} info",
            "channel_id": channel.json()["id"],
            "source_kind": source_kind,
            "severity_min": "info",
        },
    )
    assert route.status_code == 200, route.text


@pytest.mark.parametrize("source_kind", [None, "import"])
def test_clean_page_text_is_notified_only_to_a_route_for_imports(flow, source_kind):
    client, pid, _provider = flow
    _route(client, pid, source_kind=source_kind)
    ref, _sheet_id, _estimate = _import(client, pid, [("scan.pdf", _blank_pdf(300))])
    with controlled_time() as clock:
        clock.wait_until(
            lambda: _settled(_session(client, pid, ref)), message="text not read"
        )
    # The outcome is recorded on the first preparation pass that sees every
    # page settled, which may be the pass after the one that reported it.
    _session(client, pid, ref)
    notices = _notifications(client, pid)
    if source_kind is None:
        assert notices == []
    else:
        (notice,) = notices
        assert (notice["source_kind"], notice["severity"], notice["title"]) == (
            "import",
            "info",
            "Import text is ready",
        )


def test_an_unpriced_page_is_never_covered_by_the_allowance(flow, monkeypatch):
    client, pid, provider = flow
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "100")
    draft = _draft(client, pid, [("scan.pdf", _blank_pdf(300))])
    _choose(
        client,
        draft,
        selection={"kind": "engine", "engine": "anthropic/claude-unpriced-9"},
    )
    estimate = _estimate(client, draft)
    assert estimate.status_code == 409, estimate.text
    assert "no exact price" in estimate.json()["detail"]
    committed = _commit(client, draft, None, key="unpriced")
    assert committed.status_code == 409, committed.text
    assert provider.calls == 0
    assert client.app.state.workspace.get(pid).sheets() == []


def _text_pdf(text: str) -> bytes:
    writer = PdfWriter()
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    page = writer.add_blank_page(width=612, height=792)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(
        " ".join(
            f"BT /F1 12 Tf 72 {720 - 16 * line} Td ({text} line {line}) Tj ET"
            for line in range(12)
        ).encode()
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 64), "white").save(out, format="PNG")
    return out.getvalue()


def _charged(client, pid):
    project = client.app.state.workspace.get(pid)
    row = project.db.execute(
        "SELECT COUNT(*), COALESCE(SUM(json_extract(units,'$.pages')),0) FROM model_calls"
    ).fetchone()
    return int(row[0]), int(row[1])


def _keep(client, pid, files, *, policy="missing", selection=None):
    draft = _draft(client, pid, files)
    _choose(
        client,
        draft,
        organization="keep_documents",
        policy=policy,
        selection=selection,
    )
    return draft


def _read_all(client, pid, ref):
    with controlled_time() as clock:
        return clock.wait_until(
            lambda: (lambda value: value if _settled(value) else None)(
                _session(client, pid, ref)
            ),
            message="page text did not settle",
        )


def test_documents_wait_for_every_upload_and_publish_one_row_each_with_undo(flow):
    client, pid, _provider = flow
    files = [("first.pdf", _blank_pdf(300)), ("second.pdf", _blank_pdf(301))]
    base = f"/api/projects/{pid}/import/pdf-imports"
    draft_id = client.post(
        base,
        json={"expected_files": 2, "expected_bytes": sum(len(d) for _n, d in files)},
    ).json()["draft_id"]
    draft = f"{base}/{draft_id}"
    client.post(
        f"{draft}/manifest",
        json={
            "batch_id": "m",
            "files": [
                {
                    "client_file_id": f"f{index}",
                    "logical_path": name,
                    "filename": name,
                    "size": len(data),
                }
                for index, (name, data) in enumerate(files)
            ],
        },
    )
    client.post(f"{draft}/manifest/seal")
    client.post(
        f"{draft}/uploads",
        files=[
            ("files", ("first.pdf", files[0][1], "application/pdf")),
            ("file_ids", (None, "f0")),
            ("logical_paths", (None, "first.pdf")),
            ("batch_id", (None, "u0")),
        ],
    )
    _choose(client, draft, organization="keep_documents", policy="none")
    early = _commit(client, draft, None)
    assert early.status_code == 409, early.text
    assert early.json()["detail"] == "Wait for every file before organizing PDFs"
    client.post(
        f"{draft}/uploads",
        files=[
            ("files", ("second.pdf", files[1][1], "application/pdf")),
            ("file_ids", (None, "f1")),
            ("logical_paths", (None, "second.pdf")),
            ("batch_id", (None, "u1")),
        ],
    )
    while client.get(draft).json()["analysis"]["files_done"] < 2:
        client.post(f"{draft}/analyze")
    committed = _commit(client, draft, None)
    assert committed.status_code == 202, committed.text
    job = committed.json()["job"]
    _publish(client)
    project = client.app.state.workspace.get(pid)
    rows = project.visible_row_ids(job["sheet_id"])
    columns = {c["name"]: c["id"] for c in project.columns(job["sheet_id"])}
    names = project.get_values(job["sheet_id"], columns["filename"], rows)
    assert [names[row] for row in rows] == ["first.pdf", "second.pdf"]
    receipt = project.db.execute(
        "SELECT action_kind,status FROM receipts WHERE id=?", (job["receipt_id"],)
    ).fetchone()
    assert tuple(receipt) == ("import.packet_split", "completed")
    assert client.get(draft).json()["state"] == "committed"
    project.undo()
    assert project.visible_row_ids(job["sheet_id"]) == []
    project.redo()
    assert project.visible_row_ids(job["sheet_id"]) == rows


def test_mixed_pdf_image_and_other_files_become_documents(flow):
    client, pid, _provider = flow
    draft = _keep(
        client,
        pid,
        [
            ("scan.pdf", _blank_pdf(300)),
            ("photo.png", _png(), "image/png"),
            ("notes.txt", b"plain notes", "text/plain"),
        ],
        policy="none",
    )
    committed = _commit(client, draft, None)
    assert committed.status_code == 202, committed.text
    _publish(client)
    sheet_id = committed.json()["job"]["sheet_id"]
    project = client.app.state.workspace.get(pid)
    rows = project.visible_row_ids(sheet_id)
    media_column = next(
        c["id"] for c in project.columns(sheet_id) if c["name"] == "media"
    )
    media = project.get_values(sheet_id, media_column, rows)
    assert [media[row]["mime"] for row in rows] == [
        "application/pdf",
        "image/png",
        "text/plain",
    ]


def test_restart_after_commit_reads_the_consented_pages_once(flow, tmp_path):
    client, pid, provider = flow
    draft = _keep(client, pid, [("scan.pdf", _blank_pdf(300))])
    estimate = _estimate(client, draft).json()
    committed = _commit(client, draft, estimate["estimate"]["promise_set_hash"])
    assert committed.status_code == 202, committed.text
    ref = committed.json()["job"]["import_ref"]
    restarted = TestClient(create_app(tmp_path / "workspace", router=provider.router))
    _publish(restarted)
    session = _read_all(restarted, pid, ref)
    assert session["ocr_completed"] == 1 and session["failed_pages"] == 0
    assert provider.calls == 1
    assert _charged(restarted, pid) == (1, 1)


def test_paid_pages_under_the_allowance_are_read_without_asking(flow, monkeypatch):
    client, pid, provider = flow
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "5")
    draft = _keep(client, pid, [("scan.pdf", _blank_pdf(300))])
    estimate = _estimate(client, draft).json()
    assert estimate["known_pages"] == 1
    assert estimate["estimate"]["requires_confirmation"] is False
    assert estimate["estimate"]["billed_cost"] > 0
    committed = _commit(client, draft, None)
    assert committed.status_code == 202, committed.text
    _publish(client)
    session = _read_all(client, pid, committed.json()["job"]["import_ref"])
    assert session["ocr_completed"] == 1
    assert provider.calls == 1
    project = client.app.state.workspace.get(pid)
    sheet_id = committed.json()["job"]["sheet_id"]
    text_column = next(
        c["id"] for c in project.columns(sheet_id) if c["name"] == "text"
    )
    (row,) = project.visible_row_ids(sheet_id)
    assert project.get_values(sheet_id, text_column, [row])[row].strip() == "Read page"


def _sample(client, draft, selection):
    samples = client.get(f"{draft}/samples", params={"category": "missing"}).json()
    occurrence = samples["items"][0]["occurrence_id"]
    url = f"{draft}/files/{occurrence}/pages/1/ocr-preview"
    estimated = client.post(f"{url}/estimate", json={"ocr_selection": selection})
    assert estimated.status_code == 200, estimated.text
    started = client.post(
        url,
        json={
            "ocr_selection": selection,
            "confirmation": estimated.json()["estimate"]["promise_set_hash"],
        },
    )
    assert started.status_code == 202, started.text
    job_id = started.json()["job_id"]
    with controlled_time() as clock:
        clock.wait_until(
            lambda: any(
                job["job_id"] == job_id and job["status"] == "done"
                for job in client.get(draft).json()["preview_jobs"]
            ),
            message="the sample page was not read",
        )
    page = client.get(f"{draft}/files/{occurrence}/pages/1").json()
    assert page["ocr_text"].strip() == "Read page"


def _sample_then_import(client, pid, selection):
    draft = _keep(client, pid, [("scan.pdf", _blank_pdf(300), "application/pdf")])
    _choose(
        client,
        draft,
        organization="keep_documents",
        selection=selection,
    )
    first = _estimate(client, draft)
    assert first.status_code == 200, first.text
    assert first.json()["known_pages"] == 1
    _sample(client, draft, selection)
    assert _charged(client, pid) == (1, 1)
    again = _estimate(client, draft)
    assert again.status_code == 200, again.text
    # The sampled page is already read, so nothing is left to estimate.
    assert again.json()["known_pages"] == 0
    committed = _commit(client, draft, None)
    assert committed.status_code == 202, committed.text
    _publish(client)
    return committed.json()["job"]


def test_the_sampled_page_is_not_charged_twice(flow):
    client, pid, provider = flow
    job = _sample_then_import(client, pid, {"kind": "engine", "engine": PAID})
    project = client.app.state.workspace.get(pid)
    text_column = next(
        c["id"] for c in project.columns(job["sheet_id"]) if c["name"] == "text"
    )
    (row,) = project.visible_row_ids(job["sheet_id"])
    assert (
        project.get_values(job["sheet_id"], text_column, [row])[row].strip()
        == "Read page"
    )
    assert provider.calls == 1
    assert _charged(client, pid) == (1, 1)


def test_an_engine_and_model_choice_is_estimated_and_its_sample_reused(flow):
    client, pid, provider = flow
    selection = {"kind": "engine_model", "engine": "llm", "model": PAID}
    _sample_then_import(client, pid, selection)
    assert provider.calls == 1
    assert _charged(client, pid) == (1, 1)


def test_a_price_change_after_consent_refuses_the_import(flow, monkeypatch):
    class Retail:
        policy_id = "test.pdf-import-retail.v1"

        def rate(self, facts):
            return RatedEstimate(
                billed_cost=facts.provider_cost * 2,
                provider_cost=facts.provider_cost,
                lane="cost_plus",
                policy_id=self.policy_id,
            )

    client, pid, provider = flow
    draft = _keep(client, pid, [("scan.pdf", _blank_pdf(300))])
    estimate = _estimate(client, draft).json()
    monkeypatch.setattr(pricing_policy, "_INSTALLED_POLICY", Retail())
    refused = _commit(client, draft, estimate["estimate"]["promise_set_hash"])
    assert refused.status_code == 409, refused.text
    assert "changed" in refused.json()["detail"]
    assert provider.calls == 0
    assert client.app.state.workspace.get(pid).sheets() == []
    assert client.get(draft).json()["state"] == "open"


def test_a_paid_engine_with_no_page_to_read_needs_no_estimate(flow):
    client, pid, provider = flow
    draft = _keep(client, pid, [("typed.pdf", _text_pdf("Typed"))])
    committed = _commit(client, draft, None)
    assert committed.status_code == 202, committed.text
    _publish(client)
    assert provider.calls == 0
    job = committed.json()["job"]
    project = client.app.state.workspace.get(pid)
    text_column = next(
        c["id"] for c in project.columns(job["sheet_id"]) if c["name"] == "text"
    )
    (row,) = project.visible_row_ids(job["sheet_id"])
    assert (
        "Typed line 0" in project.get_values(job["sheet_id"], text_column, [row])[row]
    )


def test_paid_ocr_of_all_pages_skips_an_unreadable_pdf_but_keeps_its_row(flow):
    client, pid, provider = flow
    draft = _keep(
        client,
        pid,
        [("bad.pdf", b"%PDF-1.4 not really a pdf"), ("scan.pdf", _blank_pdf(300))],
        policy="all",
    )
    estimate = _estimate(client, draft).json()
    assert estimate["known_pages"] == 1 and estimate["selected_count"] == 1
    committed = _commit(client, draft, estimate["estimate"]["promise_set_hash"])
    assert committed.status_code == 202, committed.text
    _publish(client)
    job = committed.json()["job"]
    session = _read_all(client, pid, job["import_ref"])
    assert session["ocr_completed"] == 1
    project = client.app.state.workspace.get(pid)
    rows = project.visible_row_ids(job["sheet_id"])
    columns = {c["name"]: c["id"] for c in project.columns(job["sheet_id"])}
    names = project.get_values(job["sheet_id"], columns["filename"], rows)
    errors = project.get_values(job["sheet_id"], columns["error"], rows)
    assert [names[row] for row in rows] == ["bad.pdf", "scan.pdf"]
    assert errors[rows[0]] and not errors.get(rows[1])
    assert provider.calls == 1


def _legacy_commit(draft_dir, **commit):
    """Write a commit that keeps a commit inventory for uploads still to come."""
    manifest = draft_dir / "manifest.json"
    header = json.loads(manifest.read_text())
    header["state"] = "committing"
    header["commit"] = {
        "commit_id": "pdf-commit-legacy",
        "idempotency_key": "legacy",
        "requested_at": header["updated_at"],
        "manifest_hash": "0" * 64,
        "selected_through": 2,
        "selected_total": 2,
        "result": None,
        "ocr_authorization": None,
        "ready_notification": None,
        "commit_inventory_path": str(draft_dir / "commit-inventory.db"),
        "source_files_root": str(draft_dir / "files"),
        "promotion_target": None,
        "packet_publication": False,
        "committed_total": 2,
        **commit,
    }
    manifest.write_text(json.dumps(header))


def test_startup_settles_drafts_whose_commit_keeps_a_commit_inventory(
    flow,
    tmp_path,
):
    client, pid, provider = flow
    sessions = f"/api/projects/{pid}/import/files/sessions"
    created = client.post(sessions, json={"sheet_name": "Legacy"})
    ref = created.json()["import_ref"]
    uploaded = client.post(
        f"{sessions}/{ref}/files",
        files=[
            ("files", ("first.pdf", _blank_pdf(300), "application/pdf")),
            ("logical_paths", (None, "first.pdf")),
            ("batch_id", (None, "first")),
        ],
    )
    assert uploaded.status_code == 200, uploaded.text
    _publish(client)
    assert client.get(f"{sessions}/{ref}").json()["sealed"] is False

    published, unpublished, moved = (
        _draft(client, pid, [("a.pdf", _blank_pdf(301)), ("b.pdf", _blank_pdf(302))])
        for _ in range(3)
    )
    root = client.app.state.workspace.root / ".pdf_import_drafts"
    _legacy_commit(
        root / published.rsplit("/", 1)[1],
        result={"import_ref": ref, "sheet_id": 1, "receipt_id": "legacy"},
        promotion_target={"inventory_path": "x", "source_files_root": "y"},
        promoted=True,
    )
    _legacy_commit(root / unpublished.rsplit("/", 1)[1])
    _legacy_commit(
        root / moved.rsplit("/", 1)[1],
        promotion_target={"inventory_path": "x", "source_files_root": "y"},
    )

    restarted = TestClient(create_app(tmp_path / "workspace", router=provider.router))
    assert restarted.get(published).json()["state"] == "committed"
    reopened = restarted.get(unpublished).json()
    assert reopened["state"] == "open" and reopened["commit"] is None
    assert restarted.get(moved).json()["state"] == "cancelled"
    listed = restarted.get(f"/api/projects/{pid}/import/pdf-imports").json()["items"]
    assert [item["draft_id"] for item in listed] == [unpublished.rsplit("/", 1)[1]]
    _publish(restarted)
    session = restarted.get(f"{sessions}/{ref}").json()
    assert session["sealed"] is True and session["state"] == "completed"
    assert session["committed_rows"] == 1
    # The reopened draft publishes through the packet import like any other.
    _choose(restarted, unpublished, organization="keep_documents", policy="none")
    committed = _commit(restarted, unpublished, None, key="again")
    assert committed.status_code == 202, committed.text


def _page_work(client, pid, ref):
    path = client.app.state.workspace.get(pid).path / ".imports" / ref / "inventory.db"
    return sqlite3.connect(path)


def _interrupt(client, pid, ref):
    """Leave the import's page as a restart does: running, its attempt gone."""
    with _page_work(client, pid, ref) as db:
        db.execute(
            "UPDATE pdf_split_page_work SET status='running',error=NULL,job_id='gone'"
        )


def _retry(client, pid, ref, *, confirm=True):
    sessions = f"/api/projects/{pid}/import/files/sessions/{ref}"
    estimate = client.post(f"{sessions}/preparation/retry/estimate")
    assert estimate.status_code == 200, estimate.text
    body = estimate.json()
    hash_ = (body["estimate"] or {}).get("promise_set_hash")
    response = client.post(
        f"{sessions}/preparation/retry",
        json={
            "plan_token": body["plan_token"],
            "confirmation": hash_ if confirm else None,
        },
    )
    return body, response


def _settle(client, pid, ref, *, read):
    with controlled_time() as clock:
        session = clock.wait_until(
            lambda: (
                lambda value: (
                    value
                    if _settled(value) and value["ocr_completed"] == read
                    else None
                )
            )(_session(client, pid, ref)),
            message="page text did not settle",
        )
    _session(client, pid, ref)
    return session


@pytest.mark.parametrize("stored", [False, True])
def test_a_page_interrupted_by_a_restart_is_retryable_and_charged_once(flow, stored):
    client, pid, provider = flow
    if not stored:
        provider.failures = 1
    ref, _sheet, _estimate = _import(client, pid, [("scan.pdf", _blank_pdf(300))])
    _settle(client, pid, ref, read=0 if not stored else 1)
    # A stored reading survives the restart; an unstored one is lost with it.
    _interrupt(client, pid, ref)
    session = _settle(client, pid, ref, read=0)
    assert session["failed_pages"] == 1
    (notice,) = [n for n in _notifications(client, pid) if n["severity"] == "warning"]
    assert notice["title"] == "Import finished with 1 page not read"
    assert not [n for n in _notifications(client, pid) if n["severity"] == "info"]

    estimate, retried = _retry(client, pid, ref)
    assert retried.status_code == 200, retried.text
    assert estimate["pages"] == (0 if stored else 1)
    session = _settle(client, pid, ref, read=1)
    assert session["failed_pages"] == 0
    assert _charged(client, pid) == (1, 1)


def test_retried_pages_are_read_under_the_retry_consent_not_the_import_consent(
    flow, monkeypatch
):
    class Retail:
        policy_id = "test.pdf-import-retail.v1"

        def rate(self, facts):
            return RatedEstimate(
                billed_cost=facts.provider_cost * 2,
                provider_cost=facts.provider_cost,
                lane="cost_plus",
                policy_id=self.policy_id,
            )

    client, pid, provider = flow
    provider.failures = 1
    ref, _sheet, original = _import(client, pid, [("scan.pdf", _blank_pdf(300))])
    _settle(client, pid, ref, read=0)
    # The price changed since the import was consented, so only the retry's
    # own consent can cover reading the page again.
    monkeypatch.setattr(pricing_policy, "_INSTALLED_POLICY", Retail())
    estimate, retried = _retry(client, pid, ref)
    assert retried.status_code == 200, retried.text
    assert (
        estimate["estimate"]["billed_cost"] == 2 * original["estimate"]["billed_cost"]
    )
    assert _settle(client, pid, ref, read=1)["failed_pages"] == 0
    assert provider.calls == 2


def test_a_retry_consent_authorizes_its_retry_once_and_only_its_pages(flow):
    client, pid, provider = flow
    provider.failures = 1
    files = [("first.pdf", _blank_pdf(300)), ("second.pdf", _blank_pdf(301))]
    ref, _sheet, _estimate = _import(client, pid, files)
    _settle(client, pid, ref, read=1)
    sessions = f"/api/projects/{pid}/import/files/sessions/{ref}"
    first = client.post(f"{sessions}/preparation/retry/estimate").json()

    # The set of pages not read changes after the estimate: its token is void.
    with _page_work(client, pid, ref) as db:
        db.execute(
            "UPDATE pdf_split_page_work SET status='failed' WHERE status='completed'"
        )
    stale = client.post(
        f"{sessions}/preparation/retry",
        json={
            "plan_token": first["plan_token"],
            "confirmation": first["estimate"]["promise_set_hash"],
        },
    )
    assert stale.status_code == 409, stale.text
    with _page_work(client, pid, ref) as db:
        db.execute(
            "UPDATE pdf_split_page_work SET status='completed' WHERE rowid=("
            "SELECT MIN(rowid) FROM pdf_split_page_work WHERE status='failed' "
            "AND prepared_ref_id IS NOT NULL)"
        )

    provider.failures = 1
    accepted, retried = _retry(client, pid, ref)
    assert retried.status_code == 200, retried.text
    _settle(client, pid, ref, read=1)
    calls = provider.calls
    # The retry failed again; its consent does not authorize another one.
    reused = client.post(
        f"{sessions}/preparation/retry",
        json={
            "plan_token": accepted["plan_token"],
            "confirmation": accepted["estimate"]["promise_set_hash"],
        },
    )
    assert reused.status_code == 409, reused.text
    assert provider.calls == calls
    fresh, again = _retry(client, pid, ref)
    assert fresh["plan_token"] != accepted["plan_token"]
    assert again.status_code == 200, again.text
    assert _settle(client, pid, ref, read=2)["failed_pages"] == 0


def test_a_page_retried_for_its_cached_reading_is_never_read_without_consent(
    flow, monkeypatch
):

    client, pid, provider = flow
    provider.failures = 1
    ref, _sheet, _estimate = _import(client, pid, [("scan.pdf", _blank_pdf(300))])
    _settle(client, pid, ref, read=0)
    calls = provider.calls
    seen = []

    def vanishing_cache(self, target):
        # Cached while the retry is estimated and decided, gone at dispatch.
        seen.append(target)
        return 1 if len(seen) <= 3 else None

    monkeypatch.setattr(PdfSplitPreparationAdapter, "cached_reference", vanishing_cache)
    estimate, retried = _retry(client, pid, ref, confirm=False)
    assert estimate["pages"] == 0
    assert retried.status_code == 200, retried.text
    monkeypatch.undo()
    session = _settle(client, pid, ref, read=0)
    assert session["failed_pages"] == 1
    assert provider.calls == calls


def test_an_import_notification_that_cannot_be_recorded_never_fails_the_import(
    flow, monkeypatch
):
    failures = []
    emit = pdf_split_preparation_jobs.emit_notification_candidate

    def broken(*args, **kwargs):
        if not failures:
            failures.append(True)
            raise RuntimeError("notification store unavailable")
        return emit(*args, **kwargs)

    client, pid, provider = flow
    provider.failures = 1
    monkeypatch.setattr(
        pdf_split_preparation_jobs, "emit_notification_candidate", broken
    )
    ref, _sheet, _estimate = _import(client, pid, [("scan.pdf", _blank_pdf(300))])
    session = _settle(client, pid, ref, read=0)
    assert session["failed_pages"] == 1 and session["error"]
    assert failures == [True]
    # The outcome is recorded on a later pass once the store accepts it.
    (notice,) = _notifications(client, pid)
    assert notice["severity"] == "warning"
    _estimate_body, retried = _retry(client, pid, ref)
    assert retried.status_code == 200, retried.text
    assert _settle(client, pid, ref, read=1)["failed_pages"] == 0


def _admitting(client, monkeypatch):
    """Admit every page attempt as a child purchase, as a hosted deployment does."""
    admitted = []

    def admit_import_child(**facts):
        admitted.append((facts["child_key"], facts["receipt_id"]))
        return nullcontext(None)

    monkeypatch.setattr(
        pdf_split_preparation_jobs, "import_execution_context", lambda *_args: None
    )
    client.app.state.workspace.import_child_admission_port = SimpleNamespace(
        admit_import_child=admit_import_child
    )
    return admitted


@pytest.mark.parametrize(
    ("ended", "same_identity"),
    [("failed", False), ("interrupted_after_start", False), ("interrupted", True)],
)
def test_a_retried_page_is_a_new_purchase_unless_its_attempt_can_be_replayed(
    flow, monkeypatch, ended, same_identity
):
    client, pid, provider = flow
    admitted = _admitting(client, monkeypatch)
    provider.failures = 1
    ref, _sheet, _estimate = _import(client, pid, [("scan.pdf", _blank_pdf(300))])
    _settle(client, pid, ref, read=0)
    project = client.app.state.workspace.get(pid)
    if ended != "failed":
        _interrupt(client, pid, ref)
        _settle(client, pid, ref, read=0)
    if ended == "interrupted":
        # The restart came before the attempt's receipt was written, so its
        # admission is all there is and the retry replays it.
        with sqlite3.connect(project.path / "project.db") as db:
            for table in ("routes", "promise_sets", "consents"):
                db.execute(
                    f"DELETE FROM {table} WHERE subject_kind='receipt' AND subject_id=?",
                    (admitted[0][1],),
                )
            db.execute("DELETE FROM receipts WHERE id=?", (admitted[0][1],))
    estimate, retried = _retry(client, pid, ref)
    assert retried.status_code == 200, retried.text
    _settle(client, pid, ref, read=1)
    (first, second) = admitted
    assert (first == second) is same_identity
    assert (
        project.db.execute(
            "SELECT status FROM receipts WHERE id=?", (second[1],)
        ).fetchone()[0]
        == "completed"
    )


def test_an_import_undone_while_its_pages_are_read_has_nothing_to_retry(flow):
    client, pid, provider = flow
    provider.gate = threading.Semaphore(0)
    provider.failures = 1
    files = [(f"scan-{index}.pdf", _blank_pdf(300 + index)) for index in range(3)]
    ref, _sheet, _estimate = _import(client, pid, files)
    with controlled_time() as clock:
        clock.wait_until(
            lambda: len(_session(client, pid, ref)["reading_row_ids"]) == 3,
            message="pages did not start reading",
        )
    # One page fails before the import is undone; the others are mid-read.
    provider.gate.release(1)

    def failed():
        with _page_work(client, pid, ref) as db:
            return db.execute(
                "SELECT COUNT(*) FROM pdf_split_page_work WHERE status='failed'"
            ).fetchone()[0]

    with controlled_time() as clock:
        clock.wait_until(failed, message="the first page did not fail")
    client.app.state.workspace.get(pid).undo()
    provider.gate.release(2)
    with controlled_time() as clock:
        clock.wait_until(
            lambda: (
                lambda value: (
                    value
                    if not value["reading_row_ids"] and not value["waiting_row_ids"]
                    else None
                )
            )(_session(client, pid, ref)),
            message="undone pages did not settle",
        )
    session = _session(client, pid, ref)
    assert session["failed_pages"] == 0 and session["error"] is None
    assert _notifications(client, pid) == []
    sessions = f"/api/projects/{pid}/import/files/sessions/{ref}"
    refused = client.post(f"{sessions}/preparation/retry/estimate")
    assert refused.status_code == 409, refused.text


def test_an_import_outcome_is_recorded_by_recovery_without_a_status_read(
    flow, monkeypatch
):
    client, pid, provider = flow
    failures = []
    emit = pdf_split_preparation_jobs.emit_notification_candidate

    def store_unavailable_once(*args, **kwargs):
        if not failures:
            failures.append(True)
            raise RuntimeError("notification store unavailable")
        return emit(*args, **kwargs)

    monkeypatch.setattr(
        pdf_split_preparation_jobs,
        "emit_notification_candidate",
        store_unavailable_once,
    )
    provider.failures = 1
    ref, _sheet, _estimate = _import(client, pid, [("scan.pdf", _blank_pdf(300))])

    def page_settled():
        with _page_work(client, pid, ref) as db:
            return db.execute(
                "SELECT COUNT(*) FROM pdf_split_page_work WHERE status='failed'"
            ).fetchone()[0]

    with controlled_time() as clock:
        clock.wait_until(page_settled, message="the page was not read")
    # Recovery ticks with no browser open: the first cannot record the outcome.
    recovery = SearchPreparationService(client.app.state.workspace)
    recovery.recover()
    assert failures == [True]
    assert _notifications(client, pid) == []
    recovery.recover()
    (notice,) = _notifications(client, pid)
    assert notice["title"] == "Import finished with 1 page not read"


def test_a_retry_spends_its_consent_before_it_resets_any_page(flow, monkeypatch):
    client, pid, provider = flow
    provider.failures = 2
    ref, _sheet, _estimate = _import(client, pid, [("scan.pdf", _blank_pdf(300))])
    _settle(client, pid, ref, read=0)
    sessions = f"/api/projects/{pid}/import/files/sessions/{ref}"
    shown = client.post(f"{sessions}/preparation/retry/estimate").json()
    consent = {
        "plan_token": shown["plan_token"],
        "confirmation": shown["estimate"]["promise_set_hash"],
    }
    write = pdf_split_preparation_jobs.write_import_header
    crashed = []

    def crash_on_spending_the_retry(directory, header):
        if (header.provider_state or {}).get("retries") and not crashed:
            crashed.append(True)
            raise OSError("disk full")
        return write(directory, header)

    monkeypatch.setattr(
        pdf_split_preparation_jobs, "write_import_header", crash_on_spending_the_retry
    )
    with pytest.raises(OSError):
        client.post(f"{sessions}/preparation/retry", json=consent)
    monkeypatch.undo()
    _session(client, pid, ref)
    # Nothing was reset, so nothing was read; the consent is still unspent.
    assert provider.calls == 1
    with _page_work(client, pid, ref) as db:
        assert db.execute("SELECT status FROM pdf_split_page_work").fetchone()[0] == (
            "failed"
        )
    assert client.post(f"{sessions}/preparation/retry", json=consent).status_code == 200
    _settle(client, pid, ref, read=0)
    reused = client.post(f"{sessions}/preparation/retry", json=consent)
    assert reused.status_code == 409, reused.text
    assert provider.calls == 2


def test_a_retried_page_with_a_stored_reading_is_published_from_it(flow, monkeypatch):
    client, pid, provider = flow
    ref, _sheet, _estimate = _import(client, pid, [("scan.pdf", _blank_pdf(300))])
    _settle(client, pid, ref, read=1)
    # The import keeps a reading of the page from an earlier generation, but
    # this generation's attempt failed.
    with _page_work(client, pid, ref) as db:
        db.execute(
            "INSERT INTO pdf_split_page_work (work_key,generation,operation_id,"
            "target_json,status,prepared_ref_id) SELECT work_key,'earlier',operation_id,"
            "target_json,'completed',prepared_ref_id FROM pdf_split_page_work"
        )
        db.execute(
            "UPDATE pdf_split_page_work SET status='failed',prepared_ref_id=NULL "
            "WHERE generation<>'earlier'"
        )
        db.execute(
            "UPDATE pdf_split_page_bindings SET published=-1 WHERE generation<>'earlier'"
        )
    monkeypatch.setattr(
        PdfSplitPreparationAdapter, "cached_reference", lambda _self, _target: None
    )
    estimate, retried = _retry(client, pid, ref, confirm=False)
    assert estimate["pages"] == 0
    assert retried.status_code == 200, retried.text
    assert _settle(client, pid, ref, read=1)["failed_pages"] == 0
    assert provider.calls == 1


def test_a_page_that_bounces_off_a_busy_engine_keeps_its_attempt(flow):
    client, pid, _provider = flow
    ref, _sheet, _estimate = _import(client, pid, [("scan.pdf", _blank_pdf(300))])
    _settle(client, pid, ref, read=1)
    path = client.app.state.workspace.get(pid).path / ".imports" / ref / "inventory.db"
    with PdfSplitPreparationStore(path) as store:
        (work,) = store.db.execute("SELECT * FROM pdf_split_page_work").fetchall()
        page = store._work(work)
        with store.db:
            store.db.execute("UPDATE pdf_split_page_work SET status='pending'")
        for _bounce in range(2):
            assert store.claim(page.work_key, generation=page.generation)
            store.release(page)
        assert (
            store.db.execute("SELECT attempt FROM pdf_split_page_work").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("stage", ["commit", "retry"])
def test_the_allowance_covers_an_import_by_its_whole_estimate_not_each_page(
    flow, monkeypatch, stage
):
    client, pid, provider = flow
    files = [(f"scan-{index}.pdf", _blank_pdf(300 + index)) for index in range(3)]
    draft = _keep(client, pid, files)
    whole = _estimate(client, draft).json()["estimate"]["cost"]
    # Each page is under the allowance, the import is over it.
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", str(whole / 3 * 1.5))
    if stage == "commit":
        estimate = _estimate(client, draft).json()["estimate"]
        assert estimate["requires_confirmation"] is True
        refused = _commit(client, draft, None)
        assert refused.status_code == 409, refused.text
        return
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    provider.failures = 3
    committed = _commit(
        client, draft, _estimate(client, draft).json()["estimate"]["promise_set_hash"]
    )
    assert committed.status_code == 202, committed.text
    _publish(client)
    ref = committed.json()["job"]["import_ref"]
    _settle(client, pid, ref, read=0)
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", str(whole / 3 * 1.5))
    sessions = f"/api/projects/{pid}/import/files/sessions/{ref}"
    shown = client.post(f"{sessions}/preparation/retry/estimate").json()
    assert shown["pages"] == 3 and shown["estimate"]["requires_confirmation"] is True
    refused = client.post(
        f"{sessions}/preparation/retry",
        json={"plan_token": shown["plan_token"], "confirmation": None},
    )
    assert refused.status_code == 409, refused.text


def test_a_draft_that_cannot_be_settled_never_stops_startup_and_settles_later(
    flow, tmp_path, monkeypatch
):
    client, pid, provider = flow
    sessions = f"/api/projects/{pid}/import/files/sessions"
    ref = client.post(sessions, json={"sheet_name": "Legacy"}).json()["import_ref"]
    client.post(
        f"{sessions}/{ref}/files",
        files=[
            ("files", ("first.pdf", _blank_pdf(300), "application/pdf")),
            ("logical_paths", (None, "first.pdf")),
            ("batch_id", (None, "first")),
        ],
    )
    _publish(client)
    published, corrupt, healthy = (
        _draft(client, pid, [("a.pdf", _blank_pdf(301))]) for _ in range(3)
    )
    root = client.app.state.workspace.root / ".pdf_import_drafts"
    _legacy_commit(
        root / published.rsplit("/", 1)[1],
        result={"import_ref": ref, "sheet_id": 1, "receipt_id": "legacy"},
        promotion_target={"inventory_path": "x", "source_files_root": "y"},
        promoted=True,
    )
    _legacy_commit(root / corrupt.rsplit("/", 1)[1])
    (root / corrupt.rsplit("/", 1)[1] / "inventory.db").write_bytes(b"not sqlite" * 64)
    _legacy_commit(root / healthy.rsplit("/", 1)[1])

    def seal_unavailable(_inventory):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ImportInventory, "seal", seal_unavailable)
    started = TestClient(create_app(tmp_path / "workspace", router=provider.router))
    # Each draft settles on its own: the healthy one reopens, the others wait.
    assert started.get(healthy).json()["state"] == "open"
    for draft in (published, corrupt):
        header = json.loads(
            (root / draft.rsplit("/", 1)[1] / "manifest.json").read_text()
        )
        assert header["state"] == "error" and header["commit"] is not None
    assert started.get(f"{sessions}/{ref}").json()["sealed"] is False
    # Expired drafts are purged at startup, but not one whose import is unsealed.
    for draft in (published, corrupt):
        manifest = root / draft.rsplit("/", 1)[1] / "manifest.json"
        header = json.loads(manifest.read_text())
        header["expires_at"] = "2000-01-01T00:00:00Z"
        manifest.write_text(json.dumps(header))

    monkeypatch.undo()
    restarted = TestClient(create_app(tmp_path / "workspace", router=provider.router))
    assert restarted.get(published).json()["state"] == "committed"
    _publish(restarted)
    session = restarted.get(f"{sessions}/{ref}").json()
    assert session["sealed"] is True and session["state"] == "completed"
    header = json.loads(
        (root / corrupt.rsplit("/", 1)[1] / "manifest.json").read_text()
    )
    assert header["state"] == "error"


def test_a_retry_whose_price_changed_after_its_estimate_is_refused(flow, monkeypatch):
    class Retail:
        policy_id = "test.pdf-import-retail.v1"

        def rate(self, facts):
            return RatedEstimate(
                billed_cost=facts.provider_cost * 2,
                provider_cost=facts.provider_cost,
                lane="cost_plus",
                policy_id=self.policy_id,
            )

    client, pid, provider = flow
    provider.failures = 1
    ref, _sheet, _estimate = _import(client, pid, [("scan.pdf", _blank_pdf(300))])
    _settle(client, pid, ref, read=0)
    sessions = f"/api/projects/{pid}/import/files/sessions/{ref}"
    shown = client.post(f"{sessions}/preparation/retry/estimate").json()
    monkeypatch.setattr(pricing_policy, "_INSTALLED_POLICY", Retail())
    refused = client.post(
        f"{sessions}/preparation/retry",
        json={
            "plan_token": shown["plan_token"],
            "confirmation": shown["estimate"]["promise_set_hash"],
        },
    )
    # The consent was to the estimate as shown; the current one asks again.
    assert refused.status_code == 409, refused.text
    assert "exact claims" in refused.json()["detail"]
    assert provider.calls == 1


def test_pages_read_on_this_machine_leave_one_receipt_for_their_pass(flow):
    pytest.importorskip("rapidocr")
    client, pid, _provider = flow
    files = [("first.pdf", _blank_pdf(300)), ("second.pdf", _blank_pdf(301))]
    draft = _draft(client, pid, files)
    _choose(client, draft, selection={"kind": "engine", "engine": "rapidocr"})
    estimate = _estimate(client, draft).json()["estimate"]
    assert estimate["requires_confirmation"] is False
    committed = _commit(client, draft, None)
    assert committed.status_code == 202, committed.text
    _publish(client)
    ref = committed.json()["job"]["import_ref"]
    session = _read_all(client, pid, ref)
    assert session["ocr_completed"] == 2
    # The settled pass is recorded by the progress pass after it settles.
    _session(client, pid, ref)
    project = client.app.state.workspace.get(pid)
    (receipt,) = project.db.execute(
        "SELECT id,status,body FROM receipts WHERE action_kind='media.ocr'"
    ).fetchall()
    body = json.loads(receipt["body"])
    assert receipt["status"] == "completed"
    (use,) = body["provider_use"]
    assert use["provider"] == "local" and use["external_api"] is False
    assert use["engine"] == "rapidocr" and use["cost_actual"] == 0.0
    assert body["value"]["settings"]["engine"] == "rapidocr"
    pages = body["value"]["pages"]
    assert [page["status"] for page in pages] == ["completed", "completed"]
    assert sorted(page["file"] for page in pages) == ["first.pdf", "second.pdf"]
    assert all(len(page["rows"]) == 1 for page in pages)
    with _page_work(client, pid, ref) as db:
        assert {
            row[0] for row in db.execute("SELECT receipt_id FROM pdf_split_page_work")
        } == {receipt["id"]}
    # Nothing left the machine and nothing was charged.
    assert _charged(client, pid) == (0, 0)
    _session(client, pid, ref)
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM receipts WHERE action_kind='media.ocr'"
        ).fetchone()[0]
        == 1
    )


def test_workspace_diagnostics_count_drafts_no_project_can_list(flow):
    client, pid, _provider = flow
    draft = _draft(client, pid, [("scan.pdf", _blank_pdf(300))])
    root = client.app.state.workspace.root / ".pdf_import_drafts"
    (root / draft.rsplit("/", 1)[1] / "manifest.json").write_text("{not json")
    report = client.get("/api/diagnose").json()["info"]["import_drafts"]
    assert report["unreadable"] == 1
    project = client.get(f"/api/projects/{pid}/diagnose").json()["info"]
    assert "import_drafts" not in project


def test_a_paid_page_never_gets_a_local_receipt_and_keeps_its_own(flow, monkeypatch):
    client, pid, provider = flow
    provider.failures = 1
    record = PdfSplitPreparationStore.record_started

    def recorded_after_the_page_failed(store, work, **facts):
        # The attempt's facts land only once the page has already failed.
        if facts.get("receipt_id") is not None:
            with controlled_time() as clock:
                clock.wait_until(
                    lambda: (
                        store.db.execute(
                            "SELECT status FROM pdf_split_page_work WHERE work_key=? "
                            "AND generation=?",
                            (work.work_key, work.generation),
                        ).fetchone()[0]
                        == "failed"
                    ),
                    message="the paid page did not fail",
                )
        return record(store, work, **facts)

    monkeypatch.setattr(
        PdfSplitPreparationStore, "record_started", recorded_after_the_page_failed
    )
    ref, _sheet, _estimate = _import(client, pid, [("scan.pdf", _blank_pdf(300))])
    _settle(client, pid, ref, read=0)
    project = client.app.state.workspace.get(pid)
    with _page_work(client, pid, ref) as db:
        (receipt_id,) = db.execute(
            "SELECT receipt_id FROM pdf_split_page_work"
        ).fetchone()
    assert tuple(
        project.db.execute(
            "SELECT action_kind,status FROM receipts WHERE id=?", (receipt_id,)
        ).fetchone()
    ) == ("media.ocr", "failed")
    # Even a paid page that lost its receipt link is never called local.
    with _page_work(client, pid, ref) as db:
        db.execute("UPDATE pdf_split_page_work SET receipt_id=NULL")
    _session(client, pid, ref)
    bodies = [
        json.loads(row[0])
        for row in project.db.execute(
            "SELECT body FROM receipts WHERE action_kind='media.ocr'"
        )
    ]
    assert not [
        use
        for body in bodies
        for use in body["provider_use"]
        if use.get("external_api") is False
    ]


def test_discarding_an_unsettled_draft_seals_what_its_import_published(
    flow, tmp_path, monkeypatch
):
    client, pid, provider = flow
    sessions = f"/api/projects/{pid}/import/files/sessions"
    ref = client.post(sessions, json={"sheet_name": "Legacy"}).json()["import_ref"]
    client.post(
        f"{sessions}/{ref}/files",
        files=[
            ("files", ("first.pdf", _blank_pdf(300), "application/pdf")),
            ("logical_paths", (None, "first.pdf")),
            ("batch_id", (None, "first")),
        ],
    )
    _publish(client)
    draft = _draft(client, pid, [("a.pdf", _blank_pdf(301))])
    root = client.app.state.workspace.root / ".pdf_import_drafts"
    _legacy_commit(
        root / draft.rsplit("/", 1)[1],
        result={"import_ref": ref, "sheet_id": 1, "receipt_id": "legacy"},
        promotion_target={"inventory_path": "x", "source_files_root": "y"},
        promoted=True,
    )

    def seal_unavailable(_inventory):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ImportInventory, "seal", seal_unavailable)
    started = TestClient(create_app(tmp_path / "workspace", router=provider.router))
    assert started.get(draft).json()["state"] == "error"
    monkeypatch.undo()
    # Discarding the draft settles what it already published, then cancels.
    discarded = started.delete(draft)
    assert discarded.status_code == 202, discarded.text
    assert discarded.json()["state"] == "cancelled"
    _publish(started)
    session = started.get(f"{sessions}/{ref}").json()
    assert session["sealed"] is True and session["state"] == "completed"
    deleted = started.delete(f"/api/projects/{pid}/sheets/{session['sheet_id']}")
    assert deleted.status_code == 200, deleted.text


@pytest.mark.parametrize("recording", ["works", "fails"])
def test_text_read_from_a_pdf_leaves_its_own_receipt(flow, monkeypatch, recording):
    client, pid, provider = flow
    if recording == "fails":

        def store_unavailable(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(pdf_split_job, "record_local_reading", store_unavailable)
    draft = _draft(client, pid, [("letter.pdf", _text_pdf("Dear editor"))])
    _choose(client, draft, policy="none")
    committed = _commit(client, draft, None)
    assert committed.status_code == 202, committed.text
    _publish(client)
    project = client.app.state.workspace.get(pid)
    kinds = sorted(
        tuple(row)
        for row in project.db.execute("SELECT action_kind,status FROM receipts")
    )
    jobs = {
        (job.kind, job.status) for job in client.app.state.workspace.queue.list_jobs()
    }
    assert ("pdf.split.apply", "done") in jobs
    if recording == "fails":
        # The import is published and complete without the reading receipt.
        assert kinds == [("import.packet_split", "completed")]
        return
    assert kinds == [
        ("import.packet_split", "completed"),
        ("media.read_pdf_text", "completed"),
    ]
    body = json.loads(
        project.db.execute(
            "SELECT body FROM receipts WHERE action_kind='media.read_pdf_text'"
        ).fetchone()[0]
    )
    (use,) = body["provider_use"]
    assert use["provider"] == "local" and use["external_api"] is False
    assert use["cost_actual"] == 0.0
    assert body["value"]["settings"] == {
        "method": "pdf_text_layer",
        "reader": "pdfium",
    }
    (document,) = body["value"]["pages"]
    assert document["file"] == "letter.pdf" and document["pages"] == [1]
    (row,) = project.visible_row_ids(committed.json()["job"]["sheet_id"])
    assert document["rows"] == [row]
    assert provider.calls == 0


def test_a_local_reading_receipt_that_cannot_be_named_waits_for_a_later_pass(
    flow, monkeypatch
):
    pytest.importorskip("rapidocr")
    client, pid, _provider = flow

    def unavailable(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(
        pdf_split_preparation_jobs, "local_reading_receipt_id", unavailable
    )
    draft = _draft(client, pid, [("first.pdf", _blank_pdf(300))])
    _choose(client, draft, selection={"kind": "engine", "engine": "rapidocr"})
    committed = _commit(client, draft, None)
    assert committed.status_code == 202, committed.text
    _publish(client)
    ref = committed.json()["job"]["import_ref"]
    _read_all(client, pid, ref)
    workspace = client.app.state.workspace
    split = pdf_split_preparation_jobs.PdfSplitPreparationJobs(
        workspace, preview_runs=ActionPreviewRunService(workspace)
    )
    # The receipt is still owed, so the import is not yet settled for recovery.
    assert split.progress(pid, ref) == 0
    monkeypatch.undo()
    assert split.progress(pid, ref) > 0
    assert (
        workspace.get(pid)
        .db.execute("SELECT COUNT(*) FROM receipts WHERE action_kind='media.ocr'")
        .fetchone()[0]
        == 1
    )


@pytest.mark.parametrize(
    ("statuses", "status"),
    [
        ({"completed"}, "completed"),
        ({"completed", "failed"}, "partial"),
        ({"failed", "cancelled"}, "failed"),
        ({"cancelled"}, "cancelled"),
    ],
)
def test_a_reading_pass_nobody_finished_is_cancelled_not_failed(statuses, status):
    assert local_reading_status(statuses) == status


def test_a_retry_waits_while_its_sheet_is_deleted(flow):
    client, pid, provider = flow
    provider.failures = 1
    ref, sheet_id, _estimate = _import(client, pid, [("scan.pdf", _blank_pdf(300))])
    _settle(client, pid, ref, read=0)
    sessions = f"/api/projects/{pid}/import/files/sessions/{ref}"
    shown = client.post(f"{sessions}/preparation/retry/estimate").json()
    deleted = client.delete(f"/api/projects/{pid}/sheets/{sheet_id}")
    assert deleted.status_code == 200, deleted.text
    refused = client.post(
        f"{sessions}/preparation/retry",
        json={
            "plan_token": shown["plan_token"],
            "confirmation": shown["estimate"]["promise_set_hash"],
        },
    )
    assert refused.status_code == 409, refused.text
    assert "sheet was deleted" in refused.json()["detail"]
    assert client.post(f"{sessions}/preparation/retry/estimate").status_code == 409
    assert provider.calls == 1
    client.app.state.workspace.get(pid).undo()
    _estimate_body, retried = _retry(client, pid, ref)
    assert retried.status_code == 200, retried.text
    assert _settle(client, pid, ref, read=1)["failed_pages"] == 0
    assert provider.calls == 2


def test_an_owed_import_outcome_is_warned_about_once(flow, monkeypatch, caplog):
    client, pid, provider = flow

    def store_unavailable(*_args, **_kwargs):
        raise RuntimeError("notification store unavailable")

    monkeypatch.setattr(
        pdf_split_preparation_jobs, "emit_notification_candidate", store_unavailable
    )
    provider.failures = 1
    ref, _sheet, _estimate = _import(client, pid, [("scan.pdf", _blank_pdf(300))])
    _settle(client, pid, ref, read=0)
    workspace = client.app.state.workspace
    split = pdf_split_preparation_jobs.PdfSplitPreparationJobs(
        workspace, preview_runs=ActionPreviewRunService(workspace)
    )
    with caplog.at_level("DEBUG"):
        caplog.clear()
        for _tick in range(3):
            assert split.progress(pid, ref) == 0
    owed = [
        r for r in caplog.records if r.getMessage() == "import_notification_emit_failed"
    ]
    assert [r.levelname for r in owed] == ["WARNING", "DEBUG", "DEBUG"]
