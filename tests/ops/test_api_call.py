"""`map.api_call` fetch op: per-row templated request -> parsed JSON in a
hidden api_result column. All HTTP is mocked (httpx.MockTransport); the SSRF
guard's DNS check is stubbed for the happy paths and exercised for real on the
blocked-URL case (a link-local/metadata IP literal, deterministic offline).

Secrets resolve via os.environ (resolve_credential checks it first), so
`monkeypatch.setenv` stands in for a real project credential store."""

from __future__ import annotations

import asyncio
import json
import math

import httpx
import pytest

from frisket.ai.llm import ModelRouter
from frisket.actions.http_types import (
    MAX_API_CALL_TIMEOUT_SECONDS,
    HttpRequest,
    request_referenced_columns as referenced_columns,
)
from frisket.actions.types import ActionRequest, Row, RowError, discover_references
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.executor.http_request import (
    AdmittedHttpRequester,
    HttpRequestCancelled,
    _pace_request,
)
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store import Project
from frisket.ops.base import OpContext


def _ctx(handler) -> OpContext:
    return OpContext(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _run(row_values, spec, ctx):
    request = HttpRequest.model_validate(
        {k: v for k, v in spec.items() if k != "output_name"}
    )
    try:
        result = asyncio.run(
            AdmittedHttpRequester(ctx).request_json(request, Row(row_values))
        )
        return {spec.get("output_name", "api_result"): result}
    except RowError as exc:
        return {
            "error": exc.message,
            "error_code": exc.code,
            **(
                {"outcome": "cancelled"}
                if isinstance(exc, HttpRequestCancelled)
                else {}
            ),
        }


def _bound(sheet=1, *, request=None, output="api_result"):
    return BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get("map.api_call"),
        ActionRequest(
            action_id="map.api_call",
            scope={"kind": "sheet_rows", "sheet_id": sheet},
            params={"request": request or {"url": "https://api.test.example/items"}},
            output_names={"api_result": output},
            idempotency_key="api-test",
        ),
    )


def _run_confirmed(project, body, router):
    result = run_action_spec(project, body, project_id="api", router=router)
    if result.status == "needs_confirmation":
        body = {**body, "confirmation": result.errors[0].details["promise_set_hash"]}
        result = run_action_spec(project, body, project_id="api", router=router)
    return result


@pytest.fixture
def _allow_egress(monkeypatch):
    """Bypass the SSRF DNS check so MockTransport hosts need not resolve.

    ``safe_request`` now resolves through ``safe_pinned_addresses`` (returning
    the addresses it pins the socket to), so the stub returns a vetted address
    rather than a bare ``True``. MockTransport opens no socket, so the returned
    address is never dialed."""
    monkeypatch.setattr(
        "frisket.ops.netguard.safe_pinned_addresses",
        lambda url, **kw: ["93.184.216.34"],
    )


def test_registered_only_in_typed_registry():
    entry = ACTION_REGISTRY.get("map.api_call").catalog_entry()
    assert entry["cost_policy"]["kind"] == "unknown"
    assert entry["cost_policy"]["requires_confirmation"]
    assert "external:api_call" in entry["required_capabilities"]


def test_output_fields_single_default_hidden_json_column(tmp_path):
    project, sheet = _sheet_with_columns(tmp_path)
    plan = build_typed_map_rows_plan(project, _bound(sheet, output="payload"))
    (field,) = plan.output_fields
    assert field["name"] == "payload" and field["column_type"] == "json"
    assert field["default_hidden"] is True and not field["hidden"]
    project.close()


