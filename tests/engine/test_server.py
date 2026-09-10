"""API server tests: the contract the frontend consumes. Model runs replay
exact typed requests from a disposable cache — keyless, deterministic."""

import csv
import gzip
import io
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter, request_key
from frisket.server.app import create_app
from frisket.engine.jobs import RUN_PROJECT_KIND, Worker
from frisket.operability.trace import trace_path
from http_test_helpers import (
    post_canonical_run_spec_as_v1_action as _post_canonical_run_spec,
    post_operation_redo_as_v1_action,
    post_operation_undo_as_v1_action,
    post_row_add_as_v1_action,
    post_v1_action_with_exact_confirmation,
    queued_python_run_spec,
)
from typed_model_fixtures import model_plan, model_request

CSV = """snippet
"The mayor's office quietly awarded a $4M paving contract to his brother-in-law's firm without competitive bidding."
"Researchers announced the city's new light rail line carried two million riders in its first quarter, beating projections."
"""


@pytest.fixture
def client(replay_client):
    return replay_client


def post_canonical_run_spec_as_v1_action(client, pid, spec, *, confirmed=True):
    if spec["action_kind"] != "map.classify":
        return _post_canonical_run_spec(client, pid, spec, confirmed=confirmed)
    action = model_request(spec).model_dump(mode="json", exclude_none=True)
    if confirmed:
        return post_v1_action_with_exact_confirmation(client, pid, action)
    return client.post(f"/api/projects/{pid}/actions/v1/run", json=action)


def _seed_classify_cache(client, pid, spec):
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


