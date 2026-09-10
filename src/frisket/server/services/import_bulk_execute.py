"""Per-format execution and bounded result projection for bulk plans."""

from __future__ import annotations

import hashlib
import json
import logging
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from frisket.actions.types import EmailInput
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.server.route_errors import RouteError
from frisket.server.services import import_bulk_sources
from frisket.server.services.import_bulk_plan import (
    INVALID_CSV_HEADER,
    stable_identifier,
    stem,
)
from frisket.server.services.import_bulk_types import ImportBulkRouteError
from frisket.server.services.import_csv_analysis import (
    CsvFileScan,
    scan_csv_stream,
    widen_csv_scans,
)
from frisket.server.services.import_csv_execute import run_scanned_csv_import
from frisket.server.services.import_files import ImportFilesUploadService
from frisket.server.services.action_runs import v1_action_result_http_status
from frisket.server.services.import_tabular import materialized_sheet_output
from frisket.server.services.import_uploads import AdmittedUpload
from frisket.server.services.import_xlsx import ImportXlsxUploadService
from frisket.server.workspace import Workspace

MAX_FAILURE_DETAILS = 20
STOPPED_FAILURE = "Not attempted because bulk execution stopped after a server error."
UNEXPECTED_FAILURE = "Import failed due to a server error."

LOG = logging.getLogger("frisket.server")


class _BulkExecutionFatal(Exception):
    """A stopped CSV child carries its precise progress boundary outward."""

    def __init__(
        self,
        cause: Exception,
        current: dict[str, Any],
        unattempted: list[dict[str, Any]],
    ) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.current = current
        self.unattempted = unattempted


@dataclass(frozen=True)
class _CsvActionOutput:
    sheet_id: int
    sheet_name: str


