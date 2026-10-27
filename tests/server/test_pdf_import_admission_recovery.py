import hashlib
import io

import pytest
from pypdf import PdfWriter

from frisket.server.services.pdf_import_commit import PdfImportCommitService
from frisket.server.services.pdf_imports.service import PdfImportService
from frisket.server.services.pdf_imports.storage import PdfImportDraftError
from frisket.server.workspace import Workspace


def _frozen_draft(tmp_path):
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    pid = workspace.create("Recovery", project_id="recovery")["id"]
    project = workspace.get(pid)
    sheet = project.add_sheet("Destination")
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=200)
    output = io.BytesIO()
    writer.write(output)
    data = output.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    staging = tmp_path / "staging"
    (staging / "files").mkdir(parents=True)
    (staging / "files" / digest).write_bytes(data)
    service = PdfImportService(workspace)
    status = service.adopt_staged(
        pid,
        staging,
        [
            {
                "logical_path": "source.pdf",
                "filename": "source.pdf",
                "mime": "application/pdf",
                "size": len(data),
                "sha256": digest,
                "path": f"files/{digest}",
            }
        ],
    )
    status = service.analyze_staged(pid, status["draft_id"])
    status = service.update_choices(
        pid,
        status["draft_id"],
        expected_revision=status["revision"],
        **{
            **status["choices"],
            "organization": "pages",
            "ocr_policy": "none",
            "semantic_search": False,
            "destination": {"kind": "existing", "sheet_id": sheet},
        },
    )
    status = service.match_destination(pid, status["draft_id"])
    frozen = service.freeze_for_commit(
        pid,
        status["draft_id"],
        idempotency_key="deleted-target",
        expected_revision=status["revision"],
    )
    return workspace, pid, sheet, service, frozen


def test_deleted_destination_reopens_organized_draft_for_discard(tmp_path):
    workspace, pid, sheet, service, frozen = _frozen_draft(tmp_path)
    project = workspace.get(pid)
    project.delete_sheet(sheet)
    with pytest.raises(PdfImportDraftError, match="destination.*unavailable") as exc:
        PdfImportCommitService(workspace, intake=service).commit_frozen(pid, frozen)
    assert exc.value.status_code == 409
    assert service.status(pid, frozen.draft_id)["state"] == "open"
    assert not list((project.path / ".imports").glob("*/pdf-split.json"))
    assert service.discard(pid, frozen.draft_id)["state"] == "cancelled"


def test_failed_handoff_preserves_admitted_plan_for_replay(tmp_path, monkeypatch):
    workspace, pid, _sheet, service, frozen = _frozen_draft(tmp_path)
    committer = PdfImportCommitService(workspace, intake=service)
    record = service.record_commit_handoff

    def fail_handoff(*args, **kwargs):
        raise OSError("handoff persistence unavailable")

    monkeypatch.setattr(service, "record_commit_handoff", fail_handoff)
    with pytest.raises(OSError, match="handoff persistence"):
        committer.commit_frozen(pid, frozen)
    project = workspace.get(pid)
    plans = list((project.path / ".imports").glob("*/pdf-split.json"))
    assert len(plans) == 1
    assert service.status(pid, frozen.draft_id)["state"] == "committing"
    monkeypatch.setattr(service, "record_commit_handoff", record)
    result = committer.commit_frozen(pid, frozen)
    assert result.import_ref == plans[0].parent.name
    assert service.status(pid, frozen.draft_id)["state"] == "committed"
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM ops WHERE kind='pdf.split'"
        ).fetchone()[0]
        == 1
    )
