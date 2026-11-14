"""Retained work is independent of current publication and provider retries."""

import asyncio
import json

import pytest

from frisket.ai.llm import ModelRouter, LLMResponse
from frisket.engine.executor import run_action_spec
from frisket.engine.runner.review import review_queue, review_bundle_page
from frisket.engine.store import Project
from frisket.engine.store.evidence import (
    record_source_artifact,
    record_source_span,
    record_evidence_link,
    list_cell_evidence,
)


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "100")
    project = Project.create(tmp_path / "recovery.frisket")
    sheet = project.add_sheet("Rows")
    source = project.add_column(sheet, "source")
    rows = project.add_rows(
        sheet, [{"source": "first"}, {"source": "second"}], {"source": source}
    )
    calls = []

    class Adapter:
        fail = None
        answer = None

        async def complete(self, request, client):
            calls.append(request)
            if self.fail and self.fail in json.dumps(request.messages):
                raise ValueError("Known rejection")
            answer = self.answer or {"answer": f"answer {len(calls)}"}
            return LLMResponse(
                content=json.dumps(answer),
                data=answer,
                model=request.model,
                tokens_in=3,
                tokens_out=2,
                cost=0.001,
            )

    router = ModelRouter(
        keys={"anthropic": "test"}, use_env_keys=False, cache_mode="off", max_retries=0
    )
    adapter = Adapter()
    router._adapters["anthropic"] = adapter

    def run(action, params, **extra):
        request = {
            "action_id": action,
            "idempotency_key": extra.pop("key", f"key-{len(calls)}-{action}"),
            "scope": {"kind": "sheet_rows", "sheet_id": sheet, "row_ids": rows},
            "params": params,
            **extra,
        }
        result = run_action_spec(project, request, project_id="test", router=router)
        if result.status == "needs_confirmation":
            result = run_action_spec(
                project,
                {
                    **request,
                    "confirmation": result.errors[0].details["promise_set_hash"],
                },
                project_id="test",
                router=router,
            )
        return result

    yield project, sheet, source, rows, calls, adapter, run
    asyncio.run(router.aclose())
    project.close()


def start(run):
    return run(
        "map.ask",
        {
            "source": ["source"],
            "model": "anthropic/claude-haiku-4-5",
            "question": "describe",
        },
        output_names={"answer": "Answer"},
    )


def test_reapply_preserves_citation_span_and_saved_result_ref_and_undoes_new_column(
    work,
):
    project, sheet, source, rows, calls, adapter, run = work
    original = start(run)
    assert original.status == "completed"
    output = next(c for c in project.columns(sheet) if c["name"] == "Answer")
    _, refs = project.get_values_with_refs(sheet, output["id"], row_ids=[rows[0]])
    artifact = record_source_artifact(
        project,
        artifact_kind="web_page",
        media_type="text/html",
        source_url="https://example.invalid/source",
    )
    span = record_source_span(
        project,
        artifact_id=artifact["id"],
        span_kind="page",
        page_start=1,
        quote="original cited passage",
    )
    record_evidence_link(
        project,
        subject_kind="cell",
        subject_ref=refs[rows[0]],
        spans=[{"span_id": span["id"]}],
        sheet_id=sheet,
        row_id=rows[0],
        column_id=output["id"],
        run_id=original.run_id,
    )
    project.undo()
    assert all(
        not item["applied"] for item in review_queue(project, run_id=original.run_id)
    )
    historical = review_bundle_page(
        project, run_id=original.run_id, include_reviewed=True
    )
    assert historical["total"] == 2
    assert all(
        not field["applied"]
        for bundle in historical["bundles"]
        for field in bundle["fields"]
    )
    project.apply_edits([{"row_id": rows[0], "column_id": source, "value": "later"}])
    reapplied = run(
        "run.reapply",
        {"source_run_id": original.run_id},
        output_names={"answer": "Restored"},
    )
    assert reapplied.status == "completed", reapplied.errors
    restored = next(c for c in project.columns(sheet) if c["name"] == "Restored")
    _, refs = project.get_values_with_refs(sheet, restored["id"], row_ids=[rows[0]])
    assert refs[rows[0]]["retained_result"]["run_id"] == original.run_id
    citations = list_cell_evidence(
        project, sheet_id=sheet, row_id=rows[0], column_id=restored["id"]
    )
    assert len(citations["links"]) == 1
    assert (
        project.db.execute(
            "SELECT COUNT(DISTINCT span_id) FROM evidence_link_spans"
        ).fetchone()[0]
        == 1
    )
    assert len(calls) == 2
    project.undo()
    assert "Restored" not in {c["name"] for c in project.columns(sheet)}


