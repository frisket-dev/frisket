"""Project Ask admission and ownership of bounded asynchronous turns.

All entrypoints run on the serving event loop. Transactions never span an
await; the project store serializes durable admission and event publication.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from weakref import WeakSet

from frisket.ai.llm import LLMError
from frisket.ai.llm.remediation import classify_llm_error
from frisket.contracts.http.project_qa import (
    AskThreadCreate,
    AskThreadUpdate,
    AskTurnRequest,
)
from frisket.engine.runner import ProviderKeyRefusal
from frisket.engine.store import Project
from frisket.engine.store.project_qa import (
    ProjectQAConflictError,
    ProjectQANotFoundError,
    ProjectQAStore,
)
from frisket.server.workspace import Workspace

logger = logging.getLogger(__name__)
TurnRunner = Callable[[Project, Any, dict[str, Any], ProjectQAStore], Awaitable[None]]


class ProjectQAService:
    def __init__(self, workspace: Workspace, *, runner: TurnRunner | None = None):
        self.workspace = workspace
        self._runner = runner
        self._seen: WeakSet[Project] = WeakSet()
        self._tasks: dict[tuple[Project, str], asyncio.Task[None]] = {}
        self._closed = False

    def store(self, project_id: str) -> ProjectQAStore:
        project = self.workspace.get(project_id)
        store = ProjectQAStore(project)
        if project not in self._seen:
            store.reconcile_abandoned_turns(
                live_turn_ids=[
                    turn_id for (owner, turn_id) in self._tasks if owner is project
                ]
            )
            self._seen.add(project)
        return store

    def list(self, project_id: str) -> list[dict[str, Any]]:
        return self.store(project_id).list_threads()

    def create(
        self, project_id: str, body: AskThreadCreate, *, actor: str | None = None
    ) -> dict:
        values = body.model_dump()
        values["scope"] = body.scope.model_dump(exclude_none=True)
        return self.store(project_id).create_thread(**values, created_by=actor)

    def update(self, project_id: str, thread_id: str, body: AskThreadUpdate) -> dict:
        values = body.model_dump(exclude_unset=True)
        if body.scope is not None:
            values["scope"] = body.scope.model_dump(exclude_none=True)
        return self.store(project_id).update_thread(thread_id, **values)

    def delete(self, project_id: str, thread_id: str) -> None:
        self.store(project_id).delete_thread(thread_id)

    def detail(self, project_id: str, thread_id: str) -> dict:
        store = self.store(project_id)
        return {
            "thread": store.get_thread(thread_id),
            "active_turn": store.get_active_turn(thread_id),
            "history": self.events(project_id, thread_id),
        }

    def events(
        self,
        project_id: str,
        thread_id: str,
        *,
        after: int = 0,
        before: int | None = None,
        limit: int = 100,
    ) -> dict:
        store = self.store(project_id)
        return {
            **store.events(thread_id, after=after, before=before, limit=limit),
            "active_turn": store.get_active_turn(thread_id),
        }

    def submit(
        self,
        project_id: str,
        thread_id: str,
        body: AskTurnRequest,
        *,
        actor: str | None = None,
    ) -> dict:
        if self._closed:
            raise ProjectQAConflictError(
                "Ask is shutting down. Try again after reconnecting."
            )
        project = self.workspace.get(project_id)
        store = self.store(project_id)
        values = body.model_dump()
        values["scope"] = body.scope.model_dump(exclude_none=True)
        turn = store.submit_turn(thread_id, **values, submitted_by=actor)
        key = (project, turn["id"])
        if turn["status"] == "running" and key not in self._tasks:
            task = asyncio.create_task(self._run(project, turn, store))
            self._tasks[key] = task
            task.add_done_callback(lambda completed: self._finished(key, completed))
        return turn

    async def _run(self, project: Project, turn: dict, store: ProjectQAStore) -> None:
        try:
            router = self.workspace.router_for(project)
            runner = self._runner
            if runner is None:
                from frisket.server.services.project_qa_runner import run_turn

                runner = run_turn
            async with asyncio.timeout(180):
                await runner(project, router, turn, store)
            store.finish_turn(turn["id"], status="completed")
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            store.append_event(
                turn["id"],
                kind="assistant",
                payload={
                    "text": "This question reached its time limit. The work above is saved; you can ask a narrower follow-up.",
                },
            )
            store.finish_turn(turn["id"], status="completed")
        except ProviderKeyRefusal as exc:
            store.finish_turn(
                turn["id"], status="failed", error_summary=exc.action_message()
            )
        except LLMError as exc:
            model = turn["model"] or ""
            message = classify_llm_error(
                exc, provider=model.split("/", 1)[0], model=model
            ).message
            store.finish_turn(turn["id"], status="failed", error_summary=message)
        except Exception:
            logger.exception("Project Ask turn failed")
            store.finish_turn(
                turn["id"],
                status="failed",
                error_summary="This question could not be completed. Your conversation is saved; please try again.",
            )

    def _finished(self, key: tuple[Project, str], task: asyncio.Task[None]) -> None:
        self._tasks.pop(key, None)
        project, turn_id = key
        store = ProjectQAStore(project)
        try:
            turn = store.get_turn(turn_id)
            if turn["status"] in {"running", "stopping"}:
                store.finish_turn(
                    turn_id,
                    status="stopped" if turn["status"] == "stopping" else "interrupted",
                )
            if not task.cancelled() and task.exception() is not None:
                logger.error("Project Ask cleanup failed", exc_info=task.exception())
        except Exception:
            logger.exception("Could not finalize Project Ask turn")

    def stop(self, project_id: str, thread_id: str, turn_id: str) -> dict:
        project = self.workspace.get(project_id)
        store = self.store(project_id)
        if store.get_turn(turn_id)["thread_id"] != thread_id:
            raise ProjectQANotFoundError("Turn not found in this conversation.")
        turn = store.request_stop(turn_id)
        task = self._tasks.get((project, turn_id))
        if task is not None:
            task.cancel()
        elif turn["status"] == "stopping":
            turn = store.finish_turn(turn_id, status="stopped")
        return turn

    async def shutdown(self) -> None:
        self._closed = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def report(self, project_id: str, thread_id: str) -> dict:
        store = self.store(project_id)
        lines = [f"# {store.get_thread(thread_id)['title']}", ""]
        cursor = 0
        while True:
            page = store.events(thread_id, after=cursor, limit=200)
            for event in page["events"]:
                payload = event["payload"]
                if event["kind"] == "question":
                    lines.extend([f"## {payload['question']}", ""])
                elif event["kind"] in {"answer", "assistant"}:
                    lines.extend([str(payload.get("text", "")), ""])
            if not page["has_more"]:
                break
            cursor = page["cursor"]
        return {"markdown": "\n".join(lines)}
