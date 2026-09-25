"""Durable Parakeet engine setup dispatch without live downloads."""

from __future__ import annotations

import pytest

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


@pytest.mark.parametrize("readiness", ["ready", "failed", "cancelled"])
def test_docling_engine_setup_waits_for_server_before_marking_done(
    tmp_path, monkeypatch, readiness
) -> None:
    queue = open_queue(workspace=tmp_path)
    try:
        row, _ = store.create_or_get_active(
            queue.engine,
            workspace_root=str(tmp_path),
            model_ref=engine_setup.DOCLING_SETUP_REF,
        )
        seen = {}

        def fake_install_docling(*, should_cancel, progress):
            seen["cancelled"] = should_cancel()
            progress("installing")

        monkeypatch.setattr(
            "frisket.runtime.model_install.install_docling", fake_install_docling
        )
        monkeypatch.setenv("FRISKET_LOCAL_MODELS_URL", "http://127.0.0.1:1234")
        monkeypatch.setenv("FRISKET_LOCAL_MODELS_TOKEN", "private-test-token")
        cancelled = False

        def wait_until_ready(url, token, *, stopped):
            nonlocal cancelled
            assert url == "http://127.0.0.1:1234"
            assert token == "private-test-token"
            assert seen == {"cancelled": False}
            assert store.get(queue.engine, row.id).status != store.STATUS_DONE
            cancelled = readiness == "cancelled"
            return readiness == "ready"

        monkeypatch.setattr(
            "frisket.runtime.model_server.wait_until_ready", wait_until_ready
        )
        kwargs = dict(
            engine=queue.engine,
            pull_id=row.id,
            should_cancel=lambda: cancelled,
            is_final_attempt=True,
        )
        if readiness == "failed":
            with pytest.raises(RuntimeError):
                engine_setup.run_engine_setup(**kwargs)
            assert store.get(queue.engine, row.id).status == store.STATUS_FAILED
        else:
            result = engine_setup.run_engine_setup(**kwargs)
            expected = "done" if readiness == "ready" else "cancelled"
            assert result == {"status": expected}
            assert store.get(queue.engine, row.id).status == expected

        assert seen == {"cancelled": False}
    finally:
        queue.close()
