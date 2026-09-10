"""The ``model.pull`` job handler against the live-captured Ollama fixtures
via ``httpx.MockTransport`` -- no real network. Covers: idempotency
(already-installed short-circuits before ever
POSTing), a full success stream, the unknown-model terminal error, and
cooperative cancellation.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest

from frisket.engine.jobs import model_pull_store as store
from frisket.engine.jobs.ports import JobHandlerContext
from frisket.engine.jobs.model_pull import register_model_pull_handler
from frisket.engine.jobs.queue import open_queue
from frisket.engine.jobs.worker import HandlerRegistry
from frisket.local_model_ids import format_local_model_id
from frisket.server import provider_config

FIXTURES = Path(__file__).parent.parent / "fixtures" / "ollama_pull"
URL = "http://127.0.0.1:11434"


def _lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text().splitlines()


class _LineStream(httpx.SyncByteStream):
    """Yields fixture lines one at a time, optionally calling back after
    each yield -- the seam a cancellation test uses to flip the durable
    cancel flag partway through a stream, without real concurrency."""

    def __init__(self, lines: list[str], on_yield=None) -> None:
        self._lines = lines
        self._on_yield = on_yield

    def __iter__(self):
        for index, line in enumerate(self._lines):
            yield (line + "\n").encode()
            if self._on_yield is not None:
                self._on_yield(index)

    def close(self) -> None:  # pragma: no cover - nothing to release
        pass


def _setup(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    queue = open_queue(workspace=root)
    registry = HandlerRegistry()
    return root, queue, registry


def _enqueue_pull(
    queue, root: Path, model_ref: str, *, max_attempts: int = 3
) -> tuple[int, int]:
    engine = queue.engine
    endpoints, notes = provider_config.resolve_local_endpoints(root)
    assert notes == []
    assert len(endpoints) == 1
    endpoint = endpoints[0]
    canonical_ref = (
        model_ref
        if model_ref.startswith("ollama/@")
        else format_local_model_id(endpoint.endpoint_id, model_ref)
    )
    row, created = store.create_or_get_active(
        engine,
        workspace_root=str(root),
        model_ref=canonical_ref,
        endpoint_id=endpoint.endpoint_id,
        endpoint_origin=endpoint.origin,
    )
    assert created is True
    job_id = queue.enqueue(
        "model.pull",
        {
            "pull_id": row.id,
            "workspace_root": str(root),
            "endpoint_id": endpoint.endpoint_id,
        },
        max_attempts=max_attempts,
    )
    store.set_job_id(engine, row.id, job_id=job_id)
    return row.id, job_id


def _run_handler(registry, queue, pull_id: int, job_id: int) -> dict:
    handler = registry.get("model.pull")
    assert handler is not None
    row = store.get(queue.engine, pull_id)
    assert row is not None
    return handler(
        {
            "pull_id": pull_id,
            "workspace_root": None,
            "job_id": job_id,
            "endpoint_id": row.endpoint_id,
        },
        JobHandlerContext.without_job_row(),
    )


def test_idempotent_when_already_installed(tmp_path, monkeypatch) -> None:
    root, queue, registry = _setup(tmp_path)
    monkeypatch.setenv("OLLAMA_URL", URL)
    pull_calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "smollm:135m",
                            "digest": "sha256:existing",
                            "size": 12345,
                        }
                    ]
                },
            )
        pull_calls.append(request)
        raise AssertionError("must not POST /api/pull when already installed")

    def client_factory():
        return httpx.Client(transport=httpx.MockTransport(handler))

    register_model_pull_handler(
        registry, workspace_root=root, queue=queue, client_factory=client_factory
    )
    pull_id, job_id = _enqueue_pull(queue, root, "smollm:135m")
    queue.claim("w1")

    result = _run_handler(registry, queue, pull_id, job_id)

    assert result["status"] == "done"
    assert pull_calls == []
    row = store.get(queue.engine, pull_id)
    assert row.status == store.STATUS_DONE
    assert row.resolved_digest == "sha256:existing"
    assert row.resolved_size == 12345
    queue.close()


def test_full_download_success_records_resolved_digest(tmp_path, monkeypatch) -> None:
    root, queue, registry = _setup(tmp_path)
    monkeypatch.setenv("OLLAMA_URL", URL)
    tags_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            tags_calls["count"] += 1
            if tags_calls["count"] == 1:
                return httpx.Response(200, json={"models": []})
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "smollm:135m",
                            "digest": "sha256:eb2c714d40d4b35ba4b8ee98475a06d51d8080a17d2d2a75a23665985c739b94",
                            "size": 91739413,
                        }
                    ]
                },
            )
        assert request.url.path == "/api/pull"
        assert request.method == "POST"
        return httpx.Response(200, stream=_LineStream(_lines("pull-stream-full.jsonl")))

    def client_factory():
        return httpx.Client(transport=httpx.MockTransport(handler))

    register_model_pull_handler(
        registry, workspace_root=root, queue=queue, client_factory=client_factory
    )
    pull_id, job_id = _enqueue_pull(queue, root, "smollm:135m")
    queue.claim("w1")

    result = _run_handler(registry, queue, pull_id, job_id)

    assert result["status"] == "done"
    row = store.get(queue.engine, pull_id)
    assert row.status == store.STATUS_DONE
    assert row.phase == "done"
    assert row.resolved_digest is not None and row.resolved_digest.startswith("sha256:")
    assert row.resolved_size == 91739413
    assert row.total_bytes is not None and row.total_bytes > 0
    assert tags_calls["count"] == 2  # idempotency check + post-success re-verify
    queue.close()


def test_unknown_model_fails_with_normalized_model_not_found_error(
    tmp_path, monkeypatch
) -> None:
    """Ollama's own 'pull model manifest ...' error line is classified as
    ``model_not_found``
    with fixed canonical copy -- the raw upstream text (a curated string in
    this case, but the classifier does not special-case that) never rides
    through to the durable row verbatim; this error class is also terminal
    (no retry -- a nonexistent model will not start existing)."""
    root, queue, registry = _setup(tmp_path)
    monkeypatch.setenv("OLLAMA_URL", URL)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        assert request.url.path == "/api/pull"
        return httpx.Response(
            200, stream=_LineStream(_lines("pull-stream-unknown-model.jsonl"))
        )

    def client_factory():
        return httpx.Client(transport=httpx.MockTransport(handler))

    register_model_pull_handler(
        registry, workspace_root=root, queue=queue, client_factory=client_factory
    )
    pull_id, job_id = _enqueue_pull(queue, root, "totally-nonexistent-model")
    queue.claim("w1")

    with pytest.raises(Exception, match="model_not_found"):
        _run_handler(registry, queue, pull_id, job_id)

    row = store.get(queue.engine, pull_id)
    assert row.status == store.STATUS_FAILED
    assert row.error_code == "model_not_found"
    assert row.error_message == "the model was not found by the model server"
    assert len(row.error_message) <= 200
    queue.close()


# ---------------------------------------------------------------------------
# Provisioning token threading + worker error boundary + redaction canary.
# ---------------------------------------------------------------------------


def test_provisioning_token_sent_for_both_tags_and_pull_calls(tmp_path) -> None:
    """The pull worker sends the PROVISIONING token for both /api/tags and
    pull calls -- it never falls back
    to an inference token when no provisioning token is configured."""
    from frisket.server import provider_config

    root = tmp_path / "ws"
    root.mkdir()
    provider_config.create_local_endpoint(
        root,
        name="Authenticated Ollama",
        url=URL,
        inference_token="inference-should-not-be-used-here",
        provisioning_token="provisioning-secret",
        edge_auth=True,
    )
    queue = open_queue(workspace=root)
    registry = HandlerRegistry()
    seen_auth: list[str | None] = []
    tags_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("authorization"))
        if request.url.path == "/api/tags":
            tags_calls["count"] += 1
            if tags_calls["count"] == 1:
                return httpx.Response(200, json={"models": []})
            # post-success reconciliation (item 4) must find the ref.
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "smollm:135m", "digest": "sha256:abc", "size": 1}
                    ]
                },
            )
        return httpx.Response(200, stream=_LineStream(_lines("pull-stream-full.jsonl")))

    def client_factory():
        return httpx.Client(transport=httpx.MockTransport(handler))

    register_model_pull_handler(
        registry, workspace_root=root, queue=queue, client_factory=client_factory
    )
    pull_id, job_id = _enqueue_pull(queue, root, "smollm:135m")
    queue.claim("w1")

    result = _run_handler(registry, queue, pull_id, job_id)

    assert result["status"] == "done"
    assert seen_auth == ["Bearer provisioning-secret"] * len(seen_auth)
    assert len(seen_auth) >= 2  # idempotency tags-list + post-success re-verify
    queue.close()


def test_unauthorized_pull_reports_local_server_unauthorized(
    tmp_path, monkeypatch
) -> None:
    root, queue, registry = _setup(tmp_path)
    monkeypatch.setenv("OLLAMA_URL", URL)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    def client_factory():
        return httpx.Client(transport=httpx.MockTransport(handler))

    register_model_pull_handler(
        registry, workspace_root=root, queue=queue, client_factory=client_factory
    )
    pull_id, job_id = _enqueue_pull(queue, root, "smollm:135m")
    queue.claim("w1")

    with pytest.raises(Exception, match="local_server_unauthorized"):
        _run_handler(registry, queue, pull_id, job_id)

    row = store.get(queue.engine, pull_id)
    assert row.error_code == "local_server_unauthorized"
    queue.close()


CANARY_TOKEN = "sk-canary-FAKE-SECRET-TOKEN-must-never-persist"


def test_worker_error_boundary_never_leaks_a_planted_token_from_an_exception(
    tmp_path,
) -> None:
    """Worker error boundary canary: an UNEXPECTED exception mid-stream (not one of the enumerated
    `_fail()` classes) must become a bounded, enumerated-code message that
    never echoes the raw exception text -- planted here with a fake token
    string that would appear in `str(exc)` if this boundary didn't exist."""
    root, queue, registry = _setup(tmp_path)
    provider_config.create_local_endpoint(root, name="Ollama", url=URL)

    class _LeakyStream(httpx.SyncByteStream):
        def __iter__(self):
            raise RuntimeError(
                f"upstream blew up while using Authorization: Bearer {CANARY_TOKEN}"
            )

        def close(self):
            pass

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(200, stream=_LeakyStream())

    def client_factory():
        return httpx.Client(transport=httpx.MockTransport(handler))

    register_model_pull_handler(
        registry, workspace_root=root, queue=queue, client_factory=client_factory
    )
    # max_attempts=1: this attempt IS the final attempt (item 1b -- a
    # retryable failure only finalizes the row when it's the job's last
    # chance; forcing that here keeps this test's assertion about the ROW's
    # terminal state meaningful without conflating it with retry-divergence,
    # which has its own dedicated tests below).
    pull_id, job_id = _enqueue_pull(queue, root, "smollm:135m", max_attempts=1)
    queue.claim("w1")

    with pytest.raises(Exception) as excinfo:
        _run_handler(registry, queue, pull_id, job_id)

    assert CANARY_TOKEN not in str(excinfo.value)
    row = store.get(queue.engine, pull_id)
    assert row.status == store.STATUS_FAILED
    assert row.error_code == "pull_worker_error"
    assert CANARY_TOKEN not in (row.error_message or "")
    assert "RuntimeError" in (row.error_message or "")
    queue.close()


