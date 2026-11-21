"""Saved-result copies retain evidence quality without N+1 source reads."""

import json


import test_execution_lifecycle_recovery as recovery
from frisket.engine.store import Project
from frisket.engine.store.citations import (
    record_citation,
    record_source_artifact,
    record_source_span,
    sheet_cited_column_ids,
)
from frisket.engine.store.transactions import write_transaction

work = recovery.work


def _source(work, *, count=4):
    project, sheet, _, rows, _, _, run = work
    original = recovery.start(run)
    column = next(c["id"] for c in project.columns(sheet) if c["name"] == "Answer")
    _, refs = project.get_values_with_refs(sheet, column, row_ids=rows)
    with write_transaction(project.db):
        artifact = record_source_artifact(
            project,
            artifact_kind="web_page",
            media_type="text/html",
            source_url="https://example.invalid/citation",
        )
    with write_transaction(project.db):
        span = record_source_span(
            project,
            artifact_id=artifact["id"],
            span_kind="page",
            page_start=1,
            quote="Retained supporting passage",
        )
    with project.transaction_scope():
        for i in range(count):
            stale = i % 2 == 1
            record_citation(
                project,
                subject_kind="cell",
                subject_ref=refs[rows[i % 2]],
                spans=[
                    {
                        "span_id": span["id"],
                        "rank": 3,
                        "span_role": "support",
                        "required": True,
                        "note": "Keep this association",
                    }
                ],
                sheet_id=sheet,
                row_id=rows[i % 2],
                column_id=column,
                run_id=original.run_id,
                link_role="support",
                status="stale" if stale else "active",
                stale_reason="source_changed" if stale else None,
                pinned=i % 4 >= 2,
                confidence=0.8,
                producer={"engine": "test"},
                metadata={"source_note": i},
            )
        project.db.execute(
            "UPDATE citations SET stale_at='2026-11-01T12:00:00Z' "
            "WHERE column_id=? AND status='stale'",
            (column,),
        )
    return original, column


def test_reapply_preserves_evidence_state_spans_and_undo_ownership(work):
    project, sheet, _, _, calls, _, run = work
    original, source_column = _source(work)
    result = run(
        "run.reapply",
        {"source_run_id": original.run_id},
        output_names={"answer": "Copy"},
    )
    assert result.status == "completed", result.errors
    copied_column = next(c["id"] for c in project.columns(sheet) if c["name"] == "Copy")
    source = project.db.execute(
        "SELECT * FROM citations WHERE column_id=? ORDER BY id", (source_column,)
    ).fetchall()
    copied = project.db.execute(
        "SELECT * FROM citations WHERE column_id=? ORDER BY id", (copied_column,)
    ).fetchall()
    assert len(copied) == len(source) == 4
    for old, new in zip(source, copied, strict=True):
        for name in (
            "status",
            "stale_reason",
            "stale_at",
            "pinned",
            "confidence",
            "link_role",
            "producer_json",
            "layer_family",
            "run_id",
        ):
            assert new[name] == old[name], name
        assert json.loads(new["metadata"]) == {
            **json.loads(old["metadata"]),
            "reapplied_from": old["stable_id"],
        }
        assert json.loads(new["subject_ref_json"])["kind"] == "copied_result"
        assert new["op_id"] is not None

        def spans(link):
            return [
                tuple(r)
                for r in project.db.execute(
                    "SELECT span_id,rank,span_role,required,note FROM citation_spans WHERE citation_id=?",
                    (link["id"],),
                )
            ]

        assert spans(new) == spans(old)
    assert project.db.execute("SELECT count(*) FROM source_spans").fetchone()[0] == 1
    assert copied_column in sheet_cited_column_ids(project, sheet)
    project.undo()
    assert copied_column not in sheet_cited_column_ids(project, sheet)
    project.redo()
    assert copied_column in sheet_cited_column_ids(project, sheet)
    assert len(calls) == 2


def test_reapply_batches_current_refs_and_copies_spans_without_readback(
    work, monkeypatch
):
    project, sheet, _, _, _, _, run = work
    original, _ = _source(work, count=205)
    reads = []
    get_values = Project.get_values_with_refs

    def tracked(self, sheet_id, column_id, **kwargs):
        reads.append((column_id, kwargs.get("row_ids")))
        return get_values(self, sheet_id, column_id, **kwargs)

    monkeypatch.setattr(Project, "get_values_with_refs", tracked)
    statements = []
    project.db.set_trace_callback(statements.append)
    try:
        result = run(
            "run.reapply",
            {"source_run_id": original.run_id},
            output_names={"answer": "Copy"},
        )
    finally:
        project.db.set_trace_callback(None)
    assert result.status == "completed", result.errors
    copied_column = next(c["id"] for c in project.columns(sheet) if c["name"] == "Copy")
    assert len([r for c, r in reads if c == copied_column]) == 2
    assert (
        project.db.execute(
            "SELECT count(*) FROM citations WHERE column_id=?", (copied_column,)
        ).fetchone()[0]
        == 205
    )
    assert not any(
        s.lstrip().startswith("SELECT span_id,rank,span_role") for s in statements
    )
