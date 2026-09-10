from __future__ import annotations

import json
from pathlib import Path

from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from frisket.server.run_payloads import column_run_provenance_payload


def _result(
    project: Project,
    *,
    run_id: int,
    row_id: int,
    column_id: int,
    value: object,
    decision: str | None = None,
) -> None:
    project.db.execute(
        "INSERT INTO results "
        "(run_id,row_id,column_id,value,review_state,review_decision,outcome) "
        "VALUES (?,?,?,?,?,?, 'ok')",
        (
            run_id,
            row_id,
            column_id,
            json.dumps(value),
            "unreviewed" if decision is None else "verified",
            decision,
        ),
    )


def _judge_receipt(
    project: Project,
    *,
    receipt_id: str,
    judge_run_id: int,
    source_run_id: int,
    source_column_id: int,
    verdict_column_id: int,
    row_ids: list[int],
    status: str = "completed",
) -> None:
    body = {
        "inputs": [
            {
                "name": "column.answer",
                "ref": {
                    "kind": "model_rows_input_column",
                    "role": "judged_output",
                    "column_id": source_column_id,
                    "source_run_id": source_run_id,
                    "value_refs": [
                        {
                            "kind": "run_result",
                            "run_id": source_run_id,
                            "row_id": row_id,
                            "column_id": source_column_id,
                        }
                        for row_id in row_ids
                    ],
                },
            }
        ],
        "outputs": [
            {
                "name": "verdict",
                "ref": {
                    "kind": "map_result_column",
                    "role": "judge_verdict",
                    "column_id": verdict_column_id,
                },
            }
        ],
    }
    project.db.execute(
        "INSERT INTO receipts (id,run_id,action_kind,status,body) "
        "VALUES (?,?,'map.judge',?,?)",
        (receipt_id, judge_run_id, status, json.dumps(body)),
    )


def test_column_run_scores_keep_humans_and_exact_completed_judges_separate(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "scores.frisket", name="scores")
    sheet_id = project.add_sheet("stories")
    source_column_id = project.add_column(sheet_id, "answer", type="text")
    other_source_column_id = project.add_column(sheet_id, "other_answer", type="text")
    verdict_column_id = project.add_column(sheet_id, "verdict", type="boolean")
    other_verdict_column_id = project.add_column(
        sheet_id, "other_verdict", type="boolean"
    )
    partial_verdict_column_id = project.add_column(
        sheet_id, "partial_verdict", type="boolean"
    )
    row_ids = project.add_rows(
        sheet_id,
        [{"answer": "a"}, {"answer": "b"}, {"answer": "c"}],
        {"answer": source_column_id},
    )
    runs = RunResultStore(project)

    source_op = project.append_op("map.classify")
    source_run_id = runs.start_run(
        source_op,
        sheet_id,
        "map.classify",
        total_rows=3,
        row_ids=row_ids,
    )
    for row_id, decision in zip(row_ids, ("accept", "edit", None)):
        _result(
            project,
            run_id=source_run_id,
            row_id=row_id,
            column_id=source_column_id,
            value=f"generated-{row_id}",
            decision=decision,
        )
        _result(
            project,
            run_id=source_run_id,
            row_id=row_id,
            column_id=other_source_column_id,
            value=f"other-{row_id}",
            decision="accept",
        )
    runs.finish_run(source_run_id)
    project.db.execute(
        "UPDATE columns SET ai_generated=1,current_run_id=? WHERE id=?",
        (source_run_id, source_column_id),
    )

    judge_op = project.append_op("map.judge")
    judge_run_id = runs.start_run(
        judge_op,
        sheet_id,
        "map.judge",
        model="openai/judge-a",
        total_rows=3,
        row_ids=row_ids,
    )
    for row_id, verdict in zip(row_ids, (True, True, False)):
        _result(
            project,
            run_id=judge_run_id,
            row_id=row_id,
            column_id=verdict_column_id,
            value=verdict,
        )
    runs.finish_run(judge_run_id)
    # Two receipt fragments for one judge run must union their exact pinned
    # row identities instead of blending or dropping one fragment.
    _judge_receipt(
        project,
        receipt_id="judge-a-1",
        judge_run_id=judge_run_id,
        source_run_id=source_run_id,
        source_column_id=source_column_id,
        verdict_column_id=verdict_column_id,
        row_ids=row_ids[:2],
    )
    _judge_receipt(
        project,
        receipt_id="judge-a-2",
        judge_run_id=judge_run_id,
        source_run_id=source_run_id,
        source_column_id=source_column_id,
        verdict_column_id=verdict_column_id,
        row_ids=row_ids[2:],
    )

    partial_op = project.append_op("map.judge")
    partial_run_id = runs.start_run(
        partial_op,
        sheet_id,
        "map.judge",
        model="openai/judge-partial",
        total_rows=1,
        row_ids=row_ids[:1],
    )
    _result(
        project,
        run_id=partial_run_id,
        row_id=row_ids[0],
        column_id=partial_verdict_column_id,
        value=False,
    )
    runs.finish_run(partial_run_id, status="partial")
    _judge_receipt(
        project,
        receipt_id="judge-partial",
        judge_run_id=partial_run_id,
        source_run_id=source_run_id,
        source_column_id=source_column_id,
        verdict_column_id=partial_verdict_column_id,
        row_ids=row_ids[:1],
        status="partial",
    )

    other_judge_op = project.append_op("map.judge")
    other_judge_run_id = runs.start_run(
        other_judge_op,
        sheet_id,
        "map.judge",
        model="openai/judge-other-column",
        total_rows=1,
        row_ids=row_ids[:1],
    )
    _result(
        project,
        run_id=other_judge_run_id,
        row_id=row_ids[0],
        column_id=other_verdict_column_id,
        value=False,
    )
    runs.finish_run(other_judge_run_id)
    _judge_receipt(
        project,
        receipt_id="judge-other-column",
        judge_run_id=other_judge_run_id,
        source_run_id=source_run_id,
        source_column_id=other_source_column_id,
        verdict_column_id=other_verdict_column_id,
        row_ids=row_ids[:1],
    )

    replacement_op = project.append_op("map.classify")
    replacement_run_id = runs.start_run(
        replacement_op,
        sheet_id,
        "map.classify",
        total_rows=1,
        row_ids=row_ids[:1],
    )
    _result(
        project,
        run_id=replacement_run_id,
        row_id=row_ids[0],
        column_id=source_column_id,
        value="new generation",
    )
    runs.finish_run(replacement_run_id)
    project.db.execute(
        "UPDATE columns SET current_run_id=? WHERE id=?",
        (replacement_run_id, source_column_id),
    )
    project.db.commit()

    payload = column_run_provenance_payload(project, source_column_id)
    scored = next(run for run in payload["runs"] if run["run_id"] == source_run_id)
    assert scored["human_score"] == {"passed": 1, "graded": 2}
    assert scored["judge_scores"] == [
        {
            "run_id": judge_run_id,
            "model": "openai/judge-a",
            "started_at": scored["judge_scores"][0]["started_at"],
            "verdict_column_id": verdict_column_id,
            "passed": 2,
            "graded": 3,
            "compared": 2,
            "disagreement_count": 1,
        }
    ]
    assert payload["current_run"]["run_id"] == replacement_run_id
    other_payload = column_run_provenance_payload(project, other_source_column_id)
    other_scored = next(
        run for run in other_payload["runs"] if run["run_id"] == source_run_id
    )
    assert other_scored["human_score"] == {"passed": 3, "graded": 3}
    assert [score["run_id"] for score in other_scored["judge_scores"]] == [
        other_judge_run_id
    ]
    project.close()
