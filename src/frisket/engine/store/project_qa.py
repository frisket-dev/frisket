"""Durable, project-owned persistence for Project Ask threads and turns.

The runner and HTTP layer deliberately stay outside this module.  This store
only makes the durable admission/lifecycle facts atomic and returns plain
records suitable for those higher layers to adapt into their wire contracts.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Collection, Mapping
from datetime import datetime, timezone
from typing import Any


ACTIVE_TURN_STATUSES = frozenset({"running", "stopping"})
TERMINAL_TURN_STATUSES = frozenset({"completed", "stopped", "failed", "interrupted"})
EVENT_KINDS = frozenset(
    {
        "question",
        "assistant",
        "tool_started",
        "tool_completed",
        "answer",
        "result_suggestion",
        "action_proposal",
        "usage",
        "status",
    }
)
_UNSET = object()


class ProjectQAStoreError(RuntimeError):
    """Base error for Project Ask persistence."""


class ProjectQANotFoundError(ProjectQAStoreError, LookupError):
    """The requested thread, turn, or citation does not exist."""


class ProjectQAConflictError(ProjectQAStoreError):
    """A stale revision, terminal write, or non-equivalent replay was refused."""


class ProjectQAActiveTurnError(ProjectQAConflictError):
    """A thread already has a running or stopping turn."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _json_object(value: Mapping[str, Any], *, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return dict(value)


