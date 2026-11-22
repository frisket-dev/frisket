import json

import pytest

from frisket.engine.store.transactions import write_transaction
from frisket.engine.store.changes import ChangePublicationClosed, ChangeStore
from frisket.engine.store.source_dependencies import (
    SOURCE_BINDING,
    ExecutionSourceUnavailable,
    execution_source_binding,
    require_execution_sources,
    stop_direct_consumers,
)
from frisket.engine.store.sheet_lifecycle import delete_sheet
from frisket.engine.store.result_generations import (
    ResultGenerationStore,
    GenerationStateError,
)
from frisket.engine.store.runs import RunResultStore
from tests.engine.test_result_generation_store import (
    _start_claimed_run,
    _declare,
    _write,
)
from tests.engine.test_change_history_navigation import _step
from frisket.engine.store.writer_fence import WriterFence


def _consumer(project, sheet, output, rows, source_sheet, source_column):
    run = _start_claimed_run(
        project, sheet_id=sheet, output_column_id=output, row_ids=rows, label="consumer"
    )
    spec = json.loads(
        project.db.execute("SELECT spec FROM ops WHERE id=?", (run.op_id,)).fetchone()[
            0
        ]
    )
    spec[SOURCE_BINDING] = execution_source_binding(
        sheet_ids=[source_sheet], column_ids=[source_column]
    )
    project.db.execute(
        "UPDATE ops SET spec=? WHERE id=?", (json.dumps(spec), run.op_id)
    )
    project.db.commit()
    _declare(ResultGenerationStore(project), run, output, write_mode="create")
    return run


def test_source_sheet_deletion_keeps_independent_results_and_fences_late_output(
    make_project,
):
    project = make_project("source")
    source_sheet = project.add_sheet("source")
    source = project.add_column(source_sheet, "text")
    output_sheet = project.add_sheet("derived")
    output = project.add_column(output_sheet, "answer", generated=True)
    rows = project.add_rows(output_sheet, [{}, {}], {})
    run = _consumer(project, output_sheet, output, rows, source_sheet, source)
    _write(
        project,
        run,
        [
            {
                "row_id": rows[0],
                "column_id": output,
                "value": "saved",
                "publication_effect": "publish_value",
            }
        ],
    )
    independent = project.add_column(output_sheet, "independent", generated=True)
    other = _consumer(project, output_sheet, independent, rows, output_sheet, output)
    delete_sheet(project, source_sheet)
    assert project.get_values(output_sheet, output) == {
        rows[0]: "saved",
        rows[1]: None,
    }
    assert (
        RunResultStore(project).get_run(run.run_id)["cancel_requested_at"] is not None
    )
    assert RunResultStore(project).get_run(other.run_id)["cancel_requested_at"] is None
    assert (
        project.db.execute(
            "SELECT state FROM execution_attempts WHERE id=?",
            (run.authority.writer_attempt_id,),
        ).fetchone()[0]
        == "dispatching"
    )
    # Closing publication does not pretend a provider request was cancelled.
    with pytest.raises(GenerationStateError):
        _write(
            project,
            run,
            [
                {
                    "row_id": rows[1],
                    "column_id": output,
                    "value": "late",
                    "publication_effect": "publish_value",
                }
            ],
        )
    assert project.get_values(output_sheet, output)[rows[0]] == "saved"
    _step(project, "undo")  # Restore source containment only.
    with pytest.raises(ExecutionSourceUnavailable):
        require_execution_sources(project, run.op_id)
    with project.db:
        project.db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ChangePublicationClosed):
            ChangeStore(project).start(run.op_id)


