"""Default, fail-open model-call trace behavior."""

from __future__ import annotations

import gzip
import json
import math

import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import LLMError, LLMRequest, LLMResponse, ModelRouter
from frisket.engine.runner import CostGate, MapRunner
from frisket.engine.jobs import Worker
from frisket.server.app import create_app
from frisket.operability.trace import (
    RowTrace,
    TraceWriter,
    TracingRouter,
    read_trace,
    read_trace_row,
    trace_path,
)
from http_test_helpers import post_v1_action_with_exact_confirmation
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from typed_model_fixtures import prepare_model_run, run_with_output_claim

CSV = (
    "story\n"
    '"Mayor Linda Reyes met lobbyist Tom Quayle."\n'
    '"Joe Park denied knowing Sandra Ochoa."\n'
)


class _StubAdapter:
    def __init__(self, reply: dict, *, fail_first: int = 0):
        self._reply = reply
        self._failures_left = fail_first
        self.calls = 0

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        self.calls += 1
        if self._failures_left > 0:
            self._failures_left -= 1
            raise LLMError("stub: 429 rate limited", status=429, retryable=True)
        return LLMResponse(
            content=json.dumps(self._reply),
            data=dict(self._reply),
            tokens_in=50,
            tokens_out=50,
            cost=0.0,
            model=req.model,
        )


def _client(adapter: _StubAdapter, tmp_path) -> TestClient:
    router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    return TestClient(create_app(tmp_path / "ws", router=router))


def _seed(client: TestClient) -> tuple[str, int]:
    pid = client.post("/api/projects", json={"name": "T"}).json()["id"]
    r = client.post(
        f"/api/projects/{pid}/import/csv", files={"file": ("s.csv", CSV, "text/csv")}
    )
    assert r.status_code == 200, r.text
    return pid, r.json()["sheet_id"]


def _spec(sid: int) -> dict:
    return {
        "action_kind": "map.classify",
        "model": "anthropic/claude-haiku-4-5",
        "sheet_id": sid,
        "input_columns": ["story"],
        "fields": [{"name": "beat", "type": "category", "labels": ["a", "b"]}],
    }


def _action(sid: int, *, key: str = "map_classify@sha256:trace") -> dict:
    """The typed classify request the HTTP tests post; consent is the exact
    402 token echoed as top-level ``confirmation``."""
    return {
        "action_id": "map.classify",
        "scope": {"kind": "sheet_rows", "sheet_id": sid},
        "params": {
            "source": ["story"],
            "engine": "llm",
            "model": "anthropic/claude-haiku-4-5",
            "fields": [{"name": "beat", "type": "category", "labels": ["a", "b"]}],
        },
        "idempotency_key": key,
    }


def _run(client: TestClient, pid: str, action: dict) -> int:
    r = post_v1_action_with_exact_confirmation(client, pid, action)
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    ws = client.app.state.workspace
    worker = Worker(ws.queue, ws.registry, worker_id="trace-test-worker")
    while worker.run_once():
        pass
    return run_id


def test_model_run_traces_by_default_and_threads_trace_id(tmp_path):
    client = _client(_StubAdapter({"beat": "a"}), tmp_path)
    pid, sid = _seed(client)
    rid = _run(client, pid, _action(sid))

    response = client.get(f"/api/projects/{pid}/actions/runs/{rid}/trace")
    assert response.status_code == 200, response.text
    body = response.json()["trace"]
    assert body["trace_id"], "run trace carries no trace_id"
    assert body["action_kind"] == "map.classify"
    assert body["action_kind"] == "map.classify"
    assert body["action_name"] == "Classify rows"
    rows = body["rows"]
    assert len(rows) == 2, "one trace row per data row"
    bundles = [p for p in (tmp_path / "ws").iterdir() if (p / "project.db").exists()]
    sidecar = trace_path(bundles[0], rid)
    assert sidecar.relative_to(bundles[0]).as_posix() == f"traces/run-{rid}.jsonl.gz"
    assert sidecar.read_bytes().startswith(b"\x1f\x8b")
    assert list((bundles[0] / "traces").glob(f"run-{rid}*")) == [sidecar]
    for row in rows:
        assert row["trace_id"] == body["trace_id"], "trace_id must thread rows"
        # the RENDERED prompt — actual row content, not a template
        prompt_text = json.dumps(row["prompt"])
        assert "Linda Reyes" in prompt_text or "Joe Park" in prompt_text
        assert json.loads(row["raw_response"]) == {"beat": "a"}
        assert row["tokens_in"] == 50 and row["tokens_out"] == 50
        assert row["latency_ms"] >= 0
        assert row["retries"] == []
        assert row["calls"], "per-call transcript missing"
        assert row["calls"][0]["events"], "router attempt events missing"


