"""One complete HTTP → agent → SQL → citation → grid flow, with a fake provider."""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.ai.llm.types import LLMResponse
from frisket.server.app import create_app
from frisket.server.workspace import Workspace


def test_ask_calculation_opens_exact_underlying_records(tmp_path, monkeypatch):
    class Provider:
        calls = 0

        async def complete(self, request, client):
            self.calls += 1
            observations = [
                json.loads(m["content"].removeprefix("Observation:\n"))
                for m in request.messages
                if isinstance(m.get("content"), str)
                and m["content"].startswith("Observation:\n")
            ]
            if self.calls == 1:
                name, args = (
                    "analytics",
                    {
                        "request": {
                            "sheet_id": 1,
                            "filter": {"status": {"eq": "awarded"}},
                            "groups": [{"column_id": 1}],
                            "metrics": [
                                {"id": "total", "kind": "sum", "column_id": 3},
                                {"id": "middle", "kind": "median", "column_id": 3},
                            ],
                            "sort": [
                                {
                                    "kind": "metric",
                                    "metric_id": "total",
                                    "direction": "desc",
                                }
                            ],
                        }
                    },
                )
            elif self.calls == 2:
                [group] = observations[-1]["groups"]
                assert group["metrics"] == {"total": 4, "middle": 2.0}
                assert group["row_count"] == 2
                name, args = "open_source", {"citation_id": group["citation_id"]}
            else:
                assert observations[-1]["row_ids"] == [1, 2]
                name = next(
                    t["name"]
                    for t in request.tools
                    if {"text", "citation_ids"}
                    <= set(t["parameters"].get("properties", {}))
                )
                args = {
                    "text": "Awarded contracts total 4; their median is 2.",
                    "citation_ids": [observations[-1]["citation_id"]],
                }
            return LLMResponse(
                content=None,
                data=None,
                tokens_in=10,
                tokens_out=5,
                cost=0.01,
                model=request.model,
                tool_calls=[{"name": name, "args": args, "id": f"call-{self.calls}"}],
            )

    router = ModelRouter(
        keys={"anthropic": "test-key"}, cache=None, cache_mode="off", use_env_keys=False
    )
    provider = Provider()
    router._adapters["anthropic"] = provider
    monkeypatch.setattr(Workspace, "router_for", lambda self, project: router)
    with TestClient(create_app(tmp_path / "ws")) as client:
        pid = client.post("/api/projects", json={"name": "Contracts"}).json()["id"]
        project = client.app.state.workspace.get(pid)
        sheet = project.add_sheet("Contracts")
        columns = {
            name: project.add_column(sheet, name, type=kind)
            for name, kind in [
                ("supplier", "text"),
                ("status", "text"),
                ("amount", "number"),
            ]
        }
        project.add_rows(
            sheet,
            [
                {"supplier": "A", "status": "awarded", "amount": 1},
                {"supplier": "A", "status": "awarded", "amount": 3},
                {"supplier": "A", "status": "draft", "amount": 1000},
            ],
            columns,
        )
        path = f"/api/projects/{pid}/qa/threads"
        thread = client.post(path, json={"scope": {"kind": "project"}}).json()
        path += "/" + thread["id"]
        submitted = client.post(
            path + "/turns",
            json={
                "request_id": "one",
                "question": "Total and median of awarded contracts by supplier?",
                "scope": {"kind": "project"},
                "model": "anthropic/test",
            },
        )
        assert submitted.status_code == 200, submitted.text
        deadline = time.monotonic() + 5
        while True:
            page = client.get(path + "/events").json()
            if page["active_turn"] is None:
                break
            assert time.monotonic() < deadline, page
            time.sleep(0.01)
        answer = next(e for e in page["events"] if e["kind"] == "answer")
        citation = client.get(
            path + "/citations/" + answer["payload"]["citation_ids"][0]
        ).json()
        assert citation["status"] == "current"
        target = citation["target"]
        assert target["total"] == 2
        grid = client.get(
            f"/api/projects/{pid}/sheets/{sheet}/data",
            params={
                "filter": json.dumps(target["filter"]),
                "limit": 1,
            },
        )
        assert grid.status_code == 200, grid.text
        assert grid.json()["total"] == 2
        assert provider.calls == 3
        assert len([e for e in page["events"] if e["kind"] == "usage"]) == 3
        assert (
            "Awarded contracts total 4"
            in client.get(path + "/report").json()["markdown"]
        )
