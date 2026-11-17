"""Prepare derived work before publication; reuse the model owner for summaries.

Join fanout and grouped model costs use their normal admission gates. Authored
handlers finish before mutations or provider calls; receipts name the real Change.
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, NoReturn

from frisket.actions.core import CreateSheet, _ProjectAction
from frisket.actions.join_types import JoinedTablesReader
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ActionRequest,
    ListTableReader,
    RefreshedSheet,
    RowSource,
    SheetRefresher,
    TableError,
)
from frisket.contracts.action import (
    ActionError,
    ActionIdentity,
    ActionOutput,
    ActionResult,
    Receipt,
    ReceiptEvidence,
    ReceiptIO,
    canonical_column_type,
)
from frisket.engine.executor.action_families.imports import _preflight_table_rows
from frisket.engine.executor.action_receipts import _result_from_receipt
from frisket.engine.executor.action_reservations import _receipt_for_idempotency
from frisket.engine.executor.action_support import _failed_result, _new_id
from frisket.engine.executor.embedding_read import TableReadRefused
from frisket.engine.executor.list_table_grounding import propagate_list_item_evidence
from frisket.engine.executor.map_rows_action import typed_request_hash
from frisket.engine.store import Project
from frisket.engine.store.leases import lease_held
from frisket.engine.store.materialization import (
    MaterializedColumnSpec,
)
from frisket.engine.store.receipts import ReceiptStore

from .sheet_refresh_ownership import (
    acquire_refresh_claim,
    require_refresh_writer,
    release_refresh_claim,
)

logger = logging.getLogger("frisket.executor")


class _RefreshRefused(Exception):
    def __init__(self, error: ActionError):
        self.error = error


@dataclass(frozen=True)
class _RefreshFacts:
    sheet_id: int
    parent_sheet_id: int
    name: str
    op_id: int
    row_count: int
    match_stats: dict[str, Any] | None = None
    reads: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class _PreparedRefresh:
    sheet_id: int
    parent_sheet_id: int
    parent_op_id: int
    name: str
    stage: Any
    columns: Any
    config: dict[str, Any]
    policy: str
    reads: tuple[dict[str, Any], ...]
    captures: tuple[dict[str, Any], ...]
    source_binding: dict[str, Any]
    match_stats: dict[str, Any] | None = None

    @property
    def row_count(self):
        return self.stage.row_count


@dataclass(frozen=True)
class _SheetRefreshPlan:
    kind: Literal["list", "join", "group"]
    request: ActionRequest


def _retired_refresh_parent(project: Project, parent_op: Any) -> bool:
    if parent_op is None:
        return False
    floor = project.db.execute(
        "SELECT value FROM meta WHERE key='domain_history_floor'"
    ).fetchone()
    return parent_op["change_order"] is None or (
        floor is not None and int(parent_op["id"]) <= int(floor[0])
    )


def _sheet_refresh_plan(
    project: Project,
    sheet: Any,
    parent_op: Any,
    *,
    idempotency_key: str,
) -> _SheetRefreshPlan | None:
    """Classify the same refresh families that `_refresh_sheet` can execute."""
    if sheet["parent_sheet_id"] is None or sheet["parent_op_id"] is None:
        return None
    from frisket.engine.store.derived_updates import refresh_config

    if _retired_refresh_parent(project, parent_op) or refresh_config(parent_op) is None:
        return None

    derive_request = _reconstruct_derive_request(parent_op, idempotency_key)
    saved_action = (
        ACTION_REGISTRY.get(derive_request.action_id)
        if derive_request is not None
        else None
    )
    if (
        saved_action is not None
        and isinstance(saved_action.definition.run, CreateSheet)
        and JoinedTablesReader in saved_action.definition.run.capabilities
    ):
        return _SheetRefreshPlan("join", derive_request)

    from frisket.actions.group_summary_types import GroupSummarizer

    if saved_action is not None and getattr(
        saved_action.definition.run, "capabilities", ()
    ) == (GroupSummarizer,):
        return _SheetRefreshPlan("group", derive_request)

    has_multiple_parents = (
        project.db.execute(
            "SELECT 1 FROM materialized_row_sources mrs "
            "JOIN rows r ON mrs.materialized_row_id = r.id "
            "WHERE r.sheet_id=? LIMIT 1",
            (int(sheet["id"]),),
        ).fetchone()
        is not None
    )
    if (
        has_multiple_parents
        or parent_op is None
        or parent_op["kind"] != "derive.table_from_list"
        or derive_request is None
    ):
        return None
    return _SheetRefreshPlan("list", derive_request)


def sheet_supports_refresh(project: Project, sheet_id: int) -> bool:
    """Whether the host can currently rebuild this sheet in place."""
    sheet = project.db.execute(
        "SELECT * FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
    ).fetchone()
    if sheet is None:
        return False
    parent_op = (
        project.db.execute(
            "SELECT * FROM ops WHERE id=?", (int(sheet["parent_op_id"]),)
        ).fetchone()
        if sheet["parent_op_id"] is not None
        else None
    )
    return (
        _sheet_refresh_plan(
            project,
            sheet,
            parent_op,
            idempotency_key="sheet-refresh-support-probe",
        )
        is not None
    )


def _fail(
    *,
    project_id: str,
    code: str,
    message: str,
    field: str | None = None,
    details: dict[str, Any] | None = None,
) -> NoReturn:
    raise _RefreshRefused(
        ActionError(
            code=code,
            message=message,
            field=field,
            details=details or {},
        )
    )


def supports_typed_sheet_refresh_action(terminal: Any) -> bool:
    return isinstance(terminal, _ProjectAction) and terminal.capabilities == (
        SheetRefresher,
    )


class _SheetRefresher:
    def __init__(
        self,
        project: Project,
        project_id: str,
        bound: BoundTypedActionRequest,
        request_hash: str,
        *,
        edition=None,
    ) -> None:
        self.edition = edition
        self.project = project
        self.project_id = project_id
        self.bound = bound
        self.request_hash = request_hash
        self.calls = 0
        self.prepared: _PreparedRefresh | None = None
        self.prepared_group = None

    def refresh(self, sheet_id: int) -> RefreshedSheet:
        self.calls += 1
        if self.calls != 1:
            raise ValueError("a refresh action must refresh exactly one sheet")
        if type(sheet_id) is not int or sheet_id <= 0:
            raise ValueError("refresh sheet_id must be a positive integer")
        sheet = self.project.db.execute(
            "SELECT * FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
        ).fetchone()
        parent = (
            self.project.db.execute(
                "SELECT * FROM ops WHERE id=?", (sheet["parent_op_id"],)
            ).fetchone()
            if sheet is not None and sheet["parent_op_id"] is not None
            else None
        )
        plan = (
            _sheet_refresh_plan(
                self.project,
                sheet,
                parent,
                idempotency_key=self.bound.request.idempotency_key,
            )
            if sheet is not None
            else None
        )
        if plan is not None:
            from frisket.execution.action_admission import action_admission_error

            refusal = action_admission_error(
                self.project,
                ACTION_REGISTRY.get(plan.request.action_id),
                edition=self.edition,
            )
            if refusal is not None:
                raise _RefreshRefused(refusal)
        if plan is not None and plan.kind == "group":
            from frisket.engine.executor.group_summary_refresh import prepare_refresh

            self.prepared_group = prepare_refresh(
                self.project,
                sheet,
                parent,
                plan.request,
                self.bound,
                edition=self.edition,
            )
            return RefreshedSheet(
                sheet_id=sheet_id,
                refreshed_row_count=len(self.prepared_group.resolved["groups"]),
            )
        self.prepared = _refresh_sheet(
            self.project, sheet_id, self.bound, self.request_hash, self.project_id
        )
        return RefreshedSheet(
            sheet_id=self.prepared.sheet_id, refreshed_row_count=self.prepared.row_count
        )


def prepare_sheet_refresh_action(project, project_id, bound, *, edition=None):
    """Resolve author-selected work once, without publication or provider calls."""
    terminal = bound.action.definition.run
    if not supports_typed_sheet_refresh_action(terminal):
        raise TypeError("typed refresh requires a sheet refresher action")
    request_hash = typed_request_hash(bound)
    capability = _SheetRefresher(
        project, project_id, bound, request_hash, edition=edition
    )
    try:
        returned = terminal.handler(bound.params, capability)
        if (
            capability.calls != 1
            or (capability.prepared is None and capability.prepared_group is None)
            or not isinstance(returned, RefreshedSheet)
        ):
            raise ValueError(
                "refresh action must refresh one sheet and return a domain result"
            )
        return capability
    except BaseException:
        if capability.prepared is not None:
            capability.prepared.stage.close()
        raise


def run_typed_sheet_refresh_action(
    project: Project,
    project_id: str,
    bound: BoundTypedActionRequest,
    router=None,
    *,
    deps=None,
    edition=None,
) -> ActionResult:
    from .group_summary_action import GroupSummaryRefused

    terminal = bound.action.definition.run
    if not supports_typed_sheet_refresh_action(terminal):
        raise TypeError("typed refresh requires a sheet refresher action")
    kind = bound.action.action_id
    request_hash = typed_request_hash(bound)

    def replay() -> ActionResult | None:
        existing = _receipt_for_idempotency(project, bound.request.idempotency_key)
        if existing is None:
            return None
        if existing["params_hash"] != request_hash:
            return _fail(
                project_id=project_id,
                code="idempotency_conflict",
                message="idempotency_key was already used with a different request",
                field="idempotency_key",
            )
        from .group_summary_runtime import _recoverable_group_run
        from .action_reservations import _recover_stale_receipt_attempt

        existing = _recover_stale_receipt_attempt(project, existing, request_hash)
        if _recoverable_group_run(project, existing, request_hash) is not None:
            return None
        return _result_from_receipt(
            Receipt.model_validate(json.loads(existing["body"]))
        )

    capability = None
    started = None
    try:
        existing = replay()
        if existing is not None:
            return existing
        capability = prepare_sheet_refresh_action(
            project, project_id, bound, edition=edition
        )
        if capability.prepared_group is not None:
            # Author validation finishes before the model owner may reserve or
            # call a provider. Its ordinary admission owns consent and billing.
            from frisket.engine.executor.group_summary_action import (
                run_typed_group_summary_action,
            )

            return run_typed_group_summary_action(
                project,
                project_id,
                bound,
                router=router,
                deps=deps,
                prepared=capability.prepared_group,
            )
        project.db.execute("BEGIN IMMEDIATE")
        existing = replay()
        if existing is not None:
            project.db.rollback()
            return existing
        started = _begin_staged_refresh(
            project, project_id, bound, capability.prepared, request_hash
        )
        project.db.commit()
        facts = _publish_staged_refresh(
            project, capability.prepared, started, deps=deps
        )
        action_id, receipt_id = started.action_id, started.receipt_id
        project.db.execute("BEGIN IMMEDIATE")
        require_refresh_writer(
            project,
            sheet_id=capability.prepared.sheet_id,
            parent_op_id=capability.prepared.parent_op_id,
            op_id=started.op_ids[0],
            token=started.receipt_id,
        )
        sheet_ref = {
            "kind": "materialized_sheet",
            "sheet_id": facts.sheet_id,
            "parent_sheet_id": facts.parent_sheet_id,
            "op_id": facts.op_id,
        }
        refresh_ref = {
            "kind": "sheet_refresh",
            "sheet_id": facts.sheet_id,
            "op_id": facts.op_id,
            "refreshed_row_count": facts.row_count,
        }
        evidence = [ReceiptEvidence(ref=refresh_ref)]
        if facts.match_stats is not None:
            refresh_ref["parent_op_kind"] = "derive.join"
            evidence = [
                ReceiptEvidence(ref=refresh_ref),
                ReceiptEvidence(ref=facts.match_stats, retention="pinned"),
            ]
        evidence.extend(
            ReceiptEvidence(ref=fact, retention="pinned") for fact in facts.reads
        )
        receipt = Receipt(
            receipt_id=receipt_id,
            project_id=project_id,
            action_id=action_id,
            action_kind=kind,
            op_ids=[facts.op_id],
            idempotency_key=bound.request.idempotency_key,
            params_hash=request_hash,
            status="completed",
            inputs=[
                ReceiptIO(
                    name="target_sheet",
                    ref={
                        "kind": "sheet",
                        "sheet_id": facts.sheet_id,
                    },
                )
            ],
            outputs=[ReceiptIO(name=facts.name, ref=sheet_ref)],
            evidence=evidence,
        )
        result = ActionResult(
            action=ActionIdentity(kind=kind, action_id=action_id),
            status="completed",
            project_id=project_id,
            op_ids=[facts.op_id],
            receipt_id=receipt_id,
            outputs=[
                ActionOutput(
                    kind="sheet",
                    name=facts.name,
                    sheet_id=facts.sheet_id,
                    ref=sheet_ref,
                )
            ],
        )
        from frisket.engine.store.changes import ChangeStore

        ChangeStore(project).close(facts.op_id)
        release_refresh_claim(project, facts.sheet_id, receipt_id)
        if not ReceiptStore(project).update_body_status(
            receipt, require_status="running", commit=False
        ):
            raise TableError(
                "stale_refresh", "The refresh receipt is no longer writable"
            )
        project.db.commit()
        return result
    except (_RefreshRefused, TableReadRefused, GroupSummaryRefused) as exc:
        project.db.rollback()
        error = exc.error.model_copy(update={"action_kind": kind})
    except TableError as exc:
        project.db.rollback()
        error = ActionError(
            code=exc.code,
            message=str(exc),
            action_kind=kind,
            details=exc.details or {},
        )
    except Exception:
        project.db.rollback()
        # Author exceptions may include private input. Log only static facts.
        logger.debug("action_failed", extra={"action_kind": kind})
        error = ActionError(
            code="project_write_failed",
            message="project write failed",
            action_kind=kind,
        )
    except BaseException:
        project.db.rollback()
        if started is not None:
            _finish_refresh_failure(
                project,
                started,
                ActionError(
                    code="cancelled", message="Refresh cancelled", action_kind=kind
                ),
            )
        raise
    finally:
        if capability is not None and capability.prepared is not None:
            capability.prepared.stage.close()
    if started is not None:
        return _finish_refresh_failure(project, started, error)
    if error.needs_confirmation:
        return ActionResult(
            action=ActionIdentity(kind=kind, action_id=_new_id("act")),
            status="needs_confirmation",
            project_id=project_id,
            errors=[error],
        )
    return _failed_result(project_id=project_id, action_kind=kind, error=error)


def _refresh_sheet(
    project: Project,
    sheet_id: int,
    bound: BoundTypedActionRequest,
    params_hash: str,
    project_id: str,
) -> _PreparedRefresh:
    now = datetime.now(timezone.utc)
    cur = project.db.cursor()
    sheet = cur.execute(
        "SELECT * FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
    ).fetchone()
    if sheet is None:
        return _fail(
            project_id=project_id,
            code="sheet_not_found",
            message="sheet.refresh target sheet does not exist",
            field="sheet_id",
            details={"sheet_id": sheet_id},
        )
    if sheet["parent_sheet_id"] is None or sheet["parent_op_id"] is None:
        return _fail(
            project_id=project_id,
            code="refresh_root_sheet_unsupported",
            message="root sheets have no parent op to refresh from",
            field="sheet_id",
            details={"sheet_id": sheet_id},
        )
    # Live refresh lease (embedding_indexes idiom): a non-expired token means
    # another refresh of this sheet is in flight. Expiry compares PARSED
    # datetimes (`lease_held`) — a raw string compare breaks under writer
    # format skew (SQLite datetime('now') vs isoformat).
    existing = _receipt_for_idempotency(project, bound.request.idempotency_key)
    own_token = existing["id"] if existing is not None else None
    if sheet["refresh_claim_token"] != own_token and lease_held(
        sheet["refresh_claim_token"], sheet["refresh_lease_expires_at"], now
    ):
        return _fail(
            project_id=project_id,
            code="sheet_refresh_busy",
            message="another refresh of this sheet is already in progress",
            field="sheet_id",
            details={"sheet_id": sheet_id},
        )

    parent_op = cur.execute(
        "SELECT * FROM ops WHERE id=?", (int(sheet["parent_op_id"]),)
    ).fetchone()
    parent_op_kind = parent_op["kind"] if parent_op is not None else None
    if _retired_refresh_parent(project, parent_op):
        return _fail(
            project_id=project_id,
            code="historical_refresh_unavailable",
            message="This refresh setup predates the history migration; recreate the derived sheet.",
            field="sheet_id",
            details={"sheet_id": sheet_id},
        )

    # Saved canonical authoring determines refresh support even for an empty
    # join. Membership rows describe outputs, not the operation that made them.
    refresh_plan = _sheet_refresh_plan(
        project,
        sheet,
        parent_op,
        idempotency_key=bound.request.idempotency_key,
    )
    if refresh_plan is not None and refresh_plan.kind == "join":
        return _refresh_join(
            project,
            cur,
            sheet,
            parent_op,
            refresh_plan.request,
            bound,
            params_hash,
            project_id,
        )

    # Other multi-parent families have no admitted in-place refresh primitive.
    is_multi_parent = (
        cur.execute(
            "SELECT 1 FROM materialized_row_sources mrs "
            "JOIN rows r ON mrs.materialized_row_id = r.id "
            "WHERE r.sheet_id=? LIMIT 1",
            (sheet_id,),
        ).fetchone()
        is not None
    )
    if is_multi_parent:
        return _fail(
            project_id=project_id,
            code="refresh_unsupported",
            message=(
                "multi-parent (resolve/reduce/link_table) sheets cannot be "
                "refreshed in place in v1"
            ),
            field="sheet_id",
            details={
                "sheet_id": sheet_id,
                "parent_op_kind": parent_op_kind,
            },
        )

    if refresh_plan is None:
        return _fail(
            project_id=project_id,
            code="refresh_unsupported",
            message="only single-parent derive.table_from_list sheets refresh in v1",
            field="sheet_id",
            details={
                "sheet_id": sheet_id,
                "parent_op_kind": parent_op_kind,
            },
        )

    update_policy = bound.request.params.get("update_policy", "keep_changes")
    derive_request = refresh_plan.request
    from frisket.engine.executor.table_action import (
        _list_output_ai_generated,
        prepare_table_producer,
    )

    bound = BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get(derive_request.action_id), derive_request
    )
    from frisket.actions.types import TableColumn

    config = _current_refresh_config(cur, sheet_id)
    logical_names = {
        physical: logical for logical, physical in derive_request.output_names.items()
    }
    empty_schema = tuple(
        TableColumn(
            key=logical_names.get(name, name),
            type=column["type"],
            format=column["format"],
            hidden=bool(column["hidden"]),
        )
        for name, column_id in config["columns"].items()
        for column in [
            cur.execute(
                "SELECT type,format,hidden FROM columns WHERE id=?", (column_id,)
            ).fetchone()
        ]
    )
    with prepare_table_producer(
        project, bound, check_sheet_name=False, empty_list_schema=empty_schema
    ) as prepared:
        reader = prepared.readers[ListTableReader]
        if prepared.parent_sheet_id != int(sheet["parent_sheet_id"]):
            raise TableError(
                "invalid_input_ref", "refreshed list must retain its parent sheet"
            )
        copied_by_output = {
            prepared.resolved_output_names.get(column.name, column.name): column
            for column in reader.copied_columns
        }
        if copied_by_output:
            current = {
                int(column["id"]): column
                for column in cur.execute(
                    "SELECT id, name, type, ai_generated, hidden FROM columns "
                    "WHERE sheet_id=? AND active=1",
                    (sheet_id,),
                ).fetchall()
            }
            drifted = [
                name
                for name, copied in copied_by_output.items()
                if (existing := current.get(config["columns"].get(name))) is None
                or existing["type"] != canonical_column_type(copied.type)
                or bool(existing["ai_generated"]) != copied.ai_generated
                or bool(existing["hidden"])
            ]
            if drifted:
                raise TableError(
                    "invalid_input_ref",
                    "Selected source or copied column metadata changed; create a new derived sheet",
                    details={"columns": drifted},
                )
        from frisket.engine.store.derived_refresh_stage import DerivedRefreshStage
        from frisket.engine.store.derived_updates import DerivedRefreshRow
        from itertools import islice

        stage = DerivedRefreshStage(keyed=bool(config["keyed"]))
        try:
            iterator = iter(prepared.rows)
            while batch := list(islice(iterator, 256)):
                checked = _preflight_table_rows(
                    project,
                    prepared.table.model_copy(
                        update={"rows": [item[0] for item in batch]}
                    ),
                    action_kind=derive_request.action_id,
                )
                if isinstance(checked, ActionError):
                    raise TableReadRefused(checked)
                for values, (_, lineage, files) in zip(checked, batch, strict=True):
                    source = lineage.parent
                    if (
                        files
                        or source is None
                        or source not in reader.item_associations
                    ):
                        raise TableError(
                            "invalid_input_ref", "list-item source was not admitted"
                        )
                    stage.add(
                        DerivedRefreshRow(values=values, parent_row_id=source.row_id),
                        metadata=reader.item_associations[source],
                    )
            stage.seal()
            return _PreparedRefresh(
                sheet_id,
                int(sheet["parent_sheet_id"]),
                int(sheet["parent_op_id"]),
                str(sheet["name"]),
                stage,
                tuple(
                    MaterializedColumnSpec(
                        name=column.name,
                        type=column.type,
                        ai_generated=_list_output_ai_generated(
                            reader, column.name, prepared.resolved_output_names
                        ),
                        hidden=column.hidden,
                        format=column.format,
                    )
                    for column in prepared.table.columns
                ),
                config,
                update_policy,
                tuple(deepcopy(reader.facts)),
                tuple(reader.consumed_captures),
                _refresh_source_binding(parent_op, reader.consumed_captures),
            )
        except BaseException:
            stage.close()
            raise


def _refresh_join(
    project: Project,
    cur: Any,
    sheet: Any,
    parent_op: Any,
    derive_request: ActionRequest,
    bound: BoundTypedActionRequest,
    params_hash: str,
    project_id: str,
) -> _PreparedRefresh:
    from frisket.engine.executor.joined_tables_read import JoinRefreshAdmission
    from frisket.engine.executor.table_action import prepare_table_producer

    join_bound = BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get(derive_request.action_id),
        derive_request.model_copy(update={"sheet_name": str(sheet["name"])}),
    )
    with prepare_table_producer(
        project,
        join_bound,
        check_sheet_name=False,
        join_refresh=JoinRefreshAdmission(
            action_kind=bound.action.action_id,
            request_hash=params_hash,
            sheet_id=int(sheet["id"]),
            parent_op_id=int(parent_op["id"]),
            confirmation=bound.request.confirmation,
        ),
    ) as prepared:
        reader = prepared.readers[JoinedTablesReader]
        if prepared.parent_sheet_id != int(sheet["parent_sheet_id"]):
            raise TableError(
                "invalid_input_ref", "refreshed join must retain its parent sheet"
            )
        from frisket.engine.store.derived_refresh_stage import DerivedRefreshStage
        from frisket.engine.store.derived_updates import (
            DerivedRefreshRow,
            join_identity,
        )

        config = _current_refresh_config(cur, int(sheet["id"]))
        stage = DerivedRefreshStage(keyed=bool(config["keyed"]))
        try:
            for values, lineage, files in prepared.rows:
                if files:
                    raise TableError(
                        "invalid_input_ref",
                        "join refresh cannot publish new file handles",
                    )
                reader.validate_lineage(lineage.sources, lineage.parent)
                sources = [
                    (source.row_id, reader.source_roles[source])
                    for source in lineage.sources
                ]
                stage.add(
                    DerivedRefreshRow(
                        values=values,
                        key=join_identity(sources),
                        parent_row_id=lineage.parent.row_id if lineage.parent else None,
                        sources=sources,
                    )
                )
            stage.seal()
            reads = tuple(deepcopy(reader.facts))
            return _PreparedRefresh(
                int(sheet["id"]),
                int(sheet["parent_sheet_id"]),
                int(parent_op["id"]),
                str(sheet["name"]),
                stage,
                prepared.table.columns,
                config,
                bound.request.params.get("update_policy", "keep_changes"),
                reads,
                tuple(reader.consumed_captures),
                _refresh_source_binding(parent_op, reader.consumed_captures),
                {
                    "kind": "derive_join_match_stats",
                    "how": reads[0]["how"],
                    **reads[0]["stats"],
                },
            )
        except BaseException:
            stage.close()
            raise


def _reconstruct_derive_request(
    parent_op: Any, idempotency_key: str
) -> ActionRequest | None:
    if parent_op is None:
        return None
    try:
        spec = json.loads(parent_op["spec"] or "{}")
        if not isinstance(spec, dict) or spec.get("action_id") != parent_op["kind"]:
            return None
        ACTION_REGISTRY.get(spec["action_id"])
        # Only saved authoring intent survives. Original execution consent and
        # idempotency never authorize a new refresh.
        return ActionRequest.model_validate(
            {
                **{
                    key: spec[key]
                    for key in ActionRequest.model_fields
                    if key in spec
                    and key
                    not in {"idempotency_key", "confirmation", "replace_existing"}
                },
                "idempotency_key": idempotency_key,
            }
        )
    except (KeyError, ValueError, TypeError):
        return None


def _current_refresh_config(cur, sheet_id):
    from frisket.engine.store.derived_updates import refresh_config

    parent = cur.execute(
        "SELECT o.* FROM ops o JOIN sheets s ON s.parent_op_id=o.id WHERE s.id=?",
        (sheet_id,),
    ).fetchone()
    config = refresh_config(parent)
    if config is None:
        raise TableError(
            "refresh_unsupported",
            "This derived sheet predates refresh identity; create a new derived sheet",
        )
    return config


def _refresh_source_binding(parent_op, captures):
    from frisket.engine.store.source_dependencies import (
        SOURCE_BINDING,
        execution_source_binding,
    )

    original = json.loads(parent_op["spec"] or "{}").get(SOURCE_BINDING, {})
    scopes = [scope for capture in captures for scope in capture.get("scopes", ())]
    return execution_source_binding(
        sheet_ids=[
            *original.get("sheet_ids", ()),
            *(scope["sheet_id"] for scope in scopes),
        ],
        column_ids=[
            *original.get("column_ids", ()),
            *(cid for scope in scopes for cid in scope["column_ids"]),
        ],
    )


def _begin_staged_refresh(project, project_id, bound, prepared, request_hash):
    from frisket.engine.store.source_dependencies import (
        SOURCE_BINDING,
        require_execution_sources,
    )

    receipt_id = _new_id("receipt")
    op_id = project.append_op(
        "sheet.refresh",
        {
            "sheet_id": prepared.sheet_id,
            "policy": prepared.policy,
            "params_hash": request_hash,
            "refresh_receipt_id": receipt_id,
            "derived_refresh": prepared.config,
            "reads": prepared.reads,
            SOURCE_BINDING: prepared.source_binding,
        },
        label=f"Refresh sheet {prepared.sheet_id}",
        commit=False,
    )
    require_execution_sources(project, op_id)
    receipt = Receipt(
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=_new_id("act"),
        action_kind=bound.action.action_id,
        op_ids=[op_id],
        idempotency_key=bound.request.idempotency_key,
        params_hash=request_hash,
        status="running",
        inputs=[
            ReceiptIO(
                name="target_sheet",
                ref={"kind": "sheet", "sheet_id": prepared.sheet_id},
            )
        ],
    )
    ReceiptStore(project).insert_running(receipt, commit=False)
    acquire_refresh_claim(
        project,
        sheet_id=prepared.sheet_id,
        parent_op_id=prepared.parent_op_id,
        token=receipt.receipt_id,
    )
    return receipt


def _publish_staged_refresh(project, prepared, receipt, *, deps):
    from frisket.engine.store.derived_updates import (
        reconcile_rows_into_open_change,
        absent_derived_batches,
        complete_derived_membership,
    )
    from frisket.engine.store.consumed_sources import merge_captures, store_capture

    op_id = receipt.op_ids[0]
    count = 0
    capture_saved = False

    def commit_batch():
        nonlocal capture_saved
        # Source facts belong to the first published work even if later batches
        # fail. Output completion is the receipt state, not a source-capture flag.
        if not capture_saved and prepared.captures:
            store_capture(
                project,
                merge_captures(prepared.captures),
                owner_op_id=op_id,
                output_sheet_id=prepared.sheet_id,
            )
        project.db.commit()
        capture_saved = True

    def begin():
        if deps is not None and deps.cancelled is not None and deps.cancelled():
            raise TableError(
                "cancelled", "Refresh cancelled; completed changes were kept"
            )
        project.db.execute("BEGIN IMMEDIATE")
        require_refresh_writer(
            project,
            sheet_id=prepared.sheet_id,
            parent_op_id=prepared.parent_op_id,
            op_id=receipt.op_ids[0],
            token=receipt.receipt_id,
        )

    for batch in prepared.stage.batches():
        begin()
        output_ids = reconcile_rows_into_open_change(
            project.db,
            op_id=op_id,
            sheet_id=prepared.sheet_id,
            config=prepared.config,
            columns=prepared.columns,
            rows=[row for _, row, _ in batch],
            policy=prepared.policy,
        )
        for output_id, (_, row, metadata) in zip(output_ids, batch, strict=True):
            if output_id is None or metadata is None:
                continue
            source = RowSource(
                sheet_id=metadata["source_sheet_id"], row_id=metadata["source_row_id"]
            )
            propagate_list_item_evidence(
                project,
                sheet_id=prepared.sheet_id,
                op_id=op_id,
                child_row_ids=[output_id],
                sources=[source],
                item_associations={source: metadata},
            )
        commit_batch()
        prepared.stage.record_published(output_ids)
        count += sum(row is not None for row in output_ids)
    # Only complete enumeration and publication can establish source absence.
    # Cancellation during this phase keeps completed absence batches, like edits.
    for absent in absent_derived_batches(
        project.db, config=prepared.config, seen=prepared.stage
    ):
        begin()
        complete_derived_membership(
            project.db,
            op_id=op_id,
            sheet_id=prepared.sheet_id,
            config=prepared.config,
            observed_rows=absent,
        )
        commit_batch()
    if not capture_saved:
        begin()
        commit_batch()
    return _RefreshFacts(
        prepared.sheet_id,
        prepared.parent_sheet_id,
        prepared.name,
        op_id,
        count,
        prepared.match_stats,
        prepared.reads,
    )


def _finish_refresh_failure(project, receipt, error):
    from frisket.engine.store.changes import ChangeStore

    project.db.execute("BEGIN IMMEDIATE")
    try:
        ChangeStore(project).close(receipt.op_ids[0])
        sheet_id = receipt.inputs[0].ref["sheet_id"]
        release_refresh_claim(project, sheet_id, receipt.receipt_id)
        terminal = receipt.model_copy(
            update={
                "status": "cancelled" if error.code == "cancelled" else "failed",
                "errors": [error],
            }
        )
        ReceiptStore(project).update_body_status(
            terminal, require_status="running", commit=False
        )
        project.db.commit()
        return _result_from_receipt(terminal)
    except BaseException:
        project.db.rollback()
        raise
