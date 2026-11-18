from contextlib import closing
import json

import pytest

from frisket.engine.store import Project
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.engine.store.project_qa_research import ProjectQAResearchStore
from frisket.engine.store.project_qa_retention import (
    remember_qa_purge_targets,
    purge_qa_content,
)


def _sheet(project, name):
    sheet = project.add_sheet(name)
    column = project.add_column(sheet, "value")
    row = project.add_rows(sheet, [{"value": "sensitive source"}], {"value": column})[0]
    return sheet, column, row


def _conversation(project, target, *, handle=True, tool=False, terminal=True):
    sheet, column, row = target
    store = ProjectQAStore(project)
    thread = store.create_thread(
        title="sensitive title",
        scope={"kind": "sources", "sources": [{"kind": "sheet", "sheet_id": sheet}]},
    )
    turn = store.submit_turn(
        thread["id"], request_id="first", question="sensitive question"
    )
    if handle:
        store.add_citation(
            turn["id"],
            label="sensitive label",
            source_kind="cell",
            locator={"sheet_id": sheet, "column_id": column, "row_id": row},
            excerpt="sensitive excerpt",
        )
    if tool:
        store.append_event(
            turn["id"],
            kind="tool_started",
            payload={"tool": "read_rows", "sheet_id": sheet},
        )
    store.append_event(
        turn["id"],
        kind="answer",
        payload={"text": "sensitive answer without any citation"},
    )
    store.append_event(turn["id"], kind="usage", payload={"cost_actual": 0.23})
    research_store = ProjectQAResearchStore(project)
    research = research_store.create(
        turn_id=turn["id"],
        actor=None,
        budget_micros=1000000,
        currency="USD",
        write_mode="ask_each",
        max_turns=None,
    )
    from pydantic_ai.messages import (
        ModelMessagesTypeAdapter,
        ModelRequest,
        ToolReturnPart,
        UserPromptPart,
        ModelResponse,
        TextPart,
    )

    research_store.checkpoint(
        research["id"],
        json.loads(
            ModelMessagesTypeAdapter.dump_json(
                [
                    ModelRequest(
                        parts=[
                            UserPromptPart(content="sensitive question"),
                            ToolReturnPart(
                                tool_name="read_rows",
                                content="sensitive tool response",
                                tool_call_id="read-1",
                            ),
                            ToolReturnPart(
                                tool_name="execute_action",
                                content="independent action output",
                                tool_call_id="action-1",
                            ),
                        ]
                    ),
                    ModelResponse(parts=[TextPart(content="sensitive AI answer")]),
                ]
            )
        ),
    )
    store.append_event(
        turn["id"],
        kind="tool_completed",
        payload={"tool": "read_rows", "text": "sensitive tool response"},
    )
    store.append_event(
        turn["id"],
        kind="tool_completed",
        payload={"tool": "execute_action", "value": "independent action output"},
    )
    research_store.publish_pause(
        research["id"],
        {
            "id": "sensitive-approval",
            "operation_id": "write-1",
            "kind": "action",
            "prompt": "sensitive request",
        },
    )
    project.db.execute(
        "INSERT INTO project_qa_research_operations(research_id,operation_id,payload_identity,operation_kind,estimate_micros,actual_micros,metadata_json,created_at) VALUES (?,?,?,'model',230000,230000,?,datetime('now'))",
        (
            research["id"],
            "call-1",
            "content-hash",
            json.dumps({"response": "sensitive output"}),
        ),
    )
    project.db.execute(
        "INSERT INTO project_qa_usage_calls(turn_id,call_id) VALUES (?,?)",
        (turn["id"], "call-1"),
    )
    project.db.commit()
    if terminal:
        store.finish_turn(
            turn["id"], status="completed", usage={"input_tokens": 30}, cost_actual=0.23
        )
    return thread, turn, research


def _begin(project, target):
    db = project.db
    db.execute("BEGIN IMMEDIATE")
    try:
        purge = db.execute("INSERT INTO project_purges DEFAULT VALUES").lastrowid
        db.execute(
            "INSERT INTO project_purge_targets(kind,object_id,purge_id) VALUES ('row',?,?)",
            (target[2], purge),
        )
        remember_qa_purge_targets(db, purge)
        db.commit()
        return purge
    except BaseException:
        db.rollback()
        raise


def _drain(project, purge):
    counts = []
    while True:
        project.db.execute("BEGIN IMMEDIATE")
        count = purge_qa_content(project.db, purge, limit=1)
        project.db.commit()
        if not count:
            return counts
        assert count == 1
        counts.append(count)