def test_reapply_from_retry_collects_success_union_and_retry_does_not_expand_scope(
    work,
):
    project, sheet, source, rows, calls, adapter, run = work
    adapter.fail = "second"
    original = start(run)
    adapter.fail = None
    retried = run("run.retry", {"source_run_id": original.run_id})
    assert retried.status == "completed", retried.errors
    combined = review_bundle_page(
        project, run_id=original.run_id, include_reviewed=True
    )
    assert combined["total"] == 2
    assert {bundle["execution_run_id"] for bundle in combined["bundles"]} == {
        original.run_id
    }
    assert {
        field["value"] for bundle in combined["bundles"] for field in bundle["fields"]
    } == {"answer 1", "answer 3"}
    reapplied = run(
        "run.reapply",
        {"source_run_id": retried.run_id},
        output_names={"answer": "Combined"},
    )
    assert reapplied.status == "completed", reapplied.errors
    column = next(c for c in project.columns(sheet) if c["name"] == "Combined")
    assert project.get_values(sheet, column["id"]) == {
        rows[0]: "answer 1",
        rows[1]: "answer 3",
    }
    assert len(calls) == 3
    refused = run("run.retry", {"source_run_id": original.run_id}, key="no-work-left")
    assert refused.status == "failed" and len(calls) == 3


def test_review_picker_combines_attempts_and_keeps_undone_results(work):
    from frisket.engine.runner.review import (
        review_runs_page,
        queue_count,
        set_review_run_status,
    )

    project, sheet, source, rows, calls, adapter, run = work
    adapter.fail = "second"
    original = start(run)
    adapter.fail = None
    retried = run("run.retry", {"source_run_id": original.run_id})
    page = review_runs_page(project)
    assert [item["run_id"] for item in page["runs"]] == [original.run_id]
    assert page["runs"][0]["total"]["eligible_count"] == 2
    assert queue_count(project, run_id=retried.run_id) == 2
    assert project.undo() is not None
    assert project.undo() is not None
    project.apply_edits(
        [{"row_id": rows[0], "column_id": source, "value": "later edit"}]
    )
    assert project.redo() is None
    pinned = review_runs_page(project, run_id=retried.run_id)
    assert pinned["runs"][0]["run_id"] == original.run_id
    assert pinned["runs"][0]["total"]["eligible_count"] == 2
    assert (
        review_bundle_page(project, run_id=original.run_id, include_reviewed=True)[
            "total"
        ]
        == 2
    )

    completed = set_review_run_status(project, run_id=retried.run_id, status="complete")
    assert completed["run_id"] == original.run_id
    assert completed["status"] == "complete"
    assert (
        review_runs_page(project, run_id=original.run_id)["runs"][0]["review_status"]
        == "complete"
    )


def test_reapply_checks_prepared_targets_before_writing(work):
    project, sheet, source, rows, calls, adapter, run = work
    original = start(run)
    before = dict(project.get_values(sheet, source))
    refused = run(
        "run.reapply",
        {"source_run_id": original.run_id},
        expected_output_columns={"Answer": None},
        replace_existing=True,
    )
    assert refused.status == "failed"
    assert refused.errors[0].code == "output_target_changed"
    assert dict(project.get_values(sheet, source)) == before
    assert len(calls) == 2
    assert (
        project.db.execute("SELECT COUNT(*) FROM ops WHERE kind='edit'").fetchone()[0]
        == 0
    )


@pytest.mark.parametrize(
    "names", [{"first": "Second"}, {"first": "Copy", "second": "Copy"}]
)
def test_reapply_refuses_colliding_output_roles_without_writes(work, names):
    project, sheet, source, rows, calls, adapter, run = work
    adapter.answer = {"first": "one", "second": "two"}
    original = run(
        "map.extract",
        {
            "source": ["source"],
            "model": "anthropic/claude-haiku-4-5",
            "fields": [
                {"name": "first", "type": "text"},
                {"name": "second", "type": "text"},
            ],
            "grounding": {"enabled": False},
        },
        output_names={"first": "First", "second": "Second"},
    )
    assert original.status == "completed", original.errors
    before = [dict(column) for column in project.columns(sheet)]
    refused = run(
        "run.reapply",
        {"source_run_id": original.run_id},
        output_names=names,
        replace_existing=True,
    )
    assert refused.status == "failed"
    assert "unique" in refused.errors[0].message
    assert [dict(column) for column in project.columns(sheet)] == before
    assert len(calls) == 2


def test_reapply_replaces_original_generated_output_with_saved_provenance(work):
    project, sheet, source, rows, calls, adapter, run = work
    original = start(run)
    column = next(c["id"] for c in project.columns(sheet) if c["name"] == "Answer")
    saved = dict(project.get_values(sheet, column))
    project.apply_edits(
        [{"row_id": rows[0], "column_id": column, "value": "manual edit"}]
    )
    reapplied = run(
        "run.reapply",
        {"source_run_id": original.run_id},
        replace_existing=True,
        expected_output_columns={"Answer": column},
    )
    assert reapplied.status == "completed", reapplied.errors
    assert dict(project.get_values(sheet, column)) == saved
    _, refs = project.get_values_with_refs(sheet, column, row_ids=[rows[0]])
    assert refs[rows[0]]["retained_result"]["run_id"] == original.run_id
    assert len(calls) == 2
    assert project.undo() is not None
    assert project.get_values(sheet, column)[rows[0]] == "manual edit"