class BulkExecutor:
    def __init__(self, workspace: Workspace):
        self.xlsx = ImportXlsxUploadService(workspace)
        self.files = ImportFilesUploadService(workspace)

    def execute(
        self,
        project: Any,
        project_id: str,
        plan: Path,
        outputs: list[dict[str, Any]],
        staged: list[dict[str, Any]],
        decisions: dict[str, str],
        *,
        deps: ExecutorDeps,
        max_rows: int | None,
    ) -> dict[str, Any]:
        by_path = {str(x["logical_path"]): x for x in staged}
        created = []
        failed: list[dict[str, Any]] = []
        failed_count = 0

        def record_failure(failure: dict[str, Any]) -> None:
            nonlocal failed_count
            failed_count += 1
            if len(failed) < MAX_FAILURE_DETAILS:
                failed.append(failure)

        def record_omitted_failures(count: int) -> None:
            nonlocal failed_count
            failed_count += count

        warnings: list[str] = []
        stopped = False

        def record_stopped(
            current: dict[str, Any],
            unattempted: list[dict[str, Any]],
            error: str,
        ) -> None:
            record_failure(failure_output(current, error))
            for remaining in unattempted:
                record_failure(failure_output(remaining, STOPPED_FAILURE))

        for output_index, output in enumerate(outputs):
            items = [by_path[str(p)] for p in output["logical_paths"]]
            kind = output["kind"]
            try:
                if kind == "csv_group":
                    _good, bad, bad_omitted = self._csv(
                        project,
                        output,
                        items,
                        decisions.get(str(output.get("question_id"))),
                        plan,
                        max_rows=max_rows,
                        project_id=project_id,
                        deps=deps,
                        on_created=created.append,
                    )
                    for failure in bad:
                        record_failure(failure)
                    record_omitted_failures(bad_omitted)
                    continue
                if kind == "xlsx":
                    result = self._xlsx(
                        project, project_id, output, items, plan, deps=deps
                    )
                elif kind == "files_group":
                    result = self._files(
                        project, project_id, output, items, plan, deps=deps
                    )
                elif kind == "email":
                    result, email_warnings = self._email(
                        project, project_id, output, items, plan, deps=deps
                    )
                    warnings = bounded_warnings([*warnings, *email_warnings])
                else:
                    raise ImportBulkRouteError(400, "unsupported bulk output kind")
                created.append(result)
            except RouteError as exc:
                if exc.status_code >= 500:
                    if not created:
                        raise
                    record_stopped(output, outputs[output_index + 1 :], str(exc.detail))
                    stopped = True
                    break
                record_failure(failure_output(output, str(exc.detail)))
            except _BulkExecutionFatal as exc:
                if not created:
                    raise exc.cause
                log_unexpected_fatal(exc.cause)
                record_stopped(
                    exc.current,
                    [*exc.unattempted, *outputs[output_index + 1 :]],
                    fatal_error_detail(exc.cause),
                )
                stopped = True
                break
            except Exception:
                if not created:
                    raise
                LOG.exception(
                    "bulk_execution_stopped_after_unexpected_error",
                    extra={"event": "bulk_execution_stopped_after_unexpected_error"},
                )
                record_stopped(output, outputs[output_index + 1 :], UNEXPECTED_FAILURE)
                stopped = True
                break
        total = len(staged)
        imported = sum(len(result["logical_paths"]) for result in created)
        if not total:
            message = "No importable files found."
        elif stopped:
            message = (
                f"Imported {imported} of {total} files; "
                "execution stopped after a server error."
            )
        elif failed_count:
            message = f"Imported {imported} of {total} files."
        else:
            message = f"Imported {imported} files."
        return {
            "created": created,
            "failed": failed,
            "failed_omitted": failed_count - len(failed),
            "first_sheet_id": created[0]["sheet_id"] if created else None,
            "warnings": warnings,
            "message": message,
        }

    def _scan(
        self, root: Path, item: dict[str, Any], force_text: bool = False
    ) -> CsvFileScan:
        source = import_bulk_sources.open_verified_source(root, item)
        try:
            return scan_csv_stream(
                source,
                logical_path=str(item["logical_path"]),
                force_text_columns=force_text,
            )
        except Exception:
            source.close()
            raise

    def _csv(
        self,
        project: Any,
        output: dict[str, Any],
        items: list[dict[str, Any]],
        decision: str | None,
        root: Path,
        *,
        max_rows: int | None,
        project_id: str,
        deps: ExecutorDeps,
        on_created: Any,
    ):
        planning_error = output.get("planning_error")
        if planning_error is not None:
            if planning_error != INVALID_CSV_HEADER or len(items) != 1:
                raise ImportBulkRouteError(404, "bulk import plan not found")
            return [], [failure_output(output, INVALID_CSV_HEADER)], 0
        good = []
        bad = []
        bad_omitted = 0

        def record_bad(failure: dict[str, Any]) -> None:
            nonlocal bad_omitted
            if len(bad) < MAX_FAILURE_DETAILS:
                bad.append(failure)
            else:
                bad_omitted += 1

        if len(items) > 1 and decision == "separate":
            for item_index, item in enumerate(items):
                name = stem(item["logical_path"])
                inserting = False
                single = {
                    "id": stable_identifier(
                        "bulk-output", "csv", [item["logical_path"]]
                    ),
                    "kind": "csv_group",
                    "logical_paths": [item["logical_path"]],
                }
                try:
                    scan = self._scan(root, item, True)
                    check_csv_row_limit(scan.row_count, max_rows)
                    inserting = True
                    inserted = csv_action_output(
                        project,
                        run_scanned_csv_import(
                            project,
                            project_id=project_id,
                            sheet_name=name,
                            scans=[scan],
                            source_paths=[str((root / item["path"]).absolute())],
                            source_sha256=[item["sha256"]],
                            source_column=None,
                            request_key=csv_request_key(root.name, single["id"], name),
                            deps=deps,
                        ),
                    )
                    good.append(
                        created_output(
                            single["id"],
                            inserted.sheet_id,
                            inserted.sheet_name,
                            single["logical_paths"],
                            scan.row_count,
                        )
                    )
                    on_created(good[-1])
                except RouteError as exc:
                    if exc.status_code >= 500:
                        remaining = [
                            csv_single_output(next_item)
                            for next_item in items[item_index + 1 :]
                        ]
                        raise _BulkExecutionFatal(exc, single, remaining) from exc
                    record_bad(failure_output(single, str(exc.detail)))
                except OSError as exc:
                    if inserting:
                        remaining = [
                            csv_single_output(next_item)
                            for next_item in items[item_index + 1 :]
                        ]
                        raise _BulkExecutionFatal(exc, single, remaining) from exc
                    record_bad(failure_output(single, str(exc)))
                except ValueError as exc:
                    record_bad(failure_output(single, str(getattr(exc, "detail", exc))))
                except Exception as exc:
                    remaining = [
                        csv_single_output(next_item)
                        for next_item in items[item_index + 1 :]
                    ]
                    raise _BulkExecutionFatal(exc, single, remaining) from exc
                finally:
                    if "scan" in locals():
                        scan.source.close()
                        del scan
            return good, bad, bad_omitted
        inserting = False
        scans = []
        try:
            for item in items:
                scans.append(self._scan(root, item))
            check_csv_row_limit(sum(scan.row_count for scan in scans), max_rows)
            source = None
            if len(scans) > 1:
                source = "source_file"
                suffix = 2
                names = set(scans[0].fieldnames)
                while source in names:
                    source = f"source_file_{suffix}"
                    suffix += 1
            name = output["sheet_name"]
            columns = widen_csv_scans(scans)
            if len(scans) > 1:
                # Preserve the existing multi-file contract: an all-integer
                # group remains text, while a decimal in any source widens the
                # shared column to number.
                columns = [
                    {**column, "type": "text"}
                    if column["type"] == "integer"
                    else column
                    for column in columns
                ]
            inserting = True
            inserted = csv_action_output(
                project,
                run_scanned_csv_import(
                    project,
                    project_id=project_id,
                    sheet_name=name,
                    scans=scans,
                    source_paths=[
                        str((root / item["path"]).absolute()) for item in items
                    ],
                    source_sha256=[item["sha256"] for item in items],
                    source_column=source,
                    columns_override=columns,
                    request_key=csv_request_key(root.name, output["id"], name),
                    deps=deps,
                ),
            )
            good.append(
                created_output(
                    output["id"],
                    inserted.sheet_id,
                    inserted.sheet_name,
                    [x["logical_path"] for x in items],
                    sum(x.row_count for x in scans),
                )
            )
            on_created(good[-1])
        except RouteError as exc:
            if exc.status_code >= 500:
                raise
            record_bad(failure_output(output, str(exc.detail)))
        except OSError as exc:
            if inserting:
                raise
            record_bad(failure_output(output, str(exc)))
        except ValueError as exc:
            record_bad(failure_output(output, str(getattr(exc, "detail", exc))))
        finally:
            for scan in scans:
                scan.source.close()
        return good, bad, bad_omitted

    def _xlsx(self, project, pid, o, items, root, *, deps: ExecutorDeps):
        item = items[0]
        with import_bulk_sources.open_verified_source(root, item) as source:
            response = self.xlsx.upload_xlsx(
                pid,
                upload=AdmittedUpload(
                    filename=item["filename"],
                    mime=item["mime"],
                    source=source,
                    sha256=item["sha256"],
                    size=item["size"],
                ),
                sheet_name=o["sheet_name"],
                deps=deps,
            )
        if response.status_code != 200:
            raise ImportBulkRouteError(
                response.status_code, error_detail(response.payload)
            )
        sheet_id, actual_name = actual_sheet(project, response.payload)
        return created_output(
            o["id"],
            sheet_id,
            actual_name,
            [item["logical_path"]],
            int(response.payload.get("rows", 0)),
        )

    def _files(self, project, pid, o, items, root, *, deps: ExecutorDeps):
        name = database_available_name(project, o["sheet_name"])
        with ExitStack() as stack:
            uploaded = []
            for item in items:
                source = stack.enter_context(
                    import_bulk_sources.open_verified_source(root, item)
                )
                uploaded.append(
                    AdmittedUpload(
                        filename=item["logical_path"],
                        mime=item["mime"],
                        source=source,
                        sha256=item["sha256"],
                        size=item["size"],
                    )
                )
            response = self.files.upload_files(
                pid,
                files=uploaded,
                sheet_name=name,
                deps=deps,
            )
        if response.status_code != 200:
            raise ImportBulkRouteError(
                response.status_code, error_detail(response.payload)
            )
        sheet_id, actual_name = actual_sheet(project, response.payload)
        return created_output(
            o["id"],
            sheet_id,
            actual_name,
            [x["logical_path"] for x in items],
            len(items),
        )

    def _email(self, project, pid, output, items, root, *, deps: ExecutorDeps):
        """Run email import against request-pinned, verified staged inodes."""

        with ExitStack() as stack:
            resolved: dict[str, EmailInput] = {}
            action_sources = []
            for index, item in enumerate(items):
                source = stack.enter_context(
                    import_bulk_sources.open_verified_source(root, item)
                )
                logical_path = str(item["logical_path"])
                email_format = item.get("email_format")
                if email_format not in {"eml", "mbox"}:
                    email_format = import_bulk_sources.source_suffix(
                        logical_path
                    ).removeprefix(".")
                if email_format not in {"eml", "mbox"}:
                    raise ImportBulkRouteError(404, "bulk import plan not found")
                source_ref = stable_identifier(
                    "bulk-email-source",
                    str(output["id"]),
                    [root.name, str(index), logical_path, str(item["sha256"])],
                )
                resolved[source_ref] = EmailInput(
                    logical_path=logical_path,
                    format=email_format,
                    stream=source,
                )
                action_sources.append(
                    {
                        "source_ref": source_ref,
                        "logical_path": logical_path,
                        "format": email_format,
                    }
                )

            action = {
                "action_id": "import.email",
                "scope": {"kind": "project"},
                "sheet_name": database_available_name(
                    project, str(output["sheet_name"])
                ),
                "params": {"sources": action_sources},
                "output_names": {},
                "idempotency_key": f"bulk:{root.name}:{output['id']}",
            }
            action_result = run_action_spec(
                project,
                action,
                project_id=pid,
                deps=replace(deps, email_sources=resolved),
            )

        if action_result.status != "completed":
            reason = next(
                (error.message for error in action_result.errors if error.message),
                "Email import failed.",
            )
            raise ImportBulkRouteError(400, reason)
        sheet = next(
            (
                value
                for value in action_result.outputs
                if value.kind == "sheet"
                and value.ref.get("kind") == "materialized_sheet"
            ),
            None,
        )
        rows = next(
            (
                value
                for value in action_result.outputs
                if value.kind == "rows" and value.ref.get("kind") == "source_rows"
            ),
            None,
        )
        if sheet is None or rows is None or sheet.sheet_id is None:
            raise ImportBulkRouteError(500, "email import returned invalid outputs")
        row_count = rows.ref.get("row_count")
        if (
            not isinstance(row_count, int)
            or isinstance(row_count, bool)
            or row_count < 0
        ):
            raise ImportBulkRouteError(500, "email import returned invalid row count")
        sheet_id, sheet_name = actual_sheet(project, {"sheet_id": sheet.sheet_id})
        return (
            created_output(
                output["id"],
                sheet_id,
                sheet_name,
                [item["logical_path"] for item in items],
                row_count,
            ),
            bounded_warnings(action_result.warnings),
        )