@pytest.mark.parametrize(
    "name",
    [
        "confidence",
        "score_confidence",
        "outcome",
        "justification",
        "error",
        "error_code",
        "__field_errors__",
    ],
)
def test_control_like_output_names_are_safe_typed_columns(
    tmp_path, _allow_egress, name
):
    project, sheet = _sheet_with_columns(tmp_path)
    project.add_rows(sheet, [{}], {})
    router = ModelRouter(cache=None, cache_mode="off")
    router._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200, json={"error": "ordinary data", "confidence": 0.9}
            )
        )
    )
    body = _bound(sheet, output=name).request.model_dump(mode="json")
    result = _run_confirmed(project, body, router)
    assert result.status == "completed", result.errors
    output = next(item for item in project.columns(sheet) if item["name"] == name)
    assert list(project.get_values(sheet, output["id"]).values()) == [
        {"error": "ordinary data", "confidence": 0.9}
    ]
    asyncio.run(router._client.aclose())
    project.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout", math.inf),
        ("timeout", math.nan),
        ("timeout", MAX_API_CALL_TIMEOUT_SECONDS + 1),
        ("max_requests_per_second", math.inf),
        ("max_requests_per_second", math.nan),
        ("max_requests_per_second", 1e-309),
    ],
)
def test_invalid_limits_refuse_before_project_writes(tmp_path, field, value):
    project, sheet = _sheet_with_columns(tmp_path)
    with pytest.raises(ValueError):
        _bound(sheet, request={"url": "https://api.test.example/items", field: value})
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    project.close()


def test_semantic_request_discovers_only_active_column_tokens():
    params = _bound(
        request={
            "url": "https://x/{{id}}",
            "headers": [["Authorization", "{{secret.TOKEN}}"]],
            "form_body": [["q", "{{inactive}}"]],
        }
    ).params
    assert [ref.column for ref in discover_references(params)] == ["id"]
    assert discover_references(params)[0].accepted_column_types is None


def test_source_columns_scan_every_field():
    spec = {
        "url": "https://x/{{id}}",
        "headers": [["Authorization", "Bearer {{secret.TOKEN}}"]],
        "query_params": [["q", "{{term}}"]],
        "body": '{"note": "{{memo}}"}',
        "body_mode": "json",
    }
    cols = referenced_columns(spec)
    # columns unioned across url/headers/params/body; secret token excluded.
    assert set(cols) == {"id", "term", "memo"}
    assert "TOKEN" not in cols


def test_get_resolves_column_and_secret(_allow_egress, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "s3cr3t")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/items/42"
        assert request.headers["authorization"] == "Bearer s3cr3t"
        return httpx.Response(200, json={"ok": True, "id": 42})

    spec = {
        "method": "GET",
        "url": "https://api.test.example/items/{{id}}",
        "headers": [["Authorization", "Bearer {{secret.API_TOKEN}}"]],
        "output_name": "api_result",
    }
    out = _run({"id": "42"}, spec, _ctx(handler))
    assert out == {"api_result": {"ok": True, "id": 42}}


def test_missing_secret_errors_cell():
    # No env var, no project store -> the secret cannot resolve.
    spec = {
        "method": "GET",
        "url": "https://api.test.example/x",
        "headers": [["Authorization", "Bearer {{secret.NOPE}}"]],
    }
    out = _run({}, spec, OpContext(http=None, project=None))
    assert out["error_code"] == "missing_secret"
    assert "NOPE" not in out["error"]  # secret NAME leaks neither value nor id


def test_non_2xx_errors_http_error_without_body_or_secret(_allow_egress, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "s3cr3t")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503, json={"detail": "boom secret-ish leak", "value": "s3cr3t"}
        )

    spec = {
        "method": "GET",
        "url": "https://api.test.example/items/{{id}}?token={{secret.API_TOKEN}}",
        "output_name": "api_result",
    }
    out = _run({"id": "42"}, spec, _ctx(handler))
    assert out["error_code"] == "http_error"
    assert "503" in out["error"]
    # bounded text: no response body, no secret value, no query string
    assert "boom" not in out["error"]
    assert "s3cr3t" not in out["error"]
    assert "token=" not in out["error"]
    assert "GET" in out["error"] and "HTTP 503" in out["error"]


def test_non_json_2xx_errors_invalid_json(_allow_egress):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    spec = {"method": "GET", "url": "https://api.test.example/page"}
    out = _run({}, spec, _ctx(handler))
    assert out["error_code"] == "invalid_json"
    assert "200" in out["error"]


