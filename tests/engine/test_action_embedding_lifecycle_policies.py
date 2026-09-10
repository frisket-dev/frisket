from __future__ import annotations


def test_manual_refresh_v1_launch_enqueues_action_run(tmp_path) -> None:
    import logging

    from frisket.engine.executor import run_action_spec
    from frisket.engine.jobs.queue import ACTION_RUN_KIND, SqliteJobQueue
    from frisket.ai.llm import ModelRouter
    from frisket.execution.provider import (
        ExecutionCompositionContext,
        open_execution_composition,
    )
    from frisket.server.action_enqueue import (
        QueuedV1ActionRunContext,
        queue_v1_action_run,
    )
    from frisket.engine.store import Project

    workspace = tmp_path / "ws"
    workspace.mkdir()
    project_id = "proj"
    project = Project.create(workspace / f"{project_id}.frisket", name="proj")
    queue = SqliteJobQueue(workspace / ".queue.db")
    try:
        sheet = project.add_sheet("data")
        col = project.add_column(sheet, "headline")
        project.add_rows(sheet, [{"headline": "story"}], {"headline": col})
        create = run_action_spec(
            project,
            {
                "action_id": "embedding.index_create",
                "scope": {"kind": "project"},
                "params": {
                    "sheet_id": sheet,
                    "source_columns": ["headline"],
                    "modality": "text",
                    "provider": "fastembed",
                    "source_policy": {"kind": "text_cell"},
                    "provider_policy": {"allow_remote": False},
                },
                "idempotency_key": "create@index",
            },
            project_id=project_id,
        )
        assert create.status == "completed", create.errors
        index_id = create.outputs[0].ref["index_id"]

        result = queue_v1_action_run(
            project,
            project_id,
            {
                "action_id": "embedding.index_refresh",
                "scope": {"kind": "project"},
                "params": {
                    "index_id": index_id,
                    "mode": "incremental",
                },
                "idempotency_key": "manual-refresh@1",
            },
            ctx=QueuedV1ActionRunContext(
                queue=queue,
                workspace_root=workspace,
                router_for=lambda _project: ModelRouter(keys={}),
                execution_composition_for=open_execution_composition,
                active_runs={},
                run_jobs={},
                queue_payload_extra={},
                logger=logging.getLogger("test"),
            ),
            execution_context=ExecutionCompositionContext.direct(),
        )

        assert result is not None
        assert result.status == "queued"
        assert result.run_id is None
        assert result.job_id is not None
        [job] = queue.list_project_jobs(project_id, kind=ACTION_RUN_KIND)
        assert job.run_id is None
        assert job.action_kind == "embedding.index_refresh"
        assert job.receipt_id == result.receipt_id
        assert job.payload["action_job"]["action_kind"] == "embedding.index_refresh"
        assert job.payload["action_job"]["receipt_id"] == result.receipt_id
    finally:
        queue.close()
        project.close()
