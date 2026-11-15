"""One admission path for reviewed tables and typed record imports."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from frisket.actions.system import typed_action_for_request
from frisket.contracts.action import ActionResult
from frisket.engine.executor.action_jobs import (
    ActionJobEnvelope,
    reserve_typed_action_job,
)
from frisket.engine.store.import_intake import (
    import_intake_dir,
    import_worker_lock,
    read_import_header,
)
from frisket.engine.store.import_sessions import (
    ImportSessionConflict,
    ImportSessionStore,
)
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.streaming_import import StreamingSheetWriter
from frisket.engine.store.table_import_intake import (
    FrozenTableImport,
    frozen_table_import,
    mark_table_committed,
)


def admit_table_import(
    project: Any,
    project_id: str,
    frozen: FrozenTableImport,
    *,
    max_rows: int | None = None,
    admitted_bound: Any | None = None,
) -> ActionJobEnvelope:
    from frisket.engine.executor.map_rows_action import typed_request_hash

    directory = import_intake_dir(project.path, frozen.import_ref)
    if directory.resolve() != frozen.intake_dir.resolve():
        raise ValueError("frozen table intake belongs to another project")
    if max_rows is not None and (
        isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 0
    ):
        raise ValueError("max_rows must be a non-negative integer or None")
    admitted_header = read_import_header(directory)
    if (
        admitted_header.intake_kind != "table"
        or admitted_header.project_id != project_id
        or admitted_header.storage_identity != project.storage_identity
    ):
        raise ValueError("table import does not belong to this project")
    limits = [
        value for value in (admitted_header.max_rows, max_rows) if value is not None
    ]
    effective_max_rows = min(limits) if limits else None
    if (
        effective_max_rows is not None
        and frozen.exact_rows is not None
        and frozen.exact_rows > effective_max_rows
    ):
        raise ValueError("table import exceeds the configured row limit")

    columns = [
        {
            "name": column.target_name,
            "source_name": column.source_name,
            "type": column.output_type,
            **({"format": column.format} if column.format is not None else {}),
        }
        for column in frozen.plan.columns
    ]
    if frozen.plan.extra_values_target_name is not None:
        columns.append({"name": frozen.plan.extra_values_target_name, "type": "json"})
    source_ref = {
        "kind": "file",
        "label": frozen.source.filename,
        "fingerprint": f"sha256:{frozen.source.sha256.removeprefix('sha256:')}",
        "importer": "table",
    }
    history_source_ref = {
        **source_ref,
        "reviewed_plan": {
            "settings": asdict(frozen.plan.settings),
            "columns": [column.to_json() for column in frozen.plan.columns],
            "extra_values_target_name": frozen.plan.extra_values_target_name,
        },
    }
    if frozen.plan.update is not None:
        history_source_ref["update_relationship"] = (
            frozen.plan.update.relationship_id or f"import:{frozen.import_ref}"
        )
    if admitted_bound is not None:
        history_source_ref["submitted_request_hash"] = frozen.plan.request_hash
        history_source_ref["submitted_action_kind"] = admitted_bound.request.action_id
    destination_sheet_id = frozen.plan.destination_sheet_id
    action: dict[str, Any] = {
        "action_id": (
            "import.rows" if destination_sheet_id is None else "import.append_rows"
        ),
        "scope": (
            {"kind": "project"}
            if destination_sheet_id is None
            else {"kind": "sheet_rows", "sheet_id": destination_sheet_id}
        ),
        "params": {"columns": columns, "rows": [], "source": source_ref},
        "idempotency_key": frozen.import_ref,
    }
    if frozen.plan.update is not None:
        action["action_id"] = "import.update_rows"
        action["params"].update(
            key_columns=[
                column.target_name
                for column in frozen.plan.columns
                if column.source_index in frozen.plan.update.key_source_indices
            ],
            update_policy=frozen.plan.update.policy,
            relationship_id=frozen.plan.update.relationship_id,
            keep_existing_on_blank=frozen.plan.update.keep_existing_on_blank,
            staged_input={
                "import_ref": frozen.import_ref,
                "plan_hash": frozen.plan_hash,
                "source_hash": f"sha256:{frozen.source.sha256.removeprefix('sha256:')}",
            },
        )
    if destination_sheet_id is None:
        action["sheet_name"] = frozen.plan.sheet_name
    bound = admitted_bound or typed_action_for_request(action)
    from frisket.engine.jobs.import_table import _validate_reserved_action

    _validate_reserved_action(bound, frozen)

    with import_worker_lock(directory, timeout=10):
        header = read_import_header(directory)
        if (
            header.intake_kind != "table"
            or header.project_id != project_id
            or header.storage_identity != project.storage_identity
        ):
            raise ValueError("table import does not belong to this project")
        current = frozen_table_import(directory)
        if current.plan_hash != frozen.plan_hash:
            raise ValueError("frozen table plan changed")
        if (
            effective_max_rows is not None
            and current.exact_rows is not None
            and current.exact_rows > effective_max_rows
        ):
            raise ValueError("table import exceeds the configured row limit")
        if header.envelope:
            reserved = ActionJobEnvelope.from_json(header.envelope)
            if (
                reserved.project_id != project_id
                or reserved.action_kind != bound.request.action_id
                or reserved.idempotency_key != bound.request.idempotency_key
                or reserved.params_hash != typed_request_hash(bound)
            ):
                raise ValueError("table import reservation identity changed")
        else:
            reserved = reserve_typed_action_job(project, project_id, bound)
        if isinstance(reserved, ActionResult):
            # Generic RowsUpdater handlers can already own their ordinary
            # queued-action receipt when they begin staging in the worker.
            stored = ReceiptStore(project).find_by_idempotency_key(
                bound.request.idempotency_key
            )

            if (
                stored is None
                or stored.status not in {"queued", "running"}
                or stored.params_hash != typed_request_hash(bound)
            ):
                raise ValueError(
                    "table import reservation is already terminal or differs"
                )
            previous = stored.parsed()
            reserved = ActionJobEnvelope(
                action_kind=bound.request.action_id,
                action_id=previous.action_id,
                receipt_id=previous.receipt_id,
                params_hash=stored.params_hash,
                idempotency_key=bound.request.idempotency_key,
                project_id=project_id,
                action=bound.request.model_dump(mode="json", exclude_none=True),
                resolve_phase="worker",
            )
        envelope: ActionJobEnvelope = reserved
        receipts = ReceiptStore(project)
        stored = receipts.find_by_id(envelope.receipt_id)
        if stored is None:
            raise RuntimeError("table import reservation disappeared")
        if (
            stored.action_id != envelope.action_id
            or stored.action_kind != envelope.action_kind
            or stored.params_hash != envelope.params_hash
            or stored.idempotency_key != envelope.idempotency_key
        ):
            raise ValueError("table import reservation identity changed")
        session = ImportSessionStore(project).get(frozen.import_ref)
        if session is not None and session.receipt_id != envelope.receipt_id:
            raise ValueError("table import session identity changed")
        if session is not None and session.state in {"completed", "kept"}:
            return envelope
        if header.cancel_requested or (
            session is not None and session.state in {"cancelled", "removed"}
        ):
            raise ImportSessionConflict("This import was cancelled.")
        mark_table_committed(
            directory,
            expected_plan_hash=frozen.plan_hash,
            envelope=envelope.to_json(),
            destination_sheet_id=destination_sheet_id,
            max_rows=effective_max_rows,
        )
        if stored.status == "queued":
            receipts.update_body_status(
                stored.parsed().model_copy(update={"status": "running"}),
                require_status="queued",
            )
        elif stored.status != "running":
            raise ValueError("table import reservation is already terminal")

        session = ImportSessionStore(project).get(frozen.import_ref)
        if session is None:
            common = {
                "project": project,
                "session_id": frozen.import_ref,
                "writer_authority": f"bootstrap:{frozen.plan_hash}",
                "columns": columns,
                "project_id": project_id,
                "action_kind": envelope.action_kind,
                "idempotency_key": envelope.idempotency_key,
                "params_hash": envelope.params_hash,
                "action_id": envelope.action_id,
                "receipt_id": envelope.receipt_id,
                "source_ref": history_source_ref,
            }
            if destination_sheet_id is None:
                StreamingSheetWriter.start_session(
                    **common,
                    sheet_name=frozen.plan.sheet_name,
                    allocate_sheet_name=True,
                ).abort()
            else:
                StreamingSheetWriter.start_append_session(
                    **common, sheet_id=destination_sheet_id
                ).abort()
        elif session.receipt_id != envelope.receipt_id:
            raise ValueError("table import session identity changed")
        if frozen.plan.update is not None:
            session = ImportSessionStore(project).get(frozen.import_ref)
            compact_request = bound.request.model_dump(mode="json", exclude_none=True)
            compact_request["params"] = {
                key: value
                for key, value in compact_request["params"].items()
                if key != "rows"
            }
            compact_request["params"]["row_count"] = frozen.exact_rows
            with project.db:
                project.db.execute(
                    "UPDATE ops SET spec=? WHERE id=?",
                    (json.dumps(compact_request), session.op_id),
                )
        header = read_import_header(directory)

    return envelope
