"""Real Ask admission, skill tools, terminal Save and same-thread reuse."""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.ai.llm.types import LLMResponse
from frisket.server.app import create_app
from frisket.server.workspace import Workspace


SKILL = """---
name: check-selected-supplier
description: Read only the selected supplier record and report its amount.
---

Inspect the admitted rows. Do not broaden the scope or execute actions.
"""


def _finished(client: TestClient, path: str) -> dict:
    deadline = time.monotonic() + 10  # realtime: actual background Ask task
    while True:
        response = client.get(path + "/events")
        assert response.status_code == 200, response.text
        page = response.json()
        if page["active_turn"] is None:
            assert not any(
                e["kind"] == "status" and e["payload"].get("status") == "failed"
                for e in page["events"]
            ), page
            return page
        assert time.monotonic() < deadline, page  # realtime: detect a stalled task
        time.sleep(0.01)  # realtime: yield to actual worker threads


def test_skill_saved_after_answer_is_used_on_selected_row_in_same_chat(
    tmp_path, monkeypatch
):
    class Provider:
        calls = 0
        marker = "EDITOR-ONLY-REVISION-ONE"
        row_id = 0
        sheet_id = 0
        seen_markers: list[str] = []

        async def complete(self, request, client):
            self.calls += 1
            tools = {tool["name"] for tool in request.tools}
            observations = [
                str(message["content"])
                for message in request.messages
                if message.get("role") == "tool"
            ]
            if self.calls == 1:
                assert "propose_skill" in tools
                name, args = "propose_skill", {"content": SKILL}
            elif self.calls == 2:
                return self.answer(request, "The skill is ready to review.")
            else:
                stage = (self.calls - 3) % 3
                if stage == 0:
                    assert "load_capability" in tools
                    name, args = "load_capability", {"id": "check-selected-supplier"}
                elif stage == 1:
                    assert self.marker in "\n".join(observations), observations
                    self.seen_markers.append(self.marker)
                    name, args = "read_rows", {"sheet_id": self.sheet_id}
                else:
                    observed = json.loads(observations[-1])
                    assert [row["row_id"] for row in observed["rows"]] == [self.row_id]
                    assert "Beta" in observations[-1]
                    assert "Alpha" not in observations[-1]
                    return self.answer(request, f"{self.marker}: Beta has amount 900.")
            return LLMResponse(
                content=None,
                data=None,
                tokens_in=10,
                tokens_out=5,
                cost=0,
                model=request.model,
                tool_calls=[{"name": name, "args": args, "id": f"step-{self.calls}"}],
            )

        @staticmethod
        def answer(request, text):
            return LLMResponse(
                content=text,
                data=None,
                tokens_in=10,
                tokens_out=5,
                cost=0,
                model=request.model,
            )

    router = ModelRouter(
        keys={"anthropic": "test-key"}, cache=None, cache_mode="off", use_env_keys=False
    )
    provider = Provider()
    router._adapters["anthropic"] = provider
    monkeypatch.setattr(Workspace, "router_for", lambda self, project: router)
    with TestClient(create_app(tmp_path / "workspace")) as client:
        pid = client.post("/api/projects", json={"name": "Skill flow"}).json()["id"]
        project = client.app.state.workspace.get(pid)
        sheet_id = project.add_sheet("Suppliers")
        columns = {
            "supplier": project.add_column(sheet_id, "supplier", type="text"),
            "amount": project.add_column(sheet_id, "amount", type="number"),
        }
        rows = project.add_rows(
            sheet_id,
            [{"supplier": "Alpha", "amount": 10}, {"supplier": "Beta", "amount": 900}],
            columns,
        )
        provider.sheet_id = sheet_id
        provider.row_id = rows[-1]
        scope = {
            "kind": "sources",
            "sources": [{"kind": "rows", "sheet_id": sheet_id, "row_ids": [rows[-1]]}],
        }
        path = f"/api/projects/{pid}/qa/threads"
        thread = client.post(path, json={"scope": scope}).json()
        path += "/" + thread["id"]

        def submit(request_id, question):
            response = client.post(
                path + "/turns",
                json={
                    "request_id": request_id,
                    "question": question,
                    "scope": scope,
                    "model": "anthropic/test",
                },
            )
            assert response.status_code == 200, response.text
            return response.json()

        submit("create", "Create a reusable skill for checking this selected supplier.")
        page = _finished(client, path)
        proposal = next(e for e in page["events"] if e["kind"] == "skill_proposal")
        edited = SKILL + f"\nInclude {provider.marker} in your answer.\n"
        save_path = (
            f"{path}/turns/{proposal['turn_id']}/skill-proposals/"
            f"{proposal['payload']['proposal_id']}/save"
        )
        saved = client.post(
            save_path,
            json={
                "request_id": "save-one",
                "content": edited,
                "expected_revision": None,
            },
        )
        assert saved.status_code == 200, saved.text
        skill = saved.json()["skill"]
        assert skill["content"] == edited
        assert skill["revision"] == 1
        assert provider.calls == 2  # Save itself never starts model/action work.
        submit("use-one", "Use the skill on the selected row.")
        _finished(client, path)
        assert provider.seen_markers == [provider.marker]

        provider.marker = "SETTINGS-ONLY-REVISION-TWO"
        revised = SKILL + f"\nInclude {provider.marker} in your answer.\n"
        changed = client.put(
            f"/api/skills/{skill['id']}",
            json={"content": revised, "expected_revision": 1},
        )
        assert changed.status_code == 200, changed.text
        submit("use-two", "Use the skill on this row again.")
        page = _finished(client, path)
        assert provider.seen_markers[-1] == provider.marker
        assert len(provider.seen_markers) == 2
        assert client.get(path).json()["thread"]["id"] == thread["id"]
        answers = [
            e["payload"]["text"] for e in page["events"] if e["kind"] == "answer"
        ]
        assert provider.marker in answers[-1]
