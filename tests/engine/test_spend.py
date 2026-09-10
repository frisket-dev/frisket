"""``GET /api/spend`` aggregates ``runs.cost_actual`` by project, model, and
month across the workspace, plus a
`has_unknown_costs` flag for spend that summed runs billed cost=None
(unpriced model — frisket.llm.pricing never estimates an unknown price).

There is no projection: the endpoint shows what you spent."""

import csv
import io
import re
import time

import pytest

from frisket.ai.llm import LLMRequest, LLMResponse, request_key
from frisket.engine.jobs import Worker
from http_test_helpers import post_v1_action_with_exact_confirmation
from typed_model_fixtures import model_plan, model_request

# identical corpus to tests/engine/test_server.py; the classify run replays
# from the strict cache seeded below at the typed prompt (keyless,
# deterministic)
CSV = """snippet
"The mayor's office quietly awarded a $4M paving contract to his brother-in-law's firm without competitive bidding."
"Researchers announced the city's new light rail line carried two million riders in its first quarter, beating projections."
"""

CLASSIFY_SPEC = {
    "action_kind": "map.classify",
    "engine": "llm",
    "model": "gemini/gemini-2.5-flash",
    "input_columns": ["snippet"],
    "context": "Each row is a one-sentence local news story summary.",
    "fields": [
        {
            "name": "beat",
            "type": "category",
            "labels": ["corruption", "transit", "public_health", "education", "other"],
            "description": "Which news beat does this story belong to?",
        }
    ],
    "include_justification": True,
}


@pytest.fixture
def client(replay_client):
    return replay_client


def _seed_classify_cache(client, pid: str, spec: dict) -> None:
    """Seed only these two known rows against the actual typed prompt/schema."""
    ws = client.app.state.workspace
    router = ws.router_for(ws.get(pid))
    assert router.cache_mode == "replay_strict"
    assert router.cache is not None
    plan = model_plan(spec)
    for row, label in zip(csv.DictReader(io.StringIO(CSV)), ("corruption", "transit")):
        call = plan.program.render(row, plan.spec_dict())
        req = LLMRequest(
            model=spec["model"],
            messages=call.messages,
            schema=call.schema,
            max_tokens=call.max_tokens,
        )
        router.cache.put(
            request_key(req, plan.program.version),
            LLMResponse(
                content=None,
                data={"beat": label, "beat_justification": f"Story concerns {label}."},
                tokens_in=50,
                tokens_out=10,
                cost=0.0001,
                model=spec["model"],
            ),
        )


def post_typed_classify_run(client, pid: str, spec: dict):
    """Run the canonical classify spec as a typed request through the real
    quote-then-exact-echo protocol."""
    _seed_classify_cache(client, pid, spec)
    action = model_request(spec).model_dump(mode="json", exclude_none=True)
    return post_v1_action_with_exact_confirmation(client, pid, action)


def make_project_with_data(client, name="Spend Test") -> tuple[str, int]:
    pid = client.post("/api/projects", json={"name": name}).json()["id"]
    r = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("stories.csv", CSV, "text/csv")},
    )
    assert r.status_code == 200, r.text
    return pid, r.json()["sheet_id"]


def drain_queue(client) -> None:
    ws = client.app.state.workspace
    worker = Worker(ws.queue, ws.registry, worker_id="spend-test-worker")
    while worker.run_once():
        pass


def seed_run(
    client,
    pid: str,
    sheet_id: int,
    *,
    model: str | None,
    cost: float,
    completed_rows: int,
    with_tokens: bool = True,
) -> int:
    """Insert a finished run directly (the seeded-run pattern from
    test_server's isolation test) so spend math is deterministic."""
    p = client.app.state.workspace.get(pid)
    p.db.execute("INSERT INTO ops (kind, label) VALUES ('run', 'seed')")
    op_id = p.db.execute("SELECT MAX(id) AS m FROM ops").fetchone()["m"]
    cur = p.db.execute(
        "INSERT INTO runs (op_id, sheet_id, action_kind, model, status, total_rows, "
        "completed_rows, cost_actual) VALUES (?, ?, 'map.classify', ?, 'completed', "
        "?, ?, ?)",
        (op_id, sheet_id, model, completed_rows, completed_rows, cost),
    )
    run_id = cur.lastrowid
    if with_tokens:
        p.db.execute(
            "INSERT INTO results (run_id, row_id, column_id, value, tokens_in, "
            "tokens_out) VALUES (?, 1, 1, '\"x\"', 100, 50)",
            (run_id,),
        )
    p.db.commit()
    return run_id


class TestSpendAggregation:
    def test_aggregates_by_project_model_month(self, client):
        pid_a, sheet_a = make_project_with_data(client, "Project A")
        r = post_typed_classify_run(
            client, pid_a, {**CLASSIFY_SPEC, "sheet_id": sheet_a}
        )
        assert r.status_code == 200, r.text
        drain_queue(client)
        pid_b, sheet_b = make_project_with_data(client, "Project B")
        seed_run(
            client,
            pid_b,
            sheet_b,
            model="anthropic/claude-haiku-4-5",
            cost=0.10,
            completed_rows=10,
        )

        body = client.get("/api/spend").json()
        month = time.strftime("%Y-%m", time.gmtime())
        assert all(re.fullmatch(r"\d{4}-\d{2}", row["month"]) for row in body["rows"])
        # project B's seeded run aggregates under (project, model, month)
        row_b = next(r for r in body["rows"] if r["project"] == pid_b)
        assert row_b["model"] == "anthropic/claude-haiku-4-5"
        assert row_b["month"] == month
        assert row_b["runs"] == 1
        assert row_b["rows"] == 10
        assert row_b["cost"] == pytest.approx(0.10)
        # project A's real (cache-replayed) run is present too
        row_a = next(r for r in body["rows"] if r["project"] == pid_a)
        assert row_a["model"] == "gemini/gemini-2.5-flash"
        assert row_a["rows"] == 2
        # the total is the sum of the aggregation rows
        assert body["total_cost"] == pytest.approx(sum(r["cost"] for r in body["rows"]))

    def test_empty_workspace(self, client):
        body = client.get("/api/spend").json()
        assert body["rows"] == []
        assert body["total_cost"] == 0.0
        assert body["has_unknown_costs"] is False
        # The dashboard shows what was spent and nothing else.
        assert "forecast" not in body