def test_trace_writer_redacts_every_serialized_record(tmp_path):
    secret = "checkpoint1b-trace-secret"
    writer = TraceWriter.open(tmp_path, 7)
    writer.write_row(
        {
            "kind": "row",
            "row_id": 1,
            "error": (
                "Traceback (most recent call last):\n"
                '  File "/private/secret.py", line 1, in boom\n'
                f"RuntimeError: {secret}"
            ),
            "nested": {"api_key": secret, "detail": f"token={secret}"},
            "calls": [{"model": "anthropic/test"}],
        }
    )
    with gzip.open(writer.path, "rt", encoding="utf-8") as stream:
        raw = stream.read()
    assert secret not in raw
    assert "Traceback (most recent call last):" not in raw
    assert '"api_key": "[REDACTED]"' in raw


def test_trace_writer_is_lazy_and_write_failures_are_fail_open(
    tmp_path, monkeypatch
) -> None:
    writer = TraceWriter.open(tmp_path, 9)
    writer.write_row({"kind": "row", "row_id": 1, "calls": []})
    assert not writer.path.exists()

    def refuse_write(*_args, **_kwargs):
        raise OSError("unwritable")

    monkeypatch.setattr("frisket.operability.trace.gzip.open", refuse_write)
    writer.write_row(
        {
            "kind": "row",
            "row_id": 1,
            "calls": [{"model": "anthropic/test"}],
        }
    )
    assert not writer.path.exists()


def test_trace_writer_drops_non_finite_json(tmp_path) -> None:
    writer = TraceWriter.open(tmp_path, 72)
    writer.write_row(
        {
            "kind": "row",
            "row_id": 1,
            "cost": math.nan,
            "calls": [{"model": "anthropic/test"}],
        }
    )

    assert not writer.path.exists(), "unsafe JSON left a partial or meta-only trace"


def test_trace_capture_failure_preserves_successful_provider_response() -> None:
    import asyncio

    secret = "checkpoint1b-redaction-boundary-failure"

    class Inner:
        def secret_values_for_model(self, _model):  # noqa: ANN001
            raise RuntimeError("credential lookup failed")

        async def complete(self, req, *, recipe_version, trace):  # noqa: ANN001
            trace.append({"event": "attempt", "detail": secret})
            return LLMResponse(
                content=secret,
                data={"answer": secret},
                model=req.model,
                tokens_in=1,
                tokens_out=1,
                cost=0.0,
            )

    async def run() -> tuple[LLMResponse, RowTrace, dict | None]:
        row = RowTrace(row_id=1, trace_id="trace")
        router = TracingRouter(Inner(), row)
        response = await router.complete(
            LLMRequest(
                model="anthropic/test",
                messages=[{"role": "user", "content": secret}],
            )
        )
        return response, row, router.record(data={"answer": secret})

    response, row, record = asyncio.run(run())
    assert response.data == {"answer": secret}
    assert row.calls == []
    assert record is None