@pytest.mark.parametrize(
    "payload",
    [
        b'{"score": NaN}',
        b'{"score": Infinity}',
        b'{"score": -Infinity}',
        b'{"score": 1e999}',
    ],
)
def test_non_finite_json_response_errors_invalid_json(_allow_egress, payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=payload,
            headers={"content-type": "application/json"},
        )

    spec = {"method": "GET", "url": "https://api.test.example/page"}
    out = _run({}, spec, _ctx(handler))
    assert out["error_code"] == "invalid_json"
    assert "200" in out["error"]


@pytest.mark.parametrize(
    "payload",
    [
        b'{"role": "user", "role": "admin"}',
        b'{"result": {"role": "user", "role": "admin"}}',
    ],
)
def test_duplicate_json_response_key_errors_invalid_json(_allow_egress, payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=payload,
            headers={"content-type": "application/json"},
        )

    spec = {"method": "GET", "url": "https://api.test.example/page"}
    out = _run({}, spec, _ctx(handler))
    assert out["error_code"] == "invalid_json"
    assert "200" in out["error"]


def test_httpx_timeout_is_classified_as_timeout(_allow_egress):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("response took too long", request=request)

    spec = {"method": "GET", "url": "https://api.test.example/page"}
    out = _run({}, spec, _ctx(handler))
    assert out["error_code"] == "timeout"


def test_excessively_nested_json_response_is_row_local_invalid_json(_allow_egress):
    payload = b"[" * 10_000 + b"0" + b"]" * 10_000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    spec = {"method": "GET", "url": "https://api.test.example/page"}
    out = _run({}, spec, _ctx(handler))
    assert out["error_code"] == "invalid_json"


@pytest.mark.parametrize("payload", [None, [1, 2], "value", 42])
def test_non_object_json_errors_invalid_json_shape(_allow_egress, payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(payload).encode("utf-8"),
            headers={"content-type": "application/json"},
        )

    spec = {"method": "GET", "url": "https://api.test.example/page"}
    out = _run({}, spec, _ctx(handler))
    assert out["error_code"] == "invalid_json_shape"
    assert "200" in out["error"]


def test_post_json_body_mode_sends_structural_json(_allow_egress):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["content_type"] = request.headers.get("content-type")
        return httpx.Response(201, json={"created": True})

    spec = {
        "method": "POST",
        "url": "https://api.test.example/items",
        "body_mode": "json",
        # templated leaf stays a string; literal number stays a number
        "body": '{"name": "{{who}}", "count": 5}',
    }
    out = _run({"who": "alice"}, spec, _ctx(handler))
    assert out == {"api_result": {"created": True}}
    assert seen["body"] == {"name": "alice", "count": 5}
    assert seen["content_type"] == "application/json"


def test_invalid_rendered_url_normalizes_not_raises(_allow_egress):
    # A non-numeric port renders an invalid URL; httpx.InvalidURL is NOT an
    # httpx.HTTPError, so without an explicit catch it would escape and halt
    # the whole run after earlier rows' side effects.
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("an invalid URL must never reach the transport")

    spec = {"method": "POST", "url": "https://api.test.example:{{port}}/x"}
    out = _run({"port": "not-a-number"}, spec, _ctx(handler))
    assert out["error_code"] == "invalid_url"  # returned a cell, did not raise


def test_secret_in_url_host_not_leaked_in_error(_allow_egress, monkeypatch):
    monkeypatch.setenv("SECRET_HOST", "supersecrethost.example")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)  # force http_error so error context is built

    spec = {"method": "GET", "url": "https://{{secret.SECRET_HOST}}/items"}
    out = _run({}, spec, _ctx(handler))
    assert out["error_code"] == "http_error"
    assert "supersecrethost" not in out["error"]  # secret host blanked in context


def test_column_value_in_url_path_not_leaked_in_error(_allow_egress):
    # A {{column}} substitution is row-local user data, not a secret, but the
    # design doc still promises error text "omits request values" -- a URL
    # like /patients/{{ssn}} must not put the row's ssn into the persisted
    # error cell's path, same as a secret would never appear there.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)  # force http_error so error context is built

    spec = {"method": "GET", "url": "https://api.test.example/patients/{{ssn}}"}
    out = _run({"ssn": "123-45-6789"}, spec, _ctx(handler))
    assert out["error_code"] == "http_error"
    assert "123-45-6789" not in out["error"]  # column value blanked in context
    assert "api.test.example" not in out["error"]  # even a host may be derived data


