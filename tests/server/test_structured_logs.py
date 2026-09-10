from __future__ import annotations

import io
import json
import logging
from collections.abc import Mapping

import pytest
from fastapi.testclient import TestClient
from uvicorn.logging import AccessFormatter

from frisket.engine.jobs import HandlerRegistry, SqliteJobQueue, Worker
from frisket.redaction import MAX_SAFE_FRAMES, SafeFrame
from frisket.server.app import create_app
from frisket.operability.structured_logging import (
    REDACTED,
    JsonLogFormatter,
    alert_log_extra,
    bind_log_context,
    configure_logging,
    set_log_verbosity,
)


@pytest.fixture
def structured_stream(monkeypatch):
    root = logging.getLogger()
    frisket_logger = logging.getLogger("frisket")
    old_handlers = list(root.handlers)
    old_level = root.level
    old_frisket_level = frisket_logger.level
    old_disable = root.manager.disable
    stream = io.StringIO()

    monkeypatch.delenv("FRISKET_LOG_LEVEL", raising=False)
    logging.disable(logging.NOTSET)
    configure_logging(stream=stream, force=True)
    set_log_verbosity("INFO")

    yield stream

    set_log_verbosity(None)
    logging.disable(old_disable)
    root.handlers = old_handlers
    root.setLevel(old_level)
    frisket_logger.setLevel(old_frisket_level)


def _log_lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def _format_frame_payload(value: object) -> tuple[dict, str]:
    record = logging.LogRecord(
        name="frisket.tests.frames",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="frame_probe",
        args=(),
        exc_info=None,
    )
    record.exception_frames = value
    rendered = JsonLogFormatter().format(record)
    return json.loads(rendered), rendered