def test_tracing_router_uses_explicit_complete_and_transport_seams() -> None:
    import asyncio

    calls: list[tuple[str, list[dict]]] = []

    class Inner:
        def secret_values_for_model(self, _model):  # noqa: ANN001
            return ()

        async def complete(self, req, *, recipe_version, trace):  # noqa: ANN001
            trace.append({"event": "complete"})
            calls.append(("complete", trace))
            return LLMResponse(
                content="ok",
                data={"answer": "ok"},
                model=req.model,
                tokens_in=1,
                tokens_out=1,
                cost=0.0,
            )

        async def complete_transport(
            self,
            req,
            *,
            recipe_version,
            trace,  # noqa: ANN001
        ):
            trace.append({"event": "transport"})
            calls.append(("transport", trace))
            return LLMResponse(
                content="ok",
                data={"answer": "ok"},
                model=req.model,
                tokens_in=1,
                tokens_out=1,
                cost=0.0,
            )

    async def run() -> tuple[list[dict], list[dict], RowTrace]:
        row = RowTrace(row_id=1, trace_id="trace")
        router = TracingRouter(Inner(), row)
        req = LLMRequest(
            model="anthropic/test",
            messages=[{"role": "user", "content": "hello"}],
        )
        complete_trace: list[dict] = []
        transport_trace: list[dict] = []
        await router.complete(req, trace=complete_trace)
        await router.complete_transport(req, trace=transport_trace)
        return complete_trace, transport_trace, row

    complete_trace, transport_trace, row = asyncio.run(run())
    assert calls == [("complete", complete_trace), ("transport", transport_trace)]
    assert complete_trace == [{"event": "complete"}]
    assert transport_trace == [{"event": "transport"}]
    assert [call["events"] for call in row.calls] == [
        complete_trace,
        transport_trace,
    ]


def test_tracing_router_transport_does_not_fall_back_to_complete() -> None:
    import asyncio

    complete_calls = 0

    class CompleteOnly:
        def secret_values_for_model(self, _model):  # noqa: ANN001
            return ()

        async def complete(self, req, *, recipe_version, trace):  # noqa: ANN001
            nonlocal complete_calls
            complete_calls += 1
            raise AssertionError("transport must not fall back to complete")

    async def run() -> None:
        router = TracingRouter(
            CompleteOnly(),  # type: ignore[arg-type] -- intentionally incomplete
            RowTrace(row_id=1, trace_id="trace"),
        )
        await router.complete_transport(
            LLMRequest(
                model="anthropic/test",
                messages=[{"role": "user", "content": "hello"}],
            )
        )

    with pytest.raises(AttributeError):
        asyncio.run(run())
    assert complete_calls == 0


def test_trace_setup_failure_does_not_fail_the_run(tmp_path, monkeypatch) -> None:
    client = _client(_StubAdapter({"beat": "a"}), tmp_path)
    pid, sheet_id = _seed(client)

    def refuse_setup(*_args, **_kwargs):
        raise OSError("trace directory unavailable")

    monkeypatch.setattr(TraceWriter, "open", refuse_setup)
    run_id = _run(client, pid, _action(sheet_id))
    assert run_id > 0
    data = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()
    assert next(column for column in data["columns"] if column["name"] == "beat")[
        "ai_generated"
    ]


def test_tracing_router_redacts_selected_adapter_key_from_prompt_and_response(
    tmp_path,
) -> None:
    import asyncio

    secret = "checkpoint1b-tracing-router-secret"

    class Adapter:
        api_key = secret

    class Inner:
        def adapter_for(self, provider):  # noqa: ANN001
            assert provider == "anthropic"
            return Adapter()

        def secret_values_for_model(self, model):  # noqa: ANN001
            assert model == "anthropic/test"
            return (secret,)

        async def complete(self, req, *, recipe_version, trace):  # noqa: ANN001
            trace.append({"event": "attempt", "detail": secret})
            return LLMResponse(
                content=secret,
                data={"answer": secret},
                tokens_in=1,
                tokens_out=1,
                cost=0,
                model=req.model,
                raw={"provider_echo": secret},
            )

    async def run() -> tuple[RowTrace, dict]:
        row = RowTrace(row_id=1, trace_id="trace")
        router = TracingRouter(Inner(), row)
        await router.complete(
            LLMRequest(
                model="anthropic/test",
                messages=[{"role": "user", "content": f"prompt {secret}"}],
            )
        )
        return row, router.record(data={"answer": secret})

    row, record = asyncio.run(run())
    assert secret not in json.dumps(row.calls)
    assert secret not in json.dumps(record)
    writer = TraceWriter.open(tmp_path, 8)
    writer.write_row(record)
    with gzip.open(writer.path, "rt", encoding="utf-8") as stream:
        assert secret not in stream.read()