def test_worker_error_boundary_redacts_a_planted_token_from_an_error_body(
    tmp_path, caplog
) -> None:
    """A planted token inside an upstream-controlled error BODY (the pull stream's own
    structured "error" field) must also never persist to the durable row,
    even though that field is otherwise passed through as curated,
    user-facing text. The RAW upstream line (token-bearing or not) must never
    reach a log record either
    -- `caplog` captures every record emitted during the handler run and
    the assertions below scan ALL of them, not just the one the handler
    itself is expected to emit."""
    root, queue, registry = _setup(tmp_path)
    from frisket.server import provider_config

    provider_config.create_local_endpoint(
        root,
        name="Authenticated Ollama",
        url=URL,
        inference_token=None,
        provisioning_token=CANARY_TOKEN,
        edge_auth=False,
    )
    # A distinctive marker that is NOT the configured secret -- catches the
    # weaker bug where the log line is still the raw text with only the
    # KNOWN token value scrubbed out of it (`_redact` only catches
    # CONFIGURED values, so any other raw content -- like this marker --
    # would still leak through that narrower fix).
    raw_marker = "unique-non-secret-marker-xyzzy-should-never-appear-in-any-log"
    raw_upstream_text = (
        f"pull failed ({raw_marker}): token leaked back as {CANARY_TOKEN}"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(200, json={"error": raw_upstream_text})

    def client_factory():
        return httpx.Client(transport=httpx.MockTransport(handler))

    register_model_pull_handler(
        registry, workspace_root=root, queue=queue, client_factory=client_factory
    )
    pull_id, job_id = _enqueue_pull(queue, root, "smollm:135m")
    queue.claim("w1")

    with caplog.at_level(logging.DEBUG, logger="frisket.jobs.model_pull"):
        with pytest.raises(Exception):
            _run_handler(registry, queue, pull_id, job_id)

    row = store.get(queue.engine, pull_id)
    # The durable row gets ONLY canonical, templated copy -- not even a
    # "[redacted]" placeholder
    # derived from the raw upstream text, since the raw text never reaches
    # the row-writing code path at all (it is discarded before
    # classification).
    assert CANARY_TOKEN not in (row.error_message or "")
    assert row.error_code == "pull_failed"
    assert row.error_message == (
        f"the model server reported an error during the pull; correlation id {job_id}"
    )

    # The raw upstream line must never reach a log record -- only the
    # classification, correlation id, and
    # the line's LENGTH may appear, never its content (`_redact` only
    # scrubs CONFIGURED token values, so it is not a substitute for never
    # logging the line at all).
    for record in caplog.records:
        # `logging`'s `extra=` kwargs land as plain attributes on the
        # LogRecord, not inside `getMessage()` -- scan the WHOLE record
        # (message plus every attribute, built-in or `extra`-supplied), or
        # a raw value smuggled in via `extra` would slip past unnoticed.
        haystack = " ".join(
            str(value) for value in (record.getMessage(), *vars(record).values())
        )
        assert CANARY_TOKEN not in haystack
        assert raw_marker not in haystack
        assert raw_upstream_text not in haystack
    queue.close()


def test_cancellation_mid_stream_stops_cleanly(tmp_path, monkeypatch) -> None:
    root, queue, registry = _setup(tmp_path)
    monkeypatch.setenv("OLLAMA_URL", URL)
    engine_holder: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        assert request.url.path == "/api/pull"

        def maybe_cancel(index: int) -> None:
            if index == 2:
                store.request_cancel(engine_holder["engine"], engine_holder["pull_id"])

        return httpx.Response(
            200,
            stream=_LineStream(_lines("pull-stream-full.jsonl"), on_yield=maybe_cancel),
        )

    def client_factory():
        return httpx.Client(transport=httpx.MockTransport(handler))

    register_model_pull_handler(
        registry, workspace_root=root, queue=queue, client_factory=client_factory
    )
    pull_id, job_id = _enqueue_pull(queue, root, "smollm:135m")
    engine_holder["engine"] = queue.engine
    engine_holder["pull_id"] = pull_id
    queue.claim("w1")

    result = _run_handler(registry, queue, pull_id, job_id)

    assert result["status"] == "cancelled"
    row = store.get(queue.engine, pull_id)
    assert row.status == store.STATUS_CANCELLED
    assert row.cancel_requested_at is not None
    queue.close()


# ---------------------------------------------------------------------------
# Retry divergence: a retryable handler failure stays retryable.
# ---------------------------------------------------------------------------


def test_retryable_failure_before_final_attempt_resumes_same_row_on_retry(
    tmp_path, monkeypatch
) -> None:
    """Before the fix, ``_fail()`` marked the pull row 'failed' on ANY
    error -- even one the queue will retry -- so a second
    ``create_or_get_active`` call for the same ref, issued while the
    requeued job was still 'queued', created a SECOND active row (the
    partial-unique index excludes 'failed'). The fixed handler leaves the
    row ACTIVE across a retryable, non-final-attempt failure so the retried
    job resumes the SAME row and no duplicate can be created."""
    from frisket.engine.jobs.worker import Worker

    root, queue, registry = _setup(tmp_path)
    monkeypatch.setenv("OLLAMA_URL", URL)
    calls = {"tags": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            calls["tags"] += 1
            if calls["tags"] == 1:
                raise httpx.ConnectError("connection refused", request=request)
            if calls["tags"] == 2:
                return httpx.Response(200, json={"models": []})
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "smollm:135m", "digest": "sha256:abc", "size": 1}
                    ]
                },
            )
        return httpx.Response(200, stream=_LineStream(_lines("pull-stream-full.jsonl")))

    def client_factory():
        return httpx.Client(transport=httpx.MockTransport(handler))

    register_model_pull_handler(
        registry, workspace_root=root, queue=queue, client_factory=client_factory
    )
    pull_id, job_id = _enqueue_pull(queue, root, "smollm:135m", max_attempts=3)
    worker = Worker(queue, registry, retry_base_seconds=0.0, lease_seconds=60)

    # attempt 1 (of 3): a connect error on the idempotency tags check --
    # retryable, NOT the final attempt -- the row must stay ACTIVE.
    assert worker.run_once() is True
    row = store.get(queue.engine, pull_id)
    assert row.status == store.STATUS_RUNNING
    assert row.error_code == "local_server_unreachable"
    assert row.finished_at is None
    job = queue.get(job_id)
    assert job.status == "queued"  # requeued for retry
    assert job.attempts == 1

    # the SAME row is still the dedupe target -- no duplicate active row for
    # this ref, and the workspace slot (item 2) is still occupied by it.
    same, created = store.create_or_get_active(
        queue.engine, workspace_root=str(root), model_ref=row.model_ref
    )
    assert created is False
    assert same.id == pull_id
    with pytest.raises(store.ModelPullBusyError):
        store.create_or_get_active(
            queue.engine, workspace_root=str(root), model_ref="qwen3:8b"
        )

    # attempt 2: succeeds -- the SAME row reaches 'done'.
    assert worker.run_once() is True
    row = store.get(queue.engine, pull_id)
    assert row.status == store.STATUS_DONE
    job = queue.get(job_id)
    assert job.status == "done"
    queue.close()


