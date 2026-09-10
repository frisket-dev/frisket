"""Output column claim store for action-run output reservations."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any


# A claim's owning run can reach a terminal status (project-local
# `runs.status`) even when the claim was acquired directly by a
# sync/direct-dispatch action that never had a queue job (job_id stays None).
# Queue/run terminality is recovery evidence only after the project-local
# dispatch authority is no longer live -- see `_owning_job_is_terminal`.
_RUN_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "partial"})


class ClaimLeaseRenewalFailed(sqlite3.OperationalError):
    """A transient claim-lease write failure, never authority loss."""


class OutputColumnClaimStore:
    def __init__(self, project: Any, *, queue: Any | None = None):
        self.project = project
        self.queue = queue

    @property
    def db(self) -> sqlite3.Connection:
        """The CALLING thread's connection, resolved per access — see
        ``RunResultStore.db``: capturing ``project.db`` in ``__init__`` hands
        every thread the CONSTRUCTING thread's connection, which races
        sqlite3's statement cache."""
        return self.project.db

    @staticmethod
    def claim_expiry(
        lease_seconds: int | None,
        *,
        now: datetime | None = None,
    ) -> str | None:
        if lease_seconds is None:
            return None
        return (
            (now or datetime.now(timezone.utc))
            + timedelta(seconds=max(1, int(lease_seconds)))
        ).isoformat(timespec="microseconds")

    def acquire(
        self,
        *,
        sheet_id: int,
        output_names: list[str],
        action_kind: str,
        receipt_id: str | None = None,
        run_id: int | None = None,
        op_id: int | None = None,
        job_id: int | None = None,
        mode: str = "replace",
        claim_token: str | None = None,
        lease_seconds: int | None = None,
        details: dict[str, Any] | None = None,
        commit: bool = True,
        now: datetime | None = None,
    ) -> tuple[list[sqlite3.Row], sqlite3.Row | None]:
        names = sorted({name.strip() for name in output_names if name.strip()})
        if not names:
            return [], None
        token = claim_token or f"claim:{uuid.uuid4()}"
        expiry = self.claim_expiry(lease_seconds, now=now)
        details_json = json.dumps(details or {}, sort_keys=True)
        try:
            if commit:
                self.db.execute("BEGIN IMMEDIATE")
            elif not self.db.in_transaction:
                raise RuntimeError(
                    "OutputColumnClaimStore.acquire(commit=False) requires "
                    "a caller-owned transaction"
                )
            for name in names:
                existing = self.db.execute(
                    "SELECT * FROM output_column_claims "
                    "WHERE sheet_id=? AND output_name=? AND status='active'",
                    (sheet_id, name),
                ).fetchone()
                if existing is not None:
                    if commit:
                        self.db.rollback()
                    return [], existing
            created: list[sqlite3.Row] = []
            for name in names:
                column = self.db.execute(
                    "SELECT column.id, "
                    "(SELECT MAX(head.run_id) FROM cell_result_heads head "
                    " WHERE head.column_id=column.id) AS current_head_run_id "
                    "FROM columns column WHERE column.sheet_id=? AND column.name=?",
                    (sheet_id, name),
                ).fetchone()
                claim_id = f"claim:{uuid.uuid4()}"
                self.db.execute(
                    "INSERT INTO output_column_claims ("
                    "id, sheet_id, column_id, output_name, run_id, op_id, "
                    "receipt_id, job_id, action_kind, mode, expected_current_run_id, "
                    "claim_token, lease_expires_at, details"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        claim_id,
                        sheet_id,
                        int(column["id"]) if column is not None else None,
                        name,
                        run_id,
                        op_id,
                        receipt_id,
                        job_id,
                        action_kind,
                        mode,
                        (
                            int(column["current_head_run_id"])
                            if column is not None
                            and column["current_head_run_id"] is not None
                            else None
                        ),
                        token,
                        expiry,
                        details_json,
                    ),
                )
                created.append(
                    self.db.execute(
                        "SELECT * FROM output_column_claims WHERE id=?", (claim_id,)
                    ).fetchone()
                )
            if commit:
                self.db.commit()
            return created, None
        except sqlite3.IntegrityError:
            if commit:
                self.db.rollback()
            placeholders = ",".join("?" for _ in names)
            conflict = self.db.execute(
                "SELECT * FROM output_column_claims "
                f"WHERE sheet_id=? AND output_name IN ({placeholders}) "
                "AND status='active' ORDER BY created_at, id LIMIT 1",
                (sheet_id, *names),
            ).fetchone()
            return [], conflict
        except Exception:
            if commit:
                self.db.rollback()
            raise

    def bind_to_run(
        self,
        *,
        claim_token: str,
        run_id: int,
        job_id: int | None = None,
        expected_output_names: list[str] | None = None,
        commit: bool = True,
    ) -> int:
        run = self.db.execute("SELECT op_id FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise ValueError(f"cannot bind output claims to missing run {run_id}")
        op_id = int(run["op_id"])
        claims = self.db.execute(
            "SELECT id, sheet_id, output_name FROM output_column_claims "
            "WHERE claim_token=? AND status='active'",
            (claim_token,),
        ).fetchall()
        if expected_output_names is not None:
            expected = sorted(
                {
                    str(name).strip()
                    for name in expected_output_names
                    if str(name).strip()
                }
            )
            actual = sorted(str(claim["output_name"]) for claim in claims)
            if actual != expected:
                raise ValueError(
                    "active output claim group does not match the prepared "
                    f"output descriptors: expected={expected!r}, actual={actual!r}"
                )
        for claim in claims:
            column = self.db.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name=?",
                (claim["sheet_id"], claim["output_name"]),
            ).fetchone()
            if column is None:
                raise ValueError(
                    "prepared output claim has no materialized output column: "
                    f"{claim['output_name']!r}"
                )
            self.db.execute(
                "UPDATE output_column_claims SET run_id=?, op_id=?, job_id=?, "
                "column_id=? WHERE id=?",
                (
                    run_id,
                    op_id,
                    job_id,
                    int(column["id"]),
                    claim["id"],
                ),
            )
        if commit:
            self.db.commit()
        return len(claims)

    def active_output_fields(
        self,
        *,
        claim_token: str,
        run_id: int,
        require_frozen_descriptors: bool = False,
    ) -> list[dict[str, Any]]:
        """Reconstruct the durable output descriptors from one active group."""

        rows = self.db.execute(
            "SELECT c.output_name, c.details, col.id AS column_id, "
            "col.type AS column_type, "
            "col.semantic_type, col.format, op.undo_info "
            "FROM output_column_claims c "
            "JOIN columns col ON col.id=c.column_id "
            "JOIN runs run ON run.id=c.run_id "
            "JOIN ops op ON op.id=run.op_id "
            "WHERE c.claim_token=? AND c.run_id=? AND c.status='active' "
            "AND col.name=c.output_name "
            "ORDER BY col.position, col.id",
            (claim_token, run_id),
        ).fetchall()
        active_count = int(
            self.db.execute(
                "SELECT COUNT(*) FROM output_column_claims "
                "WHERE claim_token=? AND run_id=? AND status='active'",
                (claim_token, run_id),
            ).fetchone()[0]
        )
        if not rows or len(rows) != active_count:
            raise ValueError(
                f"claim token {claim_token!r} has no exact active output plan "
                f"bound to run {run_id}"
            )
        claimed_names = {str(row["output_name"]) for row in rows}
        frozen_plan: dict[str, dict[str, Any]] | None = None
        if require_frozen_descriptors:
            for row in rows:
                try:
                    details = json.loads(row["details"] or "{}")
                except (TypeError, ValueError):
                    details = {}
                declared_fields = details.get("output_fields")
                if not isinstance(declared_fields, list):
                    raise ValueError(
                        "generation-managed output claim has no frozen output "
                        "descriptors"
                    )
                plan = {
                    str(candidate.get("name")): dict(candidate)
                    for candidate in declared_fields
                    if isinstance(candidate, dict)
                    and isinstance(candidate.get("name"), str)
                    and isinstance(candidate.get("column_type"), str)
                }
                if len(plan) != len(declared_fields) or set(plan) != claimed_names:
                    raise ValueError(
                        "generation-managed output claim descriptor roles do not "
                        "match its exact output plan"
                    )
                if frozen_plan is None:
                    frozen_plan = plan
                elif frozen_plan != plan:
                    raise ValueError(
                        "generation-managed output claim carries inconsistent frozen "
                        "descriptors"
                    )
            if frozen_plan is None:  # pragma: no cover - nonempty rows above
                raise ValueError("generation-managed output claim has no output plan")
        fields: list[dict[str, Any]] = []
        for row in rows:
            field: dict[str, Any] = {
                "name": str(row["output_name"]),
                "column_type": str(row["column_type"]),
            }
            if row["semantic_type"] is not None:
                field["semantic_type"] = str(row["semantic_type"])
            if row["format"] is not None:
                field["format"] = row["format"]
            if frozen_plan is not None:
                declared = frozen_plan[field["name"]]
            else:
                try:
                    details = json.loads(row["details"] or "{}")
                except (TypeError, ValueError):
                    details = {}
                declared_fields = details.get("output_fields")
                declared = (
                    next(
                        (
                            candidate
                            for candidate in declared_fields
                            if isinstance(candidate, dict)
                            and candidate.get("name") == field["name"]
                        ),
                        None,
                    )
                    if isinstance(declared_fields, list)
                    else None
                )
            if declared is not None:
                physical = {
                    "column_type": field["column_type"],
                    "semantic_type": field.get("semantic_type"),
                    "format": field.get("format"),
                }
                mismatches = {
                    key for key, value in physical.items() if declared.get(key) != value
                }
                pending_descriptor_change = False
                if mismatches:
                    from frisket.engine.store.result_generations import (
                        ResultGenerationStore,
                    )

                    try:
                        operation_info = json.loads(row["undo_info"] or "{}")
                    except (TypeError, ValueError):
                        operation_info = {}
                    pending = ResultGenerationStore._pending_descriptor_changes(
                        operation_info if isinstance(operation_info, dict) else {}
                    )
                    change = pending.get(str(int(row["column_id"])))
                    pending_descriptor_change = change == {
                        "before": {
                            "type": physical["column_type"],
                            "format": physical["format"],
                            "semantic_type": physical["semantic_type"],
                        },
                        "after": {
                            "type": declared.get("column_type"),
                            "format": declared.get("format"),
                            "semantic_type": declared.get("semantic_type"),
                        },
                    }
                if mismatches and not pending_descriptor_change:
                    raise ValueError(
                        "claimed output descriptor no longer matches its "
                        f"materialized column: {field['name']!r}"
                    )
                field = (
                    dict(declared)
                    if frozen_plan is not None
                    else {
                        **dict(declared),
                        **field,
                    }
                )
            fields.append(field)
        return fields

    def renew(
        self,
        *,
        claim_token: str,
        lease_seconds: int,
        run_id: int | None = None,
        commit: bool = True,
        now: datetime | None = None,
    ) -> int:
        renewed = self.renew_in_transaction(
            self.db,
            claim_token=claim_token,
            lease_seconds=lease_seconds,
            run_id=run_id,
            now=now,
        )
        if commit:
            self.db.commit()
        return renewed

    @staticmethod
    def renew_in_transaction(
        db: sqlite3.Connection,
        *,
        claim_token: str,
        lease_seconds: int,
        run_id: int | None = None,
        now: datetime | None = None,
    ) -> int:
        observed_now = now or datetime.now(timezone.utc)
        where = "claim_token=? AND status='active'"
        args: list[Any] = [
            observed_now.isoformat(timespec="microseconds"),
            OutputColumnClaimStore.claim_expiry(
                lease_seconds,
                now=observed_now,
            ),
            claim_token,
        ]
        if run_id is not None:
            where += " AND run_id=?"
            args.append(run_id)
        renewed = db.execute(
            "UPDATE output_column_claims SET renewed_at=?, lease_expires_at=? "
            f"WHERE {where}",
            args,
        )
        return int(renewed.rowcount)

    @staticmethod
    def require_current_writer(
        db: sqlite3.Connection,
        *,
        run_id: int,
        writer_attempt_id: str,
        claim_token: str | None,
        output_column_ids: set[int] | frozenset[int] | None = None,
        claimless_direct_effect: bool = False,
    ) -> str:
        """Fence one run-scoped mutation inside its caller's write transaction."""

        from frisket.execution.attempt import StaleAttemptWriter

        if not isinstance(writer_attempt_id, str) or not writer_attempt_id:
            raise StaleAttemptWriter(
                "the mutation supplied no immutable writer attempt"
            )
        attempt = db.execute(
            "SELECT run_id, state FROM execution_attempts WHERE id=?",
            (writer_attempt_id,),
        ).fetchone()
        if attempt is None:
            raise StaleAttemptWriter(
                f"attempt {writer_attempt_id!r} is missing; "
                "no writer owns this mutation"
            )
        attempt_run_id = attempt["run_id"]
        if attempt_run_id is None or int(attempt_run_id) != int(run_id):
            raise StaleAttemptWriter(
                f"attempt {writer_attempt_id!r} belongs to run "
                f"{attempt_run_id!r}, not run {run_id}"
            )
        if str(attempt["state"]) != "dispatching":
            raise StaleAttemptWriter(
                f"attempt {writer_attempt_id!r} is {attempt['state']!r}, "
                "not 'dispatching'"
            )

        if claimless_direct_effect:
            if output_column_ids:
                raise StaleAttemptWriter(
                    "a claimless direct writer cannot mutate output columns"
                )
            active_claim = db.execute(
                "SELECT claim_token FROM output_column_claims "
                "WHERE run_id=? AND status='active' LIMIT 1",
                (run_id,),
            ).fetchone()
            if active_claim is not None:
                raise StaleAttemptWriter(
                    "a claimed output run cannot use claimless direct authority"
                )
            run = db.execute(
                "SELECT current_attempt_id FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if run is None or run["current_attempt_id"] != writer_attempt_id:
                holder = None if run is None else run["current_attempt_id"]
                raise StaleAttemptWriter(
                    "the claimless dispatch is no longer held by this writer "
                    f"(current holder {holder!r})"
                )
            return writer_attempt_id

        if not isinstance(claim_token, str) or not claim_token:
            raise StaleAttemptWriter(
                "an output-producing mutation supplied no claim token"
            )
        claims = db.execute(
            "SELECT column_id FROM output_column_claims "
            "WHERE claim_token=? AND run_id=? AND status='active'",
            (claim_token, run_id),
        ).fetchall()
        if not claims:
            raise StaleAttemptWriter(
                f"claim token {claim_token!r} has no active claims "
                f"bound to run {run_id}"
            )
        required_columns = set(output_column_ids or ())
        if required_columns:
            claimed_columns = {
                int(row["column_id"]) for row in claims if row["column_id"] is not None
            }
            missing = sorted(required_columns - claimed_columns)
            if missing:
                raise StaleAttemptWriter(
                    f"claim token {claim_token!r} does not cover "
                    f"output columns {missing}"
                )
        return writer_attempt_id

    def abandon_stale_dispatching_attempts(
        self,
        *,
        run_id: int,
        cutoff: str,
        lease_cutoff: str,
        commit: bool,
    ) -> int:
        """Close attempts whose existing output claim lease has gone silent."""

        if commit:
            self.db.execute("BEGIN IMMEDIATE")
        elif not self.db.in_transaction:
            raise RuntimeError(
                "stale-attempt sweep with commit=False requires "
                "a caller-owned transaction"
            )
        try:
            cursor = self.db.execute(
                "UPDATE execution_attempts SET state='abandoned' "
                "WHERE run_id=? AND state='dispatching' AND created_at < ? "
                "AND ("
                "NOT EXISTS (SELECT 1 FROM output_column_claims c "
                "WHERE c.run_id=execution_attempts.run_id "
                "AND c.status='active') "
                "OR NOT EXISTS (SELECT 1 FROM output_column_claims live "
                "WHERE live.run_id=execution_attempts.run_id "
                "AND live.status='active' AND live.lease_expires_at > ?)"
                ")",
                (run_id, cutoff, lease_cutoff),
            )
            if cursor.rowcount:
                self.db.execute(
                    "UPDATE runs SET current_attempt_id=NULL WHERE id=? AND "
                    "current_attempt_id IN (SELECT id FROM execution_attempts "
                    "WHERE run_id=? AND state='abandoned')",
                    (run_id, run_id),
                )
            if commit:
                self.db.commit()
            return int(cursor.rowcount or 0)
        except BaseException:
            if commit:
                self.db.rollback()
            raise

    def release(
        self,
        *,
        claim_token: str,
        status: str = "released",
        commit: bool = True,
    ) -> int:
        released = self.db.execute(
            "UPDATE output_column_claims SET status=?, released_at=datetime('now') "
            "WHERE claim_token=? AND status='active'",
            (status, claim_token),
        )
        if commit:
            self.db.commit()
        return released.rowcount

    def finish_current_writer(
        self,
        *,
        run_id: int,
        writer_attempt_id: str,
        claim_token: str | None,
        attempt_state: str,
        claim_status: str = "released",
        claimless_direct_effect: bool = False,
    ) -> int:
        """Close only the still-current writer and its own active claim group.

        Canonical action finalizers use this after their terminal receipt or
        retained checkpoint transaction. A replacement that won in between
        makes the fence refuse before either the attempt pointer or claims are
        touched.
        """

        from frisket.execution.attempt import set_attempt_state

        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.require_current_writer(
                self.db,
                run_id=run_id,
                writer_attempt_id=writer_attempt_id,
                claim_token=claim_token,
                claimless_direct_effect=claimless_direct_effect,
            )
            set_attempt_state(
                self.project,
                writer_attempt_id,
                attempt_state,
                commit=False,
            )
            released = (
                0
                if claim_token is None
                else self.release(
                    claim_token=claim_token,
                    status=claim_status,
                    commit=False,
                )
            )
            self.db.commit()
            return int(released)
        except BaseException:
            self.db.rollback()
            raise

    def bind_job_to_receipt(
        self,
        *,
        receipt_id: str,
        job_id: int,
        commit: bool = True,
    ) -> int:
        """Point every active claim for a receipt at the async job that owns it."""
        bound = self.db.execute(
            "UPDATE output_column_claims SET job_id=? "
            "WHERE receipt_id=? AND status='active'",
            (job_id, receipt_id),
        )
        if commit:
            self.db.commit()
        return bound.rowcount

    def active_column_ids(
        self,
        *,
        run_id: int,
        claim_token: str,
    ) -> frozenset[int]:
        """Return the active output-column coverage for one run-bound token."""

        rows = self.db.execute(
            "SELECT column_id FROM output_column_claims "
            "WHERE claim_token=? AND run_id=? AND status='active'",
            (claim_token, run_id),
        ).fetchall()
        return frozenset(
            int(row["column_id"]) for row in rows if row["column_id"] is not None
        )

    def has_active_claims(self, *, run_id: int) -> bool:
        """Whether a run currently owns any active output-column claim."""

        return (
            self.db.execute(
                "SELECT 1 FROM output_column_claims "
                "WHERE run_id=? AND status='active' LIMIT 1",
                (run_id,),
            ).fetchone()
            is not None
        )

    def release_for_receipt(
        self,
        *,
        receipt_id: str,
        status: str,
        commit: bool = True,
    ) -> int:
        released = self.db.execute(
            "UPDATE output_column_claims SET status=?, released_at=datetime('now') "
            "WHERE receipt_id=? AND status='active'",
            (status, receipt_id),
        )
        if commit:
            self.db.commit()
        return released.rowcount

    def release_for_run_receipt(
        self,
        *,
        run_id: int,
        receipt_id: str,
        status: str,
        commit: bool = True,
    ) -> int:
        """Release claims by durable run/receipt identity, not token shape.

        Recovery cannot trust a caller-supplied token, and claim tokens are
        intentionally opaque (including custom tokens). The run/receipt pair
        is the stable join shared by the terminal transaction.
        """

        released = self.db.execute(
            "UPDATE output_column_claims SET status=?, released_at=datetime('now') "
            "WHERE run_id=? AND receipt_id=? AND status='active'",
            (status, run_id, receipt_id),
        )
        if commit:
            self.db.commit()
        return released.rowcount

    def discard_for_receipt(
        self,
        *,
        receipt_id: str,
        commit: bool = True,
    ) -> int:
        """Delete an unobservable pre-execution lease.

        A confirmation refusal is not a failed attempt: no work was authorized
        and no durable action state may remain.  This is deliberately narrower
        than ``release_for_receipt`` so terminal attempts retain their audit row.
        """

        discarded = self.db.execute(
            "DELETE FROM output_column_claims WHERE receipt_id=? AND status='active'",
            (receipt_id,),
        )
        if commit:
            self.db.commit()
        return discarded.rowcount

    def release_for_claim_and_receipt(
        self,
        *,
        claim_token: str,
        receipt_id: str,
        status: str,
        commit: bool = True,
    ) -> int:
        released = self.db.execute(
            "UPDATE output_column_claims SET status=?, released_at=datetime('now') "
            "WHERE claim_token=? AND receipt_id=? AND status='active'",
            (status, claim_token, receipt_id),
        )
        if commit:
            self.db.commit()
        return released.rowcount

    def active_for_columns(
        self, column_ids: list[int] | set[int]
    ) -> sqlite3.Row | None:
        """The output_column_busy gate (edits.py/operations.py's
        _claimed_column_error / _operation_claimed_column_error both call
        this). RECOVERY (queued-run-claim-release-v1): a claim normally dies
        with its owning action's finalize step, but that release is a
        SEPARATE, non-atomic commit from the run/job reaching a terminal
        status -- a worker crash/restart in the window between them (or any
        other silent finalize miss) leaves the claim 'active' forever with
        nothing to revisit it, and the busy error already advertises
        `requires_recovery: true` for exactly this. So: skip past (and, in
        the same breath, release) any candidate claim whose owner has
        already gone terminal, instead of reporting it as busy. This runs
        inside the caller's own BEGIN IMMEDIATE (edits.py/operations.py's
        in-txn perform core) -- the release is NOT committed here
        (`release_stale(..., commit=False)`); it lands or rolls back with
        the rest of that transaction, same as every other write in this
        gate's callers."""
        ids = sorted({int(column_id) for column_id in column_ids})
        if not ids:
            return None
        placeholders = ",".join("?" for _ in ids)
        query = (
            "SELECT occ.* FROM output_column_claims occ "
            "LEFT JOIN columns c ON c.sheet_id=occ.sheet_id "
            "AND c.name=occ.output_name "
            "WHERE occ.status='active' "
            f"AND (occ.column_id IN ({placeholders}) "
            f"OR c.id IN ({placeholders})) "
            "ORDER BY occ.created_at, occ.id LIMIT 1"
        )
        while True:
            claim = self.db.execute(query, (*ids, *ids)).fetchone()
            if claim is None:
                return None
            if not self._owning_job_is_terminal(claim):
                return claim
            self.release_stale(
                claim_id=claim["id"],
                reason="stale_claim_recovery: owning job terminal",
                commit=False,
            )
            # Loop: another (genuinely active) claim may still block these
            # columns once the stale one is out of the way.

    def active_for_output_name(
        self,
        *,
        sheet_id: int,
        output_name: str,
        recover_stale: bool = True,
    ) -> sqlite3.Row | None:
        """The output_column_busy gate for a TARGET name that may not exist as
        a column yet (the resolve.* transforms' pre-write check). Same
        stale-owner semantics as `active_for_columns`: a write caller releases
        terminal-owner claims under its transaction; a read-only caller can
        pass ``recover_stale=False`` to ignore them without mutating state."""
        query = (
            "SELECT * FROM output_column_claims "
            "WHERE sheet_id=? AND output_name=? AND status='active' "
            "ORDER BY created_at, id LIMIT 1"
        )
        while True:
            claim = self.db.execute(query, (int(sheet_id), output_name)).fetchone()
            if claim is None:
                return None
            if not self._owning_job_is_terminal(claim):
                return claim
            if not recover_stale:
                return None
            self.release_stale(
                claim_id=claim["id"],
                reason="stale_claim_recovery: owning job terminal",
                commit=False,
            )

    def _owning_job_is_terminal(self, claim: sqlite3.Row) -> bool:
        """Whether terminal owner evidence makes this claim safe to recover.

        The queue row is a projection of scheduling, not writer liveness. A
        queue lease/renewal failure can exhaust the job while the exact
        project-local attempt remains ``dispatching`` under its output-claim
        lease. Releasing in that state lets an unrelated editor mutate the
        output while the writer can still pass every effect-site fence.

        The stale-attempt sweep serializes claim-lease expiry with changing
        the attempt state and clearing ``runs.current_attempt_id``. Therefore
        a still-current dispatching attempt always wins over terminal queue or
        run status here; only after recovery closes that authority may those
        terminal projections release a missed-finalization claim.
        """
        run_id = claim["run_id"]
        if run_id is not None:
            live = self.db.execute(
                "SELECT 1 FROM runs r "
                "JOIN execution_attempts a ON a.id=r.current_attempt_id "
                "WHERE r.id=? AND a.run_id=r.id AND a.state='dispatching'",
                (run_id,),
            ).fetchone()
            if live is not None:
                return False

        job_id = claim["job_id"]
        if job_id is not None:
            status = self._queued_job_status(int(job_id))
            if status is not None:
                from frisket.engine.jobs.queue import TERMINAL as _JOB_TERMINAL_STATUSES

                return status in _JOB_TERMINAL_STATUSES
            # Job row unreadable (queue db missing/unreachable): fall through
            # to the run-status signal below rather than assume terminal.
        if run_id is not None:
            row = self.db.execute(
                "SELECT status FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if row is not None:
                return str(row["status"]) in _RUN_TERMINAL_STATUSES
        return False

    def _queued_job_status(self, job_id: int) -> str | None:
        """Read the owning job's status from the active run queue.

        Local source checkouts use the sibling `<workspace>/.queue.db`; hosted
        workspaces attach their shared app.state.run_queue to the Project, or
        pass it directly here. The explicit `FRISKET_RUN_QUEUE_DATABASE_URL`
        fallback is for non-Workspace callers; never infer the queue from the
        hosted control-plane `FRISKET_DATABASE_URL`.
        """
        path = getattr(self.project, "path", None)
        if path is None:
            return None
        queue = self.queue or getattr(self.project, "_frisket_run_queue", None)
        should_close = False
        from frisket.engine.jobs.queue import open_queue

        try:
            if queue is None:
                queue = open_queue(
                    workspace=path.parent,
                    database_url=os.environ.get("FRISKET_RUN_QUEUE_DATABASE_URL"),
                )
                should_close = True
            job = queue.get(job_id)
        except Exception:
            return None
        finally:
            if should_close and queue is not None:
                try:
                    queue.close()
                except Exception:
                    pass
        return job.status if job is not None else None

    def release_stale(
        self,
        *,
        claim_id: str,
        reason: str,
        commit: bool = True,
    ) -> int:
        """The RECOVERY release (distinct from `release()`'s normal
        finalize-time release): stamps `details.recovered_by` so a recovered
        claim is auditable/queryable separately from an ordinary release."""
        row = self.db.execute(
            "SELECT details FROM output_column_claims WHERE id=?", (claim_id,)
        ).fetchone()
        if row is None:
            return 0
        try:
            details = json.loads(row["details"] or "{}")
        except (TypeError, ValueError):
            details = {}
        if not isinstance(details, dict):
            details = {}
        details["recovered_by"] = reason
        released = self.db.execute(
            "UPDATE output_column_claims SET status='released', "
            "released_at=datetime('now'), details=? "
            "WHERE id=? AND status='active'",
            (json.dumps(details, sort_keys=True), claim_id),
        )
        if commit:
            self.db.commit()
        return released.rowcount
