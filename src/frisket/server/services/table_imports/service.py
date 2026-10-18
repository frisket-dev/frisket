"""Draft lifecycle for streaming one-file table imports."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from frisket.contracts.http.table_imports import (
    TABLE_IMPORT_LARGE_PASTE_WARNING_BYTES,
    TABLE_IMPORT_SCHEMA_VERSION,
    TableImportCommitResponse,
    TableImportDraft,
    TableImportRequestedSettings,
)
from frisket.engine.store.import_intake import ImportIntakeHeader
from frisket.engine.store.table_import_intake import (
    FrozenTableImport,
    TableImportColumn as FrozenTableImportColumn,
    TableImportDedup as FrozenTableImportDedup,
    TableImportPlan,
    TableImportScan,
    TableImportSource as IntakeTableImportSource,
    freeze_table_plan,
    initialize_table_intake,
    read_table_settings,
    replace_table_settings,
    write_table_scan,
)
from frisket.engine.store.value_codec import decode_stored_value
from frisket.server.services.import_uploads import AdmittedUpload
from frisket.table_import import (
    ColumnStatsAccumulator,
    PreviewAccumulator,
    RawCell,
    TableReadError,
    TableReadSettings,
    convert_cell,
    inspect_source,
    open_table,
    read_prefix,
)

from .storage import TableImportDraftError, TableImportDraftStore, now


_COPY_CHUNK = 1024 * 1024
_PREFIX_RECORDS = 64
_PREVIEW_HEAD = 12
_PREVIEW_COLUMNS = 8


EnqueueAnalysis = Callable[[str, str, int], int | None]


class TableImportService:
    def __init__(
        self,
        workspace: Any,
        *,
        enqueue_analysis: EnqueueAnalysis | None = None,
        max_upload_bytes: int | None = None,
    ) -> None:
        self._workspace = workspace
        self._store = TableImportDraftStore(workspace)
        self._enqueue_analysis = enqueue_analysis
        self._max_upload_bytes = max_upload_bytes

    def create(
        self,
        project_id: str,
        upload: AdmittedUpload,
        *,
        input_kind: str,
        large_paste_confirmed: bool = False,
    ) -> TableImportDraft:
        if input_kind not in {"file", "paste"}:
            raise TableImportDraftError(
                422, self._problem("invalid_table", "invalid table source kind")
            )
        if upload.size == 0:
            raise TableImportDraftError(
                422, self._problem("empty_source", "The selected source is empty.")
            )
        if self._max_upload_bytes is not None and upload.size > self._max_upload_bytes:
            raise TableImportDraftError(
                413,
                self._problem(
                    "source_too_large",
                    "The selected source exceeds the deployment limit.",
                ),
            )
        if (
            input_kind == "paste"
            and upload.size > TABLE_IMPORT_LARGE_PASTE_WARNING_BYTES
            and not large_paste_confirmed
        ):
            raise TableImportDraftError(
                413,
                self._problem(
                    "large_paste_confirmation_required",
                    "Confirm this large paste before continuing.",
                ),
            )
        project = self._workspace.get(project_id)
        draft_id, directory = self._store.create_directory(project_id)
        try:
            source_path = directory / "source"
            self._copy_upload(upload, source_path)
            self._reject_known_unsupported(source_path)
            source = {
                "input_kind": input_kind,
                "filename": "Pasted rows.csv"
                if input_kind == "paste"
                else upload.filename,
                "size": upload.size,
                "sha256": upload.sha256,
                "format": None,
                "sheets": [],
                "can_rerun_workbook": False,
            }
            timestamp = now()
            header: dict[str, Any] = {
                "schema_version": TABLE_IMPORT_SCHEMA_VERSION,
                "draft_id": draft_id,
                "project_id": project_id,
                "storage_identity": project.storage_identity,
                "state": "analyzing",
                "revision": 1,
                "created_at": timestamp,
                "updated_at": timestamp,
                "source": source,
                "settings": None,
                "column_choices": [],
                "destination_request": None,
                "analysis_job_id": None,
                "error": None,
                "commit_key": None,
                "commit": None,
            }
            if self._office_encrypted(source_path):
                header["state"] = "password_required"
                self._store.write_json(directory / "table-draft.json", header)
                return self._snapshot(directory, header)
            self._inspect_and_prime(directory, header, initialize=True)
            self._store.write_json(directory / "table-draft.json", header)
            self._enqueue_scan(project_id, directory, header)
            return self._snapshot(directory, header)
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    def status(self, project_id: str, draft_id: str) -> TableImportDraft:
        directory, header = self._store.open(project_id, draft_id)
        return self._snapshot(directory, header)

    def unlock(
        self, project_id: str, draft_id: str, *, password: str
    ) -> TableImportDraft:
        directory, _ = self._store.open(project_id, draft_id)
        with self._store.lock(directory):
            header = self._store.open(project_id, draft_id)[1]
            if header["state"] != "password_required":
                raise TableImportDraftError(
                    409,
                    self._problem(
                        "not_password_protected",
                        "This source does not need a password.",
                    ),
                )
            self._decrypt_office(
                directory / "source", directory / "decrypted", password
            )
            os.replace(directory / "source", directory / "encrypted-source")
            os.replace(directory / "decrypted", directory / "source")
            digest = hashlib.sha256()
            with (directory / "source").open("rb") as source:
                while chunk := source.read(_COPY_CHUNK):
                    digest.update(chunk)
            header["source"]["size"] = (directory / "source").stat().st_size
            header["source"]["sha256"] = digest.hexdigest()
            self._inspect_and_prime(directory, header, initialize=True)
            self._store.save_header(directory, header)
            self._enqueue_scan(project_id, directory, header)
            return self._snapshot(directory, header)

    def update_settings(
        self,
        project_id: str,
        draft_id: str,
        *,
        expected_revision: int,
        settings: Mapping[str, Any],
    ) -> TableImportDraft:
        directory, _ = self._store.open(project_id, draft_id)
        with self._store.lock(directory):
            header = self._store.open(project_id, draft_id)[1]
            self._require_open_revision(header, expected_revision)
            requested = TableImportRequestedSettings.model_validate(settings)
            source_info = self._source_info(directory, header)
            next_settings_revision = int(header["settings"]["revision"]) + 1
            header["settings"] = {
                "revision": next_settings_revision,
                "requested": requested.model_dump(mode="json"),
                "resolved": self._resolve_settings(source_info, requested),
            }
            replace_table_settings(
                directory,
                self._table_settings(header),
                expected_revision=next_settings_revision - 1,
            )
            header["state"] = "analyzing"
            header["analysis_job_id"] = None
            header["error"] = None
            header["destination_request"] = None
            self._store.scan(
                directory
            )  # Validate any existing file before replacement.
            (directory / "table-preview.json").unlink(missing_ok=True)
            self._prime_prefix(directory, header)
            self._store.save_header(directory, header)
            self._enqueue_scan(project_id, directory, header)
            return self._snapshot(directory, header)

    def update_columns(
        self,
        project_id: str,
        draft_id: str,
        *,
        expected_revision: int,
        columns: Sequence[Mapping[str, Any]],
    ) -> TableImportDraft:
        directory, _ = self._store.open(project_id, draft_id)
        with self._store.lock(directory):
            header = self._store.open(project_id, draft_id)[1]
            self._require_open_revision(header, expected_revision)
            scan = self._require_current_scan(directory, header)
            known = {int(item["column"]["index"]) for item in scan["stats"]["columns"]}
            seen: set[int] = set()
            choices: list[dict[str, Any]] = []
            for raw in columns:
                index = raw.get("source_index")
                if type(index) is not int or index not in known or index in seen:
                    raise TableImportDraftError(
                        422,
                        self._problem(
                            "invalid_columns",
                            "Column choices must name each source column once.",
                        ),
                    )
                seen.add(index)
                target = str(raw.get("target_type"))
                included = bool(raw.get("included")) and target != "omit"
                if target not in {
                    "text",
                    "integer",
                    "number",
                    "date",
                    "datetime",
                    "boolean",
                    "omit",
                }:
                    raise TableImportDraftError(
                        422,
                        self._problem(
                            "invalid_columns", "A selected column type is unavailable."
                        ),
                    )
                date_order = raw.get("date_order")
                if date_order not in {None, "month_first", "day_first"}:
                    raise TableImportDraftError(
                        422,
                        self._problem(
                            "invalid_columns", "A selected date order is unavailable."
                        ),
                    )
                choices.append(
                    {
                        "source_index": index,
                        "included": included,
                        "target_type": target,
                        "date_order": date_order,
                    }
                )
            if seen != known:
                raise TableImportDraftError(
                    422,
                    self._problem(
                        "invalid_columns",
                        "Column choices must review every source column.",
                    ),
                )
            header["column_choices"] = choices
            header["destination_request"] = None
            self._store.save_header(directory, header)
            return self._snapshot(directory, header)

    def update_destination(
        self,
        project_id: str,
        draft_id: str,
        *,
        expected_revision: int,
        destination: Mapping[str, Any],
        mappings: Sequence[Mapping[str, Any]],
        dedup: Mapping[str, Any],
    ) -> TableImportDraft:
        directory, _ = self._store.open(project_id, draft_id)
        with self._store.lock(directory):
            header = self._store.open(project_id, draft_id)[1]
            self._require_open_revision(header, expected_revision)
            request = {
                "destination": dict(destination),
                "mappings": [dict(item) for item in mappings],
                "dedup": dict(dedup),
            }
            request["comparison"] = self._compare_destination(
                project_id, directory, header, request
            )
            header["destination_request"] = request
            self._store.save_header(directory, header)
            return self._snapshot(directory, header)

    def scan(
        self, project_id: str, draft_id: str, *, settings_revision: int
    ) -> TableImportDraft:
        directory, header = self._store.open(project_id, draft_id)
        if (
            header.get("settings") is None
            or int(header["settings"]["revision"]) != settings_revision
        ):
            return self._snapshot(directory, header)
        source_path = self._source_path(directory)
        resolved = self._table_settings(header)
        total = None
        if resolved.skip_rows_end:
            from frisket.table_import import count_records

            total = count_records(source_path, resolved)
        preview = PreviewAccumulator(
            head_rows=_PREVIEW_HEAD, tail_rows=2, column_limit=_PREVIEW_COLUMNS
        )
        issue_counts = self._zero_issue_counts()
        with open_table(source_path, resolved, total_record_count=total) as reader:
            stats = ColumnStatsAccumulator(
                reader.columns,
                decimal_separator=resolved.decimal_separator,
                thousands_separator=resolved.thousands_separator,
            )
            record_count = 0
            data_rows = 0
            for row in reader:
                record_count = row.source.record_ordinal
                preview.accept(row)
                stats.accept(row)
                if row.role == "data":
                    data_rows += 1
                self._count_issues(issue_counts, row.issues)
            scan = {
                "settings_revision": settings_revision,
                "record_count": record_count,
                "data_rows": data_rows,
                "preview": self._preview_json(
                    reader.columns,
                    preview.snapshot(complete=True),
                    issue_counts,
                    resolved.encoding,
                ),
                "stats": self._stats_json(stats.snapshot(complete=True)),
                "issue_counts": issue_counts,
                "complete": True,
                "error": None,
            }
        with self._store.lock(directory):
            current = self._store.open(project_id, draft_id)[1]
            if (
                current.get("settings") is None
                or int(current["settings"]["revision"]) != settings_revision
            ):
                return self._snapshot(directory, current)
            revision, settings_hash, _ = read_table_settings(directory)
            write_table_scan(
                directory,
                TableImportScan(
                    settings_revision=revision,
                    settings_hash=settings_hash,
                    scanned_rows=record_count,
                    estimated_rows=None,
                    exact_rows=data_rows,
                    complete=True,
                    problem_count=sum(issue_counts.values()),
                ),
                expected_settings_revision=revision,
            )
            self._store.write_json(directory / "table-preview.json", scan)
            current["state"] = "ready"
            current["error"] = None
            if not current["column_choices"]:
                current["column_choices"] = self._default_choices(scan)
            if current.get("destination_request") is not None:
                current["destination_request"]["comparison"] = (
                    self._compare_destination(
                        project_id,
                        directory,
                        current,
                        current["destination_request"],
                        scan=scan,
                    )
                )
            self._store.save_header(directory, current)
            return self._snapshot(directory, current)

    def freeze_for_commit(
        self,
        project_id: str,
        draft_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> FrozenTableImport | TableImportCommitResponse:
        directory, _ = self._store.open(project_id, draft_id)
        with self._store.lock(directory):
            header = self._store.open(project_id, draft_id)[1]
            if header.get("commit") is not None:
                if header.get("commit_key") != idempotency_key:
                    raise TableImportDraftError(
                        409,
                        self._problem(
                            "commit_identity_changed",
                            "This import was committed by another request.",
                        ),
                    )
                return TableImportCommitResponse.model_validate(header["commit"])
            self._require_open_revision(header, expected_revision)
            can_commit, reason = self._commit_readiness(directory, header)
            if not can_commit:
                code = (
                    "analysis_incomplete" if reason == "analyzing" else "invalid_table"
                )
                raise TableImportDraftError(
                    409, self._problem(code, "The table import is not ready to commit.")
                )
            scan = self._require_current_scan(directory, header)
            settings = header["settings"]
            assert settings is not None and header["destination_request"] is not None
            destination_request = header["destination_request"]
            destination = destination_request["destination"]
            project = self._workspace.get(project_id)
            destination_sheet_id = (
                None if destination["kind"] == "new" else int(destination["sheet_id"])
            )
            if destination_sheet_id is None:
                sheet_name = str(destination["sheet_name"])
            else:
                sheet = project.db.execute(
                    "SELECT name FROM sheets WHERE id=? AND hidden=0",
                    (destination_sheet_id,),
                ).fetchone()
                if sheet is None:
                    raise TableImportDraftError(
                        409,
                        self._problem(
                            "destination_changed",
                            "The destination sheet is unavailable.",
                        ),
                    )
                sheet_name = str(sheet["name"])
            stats_by_index = {
                int(item["column"]["index"]): item["column"]
                for item in scan["stats"]["columns"]
            }
            mappings = {
                int(item["source_index"]): item["target"]
                for item in destination_request["mappings"]
            }
            frozen_columns = []
            for choice in header["column_choices"]:
                if not choice.get("included") or choice["target_type"] == "omit":
                    continue
                source_index = int(choice["source_index"])
                target = mappings[source_index]
                target_column_id = (
                    int(target["column_id"]) if target["kind"] == "existing" else None
                )
                if target_column_id is None:
                    target_name = str(target["name"])
                else:
                    target_row = project.db.execute(
                        "SELECT name FROM columns WHERE id=? AND sheet_id=? "
                        "AND active=1 AND hidden=0",
                        (target_column_id, destination_sheet_id),
                    ).fetchone()
                    if target_row is None:
                        raise TableImportDraftError(
                            409,
                            self._problem(
                                "destination_changed",
                                "A destination column is unavailable.",
                            ),
                        )
                    target_name = str(target_row["name"])
                frozen_columns.append(
                    FrozenTableImportColumn(
                        source_index=source_index,
                        source_name=str(stats_by_index[source_index]["name"]),
                        target_name=target_name,
                        target_type=str(choice["target_type"]),
                        target_column_id=target_column_id,
                        date_order=choice.get("date_order"),
                    )
                )
            dedup = None
            dedup_request = destination_request["dedup"]
            if dedup_request.get("enabled"):
                key_source_index = int(dedup_request["key_source_index"])
                target = mappings[key_source_index]
                if target["kind"] != "existing":
                    raise TableImportDraftError(
                        422,
                        self._problem(
                            "invalid_dedup",
                            "Duplicate checking requires an existing key column.",
                        ),
                    )
                snapshot_revision = int(
                    project.db.execute(
                        "SELECT COALESCE(MAX(id),0) FROM ops"
                    ).fetchone()[0]
                )
                dedup = FrozenTableImportDedup(
                    source_index=key_source_index,
                    target_column_id=int(target["column_id"]),
                    snapshot_revision=snapshot_revision,
                )
            plan = TableImportPlan(
                settings_revision=int(settings["revision"]),
                settings=self._table_settings(header),
                sheet_name=sheet_name,
                destination_sheet_id=destination_sheet_id,
                columns=tuple(frozen_columns),
                extra_values_target_name="extra values",
                dedup=dedup,
            )
            frozen = freeze_table_plan(
                directory,
                plan,
                require_exact_count=bool(plan.settings.skip_rows_end),
            )
            header["state"] = "committing"
            header["commit_key"] = idempotency_key
            self._store.save_header(directory, header)
            return frozen

    def record_commit(
        self,
        project_id: str,
        draft_id: str,
        *,
        idempotency_key: str,
        result: Mapping[str, Any],
    ) -> TableImportCommitResponse:
        directory, _ = self._store.open(project_id, draft_id)
        with self._store.lock(directory):
            header = self._store.open(project_id, draft_id)[1]
            if header.get("commit_key") != idempotency_key:
                raise TableImportDraftError(
                    409,
                    self._problem(
                        "commit_identity_changed",
                        "This import commit identity changed.",
                    ),
                )
            response = TableImportCommitResponse.model_validate(
                {
                    "schema_version": TABLE_IMPORT_SCHEMA_VERSION,
                    "draft_id": draft_id,
                    "status": "accepted",
                    **dict(result),
                }
            )
            existing = header.get("commit")
            if existing is not None and existing != response.model_dump(mode="json"):
                raise TableImportDraftError(
                    409,
                    self._problem(
                        "commit_identity_changed", "This import commit result changed."
                    ),
                )
            header["state"] = "committed"
            header["commit"] = response.model_dump(mode="json")
            self._store.save_header(directory, header)
            return response

    def discard(self, project_id: str, draft_id: str) -> None:
        directory, header = self._store.open(project_id, draft_id)
        with self._store.lock(directory):
            header = self._store.open(project_id, draft_id)[1]
            if header["state"] in {"committing", "committed"}:
                raise TableImportDraftError(
                    409,
                    self._problem(
                        "already_committed",
                        "A committed table import cannot be discarded.",
                    ),
                )
            self._store.discard(directory)

    def rerun(
        self, project_id: str, draft_id: str, *, worksheet: str
    ) -> TableImportDraft:
        source_directory, source_header = self._store.open(project_id, draft_id)
        if source_header["state"] != "committed" or source_header["source"].get(
            "format"
        ) not in {"xlsx", "xls", "ods"}:
            raise TableImportDraftError(
                409,
                self._problem(
                    "rerun_unavailable", "This import source cannot be reused."
                ),
            )
        if worksheet not in {
            item["name"]
            for item in source_header["source"].get("sheets", [])
            if not item.get("empty")
        }:
            raise TableImportDraftError(
                422,
                self._problem(
                    "worksheet_unavailable", "The selected worksheet is unavailable."
                ),
            )
        project = self._workspace.get(project_id)
        new_id, directory = self._store.create_directory(project_id)
        try:
            os.link(self._source_path(source_directory), directory / "source")
            timestamp = now()
            source = dict(source_header["source"])
            source["can_rerun_workbook"] = False
            requested = self._default_requested()
            requested["worksheet"] = worksheet
            info = self._source_info(source_directory, source_header)
            resolved = self._resolve_settings(
                info, TableImportRequestedSettings.model_validate(requested)
            )
            header = {
                "schema_version": TABLE_IMPORT_SCHEMA_VERSION,
                "draft_id": new_id,
                "project_id": project_id,
                "storage_identity": project.storage_identity,
                "state": "analyzing",
                "revision": 1,
                "created_at": timestamp,
                "updated_at": timestamp,
                "source": source,
                "settings": {
                    "revision": 0,
                    "requested": requested,
                    "resolved": resolved,
                },
                "column_choices": [],
                "destination_request": None,
                "analysis_job_id": None,
                "error": None,
                "commit_key": None,
                "commit": None,
            }
            initialize_table_intake(
                directory,
                ImportIntakeHeader(
                    project_id=project_id,
                    storage_identity=project.storage_identity,
                    envelope={},
                    intake_kind="table",
                ),
                source=IntakeTableImportSource(
                    filename=str(source["filename"]),
                    size=int(source["size"]),
                    sha256=str(source["sha256"]),
                    kind=str(source["format"]),
                    retain_for_workbook_reuse=True,
                ),
                settings=TableReadSettings(**resolved),
            )
            self._prime_prefix(directory, header)
            self._store.write_json(directory / "table-draft.json", header)
            self._enqueue_scan(project_id, directory, header)
            return self._snapshot(directory, header)
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    def _inspect_and_prime(
        self, directory: Path, header: dict[str, Any], *, initialize: bool
    ) -> None:
        try:
            info = inspect_source(self._source_path(directory))
        except TableReadError as exc:
            code = "empty_source" if "empty" in str(exc).casefold() else "invalid_table"
            raise TableImportDraftError(422, self._problem(code, str(exc))) from exc
        header["source"].update(
            {
                "format": info.kind,
                "sheets": [asdict(item) for item in info.sheets],
                "can_rerun_workbook": info.kind in {"xlsx", "xls", "ods"},
            }
        )
        requested = TableImportRequestedSettings.model_validate(
            self._default_requested()
        )
        header["settings"] = {
            "revision": 0,
            "requested": requested.model_dump(mode="json"),
            "resolved": self._resolve_settings(info, requested),
        }
        if initialize:
            initialize_table_intake(
                directory,
                ImportIntakeHeader(
                    project_id=str(header["project_id"]),
                    storage_identity=str(header["storage_identity"]),
                    envelope={},
                    intake_kind="table",
                ),
                source=IntakeTableImportSource(
                    filename=str(header["source"]["filename"]),
                    size=int(header["source"]["size"]),
                    sha256=str(header["source"]["sha256"]),
                    kind=info.kind,
                    retain_for_workbook_reuse=info.kind in {"xlsx", "xls", "ods"},
                ),
                settings=self._table_settings(header),
            )
        self._prime_prefix(directory, header)
        header["state"] = "analyzing"

    def _prime_prefix(self, directory: Path, header: dict[str, Any]) -> None:
        settings = self._table_settings(header)
        prefix = read_prefix(
            self._source_path(directory), settings, record_limit=_PREFIX_RECORDS
        )
        preview = PreviewAccumulator(
            head_rows=_PREVIEW_HEAD, tail_rows=2, column_limit=_PREVIEW_COLUMNS
        )
        stats = ColumnStatsAccumulator(
            prefix.columns,
            decimal_separator=settings.decimal_separator,
            thousands_separator=settings.thousands_separator,
        )
        issues = self._zero_issue_counts()
        data_rows = 0
        for row in prefix.rows:
            preview.accept(row)
            stats.accept(row)
            data_rows += int(row.role == "data")
            self._count_issues(issues, row.issues)
        scan = {
            "settings_revision": int(header["settings"]["revision"]),
            "record_count": len(prefix.rows),
            "data_rows": data_rows,
            "preview": self._preview_json(
                prefix.columns,
                preview.snapshot(complete=False),
                issues,
                settings.encoding,
            ),
            "stats": self._stats_json(stats.snapshot(complete=False)),
            "issue_counts": issues,
            "complete": False,
            "error": None,
        }
        revision, settings_hash, _ = read_table_settings(directory)
        write_table_scan(
            directory,
            TableImportScan(
                settings_revision=revision,
                settings_hash=settings_hash,
                scanned_rows=len(prefix.rows),
                estimated_rows=None,
                exact_rows=None,
                complete=False,
                problem_count=sum(issues.values()),
            ),
            expected_settings_revision=revision,
        )
        self._store.write_json(directory / "table-preview.json", scan)
        header["column_choices"] = self._default_choices(scan)
        name = (
            "Pasted rows"
            if header["source"]["input_kind"] == "paste"
            else Path(header["source"]["filename"]).stem or "Imported table"
        )
        header["destination_request"] = self._new_destination(scan, name[:80])

    def _snapshot(self, directory: Path, header: Mapping[str, Any]) -> TableImportDraft:
        scan = self._store.scan(directory)
        current = (
            scan is not None
            and header.get("settings") is not None
            and scan.get("settings_revision") == header["settings"]["revision"]
        )
        if not current:
            scan = None
        analysis = {
            "state": "error"
            if header.get("error")
            else (
                "complete"
                if scan and scan.get("complete")
                else ("idle" if header["state"] == "password_required" else "running")
            ),
            "job_id": header.get("analysis_job_id"),
            "scanned_rows": int(scan.get("data_rows", 0)) if scan else 0,
            "estimated_rows": None,
            "exact_rows": int(scan["data_rows"])
            if scan and scan.get("complete")
            else None,
            "complete": bool(scan and scan.get("complete")),
            "issue_counts": dict(scan.get("issue_counts", self._zero_issue_counts()))
            if scan
            else self._zero_issue_counts(),
            "error": header.get("error"),
        }
        columns = (
            self._public_columns(scan, header.get("column_choices", [])) if scan else []
        )
        destination = (
            self._public_destination(scan, header, columns)
            if scan and header.get("destination_request")
            else None
        )
        can_commit, reason = self._commit_readiness(directory, header, scan=scan)
        return TableImportDraft.model_validate(
            {
                "schema_version": TABLE_IMPORT_SCHEMA_VERSION,
                "id": header["draft_id"],
                "draft_id": header["draft_id"],
                "state": header["state"],
                "revision": header["revision"],
                "created_at": header["created_at"],
                "updated_at": header["updated_at"],
                "source": header["source"],
                "settings": header.get("settings"),
                "analysis": analysis,
                "preview": scan.get("preview") if scan else None,
                "columns": columns,
                "destination": destination,
                "can_commit": can_commit,
                "commit_blocked_reason": reason,
                "commit": header.get("commit"),
            }
        )

    def _commit_readiness(
        self,
        directory: Path,
        header: Mapping[str, Any],
        *,
        scan: Mapping[str, Any] | None = None,
    ) -> tuple[bool, str | None]:
        if header["state"] == "password_required":
            return False, "password_required"
        if header.get("error"):
            return False, "error"
        scan = scan if scan is not None else self._store.scan(directory)
        if not scan or not scan.get("stats", {}).get("columns"):
            return False, "empty"
        settings = header.get("settings") or {}
        resolved = settings.get("resolved") or {}
        if int(resolved.get("skip_rows_end", 0)) and not scan.get("complete"):
            return False, "analyzing"
        if not any(item.get("included") for item in header.get("column_choices", [])):
            return False, "empty"
        destination = header.get("destination_request")
        if destination is None or destination.get("comparison", {}).get(
            "state"
        ) not in {"ready"}:
            return False, "destination"
        if destination.get("dedup", {}).get("enabled") and not destination.get(
            "comparison", {}
        ).get("complete"):
            return False, "analyzing"
        return True, None

    def _compare_destination(
        self,
        project_id: str,
        directory: Path,
        header: Mapping[str, Any],
        request: Mapping[str, Any],
        *,
        scan: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        choices = {
            int(item["source_index"]): item
            for item in header["column_choices"]
            if item.get("included")
        }
        destination = request["destination"]
        if destination.get("kind") == "new":
            if not str(destination.get("sheet_name") or "").strip():
                raise TableImportDraftError(
                    422,
                    self._problem(
                        "invalid_destination", "A new sheet name is required."
                    ),
                )
            return {
                "state": "ready",
                "matched_columns": 0,
                "new_columns": len(choices),
                "type_clashes": 0,
                "duplicate_rows": 0,
                "import_rows": (
                    int(scan["data_rows"]) if scan and scan.get("complete") else None
                ),
                "complete": bool(scan and scan.get("complete")),
                "error": None,
            }
        if (
            destination.get("kind") != "existing"
            or type(destination.get("sheet_id")) is not int
        ):
            raise TableImportDraftError(
                422,
                self._problem(
                    "invalid_destination", "The import destination is invalid."
                ),
            )
        sheet_id = int(destination["sheet_id"])
        if (
            project.db.execute(
                "SELECT 1 FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
            ).fetchone()
            is None
        ):
            raise TableImportDraftError(
                409,
                self._problem(
                    "destination_changed", "The destination sheet is unavailable."
                ),
            )
        targets = {
            int(row["id"]): row
            for row in project.db.execute(
                "SELECT id,name,type FROM columns WHERE sheet_id=? AND active=1 AND hidden=0",
                (sheet_id,),
            )
        }
        mappings = request.get("mappings", [])
        seen_sources: set[int] = set()
        seen_targets: set[int] = set()
        matched = new = clashes = 0
        for mapping in mappings:
            source_index = mapping.get("source_index")
            if source_index not in choices or source_index in seen_sources:
                raise TableImportDraftError(
                    422,
                    self._problem(
                        "invalid_destination",
                        "Mappings must name each imported column once.",
                    ),
                )
            seen_sources.add(source_index)
            target = mapping.get("target", {})
            if target.get("kind") == "new":
                if not str(target.get("name") or "").strip():
                    raise TableImportDraftError(
                        422,
                        self._problem(
                            "invalid_destination", "New columns require names."
                        ),
                    )
                new += 1
            elif (
                target.get("kind") == "existing"
                and type(target.get("column_id")) is int
                and int(target["column_id"]) in targets
            ):
                target_id = int(target["column_id"])
                if target_id in seen_targets:
                    raise TableImportDraftError(
                        422,
                        self._problem(
                            "invalid_destination",
                            "Mappings cannot target a column twice.",
                        ),
                    )
                seen_targets.add(target_id)
                physical = self._physical_type(
                    str(choices[source_index]["target_type"])
                )
                if str(targets[target_id]["type"]) != physical:
                    clashes += 1
                else:
                    matched += 1
            else:
                raise TableImportDraftError(
                    409,
                    self._problem(
                        "destination_changed",
                        "A mapped destination column is unavailable.",
                    ),
                )
        if seen_sources != set(choices):
            raise TableImportDraftError(
                422,
                self._problem(
                    "invalid_destination", "Mappings must review every imported column."
                ),
            )
        dedup = request.get("dedup", {})
        duplicate_rows = 0
        import_rows = int(scan["data_rows"]) if scan and scan.get("complete") else None
        complete = bool(scan and scan.get("complete"))
        if dedup.get("enabled"):
            key = dedup.get("key_source_index")
            if type(key) is not int or key not in choices:
                raise TableImportDraftError(
                    422,
                    self._problem(
                        "invalid_dedup",
                        "Choose an imported key column for duplicate checking.",
                    ),
                )
            if not complete:
                return {
                    "state": "running",
                    "matched_columns": matched,
                    "new_columns": new,
                    "type_clashes": clashes,
                    "duplicate_rows": None,
                    "import_rows": None,
                    "complete": False,
                    "error": None,
                }
            key_mapping = next(item for item in mappings if item["source_index"] == key)
            target = key_mapping["target"]
            if target["kind"] == "existing":
                duplicate_rows = self._count_existing_duplicates(
                    project, directory, header, int(key), int(target["column_id"])
                )
                import_rows = max(0, int(scan["data_rows"]) - duplicate_rows)
        return {
            "state": "ready",
            "matched_columns": matched,
            "new_columns": new,
            "type_clashes": clashes,
            "duplicate_rows": duplicate_rows,
            "import_rows": import_rows,
            "complete": complete,
            "error": None,
        }

    def _count_existing_duplicates(
        self,
        project: Any,
        directory: Path,
        header: Mapping[str, Any],
        source_index: int,
        target_column_id: int,
    ) -> int:
        scratch = directory / ".dedup.db"
        scratch.unlink(missing_ok=True)
        connection = sqlite3.connect(scratch)
        try:
            connection.execute("CREATE TABLE existing_keys(value TEXT PRIMARY KEY)")
            rows = project.db.execute(
                "SELECT live.value_kind,live.value FROM current_cell_values live JOIN rows r ON r.id=live.row_id JOIN columns c ON c.id=live.column_id WHERE live.column_id=? AND r.hidden=0 AND c.active=1 AND c.hidden=0",
                (target_column_id,),
            )
            for row in rows:
                value = decode_stored_value(
                    row["value_kind"], row["value"], tolerate_errors=True
                )
                if value is not None and value != "":
                    connection.execute(
                        "INSERT OR IGNORE INTO existing_keys(value) VALUES (?)",
                        (self._exact_key(value),),
                    )
            connection.commit()
            choice = next(
                item
                for item in header["column_choices"]
                if int(item["source_index"]) == source_index
            )
            settings = self._table_settings(header)
            count = 0
            with open_table(
                self._source_path(directory), settings, total_record_count=None
            ) as reader:
                for row in reader:
                    if row.role != "data":
                        continue
                    converted = convert_cell(
                        row.cells[source_index],
                        choice["target_type"],
                        decimal_separator=settings.decimal_separator,
                        thousands_separator=settings.thousands_separator,
                        date_order=choice.get("date_order") or "month_first",
                    )
                    value = converted.value
                    if value is None or value == "":
                        continue
                    count += (
                        connection.execute(
                            "SELECT 1 FROM existing_keys WHERE value=?",
                            (self._exact_key(value),),
                        ).fetchone()
                        is not None
                    )
            return count
        finally:
            connection.close()
            scratch.unlink(missing_ok=True)

    @staticmethod
    def _exact_key(value: Any) -> str:
        return json.dumps(
            [type(value).__name__, value],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _public_destination(
        self,
        scan: Mapping[str, Any],
        header: Mapping[str, Any],
        columns: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        request = header["destination_request"]
        by_index = {int(item["source_index"]): item for item in columns}
        mappings = []
        project = self._workspace.get(header["project_id"])
        target_rows: dict[int, Any] = {}
        if request["destination"]["kind"] == "existing":
            sheet_id = int(request["destination"]["sheet_id"])
            target_rows = {
                int(row["id"]): row
                for row in project.db.execute(
                    "SELECT id,name,type FROM columns WHERE sheet_id=? AND active=1 AND hidden=0",
                    (sheet_id,),
                )
            }
        for raw in request["mappings"]:
            source = by_index[int(raw["source_index"])]
            target = raw["target"]
            match = "new"
            detail = None
            if target["kind"] == "existing":
                row = target_rows.get(int(target["column_id"]))
                if row is None:
                    match, detail = "type_clash", "Destination column is unavailable."
                elif str(row["type"]) != self._physical_type(source["target_type"]):
                    match, detail = "type_clash", f"Sheet column is {row['type']}."
                else:
                    match = (
                        "matched"
                        if self._normalized_name(source["name"])
                        == self._normalized_name(str(row["name"]))
                        else "similar"
                    )
            mappings.append(
                {
                    "source_index": source["source_index"],
                    "source_name": source["name"],
                    "source_type": source["target_type"],
                    "target": target,
                    "match": match,
                    "detail": detail,
                }
            )
        return {
            "configuration": request["destination"],
            "mappings": mappings,
            "dedup": request["dedup"],
            "comparison": request["comparison"],
        }

    def _public_columns(
        self, scan: Mapping[str, Any], choices: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        by_choice = {int(item["source_index"]): item for item in choices}
        result = []
        for stat in scan["stats"]["columns"]:
            source = stat["column"]
            choice = by_choice.get(
                int(source["index"]),
                {
                    "included": stat["suggested_type"] != "omit",
                    "target_type": stat["suggested_type"],
                    "date_order": None,
                },
            )
            target = choice["target_type"]
            counts = stat["counts_by_type"].get(target, self._zero_conversion_counts())
            samples = [
                self._conversion_json(
                    value, target, choice.get("date_order"), header_settings=None
                )
                for value in stat["sample_values"][:5]
            ]
            needs = [
                {"leading_zero": "leading_zeros"}.get(item, item)
                for item in stat["needs_review"]
            ]
            if stat["ambiguous_date_order"] and "ambiguous_date_order" not in needs:
                needs.append("ambiguous_date_order")
            if counts.get("time_dropped") and "time_dropped" not in needs:
                needs.append("time_dropped")
            result.append(
                {
                    "source_index": source["index"],
                    "name": source["name"],
                    "original_name": source["original_name"],
                    "name_status": source["name_status"],
                    "included": bool(choice["included"]),
                    "target_type": target,
                    "date_order": choice.get("date_order"),
                    "suggested_type": stat["suggested_type"],
                    "ambiguous_date_order": stat["ambiguous_date_order"],
                    "needs_review": needs,
                    "samples": samples,
                    "conversion_counts": counts,
                    "stats_complete": bool(scan["stats"]["complete"]),
                }
            )
        return result

    @staticmethod
    def _conversion_json(
        original: str, target: str, date_order: str | None, header_settings: Any
    ) -> dict[str, Any]:
        converted = convert_cell(
            RawCell(original), target, date_order=date_order or "month_first"
        )
        return {
            "original": converted.original,
            "value": converted.value,
            "display": converted.display,
            "outcome": converted.outcome,
        }

    @staticmethod
    def _preview_json(
        columns: Sequence[Any],
        preview: Any,
        issues: Mapping[str, int],
        encoding: str | None,
    ) -> dict[str, Any]:
        payload = asdict(preview)
        payload["columns"] = [asdict(item) for item in columns]
        payload["expected_values"] = len(columns)
        payload["notice"] = TableImportService._notice(
            len(columns), issues, preview.complete, encoding
        )
        return payload

    @staticmethod
    def _notice(
        width: int, issues: Mapping[str, int], complete: bool, encoding: str | None
    ) -> dict[str, Any]:
        if issues["extra_values"]:
            kind, count = "extra_values", issues["extra_values"]
        elif issues["unreadable_cells"]:
            kind, count = "unreadable_characters", issues["unreadable_cells"]
        elif width <= 1:
            kind, count = "one_column", None
        elif issues["missing_values"]:
            kind, count = "some_wrong_width", issues["missing_values"]
        elif not complete:
            kind, count = "counting", None
        else:
            kind, count = "consistent", None
        return {
            "kind": kind,
            "count": count,
            "common_values": None,
            "expected_values": width,
            "encoding": encoding,
        }

    @staticmethod
    def _stats_json(stats: Any) -> dict[str, Any]:
        return {
            "columns": [asdict(item) for item in stats.columns],
            "rows_checked": stats.rows_checked,
            "inference_rows_checked": stats.inference_rows_checked,
            "complete": stats.complete,
        }

    @staticmethod
    def _default_choices(scan: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "source_index": int(item["column"]["index"]),
                "included": item["suggested_type"] != "omit",
                "target_type": item["suggested_type"],
                "date_order": ("month_first" if item["ambiguous_date_order"] else None),
            }
            for item in scan["stats"]["columns"]
        ]

    @staticmethod
    def _new_destination(scan: Mapping[str, Any], sheet_name: str) -> dict[str, Any]:
        mappings = [
            {
                "source_index": int(item["column"]["index"]),
                "target": {"kind": "new", "name": item["column"]["name"]},
            }
            for item in scan["stats"]["columns"]
            if item["suggested_type"] != "omit"
        ]
        return {
            "destination": {"kind": "new", "sheet_name": sheet_name},
            "mappings": mappings,
            "dedup": {"enabled": False, "key_source_index": None},
            "comparison": {
                "state": "ready",
                "matched_columns": 0,
                "new_columns": len(mappings),
                "type_clashes": 0,
                "duplicate_rows": 0,
                "import_rows": None,
                "complete": False,
                "error": None,
            },
        }

    @staticmethod
    def _default_requested() -> dict[str, Any]:
        return {
            "delimiter": None,
            "encoding": None,
            "decimal_separator": "auto",
            "thousands_separator": "auto",
            "worksheet": None,
            "skip_rows_start": 0,
            "first_row_is_header": True,
            "skip_rows_end": 0,
        }

    @staticmethod
    def _resolve_settings(
        info: Any, requested: TableImportRequestedSettings
    ) -> dict[str, Any]:
        worksheet = requested.worksheet
        if info.kind != "delimited":
            available = [item for item in info.sheets if not item.empty]
            if worksheet is None and available:
                worksheet = available[0].name
            if worksheet not in {item.name for item in info.sheets}:
                raise TableImportDraftError(
                    422,
                    TableImportService._problem(
                        "worksheet_unavailable",
                        "The selected worksheet is unavailable.",
                    ),
                )
        decimal = (
            info.detected_decimal_separator
            if requested.decimal_separator == "auto"
            else requested.decimal_separator
        )
        thousands_requested = requested.thousands_separator
        thousands = (
            info.detected_thousands_separator
            if thousands_requested == "auto"
            else (None if thousands_requested == "none" else thousands_requested)
        )
        return {
            "kind": info.kind,
            "delimiter": (requested.delimiter or info.detected_delimiter)
            if info.kind == "delimited"
            else None,
            "encoding": (requested.encoding or info.detected_encoding)
            if info.kind == "delimited"
            else None,
            "decimal_separator": decimal or ".",
            "thousands_separator": thousands,
            "worksheet": worksheet if info.kind != "delimited" else None,
            "skip_rows_start": requested.skip_rows_start,
            "first_row_is_header": requested.first_row_is_header,
            "skip_rows_end": requested.skip_rows_end,
        }

    def _source_info(self, directory: Path, header: Mapping[str, Any]) -> Any:
        return inspect_source(self._source_path(directory))

    def _table_settings(self, header: Mapping[str, Any]) -> TableReadSettings:
        return TableReadSettings(**header["settings"]["resolved"])

    @staticmethod
    def _source_path(directory: Path) -> Path:
        decrypted = directory / "decrypted"
        return decrypted if decrypted.exists() else directory / "source"

    def _enqueue_scan(
        self, project_id: str, directory: Path, header: dict[str, Any]
    ) -> None:
        if self._enqueue_analysis is None:
            return
        job_id = self._enqueue_analysis(
            project_id, str(header["draft_id"]), int(header["settings"]["revision"])
        )
        header["analysis_job_id"] = job_id
        self._store.write_json(directory / "table-draft.json", header)

    @staticmethod
    def _require_open_revision(header: Mapping[str, Any], expected: int) -> None:
        if header["state"] in {"committing", "committed"}:
            raise TableImportDraftError(
                409,
                TableImportService._problem(
                    "already_committed", "This table import can no longer be changed."
                ),
            )
        if int(header["revision"]) != expected:
            raise TableImportDraftError(
                409,
                TableImportService._problem(
                    "revision_conflict",
                    "The table import changed. Refresh and try again.",
                ),
            )

    def _require_current_scan(
        self, directory: Path, header: Mapping[str, Any]
    ) -> dict[str, Any]:
        scan = self._store.scan(directory)
        if (
            scan is None
            or header.get("settings") is None
            or scan.get("settings_revision") != header["settings"]["revision"]
        ):
            raise TableImportDraftError(
                409,
                self._problem(
                    "analysis_incomplete", "The current preview is not ready."
                ),
            )
        return scan

    @staticmethod
    def _physical_type(target: str) -> str:
        return target

    @staticmethod
    def _normalized_name(value: str) -> str:
        return "".join(
            character for character in value.casefold() if character not in {" ", "_"}
        )

    @staticmethod
    def _problem(code: str, message: str) -> dict[str, str]:
        return {"code": code, "message": message}

    @staticmethod
    def _zero_issue_counts() -> dict[str, int]:
        return {
            "extra_values": 0,
            "missing_values": 0,
            "malformed_records": 0,
            "unreadable_cells": 0,
        }

    @staticmethod
    def _zero_conversion_counts() -> dict[str, int]:
        return {
            "rows_checked": 0,
            "valid": 0,
            "blank": 0,
            "changed": 0,
            "invalid": 0,
            "leading_zero_loss": 0,
            "time_dropped": 0,
        }

    @staticmethod
    def _count_issues(counts: dict[str, int], issues: Sequence[Any]) -> None:
        mapping = {
            "extra_values": "extra_values",
            "missing_values": "missing_values",
            "malformed_record": "malformed_records",
            "unreadable_cell": "unreadable_cells",
        }
        for issue in issues:
            counts[mapping[issue.code]] += 1

    @staticmethod
    def _copy_upload(upload: AdmittedUpload, target: Path) -> None:
        with (
            TableImportService._upload_source(upload) as source,
            target.open("xb") as sink,
        ):
            digest = hashlib.sha256()
            size = 0
            while chunk := source.read(_COPY_CHUNK):
                sink.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            sink.flush()
            os.fsync(sink.fileno())
        if size != upload.size or digest.hexdigest() != upload.sha256:
            raise TableImportDraftError(
                409,
                TableImportService._problem(
                    "source_changed", "The uploaded source changed during admission."
                ),
            )

    @staticmethod
    @contextlib.contextmanager
    def _upload_source(upload: AdmittedUpload):
        if upload.open_source is not None:
            with upload.open_source() as source:
                yield source
            return
        assert upload.source is not None
        upload.source.seek(0)
        try:
            yield upload.source
        finally:
            upload.source.seek(0)

    @staticmethod
    def _reject_known_unsupported(path: Path) -> None:
        with path.open("rb") as source:
            prefix = source.read(16)
        if prefix.startswith(
            (b"%PDF-", b"\x89PNG", b"GIF87a", b"GIF89a", b"\xff\xd8\xff")
        ):
            raise TableImportDraftError(
                422,
                TableImportService._problem(
                    "unsupported_source",
                    "Choose a CSV, TSV, text, Excel, or OpenDocument spreadsheet.",
                ),
            )

    @staticmethod
    def _office_encrypted(path: Path) -> bool:
        try:
            import msoffcrypto

            with path.open("rb") as source:
                office = msoffcrypto.OfficeFile(source)
                return bool(office.is_encrypted())
        except Exception:
            return False

    @staticmethod
    def _decrypt_office(source_path: Path, output_path: Path, password: str) -> None:
        temporary = output_path.with_name(".decrypted.tmp")
        temporary.unlink(missing_ok=True)
        try:
            import msoffcrypto

            with source_path.open("rb") as source, temporary.open("xb") as sink:
                office = msoffcrypto.OfficeFile(source)
                office.load_key(password=password, verify_password=True)
                office.decrypt(sink)
                sink.flush()
                os.fsync(sink.fileno())
            os.replace(temporary, output_path)
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            raise TableImportDraftError(
                422,
                TableImportService._problem(
                    "invalid_password", "The workbook password is incorrect."
                ),
            ) from exc


__all__ = ["FrozenTableImport", "TableImportService"]