def test_blocked_private_url_errors_blocked_url():
    # Real SSRF guard: a link-local/metadata IP literal is refused offline,
    # before the transport is ever hit.
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("blocked URL must never reach the transport")

    spec = {"method": "GET", "url": "http://169.254.169.254/latest/meta-data/"}
    out = _run({}, spec, _ctx(handler))
    assert out["error_code"] == "blocked_url"
    assert "169.254.169.254" not in out["error"]


def test_missing_referenced_column_fails_before_egress(_allow_egress):
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("missing columns must fail before the request")

    spec = {
        "method": "GET",
        "url": "https://api.test.example/items/{{id}}",
    }
    out = _run({}, spec, _ctx(handler))
    assert out["error_code"] == "missing_column"


def test_existing_blank_referenced_column_still_executes(_allow_egress):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/items/"
        return httpx.Response(200, json={"ok": True})

    spec = {
        "method": "GET",
        "url": "https://api.test.example/items/{{id}}",
    }
    out = _run({"id": None}, spec, _ctx(handler))
    assert out == {"api_result": {"ok": True}}


def test_recipe_allows_all_empty_source_values(tmp_path):
    project, sheet = _sheet_with_columns(tmp_path)
    assert (
        build_typed_map_rows_plan(project, _bound(sheet)).program.allow_all_empty_input
        is True
    )
    project.close()


def test_blank_rate_limit_normalizes_to_unlimited():
    params = HttpRequest(
        url="https://api.test.example/items",
        max_requests_per_second="",  # type: ignore[arg-type]
    )
    assert params.max_requests_per_second is None


@pytest.mark.parametrize("field", ["timeout", "max_requests_per_second"])
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_api_numeric_limits_reject_non_finite_values(field, value):
    with pytest.raises(ValueError):
        HttpRequest(
            url="https://api.test.example/items",
            **{field: value},
        )


def test_api_numeric_limits_accept_finite_values():
    params = HttpRequest(
        url="https://api.test.example/items",
        timeout=0.25,
        max_requests_per_second=2.5,
    )
    assert params.timeout == 0.25
    assert params.max_requests_per_second == 2.5


def test_api_rate_rejects_non_finite_pacing_interval():
    with pytest.raises(ValueError, match="invalid_params"):
        HttpRequest(
            url="https://api.test.example/items",
            max_requests_per_second=1e-309,
        )


def test_api_timeout_ceiling_boundary_accepted():
    # A slow endpoint must not hold a worker forever, but the cap itself is
    # a valid value, not an off-by-one exclusive bound.
    params = HttpRequest(
        url="https://api.test.example/items",
        timeout=MAX_API_CALL_TIMEOUT_SECONDS,
    )
    assert params.timeout == MAX_API_CALL_TIMEOUT_SECONDS


def test_api_timeout_exceeding_ceiling_rejected():
    with pytest.raises(ValueError):
        HttpRequest(
            url="https://api.test.example/items",
            timeout=MAX_API_CALL_TIMEOUT_SECONDS + 1,
        )


def test_capability_revalidates_actual_request_limits(_allow_egress):
    request = HttpRequest(url="https://api.test.example/items").model_copy(
        update={"timeout": MAX_API_CALL_TIMEOUT_SECONDS + 1}
    )
    with pytest.raises(RowError, match="Invalid HTTP request arguments"):
        asyncio.run(
            AdmittedHttpRequester(
                _ctx(lambda req: pytest.fail("must not egress"))
            ).request_json(request, Row({}))
        )