def _render_uvicorn_access_log(
    monkeypatch: pytest.MonkeyPatch,
    request_target: str,
    *,
    status_code: int,
) -> str:
    stream = io.StringIO()
    logger = logging.getLogger("uvicorn.access")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        AccessFormatter(
            fmt='%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            use_colors=False,
        )
    )
    monkeypatch.setattr(logger, "handlers", [handler])
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(logger, "disabled", False)
    monkeypatch.setattr(logger, "level", logging.INFO)

    logger.info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:4312",
        "GET",
        request_target,
        "1.1",
        status_code,
    )
    return stream.getvalue()


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("token={raw_token}&next=/projects", id="token_first"),
        pytest.param("next=/projects&token={raw_token}", id="token_last"),
        pytest.param("%74oken={raw_token}", id="encoded_token_key"),
    ],
)
def test_uvicorn_access_log_redacts_callback_token(
    monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    raw_token = "magic-link-token-must-not-reach-access-log"
    rendered = _render_uvicorn_access_log(
        monkeypatch,
        f"/auth/callback?{query.format(raw_token=raw_token)}",
        status_code=302,
    )

    assert raw_token not in rendered
    assert "127.0.0.1:4312" in rendered
    assert "GET /auth/callback HTTP/1.1" in rendered
    assert "302 Found" in rendered


def test_uvicorn_access_log_omits_ordinary_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered = _render_uvicorn_access_log(
        monkeypatch,
        "/api/projects?page=2&sort=name",
        status_code=200,
    )

    assert '127.0.0.1:4312 - "GET /api/projects HTTP/1.1" 200 OK' in rendered
    assert "page=2" not in rendered
    assert "sort=name" not in rendered


def test_json_logs_redact_sensitive_fields(structured_stream):
    logger = logging.getLogger("frisket.tests.structured")

    with bind_log_context(
        trace_id="trace-redact",
        request_id="req-redact",
        run_id=17,
        job_id=29,
    ):
        logger.info(
            "redaction_probe",
            extra={
                "event": "redaction_probe",
                "api_key": "sk-live-secret",
                "stripe_key": "opaque-stripe-credential",
                "resend_key": "opaque-resend-credential",
                "database_url": "postgresql://user:password@example.test/db",
                "nested": {
                    "Authorization": "Bearer abc123",
                    "safe": "visible",
                },
            },
        )

    [entry] = _log_lines(structured_stream)
    assert entry["message"] == "redaction_probe"
    assert entry["trace_id"] == "trace-redact"
    assert entry["request_id"] == "req-redact"
    assert entry["run_id"] == 17
    assert entry["job_id"] == 29
    assert entry["api_key"] == REDACTED
    assert entry["stripe_key"] == REDACTED
    assert entry["resend_key"] == REDACTED
    assert entry["database_url"] == REDACTED
    assert entry["nested"] == {"Authorization": REDACTED, "safe": "visible"}
    assert "sk-live-secret" not in structured_stream.getvalue()
    assert "opaque-stripe-credential" not in structured_stream.getvalue()
    assert "opaque-resend-credential" not in structured_stream.getvalue()
    assert (
        "postgresql://user:password@example.test/db" not in structured_stream.getvalue()
    )
    assert "abc123" not in structured_stream.getvalue()


def test_runtime_verbosity_changes_without_reconfiguring(structured_stream):
    logger = logging.getLogger("frisket.tests.verbosity")

    set_log_verbosity("WARNING")
    logger.info("hidden_info")
    logger.warning("visible_warning")
    set_log_verbosity("DEBUG")
    logger.debug("visible_debug")

    messages = [entry["message"] for entry in _log_lines(structured_stream)]
    assert messages == ["visible_warning", "visible_debug"]


def test_alert_log_extra_standardizes_routing_fields(structured_stream):
    logger = logging.getLogger("frisket.tests.alerts")

    logger.error(
        "hosted_tenant_billing_update_failed",
        extra=alert_log_extra(
            "hosted_tenant_billing_update_failed",
            owner="Hosted",
            category="Tenant_Dispatch",
            severity="Critical",
            org_id=12,
            token="secret-token",
        ),
    )

    [entry] = _log_lines(structured_stream)
    assert entry["event"] == "hosted_tenant_billing_update_failed"
    assert entry["alert"] is True
    assert entry["alert_schema"] == "frisket.alert.v1"
    assert entry["alert_owner"] == "hosted"
    assert entry["alert_category"] == "tenant_dispatch"
    assert entry["alert_severity"] == "critical"
    assert entry["alert_routing_key"] == (
        "hosted.tenant_dispatch.critical.hosted_tenant_billing_update_failed"
    )
    assert entry["org_id"] == 12
    assert entry["token"] == REDACTED
    assert "secret-token" not in structured_stream.getvalue()


def test_alert_log_extra_rejects_unrouteable_names() -> None:
    with pytest.raises(ValueError, match="event must be lower snake_case"):
        alert_log_extra("Hosted Tenant Failed", owner="hosted", category="tenant")
    with pytest.raises(ValueError, match="severity must be one of"):
        alert_log_extra(
            "hosted_tenant_failed", owner="hosted", category="tenant", severity="page"
        )


def test_server_request_logs_json_with_request_and_trace_ids(
    tmp_path, structured_stream
):
    app = create_app(tmp_path / "workspace")
    client = TestClient(app)

    response = client.get(
        "/api/projects?api_key=should-not-log",
        headers={"x-request-id": "req-http", "x-trace-id": "trace-http"},
    )

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "req-http"
    completed = next(
        entry
        for entry in _log_lines(structured_stream)
        if entry.get("event") == "http_request_completed"
    )
    assert completed["logger"] == "frisket.server"
    assert completed["request_id"] == "req-http"
    assert completed["trace_id"] == "trace-http"
    assert completed["method"] == "GET"
    assert completed["path"] == "/api/projects"
    assert completed["status_code"] == 200
    assert "should-not-log" not in structured_stream.getvalue()


def test_worker_logs_job_context_and_redacts_handler_fields(
    tmp_path, structured_stream
):
    queue = SqliteJobQueue(tmp_path / "queue.db")
    registry = HandlerRegistry()

    def handle(payload: dict, _context) -> dict:
        logging.getLogger("frisket.tests.worker").info(
            "handler_payload_seen",
            extra={
                "event": "handler_payload_seen",
                "payload": {"api_key": payload["api_key"], "safe": "visible"},
            },
        )
        return {"ok": True}

    registry.register("demo", handle)
    job_id = queue.enqueue(
        "demo",
        {
            "project_id": "project-a",
            "run_id": 42,
            "request_id": "req-job",
            "trace_id": "trace-job",
            "api_key": "queued-secret",
        },
        max_attempts=1,
    )

    try:
        assert Worker(queue, registry, worker_id="worker-a").run_once()
        lines = _log_lines(structured_stream)
    finally:
        queue.close()

    started = next(entry for entry in lines if entry.get("event") == "job_started")
    completed = next(entry for entry in lines if entry.get("event") == "job_completed")
    handler = next(
        entry for entry in lines if entry.get("event") == "handler_payload_seen"
    )

    for entry in (started, completed, handler):
        assert entry["job_id"] == job_id
        assert entry["project_id"] == "project-a"
        assert entry["run_id"] == 42
        assert entry["request_id"] == "req-job"
        assert entry["trace_id"] == "trace-job"

    assert handler["payload"] == {"api_key": REDACTED, "safe": "visible"}
    assert "queued-secret" not in structured_stream.getvalue()


def test_exception_log_uses_bounded_frames_without_raw_traceback(structured_stream):
    logger = logging.getLogger("frisket.tests.exception")
    sentinel = "sk-structured-exception-sentinel"

    try:
        try:
            raise ValueError(f"cause api_key={sentinel}")
        except ValueError as cause:
            local_secret = sentinel
            raise RuntimeError(f"api_key={local_secret}\nouter failure") from cause
    except RuntimeError:
        logger.exception(
            "structured_exception_probe",
            stack_info=True,
            extra={"event": "structured_exception_probe", "error_code": "probe_error"},
        )

    [entry] = _log_lines(structured_stream)
    assert entry["error_code"] == "probe_error"
    assert entry["exception_type"] == "RuntimeError"
    assert entry["exception"] == "api_key=[REDACTED] outer failure"
    assert "stack" not in entry
    assert 1 <= len(entry["exception_frames"]) <= 12
    for frame in entry["exception_frames"]:
        assert set(frame) == {"module", "path", "function", "line"}
        assert not frame["path"].startswith("/")
        assert frame["line"] > 0
    serialized = structured_stream.getvalue()
    assert sentinel not in serialized
    assert "Traceback (most recent call last)" not in serialized
    assert "raise RuntimeError" not in serialized
    assert "ValueError" not in serialized


def test_formatter_is_cycle_safe_and_keeps_observability_sessions(structured_stream):
    logger = logging.getLogger("frisket.tests.cycle")
    cyclic: dict[str, object] = {"api_key": "sk-cycle-sentinel"}
    cyclic["self"] = cyclic

    logger.info(
        "cycle_probe",
        extra={
            "event": "cycle_probe",
            "payload": cyclic,
            "session": "interactive",
            "session_id": "sess-123",
            "request_id": "req-123",
            "auth_session_id": "auth-secret",
        },
    )

    [entry] = _log_lines(structured_stream)
    assert entry["payload"] == {"api_key": REDACTED, "self": "[CYCLE]"}
    assert entry["session"] == "interactive"
    assert entry["session_id"] == "sess-123"
    assert entry["request_id"] == "req-123"
    assert entry["auth_session_id"] == REDACTED


def test_formatter_rejects_caller_supplied_frame_mappings(structured_stream):
    logging.getLogger("frisket.tests.frames").error(
        "frame_probe",
        extra={
            "event": "frame_probe",
            "exception_frames": [
                {
                    "path": "/private/secret.py",
                    "function": "run",
                    "line": 1,
                    "source": "api_key=sk-frame-sentinel",
                }
            ],
        },
    )

    [entry] = _log_lines(structured_stream)
    assert "exception_frames" not in entry
    assert "/private/secret.py" not in structured_stream.getvalue()
    assert "sk-frame-sentinel" not in structured_stream.getvalue()


def test_formatter_failure_falls_back_to_constant_json() -> None:
    record = logging.LogRecord(
        name="frisket.tests.boundary",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="boundary_probe",
        args=(),
        exc_info=None,
    )
    record.created = None  # type: ignore[assignment] - a malformed record

    rendered = JsonLogFormatter().format(record)

    assert json.loads(rendered) == {
        "level": "ERROR",
        "message": "[LOG FORMAT FAILED]",
    }


@pytest.mark.parametrize("container_type", [list, tuple])
def test_formatter_accepts_plain_frame_containers_with_the_global_bound(
    container_type,
) -> None:
    frames = [
        SafeFrame(None, f"frisket/frame_{index}.py", "run", index + 1)
        for index in range(MAX_SAFE_FRAMES + 3)
    ]
    entry, _ = _format_frame_payload(container_type(frames))

    assert len(entry["exception_frames"]) == MAX_SAFE_FRAMES
    assert entry["exception_frames"][0]["path"] == "frisket/frame_0.py"
    assert entry["exception_frames"][-1]["path"] == (
        f"frisket/frame_{MAX_SAFE_FRAMES - 1}.py"
    )


def test_formatter_accepts_one_plain_safe_frame() -> None:
    entry, _ = _format_frame_payload(
        SafeFrame("frisket.worker", "frisket/x.py", "run", 7)
    )

    assert entry["exception_frames"] == [
        {
            "module": "frisket.worker",
            "path": "frisket/x.py",
            "function": "run",
            "line": 7,
        }
    ]


def test_formatter_handles_buggy_str_arguments() -> None:
    class Buggy:
        def __str__(self) -> str:
            raise RuntimeError("buggy str")

    record = logging.LogRecord(
        name="frisket.tests.buggy",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="value=%s",
        args=(Buggy(),),
        exc_info=None,
    )

    entry = json.loads(JsonLogFormatter().format(record))
    assert entry["message"] == "value=[UNPRINTABLE Buggy]"


def test_formatter_interpolates_before_assignment_redaction() -> None:
    record = logging.LogRecord(
        name="frisket.tests.formatting",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="api_key=%s status=%s",
        args=("opaque-nonpattern", "failed"),
        exc_info=None,
    )

    entry = json.loads(JsonLogFormatter().format(record))
    assert entry["message"] == "api_key=[REDACTED] status=failed"


def test_formatter_sanitizes_mapping_interpolation_arguments() -> None:
    record = logging.LogRecord(
        name="frisket.tests.formatting",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="api_key=%(api_key)s status=%(status)s",
        args=({"api_key": "opaque-nonpattern", "status": "ok"},),
        exc_info=None,
    )

    entry = json.loads(JsonLogFormatter().format(record))
    assert entry["message"] == "api_key=[REDACTED] status=ok"


def test_formatter_bounds_infinite_mapping_output() -> None:
    class InfiniteMapping(Mapping[str, str]):
        def __init__(self) -> None:
            self.reads = 0

        def __getitem__(self, key: str) -> str:
            return key

        def __iter__(self):
            index = 0
            while True:
                self.reads += 1
                yield f"item_{index}"
                index += 1

        def __len__(self) -> int:
            return 2**31

    value = InfiniteMapping()
    record = logging.LogRecord(
        name="frisket.tests.mapping",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="mapping_probe",
        args=(),
        exc_info=None,
    )
    record.payload = value

    entry = json.loads(JsonLogFormatter().format(record))
    assert entry["payload"]["[TRUNCATED]"] == "[TRUNCATED]"
    assert len(entry["payload"]) <= 257
    assert value.reads <= 257
