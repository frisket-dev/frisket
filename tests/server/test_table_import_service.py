from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
from pathlib import Path
from threading import Event, Lock
import zipfile

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
    assert created.state == "ready"
    assert created.settings is not None and created.settings.revision == 0
    assert created.preview is not None and created.preview.tail_pending is False
    assert created.preview.notice.kind == "consistent"
    assert created.analysis.exact_rows == 2
    assert created.analysis.estimated_rows is None
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
    assert frozen.plan.extra_values_target_name is None
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


def test_unknown_zip_container_is_rejected_as_an_unsupported_source(
    tmp_path: Path,
) -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("Index/Document.iwa", b"numbers-document")
        output.writestr("Metadata/Properties.plist", b"<plist/>")
    _workspace, project_id, service = _service(tmp_path)

    with pytest.raises(TableImportDraftError) as rejected:
        service.create(
            project_id,
            _upload(archive.getvalue(), "report.numbers"),
            input_kind="file",
        )

    assert rejected.value.status_code == 422
    assert rejected.value.detail["code"] == "unsupported_source"


def test_extra_values_column_is_reserved_for_provisional_or_observed_overflow(
    tmp_path: Path,
) -> None:
    _workspace, project_id, service = _service(tmp_path)
    provisional_payload = b"name,value\n" + b"".join(
        f"person-{index},{index}\n".encode() for index in range(80)
    )
    provisional = service.create(
        project_id, _upload(provisional_payload), input_kind="file"
    )
    provisional_plan = service.freeze_for_commit(
        project_id,
        provisional.draft_id,
        expected_revision=provisional.revision,
        idempotency_key="provisional-extra-values",
    )
    assert isinstance(provisional_plan, FrozenTableImport)
    assert provisional_plan.exact_rows is None
    assert provisional_plan.plan.extra_values_target_name == "extra values"

    overflow = service.create(
        project_id,
        _upload(b"name\nAlice,overflow\n", "overflow.csv"),
        input_kind="file",
    )
    assert overflow.preview is not None
    overflow_row = next(row for row in overflow.preview.head if row.role == "data")
    assert overflow_row.cells == ["Alice", "overflow"]
    assert overflow_row.hidden_cell_count == 0
    assert overflow_row.issues[0].code == "extra_values"
    assert overflow_row.issues[0].original_values == ["overflow"]

    missing = service.create(
        project_id,
        _upload(b"name,value,place\nAlice\n", "missing.csv"),
        input_kind="file",
    )
    assert missing.preview is not None
    missing_row = next(row for row in missing.preview.head if row.role == "data")
    assert missing.preview.expected_values == 3
    assert missing_row.cells == ["Alice"]
    assert missing_row.hidden_cell_count == 0
    assert missing_row.issues[0].code == "missing_values"
    assert missing_row.issues[0].count == 1
    assert missing_row.issues[0].original_values == []

    overflow_plan = service.freeze_for_commit(
        project_id,
        overflow.draft_id,
        expected_revision=overflow.revision,
        idempotency_key="observed-extra-values",
    )
    assert isinstance(overflow_plan, FrozenTableImport)
    assert overflow_plan.exact_rows == 1
    assert overflow_plan.plan.extra_values_target_name == "extra values"