def test_rate_limiter_spaces_requests_with_shared_run_state(monkeypatch):
    clock = {"now": 100.0}
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        clock["now"] += delay

    monkeypatch.setattr(
        "frisket.engine.executor.http_request.time.monotonic", lambda: clock["now"]
    )
    monkeypatch.setattr(
        "frisket.engine.executor.http_request.asyncio.sleep", fake_sleep
    )
    ctx = OpContext(http=None, extras={"run_state": {}})

    async def pace_three() -> None:
        await _pace_request(ctx, 2.0)
        await _pace_request(ctx, 2.0)
        await _pace_request(ctx, 2.0)

    asyncio.run(pace_three())
    assert sum(delays) == pytest.approx(1.0)
    assert max(delays) <= 0.25


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_rate_limiter_rejects_non_finite_values(value):
    with pytest.raises(ValueError, match="invalid_params"):
        asyncio.run(_pace_request(OpContext(http=None), value))


def test_rate_limiter_rejects_non_finite_pacing_interval():
    with pytest.raises(ValueError, match="invalid_params"):
        asyncio.run(_pace_request(OpContext(http=None), 1e-309))


def test_rate_limiter_returns_false_on_cancellation_during_wait(monkeypatch):
    # A cooperative cancel observed while waiting for the shared bucket is
    # reported as "not acquired" (False), NOT raised: asyncio.CancelledError is
    # a BaseException on 3.11 and a manual raise would escape execute's
    # normalization and MapRunner's finalization.
    clock = {"now": 100.0}
    cancelled = {"value": False}

    async def fake_sleep(delay: float) -> None:
        clock["now"] += delay
        cancelled["value"] = True

    monkeypatch.setattr(
        "frisket.engine.executor.http_request.time.monotonic", lambda: clock["now"]
    )
    monkeypatch.setattr(
        "frisket.engine.executor.http_request.asyncio.sleep", fake_sleep
    )
    ctx = OpContext(
        http=None,
        extras={"run_state": {}, "cancelled": lambda: cancelled["value"]},
    )

    async def pace_twice() -> tuple[bool, bool]:
        first = await _pace_request(ctx, 0.5)
        second = await _pace_request(ctx, 0.5)
        return first, second

    first, second = asyncio.run(pace_twice())
    assert first is True
    assert second is False  # cancelled during the wait → not acquired, no raise
    assert clock["now"] == pytest.approx(100.25)


def test_execute_cooperative_cancel_returns_cell_without_egress(_allow_egress):
    # Cancel while paced → a normal "cancelled" cell, never egress, never raise.
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        calls.append(1)
        return httpx.Response(200, json={"ok": True})

    ctx = OpContext(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        extras={"cancelled": lambda: True},
    )
    spec = {
        "method": "GET",
        "url": "https://api.test.example/x",
        "max_requests_per_second": 5,
    }
    out = _run({}, spec, ctx)
    assert out["error_code"] == "cancelled"
    # Not a row FAILURE: "cancelled" sits outside FAILURE_OUTCOMES
    # (engine/store/runs.py), so it must not inflate failed_rows/receipt
    # status the way "model_error" (the fallback when no outcome is set)
    # would.
    assert out["outcome"] == "cancelled"
    assert calls == []  # never egressed

    from frisket.engine.store.runs import FAILURE_OUTCOMES, TERMINAL_OUTCOMES

    assert "cancelled" not in FAILURE_OUTCOMES
    assert "cancelled" not in TERMINAL_OUTCOMES


def test_referenced_columns_excludes_secrets():
    spec = {
        "url": "https://x/{{id}}",
        "headers": [["Authorization", "Bearer {{secret.TOKEN}}"]],
        "query_params": [["q", "{{term}}"]],
        "body_mode": "json",
        "body": '{"note": "{{memo}}"}',
    }
    # first-seen order across url/headers/query/body; the secret token is not a column.
    assert referenced_columns(spec) == ["id", "term", "memo"]
    assert "TOKEN" not in referenced_columns(spec)