def test_tracing_router_final_record_resolves_observed_models_without_key_cache():
    import asyncio

    first_secret = "checkpoint1b-first-observed-provider"
    borrowed_secret = "checkpoint1b-borrowed-provider"

    class Adapter:
        def __init__(self, key):  # noqa: ANN001
            self.api_key = key

    class Inner:
        adapters = {
            "anthropic": Adapter(first_secret),
            "openai": Adapter(borrowed_secret),
        }

        def adapter_for(self, provider):  # noqa: ANN001
            return self.adapters.get(provider)

        def secret_values_for_model(self, model):  # noqa: ANN001
            provider = model.split("/", 1)[0]
            adapter = self.adapters.get(provider)
            return (adapter.api_key,) if adapter is not None else ()

        async def complete(self, req, *, recipe_version, trace):  # noqa: ANN001
            return LLMResponse(
                content="ok",
                data={"answer": first_secret},
                tokens_in=1,
                tokens_out=1,
                cost=0,
                model=req.model,
            )

    async def run() -> dict:
        row = RowTrace(row_id=1, trace_id="trace")
        router = TracingRouter(Inner(), row)
        await router.complete(
            LLMRequest(
                model="anthropic/test", messages=[{"role": "user", "content": "x"}]
            )
        )
        assert router.adapter_for("openai") is not None
        assert router._observed_models == ["anthropic/test", "openai"]  # noqa: SLF001
        assert not hasattr(router, "_observed_secret_values")
        return router.record(data={"first": first_secret, "borrowed": borrowed_secret})

    record = asyncio.run(run())
    serialized = json.dumps(record)
    assert first_secret not in serialized
    assert borrowed_secret not in serialized

    no_call_row = RowTrace(row_id=2, trace_id="trace")
    no_call_router = TracingRouter(Inner(), no_call_row)
    assert no_call_router.adapter_for("openai") is not None
    assert no_call_router._observed_models == ["openai"]  # noqa: SLF001
    assert not hasattr(no_call_router, "_observed_secret_values")

    class HostileMeta:
        def __str__(self) -> str:
            raise RuntimeError(borrowed_secret)

    no_call_record = no_call_router.record(
        data={"borrowed": borrowed_secret},
        meta={
            "tokens_in": borrowed_secret,
            "tokens_out": borrowed_secret,
            "cost": HostileMeta(),
        },
    )
    assert no_call_row.calls == []
    assert borrowed_secret not in json.dumps(no_call_record)
    assert no_call_record["tokens_in"] == "[REDACTED]"
    assert no_call_record["tokens_out"] == "[REDACTED]"
    assert no_call_record["cost"] == "[UNPRINTABLE HostileMeta]"

    numeric_record = no_call_router.record(
        meta={"tokens_in": 7, "tokens_out": 3, "cost": 0.25}
    )
    assert numeric_record["tokens_in"] == 7
    assert numeric_record["tokens_out"] == 3
    assert numeric_record["cost"] == 0.25


def test_trace_records_retry_attempts_with_errors(tmp_path, monkeypatch):
    # first adapter call 429s; the router retries and succeeds — the trace
    # must show that failed attempt with its error text. Driven at the
    # runner level so the router's retry backoff doesn't race the HTTP
    # endpoint's startup window.
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    import asyncio

    from frisket.engine.store import Project

    p = Project.create(tmp_path / "p.frisket")
    sheet = p.add_sheet("data")
    cols = {"story": p.add_column(sheet, "story")}
    p.add_rows(sheet, [{"story": "Mayor Reyes met Quayle."}], cols)

    router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = _StubAdapter(  # noqa: SLF001
        {"beat": "a"}, fail_first=1
    )
    runner = MapRunner(p, router, authority=UnroutedOnlyAuthority(p))
    spec = _spec(sheet)
    with pytest.raises(CostGate) as quote:
        prepare_model_run(runner, spec)
    assert quote.value.promise_set_hash
    spec["consented_promise_set_hash"] = quote.value.promise_set_hash
    prog = asyncio.run(run_with_output_claim(runner, spec, confirmed=True))
    assert prog.done and prog.failed == 0

    rows = read_trace(p.path, prog.run_id)["rows"]
    retried = [r for r in rows if r["retries"]]
    assert retried, "no retry recorded despite an injected 429"
    err = retried[0]["retries"][0]
    assert "429" in err["error"] and err["status"] == 429 and err["retryable"]
    # the row still succeeded after the retry
    assert json.loads(retried[0]["raw_response"]) == {"beat": "a"}
    p.close()