def make_project_with_data(client) -> tuple[str, int]:
    pid = client.post("/api/projects", json={"name": "Test Project"}).json()["id"]
    resp = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("stories.csv", CSV, "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    return pid, resp.json()["sheet_id"]


def drain_queue(client) -> None:
    ws = client.app.state.workspace
    worker = Worker(ws.queue, ws.registry, worker_id="test-worker", poll_interval=0.01)
    while worker.run_once():
        pass


def action_run_public_status(client, pid: str, run_id: int) -> dict:
    response = client.get(f"/api/projects/{pid}/actions/runs/{run_id}/status")
    assert response.status_code == 200, response.text
    return response.json()["run"]["public_status"]


class TestProjects:
    def test_create_and_list(self, client):
        out = client.post("/api/projects", json={"name": "Tow Trucks"}).json()
        assert out["id"] == "tow-trucks"
        assert any(p["id"] == "tow-trucks" for p in client.get("/api/projects").json())

    def test_import_csv_sniffs_types(self, client):
        pid = client.post("/api/projects", json={"name": "x"}).json()["id"]
        csv_data = "name,age,site,photo\nAda,36,https://x.com,https://i.co/a.jpg\n"
        r = client.post(
            f"/api/projects/{pid}/import/csv",
            files={"file": ("p.csv", csv_data, "text/csv")},
        )
        sheet_id = r.json()["sheet_id"]
        data = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()
        types = {c["name"]: c["type"] for c in data["columns"]}
        assert types == {
            "name": "text",
            "age": "integer",
            "site": "link",
            "photo": "image",
        }

    def test_import_csv_detects_semicolon_and_european_numbers(self, client):
        pid = client.post("/api/projects", json={"name": "eu csv"}).json()["id"]
        csv_data = (
            "name;amount;population;note\n"
            'Ada;12,50;1.234;"commas, and semicolons; stay quoted"\n'
            'Grace;7,25;2.345;"another, note; here"\n'
        )
        response = client.post(
            f"/api/projects/{pid}/import/csv",
            files={"file": ("people.csv", csv_data, "text/csv")},
        )

        assert response.status_code == 200, response.text
        data = client.get(
            f"/api/projects/{pid}/sheets/{response.json()['sheet_id']}/data"
        ).json()
        columns = {column["name"]: column for column in data["columns"]}
        assert {name: column["type"] for name, column in columns.items()} == {
            "name": "text",
            "amount": "number",
            "population": "integer",
            "note": "text",
        }
        assert data["rows"][0]["cells"][str(columns["amount"]["id"])] == 12.5
        assert data["rows"][0]["cells"][str(columns["population"]["id"])] == 1234
        assert data["rows"][0]["cells"][str(columns["note"]["id"])] == (
            "commas, and semicolons; stay quoted"
        )

    def test_import_csv_detects_tab_delimiter(self, client):
        pid = client.post("/api/projects", json={"name": "tsv csv"}).json()["id"]
        response = client.post(
            f"/api/projects/{pid}/import/csv",
            files={
                "file": (
                    "people.csv",
                    "name\tage\nAda\t36\nGrace\t40\n",
                    "text/csv",
                )
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["columns"] == ["name", "age"]

    def test_sheet_data_paging(self, client):
        pid, sheet_id = make_project_with_data(client)
        data = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data?limit=1").json()
        assert data["total"] == 2 and len(data["rows"]) == 1

    def test_sheet_data_filters_sorts_before_paging(self, client):
        pid = client.post("/api/projects", json={"name": "Grid Data"}).json()["id"]
        resp = client.post(
            f"/api/projects/{pid}/import/csv",
            files={
                "file": (
                    "rows.csv",
                    "city,status\n"
                    "Syracuse,open\n"
                    "Albany,open\n"
                    "Buffalo,closed\n"
                    "Rochester,open\n",
                    "text/csv",
                )
            },
        )
        assert resp.status_code == 200, resp.text
        sheet_id = resp.json()["sheet_id"]

        filtered = client.get(
            f"/api/projects/{pid}/sheets/{sheet_id}/data",
            params={
                "filter": json.dumps({"status": {"eq": "open"}}),
                "sort": json.dumps([{"column": "city", "dir": "asc"}]),
                "limit": 1,
                "offset": 1,
            },
        ).json()
        cols = {c["name"]: c for c in filtered["columns"]}
        assert filtered["total"] == 3
        assert len(filtered["rows"]) == 1
        assert filtered["rows"][0]["cells"][str(cols["city"]["id"])] == "Rochester"
        assert filtered["rows"][0]["cells"][str(cols["status"]["id"])] == "open"

        descending = client.get(
            f"/api/projects/{pid}/sheets/{sheet_id}/data",
            params={"sort": json.dumps([{"column": "city", "dir": "desc"}])},
        ).json()
        city_id = str(cols["city"]["id"])
        assert [row["cells"][city_id] for row in descending["rows"]] == [
            "Syracuse",
            "Rochester",
            "Buffalo",
            "Albany",
        ]

        contains = client.get(
            f"/api/projects/{pid}/sheets/{sheet_id}/data",
            params={"filter": json.dumps({"city": {"contains": "och"}})},
        ).json()
        assert contains["total"] == 1
        assert contains["rows"][0]["cells"][city_id] == "Rochester"

        not_closed = client.get(
            f"/api/projects/{pid}/sheets/{sheet_id}/data",
            params={"filter": json.dumps({"status": {"neq": "closed"}})},
        ).json()
        assert not_closed["total"] == 3

    def test_sheet_data_filter_preserves_parent_row_scope(self, client):
        pid = client.post("/api/projects", json={"name": "Child Filter"}).json()["id"]
        p = client.app.state.workspace.get(pid)
        parent_sheet = p.add_sheet("parents")
        parent_cols = {"name": p.add_column(parent_sheet, "name")}
        parent_rows = p.add_rows(
            parent_sheet,
            [{"name": "parent-a"}, {"name": "parent-b"}],
            parent_cols,
        )
        child_sheet = p.add_sheet("children", parent_sheet_id=parent_sheet)
        child_cols = {
            "city": p.add_column(child_sheet, "city"),
            "status": p.add_column(child_sheet, "status"),
        }
        p.add_rows(
            child_sheet,
            [
                {"city": "Albany", "status": "open"},
                {"city": "Buffalo", "status": "closed"},
                {"city": "Syracuse", "status": "open"},
            ],
            child_cols,
            parent_row_ids=[parent_rows[0], parent_rows[0], parent_rows[1]],
        )

        data = client.get(
            f"/api/projects/{pid}/sheets/{child_sheet}/data",
            params={
                "parent_row_id": parent_rows[0],
                "filter": json.dumps({"status": {"eq": "open"}}),
                "sort": json.dumps([{"column": "city", "dir": "asc"}]),
            },
        ).json()
        cols = {c["name"]: c for c in data["columns"]}
        assert data["total"] == 1
        assert [row["parent_row_id"] for row in data["rows"]] == [parent_rows[0]]
        assert [row["cells"][str(cols["city"]["id"])] for row in data["rows"]] == [
            "Albany"
        ]

    def test_sheet_data_sorts_number_columns_numerically(self, client):
        pid = client.post("/api/projects", json={"name": "Number Sort"}).json()["id"]
        p = client.app.state.workspace.get(pid)
        sheet_id = p.add_sheet("scores")
        cols = {"score": p.add_column(sheet_id, "score", type="number")}
        p.add_rows(
            sheet_id,
            [{"score": 10}, {"score": 2}, {"score": 1}],
            cols,
        )

        data = client.get(
            f"/api/projects/{pid}/sheets/{sheet_id}/data",
            params={"sort": json.dumps([{"column": "score", "dir": "asc"}])},
        ).json()
        score_id = str(cols["score"])
        assert [row["cells"][score_id] for row in data["rows"]] == [1, 2, 10]

    def test_sheet_data_contains_filter_escapes_like_wildcards(self, client):
        pid = client.post("/api/projects", json={"name": "Literal Contains"}).json()[
            "id"
        ]
        resp = client.post(
            f"/api/projects/{pid}/import/csv",
            files={
                "file": (
                    "rows.csv",
                    "label\n"
                    '"100% literal"\n'
                    '"100x wildcard-looking"\n'
                    '"code_A"\n'
                    '"codeXA"\n',
                    "text/csv",
                )
            },
        )
        assert resp.status_code == 200, resp.text
        sheet_id = resp.json()["sheet_id"]

        percent = client.get(
            f"/api/projects/{pid}/sheets/{sheet_id}/data",
            params={"filter": json.dumps({"label": {"contains": "%"}})},
        ).json()
        cols = {c["name"]: c for c in percent["columns"]}
        label_id = str(cols["label"]["id"])
        assert percent["total"] == 1
        assert [row["cells"][label_id] for row in percent["rows"]] == ["100% literal"]

        underscore = client.get(
            f"/api/projects/{pid}/sheets/{sheet_id}/data",
            params={"filter": json.dumps({"label": {"contains": "_"}})},
        ).json()
        assert underscore["total"] == 1
        assert [row["cells"][label_id] for row in underscore["rows"]] == ["code_A"]

    def test_sheet_data_boolean_filter_matches_text_input_values(self, client):
        pid = client.post("/api/projects", json={"name": "Boolean Filter"}).json()["id"]
        p = client.app.state.workspace.get(pid)
        sheet_id = p.add_sheet("flags")
        cols = {"flag": p.add_column(sheet_id, "flag", type="boolean")}
        p.add_rows(sheet_id, [{"flag": True}, {"flag": False}], cols)

        data = client.get(
            f"/api/projects/{pid}/sheets/{sheet_id}/data",
            params={"filter": json.dumps({"flag": {"eq": "true"}})},
        ).json()
        flag_id = str(cols["flag"])
        assert data["total"] == 1
        assert [row["cells"][flag_id] for row in data["rows"]] == [True]

        bad = client.get(
            f"/api/projects/{pid}/sheets/{sheet_id}/data",
            params={"filter": json.dumps({"flag": {"contains": "true"}})},
        )
        assert bad.status_code == 400

    def test_saved_recipe_concurrent_writes_keep_all_entries(self, client):
        ws = client.app.state.workspace

        def save(i: int) -> dict:
            return ws.save_recipe(
                f"recipe {i}", {"action_kind": "test.concurrent", "i": i}
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            entries = list(pool.map(save, range(20)))

        saved = ws.saved_recipes()
        assert len(saved) == 20
        assert sorted(r["id"] for r in saved) == list(range(1, 21))
        assert sorted(r["spec"]["i"] for r in saved) == list(range(20))
        assert sorted(r["id"] for r in entries) == list(range(1, 21))

    def test_column_stats_summarizes_types_and_requires_large_manual_analyze(
        self, client, monkeypatch
    ):
        pid = client.post("/api/projects", json={"name": "Column Stats"}).json()["id"]
        project = client.app.state.workspace.get(pid)
        sheet_id = project.add_sheet("stats")
        cols = {
            "name": project.add_column(sheet_id, "name", type="text"),
            "score": project.add_column(sheet_id, "score", type="number"),
            "bytes": project.add_column(
                sheet_id, "bytes", type="integer", format="filesize"
            ),
            "status": project.add_column(sheet_id, "status", type="category"),
            "seen_at": project.add_column(sheet_id, "seen_at", type="date"),
            "doc": project.add_column(sheet_id, "doc", type="file"),
        }
        first_blob = project.add_blob(b"small", filename="small.txt", mime="text/plain")
        second_blob = project.add_blob(
            b"larger file", filename="large.txt", mime="text/plain"
        )
        project.add_rows(
            sheet_id,
            [
                {
                    "name": "Ada",
                    "score": 10,
                    "bytes": 7906,
                    "status": "open",
                    "seen_at": "2026-01-01",
                    "doc": {
                        "blob": first_blob,
                        "filename": "small.txt",
                        "mime": "text/plain",
                    },
                },
                {
                    "name": "Grace Hopper",
                    "score": 30,
                    "bytes": 137689,
                    "status": "open",
                    "seen_at": "2026-01-03",
                    "doc": {
                        "blob": second_blob,
                        "filename": "large.txt",
                        "mime": "text/plain",
                    },
                },
                {
                    "name": "",
                    "score": 20,
                    "bytes": 27281,
                    "status": "closed",
                    "seen_at": "2026-01-02",
                    "doc": None,
                },
            ],
            cols,
        )

        score_stats = client.get(
            f"/api/projects/{pid}/sheets/{sheet_id}/columns/{cols['score']}/stats"
        )
        assert score_stats.status_code == 200, score_stats.text
        score_body = score_stats.json()
        assert score_body["schema_version"] == "frisket.column_stats.v1"
        assert score_body["computed"] is True
        assert score_body["row_count"] == 3
        assert score_body["missing"] == 0
        assert score_body["numeric"]["mean"] == 20
        assert score_body["numeric"]["median"] == 20
        assert score_body["numeric"]["min"] == 10
        assert score_body["numeric"]["max"] == 30
        assert score_body["numeric"]["histogram"]

        bytes_body = client.get(
            f"/api/projects/{pid}/sheets/{sheet_id}/columns/{cols['bytes']}/stats"
        ).json()
        assert bytes_body["column"]["format"] == "filesize"
        assert bytes_body["numeric"]["min"] == 7906
        assert bytes_body["numeric"]["max"] == 137689
        assert bytes_body["numeric"]["histogram"] == [
            {"min": 0, "max": 50000, "count": 2},
            {"min": 50000, "max": 100000, "count": 0},
            {"min": 100000, "max": 150000, "count": 1},
        ]

        name_body = client.get(
            f"/api/projects/{pid}/sheets/{sheet_id}/columns/{cols['name']}/stats"
        ).json()
        assert name_body["missing"] == 1
        assert name_body["text"]["shortest"] == "Ada"
        assert name_body["text"]["longest"] == "Grace Hopper"

        status_body = client.get(
            f"/api/projects/{pid}/sheets/{sheet_id}/columns/{cols['status']}/stats"
        ).json()
        assert status_body["top_values"][0] == {"value": "open", "count": 2}

        date_body = client.get(
            f"/api/projects/{pid}/sheets/{sheet_id}/columns/{cols['seen_at']}/stats"
        ).json()
        assert date_body["date"]["min"].startswith("2026-01-01")
        assert date_body["date"]["max"].startswith("2026-01-03")

        file_body = client.get(
            f"/api/projects/{pid}/sheets/{sheet_id}/columns/{cols['doc']}/stats"
        ).json()
        assert file_body["present"] == 2
        assert file_body["missing"] == 1
        assert file_body["top_values"] == []
        assert file_body["numeric"] is None
        assert file_body["text"] is None
        assert file_body["json_types"] == []
        assert file_body["file"] == {"count": 2, "min_size": 5, "max_size": 11}

        import frisket.server.services.sheet_grid as sheet_grid_service

        monkeypatch.setattr(sheet_grid_service, "COLUMN_STATS_AUTO_ROW_LIMIT", 2)
        gated_body = client.get(
            f"/api/projects/{pid}/sheets/{sheet_id}/columns/{cols['score']}/stats"
        ).json()
        assert gated_body["computed"] is False
        assert gated_body["requires_manual_analyze"] is True
        assert gated_body["threshold"] == 2

        forced_body = client.get(
            f"/api/projects/{pid}/sheets/{sheet_id}/columns/{cols['score']}/stats",
            params={"force": "true"},
        ).json()
        assert forced_body["computed"] is True
        assert forced_body["requires_manual_analyze"] is False


class TestRuns:
    def test_estimate_then_run_from_cache(self, client):
        pid, sheet_id = make_project_with_data(client)
        spec = {
            "action_kind": "map.classify",
            "engine": "llm",
            "model": "gemini/gemini-3.5-flash",
            "sheet_id": sheet_id,
            "input_columns": ["snippet"],
            "context": "Each row is a one-sentence local news story summary.",
            "fields": [
                {
                    "name": "beat",
                    "type": "category",
                    "labels": [
                        "corruption",
                        "transit",
                        "public_health",
                        "education",
                        "other",
                    ],
                    "description": "Which news beat does this story belong to?",
                }
            ],
            "include_justification": True,
        }
        _seed_classify_cache(client, pid, spec)
        estimate_action = model_request(spec).model_dump(mode="json", exclude_none=True)
        est = client.post(
            f"/api/projects/{pid}/actions/v1/estimate",
            json={"action": estimate_action},
        ).json()
        assert est["action"]["kind"] == "map.classify"
        assert est["estimate"]["cost"] < 0.05

        r = post_canonical_run_spec_as_v1_action(client, pid, spec)
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]

        queued = action_run_public_status(client, pid, run_id)
        assert queued["status"] == "queued"
        assert queued["completed"] == 0
        assert r.json()["status"] == "queued"

        drain_queue(client)
        status = action_run_public_status(client, pid, run_id)
        assert status["status"] == "completed"
        assert status["completed"] == 2 and status["failed"] == 0

        data = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()
        cols = {c["name"]: c for c in data["columns"]}
        assert cols["beat"]["ai_generated"] is True
        beats = [row["cells"][str(cols["beat"]["id"])] for row in data["rows"]]
        assert beats == ["corruption", "transit"]
        # provenance meta rides along
        meta = data["rows"][0]["meta"].get(str(cols["beat"]["id"]))
        assert meta and meta["justification"]

    def test_run_status_reconciles_terminal_queue_failure(self, client):
        pid, sheet_id = make_project_with_data(client)
        spec = queued_python_run_spec(sheet_id, "snippet", "copied_snippet")
        r = post_canonical_run_spec_as_v1_action(client, pid, spec)
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]

        ws = client.app.state.workspace
        job = ws.queue.claim("stale-worker")
        assert job is not None
        assert job.kind == RUN_PROJECT_KIND
        assert job.payload["project_id"] == pid
        assert job.payload["run_id"] == run_id
        assert ws.queue.fail(
            job.id,
            "stale-worker",
            "no handler registered for kind 'project.run'",
            retry=False,
        )

        status = action_run_public_status(client, pid, run_id)
        assert status["status"] == "failed"
        assert status["live"] is False
        assert "no handler registered" in status["error"]

        project = ws.get(pid)
        row = project.db.execute(
            "SELECT status FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        assert row["status"] == "failed"

    def test_cost_gate_returns_402(self, tmp_path):
        # replay_strict proves that no provider effect can happen, so it
        # correctly has no spend-confirmation gate. Exercise the live posture
        # whose provider effect the gate protects.
        router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
        client = TestClient(create_app(tmp_path / "cost-gate", router=router))
        pid, sheet_id = make_project_with_data(client)
        # inflate the ESTIMATE well past the $1 gate via sheer input size —
        # not via price assumptions: the table updates from a live source
        # (scripts/dev/update_pricing.py) and a 3x opus price drop once silently
        # un-tripped this test at 200k chars
        spec = {
            "action_kind": "map.classify",
            "model": "anthropic/claude-opus-4-8",
            "sheet_id": sheet_id,
            "input_columns": ["snippet"],
            "context": "x" * 2_000_000,
            "fields": [{"name": "s", "type": "score"}],
        }
        r = post_canonical_run_spec_as_v1_action(client, pid, spec, confirmed=False)
        assert r.status_code == 402
        body = r.json()
        assert body["status"] == "needs_confirmation"
        assert body["errors"][0]["code"] == "model_cost_requires_confirmation"

    def test_cost_gate_unknown_model_requires_confirmation(self, tmp_path):
        """A model absent from pricing_data.json has an UNKNOWN estimate —
        the gate demands confirmation rather than guessing a price, however
        small the input."""
        router = ModelRouter(keys={"acme": "k"}, cache=None, cache_mode="off")
        client = TestClient(create_app(tmp_path / "unknown-cost-gate", router=router))
        pid, sheet_id = make_project_with_data(client)
        spec = {
            "action_kind": "map.classify",
            "model": "acme/unpriced-9000",
            "sheet_id": sheet_id,
            "input_columns": ["snippet"],
            "fields": [{"name": "s", "type": "score"}],
        }
        r = post_canonical_run_spec_as_v1_action(client, pid, spec, confirmed=False)
        assert r.status_code == 402
        body = r.json()
        assert body["status"] == "needs_confirmation"
        assert body["errors"][0]["code"] == "model_cost_requires_confirmation"

    def test_undo_redo_history(self, client):
        pid, sheet_id = make_project_with_data(client)
        hist = client.get(f"/api/projects/{pid}/history").json()["ops"]
        assert hist[-1]["kind"] == "import.csv" and hist[-1]["barrier"] is False
        op_id = hist[-1]["id"]

        r = post_operation_undo_as_v1_action(client, pid, expected_op_id=op_id)
        assert r.status_code == 200
        body = r.json()
        assert body["schema_version"] == "frisket.action_result.v1"
        assert body["status"] == "completed"
        assert body["op_ids"] == [op_id]
        r = post_operation_redo_as_v1_action(client, pid, expected_op_id=op_id)
        assert r.status_code == 200
        body = r.json()
        assert body["schema_version"] == "frisket.action_result.v1"
        assert body["status"] == "completed"
        assert body["op_ids"] == [op_id]

    def test_debug_dump_reflects_real_state(self, client):
        """The debug dump must mirror the project's
        actual internals — op log entries for the import + run, the run row
        with its true status/counts, a results summary, cache stats, and the
        column-lineage edge from the AI column back to its producing run."""
        pid, sheet_id = make_project_with_data(client)
        spec = {
            "action_kind": "map.classify",
            "engine": "llm",
            "model": "gemini/gemini-3.5-flash",
            "sheet_id": sheet_id,
            "input_columns": ["snippet"],
            "context": "Each row is a one-sentence local news story summary.",
            "fields": [
                {
                    "name": "beat",
                    "type": "category",
                    "labels": [
                        "corruption",
                        "transit",
                        "public_health",
                        "education",
                        "other",
                    ],
                    "description": "Which news beat does this story belong to?",
                }
            ],
            "include_justification": True,
        }
        _seed_classify_cache(client, pid, spec)
        run_id = post_canonical_run_spec_as_v1_action(client, pid, spec).json()[
            "run_id"
        ]
        drain_queue(client)

        body = client.get(f"/api/projects/{pid}/debug").json()
        # op log: import then the classify run, cursor on the latest op
        kinds = [o["kind"] for o in body["ops"]]
        assert "import.csv" in kinds
        assert body["ops"][-1]["at_cursor"] is True
        assert body["op_cursor"] == body["ops"][-1]["id"]
        # runs: the real run row with live counts
        run = next(r for r in body["runs"] if r["run_id"] == run_id)
        assert run["status"] == "completed"
        assert run["action_kind"] == "map.classify"
        assert run["action_kind"] == "map.classify"
        assert run["action_name"] == "Classify rows"
        assert run["completed_rows"] == 2 and run["failed_rows"] == 0
        # results summary: 2 rows x (beat + beat_justification), no errors
        summary = next(s for s in body["results"] if s["run_id"] == run_id)
        assert summary["cells"] == 4 and summary["errors"] == 0
        assert summary["columns"] == 2 and summary["tokens_in"] > 0
        # cache stats from the replay cache the run used
        assert body["cache"]["enabled"] is True
        assert body["cache"]["mode"] == "replay_strict"
        assert body["cache"]["entries"] > 0
        # lineage: the AI column points back at its producing run + inputs
        sheet = next(s for s in body["lineage"] if s["sheet_id"] == sheet_id)
        beat = next(c for c in sheet["columns"] if c["name"] == "beat")
        assert beat["ai_generated"] is True
        assert beat["derived_from"]["run_id"] == run_id
        assert beat["derived_from"]["action_kind"] == "map.classify"
        assert beat["derived_from"]["action_kind"] == "map.classify"
        assert beat["derived_from"]["action_name"] == "Classify rows"
        assert beat["derived_from"]["input_columns"] == ["snippet"]

    def test_debug_404_for_unknown_project(self, client):
        assert client.get("/api/projects/nope/debug").status_code == 404


class TestSavedViews:
    """Filter/sort operations and saved views. A saved view is a named filter/
    sort over a sheet, and saving one is a LOGGED op (provenance), not an
    ephemeral grid setting."""

    def test_create_view_is_logged_op(self, client):
        pid, sheet_id = make_project_with_data(client)
        spec = {
            "name": "high-confidence",
            "sheet_id": sheet_id,
            "filter": {"confidence": {"gte": 0.8}},
            "sort": [{"column": "snippet", "dir": "asc"}],
        }
        r = client.post(f"/api/projects/{pid}/views", json=spec)
        assert r.status_code == 200, r.text
        view = r.json()
        assert view["name"] == "high-confidence"
        assert view["sheet_id"] == sheet_id
        assert view["spec"]["filter"] == {"confidence": {"gte": 0.8}}
        assert view["spec"]["sort"] == [{"column": "snippet", "dir": "asc"}]
        # the provenance contract: saving a view appends a 'view' op to history
        hist = client.get(f"/api/projects/{pid}/history").json()["ops"]
        view_ops = [o for o in hist if o["kind"] == "view"]
        assert view_ops, "saving a view must be a logged op"
        assert view_ops[-1]["label"] == "view high-confidence"

    def test_list_and_get_view(self, client):
        pid, sheet_id = make_project_with_data(client)
        vid = client.post(
            f"/api/projects/{pid}/views",
            json={"name": "v1", "sheet_id": sheet_id, "filter": {}},
        ).json()["id"]
        listing = client.get(f"/api/projects/{pid}/views").json()
        assert any(v["id"] == vid for v in listing)
        scoped = client.get(
            f"/api/projects/{pid}/views", params={"sheet_id": sheet_id}
        ).json()
        assert any(v["id"] == vid for v in scoped)
        got = client.get(f"/api/projects/{pid}/views/{vid}")
        assert got.status_code == 200 and got.json()["name"] == "v1"
        missing = client.get(f"/api/projects/{pid}/views/999999")
        assert missing.status_code == 404

    def test_replace_view_definition_logs_another_op(self, client):
        pid, sheet_id = make_project_with_data(client)
        vid = client.post(
            f"/api/projects/{pid}/views",
            json={"name": "v", "sheet_id": sheet_id, "filter": {"a": 1}},
        ).json()["id"]
        before = len(
            [
                o
                for o in client.get(f"/api/projects/{pid}/history").json()["ops"]
                if o["kind"] == "view"
            ]
        )
        r = client.put(
            f"/api/projects/{pid}/views/{vid}/definition",
            json={
                "filter": {"a": 2},
                "sort": None,
                "columns": None,
                "column_groups": None,
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["spec"]["filter"] == {"a": 2}
        after = len(
            [
                o
                for o in client.get(f"/api/projects/{pid}/history").json()["ops"]
                if o["kind"] == "view"
            ]
        )
        assert after == before + 1, "re-saving a view must log a new op"

    def test_replace_view_definition_can_clear_spec_keys(self, client):
        pid, sheet_id = make_project_with_data(client)
        vid = client.post(
            f"/api/projects/{pid}/views",
            json={
                "name": "v",
                "sheet_id": sheet_id,
                "filter": {"status": {"eq": "open"}},
                "sort": [{"column": "snippet", "dir": "asc"}],
                "columns": ["snippet"],
            },
        ).json()["id"]

        r = client.put(
            f"/api/projects/{pid}/views/{vid}/definition",
            json={
                "filter": {},
                "sort": None,
                "columns": None,
                "column_groups": None,
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["spec"] == {"filter": {}}

    def test_backfill_resume_keeps_run_counters_within_total(self, client):
        pid, sheet_id = make_project_with_data(client)
        spec = queued_python_run_spec(sheet_id, "snippet", "display")
        run = post_canonical_run_spec_as_v1_action(client, pid, spec)
        assert run.status_code == 200, run.text
        drain_queue(client)
        initial_status = action_run_public_status(client, pid, run.json()["run_id"])
        assert initial_status["status"] == "completed", [
            job.error for job in client.app.state.workspace.queue.list_jobs()
        ]

        added = post_row_add_as_v1_action(
            client,
            pid,
            sheet_id,
            {"snippet": "late row"},
        )
        assert added.status_code == 200, added.text

        backfill = client.post(
            f"/api/projects/{pid}/actions/v1/run",
            json={
                "action_id": "run.backfill",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {"column": "display"},
                "idempotency_key": "saved-views-backfill@sha256:test",
            },
        )
        assert backfill.status_code == 200, backfill.text
        body = backfill.json()
        output = next(
            item for item in body["outputs"] if item["kind"] == "run_backfill"
        )
        assert output["ref"]["filled"] == 1

        status = action_run_public_status(client, pid, body["run_id"])
        # Backfill is a fresh successor scoped only to the missing row; it
        # does not reopen the sealed two-row generation or inflate counters
        # with inherited heads.
        assert status["completed"] == status["total"] == 1

    def test_delete_view(self, client):
        pid, sheet_id = make_project_with_data(client)
        vid = client.post(
            f"/api/projects/{pid}/views",
            json={"name": "v", "sheet_id": sheet_id, "filter": {}},
        ).json()["id"]
        r = client.delete(f"/api/projects/{pid}/views/{vid}")
        assert r.status_code == 200 and r.json()["deleted"] == vid
        assert client.get(f"/api/projects/{pid}/views/{vid}").status_code == 404


class TestCancelRun:
    """``POST /api/projects/{pid}/actions/runs/{id}/cancel`` stops a long run;
    the run lands in status=cancelled
    with its partial results kept."""

    SLOW_CSV = "snippet\n" + "\n".join(
        f'"story number {i} about city hall"' for i in range(32)
    )

    def _slow_client(self, tmp_path, delay=0.3):
        import json as _json

        from frisket.ai.llm import LLMResponse

        class SlowAdapter:
            async def complete(self, req, http):  # noqa: ANN001
                import asyncio

                await asyncio.sleep(delay)
                return LLMResponse(
                    content=_json.dumps({"beat": "other"}),
                    data={"beat": "other"},
                    tokens_in=10,
                    tokens_out=5,
                    cost=0.0,
                    model=req.model,
                )

        router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
        router._adapters["anthropic"] = SlowAdapter()  # noqa: SLF001
        return TestClient(create_app(tmp_path / "ws-slow", router=router))

    def _spec(self, sheet_id):
        return {
            "action_kind": "map.classify",
            "model": "anthropic/claude-haiku-4-5",
            "sheet_id": sheet_id,
            "input_columns": ["snippet"],
            "fields": [
                {"name": "beat", "type": "category", "labels": ["corruption", "other"]}
            ],
        }

    def test_cancel_live_run_e2e(self, tmp_path, monkeypatch):
        """A live queued run is cancellable end-to-end (HTTP launch -> job
        queue -> app-level Worker thread -> HTTP cancel).

        The single row worker parks inside its first recipe call only after
        the dispatch CAS.  Cancellation therefore records durable intent
        while that exact writer owns the run; releasing the row proves the
        worker observes the intent at the next row boundary without sleeps."""
        from threading import Event, Thread

        from frisket.engine.runner.map_runner import MapRunner
        from frisket.engine.executor.python_evaluator import AdmittedPythonEvaluator

        client = self._slow_client(tmp_path)
        pid = client.post("/api/projects", json={"name": "c"}).json()["id"]
        r = client.post(
            f"/api/projects/{pid}/import/csv",
            files={"file": ("s.csv", self.SLOW_CSV, "text/csv")},
        )
        sheet_id = r.json()["sheet_id"]
        ws = client.app.state.workspace
        worker = Worker(
            ws.queue,
            ws.registry,
            worker_id="cancel-worker",
            poll_interval=0.01,
            lease_seconds=30.0,
        )
        spec = queued_python_run_spec(sheet_id, "snippet", "num")
        launched = post_canonical_run_spec_as_v1_action(client, pid, spec)
        assert launched.status_code == 200, launched.text
        run = launched.json()
        assert run["status"] == "queued"
        rid = int(run["run_id"])
        receipt_id = str(run["receipt_id"])
        project = ws.get(pid)

        entered = Event()
        release = Event()
        executed_rows: list[dict[str, object]] = []
        observed_attempt_ids: list[str] = []
        gate_errors: list[BaseException] = []
        worker_results: list[bool] = []
        worker_errors: list[BaseException] = []
        original_evaluate = AdmittedPythonEvaluator.evaluate

        monkeypatch.setattr(
            MapRunner,
            "_row_worker_count",
            lambda _self, _recipe, _spec: 1,
        )

        async def gated_evaluate(
            self: AdmittedPythonEvaluator,
            *,
            code: str,
            row: dict[str, object],
        ) -> object:
            executed_rows.append(dict(row))
            if len(executed_rows) == 1:
                try:
                    live = project.db.execute(
                        "SELECT r.current_attempt_id, a.state "
                        "FROM runs r JOIN execution_attempts a "
                        "ON a.id=r.current_attempt_id "
                        "WHERE r.id=?",
                        (rid,),
                    ).fetchone()
                    assert live is not None
                    assert live["state"] == "dispatching"
                    observed_attempt_ids.append(str(live["current_attempt_id"]))
                except BaseException as exc:
                    gate_errors.append(exc)
                    entered.set()
                    raise
                entered.set()
                if not release.wait(timeout=10):
                    raise AssertionError("timed out waiting to release the live row")
            return await original_evaluate(self, code=code, row=row)

        monkeypatch.setattr(AdmittedPythonEvaluator, "evaluate", gated_evaluate)

        def run_worker() -> None:
            try:
                worker_results.append(worker.run_once())
            except BaseException as exc:
                worker_errors.append(exc)

        worker_thread = Thread(target=run_worker, name="cancel-live-run-e2e")
        worker_thread.start()
        try:
            assert entered.wait(timeout=10), "worker never entered its first row"
            assert gate_errors == []
            assert len(observed_attempt_ids) == 1
            attempt_id = observed_attempt_ids[0]

            pending = client.post(f"/api/projects/{pid}/actions/runs/{rid}/cancel")
            assert pending.status_code == 409, pending.text
            detail = pending.json()["detail"]
            assert detail["status"] == "cancel_pending"
            assert detail["run_id"] == rid
            assert detail["cancel_requested"] is True
            assert detail["queue_cancelled"] is False

            live_run = project.db.execute(
                "SELECT status, finished_at, current_attempt_id, "
                "cancel_requested_at FROM runs WHERE id=?",
                (rid,),
            ).fetchone()
            assert live_run is not None
            assert tuple(live_run)[:3] == ("running", None, attempt_id)
            assert live_run["cancel_requested_at"] is not None
            assert (
                project.db.execute(
                    "SELECT state FROM execution_attempts WHERE id=?",
                    (attempt_id,),
                ).fetchone()["state"]
                == "dispatching"
            )
            active_claims = project.db.execute(
                "SELECT status FROM output_column_claims WHERE run_id=? ORDER BY id",
                (rid,),
            ).fetchall()
            assert active_claims
            assert {row["status"] for row in active_claims} == {"active"}
        finally:
            release.set()
            worker_thread.join(timeout=15)

        assert not worker_thread.is_alive(), "worker did not converge after cancel"
        assert worker_errors == []
        assert worker_results == [True]
        assert len(executed_rows) == 1

        status = action_run_public_status(client, pid, rid)
        assert status["status"] == "cancelled", [
            job.error for job in ws.queue.list_jobs()
        ]
        assert status["completed"] < status["total"], "cancel did not stop queued rows"

        terminal_run = project.db.execute(
            "SELECT status, finished_at, completed_rows, total_rows, "
            "current_attempt_id, cancel_requested_at FROM runs WHERE id=?",
            (rid,),
        ).fetchone()
        terminal_receipt = project.db.execute(
            "SELECT status, body FROM receipts WHERE id=?",
            (receipt_id,),
        ).fetchone()
        terminal_attempt = project.db.execute(
            "SELECT state FROM execution_attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
        terminal_claims = project.db.execute(
            "SELECT status, released_at FROM output_column_claims "
            "WHERE run_id=? ORDER BY id",
            (rid,),
        ).fetchall()
        assert terminal_run is not None
        assert terminal_receipt is not None
        assert terminal_attempt is not None
        assert terminal_claims
        assert terminal_run["status"] == "cancelled"
        assert terminal_run["finished_at"] is not None
        assert int(terminal_run["completed_rows"]) < int(terminal_run["total_rows"])
        assert terminal_run["current_attempt_id"] is None
        assert terminal_run["cancel_requested_at"] is not None
        assert terminal_receipt["status"] == "cancelled"
        assert json.loads(terminal_receipt["body"])["status"] == "cancelled"
        assert terminal_attempt["state"] == "effected"
        assert {row["status"] for row in terminal_claims} == {"cancelled"}
        assert all(row["released_at"] is not None for row in terminal_claims)

        before_repeat = (
            tuple(terminal_run),
            tuple(terminal_receipt),
            tuple(terminal_attempt),
            [tuple(row) for row in terminal_claims],
        )
        repeated = client.post(f"/api/projects/{pid}/actions/runs/{rid}/cancel")
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["status"] == "cancelled"
        assert repeated.json()["run_id"] == rid
        after_repeat = (
            tuple(
                project.db.execute(
                    "SELECT status, finished_at, completed_rows, total_rows, "
                    "current_attempt_id, cancel_requested_at FROM runs WHERE id=?",
                    (rid,),
                ).fetchone()
            ),
            tuple(
                project.db.execute(
                    "SELECT status, body FROM receipts WHERE id=?",
                    (receipt_id,),
                ).fetchone()
            ),
            tuple(
                project.db.execute(
                    "SELECT state FROM execution_attempts WHERE id=?",
                    (attempt_id,),
                ).fetchone()
            ),
            [
                tuple(row)
                for row in project.db.execute(
                    "SELECT status, released_at FROM output_column_claims "
                    "WHERE run_id=? ORDER BY id",
                    (rid,),
                ).fetchall()
            ],
        )
        assert after_repeat == before_repeat

    def test_cancel_finished_run_409(self, client):
        pid, sheet_id = make_project_with_data(client)
        spec = {
            "action_kind": "map.classify",
            "model": "gemini/gemini-3.5-flash",
            "sheet_id": sheet_id,
            "input_columns": ["snippet"],
            "context": "Each row is a one-sentence local news story summary.",
            "fields": [
                {
                    "name": "beat",
                    "type": "category",
                    "labels": [
                        "corruption",
                        "transit",
                        "public_health",
                        "education",
                        "other",
                    ],
                    "description": "Which news beat does this story belong to?",
                }
            ],
            "include_justification": True,
        }
        _seed_classify_cache(client, pid, spec)
        rid = post_canonical_run_spec_as_v1_action(client, pid, spec).json()["run_id"]
        drain_queue(client)
        # tiny cached run completes through the queue — cancel has nothing to stop
        r = client.post(f"/api/projects/{pid}/actions/runs/{rid}/cancel")
        assert r.status_code == 409

    def test_cancel_unknown_run_404(self, client):
        pid, _ = make_project_with_data(client)
        r = client.post(f"/api/projects/{pid}/actions/runs/9999/cancel")
        assert r.status_code == 404

    def test_run_rows_inspector_groups_errors_retries_and_pages(self, client):
        pid, sheet_id = make_project_with_data(client)
        p = client.app.state.workspace.get(pid)
        rows = p.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (sheet_id,)
        ).fetchall()
        out_col = p.add_column(sheet_id, "beat", ai_generated=True)
        note_col = p.add_column(sheet_id, "beat_note", ai_generated=True)
        p.db.execute("INSERT INTO ops (kind, label) VALUES ('run', 'seed inspector')")
        op_id = p.db.execute("SELECT MAX(id) AS m FROM ops").fetchone()["m"]
        cur = p.db.execute(
            "INSERT INTO runs (op_id, sheet_id, action_kind, model, params, status, "
            "total_rows, completed_rows, failed_rows, cost_actual) "
            "VALUES (?, ?, 'map.classify', 'anthropic/test', ?, 'completed', 2, 2, 1, 0.01)",
            (
                op_id,
                sheet_id,
                json.dumps({"row_ids": [rows[0]["id"], rows[1]["id"]]}),
            ),
        )
        run_id = cur.lastrowid
        p.db.executemany(
            "INSERT INTO results (run_id, row_id, column_id, value, tokens_in, "
            "tokens_out, error, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    rows[0]["id"],
                    out_col,
                    json.dumps("transit"),
                    10,
                    4,
                    None,
                    "success",
                ),
                (
                    run_id,
                    rows[0]["id"],
                    note_col,
                    json.dumps("ok"),
                    3,
                    2,
                    None,
                    "success",
                ),
                (
                    run_id,
                    rows[1]["id"],
                    out_col,
                    None,
                    8,
                    0,
                    "provider 429: retry budget exhausted",
                    "model_error",
                ),
            ],
        )
        sidecar = trace_path(p.path, run_id)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(sidecar, "wt", encoding="utf-8") as stream:
            stream.write(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "kind": "meta",
                                "run_id": run_id,
                                "trace_id": "trace-1",
                                "action_kind": "map.classify",
                                "model": "anthropic/test",
                            }
                        ),
                        json.dumps(
                            {
                                "kind": "row",
                                "row_id": rows[1]["id"],
                                "trace_id": "trace-1",
                                "error": "provider 429",
                                "retries": [
                                    {
                                        "event": "attempt",
                                        "status": 429,
                                        "retryable": True,
                                        "error": "rate limited",
                                    }
                                ],
                            }
                        ),
                    ]
                )
                + "\n"
            )
        p.db.commit()

        body = client.get(f"/api/projects/{pid}/actions/runs/{run_id}/rows").json()

        assert "run_id" not in body and "status" not in body
        assert body["run"]["id"] == run_id
        assert body["total"] == 2
        assert [r["status"] for r in body["rows"]] == ["complete", "error"]
        assert body["rows"][0]["tokens_in"] == 13
        assert body["rows"][0]["cells"][0]["value"] == "transit"
        assert body["rows"][1]["error"] == "provider 429: retry budget exhausted"
        assert body["rows"][1]["retry_count"] == 1
        assert body["rows"][1]["retries"][0]["status"] == 429

        page = client.get(
            f"/api/projects/{pid}/actions/runs/{run_id}/rows?offset=1&limit=1"
        ).json()
        assert page["offset"] == 1
        assert len(page["rows"]) == 1
        assert page["rows"][0]["row_id"] == rows[1]["id"]

        error_page = client.get(
            f"/api/projects/{pid}/actions/runs/{run_id}/rows?status=error"
        ).json()
        assert error_page["total"] == 1
        assert len(error_page["rows"]) == 1
        assert error_page["rows"][0]["row_id"] == rows[1]["id"]

        invalid = client.get(
            f"/api/projects/{pid}/actions/runs/{run_id}/rows?status=pending"
        )
        assert invalid.status_code == 400

    def test_run_rows_unknown_run_404s(self, client):
        pid, _ = make_project_with_data(client)
        r = client.get(f"/api/projects/{pid}/actions/runs/9999/rows")
        assert r.status_code == 404


class TestCrossProjectRunIsolation:
    """Run IDs are per-project autoincrements, so a
    workspace-global active_runs dict keyed by bare run_id collides across
    projects — POST /projects/A/runs/1/cancel could cancel project B's live
    run 1. active_runs is keyed (pid, run_id) now; prove cancel can't cross."""

    def test_cancel_does_not_cross_projects(self, client, tmp_path):
        from frisket.engine.runner import RunProgress

        pid_a, _ = make_project_with_data(client)
        pid_b = client.post("/api/projects", json={"name": "Other"}).json()["id"]
        ws = client.app.state.workspace
        # project B has a live run with id 1 (every project's first run is 1)
        prog_b = RunProgress(run_id=1, total=5)
        ws.active_runs[(pid_b, 1)] = prog_b
        # project A's db has a 'running' run with the SAME id
        p_a = ws.get(pid_a)
        p_a.db.execute("INSERT INTO ops (kind, label) VALUES ('run', 'x')")
        op_id = p_a.db.execute("SELECT MAX(id) AS m FROM ops").fetchone()["m"]
        p_a.db.execute(
            "INSERT INTO runs (id, op_id, sheet_id, action_kind, status, total_rows)"
            " VALUES (1, ?, 1, 'map.classify', 'running', 5)",
            (op_id,),
        )
        p_a.db.commit()
        r = client.post(f"/api/projects/{pid_a}/actions/runs/1/cancel")
        assert r.status_code in (200, 409)
        assert prog_b.cancelled is False, (
            "cancelling project A's run 1 flipped project B's run 1"
        )

    def test_run_rows_does_not_cross_projects(self, client):
        pid_a, _ = make_project_with_data(client)
        pid_b = client.post("/api/projects", json={"name": "Other"}).json()["id"]
        p_b = client.app.state.workspace.get(pid_b)
        sheet_b = p_b.add_sheet("data")
        p_b.db.execute("INSERT INTO ops (kind, label) VALUES ('run', 'b')")
        op_id = p_b.db.execute("SELECT MAX(id) AS m FROM ops").fetchone()["m"]
        p_b.db.execute(
            "INSERT INTO runs (id, op_id, sheet_id, action_kind, status, total_rows) "
            "VALUES (1, ?, ?, 'map.classify', 'completed', 0)",
            (op_id, sheet_b),
        )
        p_b.db.commit()

        r = client.get(f"/api/projects/{pid_a}/actions/runs/1/rows")

        assert r.status_code == 404
