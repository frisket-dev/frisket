"""Project Ask admission and ownership of bounded asynchronous turns.

The event loop owns task lifetimes. Blocking project reads/writes run through
our cancellation-safe thread worker; SQLite owns durable admission and ordering.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from typing import Any
from weakref import WeakSet

from frisket.ai.llm import LLMError
from frisket.ai.llm.types import provider_from_model_id
from frisket.ai.llm.remediation import classify_llm_error
from frisket.authoring.project_ask import default_project_ask_model
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
from frisket.server.services.project_qa_citations import (
    resolve_citation,
    project_qa_safe_citation_projection,
)
from frisket.server.services.project_qa_tools import validate_scope
from frisket.server.thread_worker import await_thread_worker
from frisket.server.workspace import Workspace
from frisket.server.project_qa_runtime import ProjectQATurnRuntime
from frisket.redaction import safe_error
from pydantic_ai.exceptions import UnexpectedModelBehavior

logger = logging.getLogger(__name__)
TurnRunner = Callable[[Project, Any, dict[str, Any], ProjectQAStore], Awaitable[Any]]
_KNOWN_TOOLS = frozenset(
    {
        "analytics",
        "describe_action",
        "final_result",
        "find_in_source",
        "inspect_sheets",
        "list_sources",
        "open_source",
        "open_web_page",
        "propose_action",
        "query_rows",
        "read_rows",
        "search_actions",
        "search_cells",
        "search_web",
    }
)
_TOOL_RETRY_RE = re.compile(r"^Tool '([^']+)' exceeded max retries count of \d+$")
_CITATION_RETRY = "Use only current-turn citation IDs returned by the tools; reopen prior sources first."


def _model_failure_reason(error: UnexpectedModelBehavior) -> tuple[str, str | None]:
    match = _TOOL_RETRY_RE.fullmatch(str(error))
    if match:
        tool = match.group(1)
        return "tool_call_invalid", tool if tool in _KNOWN_TOOLS else None
    seen: set[int] = set()
    cause: BaseException | None = error
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if str(cause) == _CITATION_RETRY:
            return "citation_invalid", None
        if type(cause).__name__ in {"ToolRetryError", "ValidationError"}:
            return "final_result_invalid", None
        cause = cause.__cause__ or cause.__context__
    return "output_failure", None


def _last_turn_activity(
    store: ProjectQAStore, turn: dict[str, Any]
) -> tuple[str | None, str | None]:
    events = store.recent_events(turn["thread_id"], limit=100)["events"]
    current = [event for event in events if event["turn_id"] == turn["id"]]
    last_event = current[-1]["kind"] if current else None
    tool = next(
        (
            event["payload"].get("tool")
            for event in reversed(current)
            if event["kind"] in {"tool_started", "tool_completed"}
            and event["payload"].get("tool") in _KNOWN_TOOLS
        ),
        None,
    )
    return last_event, tool


class ProjectQAService:
    def __init__(self, workspace: Workspace, *, runner: TurnRunner | None = None):
        self.workspace = workspace
        self._runner = runner
        self._seen: WeakSet[Project] = WeakSet()
        self._tasks: dict[tuple[Project, str], asyncio.Task[None]] = {}
        self._started: set[tuple[Project, str]] = set()
        self._runtime_closers: set[asyncio.Task[None]] = set()
        self._admission = asyncio.Lock()
        self._closed = False

    async def _failure_diagnostic(
        self,
        store: ProjectQAStore,
        turn: dict[str, Any],
        error: BaseException,
        *,
        code: str,
        model: str | None,
        reason: str | None = None,
        tool: str | None = None,
    ) -> dict[str, str]:
        try:
            last_event, observed_tool = await await_thread_worker(
                _last_turn_activity, store, turn
            )
        except Exception:
            last_event, observed_tool = None, None
        tool = tool or observed_tool
        diagnostic = {
            "code": code,
            "reference": turn["id"],
            **({"reason": reason} if reason is not None else {}),
            **({"last_event": last_event} if last_event is not None else {}),
            **({"tool": tool} if tool is not None else {}),
        }
        safe = safe_error(code, error, include_frames=True)
        logger.error(
            "project_qa_turn_failed",
            extra={
                "event": "project_qa_turn_failed",
                "error_code": code,
                "reference": turn["id"],
                "reason": reason,
                "turn_id": turn["id"],
                "thread_id": turn["thread_id"],
                "model": model,
                "provider": provider_from_model_id(model) if model else None,
                "last_event": last_event,
                "tool": tool,
                "exception_type": safe.exception_type,
                "exception_frames": safe.frames,
            },
            exc_info=False,
        )
        return diagnostic

    async def store(self, project_id: str) -> ProjectQAStore:
        project = await await_thread_worker(self.workspace.get, project_id)
        store = ProjectQAStore(project)
        async with self._admission:
            if project not in self._seen:
                await await_thread_worker(
                    store.reconcile_abandoned_turns, live_turn_ids=[]
                )
                self._seen.add(project)
        return store

    async def list(
        self, project_id: str, *, offset: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        store = await self.store(project_id)
        return await await_thread_worker(store.list_threads, offset=offset, limit=limit)

    async def create(
        self, project_id: str, body: AskThreadCreate, *, actor: str | None = None
    ) -> dict:
        store = await self.store(project_id)

        def create():
            values = body.model_dump()
            values["scope"] = body.scope.model_dump(exclude_none=True)
            validate_scope(self.workspace.get(project_id), values["scope"])
            return store.create_thread(**values, created_by=actor)

        return await await_thread_worker(create)

    async def update(
        self, project_id: str, thread_id: str, body: AskThreadUpdate
    ) -> dict:
        store = await self.store(project_id)

        def update():
            values = body.model_dump(exclude_unset=True)
            if body.scope is not None:
                values["scope"] = body.scope.model_dump(exclude_none=True)
                validate_scope(self.workspace.get(project_id), values["scope"])
            return store.update_thread(thread_id, **values)

        return await await_thread_worker(update)

    async def delete(self, project_id: str, thread_id: str) -> None:
        store = await self.store(project_id)
        await await_thread_worker(store.delete_thread, thread_id)

    async def detail(self, project_id: str, thread_id: str) -> dict:
        store = await self.store(project_id)

        def read():
            detail = store.detail_with_history(thread_id)
            detail["history"] = self._project_page(
                project_id, {**detail["history"], "active_turn": detail["active_turn"]}
            )
            return detail

        return await await_thread_worker(read)

    async def events(
        self,
        project_id: str,
        thread_id: str,
        *,
        after: int = 0,
        before: int | None = None,
        limit: int = 100,
    ) -> dict:
        store = await self.store(project_id)

        def read():
            page = store.events_with_active(
                thread_id, after=after, before=before, limit=limit
            )
            return self._project_page(project_id, page)

        return await await_thread_worker(read)

    def _project_page(self, project_id: str, page: dict) -> dict:
        project = self.workspace.get(project_id)
        events = []
        for event in page["events"]:
            ids = list(event["payload"].get("citation_ids", []))
            if event["kind"] == "result_suggestion" and event["payload"].get(
                "citation_id"
            ):
                ids.append(event["payload"]["citation_id"])
            citations = []
            for citation_id in ids:
                try:
                    citations.append(
                        resolve_citation(project, event["thread_id"], citation_id)
                    )
                except ProjectQANotFoundError:
                    continue
            events.append({**event, "citations": citations})
        return {**page, "events": events}

    async def citation(self, project_id: str, thread_id: str, citation_id: str) -> dict:
        project = await await_thread_worker(self.workspace.get, project_id)
        return await await_thread_worker(
            resolve_citation, project, thread_id, citation_id
        )

    async def submit(
        self,
        project_id: str,
        thread_id: str,
        body: AskTurnRequest,
        *,
        actor: str | None = None,
    ) -> dict:
        # Once durable admission starts, a disconnected HTTP caller must not
        # strand a running row without its task or hosted runtime cleanup.
        admission = asyncio.create_task(
            self._submit_owned(project_id, thread_id, body, actor=actor)
        )
        try:
            return await asyncio.shield(admission)
        except asyncio.CancelledError:
            await admission
            raise

    async def _submit_owned(
        self,
        project_id: str,
        thread_id: str,
        body: AskTurnRequest,
        *,
        actor: str | None,
    ) -> dict:
        store = await self.store(project_id)
        project = await await_thread_worker(self.workspace.get, project_id)
        async with self._admission:
            if self._closed:
                raise ProjectQAConflictError(
                    "Ask is shutting down. Try again after reconnecting."
                )

            def admit():
                values = body.model_dump()
                values["scope"] = body.scope.model_dump(exclude_none=True)
                validate_scope(project, values["scope"])
                return store.submit_turn(thread_id, **values, submitted_by=actor)

            turn = await await_thread_worker(admit)
            key = (project, turn["id"])
            if turn["status"] == "running" and key not in self._tasks:
                runtime = None
                port = self.workspace.project_qa_runtime_port
                try:
                    if port is not None:
                        runtime = await port.prepare_turn(
                            project=project,
                            project_id=project_id,
                            turn_id=turn["id"],
                            actor=actor,
                        )
                except Exception:
                    await await_thread_worker(
                        store.finalize_turn,
                        turn["id"],
                        status="failed",
                        error_summary="This question could not be started. Please try again.",
                    )
                    raise
                task = asyncio.create_task(self._run(project, turn, store, runtime))
                self._tasks[key] = task
                task.add_done_callback(
                    lambda completed: self._finished(key, completed, runtime)
                )
            return turn

    async def _run(
        self,
        project: Project,
        turn: dict,
        store: ProjectQAStore,
        runtime: ProjectQATurnRuntime | None = None,
    ) -> None:
        self._started.add((project, turn["id"]))
        status = "interrupted"
        error = None
        diagnostic = None
        model = turn["model"]
        budget = asyncio.timeout(180)
        try:
            if (await await_thread_worker(store.get_turn, turn["id"]))[
                "status"
            ] == "stopping":
                return
            router = await await_thread_worker(self.workspace.router_for, project)
            model = model or default_project_ask_model(router)

            async def settle_call(call_id: str) -> None:
                if runtime is not None:
                    await await_thread_worker(
                        runtime.settle_call,
                        project=project,
                        turn_id=turn["id"],
                        call_id=call_id,
                    )

            async with budget:
                if self._runner is not None:
                    result = await self._runner(project, router, turn, store)
                else:
                    from frisket.server.services.project_qa_runner import run_turn

                    result = await run_turn(
                        project,
                        router,
                        turn,
                        store,
                        on_call=settle_call,
                        call_scope=lambda: (
                            runtime.call_scope(router)
                            if runtime is not None
                            else nullcontext()
                        ),
                    )
            if isinstance(result, dict) and result.get("limited"):
                error = "This investigation reached its limit. The work above is saved."
            else:
                status = "completed"
        except asyncio.CancelledError:
            # finalize_turn atomically maps a requested Stop to stopped.
            pass
        except TimeoutError:
            status = "interrupted" if budget.expired() else "failed"
            error = (
                "This question reached its time limit. The work above is saved; you can ask a narrower follow-up."
                if budget.expired()
                else "A source took too long to respond. Your conversation is saved; please try again."
            )
        except ProviderKeyRefusal as exc:
            status, error = "failed", exc.action_message()
        except LLMError as exc:
            status = "failed"
            error = classify_llm_error(
                exc, provider=(model or "").split("/", 1)[0], model=model or ""
            ).message
        except UnexpectedModelBehavior as exc:
            status = "failed"
            reason, tool = _model_failure_reason(exc)
            diagnostic = await self._failure_diagnostic(
                store,
                turn,
                exc,
                code="invalid_model_response",
                model=model,
                reason=reason,
                tool=tool,
            )
            error = (
                "The selected model could not complete a valid Ask response after retrying. "
                "Your conversation is saved; please try again."
            )
        except Exception as exc:
            status = "failed"
            diagnostic = await self._failure_diagnostic(
                store, turn, exc, code="internal_error", model=model
            )
            error = (
                "This question could not be completed. Your conversation is saved; "
                "please try again."
            )
        finally:
            await await_thread_worker(
                store.finalize_turn,
                turn["id"],
                status=status,
                error_summary=error,
                diagnostic=diagnostic,
            )

    async def _close_runtime(self, runtime: ProjectQATurnRuntime) -> None:
        try:
            await runtime.aclose()
        except Exception:
            logger.exception("Project Ask runtime cleanup failed")

    def _finished(
        self,
        key: tuple[Project, str],
        task: asyncio.Task[None],
        runtime: ProjectQATurnRuntime | None = None,
    ) -> None:
        if runtime is not None:
            cleanup = asyncio.create_task(self._close_runtime(runtime))
            self._runtime_closers.add(cleanup)
            cleanup.add_done_callback(self._runtime_closers.discard)
        self._started.discard(key)
        if task.cancelled() or task.exception() is not None:
            # Retain the task guard after failed finalization: a request replay
            # must never repeat paid work. A fresh process reconciles the row.
            logger.error(
                "Project Ask finalization failed",
                exc_info=None if task.cancelled() else task.exception(),
            )
            return
        self._tasks.pop(key, None)

    async def stop(self, project_id: str, thread_id: str, turn_id: str) -> dict:
        store = await self.store(project_id)
        project = await await_thread_worker(self.workspace.get, project_id)

        def request_stop():
            if store.get_turn(turn_id)["thread_id"] != thread_id:
                raise ProjectQANotFoundError("Turn not found in this conversation.")
            return store.request_stop(turn_id)

        turn = await await_thread_worker(request_stop)
        task = self._tasks.get((project, turn_id))
        if task is not None and not task.done():
            if (project, turn_id) in self._started:
                task.cancel()
        elif turn["status"] == "stopping":
            turn = await await_thread_worker(
                store.finalize_turn, turn_id, status="stopped"
            )
            self._tasks.pop((project, turn_id), None)
        return turn

    async def shutdown(self) -> None:
        async with self._admission:
            self._closed = True
            tasks = list(self._tasks.values())
            for task in tasks:
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Releasing the last hosted app lease can itself trigger app shutdown.
        # That cleanup task already owns its completion; never await itself.
        await asyncio.gather(
            *(
                task
                for task in self._runtime_closers
                if task is not asyncio.current_task()
            ),
            return_exceptions=True,
        )
        # A task cancelled before its first instruction cannot enter _run's
        # finally. Reconcile only after all workers have drained.
        for project in list(self._seen):
            await await_thread_worker(
                ProjectQAStore(project).reconcile_abandoned_turns, live_turn_ids=[]
            )
        self._tasks.clear()

    async def report(self, project_id: str, thread_id: str) -> dict:
        store = await self.store(project_id)

        def render():
            lines = [f"# {store.get_thread(thread_id)['title']}", ""]
            cursor = 0
            while True:
                page = store.events(thread_id, after=cursor, limit=200)
                for event in page["events"]:
                    payload = event["payload"]
                    if event["kind"] == "question":
                        lines.extend([f"## {payload.get('question', '')}", ""])
                    elif event["kind"] in {"answer", "assistant"}:
                        lines.extend([str(payload.get("text", "")), ""])
                        for citation_id in payload.get("citation_ids", []):
                            citation = project_qa_safe_citation_projection(
                                store.get_citation(citation_id)
                            )
                            lines.extend(
                                [
                                    f"- {citation['label']}"
                                    + (
                                        f" — {citation['url']}"
                                        if citation["url"]
                                        else ""
                                    ),
                                    "",
                                ]
                            )
                if not page["has_more"]:
                    break
                cursor = page["cursor"]
            return {"markdown": "\n".join(lines)}

        return await await_thread_worker(render)
