"""Retained work is independent of current publication and provider retries."""

from frisket.execution.purchases import LOCAL_PURCHASE_POLICY

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
        model_call_policy=LOCAL_PURCHASE_POLICY,
        keys={"anthropic": "test"},
        use_env_keys=False,
        cache_mode="off",
        max_retries=0,
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
    assert (
        project.db.execute(
            "SELECT published FROM results WHERE run_id=? AND row_id=?",
            (retried.run_id, rows[1]),
        ).fetchone()[0]
        == 1
    )
    assert (
        project.db.execute(
            "SELECT publication_closed FROM ops JOIN runs ON runs.op_id=ops.id "
            "WHERE runs.id=?",
            (retried.run_id,),
        ).fetchone()[0]
        == 1
    )
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


def test_backfill_after_retry_starts_a_separate_execution(work):
    from frisket.engine.store.execution_groups import execution_runs

    project, sheet, source, rows, calls, adapter, run = work
    adapter.fail = "second"
    original = start(run)
    adapter.fail = None
    retried = run("run.retry", {"source_run_id": original.run_id})
    fresh = run(
        "run.backfill",
        {"column": "Answer"},
        scope={"kind": "sheet_rows", "sheet_id": sheet, "row_ids": [rows[1]]},
    )
    assert fresh.status == "completed", fresh.errors
    assert execution_runs(project, original.run_id) == (
        original.run_id,
        [original.run_id, retried.run_id],
    )
    assert execution_runs(project, fresh.run_id) == (fresh.run_id, [fresh.run_id])


def test_reapply_uses_saved_logical_role_after_column_rename(work):
    project, sheet, source, rows, calls, adapter, run = work
    original = start(run)
    output = next(c for c in project.columns(sheet) if c["name"] == "Answer")
    project.db.execute(
        "UPDATE columns SET name=? WHERE id=?", ("Renamed", output["id"])
    )
    project.db.commit()
    reapplied = run(
        "run.reapply",
        {"source_run_id": original.run_id},
        output_names={"answer": "Restored"},
    )
    assert reapplied.status == "completed", reapplied.errors
    restored = next(c for c in project.columns(sheet) if c["name"] == "Restored")
    assert dict(project.get_values(sheet, restored["id"])) == dict(
        project.get_values(sheet, output["id"])
    )
    assert len(calls) == 2


def test_retry_after_output_undo_explains_recovery_without_purchasing(work):
    project, sheet, source, rows, calls, adapter, run = work
    adapter.fail = "second"
    original = start(run)
    assert project.undo() is not None
    adapter.fail = None
    retried = run("run.retry", {"source_run_id": original.run_id})
    assert retried.status == "failed"
    assert "Reapply saved results" in retried.errors[0].message
    assert len(calls) == 2


def test_reapply_retains_original_output_type_after_column_retype(work):
    project, sheet, source, rows, calls, adapter, run = work
    original = start(run)
    output = next(c for c in project.columns(sheet) if c["name"] == "Answer")
    saved = dict(project.get_values(sheet, output["id"]))
    project.set_column_type(output["id"], "number")
    project.set_column_format(output["id"], "currency")
    project.set_column_semantic_type(output["id"], "later_numeric_meaning")
    refused = run(
        "run.reapply",
        {"source_run_id": original.run_id},
        key="incompatible-target",
        scope={"kind": "sheet_rows", "sheet_id": sheet, "row_ids": [rows[0]]},
        replace_existing=True,
    )
    assert refused.status == "failed"
    assert refused.errors[0].code == "output_column_exists"
    replaced = run(
        "run.reapply",
        {"source_run_id": original.run_id},
        key="whole-target",
        replace_existing=True,
    )
    assert replaced.status == "completed", replaced.errors
    assert project.get_values(sheet, output["id"]) == saved
    assert (
        next(c for c in project.columns(sheet) if c["id"] == output["id"])["type"]
        == "text"
    )
    project.undo()
    assert (
        next(c for c in project.columns(sheet) if c["id"] == output["id"])["type"]
        == "number"
    )
    restored_result = run(
        "run.reapply",
        {"source_run_id": original.run_id},
        output_names={"answer": "Restored"},
    )
    assert restored_result.status == "completed", restored_result.errors
    restored = next(c for c in project.columns(sheet) if c["name"] == "Restored")
    assert restored["type"] == "text"
    assert restored["format"] == output["format"]
    assert restored["semantic_type"] == output["semantic_type"]
    assert dict(project.get_values(sheet, restored["id"])) == saved
    _, refs = project.get_values_with_refs(sheet, restored["id"], row_ids=[rows[0]])
    assert refs[rows[0]]["retained_result"]["run_id"] == original.run_id
    assert len(calls) == 2


@pytest.mark.parametrize("replace_existing", [False, True])
def test_reapply_refuses_another_producers_hidden_active_output(work, replace_existing):
    project, sheet, source, rows, calls, adapter, run = work
    original = start(run)
    adapter.fail = "first"
    other = run(
        "map.ask",
        {
            "source": ["source"],
            "model": "anthropic/claude-haiku-4-5",
            "question": "other",
        },
        output_names={"answer": "Other"},
        scope={"kind": "sheet_rows", "sheet_id": sheet, "row_ids": [rows[0]]},
    )
    assert other.run_id is not None
    target = next(
        c for c in project.columns(sheet, include_hidden=True) if c["name"] == "Other"
    )
    assert target["active"] and target["hidden"]
    before = dict(target)
    result = run(
        "run.reapply",
        {"source_run_id": original.run_id},
        output_names={"answer": "Other"},
        replace_existing=replace_existing,
    )
    assert result.status == "failed", result.errors
    assert "hidden" in result.errors[0].message.lower()
    assert dict(project.get_column(target["id"])) == before
    assert len(calls) == 3