def test_removed_debug_param_is_rejected(tmp_path):
    client = _client(_StubAdapter({"beat": "a"}), tmp_path)
    pid, sid = _seed(client)
    action = _action(sid)
    action["params"]["debug"] = True
    response = client.post(f"/api/projects/{pid}/actions/v1/run", json=action)
    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "invalid_params"


def test_trace_is_a_disposable_sidecar(tmp_path):
    # deleting the sidecar loses ONLY diagnostic detail: results stay intact.
    client = _client(_StubAdapter({"beat": "a"}), tmp_path)
    pid, sid = _seed(client)
    rid = _run(client, pid, _action(sid))

    ws = tmp_path / "ws"
    bundles = [p for p in ws.iterdir() if (p / "project.db").exists()]
    sidecar = trace_path(bundles[0], rid)
    assert sidecar.exists()
    sidecar.unlink()

    r = client.get(f"/api/projects/{pid}/actions/runs/{rid}/trace")
    assert r.status_code == 200, r.text
    assert r.json()["recorded"] is False
    assert r.json()["trace"] is None
    data = client.get(f"/api/projects/{pid}/sheets/{sid}/data").json()
    beat = next(c for c in data["columns"] if c["name"] == "beat")
    assert beat["ai_generated"], "deleting the trace must not touch results"


def test_corrupt_trace_is_reported_as_unavailable(tmp_path):
    sidecar = trace_path(tmp_path, 11)
    sidecar.parent.mkdir(parents=True)
    sidecar.write_bytes(b"not gzip")
    assert read_trace(tmp_path, 11) is None


def test_corrupt_trace_tail_makes_the_whole_trace_unavailable(tmp_path):
    writer = TraceWriter.open(tmp_path, 12)
    writer.write_row(
        {
            "kind": "row",
            "row_id": 4,
            "calls": [{"model": "anthropic/test"}],
        }
    )
    with writer.path.open("ab") as stream:
        stream.write(b"broken gzip tail")

    assert read_trace(tmp_path, 12) is None
    assert read_trace_row(tmp_path, 12, 4) is None


@pytest.mark.parametrize(
    "corrupt_line",
    [b'{"kind":"row",not-json}\n', b'{"kind":"row","value":"\xff"}\n'],
    ids=["json", "utf8"],
)
def test_corrupt_json_or_utf8_trace_is_unavailable(tmp_path, corrupt_line):
    sidecar = trace_path(tmp_path, 13)
    sidecar.parent.mkdir(parents=True)
    with gzip.open(sidecar, "wb") as stream:
        stream.write(b'{"kind":"meta","trace_id":"trace"}\n')
        stream.write(corrupt_line)
    assert read_trace(tmp_path, 13) is None
    assert read_trace_row(tmp_path, 13, 1) is None


def test_trace_unknown_run_404s(tmp_path):
    client = _client(_StubAdapter({"beat": "a"}), tmp_path)
    pid, _ = _seed(client)
    assert client.get(f"/api/projects/{pid}/actions/runs/9999/trace").status_code == 404


def test_router_trace_collects_cache_and_attempt_events(tmp_path):
    # the router seam itself: cache miss → attempt ok, then cache hit
    import asyncio

    from frisket.ai.llm.cache import ResponseCache

    router = ModelRouter(
        keys={"anthropic": "k"},
        cache=ResponseCache(tmp_path / "c.db"),
        cache_mode="replay",
    )
    router._adapters["anthropic"] = _StubAdapter({"x": 1})  # noqa: SLF001
    req = LLMRequest(model="anthropic/m", messages=[{"role": "user", "content": "hi"}])

    async def run() -> tuple[list[dict], list[dict]]:
        miss_events: list[dict] = []
        await router.complete(req, trace=miss_events)
        hit_events: list[dict] = []
        await router.complete(req, trace=hit_events)
        await router.aclose()
        return miss_events, hit_events

    miss_events, hit_events = asyncio.run(run())
    kinds = [(e["event"], e.get("hit"), e.get("outcome")) for e in miss_events]
    assert ("cache", False, None) in kinds
    assert ("attempt", None, "ok") in kinds
    assert hit_events[0]["event"] == "cache" and hit_events[0]["hit"] is True
