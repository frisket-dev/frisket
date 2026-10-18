from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from frisket.engine.store.table_import_intake import FrozenTableImport
from frisket.server.services.import_uploads import AdmittedUpload
from frisket.server.services.table_imports import (
    TableImportDraftError,
    TableImportService,
)
from frisket.server.workspace import Workspace


CSV = b"name,joined,score\nAlice,2025-01-02T03:04:05,1.5\nBob,2025-02-03T04:05:06,2.5\n"


def _upload(payload: bytes, filename: str = "people.csv") -> AdmittedUpload:
    return AdmittedUpload(
        filename=filename,
        mime="application/octet-stream",
        source=io.BytesIO(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
    )


def _service(tmp_path: Path) -> tuple[Workspace, str, TableImportService]:
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Table intake", project_id="table-intake")["id"])
    return workspace, project_id, TableImportService(workspace)


def test_table_draft_stages_privately_and_freezes_reviewed_plan(tmp_path: Path) -> None:
    workspace, project_id, service = _service(tmp_path)
    created = service.create(project_id, _upload(CSV), input_kind="file")

    project = workspace.get(project_id)
    directory = Path(project.path) / ".imports" / created.draft_id
    assert created.state == "analyzing"
    assert created.settings is not None and created.settings.revision == 0
    assert created.preview is not None and created.preview.tail_pending is True
    assert created.analysis.exact_rows is None
    assert created.can_commit is True
    assert directory.joinpath("source").read_bytes() == CSV
    assert project.sheets() == []
    assert project.db.execute("SELECT COUNT(*) FROM rows").fetchone()[0] == 0

    scanned = service.scan(project_id, created.draft_id, settings_revision=0)
    assert scanned.state == "ready"
    assert scanned.analysis.exact_rows == 2
    assert scanned.preview is not None and scanned.preview.tail_pending is False
    joined = next(column for column in scanned.columns if column.name == "joined")
    assert joined.target_type == "datetime"

    frozen = service.freeze_for_commit(
        project_id,
        created.draft_id,
        expected_revision=scanned.revision,
        idempotency_key="table-request-1",
    )
    assert isinstance(frozen, FrozenTableImport)
    assert frozen.import_ref == created.draft_id
    assert frozen.exact_rows == 2
    assert [column.output_type for column in frozen.plan.columns] == [
        "text",
        "datetime",
        "number",
    ]
    replayed = service.freeze_for_commit(
        project_id,
        created.draft_id,
        expected_revision=scanned.revision,
        idempotency_key="table-request-1",
    )
    assert isinstance(replayed, FrozenTableImport)
    assert replayed.plan_hash == frozen.plan_hash
    assert project.sheets() == []


def test_table_settings_revision_rejects_stale_preview_and_footer_waits(
    tmp_path: Path,
) -> None:
    _workspace, project_id, service = _service(tmp_path)
    created = service.create(project_id, _upload(CSV), input_kind="file")
    assert created.settings is not None
    requested = created.settings.requested.model_copy(update={"skip_rows_end": 1})
    changed = service.update_settings(
        project_id,
        created.draft_id,
        expected_revision=created.revision,
        settings=requested.model_dump(mode="json"),
    )
    assert changed.settings is not None and changed.settings.revision == 1
    assert changed.can_commit is False
    assert changed.commit_blocked_reason == "analyzing"

    with pytest.raises(TableImportDraftError) as stale:
        service.update_settings(
            project_id,
            created.draft_id,
            expected_revision=created.revision,
            settings=requested.model_dump(mode="json"),
        )
    assert stale.value.status_code == 409
    assert stale.value.detail["code"] == "revision_conflict"

    scanned = service.scan(project_id, created.draft_id, settings_revision=1)
    assert scanned.analysis.exact_rows == 1
    assert scanned.can_commit is True


def test_invalid_settings_leave_the_published_revision_retryable(
    tmp_path: Path,
) -> None:
    _workspace, project_id, service = _service(tmp_path)
    created = service.create(project_id, _upload(CSV), input_kind="file")
    assert created.settings is not None
    invalid = created.settings.requested.model_copy(update={"skip_rows_start": 999})

    with pytest.raises(TableImportDraftError) as rejected:
        service.update_settings(
            project_id,
            created.draft_id,
            expected_revision=created.revision,
            settings=invalid.model_dump(mode="json"),
        )
    assert rejected.value.status_code == 422
    assert rejected.value.detail["code"] == "invalid_table"

    unchanged = service.status(project_id, created.draft_id)
    assert unchanged.revision == created.revision
    assert unchanged.settings is not None and unchanged.settings.revision == 0
    retried = service.update_settings(
        project_id,
        created.draft_id,
        expected_revision=created.revision,
        settings=created.settings.requested.model_dump(mode="json"),
    )
    assert retried.settings is not None and retried.settings.revision == 1


def test_snapshot_keeps_mixed_datetime_warning_and_resolved_number_samples(
    tmp_path: Path,
) -> None:
    _workspace, project_id, service = _service(tmp_path)
    payload = (
        "when;amount\n2025-01-02T03:04:05;1,5\n2025-01-02T03:04:05+00:00;2,5\n"
    ).encode()
    created = service.create(project_id, _upload(payload), input_kind="file")
    scanned = service.scan(project_id, created.draft_id, settings_revision=0)

    when = next(column for column in scanned.columns if column.name == "when")
    amount = next(column for column in scanned.columns if column.name == "amount")
    assert when.target_type == "text"
    assert "mixed_datetime_awareness" in when.needs_review
    assert amount.target_type == "number"
    assert [sample.value for sample in amount.samples] == [1.5, 2.5]


def test_full_scan_refreshes_unreviewed_prefix_inference(tmp_path: Path) -> None:
    _workspace, project_id, service = _service(tmp_path)
    payload = (
        "value\n" + "".join(f"{index}\n" for index in range(64)) + "text\n" * 64
    ).encode()
    created = service.create(project_id, _upload(payload), input_kind="file")
    assert created.columns[0].target_type == "integer"

    scanned = service.scan(project_id, created.draft_id, settings_revision=0)

    assert scanned.columns[0].suggested_type == "text"
    assert scanned.columns[0].target_type == "text"
    assert scanned.destination is not None
    assert scanned.destination.mappings[0].source_type == "text"


def test_existing_destination_preview_counts_only_frozen_sheet_duplicates(
    tmp_path: Path,
) -> None:
    workspace, project_id, service = _service(tmp_path)
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("People")
    name_column = project.add_column(sheet_id, "name", type="text")
    project.add_rows(
        sheet_id,
        [{"name": "Alice"}, {"name": "Elsewhere"}],
        {"name": name_column},
    )
    created = service.create(project_id, _upload(CSV), input_kind="file")
    scanned = service.scan(project_id, created.draft_id, settings_revision=0)
    mappings = [
        {
            "source_index": column.source_index,
            "target": (
                {"kind": "existing", "column_id": name_column}
                if column.name == "name"
                else {"kind": "new", "name": column.name}
            ),
        }
        for column in scanned.columns
        if column.included
    ]

    compared = service.update_destination(
        project_id,
        created.draft_id,
        expected_revision=scanned.revision,
        destination={"kind": "existing", "sheet_id": sheet_id},
        mappings=mappings,
        dedup={"enabled": True, "key_source_index": 0},
    )

    assert compared.destination is not None
    assert compared.destination.comparison.complete is True
    assert compared.destination.comparison.duplicate_rows == 1
    assert compared.destination.comparison.import_rows == 1


def test_late_encoding_damage_requires_an_explicit_encoding_choice(
    tmp_path: Path,
) -> None:
    _workspace, project_id, service = _service(tmp_path)
    payload = b"value\n" + (b"ok\n" * 22_000) + b"\x80\x81\n"
    created = service.create(project_id, _upload(payload), input_kind="file")
    assert created.can_commit is True

    scanned = service.scan(project_id, created.draft_id, settings_revision=0)

    assert scanned.analysis.issue_counts.unreadable_cells == 2
    assert scanned.can_commit is False
    assert scanned.commit_blocked_reason == "encoding"
    assert scanned.preview is not None
    assert scanned.preview.notice.kind == "unreadable_characters"
    assert scanned.preview.notice.count == 2

    assert scanned.settings is not None
    explicit = scanned.settings.requested.model_copy(update={"encoding": "utf-8"})
    changed = service.update_settings(
        project_id,
        created.draft_id,
        expected_revision=scanned.revision,
        settings=explicit.model_dump(mode="json"),
    )
    rescanned = service.scan(
        project_id,
        created.draft_id,
        settings_revision=changed.settings.revision if changed.settings else -1,
    )
    assert rescanned.analysis.issue_counts.unreadable_cells == 2
    assert rescanned.can_commit is True


def test_encrypted_workbook_password_is_transient_and_decrypted_source_is_readable(
    tmp_path: Path,
) -> None:
    from openpyxl import Workbook
    from msoffcrypto.format.ooxml import OOXMLFile

    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(["name", "amount"])
    sheet.append(["Alice", 4])
    plain = io.BytesIO()
    workbook.save(plain)
    encrypted = io.BytesIO()
    OOXMLFile(io.BytesIO(plain.getvalue())).encrypt("correct horse", encrypted)

    workspace, project_id, service = _service(tmp_path)
    created = service.create(
        project_id,
        _upload(encrypted.getvalue(), "protected.xlsx"),
        input_kind="file",
    )
    assert created.state == "password_required"
    assert created.settings is None
    assert "correct horse" not in (
        Path(workspace.get(project_id).path)
        / ".imports"
        / created.draft_id
        / "table-draft.json"
    ).read_text("utf-8")

    with pytest.raises(TableImportDraftError) as invalid:
        service.unlock(project_id, created.draft_id, password="wrong")
    assert invalid.value.detail["code"] == "invalid_password"

    opened = service.unlock(project_id, created.draft_id, password="correct horse")
    assert opened.state == "analyzing"
    assert opened.source.format == "xlsx"
    assert opened.preview is not None
    draft_text = (
        Path(workspace.get(project_id).path)
        / ".imports"
        / created.draft_id
        / "table-draft.json"
    ).read_text("utf-8")
    assert "correct horse" not in draft_text
