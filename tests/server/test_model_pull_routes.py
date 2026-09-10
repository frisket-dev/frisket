"""``/api/providers/models/pull*`` route contract.

It covers the opt-in gate, grammar validation, server-side re-probe
(unreachable, unauthorized, or non-native protocol), one active pull per
workspace, same-ref deduplication, and cancellation.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from frisket.engine.jobs import model_pull_store as store
from frisket.server import provider_config
from frisket.server.app import create_app


def _client(tmp_path):
    root = tmp_path / "ws"
    endpoint = provider_config.create_local_endpoint(
        root,
        name="Ollama test server",
        url="http://127.0.0.1:11434",
    )
    client = TestClient(create_app(root))
    client.local_endpoint_id = endpoint.endpoint_id
    return client


def _enable_pull(client) -> None:
    resp = client.patch(
        f"/api/providers/local-endpoints/{client.local_endpoint_id}",
        json={"pull_enabled": True},
    )
    assert resp.status_code == 200


def _local_ref(client, model: str) -> str:
    return f"ollama/@{client.local_endpoint_id}/{model}"


def _native_reachable(monkeypatch, **overrides) -> None:
    base = {
        "reachable": True,
        "status": 200,
        "models": [],
        "detail": None,
        "protocol": "ollama_native",
        "auth_status": "ok",
    }
    base.update(overrides)
    monkeypatch.setattr(provider_config, "ollama_reachable", lambda *a, **k: base)


def test_pull_disabled_by_default_returns_403(tmp_path, monkeypatch) -> None:
    _native_reachable(monkeypatch)
    client = _client(tmp_path)

    resp = client.post(
        "/api/providers/models/pull", json={"ref": _local_ref(client, "smollm:135m")}
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "model_pull_disabled"


def test_invalid_model_ref_returns_400(tmp_path, monkeypatch) -> None:
    _native_reachable(monkeypatch)
    client = _client(tmp_path)
    _enable_pull(client)

    resp = client.post(
        "/api/providers/models/pull", json={"ref": "ollama:evil.example/x/y"}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_model_ref"


def test_unreachable_server_returns_503(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        provider_config,
        "ollama_reachable",
        lambda *a, **k: {
            "reachable": False,
            "status": None,
            "models": [],
            "detail": "not running",
            "protocol": "unknown",
            "auth_status": "unknown",
        },
    )
    client = _client(tmp_path)
    _enable_pull(client)

    resp = client.post(
        "/api/providers/models/pull", json={"ref": _local_ref(client, "smollm:135m")}
    )
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "local_server_unreachable"


def test_unauthorized_server_returns_409(tmp_path, monkeypatch) -> None:
    _native_reachable(monkeypatch, auth_status="unauthorized", protocol="unknown")
    client = _client(tmp_path)
    _enable_pull(client)

    resp = client.post(
        "/api/providers/models/pull", json={"ref": _local_ref(client, "smollm:135m")}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "local_server_unauthorized"


def test_non_native_protocol_returns_409_pull_unsupported(
    tmp_path, monkeypatch
) -> None:
    _native_reachable(monkeypatch, protocol="openai_compatible")
    client = _client(tmp_path)
    _enable_pull(client)

    resp = client.post(
        "/api/providers/models/pull", json={"ref": _local_ref(client, "smollm:135m")}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "pull_unsupported"


def test_happy_path_enqueues_job_and_returns_202(tmp_path, monkeypatch) -> None:
    _native_reachable(monkeypatch)
    client = _client(tmp_path)
    _enable_pull(client)

    resp = client.post(
        "/api/providers/models/pull", json={"ref": _local_ref(client, "smollm:135m")}
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["deduplicated"] is False
    pull = body["pull"]
    assert pull["model"] == _local_ref(client, "smollm:135m")
    assert pull["endpoint_id"] == client.local_endpoint_id
    assert pull["status"] in ("pending", "running")
    assert pull["schemaVersion"] == "frisket.model_pull.v3"
    assert pull["artifact"] is None

    workspace = client.app.state.workspace
    row = store.get(workspace.queue.engine, pull["id"])
    assert row is not None
    assert row.job_id is not None
    job = workspace.queue.get(row.job_id)
    assert job is not None
    assert job.kind == "model.pull"
    # hosted-job-attribution-v1: a pull fills the model cache under this
    # workspace root, shared by every project there, so it declares itself
    # owned by the server rather than by a tenant. The declaration is explicit
    # precisely so it is not confused with an author forgetting to label a job.
    assert job.payload == {
        "pull_id": row.id,
        "workspace_root": str(workspace.root),
        "endpoint_id": client.local_endpoint_id,
        "server_scoped": True,
    }


def test_explicit_ollama_ref_forces_artifact_like_name_through_ollama(
    tmp_path, monkeypatch
) -> None:
    probes = []

    def reachable(*args, **kwargs):
        probes.append((args, kwargs))
        return {
            "reachable": True,
            "status": 200,
            "models": [],
            "detail": None,
            "protocol": "ollama_native",
            "auth_status": "ok",
        }

    monkeypatch.setattr(provider_config, "ollama_reachable", reachable)
    client = _client(tmp_path)
    _enable_pull(client)

    response = client.post(
        "/api/providers/models/pull", json={"ref": _local_ref(client, "opus-mt:en-es")}
    )

    assert response.status_code == 202, response.text
    assert probes
    assert response.json()["pull"]["model"] == _local_ref(client, "opus-mt:en-es")
    assert response.json()["pull"]["artifact"] is None


def test_explicit_ollama_ref_uses_generic_artifact_length_guard(
    tmp_path, monkeypatch
) -> None:
    client = _client(tmp_path)
    _enable_pull(client)
    model = "a" * 314

    response = client.post(
        "/api/providers/models/pull", json={"ref": _local_ref(client, model)}
    )

    assert response.status_code == 400
    assert response.json()["detail"] == {
        "code": "invalid_model_ref",
        "message": "artifact reference is too long (max 320 characters)",
    }


def test_same_ref_dedupes_instead_of_erroring(tmp_path, monkeypatch) -> None:
    _native_reachable(monkeypatch)
    client = _client(tmp_path)
    _enable_pull(client)

    first = client.post(
        "/api/providers/models/pull", json={"ref": _local_ref(client, "smollm:135m")}
    )
    assert first.status_code == 202
    second = client.post(
        "/api/providers/models/pull", json={"ref": _local_ref(client, "smollm:135m")}
    )
    assert second.status_code == 202
    assert second.json()["deduplicated"] is True
    assert second.json()["pull"]["id"] == first.json()["pull"]["id"]


def test_different_ref_while_active_returns_409_busy(tmp_path, monkeypatch) -> None:
    _native_reachable(monkeypatch)
    client = _client(tmp_path)
    _enable_pull(client)

    first = client.post(
        "/api/providers/models/pull", json={"ref": _local_ref(client, "smollm:135m")}
    )
    assert first.status_code == 202
    second = client.post(
        "/api/providers/models/pull", json={"ref": _local_ref(client, "qwen3:8b")}
    )
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "pull_busy"
    assert second.json()["detail"]["active"]["model"] == _local_ref(
        client, "smollm:135m"
    )


def test_get_pulls_and_pull_by_id(tmp_path, monkeypatch) -> None:
    _native_reachable(monkeypatch)
    client = _client(tmp_path)
    _enable_pull(client)

    created = client.post(
        "/api/providers/models/pull", json={"ref": _local_ref(client, "smollm:135m")}
    ).json()["pull"]

    listing = client.get("/api/providers/models/pulls")
    assert listing.status_code == 200
    assert any(p["id"] == created["id"] for p in listing.json()["pulls"])

    single = client.get(f"/api/providers/models/pulls/{created['id']}")
    assert single.status_code == 200
    assert single.json()["id"] == created["id"]

    missing = client.get("/api/providers/models/pulls/999999")
    assert missing.status_code == 404


def test_cancel_route_flips_state(tmp_path, monkeypatch) -> None:
    _native_reachable(monkeypatch)
    client = _client(tmp_path)
    _enable_pull(client)

    created = client.post(
        "/api/providers/models/pull", json={"ref": _local_ref(client, "smollm:135m")}
    ).json()["pull"]

    cancel_resp = client.post(f"/api/providers/models/pulls/{created['id']}/cancel")
    assert cancel_resp.status_code == 202
    body = cancel_resp.json()
    assert body["cancel_requested"] is True

    workspace = client.app.state.workspace
    job = workspace.queue.get(store.get(workspace.queue.engine, created["id"]).job_id)
    assert job.status == "cancelled"


def test_cancel_of_a_never_claimed_pull_finalizes_the_row_immediately(
    tmp_path, monkeypatch
) -> None:
    """Red-first regression for the review's reproduced defect (item 1a): no
    worker is running in this test, so the enqueued job is still 'queued'
    (never claimed) -- its handler will NEVER run to mark the pull row
    terminal itself. Before the fix, the pull row was left 'pending' forever
    even though the job was cancelled; the route must finalize it directly."""
    _native_reachable(monkeypatch)
    client = _client(tmp_path)
    _enable_pull(client)

    created = client.post(
        "/api/providers/models/pull", json={"ref": _local_ref(client, "smollm:135m")}
    ).json()["pull"]

    workspace = client.app.state.workspace
    row_before = store.get(workspace.queue.engine, created["id"])
    assert row_before.status == store.STATUS_PENDING
    job_before = workspace.queue.get(row_before.job_id)
    assert job_before.status == "queued"  # never claimed -- no worker running

    cancel_resp = client.post(f"/api/providers/models/pulls/{created['id']}/cancel")
    assert cancel_resp.status_code == 202
    body = cancel_resp.json()
    assert body["status"] == "cancelled"

    row_after = store.get(workspace.queue.engine, created["id"])
    assert row_after.status == store.STATUS_CANCELLED
    assert row_after.finished_at is not None
    job_after = workspace.queue.get(row_after.job_id)
    assert job_after.status == "cancelled"


def test_enqueue_failure_marks_row_failed_not_orphaned(tmp_path, monkeypatch) -> None:
    """Item 1d: if enqueue itself blows up, the freshly-created active row
    must not survive as an orphan -- nothing will ever come along to mark it
    terminal, and it would permanently occupy the workspace's one-active-pull
    slot (item 2)."""
    _native_reachable(monkeypatch)
    client = _client(tmp_path)
    _enable_pull(client)
    workspace = client.app.state.workspace
    original_enqueue = workspace.queue.enqueue

    def boom(*args, **kwargs):
        raise RuntimeError("queue backend unavailable")

    monkeypatch.setattr(workspace.queue, "enqueue", boom)

    resp = client.post(
        "/api/providers/models/pull", json={"ref": _local_ref(client, "smollm:135m")}
    )
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["code"] == "enqueue_failed"

    rows = store.list_recent(workspace.queue.engine, str(workspace.root))
    assert len(rows) == 1
    assert rows[0].status == store.STATUS_FAILED
    assert rows[0].error_code == "enqueue_failed"

    # the slot is free again -- a retried pull must not see a phantom busy.
    monkeypatch.setattr(workspace.queue, "enqueue", original_enqueue)
    retry = client.post(
        "/api/providers/models/pull", json={"ref": _local_ref(client, "smollm:135m")}
    )
    assert retry.status_code == 202, retry.text


def test_pull_enabled_reflected_in_provider_catalog(tmp_path, monkeypatch) -> None:
    _native_reachable(monkeypatch)
    client = _client(tmp_path)

    catalog = client.get("/api/providers").json()
    ollama = next(p for p in catalog["providers"] if p["kind"] == "local_http")
    assert ollama["pull_enabled"] is False
    assert ollama["source"] == "stored"

    _enable_pull(client)
    catalog = client.get("/api/providers").json()
    ollama = next(p for p in catalog["providers"] if p["kind"] == "local_http")
    assert ollama["pull_enabled"] is True
    assert ollama["source"] == "stored"


# --- Artifact-generic /api/providers/models/* routes -------------------


def test_artifact_pull_ollama_ref_runs_daemon_preflight(tmp_path, monkeypatch) -> None:
    _native_reachable(monkeypatch)
    client = _client(tmp_path)
    _enable_pull(client)

    resp = client.post(
        "/api/providers/models/pull", json={"ref": _local_ref(client, "smollm:135m")}
    )
    assert resp.status_code == 202, resp.text
    pull = resp.json()["pull"]
    assert pull["model"] == _local_ref(client, "smollm:135m")
    assert pull["endpoint_id"] == client.local_endpoint_id
    assert pull["artifact"] is None


def test_artifact_pull_opus_mt_skips_daemon_and_enqueues(tmp_path, monkeypatch) -> None:
    _native_reachable(monkeypatch)
    client = _client(tmp_path)
    _enable_pull(client)

    # From here on, the pull route for an artifact ref must NOT probe ollama.
    def _boom(*a, **k):
        raise AssertionError("artifact refs must not probe the ollama daemon")

    monkeypatch.setattr(provider_config, "ollama_reachable", _boom)
    resp = client.post("/api/providers/models/pull", json={"ref": "opus-mt:en-es"})
    assert resp.status_code == 202, resp.text
    pull = resp.json()["pull"]
    assert pull["model"] == "opus-mt:en-es"


def test_artifact_pull_pinned_spacy_model_enqueues_without_unpinned_ack(
    tmp_path, monkeypatch
) -> None:
    from frisket.ai.models.artifact_manifest import SPACY_MODEL_REF

    client = _client(tmp_path)
    _enable_pull(client)

    def _boom(*a, **k):
        raise AssertionError("artifact refs must not probe the ollama daemon")

    monkeypatch.setattr(provider_config, "ollama_reachable", _boom)
    resp = client.post("/api/providers/models/pull", json={"ref": SPACY_MODEL_REF})
    assert resp.status_code == 202, resp.text
    assert resp.json()["pull"]["model"] == SPACY_MODEL_REF


def test_artifact_pull_invalid_ref_returns_400(tmp_path) -> None:
    client = _client(tmp_path)
    _enable_pull(client)
    resp = client.post("/api/providers/models/pull", json={"ref": "opus-mt:bogus"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_model_ref"


def test_artifact_pull_unpinned_hf_requires_acknowledgment(tmp_path) -> None:
    client = _client(tmp_path)
    _enable_pull(client)
    ref = "hf:owner/repo@abc123/model.gguf"
    resp = client.post("/api/providers/models/pull", json={"ref": ref})
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "unpinned_unacknowledged"
    # acknowledged -> enqueues
    resp2 = client.post(
        "/api/providers/models/pull",
        json={"ref": ref, "unpinned_acknowledged": True},
    )
    assert resp2.status_code == 202, resp2.text


def test_artifact_pull_hf_snapshot_pinned_ref_enqueues(tmp_path, monkeypatch) -> None:
    """Decision 2 (2026-07-19 review): the explicit pull route is the ONLY way
    an hf-snapshot pull starts. No daemon probe for it, and a second concurrent
    pull respects the existing one-active-pull-per-workspace behavior."""
    from frisket.ai.models.artifact_manifest import PARAKEET_MODEL_REF, PARAKEET_VAD_REF

    client = _client(tmp_path)
    _enable_pull(client)

    def _boom(*a, **k):
        raise AssertionError("artifact refs must not probe the ollama daemon")

    monkeypatch.setattr(provider_config, "ollama_reachable", _boom)

    resp = client.post("/api/providers/models/pull", json={"ref": PARAKEET_VAD_REF})
    assert resp.status_code == 202, resp.text
    pull = resp.json()["pull"]
    assert pull["model"] == PARAKEET_VAD_REF

    busy = client.post("/api/providers/models/pull", json={"ref": PARAKEET_MODEL_REF})
    assert busy.status_code == 409
    assert busy.json()["detail"]["code"] == "pull_busy"
    assert busy.json()["detail"]["active"]["model"] == PARAKEET_VAD_REF


def test_artifact_pull_unpinned_hf_snapshot_rejected_at_parse(tmp_path) -> None:
    """The manifest allowlist is enforced at parse time: an arbitrary repo is
    invalid_model_ref (400), never unpinned_unacknowledged -- there is no
    acknowledgment path for a multi-file snapshot."""
    client = _client(tmp_path)
    _enable_pull(client)
    resp = client.post(
        "/api/providers/models/pull",
        json={"ref": "hf-snapshot:owner/repo@0123abc", "unpinned_acknowledged": True},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_model_ref"


def test_uninstall_ollama_ref_rejected(tmp_path) -> None:
    client = _client(tmp_path)
    resp = client.post(
        "/api/providers/models/uninstall",
        json={"ref": _local_ref(client, "smollm:135m")},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "uninstall_unsupported"


def test_uninstall_hf_snapshot_ref_rejected(tmp_path) -> None:
    """hf-snapshot bytes live in the HF hub cache, not the frisket model
    cache -- a clean 400, never a model_cache scheme error (500)."""
    from frisket.ai.models.artifact_manifest import PARAKEET_VAD_REF

    client = _client(tmp_path)
    resp = client.post(
        "/api/providers/models/uninstall", json={"ref": PARAKEET_VAD_REF}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "uninstall_unsupported"


def test_uninstall_not_installed_returns_404(tmp_path) -> None:
    client = _client(tmp_path)
    resp = client.post("/api/providers/models/uninstall", json={"ref": "opus-mt:en-es"})
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "artifact_not_installed"


def test_uninstall_done_artifact_marks_uninstalled(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path)
    workspace = client.app.state.workspace
    engine = workspace.queue.engine
    # simulate an installed (done) opus-mt pull row
    row, _ = store.create_or_get_active(
        engine, workspace_root=str(workspace.root), model_ref="opus-mt:en-es"
    )
    store.mark_running(engine, row.id, job_id=1)
    store.mark_done(engine, row.id, resolved_digest="sha256:x", resolved_size=1)

    resp = client.post("/api/providers/models/uninstall", json={"ref": "opus-mt:en-es"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "uninstalled"
    fresh = store.get(engine, row.id)
    assert fresh.status == store.STATUS_UNINSTALLED


def test_uninstall_rejected_while_a_pull_is_in_flight(tmp_path) -> None:
    """Uninstalling a ref that has an active (pending/running) pull
    must be refused (409 pull_in_flight), never racing the worker's promote."""
    client = _client(tmp_path)
    workspace = client.app.state.workspace
    engine = workspace.queue.engine
    # a prior install (done) PLUS a fresh active re-pull for the same ref
    done, _ = store.create_or_get_active(
        engine, workspace_root=str(workspace.root), model_ref="opus-mt:en-es"
    )
    store.mark_running(engine, done.id, job_id=1)
    store.mark_done(engine, done.id, resolved_digest="sha256:x", resolved_size=1)
    # an in-flight re-pull occupies the active slot
    store.create_or_get_active(
        engine, workspace_root=str(workspace.root), model_ref="opus-mt:en-es"
    )

    resp = client.post("/api/providers/models/uninstall", json={"ref": "opus-mt:en-es"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "pull_in_flight"


def test_uninstall_flips_all_done_rows_for_the_ref(tmp_path) -> None:
    """A duplicate historical ``done`` row must not resurrect the
    artifact as installed -- every done row for the ref flips to uninstalled."""
    client = _client(tmp_path)
    workspace = client.app.state.workspace
    engine = workspace.queue.engine
    ids = []
    for _ in range(2):
        row, _ = store.create_or_get_active(
            engine, workspace_root=str(workspace.root), model_ref="opus-mt:en-es"
        )
        store.mark_running(engine, row.id, job_id=1)
        store.mark_done(engine, row.id, resolved_digest="sha256:x", resolved_size=1)
        ids.append(row.id)

    resp = client.post("/api/providers/models/uninstall", json={"ref": "opus-mt:en-es"})
    assert resp.status_code == 200, resp.text
    for pid in ids:
        assert store.get(engine, pid).status == store.STATUS_UNINSTALLED