def check_csv_row_limit(row_count: int, max_rows: int | None) -> None:
    if max_rows is not None and row_count > max_rows:
        raise ImportBulkRouteError(
            400,
            f"import.csv row count exceeds the deployment limit of {max_rows}",
        )


def csv_request_key(plan_id: str, output_id: str, base_name: str) -> str:
    name_hash = hashlib.sha256(base_name.encode("utf-8")).hexdigest()
    return f"bulk:{plan_id}:{output_id}:{name_hash}"


def database_available_name(project: Any, base: str) -> str:
    used = {
        str(row["name"])
        for row in project.db.execute("SELECT name FROM sheets").fetchall()
    }
    if base not in used:
        return base
    n = 2
    while f"{base}-{n}" in used:
        n += 1
    return f"{base}-{n}"


def actual_sheet(project: Any, payload: dict[str, Any]) -> tuple[int, str]:
    sheet_id = payload.get("sheet_id")
    if not isinstance(sheet_id, int) or isinstance(sheet_id, bool):
        raise ImportBulkRouteError(500, "import did not return a sheet output")
    row = project.db.execute(
        "SELECT id, name FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
    ).fetchone()
    if row is None:
        raise ImportBulkRouteError(500, "import did not materialize its sheet output")
    return int(row["id"]), str(row["name"])