def test_source_stop_rolls_back_with_deletion_and_does_not_stop_for_hidden_presentation(
    make_project,
):
    project = make_project("rollback")
    sheet = project.add_sheet("data")
    source = project.add_column(sheet, "source")
    output = project.add_column(sheet, "answer", generated=True)
    rows = project.add_rows(sheet, [{}], {})
    run = _consumer(project, sheet, output, rows, sheet, source)
    project.db.execute("UPDATE columns SET hidden=1 WHERE id=?", (source,))
    project.db.commit()
    require_execution_sources(project, run.op_id)
    project.db.execute("BEGIN IMMEDIATE")
    project.db.execute("UPDATE columns SET active=0 WHERE id=?", (source,))
    assert stop_direct_consumers(project, column_ids=[source]) == [run.run_id]
    project.db.rollback()
    require_execution_sources(project, run.op_id)
    assert RunResultStore(project).get_run(run.run_id)["cancel_requested_at"] is None
    assert (
        project.db.execute(
            "SELECT publication_closed FROM ops WHERE id=?", (run.op_id,)
        ).fetchone()[0]
        == 0
    )


def test_close_before_first_materialization_is_irreversible(make_project):
    project = make_project("not-started")
    sheet = project.add_sheet("source")
    op = project.append_change(
        "reduce.group_summary",
        {SOURCE_BINDING: execution_source_binding(sheet_ids=[sheet])},
    )
    store = RunResultStore(project)
    run_id = store.start_run(op, sheet, "reduce.group_summary")
    delete_sheet(project, sheet)
    with project.db:
        project.db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ChangePublicationClosed):
            ChangeStore(project).start(op)
    assert (
        project.db.execute("SELECT change_order FROM ops WHERE id=?", (op,)).fetchone()[
            0
        ]
        is None
    )
    _step(project, "undo")
    store.finish_run(run_id, "cancelled")
    assert not store.begin_run_resume(run_id).admitted
    assert store.get_run(run_id)["cancel_requested_at"]


def test_prepared_action_binds_template_sources_and_refuses_deleted_source_before_dispatch(
    make_project,
):
    from tests.engine.test_typed_map_rows_executor import REGISTRY, _runner
    from frisket.actions.system import BoundTypedActionRequest
    from frisket.actions.types import ActionRequest, SheetRows
    from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
    from frisket.execution.attempt import claim, AttemptClaimRefused

    project = make_project("queued")
    sheet = project.add_sheet("data")
    source = project.add_column(sheet, "source")
    note = project.add_column(sheet, "note")
    rows = project.add_rows(
        sheet, [{"source": "a", "note": "b"}], {"source": source, "note": note}
    )
    request = ActionRequest(
        action_id="map.render_test",
        idempotency_key="queued-source",
        scope=SheetRows(sheet_id=sheet),
        params={"source": "source", "template": {"text": "{{note}}"}},
        output_names={"rendered": "answer"},
    )
    plan = build_typed_map_rows_plan(
        project,
        BoundTypedActionRequest.bind(REGISTRY.get(request.action_id), request),
    )
    runner = _runner(project)
    runner.allow_action_lifecycle_only_programs = True
    prepared = runner.prepare_run(
        plan.spec_dict(), program=plan.program, confirmed=True
    )
    attempt = runner.authority.mint(
        program=plan.program,
        spec=plan.spec_dict(),
        run_id=prepared.run_id,
        scope=tuple(rows),
    )
    stored = json.loads(
        project.db.execute(
            "SELECT op.spec FROM ops op JOIN runs run ON run.op_id=op.id WHERE run.id=?",
            (prepared.run_id,),
        ).fetchone()[0]
    )
    assert stored[SOURCE_BINDING] == execution_source_binding(
        sheet_ids=[sheet], column_ids=[source, note]
    )
    delete_sheet(project, sheet)

    with pytest.raises(AttemptClaimRefused, match="cancelled") as exc:
        claim(project, attempt, fence=WriterFence(claimless_direct_effect=True))
    assert exc.value.code == "input_source_unavailable"
    assert (
        project.db.execute(
            "SELECT state FROM execution_attempts WHERE id=?", (attempt.attempt_id,)
        ).fetchone()[0]
        == "admitted"
    )