def test_retryable_failure_on_final_attempt_finalizes_the_row(
    tmp_path, monkeypatch
) -> None:
    """The other half of item 1b: when the failing attempt IS the job's
    last, the queue's own `fail()` will terminal-fail the JOB regardless --
    the row must agree and finalize too, not stay active forever."""
    root, queue, registry = _setup(tmp_path)
    monkeypatch.setenv("OLLAMA_URL", URL)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    def client_factory():
        return httpx.Client(transport=httpx.MockTransport(handler))

    register_model_pull_handler(
        registry, workspace_root=root, queue=queue, client_factory=client_factory
    )
    pull_id, job_id = _enqueue_pull(queue, root, "smollm:135m", max_attempts=1)
    queue.claim("w1")

    with pytest.raises(Exception, match="local_server_unreachable"):
        _run_handler(registry, queue, pull_id, job_id)

    row = store.get(queue.engine, pull_id)
    assert row.status == store.STATUS_FAILED
    assert row.error_code == "local_server_unreachable"
    assert row.finished_at is not None
    queue.close()


def test_mark_running_refusal_aborts_cleanly_without_any_daemon_contact(
    tmp_path, monkeypatch
) -> None:
    """`mark_running` reports whether reactivation actually applied -- a refusal (the row
    is terminal in a way this job_id may NOT reactivate, e.g. an operator
    cancel, or the migration-reconciliation fix's own job-side fence) must
    make the handler abort IMMEDIATELY with a `NonRetryableJobError` tagged
    'row_terminal', before contacting any upstream server at all. This is
    the general fence: any terminal row whose job somehow still re-runs
    (an admin retry of a cancelled job, modeled directly here) becomes a
    clean no-op instead of a zombie pull reopening a row it no longer owns."""
    from frisket.engine.jobs.worker import NonRetryableJobError

    root, queue, registry = _setup(tmp_path)
    monkeypatch.setenv("OLLAMA_URL", URL)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must never contact the daemon when mark_running refuses")

    def client_factory():
        return httpx.Client(transport=httpx.MockTransport(handler))

    register_model_pull_handler(
        registry, workspace_root=root, queue=queue, client_factory=client_factory
    )
    pull_id, job_id = _enqueue_pull(queue, root, "smollm:135m")
    queue.claim("w1")

    # The row is permanently cancelled out-of-band (an operator cancel, or
    # -- the reviewed defect -- a migration reconciliation that terminalized
    # this row's job). `cancelled` is deliberately excluded from
    # `mark_running`'s reactivation OR, so even the row's OWN job_id cannot
    # reopen it.
    assert store.mark_cancelled(queue.engine, pull_id) is True

    with pytest.raises(NonRetryableJobError, match="row_terminal"):
        _run_handler(registry, queue, pull_id, job_id)

    row = store.get(queue.engine, pull_id)
    assert row.status == store.STATUS_CANCELLED
    queue.close()