def _positive_int(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _scope(scope: Mapping[str, Any] | None) -> dict[str, Any]:
    value = {"kind": "project"} if scope is None else _json_object(scope, name="scope")
    kind = value.get("kind")
    if kind == "project" and set(value) == {"kind"}:
        return value
    if kind != "sources" or set(value) != {"kind", "sources"}:
        raise ValueError("scope must be project or a sources scope")
    sources = value["sources"]
    if not isinstance(sources, list) or not sources:
        raise ValueError("sources scope must have at least one source")
    normalized: list[dict[str, Any]] = []
    whole_sheets: set[int] = set()
    seen_files: set[tuple[int, int, int]] = set()
    for source in sources:
        item = _json_object(source, name="source")
        source_kind = item.get("kind")
        if source_kind == "sheet" and set(item) == {"kind", "sheet_id"}:
            sheet_id = _positive_int(item["sheet_id"], name="sheet_id")
            if sheet_id in whole_sheets:
                continue
            normalized = [
                entry
                for entry in normalized
                if not (entry["kind"] == "rows" and entry["sheet_id"] == sheet_id)
            ]
            whole_sheets.add(sheet_id)
            normalized.append(
                {
                    "kind": "sheet",
                    "sheet_id": sheet_id,
                }
            )
        elif source_kind == "rows" and set(item) == {"kind", "sheet_id", "row_ids"}:
            row_ids = item["row_ids"]
            if not isinstance(row_ids, list) or not row_ids:
                raise ValueError("row_ids must be a non-empty list")
            sheet_id = _positive_int(item["sheet_id"], name="sheet_id")
            if sheet_id in whole_sheets:
                continue
            ids = list(
                dict.fromkeys(
                    _positive_int(row_id, name="row_id") for row_id in row_ids
                )
            )
            existing = next(
                (
                    entry
                    for entry in normalized
                    if entry["kind"] == "rows" and entry["sheet_id"] == sheet_id
                ),
                None,
            )
            if existing is None:
                normalized.append(
                    {"kind": "rows", "sheet_id": sheet_id, "row_ids": ids}
                )
            else:
                existing["row_ids"] = list(dict.fromkeys(existing["row_ids"] + ids))
        elif source_kind == "file" and set(item) == {
            "kind",
            "sheet_id",
            "row_id",
            "column_id",
        }:
            file_key = (
                _positive_int(item["sheet_id"], name="sheet_id"),
                _positive_int(item["row_id"], name="row_id"),
                _positive_int(item["column_id"], name="column_id"),
            )
            if file_key in seen_files:
                continue
            seen_files.add(file_key)
            normalized.append(
                {
                    "kind": "file",
                    "sheet_id": file_key[0],
                    "row_id": file_key[1],
                    "column_id": file_key[2],
                }
            )
        else:
            raise ValueError("unsupported source scope")
    row_count = sum(
        len(entry["row_ids"]) for entry in normalized if entry["kind"] == "rows"
    )
    if row_count > 1000:
        raise ValueError("sources scope may contain at most 1000 selected rows")
    return {"kind": "sources", "sources": normalized}


def _model(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("model must be a non-empty string or null")
    return value


def _bool(value: Any, *, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


class ProjectQAStore:
    """Small transactional store over a project's thread-owned SQLite connection."""

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
    def _thread_record(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            raise ProjectQANotFoundError("thread not found")
        return {
            "id": row["id"],
            "title": row["title"],
            "scope": json.loads(row["scope_json"]),
            "model": row["model"],
            "web": bool(row["web"]),
            "suggest_actions": bool(row["suggest_actions"]),
            "revision": row["revision"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _turn_record(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            raise ProjectQANotFoundError("turn not found")
        return {
            "id": row["id"],
            "thread_id": row["thread_id"],
            "request_id": row["request_id"],
            "question": row["question"],
            "scope": json.loads(row["scope_json"]),
            "model": row["model"],
            "web": bool(row["web"]),
            "suggest_actions": bool(row["suggest_actions"]),
            "status": row["status"],
            "submitted_by": row["submitted_by"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "usage": json.loads(row["usage_json"]) if row["usage_json"] else None,
            "cost_actual": row["cost_actual"],
            "error_summary": row["error_summary"],
        }

    def create_thread(
        self,
        *,
        title: str,
        scope: Mapping[str, Any] | None = None,
        model: str | None = None,
        web: bool = False,
        suggest_actions: bool = True,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must be a non-empty string")
        if created_by is not None and (
            not isinstance(created_by, str) or not created_by
        ):
            raise ValueError("created_by must be a non-empty string or null")
        saved_scope, saved_model = _scope(scope), _model(model)
        saved_web, saved_suggest = (
            _bool(web, name="web"),
            _bool(suggest_actions, name="suggest_actions"),
        )
        now, thread_id = _now(), _id("qa_thread")

        def write(db: sqlite3.Connection) -> dict[str, Any]:
            db.execute(
                "INSERT INTO project_qa_threads "
                "(id,title,scope_json,model,web,suggest_actions,revision,created_by,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    thread_id,
                    title,
                    _json(saved_scope),
                    saved_model,
                    int(saved_web),
                    int(saved_suggest),
                    1,
                    created_by,
                    now,
                    now,
                ),
            )
            return self._thread_record(
                db.execute(
                    "SELECT * FROM project_qa_threads WHERE id=?", (thread_id,)
                ).fetchone()
            )

        return self._write(write)

    def list_threads(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 200
        ):
            raise ValueError("limit must be between 1 and 200")
        return [
            self._thread_record(row)
            for row in self.db.execute(
                "SELECT * FROM project_qa_threads ORDER BY updated_at DESC, id DESC LIMIT ?",
                (limit,),
            )
        ]

    def get_thread(self, thread_id: str) -> dict[str, Any]:
        return self._thread_record(
            self.db.execute(
                "SELECT * FROM project_qa_threads WHERE id=?", (thread_id,)
            ).fetchone()
        )

    def update_thread(
        self,
        thread_id: str,
        *,
        expected_revision: int,
        title: str | None = None,
        scope: Mapping[str, Any] | None | object = _UNSET,
        model: str | None | object = _UNSET,
        web: bool | None = None,
        suggest_actions: bool | None = None,
    ) -> dict[str, Any]:
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise ValueError("expected_revision must be a positive integer")
        if title is not None and (not isinstance(title, str) or not title.strip()):
            raise ValueError("title must be a non-empty string")
        if web is not None:
            _bool(web, name="web")
        if suggest_actions is not None:
            _bool(suggest_actions, name="suggest_actions")
        saved_scope = (
            _scope(scope) if scope is not _UNSET and scope is not None else _UNSET
        )
        saved_model = _model(model) if model is not _UNSET else _UNSET

        def write(db: sqlite3.Connection) -> dict[str, Any]:
            current = self._thread_record(
                db.execute(
                    "SELECT * FROM project_qa_threads WHERE id=?", (thread_id,)
                ).fetchone()
            )
            if current["revision"] != expected_revision:
                raise ProjectQAConflictError("thread revision is stale")
            values = {
                "title": current["title"],
                "scope_json": _json(current["scope"]),
                "model": current["model"],
                "web": int(current["web"]),
                "suggest_actions": int(current["suggest_actions"]),
            }
            if title is not None:
                values["title"] = title
            if saved_scope is not _UNSET:
                values["scope_json"] = _json(saved_scope)
            if model is not _UNSET:
                values["model"] = saved_model
            if web is not None:
                values["web"] = int(web)
            if suggest_actions is not None:
                values["suggest_actions"] = int(suggest_actions)
            now = _now()
            db.execute(
                "UPDATE project_qa_threads SET title=?,scope_json=?,model=?,web=?,suggest_actions=?,revision=revision+1,updated_at=? WHERE id=?",
                (*values.values(), now, thread_id),
            )
            return self._thread_record(
                db.execute(
                    "SELECT * FROM project_qa_threads WHERE id=?", (thread_id,)
                ).fetchone()
            )

        return self._write(write)

    def delete_thread(self, thread_id: str) -> None:
        def write(db: sqlite3.Connection) -> None:
            self._thread_record(
                db.execute(
                    "SELECT * FROM project_qa_threads WHERE id=?", (thread_id,)
                ).fetchone()
            )
            active = db.execute(
                "SELECT 1 FROM project_qa_turns WHERE thread_id=? AND status IN ('running','stopping')",
                (thread_id,),
            ).fetchone()
            if active is not None:
                raise ProjectQAActiveTurnError("thread has an active turn")
            db.execute("DELETE FROM project_qa_threads WHERE id=?", (thread_id,))

        self._write(write)

    def get_turn(self, turn_id: str) -> dict[str, Any]:
        return self._turn_record(
            self.db.execute(
                "SELECT * FROM project_qa_turns WHERE id=?", (turn_id,)
            ).fetchone()
        )

    def get_active_turn(self, thread_id: str) -> dict[str, Any] | None:
        self.get_thread(thread_id)
        row = self.db.execute(
            "SELECT * FROM project_qa_turns WHERE thread_id=? "
            "AND status IN ('running','stopping') ORDER BY started_at DESC, id DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        return self._turn_record(row) if row is not None else None

    def submit_turn(
        self,
        thread_id: str,
        *,
        request_id: str,
        question: str,
        scope: Mapping[str, Any] | None = None,
        model: str | None = None,
        web: bool = False,
        suggest_actions: bool = True,
        submitted_by: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be a non-empty string")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must be a non-empty string")
        if submitted_by is not None and (
            not isinstance(submitted_by, str) or not submitted_by
        ):
            raise ValueError("submitted_by must be a non-empty string or null")
        saved_scope, saved_model = _scope(scope), _model(model)
        saved_web, saved_suggest = (
            _bool(web, name="web"),
            _bool(suggest_actions, name="suggest_actions"),
        )

        def write(db: sqlite3.Connection) -> dict[str, Any]:
            thread = self._thread_record(
                db.execute(
                    "SELECT * FROM project_qa_threads WHERE id=?", (thread_id,)
                ).fetchone()
            )
            existing = db.execute(
                "SELECT * FROM project_qa_turns WHERE thread_id=? AND request_id=?",
                (thread_id, request_id),
            ).fetchone()
            if existing is not None:
                turn = self._turn_record(existing)
                expected = (
                    question,
                    saved_scope,
                    saved_model,
                    saved_web,
                    saved_suggest,
                )
                actual = (
                    turn["question"],
                    turn["scope"],
                    turn["model"],
                    turn["web"],
                    turn["suggest_actions"],
                )
                if actual != expected:
                    raise ProjectQAConflictError(
                        "request_id was already submitted with different content"
                    )
                return turn
            if (
                db.execute(
                    "SELECT 1 FROM project_qa_turns WHERE thread_id=? AND status IN ('running','stopping')",
                    (thread_id,),
                ).fetchone()
                is not None
            ):
                raise ProjectQAActiveTurnError("thread already has an active turn")
            now, turn_id = _now(), _id("qa_turn")
            db.execute(
                "INSERT INTO project_qa_turns (id,thread_id,request_id,question,scope_json,model,web,suggest_actions,status,submitted_by,started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    turn_id,
                    thread_id,
                    request_id,
                    question,
                    _json(saved_scope),
                    saved_model,
                    int(saved_web),
                    int(saved_suggest),
                    "running",
                    submitted_by,
                    now,
                ),
            )
            self._append_event(
                db,
                thread_id=thread_id,
                turn_id=turn_id,
                kind="question",
                payload={"question": question},
                created_at=now,
            )
            preferences_changed = (
                thread["scope"],
                thread["model"],
                thread["web"],
                thread["suggest_actions"],
            ) != (saved_scope, saved_model, saved_web, saved_suggest)
            if preferences_changed:
                db.execute(
                    "UPDATE project_qa_threads SET scope_json=?,model=?,web=?,suggest_actions=?,revision=revision+1,updated_at=? WHERE id=?",
                    (
                        _json(saved_scope),
                        saved_model,
                        int(saved_web),
                        int(saved_suggest),
                        now,
                        thread_id,
                    ),
                )
            else:
                db.execute(
                    "UPDATE project_qa_threads SET updated_at=? WHERE id=?",
                    (now, thread_id),
                )
            return self._turn_record(
                db.execute(
                    "SELECT * FROM project_qa_turns WHERE id=?", (turn_id,)
                ).fetchone()
            )

        return self._write(write)

    def _append_event(
        self,
        db: sqlite3.Connection,
        *,
        thread_id: str,
        turn_id: str,
        kind: str,
        payload: Mapping[str, Any],
        created_at: str | None = None,
    ) -> dict[str, Any]:
        if kind not in EVENT_KINDS:
            raise ValueError("unsupported event kind")
        saved_payload = _json(_json_object(payload, name="payload"))
        if len(saved_payload.encode("utf-8")) > 65_536:
            raise ValueError("event payload exceeds 64 KiB")
        seq = int(
            db.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM project_qa_events WHERE thread_id=?",
                (thread_id,),
            ).fetchone()[0]
        )
        now = created_at or _now()
        db.execute(
            "INSERT INTO project_qa_events (thread_id,turn_id,seq,kind,payload_json,created_at) VALUES (?,?,?,?,?,?)",
            (thread_id, turn_id, seq, kind, saved_payload, now),
        )
        return {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "seq": seq,
            "kind": kind,
            "payload": json.loads(saved_payload),
            "created_at": now,
        }

    def append_event(
        self, turn_id: str, *, kind: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        def write(db: sqlite3.Connection) -> dict[str, Any]:
            turn = self._turn_record(
                db.execute(
                    "SELECT * FROM project_qa_turns WHERE id=?", (turn_id,)
                ).fetchone()
            )
            if turn["status"] in TERMINAL_TURN_STATUSES:
                raise ProjectQAConflictError("cannot append content to a terminal turn")
            if turn["status"] == "stopping" and kind not in {"usage", "status"}:
                raise ProjectQAConflictError("cannot append content to a stopping turn")
            return self._append_event(
                db,
                thread_id=turn["thread_id"],
                turn_id=turn_id,
                kind=kind,
                payload=payload,
            )

        return self._write(write)

    def record_usage(
        self,
        turn_id: str,
        *,
        call_id: str,
        payload: Mapping[str, Any],
        calls: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Atomically persist one returned provider call and its turn event.

        The association table closes the gap between the neutral ledger's
        call-fact dedupe and this turn's aggregate: replaying this method for
        the same returned call writes neither a duplicate event nor a second
        aggregate contribution.
        """

        if not isinstance(call_id, str) or not call_id:
            raise ValueError("call_id must be a non-empty string")
        saved_payload = _json_object(payload, name="payload")
        if saved_payload.get("call_id") != call_id:
            raise ValueError("usage payload call_id must match call_id")
        if (
            len(calls) != 1
            or not isinstance(calls[0], Mapping)
            or calls[0].get("id") != call_id
        ):
            raise ValueError("usage must contain exactly the matching call fact")

        def write(db: sqlite3.Connection) -> dict[str, Any] | None:
            turn = self._turn_record(
                db.execute(
                    "SELECT * FROM project_qa_turns WHERE id=?", (turn_id,)
                ).fetchone()
            )
            inserted = db.execute(
                "INSERT INTO project_qa_usage_calls (turn_id,call_id) VALUES (?,?) "
                "ON CONFLICT(turn_id,call_id) DO NOTHING",
                (turn_id, call_id),
            ).rowcount
            if inserted == 0:
                return None
            from frisket.engine.store.runs import RunResultStore

            RunResultStore(self._project).write_unscoped_model_calls(
                calls, row_id=None, column_id=None, commit=False
            )
            event = self._append_event(
                db,
                thread_id=turn["thread_id"],
                turn_id=turn_id,
                kind="usage",
                payload=saved_payload,
            )
            if turn["status"] in TERMINAL_TURN_STATUSES:
                usage, cost = self._usage_summary(db, turn_id)
                db.execute(
                    "UPDATE project_qa_turns SET usage_json=?,cost_actual=? WHERE id=?",
                    (_json(usage), cost, turn_id),
                )
            return event

        return self._write(write)

    def events(
        self,
        thread_id: str,
        *,
        after: int = 0,
        before: int | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        return self._events_page(
            self.db, thread_id, after=after, before=before, limit=limit
        )

    def _events_page(
        self,
        db: sqlite3.Connection,
        thread_id: str,
        *,
        after: int,
        before: int | None,
        limit: int,
    ) -> dict[str, Any]:
        self._thread_record(
            db.execute(
                "SELECT * FROM project_qa_threads WHERE id=?", (thread_id,)
            ).fetchone()
        )
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("after must be a non-negative integer")
        if before is not None and (
            isinstance(before, bool) or not isinstance(before, int) or before < 1
        ):
            raise ValueError("before must be a positive integer or null")
        if before is not None and after:
            raise ValueError("before and nonzero after cannot be combined")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 200
        ):
            raise ValueError("limit must be between 1 and 200")
        if before is None:
            rows = db.execute(
                "SELECT * FROM project_qa_events WHERE thread_id=? AND seq>? ORDER BY seq LIMIT ?",
                (thread_id, after, limit + 1),
            ).fetchall()
            returned, has_more = rows[:limit], len(rows) > limit
        else:
            rows = db.execute(
                "SELECT * FROM project_qa_events WHERE thread_id=? AND seq<? "
                "ORDER BY seq DESC LIMIT ?",
                (thread_id, before, limit + 1),
            ).fetchall()
            returned, has_more = list(reversed(rows[:limit])), len(rows) > limit
        events = [
            {
                "thread_id": row["thread_id"],
                "turn_id": row["turn_id"],
                "seq": row["seq"],
                "kind": row["kind"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in returned
        ]
        return {
            "events": events,
            "cursor": (
                events[0]["seq"] if before is not None and events
                else events[-1]["seq"] if events
                else before if before is not None
                else after
            ),
            "has_more": has_more,
        }

    def recent_events(self, thread_id: str, *, limit: int = 100) -> dict[str, Any]:
        """Return the newest bounded page in chronological order."""

        self.get_thread(thread_id)
        last_seq = int(
            self.db.execute(
                "SELECT COALESCE(MAX(seq),0) FROM project_qa_events WHERE thread_id=?",
                (thread_id,),
            ).fetchone()[0]
        )
        return self.events(thread_id, before=last_seq + 1, limit=limit)

    def events_with_active(
        self,
        thread_id: str,
        *,
        after: int = 0,
        before: int | None = None,
        limit: int = 100,
        recent: bool = False,
    ) -> dict[str, Any]:
        """Read one event page and its active turn from one SQLite snapshot."""

        db = self.db
        owns = not db.in_transaction
        if owns:
            db.execute("BEGIN")
        try:
            if recent:
                last_seq = int(
                    db.execute(
                        "SELECT COALESCE(MAX(seq),0) FROM project_qa_events WHERE thread_id=?",
                        (thread_id,),
                    ).fetchone()[0]
                )
                before, after = last_seq + 1, 0
            page = self._events_page(
                db, thread_id, after=after, before=before, limit=limit
            )
            row = db.execute(
                "SELECT * FROM project_qa_turns WHERE thread_id=? AND status IN ('running','stopping') ORDER BY started_at DESC,id DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
            page["active_turn"] = self._turn_record(row) if row is not None else None
            if owns:
                db.commit()
            return page
        except Exception:
            if owns:
                db.rollback()
            raise

    def detail_with_history(self, thread_id: str, *, limit: int = 100) -> dict[str, Any]:
        """Read thread preferences, newest history page, and active turn together."""

        db = self.db
        owns = not db.in_transaction
        if owns:
            db.execute("BEGIN")
        try:
            thread = self._thread_record(
                db.execute("SELECT * FROM project_qa_threads WHERE id=?", (thread_id,)).fetchone()
            )
            last_seq = int(
                db.execute(
                    "SELECT COALESCE(MAX(seq),0) FROM project_qa_events WHERE thread_id=?",
                    (thread_id,),
                ).fetchone()[0]
            )
            history = self._events_page(
                db, thread_id, after=0, before=last_seq + 1, limit=limit
            )
            row = db.execute(
                "SELECT * FROM project_qa_turns WHERE thread_id=? AND status IN ('running','stopping') ORDER BY started_at DESC,id DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
            active = self._turn_record(row) if row is not None else None
            if owns:
                db.commit()
            return {"thread": thread, "active_turn": active, "history": history}
        except Exception:
            if owns:
                db.rollback()
            raise

    def add_citation(
        self,
        turn_id: str,
        *,
        label: str,
        source_kind: str,
        locator: Mapping[str, Any],
        excerpt: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if (
            not isinstance(label, str)
            or not label.strip()
            or not isinstance(source_kind, str)
            or not source_kind.strip()
        ):
            raise ValueError("citation label and source_kind must be non-empty strings")
        if excerpt is not None and not isinstance(excerpt, str):
            raise ValueError("excerpt must be a string or null")
        saved_locator, saved_metadata = (
            _json_object(locator, name="locator"),
            _json_object(metadata or {}, name="metadata"),
        )

        def write(db: sqlite3.Connection) -> dict[str, Any]:
            turn = self._turn_record(
                db.execute(
                    "SELECT * FROM project_qa_turns WHERE id=?", (turn_id,)
                ).fetchone()
            )
            if turn["status"] != "running":
                raise ProjectQAConflictError("cannot append content to a terminal turn")
            citation_id, now = _id("qa_citation"), _now()
            db.execute(
                "INSERT INTO project_qa_citations (id,turn_id,label,source_kind,locator_json,excerpt,metadata_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    citation_id,
                    turn_id,
                    label,
                    source_kind,
                    _json(saved_locator),
                    excerpt,
                    _json(saved_metadata),
                    now,
                ),
            )
            return self.get_citation(citation_id)

        return self._write(write)

    def get_citation(self, citation_id: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM project_qa_citations WHERE id=?", (citation_id,)
        ).fetchone()
        if row is None:
            raise ProjectQANotFoundError("citation not found")
        return {
            "id": row["id"],
            "turn_id": row["turn_id"],
            "label": row["label"],
            "source_kind": row["source_kind"],
            "locator": json.loads(row["locator_json"]),
            "excerpt": row["excerpt"],
            "metadata": json.loads(row["metadata_json"]),
            "created_at": row["created_at"],
        }

    def request_stop(self, turn_id: str) -> dict[str, Any]:
        def write(db: sqlite3.Connection) -> dict[str, Any]:
            turn = self._turn_record(
                db.execute(
                    "SELECT * FROM project_qa_turns WHERE id=?", (turn_id,)
                ).fetchone()
            )
            if turn["status"] == "running":
                now = _now()
                db.execute(
                    "UPDATE project_qa_turns SET status='stopping' WHERE id=?",
                    (turn_id,),
                )
                db.execute(
                    "UPDATE project_qa_threads SET updated_at=? WHERE id=?",
                    (now, turn["thread_id"]),
                )
                self._append_event(
                    db,
                    thread_id=turn["thread_id"],
                    turn_id=turn_id,
                    kind="status",
                    payload={"status": "stopping"},
                    created_at=now,
                )
            elif turn["status"] in TERMINAL_TURN_STATUSES:
                return turn
            return self._turn_record(
                db.execute(
                    "SELECT * FROM project_qa_turns WHERE id=?", (turn_id,)
                ).fetchone()
            )

        return self._write(write)

    def finish_turn(
        self,
        turn_id: str,
        *,
        status: str,
        usage: Mapping[str, Any] | None = None,
        cost_actual: float | None | object = _UNSET,
        error_summary: str | None = None,
    ) -> dict[str, Any]:
        if status not in TERMINAL_TURN_STATUSES:
            raise ValueError("status must be terminal")
        if usage is not None:
            _json_object(usage, name="usage")
        if (
            cost_actual is not _UNSET
            and cost_actual is not None
            and (
                isinstance(cost_actual, bool)
                or not isinstance(cost_actual, (int, float))
            )
        ):
            raise ValueError("cost_actual must be numeric or null")
        if error_summary is not None and not isinstance(error_summary, str):
            raise ValueError("error_summary must be a string or null")

        def write(db: sqlite3.Connection) -> dict[str, Any]:
            turn = self._turn_record(
                db.execute(
                    "SELECT * FROM project_qa_turns WHERE id=?", (turn_id,)
                ).fetchone()
            )
            if turn["status"] in TERMINAL_TURN_STATUSES:
                if turn["status"] == status:
                    return turn
                raise ProjectQAConflictError("turn is already terminal")
            if status == "stopped" and turn["status"] != "stopping":
                raise ProjectQAConflictError("only a stopping turn may become stopped")
            if status != "stopped" and turn["status"] == "stopping":
                raise ProjectQAConflictError("a stopping turn must become stopped")
            now = _now()
            derived_usage, derived_cost = self._usage_summary(db, turn_id)
            saved_usage = dict(usage) if usage is not None else derived_usage
            saved_cost = derived_cost if cost_actual is _UNSET else cost_actual
            db.execute(
                "UPDATE project_qa_turns SET status=?,finished_at=?,usage_json=?,cost_actual=?,error_summary=? WHERE id=?",
                (
                    status,
                    now,
                    _json(saved_usage),
                    saved_cost,
                    error_summary,
                    turn_id,
                ),
            )
            self._append_event(
                db,
                thread_id=turn["thread_id"],
                turn_id=turn_id,
                kind="status",
                payload={
                    "status": status,
                    **(
                        {"error_summary": error_summary}
                        if error_summary is not None
                        else {}
                    ),
                },
                created_at=now,
            )
            db.execute(
                "UPDATE project_qa_threads SET updated_at=? WHERE id=?",
                (now, turn["thread_id"]),
            )
            return self._turn_record(
                db.execute(
                    "SELECT * FROM project_qa_turns WHERE id=?", (turn_id,)
                ).fetchone()
            )

        return self._write(write)

    def finalize_turn(
        self,
        turn_id: str,
        *,
        status: str,
        error_summary: str | None = None,
    ) -> dict[str, Any]:
        """Terminalize a runner result without racing a concurrent Stop."""

        if status not in TERMINAL_TURN_STATUSES - {"stopped"}:
            raise ValueError("finalize status must be completed, failed, or interrupted")
        if error_summary is not None and not isinstance(error_summary, str):
            raise ValueError("error_summary must be a string or null")

        def write(db: sqlite3.Connection) -> dict[str, Any]:
            turn = self._turn_record(
                db.execute("SELECT * FROM project_qa_turns WHERE id=?", (turn_id,)).fetchone()
            )
            if turn["status"] in TERMINAL_TURN_STATUSES:
                return turn
            target = "stopped" if turn["status"] == "stopping" else status
            now = _now()
            usage, cost = self._usage_summary(db, turn_id)
            db.execute(
                "UPDATE project_qa_turns SET status=?,finished_at=?,usage_json=?,cost_actual=?,error_summary=? WHERE id=?",
                (target, now, _json(usage), cost, error_summary, turn_id),
            )
            self._append_event(
                db, thread_id=turn["thread_id"], turn_id=turn_id, kind="status",
                payload={"status": target, **({"error_summary": error_summary} if error_summary else {})},
                created_at=now,
            )
            db.execute("UPDATE project_qa_threads SET updated_at=? WHERE id=?", (now, turn["thread_id"]))
            return self._turn_record(db.execute("SELECT * FROM project_qa_turns WHERE id=?", (turn_id,)).fetchone())

        return self._write(write)

    @staticmethod
    def _usage_summary(
        db: sqlite3.Connection, turn_id: str
    ) -> tuple[dict[str, Any], float | None]:
        rows = db.execute(
            "SELECT payload_json FROM project_qa_events WHERE turn_id=? AND kind='usage' "
            "ORDER BY seq",
            (turn_id,),
        ).fetchall()
        tokens_in = 0
        tokens_out = 0
        unknown_cost = False
        total_cost = 0.0
        for row in rows:
            payload = json.loads(row["payload_json"])
            tokens_in += int(payload.get("tokens_in", 0))
            tokens_out += int(payload.get("tokens_out", 0))
            cost = payload.get("cost")
            if cost is None:
                unknown_cost = True
            else:
                total_cost += float(cost)
        return (
            {"calls": len(rows), "tokens_in": tokens_in, "tokens_out": tokens_out},
            None if unknown_cost else total_cost,
        )

    def reconcile_abandoned_turns(self, *, live_turn_ids: Collection[str]) -> list[str]:
        live = frozenset(live_turn_ids)
        if not all(isinstance(turn_id, str) and turn_id for turn_id in live):
            raise ValueError("live_turn_ids must contain non-empty strings")

        def write(db: sqlite3.Connection) -> list[str]:
            placeholders = ",".join("?" for _ in live) or "''"
            rows = db.execute(
                f"SELECT * FROM project_qa_turns WHERE status IN ('running','stopping') AND id NOT IN ({placeholders})",
                tuple(live),
            ).fetchall()
            reconciled: list[str] = []
            for row in rows:
                turn = self._turn_record(row)
                status = "stopped" if turn["status"] == "stopping" else "interrupted"
                now = _now()
                db.execute(
                    "UPDATE project_qa_turns SET status=?,finished_at=? WHERE id=?",
                    (status, now, turn["id"]),
                )
                db.execute(
                    "UPDATE project_qa_threads SET updated_at=? WHERE id=?",
                    (now, turn["thread_id"]),
                )
                self._append_event(
                    db,
                    thread_id=turn["thread_id"],
                    turn_id=turn["id"],
                    kind="status",
                    payload={"status": status},
                    created_at=now,
                )
                reconciled.append(turn["id"])
            return reconciled

        return self._write(write)
