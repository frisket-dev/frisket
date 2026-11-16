"""Remove source-tool payloads while preserving questions, answers and usage.

A thread's persisted source handles identify affected reads, including reads
whose final answers did not cite the source. Cleanup is bounded and repeatable.
"""

from __future__ import annotations

import sqlite3
import json


_SOURCE_TOOLS = (
    "inspect_sheets",
    "read_rows",
    "query_rows",
    "search_cells",
    "open_source",
    "find_in_source",
    "list_sources",
    "analytics",
)

_AFFECTED_SHEETS = """
SELECT object_id FROM project_purge_targets WHERE purge_id=:purge AND kind='sheet'
UNION SELECT row.sheet_id FROM project_purge_targets target JOIN rows row
  ON row.id=target.object_id WHERE target.purge_id=:purge AND target.kind='row'
UNION SELECT col.sheet_id FROM project_purge_targets target JOIN columns col
  ON col.id=target.object_id WHERE target.purge_id=:purge AND target.kind='column'
"""
_THREADS = """SELECT thread.id FROM project_qa_threads thread
JOIN project_purge_targets target ON target.kind='qa_thread'
  AND target.object_id=thread.rowid WHERE target.purge_id=:purge"""
_TURNS = f"SELECT id FROM project_qa_turns WHERE thread_id IN ({_THREADS})"
_RESEARCH = f"SELECT id FROM project_qa_research_runs WHERE turn_id IN ({_TURNS})"


def remember_qa_purge_targets(db: sqlite3.Connection, purge_id: int) -> None:
    """Call during begin_purge, before any source handle or scope is removed."""
    if not db.in_transaction:
        raise RuntimeError("Q&A purge discovery requires a caller transaction")
    db.execute(
        f"""WITH affected_sheets AS ({_AFFECTED_SHEETS})
        INSERT OR IGNORE INTO project_purge_targets(kind,object_id,purge_id)
        SELECT 'qa_thread',thread.rowid,:purge FROM project_qa_threads thread
        WHERE EXISTS (
          SELECT 1 FROM project_qa_turns turn JOIN project_qa_citations cite ON cite.turn_id=turn.id
          WHERE turn.thread_id=thread.id AND (
            EXISTS (SELECT 1 FROM project_purge_targets target WHERE target.purge_id=:purge AND (
              (target.kind='sheet' AND target.object_id=json_extract(cite.locator_json,'$.sheet_id')) OR
              (target.kind='row' AND target.object_id=json_extract(cite.locator_json,'$.row_id')) OR
              (target.kind='column' AND target.object_id=json_extract(cite.locator_json,'$.column_id'))))
            OR (cite.source_kind IN ('query','analytics') AND
                json_extract(cite.locator_json,'$.sheet_id') IN (SELECT * FROM affected_sheets))))
        OR EXISTS (
          SELECT 1 FROM project_qa_events event JOIN project_qa_turns turn ON turn.id=event.turn_id
          WHERE turn.thread_id=thread.id AND event.kind='tool_started' AND (
            json_extract(event.payload_json,'$.sheet_id') IN (SELECT * FROM affected_sheets)
            OR json_extract(event.payload_json,'$.query.scope.sheet_id') IN (SELECT * FROM affected_sheets)
            OR (json_extract(event.payload_json,'$.tool') IN
                ('search_cells','inspect_sheets','open_source','find_in_source','list_sources')
              AND json_extract(event.payload_json,'$.sheet_id') IS NULL AND (
                json_extract(turn.scope_json,'$.kind')='project' OR EXISTS (
                  SELECT 1 FROM json_each(turn.scope_json,'$.sources') source
                  WHERE json_extract(source.value,'$.sheet_id') IN (SELECT * FROM affected_sheets))))))
        """,
        {"purge": purge_id},
    )
    # Offline does not confer authority to discard unfinished provider usage.
    if db.execute(
        f"SELECT 1 FROM project_qa_turns WHERE id IN ({_TURNS}) AND status IN ('running','stopping') LIMIT 1",
        {"purge": purge_id},
    ).fetchone():
        raise ValueError("Stop affected Ask conversations before permanent cleanup")
    if db.execute(
        f"SELECT 1 FROM project_qa_research_operations WHERE research_id IN ({_RESEARCH}) AND actual_micros IS NULL LIMIT 1",
        {"purge": purge_id},
    ).fetchone():
        raise ValueError("Resolve affected Ask provider usage before permanent cleanup")


def purge_qa_content(db: sqlite3.Connection, purge_id: int, *, limit: int = 500) -> int:
    """Scrub at most limit records; caller commits and repeats until zero."""
    if not db.in_transaction:
        raise RuntimeError("Q&A purge cleanup requires a caller transaction")
    if not 1 <= limit <= 10_000:
        raise ValueError("Q&A purge batch size must be between 1 and 10000")
    params = {"purge": purge_id, "limit": limit, "tools": json.dumps(_SOURCE_TOOLS)}
    statements = [
        f"""DELETE FROM project_qa_events WHERE (thread_id,seq) IN (
          SELECT thread_id,seq FROM project_qa_events WHERE thread_id IN ({_THREADS})
          AND kind='tool_completed' AND json_extract(payload_json,'$.tool') IN (SELECT value FROM json_each(:tools)) LIMIT :limit)""",
        f"""DELETE FROM project_qa_citations WHERE id IN (
          SELECT id FROM project_qa_citations WHERE turn_id IN ({_TURNS}) LIMIT :limit)""",
    ]
    for statement in statements:
        changed = db.execute(statement, params).rowcount
        if changed:
            return changed
    rows = db.execute(
        f"SELECT id,saved_messages_json FROM project_qa_research_runs WHERE turn_id IN ({_TURNS}) "
        "AND EXISTS (SELECT 1 FROM json_tree(saved_messages_json) part "
        "WHERE (part.key='role' AND part.value='tool') "
        "OR (part.key='part_kind' AND part.value='tool-return'))",
        params,
    )
    changed = 0
    for identity, encoded in rows:
        messages = json.loads(encoded)
        dirty = False
        for message in messages:
            parts = (
                [message] if message.get("role") == "tool" else message.get("parts", [])
            )
            for part in parts:
                if (
                    (
                        part.get("role") == "tool"
                        or part.get("part_kind") == "tool-return"
                    )
                    and part.get("tool_name") in (None, *_SOURCE_TOOLS)
                    and part.get("content") != "[Source unavailable]"
                ):
                    part["content"] = "[Source unavailable]"
                    dirty = True
        if dirty:
            db.execute(
                "UPDATE project_qa_research_runs SET saved_messages_json=?,revision=revision+1 WHERE id=?",
                (json.dumps(messages), identity),
            )
            changed += 1
            if changed == limit:
                break
    return changed