# ---------------------------------------------------------------------------
# Endpoint fingerprint.
# ---------------------------------------------------------------------------


def test_endpoint_changed_after_queueing_is_a_terminal_failure(
    tmp_path, monkeypatch
) -> None:
    root, queue, registry = _setup(tmp_path)
    monkeypatch.setenv("OLLAMA_URL", URL)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not contact the server after an endpoint change")

    def client_factory():
        return httpx.Client(transport=httpx.MockTransport(handler))

    register_model_pull_handler(
        registry, workspace_root=root, queue=queue, client_factory=client_factory
    )
    engine = queue.engine
    endpoint, notes = provider_config.resolve_env_local_endpoint()
    assert notes == []
    assert endpoint is not None
    row, created = store.create_or_get_active(
        engine,
        workspace_root=str(root),
        model_ref=f"ollama/@{endpoint.endpoint_id}/smollm:135m",
        endpoint_id=endpoint.endpoint_id,
        endpoint_origin="http://a-completely-different-host:11434",
    )
    assert created is True
    job_id = queue.enqueue(
        "model.pull",
        {
            "pull_id": row.id,
            "workspace_root": str(root),
            "endpoint_id": row.endpoint_id,
        },
    )
    store.set_job_id(engine, row.id, job_id=job_id)
    queue.claim("w1")

    with pytest.raises(Exception, match="endpoint_changed"):
        _run_handler(registry, queue, row.id, job_id)

    after = store.get(engine, row.id)
    assert after.status == store.STATUS_FAILED
    assert after.error_code == "endpoint_changed"
    queue.close()


