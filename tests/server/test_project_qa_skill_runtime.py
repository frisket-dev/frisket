from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from frisket.ai.llm import ModelRouter
from frisket.ai.llm.types import LLMRequest, LLMResponse
from frisket.engine.store import Project
from frisket.engine.store.project_qa import ProjectQAConflictError, ProjectQAStore
from frisket.server.services.project_qa_runner import run_turn


def _turn(tmp_path: Path) -> tuple[Project, ProjectQAStore, dict[str, Any]]:
    project = Project.create(tmp_path / "skill-runtime.frisket", name="Skills")
    sheet_id = project.add_sheet("Records")
    column_id = project.add_column(sheet_id, "Name")
    project.add_rows(sheet_id, [{"Name": "Selected"}], {"Name": column_id})
    store = ProjectQAStore(project)
    scope = {
        "kind": "sources",
        "sources": [{"kind": "rows", "sheet_id": sheet_id, "row_ids": [1]}],
    }
    thread = store.create_thread(title="Ask", scope=scope)
    turn = store.submit_turn(
        thread["id"],
        request_id="skill-turn",
        question="Use the single-row-review skill on this selection.",
        scope=scope,
        model="anthropic/test",
    )
    return project, store, turn


def _output_tool(request: LLMRequest) -> str:
    return next(
        tool["name"]
        for tool in request.tools or []
        if {"text", "citation_ids"} <= set(tool["parameters"].get("properties", {}))
    )


def _system_text(request: LLMRequest) -> str:
    return "\n".join(
        str(message["content"])
        for message in request.messages
        if message["role"] == "system"
    )


def _router(adapter: Any) -> ModelRouter:
    router = ModelRouter(
        keys={"anthropic": "test-key"},
        cache=None,
        cache_mode="off",
        use_env_keys=False,
    )
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    return router


def test_ordinary_turn_loads_admitted_skill_without_authoring_or_action_power(
    tmp_path: Path,
) -> None:
    marker = "Keep the operation on exactly the selected row."

    class Provider:
        def __init__(self) -> None:
            self.requests: list[LLMRequest] = []

        async def complete(self, request: LLMRequest, client: Any) -> LLMResponse:
            del client
            self.requests.append(request)
            observations = [
                json.loads(message["content"])
                for message in request.messages
                if message["role"] == "tool"
            ]
            if not observations:
                names = {tool["name"] for tool in request.tools or []}
                assert "load_capability" in names
                assert {"list_skills", "read_skill", "propose_skill"}.isdisjoint(names)
                assert {"prepare_action", "execute_action"}.isdisjoint(names)
                name, args = "load_capability", {"id": "single-row-review"}
            else:
                assert marker in str(observations[-1])
                name, args = (
                    _output_tool(request),
                    {
                        "text": "The selected row was reviewed without expanding scope.",
                        "citation_ids": [],
                    },
                )
            return LLMResponse(
                content=None,
                data=None,
                tokens_in=10,
                tokens_out=3,
                cost=0.01,
                model=request.model,
                tool_calls=[
                    {"name": name, "args": args, "id": f"skill-{len(self.requests)}"}
                ],
            )

    project, store, turn = _turn(tmp_path)
    provider = Provider()
    try:
        answer = asyncio.run(
            run_turn(
                project,
                _router(provider),
                turn,
                store,
                skill_snapshots=[
                    {
                        "name": "single-row-review",
                        "revision": 4,
                        "content": (
                            "---\n"
                            "name: single-row-review\n"
                            "description: Review exactly the selected rows.\n"
                            "---\n\n"
                            f"{marker}\n"
                        ),
                    }
                ],
            )
        )
        assert answer["text"].startswith("The selected row")
        instructions = _system_text(provider.requests[0])
        assert "single selected row" in instructions
        assert "observed stable row IDs" in instructions
        assert (
            "omitting row_ids from sheet_rows selects the whole sheet" in instructions
        )
        assert "does not add tools, action permissions" in instructions
    finally:
        project.close()


