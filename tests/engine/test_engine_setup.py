"""Durable Parakeet engine setup dispatch without live downloads."""

from __future__ import annotations

from frisket.engine.jobs import engine_setup, model_pull_store as store
from frisket.engine.jobs.queue import open_queue


def test_engine_setup_uses_existing_row_and_marks_done(tmp_path, monkeypatch) -> None:
    queue = open_queue(workspace=tmp_path)
    try:
        row, created = store.create_or_get_active(
            queue.engine,
            workspace_root=str(tmp_path),
            model_ref=engine_setup.PARAKEET_TDT_SETUP_REF,
        )
        assert created
        store.mark_running(queue.engine, row.id, job_id=1)
        seen: dict[str, object] = {}

        async def fake_resolve(*, vad: bool, should_cancel):
            seen["vad"] = vad
            seen["cancelled"] = should_cancel()
            return object()

        monkeypatch.setattr(
            "frisket.engine._workers.parakeet_artifacts.resolve_parakeet_artifacts",
            fake_resolve,
        )
        result = engine_setup.run_engine_setup(
            engine=queue.engine,
            pull_id=row.id,
            should_cancel=lambda: False,
            is_final_attempt=True,
        )

        assert result == {"status": "done"}
        assert seen == {"vad": True, "cancelled": False}
        completed = store.get(queue.engine, row.id)
        assert completed is not None
        assert completed.status == store.STATUS_DONE
        assert completed.artifact_kind == "engine_setup"
    finally:
        queue.close()


def test_engine_setup_cancel_is_terminal_without_resolver(
    tmp_path, monkeypatch
) -> None:
    queue = open_queue(workspace=tmp_path)
    try:
        row, _ = store.create_or_get_active(
            queue.engine,
            workspace_root=str(tmp_path),
            model_ref=engine_setup.PARAKEET_TDT_SETUP_REF,
        )

        async def should_not_resolve(**_kwargs):
            raise AssertionError("cancelled setup must not start provisioning")

        monkeypatch.setattr(
            "frisket.engine._workers.parakeet_artifacts.resolve_parakeet_artifacts",
            should_not_resolve,
        )
        result = engine_setup.run_engine_setup(
            engine=queue.engine,
            pull_id=row.id,
            should_cancel=lambda: True,
            is_final_attempt=True,
        )

        assert result == {"status": "cancelled"}
        assert store.get(queue.engine, row.id).status == store.STATUS_CANCELLED
    finally:
        queue.close()
