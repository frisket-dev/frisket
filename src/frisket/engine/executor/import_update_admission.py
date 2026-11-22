"""Adapt typed row/file updates to the existing immutable table intake."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable
from typing import Any

from frisket.actions.system import typed_action_for_request
from frisket.contracts.action import ActionError, ActionResult
from frisket.engine.store.import_intake import (
    ImportIntakeHeader,
    import_admit_lock,
    import_intake_dir,
    read_import_header,
)
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.table_import_intake import (
    FrozenTableImport,
    TableImportColumn,
    TableImportPlan,
    TableImportScan,
    TableImportSource,
    TableImportUpdate,
    freeze_table_plan,
    frozen_table_import,
    initialize_table_intake,
    read_table_settings,
    write_table_scan,
)
from frisket.engine.store.value_codec import encode_stored_value
from frisket.engine.store.temporal_ingress import preflight_typed_rows_for_persistence
from frisket.table_import import TableReadSettings

from .action_jobs import ActionJobEnvelope, _result_from_existing_action_job_receipt
from .action_support import ActionCtx
from .import_sources import open_local_file_reader
from .map_rows_action import typed_request_hash
from .table_import_admission import admit_table_import

UPDATE_ACTIONS = frozenset(
    {"import.update_rows", "import.update_csv", "import.update_xlsx"}
)


def _records(params: Any, kind: str, deps: Any):
    if kind == "import.update_rows":
        yield from params.rows
        return
    if kind == "import.update_csv":
        from frisket.actions.imports import import_csv as producer
    else:
        from frisket.actions.import_xlsx import import_xlsx as producer
    with open_local_file_reader(deps.local_file_sources) as reader:
        table_rows = producer(params, reader).rows
        if not isinstance(table_rows, Iterable):
            raise TypeError(f"{kind} must produce a synchronous row iterable")
        yield from (row.output.root for row in table_rows)


def _existing_result(
    project: Any, project_id: str, bound: Any, request_hash: str
) -> ActionResult | None:
    from frisket.contracts.action import ActionError

    existing = ReceiptStore(project).find_by_idempotency_key(
        bound.request.idempotency_key
    )
    if existing is None:
        return None
    receipt = existing.parsed()
    previous = next(
        (
            item.ref.get("submitted_request_hash")
            for item in receipt.inputs
            if isinstance(item.ref, dict) and item.ref.get("submitted_request_hash")
        ),
        None,
    )
    if previous is not None and previous != request_hash:
        return ActionCtx(project_id, bound.request.action_id).failed(
            ActionError(
                code="idempotency_conflict",
                message="This request key already identifies a different import update.",
            )
        )
    if previous == request_hash and receipt.status in {
        "completed",
        "partial",
        "failed",
        "cancelled",
    }:
        return _result_from_existing_action_job_receipt(receipt, job_id=None)
    return None


def admit_typed_update(
    project: Any,
    project_id: str,
    bound: Any,
    deps: Any,
    *,
    update_params: Any = None,
    records_kind: str | None = None,
) -> tuple[Any, ActionJobEnvelope] | ActionResult:
    """No database writer lock is held while normalizing or staging input."""
    params = update_params if update_params is not None else bound.params
    supplied = getattr(params, "staged_input", None)
    request_hash = (
        supplied.request_hash if supplied is not None else None
    ) or typed_request_hash(bound)
    if supplied is not None:
        prior = ActionJobEnvelope.from_json(
            read_import_header(
                import_intake_dir(project.path, supplied.import_ref)
            ).envelope
        )
        if typed_request_hash(bound) != prior.params_hash:
            raise ValueError("staged update request differs from its admitted request")
    replay = _existing_result(project, project_id, bound, request_hash)
    if replay is not None:
        return replay
    if supplied is not None:
        directory = import_intake_dir(project.path, supplied.import_ref)
        frozen = frozen_table_import(directory)
        if frozen.plan_hash != supplied.plan_hash or frozen.source.sha256.removeprefix(
            "sha256:"
        ) != supplied.source_hash.removeprefix("sha256:"):
            raise ValueError("staged input identity changed")
        envelope = admit_table_import(project, project_id, frozen, admitted_bound=bound)
        return frozen, envelope
    identity = (
        f"{bound.request.action_id}:{bound.request.idempotency_key or uuid.uuid4().hex}"
    )
    ref = "import-" + hashlib.sha256(identity.encode()).hexdigest()[:32]
    directory = import_intake_dir(project.path, ref)
    directory.mkdir(parents=True, exist_ok=True)
    sheet_id = int(bound.request.scope.sheet_id)
    sheet = project.db.execute(
        "SELECT name,parent_sheet_id FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
    ).fetchone()
    if sheet is None:
        raise ValueError("update target sheet is unavailable")
    destination = {
        str(row["name"]): row
        for row in project.db.execute(
            "SELECT id,name,type FROM columns WHERE sheet_id=? AND active=1 AND hidden=0",
            (sheet_id,),
        )
    }
    for column in params.columns:
        if (
            column.name not in destination
            or destination[column.name]["type"] != column.type
        ):
            raise ValueError("update column mapping is incompatible")
    with import_admit_lock(directory):
        if (directory / "manifest.json").exists() and read_import_header(
            directory
        ).envelope:
            frozen = frozen_table_import(directory)
            prior = ActionJobEnvelope.from_json(read_import_header(directory).envelope)
            if frozen.plan.request_hash != request_hash:
                raise ValueError("import request key identifies another staged source")
            from .action_jobs import bind_typed_action_job

            admitted_bound = bind_typed_action_job(prior, project=project)
            if isinstance(admitted_bound, ActionResult):
                raise ValueError("staged update action is no longer available")
        else:
            settings = TableReadSettings(
                kind="delimited", delimiter=",", encoding="utf-8"
            )
            digest = hashlib.sha256()
            count = size = 0
            types = {column.name: column.type for column in params.columns}
            limits = getattr(deps, "import_workload_limits", None)
            max_rows = None if limits is None else limits.max_rows
            temporary = directory / "source.pending"
            with temporary.open("wb") as sink:
                for record in _records(
                    params, records_kind or bound.request.action_id, deps
                ):
                    count += 1
                    if max_rows is not None and count > max_rows:
                        from frisket.actions.types import TableError

                        raise TableError(
                            "import_workload_limit_exceeded",
                            "import exceeds the deployment row limit",
                        )
                    normalized = {
                        name: (
                            None
                            if isinstance(value, str) and not value.strip()
                            else value
                        )
                        for name, value in record.items()
                    }
                    [normalized] = preflight_typed_rows_for_persistence(
                        project, column_types_by_name=types, rows=[normalized]
                    )
                    encoded = (
                        json.dumps(
                            {
                                name: encode_stored_value(value)
                                for name, value in normalized.items()
                            },
                            separators=(",", ":"),
                            allow_nan=False,
                        ).encode()
                        + b"\n"
                    )
                    sink.write(encoded)
                    digest.update(encoded)
                    size += len(encoded)
            temporary.replace(directory / "source")
            initialize_table_intake(
                directory,
                ImportIntakeHeader(
                    project_id, project.storage_identity, {}, max_rows=max_rows
                ),
                source=TableImportSource(
                    filename="records.jsonl",
                    size=size,
                    sha256=digest.hexdigest(),
                    kind="delimited",
                ),
                settings=settings,
            )
            settings_hash = read_table_settings(directory)[1]
            write_table_scan(
                directory,
                TableImportScan(0, settings_hash, count, count, count, True),
                expected_settings_revision=0,
            )
            frozen = freeze_table_plan(
                directory,
                TableImportPlan(
                    settings_revision=0,
                    settings=settings,
                    sheet_name=sheet["name"],
                    destination_sheet_id=sheet_id,
                    columns=tuple(
                        TableImportColumn(
                            index,
                            column.source_name or column.name,
                            column.name,
                            column.type,
                            target_column_id=destination[column.name]["id"],
                            format=column.format,
                        )
                        for index, column in enumerate(params.columns)
                    ),
                    update=TableImportUpdate(
                        tuple(
                            index
                            for index, column in enumerate(params.columns)
                            if column.name in params.key_columns
                        ),
                        policy=params.update_policy,
                        relationship_id=params.relationship_id,
                        keep_existing_on_blank=params.keep_existing_on_blank,
                    ),
                    record_encoding="stored_values",
                    action_kind=bound.request.action_id,
                    request_hash=request_hash,
                ),
            )
            if update_params is not None:
                admitted_bound = bound
            else:
                request = bound.request.model_dump(mode="json", exclude_none=True)
                request["params"] = params.model_dump(mode="json", exclude_none=True)
                request["params"]["staged_input"] = {
                    "import_ref": ref,
                    "plan_hash": frozen.plan_hash,
                    "source_hash": "sha256:" + digest.hexdigest(),
                    "request_hash": request_hash,
                }
                if "rows" in request["params"]:
                    request["params"]["rows"] = []
                admitted_bound = typed_action_for_request(request)
        envelope = admit_table_import(
            project, project_id, frozen, admitted_bound=admitted_bound
        )
    return frozen, envelope


def _finalize_update(
    project: Any,
    frozen: FrozenTableImport,
    envelope: ActionJobEnvelope,
    *,
    writer_authority: str,
    failure: ActionError | None,
    enumeration_complete: bool,
) -> ActionResult:
    """Close a direct update while its caller still owns the intake worker lock."""
    from frisket.engine.store.import_sessions import (
        ImportSessionConflict,
        ImportSessionStore,
    )
    from frisket.engine.store.streaming_import import StreamingSheetWriter
    from frisket.engine.store.table_import_intake import (
        compact_table_intake,
        finish_table_intake,
    )

    session = ImportSessionStore(project).require(frozen.import_ref)
    receipt = ReceiptStore(project).require_parsed(envelope.receipt_id)
    if receipt.status == "running" and session.state in {"active", "paused"}:
        if session.writer_authority != writer_authority:
            raise ImportSessionConflict("row update no longer owns publication")
        publication = StreamingSheetWriter._from_session(
            project, session
        ).finalize_session(
            expected_cursor=session.cursor,
            failure=failure,
            enumeration_complete=enumeration_complete,
        )
        receipt = publication.receipt
        finish_table_intake(frozen.intake_dir, exact_rows=session.cursor)
        compact_table_intake(frozen.intake_dir)
    return _result_from_existing_action_job_receipt(receipt, job_id=None)


def run_typed_update(
    project: Any,
    project_id: str,
    bound: Any,
    deps: Any,
    *,
    update_params: Any = None,
    records_kind: str | None = None,
) -> ActionResult:
    from contextlib import ExitStack
    from frisket.contracts.action import ActionError
    from frisket.engine.jobs.import_table import run_table_import
    from frisket.engine.store.artifact_timeline import TimelineError
    from frisket.actions.types import TableError

    def failure(exc: Exception) -> ActionError:
        return ActionError(
            code=exc.code
            if isinstance(exc, (TimelineError, TableError))
            else "import_update_failed",
            message=str(exc),
        )

    try:
        admitted = admit_typed_update(
            project,
            project_id,
            bound,
            deps,
            update_params=update_params,
            records_kind=records_kind,
        )
    except (ValueError, OSError, TypeError) as exc:
        return ActionCtx(project_id, bound.request.action_id).failed(failure(exc))
    if isinstance(admitted, ActionResult):
        return admitted
    frozen, envelope = admitted
    writer_authority = "direct:" + uuid.uuid4().hex
    with ExitStack() as publication_owner:
        error = None
        enumeration_complete = False
        try:
            outcome = run_table_import(
                project,
                project_id=project_id,
                import_ref=frozen.import_ref,
                plan_hash=frozen.plan_hash,
                writer_authority=writer_authority,
                live=lambda: not (deps.cancelled and deps.cancelled()),
                publication_owner=publication_owner,
            )
            enumeration_complete = outcome["status"] == "published"
            if not enumeration_complete:
                error = ActionError(
                    code="import_update_failed", message="Row update was interrupted"
                )
        except Exception as exc:
            error = failure(exc)
        return _finalize_update(
            project,
            frozen,
            envelope,
            writer_authority=writer_authority,
            failure=error,
            enumeration_complete=enumeration_complete,
        )


def run_rows_updater(
    project: Any, project_id: str, bound: Any, deps: Any
) -> ActionResult:
    """Run a sole RowsUpdater handler without holding an outer writer transaction."""
    from frisket.actions.imports import UpdateRowsParams
    from frisket.actions.types import UpdatedImportedRows
    from frisket.contracts.action import ActionError

    replay = _existing_result(project, project_id, bound, typed_request_hash(bound))
    if replay is not None:
        return replay

    from contextlib import ExitStack
    from frisket.engine.jobs.import_table import run_table_import
    from frisket.engine.store.import_sessions import ImportSessionStore
    from frisket.engine.store.streaming_import import StreamingSheetWriter

    publication_owner = ExitStack()

    class Updater:
        called = False
        result = None
        frozen = None
        envelope = None
        replay = None
        enumeration_complete = False
        writer_authority = "direct:" + uuid.uuid4().hex

        def _run(self, params, kind):
            if self.called:
                raise ValueError("RowsUpdater may be called once per action")
            self.called = True
            admitted = admit_typed_update(
                project,
                project_id,
                bound,
                deps,
                update_params=params,
                records_kind=kind,
            )
            if isinstance(admitted, ActionResult):
                self.replay = admitted
                raise ValueError("row update already has a terminal result")
            self.frozen, self.envelope = admitted
            outcome = run_table_import(
                project,
                project_id=project_id,
                import_ref=self.frozen.import_ref,
                plan_hash=self.frozen.plan_hash,
                writer_authority=self.writer_authority,
                live=lambda: not (deps.cancelled and deps.cancelled()),
                publication_owner=publication_owner,
            )
            if outcome["status"] != "published":
                raise ValueError("row update was interrupted")
            self.enumeration_complete = True
            session = ImportSessionStore(project).require(self.frozen.import_ref)
            writer = StreamingSheetWriter._from_session(project, session)
            receipt = ReceiptStore(project).require_parsed(self.envelope.receipt_id)
            snapshot = writer._completed_update_receipt(
                receipt, row_count=session.committed_rows, status="running"
            )
            ref = snapshot.outputs[0].ref
            op_id = snapshot.op_ids[0] if snapshot.op_ids else None
            row_ids = (
                ()
                if op_id is None
                else tuple(
                    int(row[0])
                    for row in project.db.execute(
                        "SELECT DISTINCT cell.row_id FROM cells cell JOIN base_cell_producers producer "
                        "ON producer.id=cell.producer_id WHERE producer.op_id=? ORDER BY cell.row_id",
                        (op_id,),
                    )
                )
            )
            self.result = UpdatedImportedRows(
                int(bound.request.scope.sheet_id),
                row_ids,
                int(ref["edit_count"]),
                op_id,
                dict(writer._column_ids),
                {},
            )
            return self.result

        def update(self, **kwargs):
            source = kwargs.pop("source", None)
            return self._run(
                UpdateRowsParams(**kwargs, source=source or None), "import.update_rows"
            )

        def update_csv(self, params):
            return self._run(params, "import.update_csv")

        def update_xlsx(self, params):
            return self._run(params, "import.update_xlsx")

    with publication_owner:
        updater = Updater()
        failure = None
        try:
            returned = bound.action.definition.run.handler(bound.params, updater)
            if updater.result is None or returned is not updater.result:
                raise ValueError("return the result of the action's RowsUpdater call")
        except Exception as exc:  # committed pages survive a failed plugin handler
            from frisket.actions.types import TableError
            from frisket.engine.store.artifact_timeline import TimelineError

            failure = ActionError(
                code=exc.code
                if isinstance(exc, (TableError, TimelineError))
                else "import_update_failed",
                message=str(exc),
            )
        if updater.replay is not None:
            return updater.replay
        if updater.frozen is None or updater.envelope is None:
            return ActionCtx(project_id, bound.request.action_id).failed(
                failure
                or ActionError(
                    code="import_update_failed", message="No update was requested"
                )
            )
        return _finalize_update(
            project,
            updater.frozen,
            updater.envelope,
            writer_authority=updater.writer_authority,
            failure=failure,
            enumeration_complete=updater.enumeration_complete,
        )
