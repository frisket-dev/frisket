"""Typed semantic-join queue: consent, publication, and writer fences."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frisket.contracts.action import ActionResult
from frisket.ai.llm import ModelRouter
from frisket.engine.runner import CostGate, MapRunner
from frisket.server.app import create_app
from http_test_helpers import drain_queue


def _post_join(
    client,
    project_id,
    source_sheet_id,
    target_sheet_id,
    *,
    output_name="semantic_match",
    child_sheet_name="Semantic Links",
    confirmation=None,
    idempotency_key,
):
    request = {
        "action_id": "join.semantic",
        "scope": {"kind": "sheet_rows", "sheet_id": source_sheet_id},
        "params": {
            "source": "donor",
            "target": {"sheet_id": target_sheet_id, "column": "company"},
        },
        "output_names": {
            "source": "donor",
            "match_value": f"{output_name}_value",
            "match_score": f"{output_name}_score",
            "matched_row_id": f"{output_name}_row_id",
        },
        "sheet_name": child_sheet_name,
        "idempotency_key": idempotency_key,
    }
    if confirmation is not None:
        request["confirmation"] = confirmation
    return client.post(f"/api/projects/{project_id}/actions/v1/run", json=request)


# unit vectors chosen so cosines are exact by construction (test_semantic_join.py)
VECS = {
    "Acme Corporation": [1.0, 0.0, 0.0],
    "Globex LLC": [0.0, 1.0, 0.0],
    "Initech Inc": [0.0, 0.0, 1.0],
    "ACME Corp": [1.0, 0.0, 0.0],
    "Globex": [0.6258, 0.78, 0.0],
    "Umbrella Holdings": [0.5774, 0.5774, 0.5774],
}


def _fake_embed(texts: list[str]) -> list[list[float]]:
    return [VECS[t] for t in texts]


@pytest.fixture(autouse=True)
def _stub_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "frisket.semantic.resolve_embedder",
        lambda router=None, **_kw: (_fake_embed, "stub/test-v1"),
    )


def _client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(
            tmp_path / "ws",
            router=ModelRouter(cache=None, cache_mode="off"),
            run_status_grace_seconds=3600.0,
        )
    )


def _seed_project(client: TestClient) -> tuple[str, int, int]:
    project_id = client.post(
        "/api/projects", json={"name": "Join semantic queued"}
    ).json()["id"]
    donors = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={
            "file": (
                "donors.csv",
                "donor\nACME Corp\nGlobex\nUmbrella Holdings\n",
                "text/csv",
            )
        },
    )
    registry = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={
            "file": (
                "registry.csv",
                "company\nAcme Corporation\nGlobex LLC\nInitech Inc\n",
                "text/csv",
            )
        },
    )
    assert donors.status_code == 200, donors.text
    assert registry.status_code == 200, registry.text
    return project_id, donors.json()["sheet_id"], registry.json()["sheet_id"]


# --- (b) full request/worker round trip -------------------------------------


def test_join_semantic_queued_launch_returns_run_handle_before_worker_executes(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    project_id, source_sheet_id, target_sheet_id = _seed_project(client)
    idempotency_key = "join-semantic-queued-launch@sha256:stable"

    gated = _post_join(
        client,
        project_id,
        source_sheet_id,
        target_sheet_id,
        output_name="semantic_match",
        child_sheet_name="Semantic Links",
        idempotency_key=idempotency_key,
    )
    assert gated.status_code == 402, gated.text
    promise_set_hash = gated.json()["errors"][0]["details"]["promise_set_hash"]

    response = _post_join(
        client,
        project_id,
        source_sheet_id,
        target_sheet_id,
        output_name="semantic_match",
        child_sheet_name="Semantic Links",
        confirmation=promise_set_hash,
        idempotency_key=idempotency_key,
    )
    assert response.status_code == 200, response.text
    result = ActionResult.model_validate(response.json())

    # HARD CONSTRAINT: launch returns the run handle immediately; the
    # semantic_join MapRunner run and the derive child-sheet materialize have
    # not executed yet.
    assert result.status == "queued"
    assert result.action.kind == "join.semantic"
    assert result.run_id is not None
    assert result.job_id is not None
    assert result.receipt_id is not None

    project = client.app.state.workspace.get(project_id)
    linkage_before = project.db.execute(
        "SELECT id FROM sheets WHERE name=?", ("Semantic Links",)
    ).fetchone()
    assert linkage_before is None

    job = client.app.state.workspace.queue.get(result.job_id)
    assert job is not None
    assert job.payload["action_kind"] == "join.semantic"
    assert job.payload["run_id"] == result.run_id
    assert "v1_semantic_join" in job.payload

    status_before = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    ).json()["run"]["public_status"]
    assert status_before["status"] == "queued"

    # Execution happens on the worker, not the request.
    drain_queue(client)

    status_after = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    ).json()["run"]["public_status"]
    assert status_after["status"] == "completed", client.app.state.workspace.queue.get(
        result.job_id
    ).error

    receipt_row = project.db.execute(
        "SELECT status FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    assert receipt_row["status"] == "completed"

    # The worker stamps worker_version on any run it claims
    # (worker-version-guard-v1, jobs/runs.py, unconditional and unmodified by
    # this task) -- proves the join now runs through that generic path.
    run_row = project.db.execute(
        "SELECT worker_version FROM runs WHERE id=?", (result.run_id,)
    ).fetchone()
    assert run_row is not None
    assert run_row["worker_version"]

    linkage = project.db.execute(
        "SELECT id FROM sheets WHERE name=?", ("Semantic Links",)
    ).fetchone()
    assert linkage is not None
    data = client.get(f"/api/projects/{project_id}/sheets/{linkage['id']}/data").json()
    assert data["total"] == 2
    cols = {c["name"] for c in data["columns"]}
    assert cols == {
        "donor",
        "semantic_match_value",
        "semantic_match_score",
        "semantic_match_row_id",
    }


def test_queued_join_semantic_terminal_fence_blocks_replaced_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(tmp_path)
    project_id, source_sheet_id, target_sheet_id = _seed_project(client)
    idempotency_key = "join-semantic-queued-stale-terminal@sha256:stable"
    gated = _post_join(
        client,
        project_id,
        source_sheet_id,
        target_sheet_id,
        output_name="semantic_stale_match",
        child_sheet_name="Stale Semantic Links",
        idempotency_key=idempotency_key,
    )
    assert gated.status_code == 402, gated.text
    promise_set_hash = gated.json()["errors"][0]["details"]["promise_set_hash"]
    launched_response = _post_join(
        client,
        project_id,
        source_sheet_id,
        target_sheet_id,
        output_name="semantic_stale_match",
        child_sheet_name="Stale Semantic Links",
        confirmation=promise_set_hash,
        idempotency_key=idempotency_key,
    )
    assert launched_response.status_code == 200, launched_response.text
    launched = ActionResult.model_validate(launched_response.json())
    assert launched.status == "queued"
    assert launched.run_id is not None
    assert launched.job_id is not None
    assert launched.receipt_id is not None

    real_run = MapRunner.run
    observed: dict[str, object] = {}

    async def replace_after_deferred_map_run(self, spec, **kwargs):
        progress = await real_run(self, spec, **kwargs)
        if spec.get("action_kind") != "join.semantic":
            return progress
        assert kwargs.get("defer_attempt_close") is True
        assert progress.writer_attempt_id is not None
        observed["attempt_a"] = progress.writer_attempt_id
        observed["run_status_before_terminal"] = self.project.db.execute(
            "SELECT status, completed_rows, failed_rows, finished_at "
            "FROM runs WHERE id=?",
            (progress.run_id,),
        ).fetchone()
        attempt = self.project.db.execute(
            "SELECT seq FROM execution_attempts WHERE id=?",
            (progress.writer_attempt_id,),
        ).fetchone()
        assert attempt is not None
        self.project.db.execute("BEGIN IMMEDIATE")
        self.project.db.execute(
            "UPDATE execution_attempts SET state='abandoned' WHERE id=?",
            (progress.writer_attempt_id,),
        )
        self.project.db.execute(
            "INSERT INTO execution_attempts "
            "(id, run_id, seq, state, action_identity_hash, scope_json, "
            "created_at) VALUES "
            "('attempt_join_terminal_b', ?, ?, 'dispatching', "
            "'test-replacement', '[]', datetime('now'))",
            (progress.run_id, int(attempt["seq"]) + 1),
        )
        self.project.db.execute(
            "UPDATE runs SET current_attempt_id='attempt_join_terminal_b' WHERE id=?",
            (progress.run_id,),
        )
        self.project.db.commit()
        return progress

    monkeypatch.setattr(MapRunner, "run", replace_after_deferred_map_run)
    drain_queue(client)

    workspace = client.app.state.workspace
    project = workspace.get(project_id)
    job = workspace.queue.get(launched.job_id)
    assert job is not None
    assert job.status == "done", job
    assert job.result is not None
    assert job.result["status"] == "stale_attempt_writer"
    assert job.result["error"]["code"] == "stale_attempt_writer"
    assert (
        project.db.execute(
            "SELECT id FROM sheets WHERE name='Stale Semantic Links'"
        ).fetchone()
        is None
    )
    receipt = project.db.execute(
        "SELECT status FROM receipts WHERE id=?",
        (launched.receipt_id,),
    ).fetchone()
    assert receipt is not None
    # join.semantic's queued receipt stays 'queued' through the whole worker
    # run (per _finalize_queued_join_semantic: requiring the direct path's
    # 'running' state made finalization roll back the child sheet and strand
    # the receipt/claim tuple even after the run completed). The fenced
    # writer replacement here means finalize never reached its terminal
    # write, so the receipt is still exactly where launch left it.
    assert receipt["status"] == "queued"
    claims = project.db.execute(
        "SELECT status FROM output_column_claims WHERE receipt_id=?",
        (launched.receipt_id,),
    ).fetchall()
    assert claims
    assert {row["status"] for row in claims} == {"active"}
    run = project.db.execute(
        "SELECT status, completed_rows, failed_rows, finished_at, "
        "current_attempt_id FROM runs WHERE id=?",
        (launched.run_id,),
    ).fetchone()
    before = observed["run_status_before_terminal"]
    assert run is not None and before is not None
    assert tuple(run[key] for key in before.keys()) == tuple(before)
    assert run["current_attempt_id"] == "attempt_join_terminal_b"
    assert (
        project.db.execute(
            "SELECT state FROM execution_attempts WHERE id='attempt_join_terminal_b'"
        ).fetchone()[0]
        == "dispatching"
    )


@pytest.mark.parametrize(
    "replace_writer",
    (False, True),
    ids=("current-writer", "replaced-writer"),
)
def test_queued_provider_failure_uses_join_terminal_writer_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replace_writer: bool,
) -> None:
    """A promoted provider error terminalizes only through the live writer."""

    from frisket.ai.llm import LLMError
    from frisket.semantic import REMOTE_EMBED_MODEL
    from frisket.team.security.secrets import encrypt_secret

    class FailingRemoteEmbedder:
        async def embed_batch_async(self, _texts):
            raise LLMError("semantic embed rate limited", status=429, retryable=True)

    monkeypatch.setattr(
        "frisket.semantic.resolve_embedder",
        lambda router=None, **_kw: (FailingRemoteEmbedder(), REMOTE_EMBED_MODEL),
    )
    client = _client(tmp_path)
    project_id, source_sheet_id, target_sheet_id = _seed_project(client)
    project = client.app.state.workspace.get(project_id)
    project.set_provider_key(
        provider="openai",
        encrypted=encrypt_secret("sk-project-semantic"),
        hint="sk-p***",
        spend_cap_micro=50_000,
    )
    idempotency_key = f"join-semantic-provider-terminal-{replace_writer}@sha256:stable"
    gated = _post_join(
        client,
        project_id,
        source_sheet_id,
        target_sheet_id,
        output_name="semantic_provider_match",
        child_sheet_name="Provider Failure Links",
        idempotency_key=idempotency_key,
    )
    assert gated.status_code == 402, gated.text
    promise_set_hash = gated.json()["errors"][0]["details"]["promise_set_hash"]
    launched_response = _post_join(
        client,
        project_id,
        source_sheet_id,
        target_sheet_id,
        output_name="semantic_provider_match",
        child_sheet_name="Provider Failure Links",
        confirmation=promise_set_hash,
        idempotency_key=idempotency_key,
    )
    assert launched_response.status_code == 200, launched_response.text
    launched = ActionResult.model_validate(launched_response.json())
    assert launched.status == "queued"
    assert launched.run_id is not None
    assert launched.job_id is not None
    assert launched.receipt_id is not None

    real_run = MapRunner.run
    observed: dict[str, object] = {}

    async def replace_after_deferred_map_run(self, spec, **kwargs):
        progress = await real_run(self, spec, **kwargs)
        if spec.get("action_kind") != "join.semantic":
            return progress
        assert kwargs.get("defer_attempt_close") is True
        assert progress.writer_attempt_id is not None
        observed["attempt_a"] = progress.writer_attempt_id
        observed["run_status_before_terminal"] = self.project.db.execute(
            "SELECT status, completed_rows, failed_rows, finished_at "
            "FROM runs WHERE id=?",
            (progress.run_id,),
        ).fetchone()
        if replace_writer:
            attempt = self.project.db.execute(
                "SELECT seq FROM execution_attempts WHERE id=?",
                (progress.writer_attempt_id,),
            ).fetchone()
            assert attempt is not None
            self.project.db.execute("BEGIN IMMEDIATE")
            self.project.db.execute(
                "UPDATE execution_attempts SET state='abandoned' WHERE id=?",
                (progress.writer_attempt_id,),
            )
            self.project.db.execute(
                "INSERT INTO execution_attempts "
                "(id, run_id, seq, state, action_identity_hash, scope_json, "
                "created_at) VALUES "
                "('attempt_join_provider_b', ?, ?, 'dispatching', "
                "'test-replacement', '[]', datetime('now'))",
                (progress.run_id, int(attempt["seq"]) + 1),
            )
            self.project.db.execute(
                "UPDATE runs SET current_attempt_id='attempt_join_provider_b' "
                "WHERE id=?",
                (progress.run_id,),
            )
            self.project.db.commit()
        return progress

    monkeypatch.setattr(MapRunner, "run", replace_after_deferred_map_run)
    drain_queue(client)

    job = client.app.state.workspace.queue.get(launched.job_id)
    assert job is not None
    assert job.status == "done", job
    assert job.result is not None
    receipt = project.db.execute(
        "SELECT status, body FROM receipts WHERE id=?",
        (launched.receipt_id,),
    ).fetchone()
    assert receipt is not None
    claims = project.db.execute(
        "SELECT status FROM output_column_claims WHERE receipt_id=? ORDER BY column_id",
        (launched.receipt_id,),
    ).fetchall()
    assert claims
    child = project.db.execute(
        "SELECT id FROM sheets WHERE name='Provider Failure Links'"
    ).fetchone()
    assert child is None

    if replace_writer:
        assert job.result["status"] == "stale_attempt_writer"
        assert job.result["error"]["code"] == "stale_attempt_writer"
        # join.semantic's queued receipt stays 'queued' through the whole
        # worker run (see the sibling terminal-fence test); the fenced
        # writer replacement means finalize never reached its terminal
        # write, so the receipt is still exactly where launch left it.
        assert receipt["status"] == "queued"
        assert {row["status"] for row in claims} == {"active"}
        run = project.db.execute(
            "SELECT status, completed_rows, failed_rows, finished_at, "
            "current_attempt_id FROM runs WHERE id=?",
            (launched.run_id,),
        ).fetchone()
        before = observed["run_status_before_terminal"]
        assert run is not None and before is not None
        assert tuple(run[key] for key in before.keys()) == tuple(before)
        assert run["current_attempt_id"] == "attempt_join_provider_b"
        assert (
            project.db.execute(
                "SELECT state FROM execution_attempts "
                "WHERE id='attempt_join_provider_b'"
            ).fetchone()[0]
            == "dispatching"
        )
    else:
        assert job.result["status"] == "failed"
        assert (
            job.result["action_result"]["errors"][0]["code"] == "provider_rate_limited"
        )
        assert receipt["status"] == "failed"
        assert {row["status"] for row in claims} == {"failed"}
        run = project.db.execute(
            "SELECT status, current_attempt_id FROM runs WHERE id=?",
            (launched.run_id,),
        ).fetchone()
        assert run is not None
        assert run["status"] == "failed"
        assert run["current_attempt_id"] is None
        assert (
            project.db.execute(
                "SELECT state FROM execution_attempts WHERE id=?",
                (observed["attempt_a"],),
            ).fetchone()[0]
            == "effected"
        )


# --- (c) request-time cost-confirmation gate: fires with zero runs created --


def test_join_semantic_queued_cost_gate_fires_before_any_run_is_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The cost gate lives inside MapRunner.prepare_run() -- called at LAUNCH
    # time, before the queued receipt/run row commit
    # (server.action_enqueue._queue_v1_project_run_request).
    full_estimate = {
        "cost": None,
        "cost_source": "unknown",
        "embedder": "openai/text-embedding-3-small",
        "requires_confirmation": True,
    }
    promise_set_hash = "a" * 64
    original_prepare_run = MapRunner.prepare_run
    seen_attempts: list[tuple[bool, str | None]] = []

    def gated_prepare_run(
        self: MapRunner, spec: dict, *, confirmed: bool = False, **kwargs
    ):
        echoed_hash = spec.get("consented_promise_set_hash")
        seen_attempts.append((confirmed, echoed_hash))
        if confirmed and echoed_hash == promise_set_hash:
            return original_prepare_run(self, spec, confirmed=confirmed, **kwargs)
        gate = CostGate(None, estimate_details=full_estimate)
        gate.promise_set_hash = promise_set_hash
        raise gate

    monkeypatch.setattr(MapRunner, "prepare_run", gated_prepare_run)
    monkeypatch.setattr(
        "frisket.semantic.embedder_is_remote",
        lambda model_id: False,
    )

    client = _client(tmp_path)
    project_id, source_sheet_id, target_sheet_id = _seed_project(client)
    idempotency_key = "join-semantic-confirmation-echo@sha256:stable"

    response = _post_join(
        client,
        project_id,
        source_sheet_id,
        target_sheet_id,
        idempotency_key=idempotency_key,
    )

    assert response.status_code == 402, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "needs_confirmation"
    assert result.run_id is None
    assert result.job_id is None
    assert result.errors[0].code == "model_cost_requires_confirmation"
    assert result.errors[0].needs_confirmation is True
    assert result.errors[0].details == {
        "reason": "unknown_estimate",
        "estimate": full_estimate,
        "promise_set_hash": promise_set_hash,
    }

    project = client.app.state.workspace.get(project_id)
    run_count = project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert run_count == 0
    # _seed_project's two import.csv actions each persist their own receipt;
    # scope the "no queued reservation left behind" check to join.semantic.
    join_receipt_count = project.db.execute(
        "SELECT COUNT(*) FROM receipts WHERE action_kind=?", ("join.semantic",)
    ).fetchone()[0]
    assert join_receipt_count == 0

    wrong_confirmation = _post_join(
        client,
        project_id,
        source_sheet_id,
        target_sheet_id,
        confirmation="b" * 64,
        idempotency_key=idempotency_key,
    )
    assert wrong_confirmation.status_code == 402, wrong_confirmation.text
    assert (
        wrong_confirmation.json()["errors"][0]["details"]["promise_set_hash"]
        == promise_set_hash
    )

    accepted = _post_join(
        client,
        project_id,
        source_sheet_id,
        target_sheet_id,
        confirmation=promise_set_hash,
        idempotency_key=idempotency_key,
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "queued"
    assert seen_attempts == [
        (False, None),
        (True, "b" * 64),
        (True, promise_set_hash),
    ]


# --- (d) idempotency replay: a second post with the same key doesn't re-run -


def test_join_semantic_queued_idempotency_replay_after_completion(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    project_id, source_sheet_id, target_sheet_id = _seed_project(client)

    key = "join_semantic_queued_replay@sha256:v1"
    gated = _post_join(
        client,
        project_id,
        source_sheet_id,
        target_sheet_id,
        output_name="semantic_match",
        child_sheet_name="Semantic Links",
        idempotency_key=key,
    )
    assert gated.status_code == 402, gated.text
    promise_set_hash = gated.json()["errors"][0]["details"]["promise_set_hash"]

    first = _post_join(
        client,
        project_id,
        source_sheet_id,
        target_sheet_id,
        output_name="semantic_match",
        child_sheet_name="Semantic Links",
        confirmation=promise_set_hash,
        idempotency_key=key,
    )
    assert first.status_code == 200, first.text
    first_result = ActionResult.model_validate(first.json())
    assert first_result.status == "queued"

    drain_queue(client)

    project = client.app.state.workspace.get(project_id)
    sheet_count_after_first = project.db.execute(
        "SELECT COUNT(*) FROM sheets WHERE name=?", ("Semantic Links",)
    ).fetchone()[0]
    assert sheet_count_after_first == 1

    second = _post_join(
        client,
        project_id,
        source_sheet_id,
        target_sheet_id,
        output_name="semantic_match",
        child_sheet_name="Semantic Links",
        idempotency_key=key,
    )
    assert second.status_code == 200, second.text
    second_result = ActionResult.model_validate(second.json())
    assert second_result.status == "completed"
    assert second_result.receipt_id == first_result.receipt_id

    # Replay does not create a second run or a second child sheet.
    sheet_count_after_second = project.db.execute(
        "SELECT COUNT(*) FROM sheets WHERE name=?", ("Semantic Links",)
    ).fetchone()[0]
    assert sheet_count_after_second == 1


@pytest.mark.parametrize("side", ["source", "target"])
def test_queued_semantic_join_refuses_changed_inputs_before_embedding(
    tmp_path, monkeypatch, side
):
    client = _client(tmp_path)
    project_id, source, target = _seed_project(client)
    key = f"semantic-queued-stale-{side}"
    gated = _post_join(client, project_id, source, target, idempotency_key=key)
    assert gated.status_code == 402, gated.text
    response = _post_join(
        client,
        project_id,
        source,
        target,
        idempotency_key=key,
        confirmation=gated.json()["errors"][0]["details"]["promise_set_hash"],
    )
    assert response.status_code == 200, response.text
    launched = ActionResult.model_validate(response.json())
    project = client.app.state.workspace.get(project_id)
    sheet = source if side == "source" else target
    column = project.columns(sheet)[0]
    project.apply_edits(
        [
            {
                "row_id": project.visible_row_ids(sheet)[0],
                "column_id": column["id"],
                "value": "Changed since queue admission",
            }
        ],
        label="change semantic input",
    )

    def forbid_embedding(*args, **kwargs):
        pytest.fail("stale queued inputs must refuse before embedding")

    monkeypatch.setattr(
        "frisket.semantic.resolve_embedder",
        lambda *args, **kwargs: (forbid_embedding, "stub/test-v1"),
    )
    drain_queue(client)
    job = client.app.state.workspace.queue.get(launched.job_id)
    assert job.status == "done", job.error
    assert job.result["status"] == "failed", job.result
    assert job.result["action_result"]["errors"][0]["code"] == "stale_input"
    assert not any(sheet["name"] == "Semantic Links" for sheet in project.sheets())
    assert (
        project.db.execute("SELECT COUNT(*) FROM cell_result_heads").fetchone()[0] == 0
    )
