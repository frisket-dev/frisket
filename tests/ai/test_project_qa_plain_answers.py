from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from frisket.ai.llm import ModelRouter
from frisket.ai.llm.types import LLMRequest, LLMResponse
from frisket.engine.store import Project
from frisket.engine.store.project_qa import ProjectQAStore
from frisket.server.services.project_qa_runner import run_turn


class _PlainTextAdapter:
    """Return ordinary prose through the real structured-agent boundary."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest, client: Any) -> LLMResponse:
        del client
        self.requests.append(request)
        return LLMResponse(
            content=self.answer,
            data=None,
            tokens_in=9,
            tokens_out=7,
            cost=0.01,
            model=request.model,
        )


def _turn(
    tmp_path: Path, question: str, *, prior_answer: str | None
) -> tuple[Project, ProjectQAStore, dict[str, Any]]:
    project = Project.create(tmp_path / "plain-answer.frisket", name="Plain answer")
    sheet_id = project.add_sheet("Dispatches")
    column_id = project.add_column(sheet_id, "Headline")
    [row_id] = project.add_rows(
        sheet_id,
        [{"Headline": "The permit decision was deferred."}],
        {"Headline": column_id},
    )
    scope = {
        "kind": "sources",
        "sources": [{"kind": "rows", "sheet_id": sheet_id, "row_ids": [row_id]}],
    }
    store = ProjectQAStore(project)
    thread = store.create_thread(title="Ask", scope=scope, model="anthropic/test")
    if prior_answer is not None:
        prior = store.submit_turn(
            thread["id"],
            request_id="prior",
            question="What happened to the permit?",
            scope=scope,
            model="anthropic/test",
        )
        store.append_event(
            prior["id"],
            kind="answer",
            payload={
                "text": prior_answer,
                "citation_ids": [],
            },
        )
        store.finish_turn(prior["id"], status="completed")
    turn = store.submit_turn(
        thread["id"],
        request_id="plain-answer",
        question=question,
        scope=scope,
        model="anthropic/test",
    )
    return project, store, turn


@pytest.mark.parametrize(
    ("question", "prior_answer", "provider_answer"),
    [
        (
            "How large is the Council audio file?",
            None,
            "I can’t see the Council audio sheet in this conversation’s selected sources.",
        ),
        (
            "what does it mean?",
            "The permit decision was deferred.",
            "“Deferred” means the permit decision was postponed until a later date.",
        ),
    ],
)
def test_plain_provider_output_saves_the_meaningful_uncited_answer(
    tmp_path: Path,
    question: str,
    prior_answer: str | None,
    provider_answer: str,
) -> None:
    project, store, turn = _turn(tmp_path, question, prior_answer=prior_answer)
    try:
        router = ModelRouter(
            keys={"anthropic": "test-key"},
            key_sources={"anthropic": "platform_key"},
            cache=None,
            cache_mode="off",
            use_env_keys=False,
        )
        adapter = _PlainTextAdapter(provider_answer)
        router._adapters["anthropic"] = adapter  # noqa: SLF001

        answer = asyncio.run(run_turn(project, router, turn, store))

        assert answer == {"text": provider_answer, "citation_ids": []}
        assert adapter.requests
        for request in adapter.requests:
            tool_names = {tool["name"] for tool in request.tools or []}
            assert {
                "inspect_sheets",
                "read_rows",
                "query_rows",
                "search_cells",
                "open_source",
            } <= tool_names
        assert store.get_turn(turn["id"])["scope"] == turn["scope"]
        assert (
            project.db.execute("SELECT COUNT(*) FROM project_qa_citations").fetchone()[
                0
            ]
            == 0
        )

        answers = [
            event
            for event in store.events(turn["thread_id"])["events"]
            if event["turn_id"] == turn["id"] and event["kind"] == "answer"
        ]
        assert [event["payload"] for event in answers] == [answer]
        assert project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[
            0
        ] == len(adapter.requests)
        usage = [
            event
            for event in store.events(turn["thread_id"])["events"]
            if event["turn_id"] == turn["id"] and event["kind"] == "usage"
        ]
        assert len(usage) == len(adapter.requests)
    finally:
        project.close()
