"""Generated column claim store for action-run output reservations."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from frisket.engine.store.transactions import require_transaction, write_transaction
from frisket.engine.store.writer_fence import WriterFence


def _stale_writer(message: str) -> Exception:
    from frisket.execution.attempt import StaleAttemptWriter

    return StaleAttemptWriter(message)


# A claim's owning run can reach a terminal status (project-local
# `runs.status`) even when the claim was acquired directly by a
# sync/direct-dispatch action that never had a queue job (job_id stays None).
# Queue/run terminality is recovery evidence only after the project-local
# dispatch authority is no longer live -- see `_owning_job_is_terminal`.
_RUN_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "partial"})


class ClaimLeaseRenewalFailed(sqlite3.OperationalError):
    """A transient claim-lease write failure, never authority loss."""


class GeneratedColumnClaimStore:
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
        change_id: int | None = None,
        job_id: int | None = None,
        mode: str = "replace",
        claim_token: str | None = None,
        lease_seconds: int | None = None,
        details: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> tuple[list[sqlite3.Row], sqlite3.Row | None]:
        """Acquire active claims for one output set in the caller's transaction."""
        require_transaction(self.db, "output claim acquisition")
        names = sorted({name.strip() for name in output_names if name.strip()})
        if not names:
            return [], None
        token = claim_token or f"claim:{uuid.uuid4()}"
        expiry = self.claim_expiry(lease_seconds, now=now)
        details_json = json.dumps(details or {}, sort_keys=True)
        savepoint = f"output_claim_acquire_{uuid.uuid4().hex}"
        self.db.execute(f"SAVEPOINT {savepoint}")
        try:
            for name in names:
                existing = self.db.execute(
                    "SELECT * FROM generated_column_claims "
                    "WHERE sheet_id=? AND output_name=? AND status='active'",
                    (sheet_id, name),
                ).fetchone()
                if existing is not None:
                    self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
                    return [], existing
            created: list[sqlite3.Row] = []
            for name in names:
                column = self.db.execute(
                    "SELECT column.id, "
                    "(SELECT MAX(head.run_id) FROM published_result_bases head "
                    " WHERE head.column_id=column.id) AS current_head_run_id "
                    "FROM columns column WHERE column.sheet_id=? AND column.name=? "
                    "AND column.active=1",
                    (sheet_id, name),
                ).fetchone()
                claim_id = f"claim:{uuid.uuid4()}"
                self.db.execute(
                    "INSERT INTO generated_column_claims ("
                    "id, sheet_id, column_id, output_name, run_id, change_id, "
                    "receipt_id, job_id, action_kind, mode, expected_current_run_id, "
                    "claim_token, lease_expires_at, details"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        claim_id,
                        sheet_id,
                        int(column["id"]) if column is not None else None,
                        name,
                        run_id,
                        change_id,
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
                        "SELECT * FROM generated_column_claims WHERE id=?", (claim_id,)
                    ).fetchone()
                )
            self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            return created, None
        except sqlite3.IntegrityError:
            self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            placeholders = ",".join("?" for _ in names)
            conflict = self.db.execute(
                "SELECT occ.* FROM generated_column_claims occ "
                "LEFT JOIN columns c ON c.id=occ.column_id AND c.active=1 "
                "WHERE occ.status='active' AND ("
                f"(occ.sheet_id=? AND occ.output_name IN ({placeholders})) "
                f"OR (c.sheet_id=? AND c.name IN ({placeholders}))"
                ") ORDER BY occ.created_at, occ.id LIMIT 1",
                (sheet_id, *names, sheet_id, *names),
            ).fetchone()
            return [], conflict
        except BaseException:
            if self.db.in_transaction:
                self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise

    def bind_to_run(
        self,
        *,
        claim_token: str,
        run_id: int,
        job_id: int | None = None,
        expected_output_names: list[str] | None = None,
    ) -> int:
        """Bind one active claim group to its run, all claims or none, in the
        caller's transaction."""
        require_transaction(self.db, "output claim binding")
        return self._bind_claims_to_run(
            claim_token=claim_token,
            run_id=run_id,
            job_id=job_id,
            expected_output_names=expected_output_names,
        )

    def _bind_claims_to_run(
        self,
        *,
        claim_token: str,
        run_id: int,
        job_id: int | None,
        expected_output_names: list[str] | None,
    ) -> int:
        run = self.db.execute(
            "SELECT change_id FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        if run is None:
            raise ValueError(f"cannot bind output claims to missing run {run_id}")
        change_id = int(run["change_id"])
        claims = self.db.execute(
            "SELECT id, sheet_id, output_name FROM generated_column_claims "
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
                "SELECT id FROM columns WHERE sheet_id=? AND name=? AND active=1",
                (claim["sheet_id"], claim["output_name"]),
            ).fetchone()
            if column is None:
                raise ValueError(
                    "prepared output claim has no materialized output column: "
                    f"{claim['output_name']!r}"
                )
            self.db.execute(
                "UPDATE generated_column_claims SET run_id=?, change_id=?, job_id=?, "
                "column_id=? WHERE id=?",
                (
                    run_id,
                    change_id,
                    job_id,
                    int(column["id"]),
                    claim["id"],
                ),
            )
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
            "FROM generated_column_claims c "
            "JOIN columns col ON col.id=c.column_id "
            "JOIN runs run ON run.id=c.run_id "
            "JOIN changes op ON op.id=run.change_id "
            "WHERE c.claim_token=? AND c.run_id=? AND c.status='active' "
            "AND col.name=c.output_name AND col.active=1 "
            "ORDER BY col.position, col.id",
            (claim_token, run_id),
        ).fetchall()
        active_count = int(
            self.db.execute(
                "SELECT COUNT(*) FROM generated_column_claims "
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
        now: datetime | None = None,
    ) -> int:
        """Renew one active claim group's lease in the caller's transaction."""
        require_transaction(self.db, "output claim renewal")
        return self.renew_in_transaction(
            self.db,
            claim_token=claim_token,
            lease_seconds=lease_seconds,
            run_id=run_id,
            now=now,
        )

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
            GeneratedColumnClaimStore.claim_expiry(
                lease_seconds,
                now=observed_now,
            ),
            claim_token,
        ]
        if run_id is not None:
            where += " AND run_id=?"
            args.append(run_id)
        renewed = db.execute(
            "UPDATE generated_column_claims SET renewed_at=?, lease_expires_at=? "
            f"WHERE {where}",
            args,
        )
        return int(renewed.rowcount)

    @staticmethod
    def _require_dispatching_attempt(
        db: sqlite3.Connection,
        fence: WriterFence,
    ) -> tuple[int, str]:
        run_id = fence.run_id
        writer_attempt_id = fence.writer_attempt_id
        if run_id is None:
            raise _stale_writer("the mutation supplied no run id")
        if not isinstance(writer_attempt_id, str) or not writer_attempt_id:
            raise _stale_writer("the mutation supplied no immutable writer attempt")
        attempt = db.execute(
            "SELECT run_id, state FROM execution_attempts WHERE id=?",
            (writer_attempt_id,),
        ).fetchone()
        if attempt is None:
            raise _stale_writer(
                f"attempt {writer_attempt_id!r} is missing; "
                "no writer owns this mutation"
            )
        attempt_run_id = attempt["run_id"]
        if attempt_run_id is None or int(attempt_run_id) != int(run_id):
            raise _stale_writer(
                f"attempt {writer_attempt_id!r} belongs to run "
                f"{attempt_run_id!r}, not run {run_id}"
            )
        if str(attempt["state"]) != "dispatching":
            raise _stale_writer(
                f"attempt {writer_attempt_id!r} is {attempt['state']!r}, "
                "not 'dispatching'"
            )
        return run_id, writer_attempt_id

    @staticmethod
    def require_current_writer(
        db: sqlite3.Connection,
        *,
        fence: WriterFence,
        output_column_ids: set[int] | frozenset[int] | None = None,
    ) -> str:
        """Fence one run-scoped mutation inside its caller's write transaction."""

        run_id, writer_attempt_id = (
            GeneratedColumnClaimStore._require_dispatching_attempt(db, fence)
        )
        claim_token = fence.claim_token
        claimless_direct_effect = fence.claimless_direct_effect

        if claimless_direct_effect:
            if output_column_ids:
                raise _stale_writer(
                    "a claimless direct writer cannot mutate output columns"
                )
            active_claim = db.execute(
                "SELECT claim_token FROM generated_column_claims "
                "WHERE run_id=? AND status='active' LIMIT 1",
                (run_id,),
            ).fetchone()
            if active_claim is not None:
                raise _stale_writer(
                    "a claimed output run cannot use claimless direct authority"
                )
            run = db.execute(
                "SELECT current_attempt_id FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if run is None or run["current_attempt_id"] != writer_attempt_id:
                holder = None if run is None else run["current_attempt_id"]
                raise _stale_writer(
                    "the claimless dispatch is no longer held by this writer "
                    f"(current holder {holder!r})"
                )
            return writer_attempt_id

        if not isinstance(claim_token, str) or not claim_token:
            raise _stale_writer("an output-producing mutation supplied no claim token")
        claims = db.execute(
            "SELECT column_id FROM generated_column_claims "
            "WHERE claim_token=? AND run_id=? AND status='active'",
            (claim_token, run_id),
        ).fetchall()
        if not claims:
            raise _stale_writer(
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
                raise _stale_writer(
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
    ) -> int:
        """Close attempts whose existing output claim lease has gone silent in
        the caller's transaction."""

        require_transaction(self.db, "stale-attempt sweep")
        has_claim = (
            self.db.execute(
                "SELECT 1 FROM generated_column_claims WHERE run_id=? "
                "AND status='active' LIMIT 1",
                (run_id,),
            ).fetchone()
            is not None
        )
        stopped_job = not has_claim and self._run_job_execution_stopped(run_id)
        cursor = self.db.execute(
            "UPDATE execution_attempts SET state='abandoned' "
            "WHERE run_id=? AND state='dispatching' "
            "AND ("
            "(? AND NOT EXISTS (SELECT 1 FROM generated_column_claims c "
            "WHERE c.run_id=execution_attempts.run_id AND c.status='active')) "
            "OR (created_at < ? AND EXISTS (SELECT 1 FROM generated_column_claims c "
            "WHERE c.run_id=execution_attempts.run_id AND c.status='active') "
            "AND NOT EXISTS (SELECT 1 FROM generated_column_claims live "
            "WHERE live.run_id=execution_attempts.run_id "
            "AND live.status='active' AND live.lease_expires_at > ?))"
            ")",
            (run_id, stopped_job, cutoff, lease_cutoff),
        )
        if cursor.rowcount:
            self.db.execute(
                "UPDATE runs SET current_attempt_id=NULL WHERE id=? AND "
                "current_attempt_id IN (SELECT id FROM execution_attempts "
                "WHERE run_id=? AND state='abandoned')",
                (run_id, run_id),
            )
        return int(cursor.rowcount or 0)

    def release(
        self,
        *,
        claim_token: str,
        status: str = "released",
    ) -> int:
        """Release one active claim group in the caller's transaction."""
        require_transaction(self.db, "output claim release")
        released = self.db.execute(
            "UPDATE generated_column_claims SET status=?, released_at=datetime('now') "
            "WHERE claim_token=? AND status='active'",
            (status, claim_token),
        )
        return released.rowcount

    def finish_current_writer(
        self,
        *,
        fence: WriterFence,
        attempt_state: str,
        claim_status: str = "released",
    ) -> int:
        """Close only the still-current writer and its own active claim group.

        Canonical action finalizers use this after their terminal receipt or
        retained checkpoint transaction. A replacement that won in between
        makes the fence refuse before either the attempt pointer or claims are
        touched.
        """

        from frisket.execution.attempt import set_attempt_state_in_transaction

        with write_transaction(self.db):
            writer_attempt_id = self.require_current_writer(self.db, fence=fence)
            set_attempt_state_in_transaction(
                self.project,
                writer_attempt_id,
                attempt_state,
            )
            released = (
                0
                if fence.claim_token is None
                else self.release(
                    claim_token=fence.claim_token,
                    status=claim_status,
                )
            )
            return int(released)

    def active_column_ids(
        self,
        *,
        run_id: int,
        claim_token: str,
    ) -> frozenset[int]:
        """Return the active output-column coverage for one run-bound token."""

        rows = self.db.execute(
            "SELECT column_id FROM generated_column_claims "
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
                "SELECT 1 FROM generated_column_claims "
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
    ) -> int:
        """Release active claims for one receipt in the caller's transaction."""
        require_transaction(self.db, "receipt output claim release")
        released = self.db.execute(
            "UPDATE generated_column_claims SET status=?, released_at=datetime('now') "
            "WHERE receipt_id=? AND status='active'",
            (status, receipt_id),
        )
        return released.rowcount

    def release_for_run_receipt(
        self,
        *,
        run_id: int,
        receipt_id: str,
        status: str,
    ) -> int:
        """Release claims by durable run/receipt identity, not token shape, in
        the caller's transaction.

        Recovery cannot trust a caller-supplied token, and claim tokens are
        intentionally opaque (including custom tokens). The run/receipt pair
        is the stable join shared by the terminal transaction.
        """

        require_transaction(self.db, "run receipt output claim release")
        released = self.db.execute(
            "UPDATE generated_column_claims SET status=?, released_at=datetime('now') "
            "WHERE run_id=? AND receipt_id=? AND status='active'",
            (status, run_id, receipt_id),
        )
        return released.rowcount

    def discard_for_receipt(
        self,
        *,
        receipt_id: str,
    ) -> int:
        """Delete an unobservable pre-execution lease in the caller's transaction.

        A confirmation refusal is not a failed attempt: no work was authorized
        and no durable action state may remain.  This is deliberately narrower
        than ``release_for_receipt`` so terminal attempts retain their audit row.
        """

        require_transaction(self.db, "receipt output claim discard")
        discarded = self.db.execute(
            "DELETE FROM generated_column_claims WHERE receipt_id=? AND status='active'",
            (receipt_id,),
        )
        return discarded.rowcount

    def active_for_columns(
        self, column_ids: list[int] | set[int]
    ) -> sqlite3.Row | None:
        """The generated_column_busy gate (edits.py/operations.py's
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
        (`release_stale(...)` joins the caller's transaction); it lands or rolls
        back with the rest of that transaction, same as every other write in
        this gate's callers."""
        ids = sorted({int(column_id) for column_id in column_ids})
        if not ids:
            return None
        placeholders = ",".join("?" for _ in ids)
        query = (
            "SELECT occ.* FROM generated_column_claims occ "
            "LEFT JOIN columns c ON c.sheet_id=occ.sheet_id "
            "AND c.name=occ.output_name AND c.active=1 "
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
        """The generated_column_busy gate for a TARGET name that may not exist as
        a column yet (the resolve.* transforms' pre-write check). Same
        stale-owner semantics as `active_for_columns`: a write caller releases
        terminal-owner claims under its transaction; a read-only caller can
        pass ``recover_stale=False`` to ignore them without mutating state."""
        query = (
            "SELECT * FROM generated_column_claims "
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

    @contextmanager
    def _run_queue(self):
        """Borrow the attached queue, or the same explicit/local queue fallback."""
        from frisket.execution.handler_lineage import current_handler_lineage

        handler = current_handler_lineage()
        queue = (
            self.queue
            or getattr(self.project, "_frisket_run_queue", None)
            or (handler.queue if handler is not None else None)
        )
        should_close = False
        try:
            if queue is None:
                path = getattr(self.project, "path", None)
                if path is None:
                    yield None
                    return
                from frisket.engine.jobs.queue import open_queue

                queue = open_queue(
                    workspace=path.parent,
                    database_url=os.environ.get("FRISKET_RUN_QUEUE_DATABASE_URL"),
                )
                should_close = True
            yield queue
        finally:
            if should_close and queue is not None:
                queue.close()

    def _queued_job_status(self, job_id: int) -> str | None:
        try:
            with self._run_queue() as queue:
                job = queue.get(job_id) if queue is not None else None
                return job.status if job is not None else None
        except Exception:
            return None

    def _run_job_execution_stopped(self, run_id: int) -> bool:
        original = self.db.execute(
            "SELECT id FROM execution_attempts WHERE run_id=? AND state='dispatching'",
            (run_id,),
        ).fetchone()
        return original is not None and self.attempt_handler_execution_stopped(
            original[0]
        )

    def attempt_handler_execution_stopped(self, attempt_id: str) -> bool:
        """Prove original handler exit, independently of attempt/job state.

        Recovery callers may inspect an already-abandoned attempt too; that
        state is not itself evidence that the original executable code stopped.
        """
        original = self.db.execute(
            "SELECT run_id,job_id,handler_authority_id FROM execution_attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
        if original is None:
            return False
        if (
            original["job_id"] is not None
            or original["handler_authority_id"] is not None
        ):
            if original["job_id"] is None or not original["handler_authority_id"]:
                return False
            try:
                with self._run_queue() as queue:
                    return queue is not None and queue.handler_execution_stopped(
                        int(original["job_id"]), str(original["handler_authority_id"])
                    )
            except Exception:
                return False
        # Older/non-worker attempts have no exact handler identity. Their
        # existing receipt association requires the stronger whole-job proof.
        run_id = original["run_id"]
        if run_id is None:
            return False
        job_ids = {
            int(row[0])
            for row in self.db.execute(
                "SELECT job_id FROM generated_column_claims WHERE run_id=? "
                "AND job_id IS NOT NULL UNION "
                "SELECT json_extract(e.value, '$.ref.job_id') "
                "FROM receipts r, json_each(r.body, '$.records') e "
                "WHERE r.run_id=? "
                "AND json_type(e.value, '$.ref.job_id')='integer' "
                "AND (json_extract(e.value, '$.ref.queue_kind')='project.run' "
                "OR json_extract(e.value, '$.ref.kind')='queued_action_job_enqueued')",
                (run_id, run_id),
            )
        }
        if not job_ids:
            return False
        try:
            with self._run_queue() as queue:
                return queue is not None and all(
                    queue.job_execution_stopped(job_id) for job_id in job_ids
                )
        except Exception:
            # Missing/unavailable scheduling evidence cannot revoke a writer.
            return False

    def release_stale(
        self,
        *,
        claim_id: str,
        reason: str,
    ) -> int:
        """The RECOVERY release (distinct from `release()`'s normal
        finalize-time release): stamps `details.recovered_by` in the caller's
        transaction so a recovered claim is auditable/queryable separately from
        an ordinary release."""
        require_transaction(self.db, "stale output claim release")
        row = self.db.execute(
            "SELECT details FROM generated_column_claims WHERE id=?", (claim_id,)
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
            "UPDATE generated_column_claims SET status='released', "
            "released_at=datetime('now'), details=? "
            "WHERE id=? AND status='active'",
            (json.dumps(details, sort_keys=True), claim_id),
        )
        return released.rowcount
