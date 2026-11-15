"""Remove affected conversation content while retaining accounting identities.

Reads create source handles even if the final answer does not cite them. Tool
start events cover interrupted reads; whole-sheet tools conservatively depend
on that sheet. Later turns reuse history, so the retention unit is the thread.
The existing purge target relation makes batch cleanup resumable.
"""

from __future__ import annotations

import sqlite3


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
    params = {"purge": purge_id, "limit": limit}
    statements = [
        f"""DELETE FROM project_qa_events WHERE (thread_id,seq) IN (
          SELECT thread_id,seq FROM project_qa_events WHERE thread_id IN ({_THREADS})
          AND kind!='usage' LIMIT :limit)""",
        f"""DELETE FROM project_qa_citations WHERE id IN (
          SELECT id FROM project_qa_citations WHERE turn_id IN ({_TURNS}) LIMIT :limit)""",
        f"""UPDATE project_qa_research_runs SET state='completed',skills_json='[]',
          saved_messages_json='[]',pending_approval_json=NULL,output_grants_json='[]',revision=revision+1
          WHERE id IN (SELECT id FROM project_qa_research_runs WHERE turn_id IN ({_TURNS})
          AND (state!='completed' OR skills_json!='[]' OR saved_messages_json!='[]'
            OR pending_approval_json IS NOT NULL OR output_grants_json!='[]') LIMIT :limit)""",
        f"""UPDATE project_qa_research_operations SET metadata_json='{{}}'
          WHERE (research_id,operation_id) IN (SELECT research_id,operation_id
          FROM project_qa_research_operations WHERE research_id IN ({_RESEARCH})
          AND metadata_json!='{{}}' LIMIT :limit)""",
        f"""UPDATE project_qa_turns SET question='[Content removed]',
          research_json=NULL,error_summary=NULL WHERE id IN (SELECT id FROM project_qa_turns
          WHERE thread_id IN ({_THREADS}) AND (question!='[Content removed]'
            OR research_json IS NOT NULL OR error_summary IS NOT NULL)
          LIMIT :limit)""",
        f"""UPDATE project_qa_threads SET title='[Content removed]',
          research_json=NULL,revision=revision+1 WHERE id IN (SELECT id FROM project_qa_threads
          WHERE id IN ({_THREADS}) AND (title!='[Content removed]'
            OR research_json IS NOT NULL) LIMIT :limit)""",
    ]
    for statement in statements:
        changed = db.execute(statement, params).rowcount
        if changed:
            return changed
    return 0
