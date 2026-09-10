"""Evidence publication failure settles paid work without authorizing repurchase."""

import asyncio

import pytest

from executor_harness import run_action_with_confirmation
from test_grounded_map_extract_executor import (
    PROJECT_ID,
    _grounded_reply,
    _map_extract_action,
    _seed_document_project,
    _stub_router,
)

from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.ai.llm import ModelRouter
from frisket.execution.attempt import StaleAttemptWriter
from frisket.engine.runner.map_runner import ResultEvidenceWriteFailed
from frisket.team.security.secrets import encrypt_secret, key_hint


def _invocation(tmp_path):
    path, sheet_id, _ = _seed_document_project(tmp_path, "evidence-settlement")
    _, adapter = _stub_router(_grounded_reply())
    router = ModelRouter(
        keys={"anthropic": "test-key"},
        key_sources={"anthropic": "project_key"},
        cache=None,
        cache_mode="off",
    )
    router._adapters["anthropic"] = adapter
    request = _map_extract_action(sheet_id, key="evidence-settlement")
    project = Project(path)
    project.set_provider_key(
        provider="anthropic",
        encrypted=encrypt_secret("test-key"),
        hint=key_hint("test-key"),
        spend_cap_micro=1_000_000,
    )
    return project, router, adapter, request


@pytest.mark.parametrize("failure", [RuntimeError, ValueError])
def test_evidence_failure_keeps_failed_receipt_and_paid_facts(
    tmp_path, monkeypatch, failure
):
    project, router, adapter, request = _invocation(tmp_path)
    record = ReceiptStore._record_writer_evidence
    grounding_link_counts = []

    def fail_evidence(self, ref, **kwargs):
        if ref.get("kind") == "map_extract_grounding_links":
            grounding_link_counts.append(
                self.db.execute("SELECT COUNT(*) FROM evidence_links").fetchone()[0]
            )
            raise failure("private tenant value must not escape")
        return record(self, ref, **kwargs)

    monkeypatch.setattr(ReceiptStore, "_record_writer_evidence", fail_evidence)
    try:
        result = run_action_with_confirmation(
            project, request, project_id=PROJECT_ID, router=router
        )
        assert result.status == "failed"
        assert grounding_link_counts and min(grounding_link_counts) > 0
        assert result.receipt_id is not None
        assert result.run_id is not None
        assert [error.code for error in result.errors] == ["project_write_failed"]
        assert result.errors[0].message == "project write failed"
        assert "private tenant" not in result.model_dump_json()
        assert len(adapter.requests) == 1
        run = project.db.execute("SELECT * FROM runs").fetchone()
        assert run["status"] == "failed"
        assert run["finished_at"] is not None
        assert run["current_attempt_id"] is None
        assert run["cost_actual"] == pytest.approx(0.008)
        assert project.provider_spend_state("anthropic").spent_micro == 8000
        assert project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 1
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM output_column_claims WHERE status='active'"
            ).fetchone()[0]
            == 0
        )
        assert {
            row[0]
            for row in project.db.execute("SELECT state FROM run_output_generations")
        } == {"sealed"}
        for table in (
            "results",
            "cell_result_heads",
            "source_artifacts",
            "source_spans",
            "evidence_links",
            "evidence_link_spans",
        ):
            assert (
                project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            )
        checkpoint = project.db.execute("SELECT * FROM effect_checkpoints").fetchone()
        assert checkpoint["state"] == "returned"
        assert (
            checkpoint["authorized_attempt_id"]
            == project.db.execute("SELECT id FROM execution_attempts").fetchone()[0]
        )
        assert (
            project.db.execute("SELECT state FROM execution_attempts").fetchone()[0]
            == "effected"
        )

        monkeypatch.setattr(ReceiptStore, "_record_writer_evidence", record)
        before = tuple(project.db.iterdump())
        replay = run_action_with_confirmation(
            project, request, project_id=PROJECT_ID, router=router
        )
        assert replay.model_dump() == result.model_dump()
        assert len(adapter.requests) == 1
        assert tuple(project.db.iterdump()) == before
    finally:
        project.close()


class ProcessDeath(BaseException):
    pass