def csv_action_output(project: Any, result: Any) -> _CsvActionOutput:
    if result.status != "completed":
        message = next(
            (error.message for error in result.errors if error.message),
            "CSV import failed.",
        )
        raise ImportBulkRouteError(v1_action_result_http_status(result), message)
    sheet = materialized_sheet_output(result)
    if sheet is None or sheet.sheet_id is None:
        raise ImportBulkRouteError(500, "import.csv did not return a sheet output")
    sheet_id, sheet_name = actual_sheet(project, {"sheet_id": sheet.sheet_id})
    return _CsvActionOutput(sheet_id, sheet_name)


def created_output(oid, sid, name, paths, rows):
    return {
        "id": oid,
        "kind": "sheet",
        "sheet_id": sid,
        "sheet_name": name,
        "logical_paths": paths,
        "rows": rows,
    }


def csv_single_output(item: dict[str, Any]) -> dict[str, Any]:
    logical_path = str(item["logical_path"])
    return {
        "id": stable_identifier("bulk-output", "csv", [logical_path]),
        "kind": "csv_group",
        "logical_paths": [logical_path],
    }


def fatal_error_detail(exc: Exception) -> str:
    if isinstance(exc, RouteError):
        return str(exc.detail)
    return UNEXPECTED_FAILURE