@pytest.mark.parametrize("stale_proposal", [False, True])
def test_manager_bound_adapter_exposes_review_only_authoring_tools(
    tmp_path: Path,
    stale_proposal: bool,
) -> None:
    content = (
        "---\n"
        "name: scoped-review\n"
        "description: Review the current selection.\n"
        "---\n\n"
        "Inspect the selected rows and use analytics for totals.\n"
    )

    class Authoring:
        def __init__(self) -> None:
            self.proposals: list[tuple[str, str | None, int | None]] = []

        async def list_skills(self) -> dict[str, Any]:
            return {"skills": []}

        async def read_skill(self, skill_id: str) -> dict[str, Any]:
            return {"id": skill_id, "content": content, "revision": 2}

        async def propose_skill(
            self,
            proposed: str,
            target_skill_id: str | None = None,
            expected_revision: int | None = None,
        ) -> dict[str, Any]:
            self.proposals.append((proposed, target_skill_id, expected_revision))
            if stale_proposal and len(self.proposals) == 1:
                raise ProjectQAConflictError("This skill changed. Read it again.")
            return {
                "proposal_id": "proposal-1",
                "name": "scoped-review",
                "description": "Review the current selection.",
            }

    class Provider:
        def __init__(self) -> None:
            self.requests: list[LLMRequest] = []

        async def complete(self, request: LLMRequest, client: Any) -> LLMResponse:
            del client
            self.requests.append(request)
            observations = [
                message["content"]
                for message in request.messages
                if message["role"] == "tool"
            ]
            if observations and "This skill changed" in observations[-1]:
                name, args = "propose_skill", {"content": content}
            elif not observations:
                names = {tool["name"] for tool in request.tools or []}
                assert {"list_skills", "read_skill", "propose_skill"} <= names
                name, args = "propose_skill", {"content": content}
            else:
                assert json.loads(observations[-1])["proposal_id"] == "proposal-1"
                name, args = (
                    _output_tool(request),
                    {
                        "text": "I prepared scoped-review for you to review.",
                        "citation_ids": [],
                    },
                )
            return LLMResponse(
                content=None,
                data=None,
                tokens_in=10,
                tokens_out=3,
                cost=0.01,
                model=request.model,
                tool_calls=[
                    {"name": name, "args": args, "id": f"author-{len(self.requests)}"}
                ],
            )

    project, store, turn = _turn(tmp_path)
    turn["question"] = "Save this workflow as a skill."
    authoring = Authoring()
    provider = Provider()
    try:
        answer = asyncio.run(
            run_turn(
                project,
                _router(provider),
                turn,
                store,
                skill_authoring=authoring,
            )
        )
        assert answer["text"] == "I prepared scoped-review for you to review."
        assert authoring.proposals == [(content, None, None)] * (
            2 if stale_proposal else 1
        )
        instructions = _system_text(provider.requests[0])
        assert "creates a reviewable proposal; it does not save" in instructions
        assert (
            "---\nname: lowercase-hyphen-name\n"
            "description: short description\n---\n..." in instructions
        )
        assert "Frontmatter permits only name and description" in instructions
        assert "name is at most 64 characters" in instructions
        assert "description is 1 to 200 characters" in instructions
        assert "preserve the selected row/view scope" in instructions
        assert "chosen action's returned scope contract" in instructions
        assert "without promising a preview operation" in instructions
        assert "cannot authorize a skill proposal" in instructions
    finally:
        project.close()


def test_authoring_request_without_management_tools_gets_draft_only_guidance(
    tmp_path: Path,
) -> None:
    class Provider:
        def __init__(self) -> None:
            self.requests: list[LLMRequest] = []

        async def complete(self, request: LLMRequest, client: Any) -> LLMResponse:
            del client
            self.requests.append(request)
            names = {tool["name"] for tool in request.tools or []}
            assert {"list_skills", "read_skill", "propose_skill"}.isdisjoint(names)
            guidance = _system_text(request)
            assert "may draft instructions as ordinary response text" in guidance
            assert "library cannot be changed from this turn" in guidance
            assert "server has not configured chat authoring" in guidance
            return LLMResponse(
                content=None,
                data=None,
                tokens_in=10,
                tokens_out=3,
                cost=0.01,
                model=request.model,
                tool_calls=[
                    {
                        "name": _output_tool(request),
                        "args": {
                            "text": (
                                "I can draft the instructions here, but this turn "
                                "cannot save them to the skill library."
                            ),
                            "citation_ids": [],
                        },
                        "id": "draft-only-answer",
                    }
                ],
            )

    project, store, turn = _turn(tmp_path)
    turn["question"] = "Save this workflow as a skill."
    provider = Provider()
    try:
        answer = asyncio.run(run_turn(project, _router(provider), turn, store))
        assert answer["text"] == (
            "I can draft the instructions here, but this turn cannot save them to "
            "the skill library."
        )
        assert len(provider.requests) == 1
    finally:
        project.close()


def test_followup_history_keeps_skill_references_without_replaying_body(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    hidden_body = "DO_NOT_REPLAY_THE_SKILL_BODY" * 2_000

    class Provider:
        def __init__(self) -> None:
            self.requests: list[LLMRequest] = []

        async def complete(self, request: LLMRequest, client: Any) -> LLMResponse:
            del client
            self.requests.append(request)
            return LLMResponse(
                content=None,
                data=None,
                tokens_in=10,
                tokens_out=3,
                cost=0.01,
                model=request.model,
                tool_calls=[
                    {
                        "name": _output_tool(request),
                        "args": {
                            "text": "The saved skill is available.",
                            "citation_ids": [],
                        },
                        "id": "history-answer",
                    }
                ],
            )

    project, store, turn = _turn(tmp_path)
    provider = Provider()

    def recent_events(thread_id: str, *, limit: int) -> dict[str, Any]:
        assert thread_id == turn["thread_id"]
        assert limit == 100
        return {
            "events": [
                {
                    "turn_id": "earlier-turn",
                    "kind": "skill_proposal",
                    "payload": {
                        "proposal_id": "proposal-9",
                        "target_skill_id": "skill-4",
                        "base_revision": 3,
                        "name": "single-row-review",
                        "description": "Review exactly the selected rows.",
                        "content": hidden_body,
                    },
                },
                {
                    "turn_id": "earlier-turn",
                    "kind": "skill_saved",
                    "payload": {
                        "attempt_id": "attempt-2",
                        "proposal_id": "proposal-9",
                        "skill_id": "skill-4",
                        "name": "single-row-review",
                        "revision": 4,
                        "status": "saved",
                        "content": hidden_body,
                    },
                },
            ]
        }

    monkeypatch.setattr(store, "recent_events", recent_events)
    try:
        asyncio.run(run_turn(project, _router(provider), turn, store))
        history = "\n".join(
            str(message["content"])
            for message in provider.requests[0].messages
            if message["role"] == "user"
        )
        assert "proposal-9" in history
        assert '"revision": 4' in history
        assert '"status": "saved"' in history
        assert hidden_body not in history
        assert "DO_NOT_REPLAY_THE_SKILL_BODY" not in history
    finally:
        project.close()