# ---------------------------------------------------------------------------
# Reconciliation.
# ---------------------------------------------------------------------------


def test_reconciliation_failure_when_model_absent_after_success_line(
    tmp_path, monkeypatch
) -> None:
    """A stream 'success' line alone is not "done" -- the post-pull re-list
    must actually find the canonical ref. If it does not (a truly odd
    upstream state), this is a terminal failure, never a silent 'done'."""
    root, queue, registry = _setup(tmp_path)
    monkeypatch.setenv("OLLAMA_URL", URL)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            # every /api/tags call (idempotency AND post-success reconcile)
            # reports nothing installed.
            return httpx.Response(200, json={"models": []})
        return httpx.Response(200, stream=_LineStream(_lines("pull-stream-full.jsonl")))

    def client_factory():
        return httpx.Client(transport=httpx.MockTransport(handler))

    register_model_pull_handler(
        registry, workspace_root=root, queue=queue, client_factory=client_factory
    )
    pull_id, job_id = _enqueue_pull(queue, root, "smollm:135m")
    queue.claim("w1")

    with pytest.raises(Exception, match="reconciliation_failed"):
        _run_handler(registry, queue, pull_id, job_id)

    row = store.get(queue.engine, pull_id)
    assert row.status == store.STATUS_FAILED
    assert row.error_code == "reconciliation_failed"
    queue.close()


