from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
from pathlib import Path
from threading import Event

import pytest

from frisket.engine.store.table_import_intake import (
    FrozenTableImport,
    table_dedup_key,
    table_key_exists,
)
from frisket.engine.store.import_intake import read_import_header
from frisket.server.services.import_uploads import AdmittedUpload
from frisket.server.services.table_imports import (
    TableImportDraftError,
    TableImportService,
    run_table_analysis,
)
from frisket.server.workspace import Workspace
from frisket.table_import import TableReadError


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
    assert created.preview.notice.kind == "consistent"
    assert created.analysis.exact_rows is None
    assert created.analysis.estimated_rows == 2
    assert created.can_commit is True
    assert directory.joinpath("source").read_bytes() == CSV
    assert project.sheets() == []
    assert project.db.execute("SELECT COUNT(*) FROM rows").fetchone()[0] == 0

    scanned = run_table_analysis(
        project, project_id, created.draft_id, settings_revision=0
    )
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


def test_immediate_analysis_callback_cannot_overwrite_completed_state(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Table intake", project_id="table-intake")["id"])
    service: TableImportService

    def enqueue(pid: str, draft_id: str, settings_revision: int) -> int:
        completed = service.scan(pid, draft_id, settings_revision=settings_revision)
        assert completed.state == "ready"
        return 41

    service = TableImportService(workspace, enqueue_analysis=enqueue)
    created = service.create(project_id, _upload(CSV), input_kind="file")

    assert created.state == "ready"
    assert created.analysis.complete is True
    assert created.analysis.job_id == 41
    assert service.status(project_id, created.draft_id).state == "ready"


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


def test_exact_scan_blocks_the_deployment_row_limit_before_freeze(
    tmp_path: Path,
) -> None:
    workspace, project_id, service = _service(tmp_path)
    created = service.create(project_id, _upload(CSV), input_kind="file", max_rows=1)
    directory = Path(workspace.get(project_id).path) / ".imports" / created.draft_id
    assert read_import_header(directory).max_rows == 1
    assert created.analysis.exact_rows is None
    assert created.can_commit is False
    assert created.commit_blocked_reason == "limit"

    with pytest.raises(TableImportDraftError) as prefix_rejected:
        service.freeze_for_commit(
            project_id,
            created.draft_id,
            expected_revision=created.revision,
            idempotency_key="prefix-over-limit",
        )
    assert prefix_rejected.value.status_code == 413

    scanned = service.scan(project_id, created.draft_id, settings_revision=0)

    assert scanned.analysis.exact_rows == 2
    assert scanned.can_commit is False
    assert scanned.commit_blocked_reason == "limit"
    with pytest.raises(TableImportDraftError) as rejected:
        service.freeze_for_commit(
            project_id,
            created.draft_id,
            expected_revision=scanned.revision,
            idempotency_key="over-limit",
        )
    assert rejected.value.status_code == 413
    assert rejected.value.detail["code"] == "source_too_large"


def test_configured_row_limit_waits_for_exact_under_limit_count(tmp_path: Path) -> None:
    _workspace, project_id, service = _service(tmp_path)
    created = service.create(project_id, _upload(CSV), input_kind="file", max_rows=2)

    assert created.analysis.exact_rows is None
    assert created.can_commit is False
    assert created.commit_blocked_reason == "analyzing"

    scanned = service.scan(project_id, created.draft_id, settings_revision=0)

    assert scanned.analysis.exact_rows == 2
    assert scanned.can_commit is True


def test_current_row_limit_is_checked_before_commit_state_changes(
    tmp_path: Path,
) -> None:
    _workspace, project_id, service = _service(tmp_path)
    created = service.create(project_id, _upload(CSV), input_kind="file")
    scanned = service.scan(project_id, created.draft_id, settings_revision=0)

    with pytest.raises(TableImportDraftError) as rejected:
        service.freeze_for_commit(
            project_id,
            created.draft_id,
            expected_revision=scanned.revision,
            idempotency_key="tightened-limit",
            max_rows=1,
        )

    assert rejected.value.status_code == 413
    assert service.status(project_id, created.draft_id).state == "ready"


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


def test_snapshot_exposes_inferred_date_order_and_both_conversion_counts(
    tmp_path: Path,
) -> None:
    _workspace, project_id, service = _service(tmp_path)
    payload = b"when\n31/01/2025\n18/02/2025\n"
    created = service.create(project_id, _upload(payload), input_kind="file")
    scanned = service.scan(project_id, created.draft_id, settings_revision=0)
    column = scanned.columns[0]

    assert column.target_type == "date"
    assert column.inferred_date_order == "day_first"
    assert column.date_order == "day_first"
    assert column.conversion_counts_by_date_order is not None
    assert column.conversion_counts_by_date_order.day_first.valid == 2
    assert column.conversion_counts_by_date_order.month_first.invalid == 2

    resolved = service.update_columns(
        project_id,
        created.draft_id,
        expected_revision=scanned.revision,
        columns=[
            {
                "source_index": 0,
                "included": True,
                "target_type": "date",
                "date_order": None,
            }
        ],
    )
    assert resolved.columns[0].date_order == "day_first"

    with pytest.raises(TableImportDraftError) as invalid:
        service.update_columns(
            project_id,
            created.draft_id,
            expected_revision=resolved.revision,
            columns=[
                {
                    "source_index": 0,
                    "included": True,
                    "target_type": "text",
                    "date_order": "day_first",
                }
            ],
        )
    assert invalid.value.detail["code"] == "invalid_columns"


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
    frozen = service.freeze_for_commit(
        project_id,
        created.draft_id,
        expected_revision=compared.revision,
        idempotency_key="existing-dedup",
    )
    assert isinstance(frozen, FrozenTableImport)
    assert frozen.plan.dedup is not None
    assert frozen.plan.dedup.snapshot_revision == 1
    key = table_dedup_key("Alice")
    assert key is not None and table_key_exists(frozen.intake_dir, key)

    replayed = service.freeze_for_commit(
        project_id,
        created.draft_id,
        expected_revision=compared.revision,
        idempotency_key="existing-dedup",
    )
    assert isinstance(replayed, FrozenTableImport)
    assert replayed.plan_hash == frozen.plan_hash


def test_footer_skip_dedup_uses_the_completed_record_count(tmp_path: Path) -> None:
    workspace, project_id, service = _service(tmp_path)
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("People")
    name_column = project.add_column(sheet_id, "name", type="text")
    project.add_rows(sheet_id, [{"name": "Alice"}], {"name": name_column})
    created = service.create(project_id, _upload(CSV), input_kind="file")
    assert created.settings is not None
    settings = created.settings.requested.model_copy(update={"skip_rows_end": 1})
    changed = service.update_settings(
        project_id,
        created.draft_id,
        expected_revision=created.revision,
        settings=settings.model_dump(mode="json"),
    )
    assert changed.settings is not None
    scanned = service.scan(
        project_id,
        created.draft_id,
        settings_revision=changed.settings.revision,
    )

    compared = service.update_destination(
        project_id,
        created.draft_id,
        expected_revision=scanned.revision,
        destination={"kind": "existing", "sheet_id": sheet_id},
        mappings=[
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
        ],
        dedup={"enabled": True, "key_source_index": 0},
    )

    assert compared.destination is not None
    assert compared.destination.comparison.complete is True
    assert compared.destination.comparison.duplicate_rows == 1


def test_full_scan_finishes_the_current_reviewed_dedup_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, project_id, service = _service(tmp_path)
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("People")
    name_column = project.add_column(sheet_id, "name", type="text")
    project.add_rows(sheet_id, [{"name": "Alice"}], {"name": name_column})
    created = service.create(project_id, _upload(CSV), input_kind="file")
    entered = Event()
    release = Event()
    from frisket.server.services.table_imports import service as service_module

    original = service_module.build_complete_scan

    def delayed_scan(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(timeout=5)
        return result

    monkeypatch.setattr(service_module, "build_complete_scan", delayed_scan)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            service.scan,
            project_id,
            created.draft_id,
            settings_revision=0,
        )
        assert entered.wait(timeout=5)
        running = service.update_destination(
            project_id,
            created.draft_id,
            expected_revision=created.revision,
            destination={"kind": "existing", "sheet_id": sheet_id},
            mappings=[
                {
                    "source_index": column.source_index,
                    "target": (
                        {"kind": "existing", "column_id": name_column}
                        if column.name == "name"
                        else {"kind": "new", "name": column.name}
                    ),
                }
                for column in created.columns
                if column.included
            ],
            dedup={"enabled": True, "key_source_index": 0},
        )
        assert running.destination is not None
        assert running.destination.comparison.state == "running"
        release.set()
        completed = future.result(timeout=5)

    assert completed.destination is not None
    assert completed.destination.comparison.state == "ready"
    assert completed.destination.comparison.complete is True
    assert completed.destination.comparison.duplicate_rows == 1


def test_destination_rejects_duplicate_and_reserved_output_names(
    tmp_path: Path,
) -> None:
    _workspace, project_id, service = _service(tmp_path)
    created = service.create(project_id, _upload(CSV), input_kind="file")
    scanned = service.scan(project_id, created.draft_id, settings_revision=0)
    included = [column for column in scanned.columns if column.included]

    with pytest.raises(TableImportDraftError) as duplicate:
        service.update_destination(
            project_id,
            created.draft_id,
            expected_revision=scanned.revision,
            destination={"kind": "new", "sheet_name": "People"},
            mappings=[
                {
                    "source_index": column.source_index,
                    "target": {"kind": "new", "name": "duplicate"},
                }
                for column in included
            ],
            dedup={"enabled": False, "key_source_index": None},
        )
    assert duplicate.value.status_code == 422
    assert duplicate.value.detail["code"] == "invalid_destination"

    with pytest.raises(TableImportDraftError) as spaced_duplicate:
        service.update_destination(
            project_id,
            created.draft_id,
            expected_revision=scanned.revision,
            destination={"kind": "new", "sheet_name": "People"},
            mappings=[
                {
                    "source_index": column.source_index,
                    "target": {
                        "kind": "new",
                        "name": "same" if index == 0 else " same ",
                    },
                }
                for index, column in enumerate(included)
            ],
            dedup={"enabled": False, "key_source_index": None},
        )
    assert spaced_duplicate.value.detail["code"] == "invalid_destination"

    reserved = service.create(
        project_id,
        _upload(b"extra values,name\noverflow,Alice\n", "reserved.csv"),
        input_kind="file",
    )
    assert reserved.destination is not None
    assert reserved.destination.comparison.state == "error"
    assert reserved.can_commit is False
    assert reserved.commit_blocked_reason == "destination"


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


@pytest.mark.parametrize("scan_fails", [False, True])
def test_scan_publication_never_reopens_a_committed_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scan_fails: bool
) -> None:
    _workspace, project_id, service = _service(tmp_path)
    created = service.create(project_id, _upload(CSV), input_kind="file")
    entered = Event()
    release = Event()
    from frisket.server.services.table_imports import service as service_module

    original = service_module.build_complete_scan

    def delayed_scan(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        if scan_fails:
            raise TableReadError("late analysis failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(service_module, "build_complete_scan", delayed_scan)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            service.scan,
            project_id,
            created.draft_id,
            settings_revision=0,
        )
        assert entered.wait(timeout=5)
        frozen = service.freeze_for_commit(
            project_id,
            created.draft_id,
            expected_revision=created.revision,
            idempotency_key="commit-during-scan",
        )
        assert isinstance(frozen, FrozenTableImport)
        service.record_commit(
            project_id,
            created.draft_id,
            idempotency_key="commit-during-scan",
            result={
                "import_ref": created.draft_id,
                "sheet_id": 1,
                "sheet_name": "People",
                "selected_total": None,
                "estimated_rows": None,
            },
        )
        release.set()
        published = future.result(timeout=5)

    assert published.state == "committed"
    assert service.status(project_id, created.draft_id).state == "committed"
    with pytest.raises(TableImportDraftError) as discard:
        service.discard(project_id, created.draft_id)
    assert discard.value.detail["code"] == "already_committed"


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
