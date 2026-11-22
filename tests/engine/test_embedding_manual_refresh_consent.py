"""Cost estimation, standing allowance, and explicit consent for manual remote embedding refresh."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest

from frisket.ai.embeddings import EmbeddingStore, build_batch_result
from frisket.ai.llm import ModelRouter
from frisket.authoring.action_metadata import PRODUCT_EDITION_SNAPSHOT_KEY
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.jobs.embeddings import register_embedding_refresh_handler
from frisket.engine.jobs.worker import HandlerRegistry, Worker
from frisket.execution.consent_coverage import ConsentCoverage
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY
from frisket.retrieval.store import RetrievalStore
from frisket.server.app import create_app


class _RecordingGateway:
    def __init__(self, *, dim: int = 1536, provider_kind: str = "platform_api"):
        self.dim = dim
        self.provider_kind = provider_kind
        self.calls: list[tuple[list[str], str, str, str]] = []

    def embed(
        self,
        texts: list[str],
        *,
        provider: str,
        model: str,
        modality: str = "text",
    ) -> dict[str, Any]:
        self.calls.append((list(texts), provider, model, modality))
        return build_batch_result(
            vectors=[[1.0] + [0.0] * (self.dim - 1) for _ in texts],
            provider_id=provider,
            provider_kind=self.provider_kind,
            requested_model=model,
            actual_model_id=model,
            modality=modality,
        )


def _create_app_client(
    tmp_path: Path,
    *,
    edition: str = "solo",
    allowance_usd: str | None = None,
) -> TestClient:
    keys = {"openai": "test-openai-key", "openrouter": "test-openrouter-key"}
    router = ModelRouter(model_call_policy=LOCAL_PURCHASE_POLICY, keys=keys)
    deps_factory = None
    if allowance_usd is not None:
        coverage = ConsentCoverage("user:test-actor", Decimal(allowance_usd))

        def _deps(_project_id: str, _request_context: Any) -> ExecutorDeps:
            return ExecutorDeps(consent_coverage=coverage)

        deps_factory = _deps
    app = create_app(
        tmp_path / f"ws-{edition}",
        router=router,
        edition=edition,
        executor_deps_factory=deps_factory,
    )
    return TestClient(app)


def _seed_sheet(client: TestClient, rows: list[str]) -> tuple[str, Any, int, int]:
    pid = client.post("/api/projects", json={"name": "embed-consent"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("Articles")
    col_id = project.add_column(sheet_id, "headline")
    project.add_rows(
        sheet_id, [{"headline": text} for text in rows], {"headline": col_id}
    )
    return pid, project, sheet_id, col_id


def _create_index(
    project: Any,
    pid: str,
    sheet_id: int,
    *,
    provider: str,
    model: str | None = None,
    allow_remote: bool = True,
    gateway: Any = None,
    key: str = "create@1",
) -> str:
    params: dict[str, Any] = {
        "sheet_id": sheet_id,
        "source_columns": ["headline"],
        "modality": "text",
        "provider": provider,
        "source_policy": {"kind": "text_cell"},
        "provider_policy": {"allow_remote": allow_remote},
    }
    if model is not None:
        params["model"] = model
    router = ModelRouter(
        model_call_policy=LOCAL_PURCHASE_POLICY,
        keys={"openai": "test-openai-key", "openrouter": "test-openrouter-key"},
    )
    req: dict[str, Any] = {
        "action_id": "embedding.index_create",
        "scope": {"kind": "project"},
        "params": params,
        "idempotency_key": key,
    }
    res = run_action_spec(
        project,
        req,
        project_id=pid,
        router=router,
        deps=ExecutorDeps(embedding_gateway=gateway),
    )
    if res.status == "needs_confirmation":
        token = res.errors[0].details["promise_set_hash"]
        res = run_action_spec(
            project,
            {**req, "confirmation": token},
            project_id=pid,
            router=router,
            deps=ExecutorDeps(embedding_gateway=gateway),
        )
    assert res.status == "completed", res.errors
    return str(res.outputs[0].ref["index_id"])


def _refresh_request(
    index_id: str,
    *,
    mode: str = "full",
    key: str = "refresh@1",
    confirmation: str | None = None,
) -> dict[str, Any]:
    req: dict[str, Any] = {
        "action_id": "embedding.index_refresh",
        "scope": {"kind": "project"},
        "params": {"index_id": index_id, "mode": mode},
        "idempotency_key": key,
    }
    if confirmation is not None:
        req["confirmation"] = confirmation
    return req


def test_local_refresh_needs_no_confirmation_even_with_zero_allowance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    client = _create_app_client(tmp_path, allowance_usd="0")
    pid, project, sheet_id, _col = _seed_sheet(client, ["alpha", "beta"])
    index_id = _create_index(
        project, pid, sheet_id, provider="fastembed", allow_remote=False
    )
    req = _refresh_request(index_id, mode="full", key="local@1")

    est_resp = client.post(
        f"/api/projects/{pid}/actions/v1/estimate", json={"action": req}
    )
    assert est_resp.status_code == 200, est_resp.text
    estimate = est_resp.json()["estimate"]
    assert estimate["cost"] == 0.0
    assert estimate["billed_cost"] == 0
    assert estimate["cost_source"] == "free_local"
    assert estimate["requires_confirmation"] is False
    assert estimate["rows"] == 2

    gw = _RecordingGateway(dim=384, provider_kind="local_process")
    direct = run_action_spec(
        project,
        req,
        project_id=pid,
        deps=ExecutorDeps(
            embedding_gateway=gw,
            consent_coverage=ConsentCoverage("user:local", Decimal("0")),
        ),
    )
    assert direct.status == "completed", direct.errors
    assert len(gw.calls) == 1


def test_remote_refresh_under_standing_allowance_runs_without_confirmation(
    tmp_path: Path,
) -> None:
    client = _create_app_client(tmp_path, allowance_usd="1")
    pid, project, sheet_id, _col = _seed_sheet(client, ["story one", "story two"])
    index_id = _create_index(
        project,
        pid,
        sheet_id,
        provider="openai",
        model="text-embedding-3-small",
        allow_remote=True,
    )
    req = _refresh_request(index_id, mode="full", key="under-allowance@1")

    est_resp = client.post(
        f"/api/projects/{pid}/actions/v1/estimate", json={"action": req}
    )
    assert est_resp.status_code == 200, est_resp.text
    estimate = est_resp.json()["estimate"]
    assert estimate["rows"] == 2
    assert estimate["cost"] > 0
    assert estimate["cost_source"] == "pricing_data"
    assert estimate["requires_confirmation"] is False
    assert isinstance(estimate["promise_set_hash"], str)

    run_resp = client.post(f"/api/projects/{pid}/actions/v1/run", json=req)
    assert run_resp.status_code == 200, run_resp.text
    assert run_resp.json()["status"] == "queued"

    gw = _RecordingGateway(dim=1536)
    registry = HandlerRegistry()
    register_embedding_refresh_handler(
        registry, workspace_root=client.app.state.workspace.root, gateway=gw
    )
    worker = Worker(client.app.state.workspace.queue, registry)
    assert worker.run_once() is True
    assert len(gw.calls) == 1

    # Incremental refresh with zero items to embed needs no confirmation even at $0.
    empty_req = _refresh_request(index_id, mode="incremental", key="empty-inc@1")
    gw_empty = _RecordingGateway(dim=1536)
    res_empty = run_action_spec(
        project,
        empty_req,
        project_id=pid,
        deps=ExecutorDeps(
            embedding_gateway=gw_empty,
            consent_coverage=ConsentCoverage("user:local", Decimal("0")),
        ),
    )
    assert res_empty.status == "completed", res_empty.errors
    assert gw_empty.calls == []


def test_remote_refresh_over_allowance_requires_exact_confirmation_before_egress(
    tmp_path: Path,
) -> None:
    client = _create_app_client(tmp_path, allowance_usd="0")
    pid, project, sheet_id, col_id = _seed_sheet(
        client, ["news item " * 200, "second story " * 200]
    )
    index_id = _create_index(
        project,
        pid,
        sheet_id,
        provider="openai",
        model="text-embedding-3-small",
        allow_remote=True,
    )
    req = _refresh_request(index_id, mode="full", key="over-allowance@1")

    est_resp = client.post(
        f"/api/projects/{pid}/actions/v1/estimate", json={"action": req}
    )
    assert est_resp.status_code == 200, est_resp.text
    estimate = est_resp.json()["estimate"]
    assert estimate["rows"] == 2
    assert estimate["cost"] > 0
    assert estimate["billed_cost"] > 0
    assert estimate["requires_confirmation"] is True
    token = estimate["promise_set_hash"]
    assert isinstance(token, str) and len(token) == 64

    before_jobs = client.app.state.workspace.queue.list_project_jobs(pid)
    effect_tables = ("changes", "runs", "model_calls", "receipts", "effect_checkpoints")
    before_counts = {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in effect_tables
    }
    challenge_resp = client.post(f"/api/projects/{pid}/actions/v1/run", json=req)
    assert challenge_resp.status_code == 402, challenge_resp.text
    body = challenge_resp.json()
    assert body["status"] == "needs_confirmation"
    [err] = body["errors"]
    assert err["code"] == "model_cost_requires_confirmation"
    assert err["field"] == "confirmation"
    assert err["needs_confirmation"] is True
    assert err["details"]["reason"] == "model_cost"
    assert err["details"]["promise_set_hash"] == token
    assert err["details"]["estimate"]["cost"] == estimate["cost"]
    assert err["details"]["estimate"]["billed_cost"] == estimate["billed_cost"]
    assert client.app.state.workspace.queue.list_project_jobs(pid) == before_jobs
    after_counts = {
        table: int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in effect_tables
    }
    assert after_counts == before_counts

    # Wrong token refuses with no writes and no queued job.
    wrong_resp = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json={**req, "confirmation": "0" * 64},
    )
    assert wrong_resp.status_code == 402
    assert wrong_resp.json()["errors"][0]["details"]["promise_set_hash"] == token
    assert client.app.state.workspace.queue.list_project_jobs(pid) == before_jobs

    # Confirmed request queues and executes through the worker.
    ok_resp = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json={**req, "confirmation": token},
    )
    assert ok_resp.status_code == 200, ok_resp.text
    assert ok_resp.json()["status"] == "queued"

    gw = _RecordingGateway(dim=1536)
    registry = HandlerRegistry()
    register_embedding_refresh_handler(
        registry, workspace_root=client.app.state.workspace.root, gateway=gw
    )
    worker = Worker(client.app.state.workspace.queue, registry)
    assert worker.run_once() is True
    assert len(gw.calls) == 1

    # Adding a row invalidates an earlier confirmation token before egress.
    project.add_rows(
        sheet_id, [{"headline": "breaking third story " * 200}], {"headline": col_id}
    )
    stale_req = _refresh_request(
        index_id, mode="full", key="over-allowance@2", confirmation=token
    )
    gw_stale = _RecordingGateway(dim=1536)
    stale_res = run_action_spec(
        project,
        stale_req,
        project_id=pid,
        deps=ExecutorDeps(
            embedding_gateway=gw_stale,
            consent_coverage=ConsentCoverage("user:local", Decimal("0")),
        ),
    )
    assert stale_res.status == "needs_confirmation"
    assert stale_res.errors[0].details["promise_set_hash"] != token
    assert gw_stale.calls == []


def test_unknown_price_remote_refresh_confirms_locally_and_refuses_on_cloud(
    tmp_path: Path,
) -> None:
    local_client = _create_app_client(tmp_path, edition="solo", allowance_usd="1000")
    pid, project, sheet_id, _col = _seed_sheet(local_client, ["alpha", "beta"])
    probe_gw = _RecordingGateway(dim=1024)
    index_id = _create_index(
        project,
        pid,
        sheet_id,
        provider="openrouter",
        model="mistralai/mistral-embed-2312",
        allow_remote=True,
        gateway=probe_gw,
        key="create-openrouter@1",
    )
    assert len(probe_gw.calls) == 1  # dimension probe only

    # Locally: even with a $1000 standing allowance, unknown price requires confirmation.
    req = _refresh_request(index_id, mode="full", key="unknown-local@1")
    est_resp = local_client.post(
        f"/api/projects/{pid}/actions/v1/estimate", json={"action": req}
    )
    assert est_resp.status_code == 200, est_resp.text
    estimate = est_resp.json()["estimate"]
    assert estimate["cost"] is None
    assert estimate["billed_cost"] is None
    assert estimate["cost_source"] == "unknown"
    assert estimate["requires_confirmation"] is True
    token = estimate["promise_set_hash"]
    assert isinstance(token, str) and len(token) == 64

    challenge_resp = local_client.post(f"/api/projects/{pid}/actions/v1/run", json=req)
    assert challenge_resp.status_code == 402, challenge_resp.text
    err = challenge_resp.json()["errors"][0]
    assert err["code"] == "model_cost_requires_confirmation"
    assert err["details"]["reason"] == "unknown_estimate"
    assert err["details"]["promise_set_hash"] == token
    assert len(probe_gw.calls) == 1  # no egress before confirmation

    # With confirmation locally: succeeds.
    gw_local = _RecordingGateway(dim=1024)
    confirmed_local = run_action_spec(
        project,
        {**req, "confirmation": token},
        project_id=pid,
        deps=ExecutorDeps(
            embedding_gateway=gw_local,
            consent_coverage=ConsentCoverage("user:local", Decimal("1000")),
        ),
    )
    assert confirmed_local.status == "completed", confirmed_local.errors
    assert len(gw_local.calls) == 1

    # On Cloud: unknown-price remote refresh is refused outright, even with confirmation.
    cloud_client = _create_app_client(tmp_path, edition="cloud", allowance_usd="1000")
    cloud_pid, cloud_project, cloud_sheet_id, _cloud_col = _seed_sheet(
        cloud_client, ["alpha", "beta"]
    )
    cloud_probe_gw = _RecordingGateway(dim=1024)
    cloud_index_id = _create_index(
        cloud_project,
        cloud_pid,
        cloud_sheet_id,
        provider="openrouter",
        model="mistralai/mistral-embed-2312",
        allow_remote=True,
        gateway=cloud_probe_gw,
        key="create-openrouter-cloud@1",
    )
    cloud_req = _refresh_request(cloud_index_id, mode="full", key="unknown-cloud@1")

    cloud_est = cloud_client.post(
        f"/api/projects/{cloud_pid}/actions/v1/estimate", json={"action": cloud_req}
    )
    assert cloud_est.status_code == 200, cloud_est.text
    cloud_estimate = cloud_est.json()["estimate"]
    assert cloud_estimate["cost"] is None
    assert cloud_estimate["billed_cost"] is None
    assert cloud_estimate["requires_confirmation"] is True
    assert cloud_estimate["promise_set_hash"] is None

    cloud_run = cloud_client.post(
        f"/api/projects/{cloud_pid}/actions/v1/run", json=cloud_req
    )
    assert cloud_run.status_code == 400, cloud_run.text
    assert cloud_run.json()["status"] == "failed"
    assert cloud_run.json()["errors"][0]["code"] == "unpriceable_model"
    assert cloud_client.app.state.workspace.queue.list_project_jobs(cloud_pid) == []

    # Direct execution with Cloud edition snapshot also refuses even if confirmation is echoed.
    gw_cloud = _RecordingGateway(dim=1024)
    cloud_direct = run_action_spec(
        cloud_project,
        {**cloud_req, "confirmation": token},
        project_id=cloud_pid,
        deps=ExecutorDeps(
            embedding_gateway=gw_cloud,
            consent_coverage=ConsentCoverage("user:cloud", Decimal("1000")),
        ),
        edition_run_context={PRODUCT_EDITION_SNAPSHOT_KEY: "cloud"},
    )
    assert cloud_direct.status == "failed"
    assert cloud_direct.errors[0].code == "unpriceable_model"
    assert gw_cloud.calls == []


def test_allow_remote_gate_remains_enforced_before_cost_consent(
    tmp_path: Path,
) -> None:
    client = _create_app_client(tmp_path, allowance_usd="100")
    pid, project, sheet_id, _col = _seed_sheet(client, ["alpha"])
    index_id = _create_index(
        project,
        pid,
        sheet_id,
        provider="openai",
        model="text-embedding-3-small",
        allow_remote=False,
    )
    req = _refresh_request(index_id, mode="full", key="no-allow-remote@1")

    est_resp = client.post(
        f"/api/projects/{pid}/actions/v1/estimate", json={"action": req}
    )
    assert est_resp.status_code == 400
    assert est_resp.json()["code"] == "embedding_remote_confirmation_required"

    run_resp = client.post(f"/api/projects/{pid}/actions/v1/run", json=req)
    assert run_resp.status_code == 400
    assert (
        run_resp.json()["errors"][0]["code"] == "embedding_remote_confirmation_required"
    )
    assert client.app.state.workspace.queue.list_project_jobs(pid) == []


def test_worker_rechecks_carried_allowance_if_rows_grow_after_enqueue(
    tmp_path: Path,
) -> None:
    client = _create_app_client(tmp_path, allowance_usd="0.00001")
    pid, project, sheet_id, col_id = _seed_sheet(client, ["tiny"])
    index_id = _create_index(
        project,
        pid,
        sheet_id,
        provider="openai",
        model="text-embedding-3-small",
        allow_remote=True,
    )
    req = _refresh_request(index_id, mode="full", key="grow-before-worker@1")
    enqueued = client.post(f"/api/projects/{pid}/actions/v1/run", json=req)
    assert enqueued.status_code == 200, enqueued.text
    assert enqueued.json()["status"] == "queued"

    # Add enough text after enqueue to exceed the carried $0.00001 allowance.
    project.add_rows(
        sheet_id,
        [{"headline": "long article " * 600} for _ in range(4)],
        {"headline": col_id},
    )
    gw = _RecordingGateway(dim=1536)
    registry = HandlerRegistry()
    register_embedding_refresh_handler(
        registry, workspace_root=client.app.state.workspace.root, gateway=gw
    )
    worker = Worker(client.app.state.workspace.queue, registry)
    assert worker.run_once() is True
    assert gw.calls == []
    [job] = client.app.state.workspace.queue.list_project_jobs(pid)
    assert job.result["error_code"] == "model_cost_requires_confirmation"


def test_create_probe_estimate_and_http_confirmation_gate_locally_and_on_cloud(
    tmp_path: Path,
) -> None:
    local_client = _create_app_client(tmp_path, edition="solo", allowance_usd="1000")
    pid, project, sheet_id, _col = _seed_sheet(local_client, ["alpha", "beta"])

    # Curated remote create (known dimension, no probe) needs no confirmation at $0.
    curated_req = {
        "action_id": "embedding.index_create",
        "scope": {"kind": "project"},
        "params": {
            "sheet_id": sheet_id,
            "source_columns": ["headline"],
            "modality": "text",
            "provider": "openai",
            "model": "text-embedding-3-small",
            "source_policy": {"kind": "text_cell"},
            "provider_policy": {"allow_remote": True},
        },
        "idempotency_key": "create-curated-no-probe@1",
    }
    curated_est = local_client.post(
        f"/api/projects/{pid}/actions/v1/estimate", json={"action": curated_req}
    )
    assert curated_est.status_code == 200, curated_est.text
    assert curated_est.json()["estimate"]["rows"] == 0
    assert curated_est.json()["estimate"]["cost"] == 0.0
    assert curated_est.json()["estimate"]["billed_cost"] == 0
    assert curated_est.json()["estimate"]["requires_confirmation"] is False

    # Missing sheet_id on a discovery-required model fails cleanly with invalid_params
    # on both /estimate and /run before any cost gate or reservation.
    no_sheet_req = {
        "action_id": "embedding.index_create",
        "scope": {"kind": "project"},
        "params": {
            "sheet_id": None,
            "source_columns": ["headline"],
            "modality": "text",
            "provider": "openrouter",
            "model": "mistralai/mistral-embed-2312",
            "source_policy": {"kind": "text_cell"},
            "provider_policy": {"allow_remote": True},
        },
        "idempotency_key": "create-probe-no-sheet@1",
    }
    no_sheet_est = local_client.post(
        f"/api/projects/{pid}/actions/v1/estimate", json={"action": no_sheet_req}
    )
    assert no_sheet_est.status_code == 400, no_sheet_est.text
    assert no_sheet_est.json()["code"] == "invalid_params"
    assert no_sheet_est.json()["field"] == "params.sheet_id"

    no_sheet_run = local_client.post(
        f"/api/projects/{pid}/actions/v1/run", json=no_sheet_req
    )
    assert no_sheet_run.status_code == 400, no_sheet_run.text
    assert no_sheet_run.json()["errors"][0]["code"] == "invalid_params"
    assert no_sheet_run.json()["errors"][0]["field"] == "params.sheet_id"

    # Custom-remote probe: /estimate and /run agree on the confirmation token.
    probe_req = {
        "action_id": "embedding.index_create",
        "scope": {"kind": "project"},
        "params": {
            "sheet_id": sheet_id,
            "source_columns": ["headline"],
            "modality": "text",
            "provider": "openrouter",
            "model": "mistralai/mistral-embed-2312",
            "source_policy": {"kind": "text_cell"},
            "provider_policy": {"allow_remote": True},
        },
        "idempotency_key": "create-probe-http@1",
    }
    est_resp = local_client.post(
        f"/api/projects/{pid}/actions/v1/estimate", json={"action": probe_req}
    )
    assert est_resp.status_code == 200, est_resp.text
    estimate = est_resp.json()["estimate"]
    assert estimate["rows"] == 1
    assert estimate["cost"] is None
    assert estimate["billed_cost"] is None
    assert estimate["cost_source"] == "unknown"
    assert estimate["requires_confirmation"] is True
    token = estimate["promise_set_hash"]
    assert isinstance(token, str) and len(token) == 64

    challenge_resp = local_client.post(
        f"/api/projects/{pid}/actions/v1/run", json=probe_req
    )
    assert challenge_resp.status_code == 402, challenge_resp.text
    body = challenge_resp.json()
    assert body["status"] == "needs_confirmation"
    [err] = body["errors"]
    assert err["code"] == "model_cost_requires_confirmation"
    assert err["field"] == "confirmation"
    assert err["needs_confirmation"] is True
    assert err["details"]["reason"] == "unknown_estimate"
    assert err["details"]["promise_set_hash"] == token

    # Wrong token refuses with 402 and re-offers the exact token.
    wrong_resp = local_client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json={**probe_req, "confirmation": "0" * 64},
    )
    assert wrong_resp.status_code == 402
    assert wrong_resp.json()["errors"][0]["details"]["promise_set_hash"] == token

    # Confirmed direct run probes once and creates the index.
    probe_gw = _RecordingGateway(dim=1024)
    confirmed = run_action_spec(
        project,
        {**probe_req, "confirmation": token},
        project_id=pid,
        deps=ExecutorDeps(
            embedding_gateway=probe_gw,
            consent_coverage=ConsentCoverage("user:local", Decimal("1000")),
        ),
    )
    assert confirmed.status == "completed", confirmed.errors
    assert len(probe_gw.calls) == 1

    # On Cloud: unpriceable custom-remote dimension probe refuses outright.
    cloud_client = _create_app_client(tmp_path, edition="cloud", allowance_usd="1000")
    cloud_pid, cloud_project, cloud_sheet_id, _cloud_col = _seed_sheet(
        cloud_client, ["alpha"]
    )
    cloud_probe_req = {
        "action_id": "embedding.index_create",
        "scope": {"kind": "project"},
        "params": {
            "sheet_id": cloud_sheet_id,
            "source_columns": ["headline"],
            "modality": "text",
            "provider": "openrouter",
            "model": "mistralai/mistral-embed-2312",
            "source_policy": {"kind": "text_cell"},
            "provider_policy": {"allow_remote": True},
        },
        "idempotency_key": "create-probe-cloud@1",
    }
    cloud_est = cloud_client.post(
        f"/api/projects/{cloud_pid}/actions/v1/estimate",
        json={"action": cloud_probe_req},
    )
    assert cloud_est.status_code == 200, cloud_est.text
    assert cloud_est.json()["estimate"]["requires_confirmation"] is True
    assert cloud_est.json()["estimate"]["promise_set_hash"] is None

    cloud_run = cloud_client.post(
        f"/api/projects/{cloud_pid}/actions/v1/run", json=cloud_probe_req
    )
    assert cloud_run.status_code == 400, cloud_run.text
    assert cloud_run.json()["status"] == "failed"
    assert cloud_run.json()["errors"][0]["code"] == "unpriceable_model"

    cloud_gw = _RecordingGateway(dim=1024)
    cloud_direct = run_action_spec(
        cloud_project,
        {**cloud_probe_req, "confirmation": token},
        project_id=cloud_pid,
        deps=ExecutorDeps(
            embedding_gateway=cloud_gw,
            consent_coverage=ConsentCoverage("user:cloud", Decimal("1000")),
        ),
        edition_run_context={PRODUCT_EDITION_SNAPSHOT_KEY: "cloud"},
    )
    assert cloud_direct.status == "failed"
    assert cloud_direct.errors[0].code == "unpriceable_model"
    assert cloud_gw.calls == []


def test_incremental_refresh_estimate_matches_live_staleness_without_creating_sidecar(
    tmp_path: Path,
) -> None:
    """Preview and execution share one refresh item plan without sidecar side effects."""
    client = _create_app_client(tmp_path, allowance_usd="0")
    pid, project, sheet_id, col_id = _seed_sheet(
        client, ["first story", "second story"]
    )
    index_id = _create_index(
        project,
        pid,
        sheet_id,
        provider="openai",
        model="text-embedding-3-small",
        allow_remote=True,
    )
    assert not RetrievalStore(project).path.exists()

    # Read-only estimate before any refresh must not create the sidecar database.
    initial_req = _refresh_request(index_id, mode="incremental", key="inc-plan@1")
    est_before = client.post(
        f"/api/projects/{pid}/actions/v1/estimate", json={"action": initial_req}
    )
    assert est_before.status_code == 200, est_before.text
    assert est_before.json()["estimate"]["rows"] == 2
    assert not RetrievalStore(project).path.exists()

    # Confirm and run the first incremental refresh (embeds 2 rows).
    token1 = est_before.json()["estimate"]["promise_set_hash"]
    gw = _RecordingGateway(dim=1536)
    first = run_action_spec(
        project,
        {**initial_req, "confirmation": token1},
        project_id=pid,
        deps=ExecutorDeps(
            embedding_gateway=gw,
            consent_coverage=ConsentCoverage("user:local", Decimal("0")),
        ),
    )
    assert first.status == "completed", first.errors
    assert len(gw.calls) == 1 and len(gw.calls[0][0]) == 2

    # Add 1 new row: incremental estimate and live refresh both plan exactly 1 row.
    project.add_rows(sheet_id, [{"headline": "third story"}], {"headline": col_id})
    inc_req = _refresh_request(index_id, mode="incremental", key="inc-plan@2")
    est_after = client.post(
        f"/api/projects/{pid}/actions/v1/estimate", json={"action": inc_req}
    )
    assert est_after.status_code == 200, est_after.text
    assert est_after.json()["estimate"]["rows"] == 1
    token2 = est_after.json()["estimate"]["promise_set_hash"]
    second = run_action_spec(
        project,
        {**inc_req, "confirmation": token2},
        project_id=pid,
        deps=ExecutorDeps(
            embedding_gateway=gw,
            consent_coverage=ConsentCoverage("user:local", Decimal("0")),
        ),
    )
    assert second.status == "completed", second.errors
    assert len(gw.calls) == 2 and len(gw.calls[1][0]) == 1
    assert second.outputs[0].ref["refreshed"] == 1
    assert second.outputs[0].ref["skipped_current"] == 2


def test_retry_persisted_paid_probe_checkpoint_finishes_without_confirmation_or_reprobe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconciling a durable paid-probe checkpoint never re-gates or re-probes."""
    client = _create_app_client(tmp_path, edition="solo", allowance_usd="0")
    pid, project, sheet_id, _col = _seed_sheet(client, ["alpha"])
    probe_req = {
        "action_id": "embedding.index_create",
        "scope": {"kind": "project"},
        "params": {
            "sheet_id": sheet_id,
            "source_columns": ["headline"],
            "modality": "text",
            "provider": "openrouter",
            "model": "mistralai/mistral-embed-2312",
            "source_policy": {"kind": "text_cell"},
            "provider_policy": {"allow_remote": True},
        },
        "idempotency_key": "create-probe-checkpoint-retry@1",
    }
    gw = _RecordingGateway(dim=1024)
    challenge = run_action_spec(
        project,
        probe_req,
        project_id=pid,
        deps=ExecutorDeps(
            embedding_gateway=gw,
            consent_coverage=ConsentCoverage("user:local", Decimal("0")),
        ),
    )
    assert challenge.status == "needs_confirmation", challenge.errors
    assert gw.calls == []
    token = challenge.errors[0].details["promise_set_hash"]

    original_create_space = EmbeddingStore.create_space_in_transaction
    failed_once = False

    def fail_first_metadata_write(self: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("injected metadata write failure after paid probe")
        return original_create_space(self, *args, **kwargs)

    monkeypatch.setattr(
        EmbeddingStore, "create_space_in_transaction", fail_first_metadata_write
    )
    first = run_action_spec(
        project,
        {**probe_req, "confirmation": token},
        project_id=pid,
        deps=ExecutorDeps(
            embedding_gateway=gw,
            consent_coverage=ConsentCoverage("user:local", Decimal("0")),
        ),
    )
    assert first.status == "failed"
    assert first.errors[0].code == "project_write_failed"
    assert len(gw.calls) == 1

    # Retrying without `confirmation` reconciles the checkpoint cleanly with zero re-probe.
    resumed = run_action_spec(
        project,
        probe_req,
        project_id=pid,
        deps=ExecutorDeps(
            embedding_gateway=gw,
            consent_coverage=ConsentCoverage("user:local", Decimal("0")),
        ),
    )
    assert resumed.status == "completed", resumed.errors
    assert len(gw.calls) == 1
    assert resumed.outputs[0].ref["dimension"] == 1024