# ---------------------------------------------------------------------------
# Provenance requirement: every completed pull needs a resolved digest and
# positive size. A
# tags entry with a missing digest or a null/non-positive size must never
# reach `mark_done`, on EITHER completion path (already-installed fast path,
# post-pull reconciliation).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "malformed_entry",
    [
        {"name": "smollm:135m", "size": 12345},  # missing digest
        {"name": "smollm:135m", "digest": ""},  # blank digest, missing size
        {"name": "smollm:135m", "digest": "sha256:abc", "size": None},  # null size
        {"name": "smollm:135m", "digest": "sha256:abc", "size": 0},  # non-positive size
        {"name": "smollm:135m", "digest": "sha256:abc", "size": -1},  # negative size
    ],
    ids=["missing_digest", "blank_digest", "null_size", "zero_size", "negative_size"],
)
def test_already_installed_with_malformed_provenance_is_reconciliation_failed(
    tmp_path, monkeypatch, malformed_entry
) -> None:
    """The already-installed fast path must REQUIRE a non-empty digest and a
    positive integer size from the matching tags entry -- never call
    `mark_done` with a null/blank digest or a null/non-positive size."""
    root, queue, registry = _setup(tmp_path)
    monkeypatch.setenv("OLLAMA_URL", URL)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [malformed_entry]})

    def client_factory():
        return httpx.Client(transport=httpx.MockTransport(handler))

    register_model_pull_handler(
        registry, workspace_root=root, queue=queue, client_factory=client_factory
    )
    pull_id, job_id = _enqueue_pull(queue, root, "smollm:135m")
    queue.claim("w1")

    with pytest.raises(Exception, match="reconciliation_failed"):
        _run_handler(registry, queue, pull_id, job_id)

    row = store.get(queue.engine, pull_id)
    assert row.status == store.STATUS_FAILED
    assert row.error_code == "reconciliation_failed"
    assert row.resolved_digest is None
    assert row.resolved_size is None


