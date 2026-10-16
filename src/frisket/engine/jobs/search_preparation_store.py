"""Durable workspace-local search-preparation intents.

The project receipt remains the authority for imported data.  These rows only
remember local orchestration facts that must survive a browser or worker
restart: the exact plan shown for consent, the decision, cancellation, and
deterministic child identities.  They deliberately live beside the queue and
never travel with a project bundle.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator, Mapping, Sequence

import sqlalchemy as sa

from frisket.engine.jobs.queue import (
    _SQLITE_BEGIN_IMMEDIATE_OPT,
    _now,
    _parse,
    jobs_metadata,
)


search_preparation_batches_table = sa.Table(
    "search_preparation_batches",
    jobs_metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("workspace_root", sa.String, nullable=False),
    sa.Column("storage_org_id", sa.Integer),
    sa.Column("project_id", sa.String, nullable=False),
    sa.Column("intent_key", sa.String, nullable=False),
    sa.Column("trigger_receipt_id", sa.String),
    sa.Column("import_ref", sa.String),
    sa.Column("materialization_job_id", sa.Integer),
    sa.Column("sheet_id", sa.Integer),
    sa.Column("scope_json", sa.Text, nullable=False),
    sa.Column("source_cutoff", sa.Integer),
    sa.Column("force_ocr", sa.Boolean, nullable=False),
    sa.Column("plan_hash", sa.String),
    sa.Column("plan_json", sa.Text),
    sa.Column("decision", sa.String),
    sa.Column("decision_at", sa.DateTime(timezone=True)),
    sa.Column("selected_setup_refs_json", sa.Text),
    sa.Column("stop_requested_at", sa.DateTime(timezone=True)),
    sa.Column("settled_at", sa.DateTime(timezone=True)),
    sa.Column("cursor", sa.Integer, nullable=False, server_default="0"),
    sa.Column("error_json", sa.Text, nullable=False, server_default="[]"),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Index(
        "idx_search_preparation_project_created",
        "storage_org_id",
        "project_id",
        "created_at",
    ),
    sa.Index(
        "idx_search_preparation_active",
        "workspace_root",
        "storage_org_id",
        "settled_at",
        "created_at",
    ),
)

sa.Index(
    "uq_search_preparation_import_receipt",
    search_preparation_batches_table.c.workspace_root,
    sa.func.coalesce(search_preparation_batches_table.c.storage_org_id, -1),
    search_preparation_batches_table.c.project_id,
    search_preparation_batches_table.c.trigger_receipt_id,
    unique=True,
    sqlite_where=sa.text("trigger_receipt_id IS NOT NULL"),
    postgresql_where=sa.text("trigger_receipt_id IS NOT NULL"),
)
sa.Index(
    "uq_search_preparation_intent",
    search_preparation_batches_table.c.workspace_root,
    sa.func.coalesce(search_preparation_batches_table.c.storage_org_id, -1),
    search_preparation_batches_table.c.project_id,
    search_preparation_batches_table.c.intent_key,
    unique=True,
)

search_preparation_children_table = sa.Table(
    "search_preparation_children",
    jobs_metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "batch_id",
        sa.String,
        sa.ForeignKey("search_preparation_batches.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("child_key", sa.String, nullable=False),
    sa.Column("kind", sa.String, nullable=False),
    sa.Column("job_id", sa.Integer),
    sa.Column("receipt_id", sa.String),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("batch_id", "child_key", name="uq_search_preparation_child"),
    sa.Index("idx_search_preparation_children_batch", "batch_id", "id"),
)


def canonical_plan_hash(plan: Mapping[str, Any]) -> str:
    payload = json.dumps(
        plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class SearchPreparationBatchRow:
    id: str
    workspace_root: str
    storage_org_id: int | None
    project_id: str
    trigger_receipt_id: str | None
    import_ref: str | None
    materialization_job_id: int | None
    sheet_id: int | None
    row_ids: tuple[int, ...] | None
    source_cutoff: int | None
    force_ocr: bool
    plan_hash: str | None
    plan: dict[str, Any] | None
    decision: str | None
    decision_at: datetime | None
    selected_setup_refs: tuple[str, ...]
    stop_requested_at: datetime | None
    settled_at: datetime | None
    cursor: int
    errors: tuple[str, ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SearchPreparationChildRow:
    id: int
    batch_id: str
    child_key: str
    kind: str
    job_id: int | None
    receipt_id: str | None


class SearchPreparationStore:
    def __init__(self, queue: Any, *, workspace_root: str):
        engine = getattr(queue, "engine", None)
        if engine is None:
            raise TypeError("search preparation requires a SQL-backed job queue")
        self.engine: sa.engine.Engine = engine
        self._is_sqlite = engine.dialect.name == "sqlite"
        self.workspace_root = workspace_root

    @contextmanager
    def _write(self) -> Iterator[sa.Connection]:
        with self.engine.connect() as cx:
            if self._is_sqlite:
                cx = cx.execution_options(**{_SQLITE_BEGIN_IMMEDIATE_OPT: True})
                with cx.begin():
                    yield cx
            else:
                with cx.begin():
                    yield cx

    @staticmethod
    def _batch(row: Any) -> SearchPreparationBatchRow:
        scope = json.loads(row.scope_json)
        selected = json.loads(row.selected_setup_refs_json or "[]")
        errors = json.loads(row.error_json or "[]")
        return SearchPreparationBatchRow(
            id=str(row.id),
            workspace_root=str(row.workspace_root),
            storage_org_id=row.storage_org_id,
            project_id=str(row.project_id),
            trigger_receipt_id=row.trigger_receipt_id,
            import_ref=row.import_ref,
            materialization_job_id=(
                None
                if row.materialization_job_id is None
                else int(row.materialization_job_id)
            ),
            sheet_id=None if row.sheet_id is None else int(row.sheet_id),
            row_ids=(
                tuple(int(value) for value in scope["row_ids"])
                if scope.get("row_ids") is not None
                else None
            ),
            source_cutoff=(
                None if row.source_cutoff is None else int(row.source_cutoff)
            ),
            force_ocr=bool(row.force_ocr),
            plan_hash=None if row.plan_hash is None else str(row.plan_hash),
            plan=(None if row.plan_json is None else dict(json.loads(row.plan_json))),
            decision=row.decision,
            decision_at=_parse(row.decision_at),
            selected_setup_refs=tuple(str(value) for value in selected),
            stop_requested_at=_parse(row.stop_requested_at),
            settled_at=_parse(row.settled_at),
            cursor=int(row.cursor),
            errors=tuple(str(value) for value in errors),
            created_at=_parse(row.created_at),
            updated_at=_parse(row.updated_at),
        )

    def create(
        self,
        *,
        workspace_root: str,
        storage_org_id: int | None,
        project_id: str,
        trigger_receipt_id: str | None,
        sheet_id: int,
        row_ids: Sequence[int] | None,
        source_cutoff: int,
        force_ocr: bool,
        plan: Mapping[str, Any],
        retry_terminal: bool = False,
    ) -> SearchPreparationBatchRow:
        now = _now()
        batch_id = "search-prep-" + uuid.uuid4().hex
        canonical_rows = (
            None if row_ids is None else sorted(set(int(v) for v in row_ids))
        )
        plan_body = dict(plan)
        base_intent_key = (
            f"import:{trigger_receipt_id}"
            if trigger_receipt_id is not None
            else "explicit:"
            + canonical_plan_hash(
                {
                    "sheet_id": sheet_id,
                    "row_ids": canonical_rows,
                    "source_cutoff": source_cutoff,
                    "force_ocr": force_ocr,
                }
            )
        )
        intent_key = base_intent_key
        if trigger_receipt_id is None:
            latest = self._latest_explicit_intent(
                storage_org_id=storage_org_id,
                project_id=project_id,
                base_key=base_intent_key,
            )
            if latest is not None:
                if not retry_terminal:
                    return latest
                intent_key = f"{base_intent_key}:retry:{latest.id}"
        values = dict(
            id=batch_id,
            workspace_root=workspace_root,
            storage_org_id=storage_org_id,
            project_id=project_id,
            intent_key=intent_key,
            trigger_receipt_id=trigger_receipt_id,
            sheet_id=sheet_id,
            scope_json=json.dumps({"row_ids": canonical_rows}, separators=(",", ":")),
            source_cutoff=source_cutoff,
            force_ocr=force_ocr,
            plan_hash=canonical_plan_hash(plan_body),
            plan_json=json.dumps(plan_body, sort_keys=True, separators=(",", ":")),
            cursor=0,
            error_json="[]",
            settled_at=None,
            created_at=now,
            updated_at=now,
        )
        if workspace_root != self.workspace_root:
            raise ValueError("search preparation workspace root does not match store")
        try:
            with self._write() as cx:
                cx.execute(search_preparation_batches_table.insert().values(**values))
        except sa.exc.IntegrityError:
            existing = (
                self._for_intent(
                    storage_org_id=storage_org_id,
                    project_id=project_id,
                    intent_key=intent_key,
                )
                if trigger_receipt_id is not None
                else self._latest_explicit_intent(
                    storage_org_id=storage_org_id,
                    project_id=project_id,
                    base_key=base_intent_key,
                )
            )
            if existing is None:
                raise
            return existing
        return self.get(batch_id, project_id=project_id, storage_org_id=storage_org_id)

    def create_pending_import(
        self,
        *,
        workspace_root: str,
        storage_org_id: int | None,
        project_id: str,
        receipt_id: str,
        import_ref: str,
    ) -> SearchPreparationBatchRow:
        """Reserve the import-triggered intent before a worker can finish it."""

        now = _now()
        batch_id = "search-prep-" + uuid.uuid4().hex
        values = dict(
            id=batch_id,
            workspace_root=workspace_root,
            storage_org_id=storage_org_id,
            project_id=project_id,
            intent_key=f"import:{receipt_id}",
            trigger_receipt_id=receipt_id,
            import_ref=import_ref,
            sheet_id=None,
            scope_json=json.dumps({"row_ids": None}, separators=(",", ":")),
            source_cutoff=None,
            force_ocr=False,
            plan_hash=None,
            plan_json=None,
            cursor=0,
            error_json="[]",
            settled_at=None,
            created_at=now,
            updated_at=now,
        )
        if workspace_root != self.workspace_root:
            raise ValueError("search preparation workspace root does not match store")
        try:
            with self._write() as cx:
                cx.execute(search_preparation_batches_table.insert().values(**values))
        except sa.exc.IntegrityError:
            existing = self._for_intent(
                storage_org_id=storage_org_id,
                project_id=project_id,
                intent_key=f"import:{receipt_id}",
            )
            if existing is None:
                raise
            if existing.import_ref not in {None, import_ref}:
                raise ValueError("import preparation receipt names another session")
            return existing
        return self.get(batch_id, project_id=project_id, storage_org_id=storage_org_id)

    def create_pending_materialization(
        self,
        *,
        workspace_root: str,
        storage_org_id: int | None,
        project_id: str,
        job_id: int,
        sheet_id: int,
        row_ids: Sequence[int],
    ) -> SearchPreparationBatchRow:
        """Record one exact enclosure job for server-side reconciliation."""

        now = _now()
        batch_id = "search-prep-" + uuid.uuid4().hex
        intent_key = f"materialization:{int(job_id)}"
        values = dict(
            id=batch_id,
            workspace_root=workspace_root,
            storage_org_id=storage_org_id,
            project_id=project_id,
            intent_key=intent_key,
            materialization_job_id=int(job_id),
            sheet_id=int(sheet_id),
            scope_json=json.dumps(
                {"row_ids": sorted(set(int(value) for value in row_ids))},
                separators=(",", ":"),
            ),
            force_ocr=False,
            plan_json=None,
            cursor=0,
            error_json="[]",
            settled_at=None,
            created_at=now,
            updated_at=now,
        )
        if workspace_root != self.workspace_root:
            raise ValueError("search preparation workspace root does not match store")
        try:
            with self._write() as cx:
                cx.execute(search_preparation_batches_table.insert().values(**values))
        except sa.exc.IntegrityError:
            existing = self._for_intent(
                storage_org_id=storage_org_id,
                project_id=project_id,
                intent_key=intent_key,
            )
            if existing is None:
                raise
            return existing
        return self.get(batch_id, project_id=project_id, storage_org_id=storage_org_id)

    def pending_materialization(
        self,
        *,
        project_id: str,
        storage_org_id: int | None,
        job_id: int,
        sheet_id: int,
        row_ids: Sequence[int],
    ) -> SearchPreparationBatchRow | None:
        wanted = sorted(set(int(value) for value in row_ids))
        c = search_preparation_batches_table.c
        predicate = (
            c.storage_org_id.is_(None)
            if storage_org_id is None
            else c.storage_org_id == storage_org_id
        )
        with self.engine.connect() as cx:
            rows = cx.execute(
                sa.select(search_preparation_batches_table)
                .where(
                    c.workspace_root == self.workspace_root,
                    c.project_id == project_id,
                    predicate,
                    c.materialization_job_id == int(job_id),
                    c.sheet_id == int(sheet_id),
                    c.plan_json.is_(None),
                    c.settled_at.is_(None),
                )
                .order_by(c.created_at, c.id)
            ).fetchall()
        return next(
            (
                self._batch(row)
                for row in rows
                if self._batch(row).row_ids == tuple(wanted)
            ),
            None,
        )

    def hydrate_pending_import(
        self,
        batch_id: str,
        *,
        project_id: str,
        storage_org_id: int | None,
        sheet_id: int,
        source_cutoff: int,
        plan: Mapping[str, Any],
    ) -> SearchPreparationBatchRow:
        """Atomically turn one pending import reservation into an ordinary plan."""

        c = search_preparation_batches_table.c
        predicate = (
            c.storage_org_id.is_(None)
            if storage_org_id is None
            else c.storage_org_id == storage_org_id
        )
        body = dict(plan)
        with self._write() as cx:
            result = cx.execute(
                search_preparation_batches_table.update()
                .where(
                    c.id == batch_id,
                    c.workspace_root == self.workspace_root,
                    c.project_id == project_id,
                    predicate,
                    c.sheet_id.is_(None),
                    c.plan_json.is_(None),
                    c.stop_requested_at.is_(None),
                )
                .values(
                    sheet_id=int(sheet_id),
                    source_cutoff=int(source_cutoff),
                    plan_hash=canonical_plan_hash(body),
                    plan_json=json.dumps(body, sort_keys=True, separators=(",", ":")),
                    settled_at=None,
                    updated_at=_now(),
                )
            )
            if result.rowcount not in {0, 1}:
                raise RuntimeError("import preparation hydration updated multiple rows")
        return self.get(batch_id, project_id=project_id, storage_org_id=storage_org_id)

    def hydrate_pending_materialization(
        self,
        batch_id: str,
        *,
        project_id: str,
        storage_org_id: int | None,
        source_cutoff: int,
        plan: Mapping[str, Any],
    ) -> SearchPreparationBatchRow:
        """Atomically attach a plan after the recorded materialization finishes."""

        c = search_preparation_batches_table.c
        predicate = (
            c.storage_org_id.is_(None)
            if storage_org_id is None
            else c.storage_org_id == storage_org_id
        )
        body = dict(plan)
        with self._write() as cx:
            result = cx.execute(
                search_preparation_batches_table.update()
                .where(
                    c.id == batch_id,
                    c.workspace_root == self.workspace_root,
                    c.project_id == project_id,
                    predicate,
                    c.materialization_job_id.is_not(None),
                    c.plan_json.is_(None),
                    c.stop_requested_at.is_(None),
                )
                .values(
                    source_cutoff=int(source_cutoff),
                    plan_hash=canonical_plan_hash(body),
                    plan_json=json.dumps(body, sort_keys=True, separators=(",", ":")),
                    settled_at=None,
                    updated_at=_now(),
                )
            )
            if result.rowcount not in {0, 1}:
                raise RuntimeError(
                    "materialization preparation hydration updated multiple rows"
                )
        return self.get(batch_id, project_id=project_id, storage_org_id=storage_org_id)

    def _for_intent(
        self, *, storage_org_id: int | None, project_id: str, intent_key: str
    ) -> SearchPreparationBatchRow | None:
        c = search_preparation_batches_table.c
        predicate = (
            c.storage_org_id.is_(None)
            if storage_org_id is None
            else c.storage_org_id == storage_org_id
        )
        with self.engine.connect() as cx:
            row = cx.execute(
                sa.select(search_preparation_batches_table).where(
                    c.workspace_root == self.workspace_root,
                    predicate,
                    c.project_id == project_id,
                    c.intent_key == intent_key,
                )
            ).first()
        return None if row is None else self._batch(row)

    def _latest_explicit_intent(
        self, *, storage_org_id: int | None, project_id: str, base_key: str
    ) -> SearchPreparationBatchRow | None:
        c = search_preparation_batches_table.c
        predicate = (
            c.storage_org_id.is_(None)
            if storage_org_id is None
            else c.storage_org_id == storage_org_id
        )
        with self.engine.connect() as cx:
            row = cx.execute(
                sa.select(search_preparation_batches_table)
                .where(
                    c.workspace_root == self.workspace_root,
                    predicate,
                    c.project_id == project_id,
                    sa.or_(
                        c.intent_key == base_key,
                        c.intent_key.like(base_key + ":retry:%"),
                    ),
                )
                .order_by(c.created_at.desc(), c.id.desc())
                .limit(1)
            ).first()
        return None if row is None else self._batch(row)

    def get(
        self, batch_id: str, *, project_id: str, storage_org_id: int | None
    ) -> SearchPreparationBatchRow:
        c = search_preparation_batches_table.c
        with self.engine.connect() as cx:
            row = cx.execute(
                sa.select(search_preparation_batches_table).where(
                    c.id == batch_id,
                    c.workspace_root == self.workspace_root,
                    c.project_id == project_id,
                    c.storage_org_id.is_(storage_org_id)
                    if storage_org_id is None
                    else c.storage_org_id == storage_org_id,
                )
            ).first()
        if row is None:
            raise KeyError(batch_id)
        return self._batch(row)

    def for_receipt(
        self,
        *,
        workspace_root: str,
        storage_org_id: int | None,
        project_id: str,
        receipt_id: str,
        include_pending: bool = False,
    ) -> SearchPreparationBatchRow | None:
        c = search_preparation_batches_table.c
        predicate = (
            c.storage_org_id.is_(None)
            if storage_org_id is None
            else c.storage_org_id == storage_org_id
        )
        with self.engine.connect() as cx:
            query = sa.select(search_preparation_batches_table).where(
                c.workspace_root == workspace_root,
                predicate,
                c.project_id == project_id,
                c.trigger_receipt_id == receipt_id,
            )
            if not include_pending:
                query = query.where(c.plan_json.is_not(None))
            row = cx.execute(query).first()
        return None if row is None else self._batch(row)

    def list_recent(
        self, *, project_id: str, storage_org_id: int | None, limit: int = 20
    ) -> list[SearchPreparationBatchRow]:
        c = search_preparation_batches_table.c
        predicate = (
            c.storage_org_id.is_(None)
            if storage_org_id is None
            else c.storage_org_id == storage_org_id
        )
        with self.engine.connect() as cx:
            rows = cx.execute(
                sa.select(search_preparation_batches_table)
                .where(c.project_id == project_id, predicate, c.plan_json.is_not(None))
                .where(c.workspace_root == self.workspace_root)
                .order_by(c.created_at.desc())
                .limit(limit)
            ).fetchall()
        return [self._batch(row) for row in rows]

    def list_active_page(
        self,
        *,
        storage_org_id: int | None,
        limit: int = 100,
        after_created_at: datetime | None = None,
        after_id: str | None = None,
    ) -> list[SearchPreparationBatchRow]:
        """Bounded stable page of coordinator work across this workspace."""

        if limit < 1 or limit > 500 or (after_created_at is None) != (after_id is None):
            raise ValueError("invalid search preparation reconciliation page")
        c = search_preparation_batches_table.c
        predicate = (
            c.storage_org_id.is_(None)
            if storage_org_id is None
            else c.storage_org_id == storage_org_id
        )
        query = sa.select(search_preparation_batches_table).where(
            c.workspace_root == self.workspace_root,
            predicate,
            c.settled_at.is_(None),
        )
        if after_created_at is not None and after_id is not None:
            query = query.where(
                sa.or_(
                    c.created_at > after_created_at,
                    sa.and_(c.created_at == after_created_at, c.id > after_id),
                )
            )
        with self.engine.connect() as cx:
            rows = cx.execute(
                query.order_by(c.created_at, c.id).limit(limit)
            ).fetchall()
        return [self._batch(row) for row in rows]

    def decide(
        self,
        batch_id: str,
        *,
        project_id: str,
        storage_org_id: int | None,
        plan_hash: str,
        decision: str,
        selected_setup_refs: Sequence[str],
    ) -> SearchPreparationBatchRow:
        if decision not in {"prepare", "not_now"}:
            raise ValueError("invalid search preparation decision")
        c = search_preparation_batches_table.c
        predicate = (
            c.storage_org_id.is_(None)
            if storage_org_id is None
            else c.storage_org_id == storage_org_id
        )
        with self._write() as cx:
            row = cx.execute(
                sa.select(
                    c.plan_hash,
                    c.decision,
                    c.decision_at,
                    c.selected_setup_refs_json,
                    c.stop_requested_at,
                ).where(
                    c.id == batch_id,
                    c.project_id == project_id,
                    predicate,
                    c.workspace_root == self.workspace_root,
                )
            ).first()
            if row is None:
                raise KeyError(batch_id)
            if row.stop_requested_at is not None:
                raise ValueError("search preparation was stopped")
            if row.plan_hash != plan_hash:
                raise ValueError("search preparation plan changed")
            refs = json.dumps(sorted(set(selected_setup_refs)), separators=(",", ":"))
            if row.decision is not None and (
                row.decision != decision
                or (row.selected_setup_refs_json or "[]") != refs
            ):
                raise ValueError("search preparation decision is already recorded")
            now = _now()
            cx.execute(
                search_preparation_batches_table.update()
                .where(c.id == batch_id)
                .values(
                    decision=decision,
                    decision_at=row.decision_at or now,
                    selected_setup_refs_json=refs,
                    settled_at=None,
                    updated_at=now,
                )
            )
        return self.get(batch_id, project_id=project_id, storage_org_id=storage_org_id)

    def replan(
        self,
        batch_id: str,
        *,
        project_id: str,
        storage_org_id: int | None,
        source_cutoff: int,
        plan: Mapping[str, Any],
    ) -> SearchPreparationBatchRow:
        """Replace an undecided stale plan while retaining completed children."""

        c = search_preparation_batches_table.c
        predicate = (
            c.storage_org_id.is_(None)
            if storage_org_id is None
            else c.storage_org_id == storage_org_id
        )
        body = dict(plan)
        with self._write() as cx:
            result = cx.execute(
                search_preparation_batches_table.update()
                .where(
                    c.id == batch_id,
                    c.workspace_root == self.workspace_root,
                    c.project_id == project_id,
                    predicate,
                    c.decision.is_(None),
                )
                .values(
                    source_cutoff=source_cutoff,
                    plan_hash=canonical_plan_hash(body),
                    plan_json=json.dumps(body, sort_keys=True, separators=(",", ":")),
                    settled_at=None,
                    updated_at=_now(),
                )
            )
            if result.rowcount != 1:
                raise ValueError("search preparation plan can no longer be refreshed")
        return self.get(batch_id, project_id=project_id, storage_org_id=storage_org_id)

    def request_stop(
        self, batch_id: str, *, project_id: str, storage_org_id: int | None
    ) -> SearchPreparationBatchRow:
        c = search_preparation_batches_table.c
        predicate = (
            c.storage_org_id.is_(None)
            if storage_org_id is None
            else c.storage_org_id == storage_org_id
        )
        with self._write() as cx:
            result = cx.execute(
                search_preparation_batches_table.update()
                .where(c.id == batch_id, c.project_id == project_id, predicate)
                .where(c.workspace_root == self.workspace_root)
                .values(stop_requested_at=_now(), settled_at=_now(), updated_at=_now())
            )
            if result.rowcount != 1:
                raise KeyError(batch_id)
        return self.get(batch_id, project_id=project_id, storage_org_id=storage_org_id)

    def append_error(
        self,
        batch_id: str,
        *,
        project_id: str,
        storage_org_id: int | None,
        message: str,
    ) -> SearchPreparationBatchRow:
        """Record a bounded terminal coordinator error for a scoped intent."""

        row = self.get(batch_id, project_id=project_id, storage_org_id=storage_org_id)
        errors = [*row.errors, str(message).strip()[:500]][-10:]
        c = search_preparation_batches_table.c
        predicate = (
            c.storage_org_id.is_(None)
            if storage_org_id is None
            else c.storage_org_id == storage_org_id
        )
        with self._write() as cx:
            result = cx.execute(
                search_preparation_batches_table.update()
                .where(
                    c.id == batch_id,
                    c.workspace_root == self.workspace_root,
                    c.project_id == project_id,
                    predicate,
                )
                .values(
                    error_json=json.dumps(errors, separators=(",", ":")),
                    settled_at=_now(),
                    updated_at=_now(),
                )
            )
            if result.rowcount != 1:
                raise KeyError(batch_id)
        return self.get(batch_id, project_id=project_id, storage_org_id=storage_org_id)

    def mark_settled(
        self,
        batch_id: str,
        *,
        project_id: str,
        storage_org_id: int | None,
    ) -> None:
        c = search_preparation_batches_table.c
        predicate = (
            c.storage_org_id.is_(None)
            if storage_org_id is None
            else c.storage_org_id == storage_org_id
        )
        with self._write() as cx:
            result = cx.execute(
                search_preparation_batches_table.update()
                .where(
                    c.id == batch_id,
                    c.workspace_root == self.workspace_root,
                    c.project_id == project_id,
                    predicate,
                    c.settled_at.is_(None),
                )
                .values(settled_at=_now(), updated_at=_now())
            )
            if result.rowcount not in {0, 1}:
                raise RuntimeError(
                    "search preparation settlement updated multiple rows"
                )

    def reserve_child_and_advance(
        self,
        batch_id: str,
        *,
        expected_cursor: int,
        child_key: str,
        kind: str,
    ) -> SearchPreparationChildRow:
        """Atomically reserve one deterministic child before cursor advancement."""

        b = search_preparation_batches_table.c
        ch = search_preparation_children_table.c
        now = _now()
        with self._write() as cx:
            existing = cx.execute(
                sa.select(search_preparation_children_table).where(
                    ch.batch_id == batch_id, ch.child_key == child_key
                )
            ).first()
            if existing is None:
                result = cx.execute(
                    search_preparation_children_table.insert().values(
                        batch_id=batch_id,
                        child_key=child_key,
                        kind=kind,
                        created_at=now,
                    )
                )
                child_id = int(result.inserted_primary_key[0])
                existing = cx.execute(
                    sa.select(search_preparation_children_table).where(
                        ch.id == child_id
                    )
                ).one()
            update = cx.execute(
                search_preparation_batches_table.update()
                .where(
                    b.id == batch_id,
                    b.cursor == expected_cursor,
                    b.stop_requested_at.is_(None),
                )
                .values(cursor=expected_cursor + 1, updated_at=now)
            )
            if update.rowcount != 1:
                current = cx.execute(
                    sa.select(b.cursor).where(b.id == batch_id)
                ).scalar_one_or_none()
                if current != expected_cursor + 1:
                    raise ValueError("search preparation cursor changed")
        return SearchPreparationChildRow(
            id=int(existing.id),
            batch_id=str(existing.batch_id),
            child_key=str(existing.child_key),
            kind=str(existing.kind),
            job_id=existing.job_id,
            receipt_id=existing.receipt_id,
        )

    def bind_child(
        self,
        child_id: int,
        *,
        job_id: int | None,
        receipt_id: str | None,
    ) -> bool:
        c = search_preparation_children_table.c
        b = search_preparation_batches_table.c
        with self._write() as cx:
            result = cx.execute(
                search_preparation_children_table.update()
                .where(
                    c.id == child_id,
                    c.job_id.is_(None),
                    c.receipt_id.is_(None),
                    sa.exists(
                        sa.select(sa.literal(1)).where(
                            b.id == c.batch_id,
                            b.stop_requested_at.is_(None),
                        )
                    ),
                )
                .values(job_id=job_id, receipt_id=receipt_id)
            )
            if result.rowcount == 1:
                return True
            row = cx.execute(
                sa.select(c.job_id, c.receipt_id, c.batch_id).where(c.id == child_id)
            ).first()
            if row is None:
                raise KeyError(child_id)
            if row.job_id == job_id and row.receipt_id == receipt_id:
                return True
            stopped = cx.execute(
                sa.select(b.stop_requested_at).where(b.id == row.batch_id)
            ).scalar_one()
            if stopped is not None and row.job_id is None and row.receipt_id is None:
                return False
            raise ValueError("search preparation child is already bound")

    def children(self, batch_id: str) -> list[SearchPreparationChildRow]:
        c = search_preparation_children_table.c
        with self.engine.connect() as cx:
            rows = cx.execute(
                sa.select(search_preparation_children_table)
                .where(c.batch_id == batch_id)
                .order_by(c.id)
            ).fetchall()
        return [
            SearchPreparationChildRow(
                id=int(row.id),
                batch_id=str(row.batch_id),
                child_key=str(row.child_key),
                kind=str(row.kind),
                job_id=row.job_id,
                receipt_id=row.receipt_id,
            )
            for row in rows
        ]


__all__ = [
    "SearchPreparationBatchRow",
    "SearchPreparationChildRow",
    "SearchPreparationStore",
    "canonical_plan_hash",
    "search_preparation_batches_table",
    "search_preparation_children_table",
]
