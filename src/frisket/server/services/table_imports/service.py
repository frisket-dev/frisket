"""Draft lifecycle for streaming one-file table imports."""

from __future__ import annotations

import copy
import os
import shutil
from dataclasses import asdict, replace
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
    frozen_table_import,
    initialize_table_intake,
    read_table_settings,
    replace_table_dedup_keys,
    replace_table_settings,
    table_dedup_revision,
    table_import_status,
    write_table_scan,
)
from frisket.server.services.import_uploads import AdmittedUpload
from frisket.table_import import (
    TableReadError,
    TableReadSettings,
    inspect_source,
)

from .analysis import (
    build_complete_scan,
    build_prefix_scan,
    default_choices,
    public_columns,
    zero_issue_counts,
)
from .destination import (
    compare_destination,
    iter_existing_dedup_keys,
    new_destination,
    public_destination,
)
from .storage import TableImportDraftError, TableImportDraftStore, now
from .source import (
    InvalidWorkbookPassword,
    adopt_decrypted_source,
    decrypt_office,
    known_unsupported,
    office_encrypted,
    stage_upload,
)


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
        max_rows: int | None = None,
    ) -> TableImportDraft:
        if input_kind not in {"file", "paste"}:
            raise TableImportDraftError(
                422, self._problem("invalid_table", "invalid table source kind")
            )
        if upload.size == 0:
            raise TableImportDraftError(
                422, self._problem("empty_source", "The selected source is empty.")
            )
        if max_rows is not None and (
            isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 0
        ):
            raise ValueError("max_rows must be a non-negative integer or None")
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
        self._store.purge_expired(project_id)
        project = self._workspace.get(project_id)
        draft_id, directory = self._store.create_directory(project_id)
        try:
            source_path = directory / "source"
            try:
                stage_upload(upload, source_path)
            except ValueError as exc:
                raise TableImportDraftError(
                    409, self._problem("source_changed", str(exc))
                ) from exc
            if known_unsupported(source_path):
                raise TableImportDraftError(
                    422,
                    self._problem(
                        "unsupported_source",
                        "Choose a CSV, TSV, text, Excel, or OpenDocument spreadsheet.",
                    ),
                )
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
                "max_rows": max_rows,
                "state": "analyzing",
                "revision": 1,
                "created_at": timestamp,
                "updated_at": timestamp,
                "source": source,
                "settings": None,
                "column_choices": [],
                "columns_reviewed": False,
                "destination_request": None,
                "destination_reviewed": False,
                "analysis_job_id": None,
                "error": None,
                "commit_key": None,
                "commit": None,
            }
            if office_encrypted(source_path):
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
            try:
                decrypt_office(directory / "source", directory / "decrypted", password)
            except InvalidWorkbookPassword as exc:
                raise TableImportDraftError(
                    422,
                    self._problem(
                        "invalid_password", "The workbook password is incorrect."
                    ),
                ) from exc
            size, digest = adopt_decrypted_source(directory)
            header["source"]["size"] = size
            header["source"]["sha256"] = digest
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
            header["state"] = "analyzing"
            header["analysis_job_id"] = None
            header["error"] = None
            header["destination_request"] = None
            header["columns_reviewed"] = False
            header["destination_reviewed"] = False
            prefix_scan = self._build_prefix(directory, header)
            replace_table_settings(
                directory,
                self._table_settings(header),
                expected_revision=next_settings_revision - 1,
            )
            (directory / "table-preview.json").unlink(missing_ok=True)
            self._publish_prefix(directory, header, prefix_scan)
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
            stats_by_index = {
                int(item["column"]["index"]): item for item in scan["stats"]["columns"]
            }
            known = set(stats_by_index)
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
                if target in {"date", "datetime"} and date_order is None:
                    date_order = (
                        stats_by_index[index].get("suggested_date_order")
                        or "month_first"
                    )
                elif target not in {"date", "datetime"} and date_order is not None:
                    raise TableImportDraftError(
                        422,
                        self._problem(
                            "invalid_columns",
                            "Date order is only available for date columns.",
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
            header["columns_reviewed"] = True
            header["destination_request"] = None
            header["destination_reviewed"] = False
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
            scan = self._require_current_scan(directory, header)
            captured = dict(header)
        request["comparison"] = compare_destination(
            self._workspace,
            project_id,
            directory,
            captured,
            request,
            source_path=self._source_path(directory),
            settings=self._table_settings(captured),
            scan=scan,
        )
        with self._store.lock(directory):
            current = self._store.open(project_id, draft_id)[1]
            self._require_open_revision(current, expected_revision)
            if current.get("settings") != captured.get("settings") or current.get(
                "column_choices"
            ) != captured.get("column_choices"):
                raise TableImportDraftError(
                    409,
                    self._problem(
                        "revision_conflict",
                        "The table import changed. Refresh and try again.",
                    ),
                )
            current["destination_request"] = request
            current["columns_reviewed"] = True
            current["destination_reviewed"] = True
            self._store.save_header(directory, current)
            return self._snapshot(directory, current)

    def scan(
        self, project_id: str, draft_id: str, *, settings_revision: int
    ) -> TableImportDraft:
        directory, header = self._store.open(project_id, draft_id)
        if (
            header.get("settings") is None
            or int(header["settings"]["revision"]) != settings_revision
        ):
            return self._snapshot(directory, header)
        resolved = self._table_settings(header)
        try:
            scan = build_complete_scan(
                self._source_path(directory),
                resolved,
                settings_revision=settings_revision,
            )
        except TableReadError as exc:
            return self._publish_analysis_error(
                project_id,
                draft_id,
                settings_revision=settings_revision,
                error=str(exc),
            )

        published, reviewed_destination = self._publish_complete_scan(
            project_id,
            draft_id,
            settings_revision=settings_revision,
            scan=scan,
        )
        if reviewed_destination is None:
            return self._snapshot(directory, published)

        reviewed_header, reviewed_request = reviewed_destination
        comparison = compare_destination(
            self._workspace,
            project_id,
            directory,
            reviewed_header,
            reviewed_request,
            source_path=self._source_path(directory),
            settings=resolved,
            scan=scan,
        )
        with self._store.lock(directory):
            current = self._store.open(project_id, draft_id)[1]
            if (
                self._analysis_publishable(current, settings_revision)
                and current.get("destination_request") == reviewed_request
            ):
                current["destination_request"]["comparison"] = comparison
                self._store.save_header(directory, current)
            return self._snapshot(directory, current)

    def _publish_complete_scan(
        self,
        project_id: str,
        draft_id: str,
        *,
        settings_revision: int,
        scan: Mapping[str, Any],
    ) -> tuple[dict[str, Any], tuple[dict[str, Any], dict[str, Any]] | None]:
        directory, _ = self._store.open(project_id, draft_id)
        with self._store.lock(directory):
            current = self._store.open(project_id, draft_id)[1]
            if not self._analysis_publishable(current, settings_revision):
                return copy.deepcopy(current), None
            revision, settings_hash, _ = read_table_settings(directory)
            write_table_scan(
                directory,
                TableImportScan(
                    settings_revision=revision,
                    settings_hash=settings_hash,
                    scanned_rows=int(scan["record_count"]),
                    estimated_rows=None,
                    exact_rows=int(scan["data_rows"]),
                    complete=True,
                    problem_count=sum(scan["issue_counts"].values()),
                ),
                expected_settings_revision=revision,
            )
            self._store.write_json(directory / "table-preview.json", scan)
            current["state"] = "ready"
            current["error"] = None
            if not current.get("columns_reviewed"):
                current["column_choices"] = default_choices(scan)
            if not current.get("destination_reviewed"):
                previous = current.get("destination_request") or {}
                configuration = previous.get("destination") or {}
                name = str(configuration.get("sheet_name") or "Imported table")
                current["destination_request"] = new_destination(scan, name)
            reviewed_destination = None
            if current.get("destination_reviewed") and current.get(
                "destination_request"
            ):
                reviewed_destination = (
                    copy.deepcopy(current),
                    copy.deepcopy(current["destination_request"]),
                )
            self._store.save_header(directory, current)
            return copy.deepcopy(current), reviewed_destination

    def _publish_analysis_error(
        self,
        project_id: str,
        draft_id: str,
        *,
        settings_revision: int,
        error: str,
    ) -> TableImportDraft:
        directory, _ = self._store.open(project_id, draft_id)
        with self._store.lock(directory):
            current = self._store.open(project_id, draft_id)[1]
            if self._analysis_publishable(current, settings_revision):
                current["state"] = "error"
                current["error"] = error
                self._store.save_header(directory, current)
            return self._snapshot(directory, current)

    @classmethod
    def _analysis_publishable(
        cls, header: Mapping[str, Any], settings_revision: int
    ) -> bool:
        settings = header.get("settings")
        return bool(
            settings is not None
            and int(settings["revision"]) == settings_revision
            and not cls._commit_started(header)
        )

    def run_analysis(
        self, project_id: str, draft_id: str, settings_revision: int
    ) -> TableImportDraft:
        """Run the revisioned full scan from a normal queue callback."""

        return self.scan(project_id, draft_id, settings_revision=settings_revision)

    def freeze_for_commit(
        self,
        project_id: str,
        draft_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> FrozenTableImport | TableImportCommitResponse:
        directory, _ = self._store.open(project_id, draft_id)
        prepared: tuple[TableImportPlan, int | None] | None = None
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
            if header["state"] == "committing":
                if header.get("commit_key") != idempotency_key:
                    raise TableImportDraftError(
                        409,
                        self._problem(
                            "commit_identity_changed",
                            "This import is already committing under another request.",
                        ),
                    )
            else:
                self._require_open_revision(header, expected_revision)
                can_commit, reason = self._commit_readiness(directory, header)
                if not can_commit:
                    if reason == "limit":
                        raise TableImportDraftError(
                            413,
                            self._problem(
                                "source_too_large",
                                "The table exceeds the deployment row limit.",
                            ),
                        )
                    code = (
                        "analysis_incomplete"
                        if reason == "analyzing"
                        else "invalid_table"
                    )
                    raise TableImportDraftError(
                        409,
                        self._problem(code, "The table import is not ready to commit."),
                    )
                scan = self._require_current_scan(directory, header)
                prepared = self._build_frozen_plan(
                    project_id, scan, header, dedup_snapshot_revision=0
                )
                header["state"] = "committing"
                header["commit_key"] = idempotency_key
                self._store.save_header(directory, header)
            captured = copy.deepcopy(header)

        with self._store.commit_lock(directory):
            phase = table_import_status(directory).phase
            if phase != "draft":
                try:
                    return frozen_table_import(directory)
                except ValueError as exc:
                    raise TableImportDraftError(
                        409,
                        self._problem(
                            "commit_state_changed",
                            "The table import commit state is unavailable.",
                        ),
                    ) from exc
            if prepared is None:
                scan = self._require_current_scan(directory, captured)
                prepared = self._build_frozen_plan(
                    project_id, scan, captured, dedup_snapshot_revision=0
                )
            plan, dedup_target_column = prepared
            if dedup_target_column is not None:
                current_revision = table_dedup_revision(directory)
                snapshot_revision = replace_table_dedup_keys(
                    directory,
                    iter_existing_dedup_keys(
                        self._workspace.get(project_id), dedup_target_column
                    ),
                    expected_revision=current_revision,
                )
                assert plan.dedup is not None
                plan = replace(
                    plan,
                    dedup=replace(plan.dedup, snapshot_revision=snapshot_revision),
                )
            try:
                return freeze_table_plan(
                    directory,
                    plan,
                    require_exact_count=bool(plan.settings.skip_rows_end),
                )
            except ValueError as exc:
                if table_import_status(directory).phase != "draft":
                    return frozen_table_import(directory)
                raise TableImportDraftError(
                    409,
                    self._problem(
                        "commit_state_changed",
                        "The table import changed while committing.",
                    ),
                ) from exc

    def _build_frozen_plan(
        self,
        project_id: str,
        scan: Mapping[str, Any],
        header: Mapping[str, Any],
        *,
        dedup_snapshot_revision: int,
    ) -> tuple[TableImportPlan, int | None]:
        settings = header["settings"]
        destination_request = header["destination_request"]
        assert settings is not None and destination_request is not None
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
                        "destination_changed", "The destination sheet is unavailable."
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
                    "SELECT name,type FROM columns WHERE id=? AND sheet_id=? "
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
                if str(target_row["type"]) != str(choice["target_type"]):
                    raise TableImportDraftError(
                        409,
                        self._problem(
                            "destination_changed",
                            "A destination column type changed.",
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
        dedup_target_column = None
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
            dedup_target_column = int(target["column_id"])
            dedup = FrozenTableImportDedup(
                source_index=key_source_index,
                target_column_id=dedup_target_column,
                snapshot_revision=dedup_snapshot_revision,
            )
        return (
            TableImportPlan(
                settings_revision=int(settings["revision"]),
                settings=self._table_settings(header),
                sheet_name=sheet_name,
                destination_sheet_id=destination_sheet_id,
                columns=tuple(frozen_columns),
                extra_values_target_name="extra values",
                dedup=dedup,
            ),
            dedup_target_column,
        )

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
        self,
        project_id: str,
        draft_id: str,
        *,
        worksheet: str,
        max_rows: int | None = None,
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
        if max_rows is not None and (
            isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 0
        ):
            raise ValueError("max_rows must be a non-negative integer or None")
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
                "max_rows": max_rows,
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
                "columns_reviewed": False,
                "destination_request": None,
                "destination_reviewed": False,
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
                    max_rows=max_rows,
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
                    max_rows=header.get("max_rows"),
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
        self._publish_prefix(directory, header, self._build_prefix(directory, header))

    def _build_prefix(
        self, directory: Path, header: Mapping[str, Any]
    ) -> dict[str, Any]:
        try:
            return build_prefix_scan(
                self._source_path(directory),
                self._table_settings(header),
                settings_revision=int(header["settings"]["revision"]),
            )
        except TableReadError as exc:
            raise TableImportDraftError(
                422, self._problem("invalid_table", str(exc))
            ) from exc

    def _publish_prefix(
        self, directory: Path, header: dict[str, Any], scan: Mapping[str, Any]
    ) -> None:
        revision, settings_hash, _ = read_table_settings(directory)
        write_table_scan(
            directory,
            TableImportScan(
                settings_revision=revision,
                settings_hash=settings_hash,
                scanned_rows=int(scan["record_count"]),
                estimated_rows=None,
                exact_rows=None,
                complete=False,
                problem_count=sum(scan["issue_counts"].values()),
            ),
            expected_settings_revision=revision,
        )
        self._store.write_json(directory / "table-preview.json", scan)
        header["column_choices"] = default_choices(scan)
        header["columns_reviewed"] = False
        name = (
            "Pasted rows"
            if header["source"]["input_kind"] == "paste"
            else Path(header["source"]["filename"]).stem or "Imported table"
        )
        header["destination_request"] = new_destination(scan, name[:80])
        header["destination_reviewed"] = False

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
            "issue_counts": dict(scan.get("issue_counts", zero_issue_counts()))
            if scan
            else zero_issue_counts(),
            "error": header.get("error"),
        }
        columns = (
            public_columns(
                scan,
                header.get("column_choices", []),
                (header.get("settings") or {}).get("resolved", {}),
            )
            if scan
            else []
        )
        destination = (
            public_destination(self._workspace, header, columns)
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
        max_rows = header.get("max_rows")
        if (
            max_rows is not None
            and scan.get("complete")
            and int(scan.get("data_rows", 0)) > int(max_rows)
        ):
            return False, "limit"
        if (
            int(scan.get("issue_counts", {}).get("unreadable_cells", 0)) > 0
            and (settings.get("requested") or {}).get("encoding") is None
        ):
            return False, "encoding"
        if not any(item.get("included") for item in header.get("column_choices", [])):
            return False, "empty"
        destination = header.get("destination_request")
        comparison = destination.get("comparison", {}) if destination else {}
        if (
            destination is None
            or comparison.get("state") != "ready"
            or int(comparison.get("type_clashes", 0)) > 0
        ):
            return False, "destination"
        if destination.get("dedup", {}).get("enabled") and not destination.get(
            "comparison", {}
        ).get("complete"):
            return False, "analyzing"
        return True, None

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

    @staticmethod
    def _commit_started(header: Mapping[str, Any]) -> bool:
        return bool(
            header.get("state") in {"committing", "committed"}
            or header.get("commit_key")
            or header.get("commit")
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
    def _problem(code: str, message: str) -> dict[str, str]:
        return {"code": code, "message": message}


__all__ = ["FrozenTableImport", "TableImportService"]