def test_post_pull_reconciliation_with_malformed_provenance_is_reconciliation_failed(
    tmp_path, monkeypatch
) -> None:
    """The post-pull reconciliation path (after a stream 'success' line)
    must ALSO require a usable digest+size -- a malformed match (present
    but missing size) is never treated as "done", never called with a null
    digest/size."""
    root, queue, registry = _setup(tmp_path)
    monkeypatch.setenv("OLLAMA_URL", URL)
    tags_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            tags_calls["count"] += 1
            if tags_calls["count"] == 1:
                return httpx.Response(200, json={"models": []})
            # post-success reconciliation finds the ref, but with no
            # usable size -- must fail closed, not "done" with a null.
            return httpx.Response(
                200,
                json={"models": [{"name": "smollm:135m", "digest": "sha256:abc"}]},
            )
        return httpx.Response(200, stream=_LineStream(_lines("pull-stream-full.jsonl")))

    def client_factory():
        return httpx.Client(transport=httpx.MockTransport(handler))

    register_model_pull_handler(
        registry, workspace_root=root, queue=queue, client_factory=client_factory
    )
    pull_id, job_id = _enqueue_pull(queue, root, "smollm:135m")
    queue.claim("w1")

    with pytest.raises(Exception, match="reconciliation_failed"):
        _run_handler(registry, queue, pull_id, job_id)

    row = store.get(queue.engine, pull_id)
    assert row.status == store.STATUS_FAILED
    assert row.error_code == "reconciliation_failed"
    assert row.resolved_digest is None
    assert row.resolved_size is None
    queue.close()


# ---------------------------------------------------------------------------
# _installed_entry case-sensitivity fix (item 4b)
# ---------------------------------------------------------------------------


def test_installed_entry_compares_name_case_insensitively_and_tag_case_sensitively() -> (
    None
):
    from frisket.engine.jobs.model_pull import _installed_entry

    models = [{"name": "Smollm:Latest", "digest": "sha256:aaa"}]
    # namespace/name compares case-insensitively.
    assert _installed_entry(models, "smollm:Latest") is not None
    # tag compares case-SENSITIVELY -- "latest" != "Latest" is a different tag.
    assert _installed_entry(models, "smollm:latest") is None