def test_evidence_failure_cannot_close_an_ambiguous_sibling_effect(
    tmp_path, monkeypatch
):
    from frisket.engine.executor.actions import _default_map_runner_factory
    from frisket.engine.executor.action_inventory import ExecutorDeps

    def concurrent_runner(project, router):
        runner = _default_map_runner_factory(project, router)
        runner.concurrency = 2
        return runner

    deps = ExecutorDeps(map_runner_factory=concurrent_runner)
    project, router, adapter, request = _invocation(tmp_path)
    record = ReceiptStore._record_writer_evidence
    sheet_id = project.db.execute("SELECT id FROM sheets").fetchone()[0]
    project.add_rows(
        sheet_id,
        [{"filename": "second.pdf", "document_text": "Second contract"}],
        {column["name"]: column["id"] for column in project.columns(sheet_id)},
    )
    project.db.commit()
    sibling_started = asyncio.Event()
    calls = []

    class ConcurrentProvider:
        async def complete(self, req, client):
            calls.append(req)
            if len(calls) == 1:
                await asyncio.wait_for(sibling_started.wait(), timeout=5)
                return await adapter.complete(req, client)
            sibling_started.set()
            # Cancellation while egress is pending cannot prove whether the
            # remote provider charged. The reserved checkpoint must survive.
            await asyncio.Future()

    router._adapters["anthropic"] = ConcurrentProvider()

    def fail_evidence(self, ref, **kwargs):
        if ref.get("kind") == "map_extract_grounding_links":
            raise RuntimeError("evidence storage unavailable")
        return record(self, ref, **kwargs)

    monkeypatch.setattr(ReceiptStore, "_record_writer_evidence", fail_evidence)
    try:
        with pytest.raises(ResultEvidenceWriteFailed):
            run_action_with_confirmation(
                project, request, project_id=PROJECT_ID, router=router, deps=deps
            )
        assert len(calls) == 2
        assert {
            row[0] for row in project.db.execute("SELECT state FROM effect_checkpoints")
        } == {"returned", "reserved"}
        assert project.db.execute("SELECT status FROM runs").fetchone()[0] == "running"
        assert (
            project.db.execute("SELECT status FROM receipts").fetchone()[0] == "running"
        )
        assert (
            project.db.execute("SELECT state FROM execution_attempts").fetchone()[0]
            == "dispatching"
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM output_column_claims WHERE status='active'"
            ).fetchone()[0]
            > 0
        )
        before = tuple(project.db.iterdump())
        replay = run_action_with_confirmation(
            project, request, project_id=PROJECT_ID, router=router
        )
        assert [error.code for error in replay.errors] == ["idempotency_in_progress"]
        assert len(calls) == 2
        assert tuple(project.db.iterdump()) == before
    finally:
        project.close()


@pytest.mark.parametrize("failure", [ProcessDeath, StaleAttemptWriter])
def test_interruption_or_stale_evidence_writer_retains_recovery_authority(
    tmp_path, monkeypatch, failure
):
    project, router, adapter, request = _invocation(tmp_path)
    record = ReceiptStore._record_writer_evidence

    def interrupt(self, ref, **kwargs):
        if ref.get("kind") == "map_extract_grounding_links":
            raise failure("injected interruption")
        return record(self, ref, **kwargs)

    monkeypatch.setattr(ReceiptStore, "_record_writer_evidence", interrupt)
    try:
        if failure is ProcessDeath:
            with pytest.raises(ProcessDeath):
                run_action_with_confirmation(
                    project, request, project_id=PROJECT_ID, router=router
                )
        else:
            result = run_action_with_confirmation(
                project, request, project_id=PROJECT_ID, router=router
            )
            assert [error.code for error in result.errors] == ["stale_attempt_writer"]
        assert len(adapter.requests) == 1
        assert project.db.execute("SELECT status FROM runs").fetchone()[0] == "running"
        assert (
            project.db.execute("SELECT status FROM receipts").fetchone()[0] == "running"
        )
        assert (
            project.db.execute("SELECT state FROM execution_attempts").fetchone()[0]
            == "dispatching"
        )
        assert (
            project.db.execute("SELECT state FROM effect_checkpoints").fetchone()[0]
            == "returned"
        )
        assert (
            project.db.execute("SELECT COUNT(*) FROM evidence_links").fetchone()[0] == 0
        )
    finally:
        project.close()