def test_source_read_removes_tool_payload_but_keeps_questions_answers_and_cost(
    tmp_path,
):
    with closing(Project.create(tmp_path / "qa")) as project:
        target = _sheet(project, "target")
        other = _sheet(project, "other")
        thread, turn, research = _conversation(project, target)
        other_thread, _, _ = _conversation(project, other)
        store = ProjectQAStore(project)
        later = store.submit_turn(
            thread["id"],
            request_id="followup",
            question="repeat sensitive prior answer",
        )
        store.append_event(
            later["id"], kind="answer", payload={"text": "sensitive follow-up"}
        )
        store.finish_turn(later["id"], status="completed")
        purge = _begin(project, target)
        # A rolled-back cleanup batch neither loses the durable target nor advances it.
        project.db.execute("BEGIN IMMEDIATE")
        assert purge_qa_content(project.db, purge, limit=1) == 1
        project.db.rollback()
        assert len(_drain(project, purge)) == 3
        assert _drain(project, purge) == []
        assert store.get_thread(thread["id"])["title"] == "sensitive title"
        assert (
            store.get_turn(later["id"])["question"] == "repeat sensitive prior answer"
        )
        events = project.db.execute(
            "SELECT kind,payload_json FROM project_qa_events WHERE thread_id=?",
            (thread["id"],),
        ).fetchall()
        assert [
            json.loads(r["payload_json"])
            for r in events
            if r["kind"] == "tool_completed"
        ] == [{"tool": "execute_action", "value": "independent action output"}]
        assert [
            (r["kind"], json.loads(r["payload_json"]))
            for r in events
            if r["kind"] in {"answer", "usage"}
        ] == [
            ("answer", {"text": "sensitive answer without any citation"}),
            ("usage", {"cost_actual": 0.23}),
            ("answer", {"text": "sensitive follow-up"}),
        ]
        kept = ProjectQAResearchStore(project).get(research["id"])
        serialized = json.dumps(kept["saved_messages"])
        assert "sensitive tool response" not in serialized
        assert (
            "sensitive question" in serialized and "sensitive AI answer" in serialized
        )
        assert "[Source unavailable]" in serialized
        assert "independent action output" in serialized
        from pydantic_ai.messages import ModelMessagesTypeAdapter

        ModelMessagesTypeAdapter.validate_python(kept["saved_messages"])
        assert kept["pending_approval"] is None
        assert store.get_turn(turn["id"])["cost_actual"] == 0.23
        assert (
            project.db.execute(
                "SELECT call_id FROM project_qa_usage_calls WHERE turn_id=?",
                (turn["id"],),
            ).fetchone()[0]
            == "call-1"
        )
        operation = project.db.execute(
            "SELECT actual_micros,metadata_json FROM project_qa_research_operations WHERE research_id=?",
            (research["id"],),
        ).fetchone()
        assert tuple(operation) == (
            230000,
            json.dumps({"response": "sensitive output"}),
        )
        assert store.get_thread(other_thread["id"])["title"] == "sensitive title"
        assert (
            project.db.execute("SELECT COUNT(*) FROM project_qa_citations").fetchone()[
                0
            ]
            == 1
        )


def test_interrupted_read_without_source_handle_still_scrubs(tmp_path):
    with closing(Project.create(tmp_path / "qa")) as project:
        target = _sheet(project, "target")
        thread, _, research = _conversation(project, target, handle=False, tool=True)
        purge = _begin(project, target)
        _drain(project, purge)
        assert (
            ProjectQAStore(project).get_thread(thread["id"])["title"]
            == "sensitive title"
        )
        assert "sensitive tool response" not in json.dumps(
            ProjectQAResearchStore(project).get(research["id"])["saved_messages"]
        )


def test_active_conversation_and_unsettled_usage_refuse_discovery_atomically(tmp_path):
    with closing(Project.create(tmp_path / "qa")) as project:
        target = _sheet(project, "target")
        _, turn, research = _conversation(project, target, terminal=False)
        with pytest.raises(ValueError, match="Stop affected Ask"):
            _begin(project, target)
        assert (
            project.db.execute("SELECT COUNT(*) FROM project_purges").fetchone()[0] == 0
        )
        ProjectQAStore(project).finish_turn(turn["id"], status="interrupted")
        project.db.execute(
            "UPDATE project_qa_research_operations SET actual_micros=NULL WHERE research_id=?",
            (research["id"],),
        )
        project.db.commit()
        with pytest.raises(ValueError, match="provider usage"):
            _begin(project, target)
        assert (
            project.db.execute("SELECT COUNT(*) FROM project_purges").fetchone()[0] == 0
        )


@pytest.mark.parametrize("kind", ["query", "project_search", "web"])
def test_sheet_query_and_project_search_scope_without_cell_citations(tmp_path, kind):
    with closing(Project.create(tmp_path / "qa")) as project:
        target = _sheet(project, "target")
        thread, turn, research = _conversation(project, target, handle=False)
        # These are persisted tool facts; neither a final answer citation nor
        # a literal occurrence of the removed value is necessary to find them.
        if kind == "query":
            project.db.execute(
                "INSERT INTO project_qa_citations(id,turn_id,label,source_kind,locator_json,created_at) VALUES ('query',?,'Aggregate','query',?,datetime('now'))",
                (turn["id"], json.dumps({"sheet_id": target[0]})),
            )
        else:
            project.db.execute(
                "UPDATE project_qa_turns SET scope_json=? WHERE id=?",
                (json.dumps({"kind": "project"}), turn["id"]),
            )
            seq = project.db.execute(
                "SELECT MAX(seq)+1 FROM project_qa_events WHERE thread_id=?",
                (thread["id"],),
            ).fetchone()[0]
            project.db.execute(
                "INSERT INTO project_qa_events(thread_id,turn_id,seq,kind,payload_json,created_at) VALUES (?,?,?,'tool_started',?,datetime('now'))",
                (
                    thread["id"],
                    turn["id"],
                    seq,
                    json.dumps(
                        {"tool": "search_web" if kind == "web" else "search_cells"}
                    ),
                ),
            )
        project.db.commit()
        purge = _begin(project, target)
        _drain(project, purge)
        assert (
            ProjectQAStore(project).get_thread(thread["id"])["title"]
            == "sensitive title"
        )
        messages = json.dumps(
            ProjectQAResearchStore(project).get(research["id"])["saved_messages"]
        )
        assert ("sensitive tool response" in messages) == (kind == "web")