def test_revert_source_column_creation_cancels_direct_consumer_only(make_project):
    from frisket.engine.store.changes import start_change
    from frisket.engine.store.op_log import append_change_in_transaction

    project = make_project("column")
    sheet = project.add_sheet("data")
    source = project.add_column(sheet, "source")
    with write_transaction(project.db):
        creator = append_change_in_transaction(
            project, "create", undo_info={"created_columns": [source]}
        )
        start_change(project.db, creator)
    output = project.add_column(sheet, "answer", generated=True)
    rows = project.add_rows(sheet, [{}], {})
    run = _consumer(project, sheet, output, rows, sheet, source)
    _step(project, "revert", creator)
    assert project.get_column(source)["active"] == 0
    assert RunResultStore(project).get_run(run.run_id)["cancel_requested_at"]
    _step(project, "restore", creator)
    assert project.get_column(source)["active"] == 1
    with pytest.raises(ExecutionSourceUnavailable):
        require_execution_sources(project, run.op_id)


def test_source_deletion_cancels_runless_pdf_import_without_losing_completed_pages(
    make_project,
):
    from tests.engine.test_pdf_split_publication import _seed, _start, _children
    from frisket.engine.store.import_sessions import (
        ImportSessionStore,
        ImportSessionConflict,
    )

    project = make_project("split")
    sheet, column, rows, values = _seed(project)
    writer = _start(project, sheet, column, rows, values, replace=False)
    children = writer.publish_source(_children(2), expected_cursor=0)
    session = ImportSessionStore(project).get("split:one")
    delete_sheet(project, sheet)
    assert ImportSessionStore(project).get("split:one").state == "cancelled"
    assert project.visible_row_ids(session.sheet_id) == children
    _step(project, "undo")
    with pytest.raises(ImportSessionConflict):
        writer.publish_source(_children(3), expected_cursor=1)


def test_group_source_deleted_during_provider_call_keeps_usage_and_stops_next_call(
    tmp_path, monkeypatch
):
    from executor_harness import case_env
    from tests.engine.test_reduce_group_summary_executor import (
        CASES,
        _reduce_action,
        _summary_router,
    )

    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        router, adapter = _summary_router()
        env.run_kwargs["router"] = router
        complete = adapter.complete

        async def delete_during_call(request, client):
            response = await complete(request, client)
            delete_sheet(env.project, env.seeded["sheet_id"])
            return response

        monkeypatch.setattr(adapter, "complete", delete_during_call)
        result = env.run(
            _reduce_action(
                env.seeded["sheet_id"], idempotency_key="group-source-deletion"
            )
        )
        assert result.status == "cancelled", result
        assert len(adapter.requests) == 1
        run = env.project.db.execute(
            "SELECT id,cancel_requested_at FROM runs WHERE action_kind='reduce.group_summary'"
        ).fetchone()
        assert run["cancel_requested_at"]
        assert RunResultStore(env.project).get_run(run["id"])["status"] == "cancelled"
        assert (
            env.project.db.execute(
                "SELECT COUNT(*) FROM model_calls WHERE run_id=?", (run["id"],)
            ).fetchone()[0]
            == 1
        )