def test_immediate_analysis_callback_cannot_overwrite_completed_state(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    project_id = str(workspace.create("Table intake", project_id="table-intake")["id"])
    service: TableImportService
    enqueued: list[str] = []

    def enqueue(pid: str, draft_id: str, settings_revision: int) -> int:
        enqueued.append(draft_id)
        completed = service.scan(pid, draft_id, settings_revision=settings_revision)
        assert completed.state == "ready"
        return 41

    service = TableImportService(workspace, enqueue_analysis=enqueue)
    tiny = service.create(project_id, _upload(CSV, "tiny.csv"), input_kind="file")
    assert tiny.analysis.complete is True
    assert enqueued == []

    payload = b"name,value\n" + b"".join(
        f"person-{index},{index}\n".encode() for index in range(80)
    )
    created = service.create(project_id, _upload(payload), input_kind="file")

    assert created.state == "ready"
    assert created.analysis.complete is True
    assert created.analysis.job_id == 41
    assert enqueued == [created.draft_id]
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


def test_skipped_preamble_and_footer_do_not_contribute_row_issues(
    tmp_path: Path,
) -> None:
    _workspace, project_id, service = _service(tmp_path)
    payload = b"preamble,has,extra\nname,value\nAlice,1\nfooter,has,extra\n"
    created = service.create(project_id, _upload(payload), input_kind="file")
    assert created.settings is not None
    requested = created.settings.requested.model_copy(
        update={"skip_rows_start": 1, "skip_rows_end": 1}
    )
    changed = service.update_settings(
        project_id,
        created.draft_id,
        expected_revision=created.revision,
        settings=requested.model_dump(mode="json"),
    )
    assert changed.settings is not None

    scanned = service.scan(
        project_id,
        created.draft_id,
        settings_revision=changed.settings.revision,
    )

    assert scanned.analysis.issue_counts.extra_values == 0
    assert scanned.analysis.issue_counts.missing_values == 0
    assert scanned.preview is not None
    assert scanned.preview.notice.kind == "consistent"


def test_completed_footer_scan_marks_the_footer_and_reports_exact_data_rows(
    tmp_path: Path,
) -> None:
    _workspace, project_id, service = _service(tmp_path)
    payload = b"name,value\nAlice,1\nBob,2\nTotal,3\n"
    created = service.create(project_id, _upload(payload), input_kind="file")
    assert created.settings is not None
    requested = created.settings.requested.model_copy(update={"skip_rows_end": 1})
    changed = service.update_settings(
        project_id,
        created.draft_id,
        expected_revision=created.revision,
        settings=requested.model_dump(mode="json"),
    )
    assert changed.settings is not None
    assert changed.analysis.complete is False

    scanned = service.scan(
        project_id,
        created.draft_id,
        settings_revision=changed.settings.revision,
    )

    assert scanned.analysis.complete is True
    assert scanned.analysis.exact_rows == 2
    assert scanned.preview is not None
    assert scanned.preview.complete is True
    assert [row.role for row in scanned.preview.head] == [
        "header",
        "data",
        "data",
        "skipped_end",
    ]


def test_exact_scan_blocks_the_deployment_row_limit_before_freeze(
    tmp_path: Path,
) -> None:
    workspace, project_id, service = _service(tmp_path)
    created = service.create(project_id, _upload(CSV), input_kind="file", max_rows=1)
    directory = Path(workspace.get(project_id).path) / ".imports" / created.draft_id
    assert read_import_header(directory).max_rows == 1
    assert created.analysis.exact_rows == 2
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

    assert created.analysis.exact_rows == 2
    assert created.can_commit is True
    assert created.commit_blocked_reason is None

    scanned = service.scan(project_id, created.draft_id, settings_revision=0)

    assert scanned.analysis.exact_rows == 2
    assert scanned.can_commit is True


def test_current_row_limit_is_checked_before_commit_state_changes(
    tmp_path: Path,
) -> None:
    _workspace, project_id, service = _service(tmp_path)
    payload = CSV + b"".join(
        f"person-{index},2025-01-02T03:04:05,{index}\n".encode() for index in range(80)
    )
    created = service.create(project_id, _upload(payload), input_kind="file")
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
    payload = CSV + b"".join(
        f"person-{index},2025-01-02T03:04:05,{index}\n".encode() for index in range(80)
    )
    created = service.create(project_id, _upload(payload), input_kind="file")
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
    payload = CSV + b"".join(
        f"person-{index},2025-01-02T03:04:05,{index}\n".encode() for index in range(80)
    )
    created = service.create(project_id, _upload(payload), input_kind="file")
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


def test_destination_canonicalizes_duplicate_output_name_hints(
    tmp_path: Path,
) -> None:
    workspace, project_id, service = _service(tmp_path)
    created = service.create(
        project_id, _upload(b"first,second\n1,2\n"), input_kind="file"
    )
    scanned = service.scan(project_id, created.draft_id, settings_revision=0)
    included = [column for column in scanned.columns if column.included]

    duplicate = service.update_destination(
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
    assert duplicate.destination is not None
    assert [mapping.target.name for mapping in duplicate.destination.mappings] == [
        "duplicate",
        "duplicate 2",
    ]

    spaced_duplicate = service.update_destination(
        project_id,
        created.draft_id,
        expected_revision=duplicate.revision,
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
    assert spaced_duplicate.destination is not None
    assert [
        mapping.target.name for mapping in spaced_duplicate.destination.mappings
    ] == ["same", "same 2"]

    sheet_id = workspace.get(project_id).add_sheet("Existing")
    shared_column_id = workspace.get(project_id).add_column(
        sheet_id, "shared", type="text"
    )
    with pytest.raises(TableImportDraftError) as duplicate_existing:
        service.update_destination(
            project_id,
            created.draft_id,
            expected_revision=spaced_duplicate.revision,
            destination={"kind": "existing", "sheet_id": sheet_id},
            mappings=[
                {
                    "source_index": column.source_index,
                    "target": {
                        "kind": "existing",
                        "column_id": shared_column_id,
                    },
                }
                for column in included
            ],
            dedup={"enabled": False, "key_source_index": None},
        )
    assert duplicate_existing.value.status_code == 422
    assert duplicate_existing.value.detail["code"] == "invalid_destination"

    reserved = service.create(
        project_id,
        _upload(b"extra values,name\noverflow,Alice\n", "reserved.csv"),
        input_kind="file",
    )
    assert reserved.destination is not None
    assert reserved.destination.comparison.state == "ready"
    assert reserved.can_commit is True


def test_existing_destination_rejects_reserved_overflow_column(tmp_path: Path) -> None:
    workspace, project_id, service = _service(tmp_path)
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("People")
    project.add_column(sheet_id, "extra values", type="text")
    created = service.create(
        project_id,
        _upload(b"name\nAlice,overflow\n", "overflow.csv"),
        input_kind="file",
    )

    with pytest.raises(TableImportDraftError) as rejected:
        service.update_destination(
            project_id,
            created.draft_id,
            expected_revision=created.revision,
            destination={"kind": "existing", "sheet_id": sheet_id},
            mappings=[
                {
                    "source_index": column.source_index,
                    "target": {"kind": "new", "name": column.name},
                }
                for column in created.columns
                if column.included
            ],
            dedup={"enabled": False, "key_source_index": None},
        )

    assert rejected.value.status_code == 409
    assert rejected.value.detail["code"] == "destination_changed"


def test_existing_destination_canonicalizes_hidden_active_column_name(
    tmp_path: Path,
) -> None:
    workspace, project_id, service = _service(tmp_path)
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("People")
    project.add_column(sheet_id, "amount", type="number", hidden=True)
    created = service.create(
        project_id, _upload(b"amount\n1\n", "amounts.csv"), input_kind="file"
    )
    included = [column for column in created.columns if column.included]
    compared = service.update_destination(
        project_id,
        created.draft_id,
        expected_revision=created.revision,
        destination={"kind": "existing", "sheet_id": sheet_id},
        mappings=[
            {
                "source_index": included[0].source_index,
                "target": {"kind": "new", "name": included[0].name},
            }
        ],
        dedup={"enabled": False, "key_source_index": None},
    )

    assert compared.destination is not None
    assert compared.destination.mappings[0].target.name == "amount 2"
    frozen = service.freeze_for_commit(
        project_id,
        created.draft_id,
        expected_revision=compared.revision,
        idempotency_key="hidden-name-canonicalized",
    )
    assert isinstance(frozen, FrozenTableImport)
    assert frozen.plan.columns[0].target_name == "amount 2"


@pytest.mark.parametrize("hidden", [False, True])
def test_freeze_rechecks_new_column_names_against_current_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hidden: bool
) -> None:
    workspace, project_id, service = _service(tmp_path)
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("People")
    created = service.create(project_id, _upload(CSV), input_kind="file")
    compared = service.update_destination(
        project_id,
        created.draft_id,
        expected_revision=created.revision,
        destination={"kind": "existing", "sheet_id": sheet_id},
        mappings=[
            {
                "source_index": column.source_index,
                "target": {"kind": "new", "name": column.name},
            }
            for column in created.columns
            if column.included
        ],
        dedup={"enabled": False, "key_source_index": None},
    )
    original = service._build_frozen_plan
    calls = 0

    def add_collision_after_review(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        if calls == 1:
            project.add_column(sheet_id, "score", type="text", hidden=hidden)
        return result

    monkeypatch.setattr(service, "_build_frozen_plan", add_collision_after_review)

    with pytest.raises(TableImportDraftError) as rejected:
        service.freeze_for_commit(
            project_id,
            created.draft_id,
            expected_revision=compared.revision,
            idempotency_key="destination-name-changed",
        )

    assert rejected.value.status_code == 409
    assert rejected.value.detail["code"] == "destination_changed"
    assert calls == 2
    assert service.status(project_id, created.draft_id).state == compared.state


def test_waiting_same_key_freeze_refuses_a_reset_commit_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, project_id, service = _service(tmp_path)
    created = service.create(project_id, _upload(CSV), input_kind="file")
    directory = Path(workspace.get(project_id).path) / ".imports" / created.draft_id
    original_commit_lock = service._store.commit_lock
    blocker = original_commit_lock(directory)
    attempts_lock = Lock()
    first_attempt = Event()
    both_attempts = Event()
    attempts = 0

    class SignallingLock:
        def __init__(self):
            self._delegate = original_commit_lock(directory)

        def __enter__(self):
            nonlocal attempts
            with attempts_lock:
                attempts += 1
                first_attempt.set()
                if attempts == 2:
                    both_attempts.set()
            return self._delegate.__enter__()

        def __exit__(self, *args):
            return self._delegate.__exit__(*args)

    monkeypatch.setattr(
        service._store, "commit_lock", lambda _directory: SignallingLock()
    )
    pool = ThreadPoolExecutor(max_workers=2)
    blocker.acquire()
    try:
        first = pool.submit(
            service.freeze_for_commit,
            project_id,
            created.draft_id,
            expected_revision=created.revision,
            idempotency_key="same-key",
        )
        assert first_attempt.wait(timeout=5)
        second = pool.submit(
            service.freeze_for_commit,
            project_id,
            created.draft_id,
            expected_revision=created.revision,
            idempotency_key="same-key",
        )
        assert both_attempts.wait(timeout=5)
        with service._store.lock(directory):
            current = service._store.open(project_id, created.draft_id)[1]
            assert current["state"] == "committing"
            current["state"] = created.state
            current["commit_key"] = None
            service._store.save_header(directory, current)
    finally:
        blocker.release()

    for future in (first, second):
        with pytest.raises(TableImportDraftError) as rejected:
            future.result(timeout=5)
        assert rejected.value.detail["code"] == "commit_state_changed"
    pool.shutdown(wait=True)


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


def test_unreadable_notice_precedes_width_mismatch(tmp_path: Path) -> None:
    _workspace, project_id, service = _service(tmp_path)
    payload = b"name,value\nAlice,\x80,extra\nBob,2\n"
    created = service.create(project_id, _upload(payload), input_kind="file")
    assert created.settings is not None
    explicit = created.settings.requested.model_copy(update={"encoding": "utf-8"})

    changed = service.update_settings(
        project_id,
        created.draft_id,
        expected_revision=created.revision,
        settings=explicit.model_dump(mode="json"),
    )

    assert changed.preview is not None
    assert changed.preview.notice.kind == "unreadable_characters"
    assert changed.preview.notice.count == 1
    assert changed.analysis.issue_counts.extra_values == 1


@pytest.mark.parametrize(
    ("rows", "expected_kind", "expected_count", "common_values"),
    [
        (["A,1,extra", "B,2,extra", "C,3"], "mostly_wrong_width", 2, 3),
        (["A,1,extra", "B,2", "C,3"], "some_wrong_width", 1, 2),
    ],
)
def test_width_notice_distinguishes_majority_from_some_rows(
    tmp_path: Path,
    rows: list[str],
    expected_kind: str,
    expected_count: int,
    common_values: int,
) -> None:
    _workspace, project_id, service = _service(tmp_path)
    payload = ("name,value\n" + "\n".join(rows) + "\n").encode()

    created = service.create(project_id, _upload(payload), input_kind="file")

    assert created.preview is not None
    assert created.preview.notice.kind == expected_kind
    assert created.preview.notice.count == expected_count
    assert created.preview.notice.common_values == common_values


def test_one_name_cell_with_mostly_two_value_rows_is_a_majority_mismatch(
    tmp_path: Path,
) -> None:
    _workspace, project_id, service = _service(tmp_path)
    created = service.create(
        project_id,
        _upload(b"title\nAlice,1\nBob,2\nCara,3\n", "title.csv"),
        input_kind="file",
    )

    assert created.preview is not None
    assert created.preview.expected_values == 1
    assert created.preview.notice.kind == "mostly_wrong_width"
    assert created.preview.notice.count == 3
    assert created.preview.notice.common_values == 2


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
    assert opened.state == "ready"
    assert opened.source.format == "xlsx"
    assert opened.preview is not None
    draft_text = (
        Path(workspace.get(project_id).path)
        / ".imports"
        / created.draft_id
        / "table-draft.json"
    ).read_text("utf-8")
    assert "correct horse" not in draft_text
