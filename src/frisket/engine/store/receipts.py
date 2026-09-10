"""Receipt table store for executor idempotency and replay rows."""

from __future__ import annotations

import json
from dataclasses import dataclass
from collections.abc import Iterable, Mapping
from typing import Any, Iterator, Literal

from frisket.contracts.action import Receipt, ReceiptEvidence

ReceiptStatus = Literal[
    "queued", "running", "completed", "partial", "failed", "cancelled"
]
FINISHED_RECEIPT_STATUSES: frozenset[ReceiptStatus] = frozenset(
    {"completed", "partial", "failed", "cancelled"}
)
_BACKFILL_ACTIVITY_WHERE = (
    "action_kind='run.backfill' "
    "AND status='completed' "
    "AND json_extract(body, '$.outputs[0].ref.kind')='run_backfill' "
    "AND json_extract(body, '$.outputs[0].ref.sheet_id') IS NOT NULL "
    "AND json_extract(body, '$.outputs[0].ref.column_id') IS NOT NULL"
)
_BACKFILL_ACTIVITY_COLUMN_EXPR = (
    "CAST(json_extract(body, '$.outputs[0].ref.column_id') AS INTEGER)"
)


@dataclass(frozen=True)
class StoredReceipt:
    id: str
    run_id: int | None
    action_kind: str
    action_id: str | None
    idempotency_key: str | None
    params_hash: str
    status: str
    body: str
    created_at: str | None

    @classmethod
    def from_row(cls, row: Any) -> "StoredReceipt":
        return cls(
            id=str(row["id"]),
            run_id=int(row["run_id"]) if row["run_id"] is not None else None,
            action_kind=str(row["action_kind"]),
            action_id=str(row["action_id"]) if row["action_id"] is not None else None,
            idempotency_key=(
                str(row["idempotency_key"])
                if row["idempotency_key"] is not None
                else None
            ),
            params_hash=str(row["params_hash"] or ""),
            status=str(row["status"]),
            body=str(row["body"] or "{}"),
            created_at=str(row["created_at"])
            if row["created_at"] is not None
            else None,
        )

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def keys(self) -> tuple[str, ...]:
        return (
            "id",
            "run_id",
            "action_kind",
            "action_id",
            "idempotency_key",
            "params_hash",
            "status",
            "body",
            "created_at",
        )

    def __iter__(self) -> Iterator[str]:
        return iter(self.keys())

    def parsed(self) -> Receipt:
        return Receipt.model_validate_json(self.body)


def receipt_body(receipt: Receipt) -> str:
    return json.dumps(receipt.model_dump(mode="json"), sort_keys=True)


def edition_run_context_body(
    edition_run_context: Mapping[str, Any] | None,
) -> str | None:
    """Encode one opaque edition snapshot for its private receipt sidecar."""
    if edition_run_context is None:
        return None
    return json.dumps(
        dict(edition_run_context),
        sort_keys=True,
        allow_nan=False,
    )