def test_direct_map_runner_stops_on_persisted_cancel_without_server_callback(
    make_project, monkeypatch
):
    import asyncio
    from tests.engine.test_typed_map_rows_executor import REGISTRY, _runner
    from frisket.actions.system import BoundTypedActionRequest
    from frisket.actions.types import ActionRequest, SheetRows
    from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan

    project = make_project("direct")
    sheet = project.add_sheet("data")
    source = project.add_column(sheet, "source")
    rows = project.add_rows(
        sheet, [{"source": "a"}, {"source": "b"}], {"source": source}
    )
    request = ActionRequest(
        action_id="map.render_test",
        idempotency_key="direct",
        scope=SheetRows(sheet_id=sheet),
        params={"source": "source", "template": {"text": "fixed"}},
        output_names={"rendered": "answer"},
    )
    plan = build_typed_map_rows_plan(
        project,
        BoundTypedActionRequest.bind(REGISTRY.get(request.action_id), request),
    )
    runner = _runner(project)
    runner.concurrency = 1
    runner.allow_action_lifecycle_only_programs = True
    execute = plan.program.execute
    calls = []

    async def delete_during_row(*args, **kwargs):
        calls.append(True)
        value = await execute(*args, **kwargs)
        delete_sheet(project, sheet)
        return value

    monkeypatch.setattr(plan.program, "execute", delete_during_row)
    prepared = runner._prepare(
        plan.spec_dict(), program=plan.program, confirmed=True, resume_run_id=None
    )
    from frisket.engine.store.generated_column_claims import GeneratedColumnClaimStore

    claims = GeneratedColumnClaimStore(project)
    with write_transaction(claims.db):
        _, conflict = claims.acquire(
            sheet_id=sheet,
            output_names=["answer"],
            action_kind=request.action_id,
            run_id=prepared.run_id,
            op_id=prepared.op_id,
            claim_token="direct-claim",
            details={"output_fields": list(plan.output_fields)},
        )
    assert conflict is None
    with write_transaction(claims.db):
        claims.bind_to_run(
            claim_token="direct-claim",
            run_id=prepared.run_id,
            expected_output_names=["answer"],
        )
    progress = asyncio.run(
        runner.run(
            plan.spec_dict(),
            program=plan.program,
            confirmed=True,
            prepared_run=prepared,
            claim_token="direct-claim",
        )
    )
    assert progress.cancel_requested
    assert RunResultStore(project).get_run(progress.run_id)["status"] == "cancelled"
    assert len(calls) == 1
    assert len(rows) == 2


def test_semantic_join_binds_both_sides_and_stops_before_next_embedding(
    make_project, monkeypatch
):
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.system import BoundTypedActionRequest
    from frisket.actions.types import ActionRequest, SheetRows
    from frisket.engine.executor.actions import _default_map_runner_factory
    from frisket.engine.executor.semantic_join_action import (
        run_typed_semantic_join_action,
    )

    project = make_project("pair")
    left, right = project.add_sheet("left"), project.add_sheet("right")
    source, target = (
        project.add_column(left, "source"),
        project.add_column(right, "target"),
    )
    project.add_rows(left, [{"source": "ACME"}], {"source": source})
    project.add_rows(right, [{"target": "Acme"}], {"target": target})
    calls = []

    def embed(texts):
        calls.append(texts)
        run = project.db.execute(
            "SELECT ops.spec,runs.id FROM ops JOIN runs ON runs.op_id=ops.id WHERE runs.action_kind='join.semantic'"
        ).fetchone()
        assert json.loads(run["spec"])[SOURCE_BINDING] == execution_source_binding(
            sheet_ids=[left, right], column_ids=[source, target]
        )
        delete_sheet(project, right)
        assert RunResultStore(project).cancellation_requested(run["id"])
        return [[1.0, 0.0] for _ in texts]

    embed.embedding_dimension = 2
    monkeypatch.setattr(
        "frisket.ai.embeddings.backends.resolve_embedder",
        lambda *args, **kwargs: (embed, "fastembed/test"),
    )
    request = ActionRequest(
        action_id="join.semantic",
        idempotency_key="pair",
        scope=SheetRows(sheet_id=left),
        params={
            "source": "source",
            "target": {"sheet_id": right, "column": "target"},
        },
        sheet_name="matches",
    )
    bound = BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get(request.action_id), request
    )
    result = run_typed_semantic_join_action(
        project, "test", bound, None, _default_map_runner_factory
    )
    assert result.status == "cancelled", result
    assert len(calls) == 1
    assert not any(sheet["name"] == "matches" for sheet in project.sheets())