@pytest.mark.parametrize("action", ["run.retry", "run.reapply"])
def test_default_recovery_skips_deleted_original_rows(work, action):
    project, sheet, source, rows, calls, adapter, run = work
    if action == "run.retry":
        adapter.fail = "describe"
    original = start(run)
    deleted = run(
        "row.delete",
        {"sheet_id": sheet, "row_ids": [rows[1]]},
        scope={"kind": "project"},
    )
    assert deleted.status == "completed", deleted.errors
    adapter.fail = None
    result = run(
        action,
        {"source_run_id": original.run_id},
        scope={"kind": "sheet_rows", "sheet_id": sheet},
        **({"output_names": {"answer": "Restored"}} if action == "run.reapply" else {}),
    )
    assert result.status == "completed", result.errors
    assert len(calls) == (3 if action == "run.retry" else 2)
    assert project.visible_row_ids(sheet) == [rows[0]]
    refused = run(
        action,
        {"source_run_id": original.run_id},
        key="explicit-deleted-row",
        scope={"kind": "sheet_rows", "sheet_id": sheet, "row_ids": [rows[1]]},
        **({"output_names": {"answer": "Explicit"}} if action == "run.reapply" else {}),
    )
    assert refused.status == "failed"


def test_failed_row_retry_uses_its_head_after_unrelated_backfill(work):
    from frisket.server.run_payloads import column_run_provenance_payload

    project, sheet, source, rows, calls, adapter, run = work
    adapter.fail = "second"
    original = start(run)
    column = next(c["id"] for c in project.columns(sheet) if c["name"] == "Answer")
    adapter.fail = None
    backfill = run(
        "run.backfill",
        {"column": "Answer"},
        scope={"kind": "sheet_rows", "sheet_id": sheet, "row_ids": [rows[0]]},
    )
    assert backfill.status == "completed", backfill.errors
    history = column_run_provenance_payload(project, column)
    assert history["latest_run"]["run_id"] == backfill.run_id
    _, refs = project.get_values_with_refs(sheet, column, row_ids=[rows[1]])
    assert refs[rows[1]]["run_id"] == original.run_id
    wrong_root = run(
        "run.retry",
        {"source_run_id": backfill.run_id},
        key="wrong-root",
        scope={"kind": "sheet_rows", "sheet_id": sheet, "row_ids": [rows[1]]},
    )
    assert wrong_root.status == "failed"
    assert "cohort" in wrong_root.errors[0].message
    retried = run(
        "run.retry",
        {"source_run_id": refs[rows[1]]["run_id"]},
        scope={"kind": "sheet_rows", "sheet_id": sheet, "row_ids": [rows[1]]},
    )
    assert retried.status == "completed", retried.errors
    assert len(calls) == 4


def test_explicit_retry_buys_current_inputs_despite_old_unknown_effect(
    work, monkeypatch
):
    from frisket.ai.llm import LLMError

    project, sheet, source, rows, calls, adapter, run = work
    complete = adapter.complete

    async def lose_first_response(request, client):
        if not calls:
            calls.append(request)
            raise LLMError("response lost after acceptance", status=502, retryable=True)
        return await complete(request, client)

    monkeypatch.setattr(adapter, "complete", lose_first_response)
    original = start(run)
    checkpoint = project.db.execute(
        "SELECT id,state FROM effect_checkpoints WHERE family='row_effect' AND group_key=? AND state='reserved'",
        (str(original.run_id),),
    ).fetchone()
    assert checkpoint is not None
    project.apply_edits(
        [{"row_id": rows[0], "column_id": source, "value": "changed current input"}]
    )
    retried = run("run.retry", {"source_run_id": original.run_id})
    assert retried.status == "completed", retried.errors
    assert any(
        "changed current input" in json.dumps(call.messages) for call in calls[1:]
    )
    assert (
        project.db.execute(
            "SELECT state FROM effect_checkpoints WHERE id=?", (checkpoint["id"],)
        ).fetchone()[0]
        == "reserved"
    )


@pytest.mark.parametrize(
    "delete_failed_row,explicit_scope,expected_status",
    [(True, True, "failed"), (False, True, "completed"), (True, False, "completed")],
)
def test_reapply_validates_explicit_rows_before_filtering_accepted_results(
    work, delete_failed_row, explicit_scope, expected_status
):
    project, sheet, source, rows, calls, adapter, run = work
    adapter.fail = "second"
    original = start(run)
    if delete_failed_row:
        deleted = run(
            "row.delete",
            {"sheet_id": sheet, "row_ids": [rows[1]]},
            scope={"kind": "project"},
        )
        assert deleted.status == "completed", deleted.errors
    before_columns = [
        dict(column) for column in project.columns(sheet, include_hidden=True)
    ]
    result = run(
        "run.reapply",
        {"source_run_id": original.run_id},
        scope={
            "kind": "sheet_rows",
            "sheet_id": sheet,
            **({"row_ids": rows} if explicit_scope else {}),
        },
        output_names={"answer": "Restored"},
    )
    assert result.status == expected_status, result.errors
    assert len(calls) == 2
    if expected_status == "failed":
        assert result.errors[0].code == "invalid_row_ref"
        assert "visible" in result.errors[0].message
        assert [
            dict(column) for column in project.columns(sheet, include_hidden=True)
        ] == before_columns
    else:
        restored = next(
            column for column in project.columns(sheet) if column["name"] == "Restored"
        )
        assert project.get_values(sheet, restored["id"])[rows[0]] == "answer 1"