class ReceiptStore:
    def __init__(self, project: Any):
        self.project = project

    @property
    def db(self) -> Any:
        """The CALLING thread's connection, resolved per access — see
        ``RunResultStore.db``: capturing ``project.db`` in ``__init__`` hands
        every thread the CONSTRUCTING thread's connection, which races
        sqlite3's statement cache."""
        return self.project.db

    def record_python_code_hash(
        self,
        code_hash: str,
        *,
        run_id: int,
        writer_attempt_id: str,
        claim_token: str,
    ) -> None:
        """Pin actual evaluator arguments before a guest can run or publish.

        The existing reservation is the durable evidence owner, including
        before its first finalization. A fresh worker can rebuild the final
        receipt without inspecting Params or executing the guest again.
        """
        self._record_writer_evidence(
            {"kind": "map_python_code", "code_hash": code_hash},
            run_id=run_id,
            writer_attempt_id=writer_attempt_id,
            claim_token=claim_token,
        )

    def record_pdf_table_read(
        self,
        fact: dict[str, Any],
        *,
        run_id: int,
        writer_attempt_id: str,
        claim_token: str,
    ) -> None:
        """Attach an output-bound PDF read inside the accepted result savepoint."""
        self._record_writer_evidence(
            {**fact, "kind": "pdf_table_read"},
            run_id=run_id,
            writer_attempt_id=writer_attempt_id,
            claim_token=claim_token,
        )

    def record_semantic_match(
        self,
        fact: dict[str, Any],
        *,
        run_id: int,
        writer_attempt_id: str,
        claim_token: str,
    ) -> None:
        """Pin actual matcher arguments before transient row checkpoints retire."""
        from frisket.ai.llm.types import provider_from_model_id
        from frisket.semantic import embedder_is_remote

        model = fact["model"]
        for ref in (
            {
                "kind": "semantic_join_embedding_backend",
                "model": model,
                "provider": provider_from_model_id(model)
                if embedder_is_remote(model)
                else "local",
                "backend_remote": embedder_is_remote(model),
            },
            {
                "kind": "semantic_join_thresholds",
                "match_threshold": fact["match_threshold"],
                "confident_threshold": fact["confident_threshold"],
            },
        ):
            self._record_writer_evidence(
                ref,
                run_id=run_id,
                writer_attempt_id=writer_attempt_id,
                claim_token=claim_token,
            )

    def _record_writer_evidence(
        self,
        ref: dict[str, Any],
        *,
        run_id: int,
        writer_attempt_id: str,
        claim_token: str,
        provider_use_fact: dict[str, Any] | None = None,
    ) -> None:
        from frisket.engine.store.output_claims import OutputColumnClaimStore

        db = self.db
        db.execute("SAVEPOINT python_code_evidence")
        try:
            OutputColumnClaimStore.require_current_writer(
                db,
                run_id=run_id,
                writer_attempt_id=writer_attempt_id,
                claim_token=claim_token,
            )
            refs = db.execute(
                "SELECT DISTINCT receipt_id FROM output_column_claims "
                "WHERE run_id=? AND claim_token=? AND status='active'",
                (run_id, claim_token),
            ).fetchall()
            if len(refs) != 1 or refs[0][0] is None:
                raise RuntimeError("Python evaluation requires its reserved receipt")
            stored = self.find_by_id(str(refs[0][0]))
            if stored is None or stored.status not in {"queued", "running"}:
                raise RuntimeError("Python evaluation receipt is no longer active")
            receipt = stored.parsed()
            op_id = db.execute(
                "SELECT op_id FROM runs WHERE id=?", (run_id,)
            ).fetchone()[0]
            ref = {
                **ref,
                "op_id": op_id,
                "run_id": run_id,
            }
            if not any(item.ref == ref for item in receipt.evidence):
                receipt.evidence.append(ReceiptEvidence(ref=ref, retention="pinned"))
                if provider_use_fact is not None:
                    receipt.provider_use.append(dict(provider_use_fact))
                receipt.run_id = run_id
                if not self.update_body_status(
                    receipt, require_status=stored.status, commit=False
                ):
                    raise RuntimeError("Python evaluation lost its reserved receipt")
            db.execute("RELEASE SAVEPOINT python_code_evidence")
        except BaseException:
            db.execute("ROLLBACK TO SAVEPOINT python_code_evidence")
            db.execute("RELEASE SAVEPOINT python_code_evidence")
            raise

    def find_by_idempotency_key(self, key: str | None) -> StoredReceipt | None:
        if not key:
            return None
        row = self.db.execute(
            "SELECT id, run_id, action_kind, action_id, "
            "idempotency_key, params_hash, status, body, created_at "
            "FROM receipts WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        return StoredReceipt.from_row(row) if row is not None else None

    def find_by_action_kind_and_idempotency_key(
        self,
        *,
        action_kind: str,
        key: str | None,
    ) -> StoredReceipt | None:
        if not key:
            return None
        row = self.db.execute(
            "SELECT id, run_id, action_kind, action_id, "
            "idempotency_key, params_hash, status, body, created_at "
            "FROM receipts WHERE action_kind=? AND idempotency_key=?",
            (action_kind, key),
        ).fetchone()
        return StoredReceipt.from_row(row) if row is not None else None

    def find_by_id(self, receipt_id: str) -> StoredReceipt | None:
        row = self.db.execute(
            "SELECT id, run_id, action_kind, action_id, "
            "idempotency_key, params_hash, status, body, created_at "
            "FROM receipts WHERE id=?",
            (receipt_id,),
        ).fetchone()
        return StoredReceipt.from_row(row) if row is not None else None

    def body_by_id(self, receipt_id: str) -> str | None:
        row = self.db.execute(
            "SELECT body FROM receipts WHERE id=?",
            (receipt_id,),
        ).fetchone()
        return str(row["body"]) if row is not None else None

    def bodies_by_id(self, receipt_ids: Iterable[str]) -> dict[str, str]:
        ids = sorted({str(receipt_id) for receipt_id in receipt_ids})
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self.db.execute(
            f"SELECT id, body FROM receipts WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        return {str(row["id"]): str(row["body"] or "{}") for row in rows}

    def parsed_by_id(self, receipt_id: str) -> Receipt | None:
        body = self.body_by_id(receipt_id)
        return Receipt.model_validate_json(body) if body is not None else None

    def status_by_id(self, receipt_id: str) -> str | None:
        row = self.db.execute(
            "SELECT status FROM receipts WHERE id=?",
            (receipt_id,),
        ).fetchone()
        return str(row["status"]) if row is not None else None

    def latest_id_for_run(self, run_id: int) -> str | None:
        row = self.db.execute(
            "SELECT id FROM receipts WHERE run_id=? ORDER BY rowid DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return str(row["id"]) if row is not None else None

    def count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0])

    def watermark_rows(self) -> list[Any]:
        return self.db.execute(
            "SELECT rowid, id, run_id, action_kind, "
            "idempotency_key, status, created_at, LENGTH(body) AS body_length "
            "FROM receipts ORDER BY rowid"
        ).fetchall()

    def index_rows(self) -> list[Any]:
        return self.db.execute(
            "SELECT rowid, id, run_id, action_kind, "
            "idempotency_key, params_hash, status, body, created_at, "
            "LENGTH(body) AS body_length "
            "FROM receipts ORDER BY rowid"
        ).fetchall()

    def recent_metadata_page(self, *, limit: int, offset: int) -> list[StoredReceipt]:
        rows = self.db.execute(
            "SELECT id, run_id, action_kind, action_id, "
            "idempotency_key, params_hash, status, body, created_at "
            "FROM receipts ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
            (int(limit), int(offset)),
        ).fetchall()
        return [StoredReceipt.from_row(row) for row in rows]

    def for_action_kind(
        self,
        action_kind: str,
        *,
        newest_first: bool = False,
    ) -> list[StoredReceipt]:
        order = "created_at DESC, rowid DESC" if newest_first else "created_at, rowid"
        rows = self.db.execute(
            "SELECT id, run_id, action_kind, action_id, "
            "idempotency_key, params_hash, status, body, created_at "
            "FROM receipts WHERE action_kind=? "
            f"ORDER BY {order}",
            (action_kind,),
        ).fetchall()
        return [StoredReceipt.from_row(row) for row in rows]

    def for_action_status(
        self,
        *,
        action_kind: str,
        status: str,
        newest_first: bool = False,
        limit: int | None = None,
    ) -> list[StoredReceipt]:
        order = "created_at DESC, rowid DESC" if newest_first else "created_at, rowid"
        limit_clause = "" if limit is None else " LIMIT ?"
        params: list[Any] = [action_kind, status]
        if limit is not None:
            params.append(int(limit))
        rows = self.db.execute(
            "SELECT id, run_id, action_kind, action_id, "
            "idempotency_key, params_hash, status, body, created_at "
            "FROM receipts WHERE action_kind=? AND status=? "
            f"ORDER BY {order}{limit_clause}",
            params,
        ).fetchall()
        return [StoredReceipt.from_row(row) for row in rows]

    def backfill_activity_count(self, *, column_id: int | None = None) -> int:
        where = _BACKFILL_ACTIVITY_WHERE
        params: list[Any] = []
        if column_id is not None:
            where += f" AND {_BACKFILL_ACTIVITY_COLUMN_EXPR}=?"
            params.append(int(column_id))
        return int(
            self.db.execute(
                f"SELECT COUNT(*) FROM receipts WHERE {where}",
                params,
            ).fetchone()[0]
        )

    def backfill_activity_rows(
        self,
        *,
        offset: int,
        limit: int,
        column_id: int | None = None,
    ) -> list[Any]:
        where = _BACKFILL_ACTIVITY_WHERE
        params: list[Any] = []
        if column_id is not None:
            where += f" AND {_BACKFILL_ACTIVITY_COLUMN_EXPR}=?"
            params.append(int(column_id))
        return self.db.execute(
            "SELECT id, run_id, status, body, created_at FROM receipts "
            f"WHERE {where} ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
            (*params, int(limit), int(offset)),
        ).fetchall()

    def latest_for_run_statuses(
        self,
        run_id: int,
        statuses: Iterable[str],
    ) -> StoredReceipt | None:
        allowed = tuple(dict.fromkeys(str(status) for status in statuses))
        if not allowed:
            raise ValueError("at least one receipt status is required")
        placeholders = ",".join("?" for _ in allowed)
        row = self.db.execute(
            "SELECT id, run_id, action_kind, action_id, "
            "idempotency_key, params_hash, status, body, created_at "
            "FROM receipts "
            f"WHERE run_id=? AND status IN ({placeholders}) "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (run_id, *allowed),
        ).fetchone()
        return StoredReceipt.from_row(row) if row is not None else None

    def latest_for_run_action_statuses(
        self,
        run_id: int,
        action_kind: str,
        statuses: Iterable[str],
    ) -> StoredReceipt | None:
        """Return the newest matching action receipt for a run.

        A run can also acquire receipts for follow-up actions such as
        ``run.backfill``. Keeping the action-kind predicate in the store avoids
        letting a newer auxiliary receipt mask the producer receipt while also
        preserving the rule that production receipt SQL lives here.
        """
        allowed = tuple(dict.fromkeys(str(status) for status in statuses))
        if not allowed:
            raise ValueError("at least one receipt status is required")
        placeholders = ",".join("?" for _ in allowed)
        row = self.db.execute(
            "SELECT id, run_id, action_kind, action_id, "
            "idempotency_key, params_hash, status, body, created_at "
            "FROM receipts WHERE run_id=? AND action_kind=? "
            f"AND status IN ({placeholders}) "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (run_id, action_kind, *allowed),
        ).fetchone()
        return StoredReceipt.from_row(row) if row is not None else None

    def bodies_for_run_status(self, run_id: int, status: str) -> list[str]:
        rows = self.db.execute(
            "SELECT body FROM receipts WHERE run_id=? AND status=? ORDER BY id",
            (run_id, status),
        ).fetchall()
        return [str(row["body"]) for row in rows]

    def bodies_for_action_status(
        self,
        *,
        action_kind: str,
        status: str,
    ) -> list[str]:
        query = "SELECT body FROM receipts WHERE action_kind=? AND status=?"
        params: list[Any] = [action_kind, status]
        query += " ORDER BY created_at, rowid"
        rows = self.db.execute(query, params).fetchall()
        return [str(row["body"]) for row in rows]

    def insert(
        self,
        receipt: Receipt,
        *,
        edition_run_context: Mapping[str, Any] | None = None,
        commit: bool = True,
    ) -> None:
        self.db.execute(
            "INSERT INTO receipts (id, run_id, action_kind, action_id, "
            "idempotency_key, params_hash, status, body, edition_run_context) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                receipt.receipt_id,
                receipt.run_id,
                receipt.action_kind,
                receipt.action_id,
                receipt.idempotency_key,
                receipt.params_hash,
                receipt.status,
                receipt_body(receipt),
                edition_run_context_body(edition_run_context),
            ),
        )
        if commit:
            self.db.commit()

    def insert_with_status(
        self,
        receipt: Receipt,
        status: ReceiptStatus,
        *,
        edition_run_context: Mapping[str, Any] | None = None,
        commit: bool = True,
    ) -> None:
        if receipt.status != status:
            raise ValueError(
                f"receipt {receipt.receipt_id} status is {receipt.status!r}, "
                f"expected {status!r}"
            )
        self.insert(
            receipt,
            edition_run_context=edition_run_context,
            commit=commit,
        )

    def insert_queued(self, receipt: Receipt, *, commit: bool = True) -> None:
        self.insert_with_status(receipt, "queued", commit=commit)

    def insert_running(
        self,
        receipt: Receipt,
        *,
        edition_run_context: Mapping[str, Any] | None = None,
        commit: bool = True,
    ) -> None:
        self.insert_with_status(
            receipt,
            "running",
            edition_run_context=edition_run_context,
            commit=commit,
        )

    def insert_completed(
        self,
        receipt: Receipt,
        *,
        edition_run_context: Mapping[str, Any] | None = None,
        commit: bool = True,
    ) -> None:
        self.insert_with_status(
            receipt,
            "completed",
            edition_run_context=edition_run_context,
            commit=commit,
        )

    def insert_partial(self, receipt: Receipt, *, commit: bool = True) -> None:
        self.insert_with_status(receipt, "partial", commit=commit)

    def insert_failed(self, receipt: Receipt, *, commit: bool = True) -> None:
        self.insert_with_status(receipt, "failed", commit=commit)

    def insert_finished(self, receipt: Receipt, *, commit: bool = True) -> None:
        if receipt.status not in FINISHED_RECEIPT_STATUSES:
            raise ValueError(
                f"receipt {receipt.receipt_id} status is {receipt.status!r}, "
                "expected completed, partial, failed, or cancelled"
            )
        self.insert(receipt, commit=commit)

    def update_body_status(
        self,
        receipt: Receipt,
        *,
        require_status: str | None = None,
        commit: bool = True,
    ) -> bool:
        where_clause = "WHERE id=?"
        params: list[Any] = [
            receipt.run_id,
            receipt.action_kind,
            receipt.action_id,
            receipt.params_hash,
            receipt.status,
            receipt_body(receipt),
            receipt.receipt_id,
        ]
        if require_status is not None:
            where_clause += " AND status=?"
            params.append(require_status)
        updated = self.db.execute(
            "UPDATE receipts SET run_id=?, action_kind=?, action_id=?, "
            f"params_hash=?, status=?, body=? {where_clause}",
            params,
        )
        if commit:
            self.db.commit()
        return updated.rowcount == 1

    def update_if_status_in(
        self,
        receipt: Receipt,
        statuses: Iterable[str],
        *,
        commit: bool = True,
    ) -> bool:
        allowed = tuple(dict.fromkeys(str(status) for status in statuses))
        if not allowed:
            raise ValueError("at least one receipt status is required")
        placeholders = ",".join("?" for _ in allowed)
        updated = self.db.execute(
            "UPDATE receipts SET run_id=?, action_kind=?, action_id=?, "
            "params_hash=?, status=?, body=? "
            f"WHERE id=? AND status IN ({placeholders})",
            (
                receipt.run_id,
                receipt.action_kind,
                receipt.action_id,
                receipt.params_hash,
                receipt.status,
                receipt_body(receipt),
                receipt.receipt_id,
                *allowed,
            ),
        )
        if commit:
            self.db.commit()
        return updated.rowcount == 1

    def delete_if_status(
        self,
        receipt_id: str,
        status: str,
        *,
        commit: bool = True,
    ) -> bool:
        deleted = self.db.execute(
            "DELETE FROM receipts WHERE id=? AND status=?",
            (receipt_id, status),
        )
        if commit:
            self.db.commit()
        return deleted.rowcount == 1

    def delete_running(
        self,
        receipt_id: str,
        *,
        commit: bool = True,
    ) -> bool:
        return self.delete_if_status(receipt_id, "running", commit=commit)

    def delete_queued(
        self,
        receipt_id: str,
        *,
        commit: bool = True,
    ) -> bool:
        return self.delete_if_status(receipt_id, "queued", commit=commit)
