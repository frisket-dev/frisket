from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient

from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.engine.jobs import Worker
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.runner.map_runner import MapRunner
from frisket.engine.runner.review import (
    queue_count,
    review_bundles,
    review_queue,
)
from frisket.server.app import create_app
from frisket.engine.store import Project
from http_test_helpers import post_v1_action_with_exact_confirmation
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from typed_model_fixtures import model_request, run_with_exact_confirmation


class StubAdapter:
    def __init__(self, reply: dict):
        self.reply = reply

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        return LLMResponse(
            content=json.dumps(self.reply),
            data=dict(self.reply),
            tokens_in=10,
            tokens_out=10,
            cost=0.0,
            model=req.model,
        )


def stub_router(reply: dict) -> ModelRouter:
    router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = StubAdapter(reply)  # noqa: SLF001
    return router


def multi_output_spec(sheet_id: int) -> dict:
    return {
        "action_kind": "map.classify",
        "model": "anthropic/claude-haiku-4-5",
        "sheet_id": sheet_id,
        "input_columns": ["story"],
        "fields": [
            {"name": "beat", "type": "category", "labels": ["civic", "private"]},
            {"name": "tone", "type": "category", "labels": ["low", "high"]},
        ],
        "include_justification": True,
        "include_confidence": True,
    }


def multi_output_request(sheet_id: int) -> dict:
    """The same intent as the runner spec, as the typed HTTP request body."""
    return model_request(
        {**multi_output_spec(sheet_id), "idempotency_key": "review-multi-output"}
    ).model_dump(mode="json", exclude_none=True)


def build_project(tmp_path: Path) -> tuple[Project, int]:
    project = Project.create(tmp_path / "review.frisket", name="review")
    sheet_id = project.add_sheet("stories")
    columns = {"story": project.add_column(sheet_id, "story")}
    project.add_rows(
        sheet_id,
        [
            {"story": "Mayor met a lobbyist before the vote."},
            {"story": "Council approved a parks grant."},
        ],
        columns,
    )
    return project, sheet_id


def run_multi_output(project: Project, sheet_id: int) -> None:
    reply = {
        "beat": "civic",
        "tone": "high",
        "beat_justification": "mentions city government",
        "tone_justification": "the event affects public decisions",
        "beat_confidence": 0.41,
    }
    runner = MapRunner(
        project, stub_router(reply), authority=UnroutedOnlyAuthority(project)
    )
    asyncio.run(run_with_exact_confirmation(runner, multi_output_spec(sheet_id)))


def test_multi_output_review_groups_sibling_fields_and_evidence(tmp_path):
    project, sheet_id = build_project(tmp_path)
    run_multi_output(project, sheet_id)

    queue = review_queue(project, sheet_id=sheet_id)
    assert len(queue) == 4
    assert {item["column_name"] for item in queue} == {"beat", "tone"}
    assert queue_count(project) == 4

    bundles = review_bundles(project, sheet_id=sheet_id)
    assert len(bundles) == 2
    first = bundles[0]
    assert {field["column_name"] for field in first["fields"]} == {"beat", "tone"}
    assert {item["column_name"] for item in first["evidence"]} == {
        "beat_justification",
        "tone_justification",
        "beat_confidence",
    }
    assert all(field["chore"] for field in first["fields"])
    assert not any(item["chore"] for item in first["evidence"])
    assert first["source"]["story"]

    beat = next(field for field in first["fields"] if field["column_name"] == "beat")
    accepted_beat = run_action_spec(
        project,
        {
            "action_id": "review.decision",
            "scope": {"kind": "project"},
            "params": {
                "run_id": beat["run_id"],
                "row_id": beat["row_id"],
                "column_id": beat["column_id"],
                "decision": "accept",
            },
            "idempotency_key": "multi-output-beat@sha256:v1",
        },
        project_id="review-multi-output",
    )
    assert accepted_beat.status == "completed", accepted_beat.errors
    updated = next(
        bundle
        for bundle in review_bundles(project, sheet_id=sheet_id)
        if bundle["row_id"] == first["row_id"]
    )
    states = {
        field["column_name"]: field["review_state"] for field in updated["fields"]
    }
    assert states == {"beat": "verified", "tone": "unreviewed"}
    assert queue_count(project) == 3

    tone = next(field for field in updated["fields"] if field["column_name"] == "tone")
    accepted_tone = run_action_spec(
        project,
        {
            "action_id": "review.decision",
            "scope": {"kind": "project"},
            "params": {
                "run_id": tone["run_id"],
                "row_id": tone["row_id"],
                "column_id": tone["column_id"],
                "decision": "accept",
            },
            "idempotency_key": "multi-output-tone@sha256:v1",
        },
        project_id="review-multi-output",
    )
    assert accepted_tone.status == "completed", accepted_tone.errors
    remaining_row_ids = {
        bundle["row_id"] for bundle in review_bundles(project, sheet_id=sheet_id)
    }
    assert first["row_id"] not in remaining_row_ids
    assert queue_count(project) == 2
    project.close()


def test_review_bundles_endpoint_returns_row_run_bundles(tmp_path):
    client = TestClient(
        create_app(
            tmp_path / "ws",
            router=stub_router(
                {
                    "beat": "civic",
                    "tone": "high",
                    "beat_justification": "mentions city government",
                    "tone_justification": "the event affects public decisions",
                    "beat_confidence": 0.41,
                }
            ),
        )
    )
    pid = client.post("/api/projects", json={"name": "Review"}).json()["id"]
    imported = client.post(
        f"/api/projects/{pid}/import/csv",
        files={
            "file": (
                "stories.csv",
                "story\nMayor met a lobbyist.\nCouncil approved a grant.\n",
                "text/csv",
            )
        },
    )
    sheet_id = imported.json()["sheet_id"]
    run = post_v1_action_with_exact_confirmation(
        client, pid, multi_output_request(sheet_id)
    )
    assert run.status_code == 200, run.text
    worker = Worker(
        client.app.state.workspace.queue, client.app.state.workspace.registry
    )
    while worker.run_once():
        pass

    response = client.get(f"/api/projects/{pid}/review/bundles")
    assert response.status_code == 200, response.text
    bundles = response.json()["bundles"]
    assert len(bundles) == 2
    assert {field["column_name"] for field in bundles[0]["fields"]} == {
        "beat",
        "tone",
    }
    assert not any(item["chore"] for item in bundles[0]["evidence"])