class TestUnknownCosts:
    def test_priced_models_do_not_flag(self, client):
        pid, sheet_id = make_project_with_data(client)
        # zero-cost run on a PRICED model with tokens recorded: cache replays
        # and local recipes legitimately cost 0, so "tokens but cost 0" must
        # NOT trip the flag — only an unpriced model does
        seed_run(
            client,
            pid,
            sheet_id,
            model="anthropic/claude-haiku-4-5",
            cost=0.0,
            completed_rows=1,
        )
        body = client.get("/api/spend").json()
        assert body["has_unknown_costs"] is False
        assert body["unknown_cost_models"] == []

    def test_unpriced_model_with_usage_flags(self, client):
        pid, sheet_id = make_project_with_data(client)
        seed_run(
            client,
            pid,
            sheet_id,
            model="acme/unpriced-9000",
            cost=0.0,  # its calls billed cost=None -> summed as 0
            completed_rows=1,
        )
        body = client.get("/api/spend").json()
        assert body["has_unknown_costs"] is True
        assert body["unknown_cost_models"] == ["acme/unpriced-9000"]

    def test_unpriced_model_without_token_usage_does_not_flag(self, client):
        pid, sheet_id = make_project_with_data(client)
        # a run that never recorded token usage did no billable LLM work, so
        # there is no unknown spend to flag
        seed_run(
            client,
            pid,
            sheet_id,
            model="acme/unpriced-9000",
            cost=0.0,
            completed_rows=0,
            with_tokens=False,
        )
        body = client.get("/api/spend").json()
        assert body["has_unknown_costs"] is False

    def test_unpriceable_provider_call_flags_even_with_no_run_model(self, client):
        """The 2026-07-26 keyed proof, as a test.

        A routed vision-OCR call billed a real BYOK key, recorded 257/35
        tokens and NO cost, and its own attempt receipt said `cost_basis:
        unpriceable, unmetered_calls: 1` — while this dashboard answered
        `has_unknown_costs: false`, because `runs.model` is NULL on a routed
        capability run and that column was the only thing it looked at. Two
        computations of one fact; the flag is now derived from the same
        `model_calls` facts the receipt's counter is.
        """
        pid, sheet_id = make_project_with_data(client)
        run_id = seed_run(client, pid, sheet_id, model=None, cost=0.0, completed_rows=1)
        p = client.app.state.workspace.get(pid)
        p.db.execute(
            "INSERT INTO model_calls (id, fact_version, run_id, row_id, column_id, "
            "capability, engine, provider, provider_kind, model_ids, "
            "credential_source, provider_cost_usd, cost_source, units, cache) "
            "VALUES ('ocr-fact-1', 'frisket.model-call-fact.v1', ?, 1, 1, 'ocr', "
            "'openai/gpt-4.1-mini', 'openai', 'platform_api', '[]', 'project_key', "
            'NULL, \'unknown\', \'{"pages": 1, "tokens_in": 257, '
            "\"tokens_out\": 35}', '{}')",
            (run_id,),
        )
        p.db.commit()

        body = client.get("/api/spend").json()
        assert body["has_unknown_costs"] is True
        assert "openai/gpt-4.1-mini" in body["unknown_cost_models"]

    def test_free_local_and_cached_calls_do_not_flag(self, client):
        """`ModelCallMeta.local`/`.sidecar` write an explicit 0.0, never NULL,
        so genuinely free work stays unflagged; a cache hit costs the provider
        nothing even when it carries no price."""
        pid, sheet_id = make_project_with_data(client)
        run_id = seed_run(client, pid, sheet_id, model=None, cost=0.0, completed_rows=1)
        p = client.app.state.workspace.get(pid)
        p.db.execute(
            "INSERT INTO model_calls (id, fact_version, run_id, row_id, column_id, "
            "capability, engine, provider, provider_kind, model_ids, "
            "credential_source, provider_cost_usd, cost_source, units, cache) "
            "VALUES ('local-fact-1', 'frisket.model-call-fact.v1', ?, 1, 1, 'ocr', "
            "'rapidocr', 'local', 'local_process', '[]', 'local', 0.0, "
            "'free_local', '{\"pages\": 1}', '{}')",
            (run_id,),
        )
        p.db.execute(
            "INSERT INTO model_calls (id, fact_version, run_id, row_id, column_id, "
            "capability, engine, provider, provider_kind, model_ids, "
            "credential_source, provider_cost_usd, cost_source, units, cache) "
            "VALUES ('cached-fact-1', 'frisket.model-call-fact.v1', ?, 1, 1, "
            "'llm.complete', 'openai/gpt-4.1-mini', 'openai', 'platform_api', '[]', "
            "'cache', NULL, 'unknown', '{\"tokens_in\": 10}', '{}')",
            (run_id,),
        )
        p.db.commit()

        body = client.get("/api/spend").json()
        assert body["has_unknown_costs"] is False
        assert body["unknown_cost_models"] == []
