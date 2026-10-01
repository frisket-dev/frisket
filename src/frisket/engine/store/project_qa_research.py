"""Durable continuation state and exact budget accounting for Ask research."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any


RESEARCH_STATES = frozenset({"running", "paused", "interrupted", "completed"})
WRITE_MODES = frozenset({"ask_each", "ask_overwrite", "full_access"})
OPERATION_KINDS = frozenset({"model", "action", "search"})
_UNSET = object()


class ProjectQAResearchStoreError(RuntimeError):
    """Base error for durable research state."""


class ProjectQAResearchNotFound(ProjectQAResearchStoreError, LookupError):
    """The requested research run or operation does not exist."""


class ProjectQAResearchConflictError(ProjectQAResearchStoreError):
    """A stale revision or non-equivalent idempotent replay was refused."""


class ProjectQAResearchUnknownCost(ProjectQAResearchStoreError):
    """An operation without an exact fixed estimate cannot be admitted."""


class ProjectQAResearchBudgetExceeded(ProjectQAResearchStoreError):
    """The requested reservation does not fit in the run budget."""

    def __init__(self, *, needed_micros: int, remaining_micros: int) -> None:
        self.needed_micros = needed_micros
        self.remaining_micros = remaining_micros
        super().__init__(
            f"operation needs {needed_micros} micros; "
            f"only {remaining_micros} micros remain"
        )


class ProjectQAResearchTurnLimitReached(ProjectQAResearchStoreError):
    """A new model request would exceed the configured turn limit."""

    def __init__(self, *, max_turns: int, turn_count: int) -> None:
        self.max_turns = max_turns
        self.turn_count = turn_count
        super().__init__(f"model turn limit {max_turns} has been reached")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id() -> str:
    return f"research_{uuid.uuid4().hex}"


def _nonnegative_int(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _positive_int_or_none(value: Any, *, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer or null")
    return value


def _nonempty(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _choice(value: Any, *, name: str, choices: frozenset[str]) -> str:
    text = _nonempty(value, name=name)
    if text not in choices:
        raise ValueError(f"unsupported {name}: {text}")
    return text


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _list(value: Any, *, name: str) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{name} must be a list")
    return list(value)


def _mapping(value: Any, *, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return dict(value)


class ProjectQAResearchStore:
    """Transactional store over a project's thread-owned SQLite connection."""

    def __init__(self, project: Any) -> None:
        self._project = project

    @property
    def db(self) -> sqlite3.Connection:
        return self._project.db

    def _write(self, write: Callable[[sqlite3.Connection], Any]) -> Any:
        db = self.db
        if db.in_transaction:
            return write(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            result = write(db)
            db.commit()
            return result
        except BaseException:
            db.rollback()
            raise

    @staticmethod
    def _run_record(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            raise ProjectQAResearchNotFound("research run not found")
        return {
            "id": row["id"],
            "turn_id": row["turn_id"],
            "actor": row["actor"],
            "state": row["state"],
            "budget_micros": row["budget_micros"],
            "currency": row["currency"],
            "write_mode": row["write_mode"],
            "max_turns": row["max_turns"],
            "turn_count": row["turn_count"],
            "skills": json.loads(row["skills_json"]),
            "saved_messages": json.loads(row["saved_messages_json"]),
            "pending_approval": (
                json.loads(row["pending_approval_json"])
                if row["pending_approval_json"] is not None
                else None
            ),
            "output_grants": json.loads(row["output_grants_json"]),
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _operation_record(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            raise ProjectQAResearchNotFound("research operation not found")
        return {
            "research_id": row["research_id"],
            "operation_id": row["operation_id"],
            "payload_identity": row["payload_identity"],
            "operation_kind": row["operation_kind"],
            "estimate_micros": row["estimate_micros"],
            "actual_micros": row["actual_micros"],
            "metadata": json.loads(row["metadata_json"]),
            "created_at": row["created_at"],
            "settled_at": row["settled_at"],
        }

    def get(self, research_id: str) -> dict[str, Any]:
        return self._run_record(
            self.db.execute(
                "SELECT * FROM project_qa_research_runs WHERE id=?", (research_id,)
            ).fetchone()
        )

    def get_for_turn(self, turn_id: str) -> dict[str, Any]:
        return self._run_record(
            self.db.execute(
                "SELECT * FROM project_qa_research_runs WHERE turn_id=?", (turn_id,)
            ).fetchone()
        )

    def create(
        self,
        *,
        turn_id: str,
        actor: str | None,
        budget_micros: int,
        currency: str,
        write_mode: str,
        max_turns: int | None,
        skills: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        turn_id = _nonempty(turn_id, name="turn_id")
        if actor is not None:
            actor = _nonempty(actor, name="actor")
        budget_micros = _nonnegative_int(budget_micros, name="budget_micros")
        currency = _nonempty(currency, name="currency").upper()
        if currency != "USD":
            raise ValueError("Research budgets currently use USD.")
        write_mode = _choice(write_mode, name="write_mode", choices=WRITE_MODES)
        max_turns = _positive_int_or_none(max_turns, name="max_turns")
        skills_json = _json([_mapping(skill, name="skill") for skill in skills])

        def write(db: sqlite3.Connection) -> dict[str, Any]:
            existing = db.execute(
                "SELECT * FROM project_qa_research_runs WHERE turn_id=?", (turn_id,)
            ).fetchone()
            if existing is not None:
                equivalent = (
                    existing["actor"] == actor
                    and existing["budget_micros"] == budget_micros
                    and existing["currency"] == currency
                    and existing["write_mode"] == write_mode
                    and existing["max_turns"] == max_turns
                    and existing["skills_json"] == skills_json
                )
                if not equivalent:
                    raise ProjectQAResearchConflictError(
                        "turn already has different research settings"
                    )
                return self._run_record(existing)
            now = _now()
            research_id = _id()
            db.execute(
                "INSERT INTO project_qa_research_runs "
                "(id,turn_id,actor,state,budget_micros,currency,write_mode,max_turns,"
                "turn_count,skills_json,saved_messages_json,pending_approval_json,output_grants_json,"
                "revision,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,0,?,'[]',NULL,'[]',1,?,?)",
                (
                    research_id,
                    turn_id,
                    actor,
                    "running",
                    budget_micros,
                    currency,
                    write_mode,
                    max_turns,
                    skills_json,
                    now,
                    now,
                ),
            )
            return self._run_record(
                db.execute(
                    "SELECT * FROM project_qa_research_runs WHERE id=?", (research_id,)
                ).fetchone()
            )

        return self._write(write)

    def update(
        self,
        research_id: str,
        *,
        expected_revision: int,
        state: str | object = _UNSET,
        budget_micros: int | object = _UNSET,
        write_mode: str | object = _UNSET,
        max_turns: int | None | object = _UNSET,
        saved_messages: Sequence[Any] | object = _UNSET,
        pending_approval: Mapping[str, Any] | None | object = _UNSET,
        output_grants: Sequence[Any] | object = _UNSET,
    ) -> dict[str, Any]:
        research_id = _nonempty(research_id, name="research_id")
        if isinstance(expected_revision, bool) or not isinstance(
            expected_revision, int
        ):
            raise ValueError("expected_revision must be an integer")
        values: dict[str, Any] = {}
        if state is not _UNSET:
            values["state"] = _choice(state, name="state", choices=RESEARCH_STATES)
        if budget_micros is not _UNSET:
            values["budget_micros"] = _nonnegative_int(
                budget_micros, name="budget_micros"
            )
        if write_mode is not _UNSET:
            values["write_mode"] = _choice(
                write_mode, name="write_mode", choices=WRITE_MODES
            )
        if max_turns is not _UNSET:
            values["max_turns"] = _positive_int_or_none(max_turns, name="max_turns")
        if saved_messages is not _UNSET:
            values["saved_messages_json"] = _json(
                _list(saved_messages, name="saved_messages")
            )
        if pending_approval is not _UNSET:
            values["pending_approval_json"] = (
                None
                if pending_approval is None
                else _json(_mapping(pending_approval, name="pending_approval"))
            )
        if output_grants is not _UNSET:
            values["output_grants_json"] = _json(
                _list(output_grants, name="output_grants")
            )

        def write(db: sqlite3.Connection) -> dict[str, Any]:
            existing = db.execute(
                "SELECT revision,turn_count FROM project_qa_research_runs WHERE id=?",
                (research_id,),
            ).fetchone()
            if existing is None:
                raise ProjectQAResearchNotFound("research run not found")
            if existing["revision"] != expected_revision:
                raise ProjectQAResearchConflictError("research revision changed")
            if "budget_micros" in values:
                committed = db.execute(
                    "SELECT COALESCE(SUM(COALESCE(actual_micros,estimate_micros)),0) "
                    "FROM project_qa_research_operations WHERE research_id=?",
                    (research_id,),
                ).fetchone()[0]
                if values["budget_micros"] < committed:
                    raise ValueError(
                        "The total budget cannot be less than spent and committed costs."
                    )
            if (
                values.get("max_turns") is not None
                and values["max_turns"] < existing["turn_count"]
            ):
                raise ValueError(
                    "The turn limit cannot be less than turns already used."
                )
            assignments = [f"{column}=?" for column in values]
            arguments = list(values.values())
            assignments.extend(["revision=revision+1", "updated_at=?"])
            arguments.extend([_now(), research_id, expected_revision])
            changed = db.execute(
                f"UPDATE project_qa_research_runs SET {','.join(assignments)} "
                "WHERE id=? AND revision=?",
                arguments,
            )
            if changed.rowcount != 1:
                raise ProjectQAResearchConflictError("research revision changed")
            return self._run_record(
                db.execute(
                    "SELECT * FROM project_qa_research_runs WHERE id=?", (research_id,)
                ).fetchone()
            )

        return self._write(write)

    def get_operation(self, research_id: str, operation_id: str) -> dict[str, Any]:
        return self._operation_record(
            self.db.execute(
                "SELECT * FROM project_qa_research_operations "
                "WHERE research_id=? AND operation_id=?",
                (research_id, operation_id),
            ).fetchone()
        )

    def interrupt_abandoned(self) -> None:
        """Follow parent reconciliation; never recover or dispatch saved work."""
        self._write(
            lambda db: db.execute(
                "UPDATE project_qa_research_runs SET state='interrupted',revision=revision+1,updated_at=? "
                "WHERE state IN ('running','paused') AND turn_id IN "
                "(SELECT id FROM project_qa_turns WHERE status NOT IN ('running','stopping'))",
                (_now(),),
            )
        )

    def finish(self, research_id: str, *, completed: bool) -> None:
        """The live owner terminalizes without racing approval revision changes."""
        self._write(
            lambda db: db.execute(
                "UPDATE project_qa_research_runs SET state=?,pending_approval_json=NULL,"
                "revision=revision+1,updated_at=? WHERE id=? AND state IN ('running','paused')",
                ("completed" if completed else "interrupted", _now(), research_id),
            )
        )

    def operations(self, research_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM project_qa_research_operations WHERE research_id=? "
            "ORDER BY created_at, operation_id",
            (research_id,),
        ).fetchall()
        return [self._operation_record(row) for row in rows]

    def admit_operation(
        self,
        research_id: str,
        *,
        operation_id: str,
        payload_identity: str,
        operation_kind: str,
        estimate_micros: int | None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        research_id = _nonempty(research_id, name="research_id")
        operation_id = _nonempty(operation_id, name="operation_id")
        payload_identity = _nonempty(payload_identity, name="payload_identity")
        operation_kind = _choice(
            operation_kind, name="operation_kind", choices=OPERATION_KINDS
        )
        if estimate_micros is None:
            raise ProjectQAResearchUnknownCost("operation estimate is unknown")
        estimate_micros = _nonnegative_int(estimate_micros, name="estimate_micros")
        metadata_json = _json(
            {} if metadata is None else _mapping(metadata, name="metadata")
        )

        def write(db: sqlite3.Connection) -> dict[str, Any]:
            existing = db.execute(
                "SELECT * FROM project_qa_research_operations "
                "WHERE research_id=? AND operation_id=?",
                (research_id, operation_id),
            ).fetchone()
            if existing is not None:
                if existing["payload_identity"] != payload_identity:
                    raise ProjectQAResearchConflictError(
                        "operation id is bound to a different payload"
                    )
                if (
                    existing["operation_kind"] != operation_kind
                    or existing["estimate_micros"] != estimate_micros
                    or existing["metadata_json"] != metadata_json
                ):
                    raise ProjectQAResearchConflictError(
                        "operation replay changed its accounting facts"
                    )
                return self._operation_record(existing)
            run = db.execute(
                "SELECT r.budget_micros,r.max_turns,r.turn_count,r.state,t.status "
                "FROM project_qa_research_runs r JOIN project_qa_turns t ON t.id=r.turn_id WHERE r.id=?",
                (research_id,),
            ).fetchone()
            if run is None:
                raise ProjectQAResearchNotFound("research run not found")
            if run["state"] != "running" or run["status"] != "running":
                raise ProjectQAResearchConflictError("Research is not running.")
            if (
                operation_kind == "model"
                and run["max_turns"] is not None
                and run["turn_count"] >= run["max_turns"]
            ):
                raise ProjectQAResearchTurnLimitReached(
                    max_turns=run["max_turns"], turn_count=run["turn_count"]
                )
            committed = db.execute(
                "SELECT COALESCE(SUM(CASE WHEN actual_micros IS NULL "
                "THEN estimate_micros ELSE actual_micros END),0) "
                "FROM project_qa_research_operations WHERE research_id=?",
                (research_id,),
            ).fetchone()[0]
            remaining = run["budget_micros"] - committed
            if estimate_micros > remaining:
                raise ProjectQAResearchBudgetExceeded(
                    needed_micros=estimate_micros,
                    remaining_micros=remaining,
                )
            now = _now()
            db.execute(
                "INSERT INTO project_qa_research_operations "
                "(research_id,operation_id,payload_identity,operation_kind,"
                "estimate_micros,actual_micros,metadata_json,created_at,settled_at) "
                "VALUES (?,?,?,?,?,NULL,?,?,NULL)",
                (
                    research_id,
                    operation_id,
                    payload_identity,
                    operation_kind,
                    estimate_micros,
                    metadata_json,
                    now,
                ),
            )
            if operation_kind == "model":
                db.execute(
                    "UPDATE project_qa_research_runs "
                    "SET turn_count=turn_count+1,updated_at=? WHERE id=?",
                    (now, research_id),
                )
            return self._operation_record(
                db.execute(
                    "SELECT * FROM project_qa_research_operations "
                    "WHERE research_id=? AND operation_id=?",
                    (research_id, operation_id),
                ).fetchone()
            )

        return self._write(write)

    def settle_operation(
        self,
        research_id: str,
        *,
        operation_id: str,
        payload_identity: str,
        actual_micros: int,
    ) -> dict[str, Any]:
        research_id = _nonempty(research_id, name="research_id")
        operation_id = _nonempty(operation_id, name="operation_id")
        payload_identity = _nonempty(payload_identity, name="payload_identity")
        actual_micros = _nonnegative_int(actual_micros, name="actual_micros")

        def write(db: sqlite3.Connection) -> dict[str, Any]:
            existing = db.execute(
                "SELECT * FROM project_qa_research_operations "
                "WHERE research_id=? AND operation_id=?",
                (research_id, operation_id),
            ).fetchone()
            if existing is None:
                raise ProjectQAResearchNotFound("research operation not found")
            if existing["payload_identity"] != payload_identity:
                raise ProjectQAResearchConflictError(
                    "operation id is bound to a different payload"
                )
            if existing["actual_micros"] is not None:
                if existing["actual_micros"] != actual_micros:
                    raise ProjectQAResearchConflictError(
                        "operation was already settled at a different amount"
                    )
                return self._operation_record(existing)
            db.execute(
                "UPDATE project_qa_research_operations "
                "SET actual_micros=?,settled_at=? "
                "WHERE research_id=? AND operation_id=? AND actual_micros IS NULL",
                (actual_micros, _now(), research_id, operation_id),
            )
            return self._operation_record(
                db.execute(
                    "SELECT * FROM project_qa_research_operations "
                    "WHERE research_id=? AND operation_id=?",
                    (research_id, operation_id),
                ).fetchone()
            )

        return self._write(write)

    def budget_summary(self, research_id: str) -> dict[str, Any]:
        summary = self.db.execute(
            "SELECT r.budget_micros,r.currency,"
            "COALESCE(SUM(CASE WHEN o.actual_micros IS NULL "
            "THEN o.estimate_micros ELSE 0 END),0) AS reserved_micros,"
            "COALESCE(SUM(CASE WHEN o.actual_micros IS NOT NULL "
            "THEN o.actual_micros ELSE 0 END),0) AS settled_micros "
            "FROM project_qa_research_runs r "
            "LEFT JOIN project_qa_research_operations o ON o.research_id=r.id "
            "WHERE r.id=? GROUP BY r.id",
            (research_id,),
        ).fetchone()
        if summary is None:
            raise ProjectQAResearchNotFound("research run not found")
        reserved = summary["reserved_micros"]
        settled = summary["settled_micros"]
        committed = reserved + settled
        return {
            "budget_micros": summary["budget_micros"],
            "currency": summary["currency"],
            "reserved_micros": reserved,
            "settled_micros": settled,
            "committed_micros": committed,
            "remaining_micros": summary["budget_micros"] - committed,
        }