def log_unexpected_fatal(exc: Exception) -> None:
    if isinstance(exc, RouteError):
        return
    LOG.error(
        "bulk_execution_stopped_after_unexpected_error",
        exc_info=(type(exc), exc, exc.__traceback__),
        extra={"event": "bulk_execution_stopped_after_unexpected_error"},
    )


def failure_output(o, error):
    logical_paths = [str(x) for x in o.get("logical_paths", [])]
    failure = {
        "id": str(o.get("id", "unknown")),
        "kind": str(o.get("kind", "files_group")),
        "logical_paths": logical_paths[:MAX_FAILURE_DETAILS],
        "error": str(error)[:500],
    }
    if len(logical_paths) > MAX_FAILURE_DETAILS:
        failure["logical_paths_omitted"] = len(logical_paths) - MAX_FAILURE_DETAILS
    return failure


def bounded_warnings(warnings: list[str]) -> list[str]:
    values = [str(warning) for warning in warnings]
    if len(values) <= 20:
        return values
    return [
        *values[:19],
        f"{len(values)} import warnings total; "
        f"{len(values) - 19} additional warnings omitted",
    ]


def error_detail(payload):
    detail = payload.get("detail") or payload.get("errors") or "Import failed."
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        for error in detail:
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                return error["message"]
    return json.dumps(detail, default=str)
