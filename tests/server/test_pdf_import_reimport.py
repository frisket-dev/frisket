from __future__ import annotations

import pytest

from frisket.server.services.pdf_import_commit import PdfImportCommitService
from frisket.server.services.pdf_imports.storage import PdfImportDraftError
from frisket.server.workspace import Workspace
from test_pdf_import_organized_publication import _admit, _pdf, _run


@pytest.mark.parametrize("organization", ["pages", "split_documents"])
@pytest.mark.parametrize("existing", [False, True])
def test_reimport_matches_original_with_visible_split_children(
    tmp_path, organization, existing
):
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    pid = workspace.create("Uploads", project_id="uploads")["id"]
    project = workspace.get(pid)
    destination = None
    if existing:
        sheet = project.add_sheet("Archive")
        project.add_column(sheet, "filename", type="text")
        project.add_column(sheet, "size", type="integer")
        destination = {"kind": "existing", "sheet_id": sheet}
    files = [("a.pdf", _pdf(3), "application/pdf", {})]
    service, status = _admit(
        workspace,
        pid,
        tmp_path / "one",
        files,
        organization=organization,
        destination=destination,
    )
    if organization == "split_documents":
        from frisket.server.services.pdf_imports.inventory import PdfDraftInventory

        with PdfDraftInventory(
            service._root / status["draft_id"] / "inventory.db"
        ) as inv:
            occurrence = inv.staged_page(after=0, limit=1)[0]["occurrence_id"]
        status = service.update_choices(
            pid,
            status["draft_id"],
            expected_revision=status["revision"],
            **{**status["choices"], "split_starts": {occurrence: [1, 2]}},
        )
        if existing:
            status = service.match_destination(pid, status["draft_id"])
    frozen = service.freeze_for_commit(
        pid,
        status["draft_id"],
        idempotency_key="one",
        expected_revision=status["revision"],
    )
    result = PdfImportCommitService(workspace, intake=service).commit_frozen(
        pid, frozen
    )
    _run(workspace, result)
    sheet = result.sheet_id
    service2, status2 = _admit(
        workspace,
        pid,
        tmp_path / "two",
        files,
        organization="pages",
        destination={"kind": "existing", "sheet_id": sheet},
    )
    assert status2["match"]["matched_count"] == 1
    assert status2["match"]["new_count"] == 0
    with pytest.raises(PdfImportDraftError, match="Nothing new to import") as exc:
        service2.freeze_for_commit(
            pid,
            status2["draft_id"],
            idempotency_key="two",
            expected_revision=status2["revision"],
        )
    assert exc.value.status_code == 409
    project.db.execute("UPDATE rows SET hidden=1 WHERE sheet_id=?", (sheet,))
    project.db.commit()
    status3 = service2.match_destination(pid, status2["draft_id"])
    assert status3["match"]["matched_count"] == 0


def test_reimport_uses_name_and_original_size_and_current_child(tmp_path):
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    pid = workspace.create("Uploads", project_id="uploads")["id"]
    original = ("a.pdf", _pdf(3), "application/pdf", {})
    service, status = _admit(workspace, pid, tmp_path / "one", [original])
    frozen = service.freeze_for_commit(
        pid,
        status["draft_id"],
        idempotency_key="one",
        expected_revision=status["revision"],
    )
    result = PdfImportCommitService(workspace, intake=service).commit_frozen(
        pid, frozen
    )
    _run(workspace, result)
    project = workspace.get(pid)
    sheet = result.sheet_id
    files = [original, ("a.pdf", _pdf(2), "application/pdf", {})]
    service2, status2 = _admit(
        workspace,
        pid,
        tmp_path / "two",
        files,
        destination={"kind": "existing", "sheet_id": sheet},
    )
    assert status2["match"]["matched_count"] == 1
    assert status2["match"]["new_count"] == 1
    rows = project.visible_row_ids(sheet)
    project.db.execute(
        "UPDATE rows SET hidden=1 WHERE sheet_id=? AND id<>?", (sheet, rows[0])
    )
    project.db.commit()
    assert (
        service2.match_destination(pid, status2["draft_id"])["match"]["matched_count"]
        == 1
    )
    media = next(col["id"] for col in project.columns(sheet) if col["name"] == "media")
    project.apply_edits(
        [
            {
                "row_id": rows[0],
                "column_id": media,
                "value": {
                    "blob": "different",
                    "filename": "a.pdf",
                    "mime": "application/pdf",
                },
            }
        ]
    )
    assert (
        service2.match_destination(pid, status2["draft_id"])["match"]["matched_count"]
        == 0
    )