def test_referenced_columns_match_active_body_mode():
    # A body/form token only counts when its mode is active — otherwise the
    # precheck would depend on a column the request can never render.
    base = {"url": "https://x/{{id}}", "form_body": [["k", "{{fcol}}"]]}
    # form_body inactive under json mode: fcol is not a dependency
    assert referenced_columns(
        {**base, "body_mode": "json", "body": '{"a":"{{bcol}}"}'}
    ) == [
        "id",
        "bcol",
    ]
    # under form mode the form pair is active; the (absent) json body isn't
    assert referenced_columns({**base, "body_mode": "form"}) == ["id", "fcol"]
    # under none, neither body nor form contributes
    assert referenced_columns({**base, "body_mode": "none"}) == ["id"]


def test_referenced_columns_json_keys_not_scanned():
    # JSON object keys are never rendered by build_json_body, so a key that
    # looks like a template must not become a required column.
    spec = {
        "url": "https://x",
        "body_mode": "json",
        "body": '{"{{notacol}}": "{{realcol}}"}',
    }
    assert referenced_columns(spec) == ["realcol"]


# --- pre-run precheck: referenced columns must exist BEFORE any request ----------
# resolve runs during action resolution, before MapRunner issues a single HTTP
# request, so a missing-column failure here is a pre-egress guard (critical for
# POST/PATCH writes). The columns that DO exist are recorded in input_column_ids
# for provenance, the same population every other map op's receipt reads.
def _sheet_with_columns(tmp_path):
    p = Project.create(tmp_path / "api.frisket", name="api")
    sheet = p.add_sheet("s")
    p.add_column(sheet, "id", type="text")
    p.add_column(sheet, "term", type="text")
    return p, sheet


def test_resolve_rejects_missing_referenced_column(tmp_path):
    project, sheet = _sheet_with_columns(tmp_path)
    with pytest.raises(ValueError, match="referenced input columns do not exist"):
        build_typed_map_rows_plan(
            project,
            _bound(
                sheet,
                request={
                    "url": "https://x/{{id}}",
                    "query_params": [["q", "{{amountt}}"]],
                },
            ),
        )
    project.close()


def test_resolve_records_existing_columns_for_provenance(tmp_path):
    project, sheet = _sheet_with_columns(tmp_path)
    plan = build_typed_map_rows_plan(
        project,
        _bound(
            sheet,
            request={"url": "https://x/{{id}}", "query_params": [["q", "{{term}}"]]},
        ),
    )
    assert set(plan.source_column_ids) == {"id", "term"}
    assert dict(plan.source_column_types) == {"id": "text", "term": "text"}
    project.close()


def test_resolve_ignores_secret_tokens(tmp_path):
    project, sheet = _sheet_with_columns(tmp_path)
    plan = build_typed_map_rows_plan(
        project,
        _bound(
            sheet,
            request={
                "url": "https://x/{{id}}",
                "headers": [["Authorization", "{{secret.API_TOKEN}}"]],
            },
        ),
    )
    assert set(plan.source_column_ids) == {"id"}
    project.close()


@pytest.mark.parametrize(
    ("failed_rows", "expected_status"),
    [(0, "completed"), (1, "partial"), (2, "failed")],
)
def test_receipt_status_provider_and_aggregate_errors(
    tmp_path, _allow_egress, failed_rows, expected_status
):
    project, sheet = _sheet_with_columns(tmp_path)
    cols = {item["name"]: item["id"] for item in project.columns(sheet)}
    project.add_rows(sheet, [{"id": "0"}, {"id": "1"}], cols)

    def handler(req):
        return httpx.Response(
            500 if int(req.url.path.rsplit("/", 1)[-1]) < failed_rows else 200,
            json={"ok": True},
        )

    router = ModelRouter(cache=None, cache_mode="off")
    router._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    body = _bound(sheet, request={"url": "https://x/{{id}}"}).request.model_dump(
        mode="json"
    )
    result = _run_confirmed(project, body, router)
    assert result.status == expected_status, result.errors
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert receipt.provider_use == [
        {
            "provider": "external_http",
            "service": "http.request",
            "external_api": True,
            "cost_actual": 0.0,
        }
    ]
    if expected_status == "failed":
        assert [error.code for error in receipt.errors] == ["external_rows_failed"]
        assert receipt.errors[0].details["failed_rows"] == 2
    else:
        assert receipt.errors == []
    asyncio.run(router._client.aclose())
    project.close()
