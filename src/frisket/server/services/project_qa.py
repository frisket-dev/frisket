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
    AskResearchResume,
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
from frisket.server.services.project_qa_tracing import (
    ProjectQATracing,
    build_project_qa_tracing,
)
from frisket.server.thread_worker import await_thread_worker
from frisket.server.workspace import Workspace
from frisket.server.project_qa_runtime import ProjectQATurnRuntime
from frisket.server.services.project_qa_authority import BackgroundAskContext
from frisket.server.services.project_qa_research import ResearchSession
from frisket.server.services.project_qa_capabilities import ProjectQACapabilityError
from frisket.engine.store.project_qa_research import (
    ProjectQAResearchStore,
    ProjectQAResearchNotFound,
)
from frisket.execution.pricing_policy import usd_to_micros
from frisket.redaction import safe_error
from pydantic_ai.exceptions import UnexpectedModelBehavior
from frisket.server.services.project_qa_research import ResearchSkillsUnavailable

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
        "prepare_action",
        "execute_action",
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
        self._tracing: ProjectQATracing | None = build_project_qa_tracing()
        self._seen: WeakSet[Project] = WeakSet()
        self._tasks: dict[tuple[Project, str], asyncio.Task[None]] = {}
        self._started: set[tuple[Project, str]] = set()
        self._runtime_closers: set[asyncio.Task[None]] = set()
        self._admission = asyncio.Lock()
        self._closed = False
        self._research_authorize: Callable[[str, BackgroundAskContext], None] | None = (
            None
        )
        self._skill_library: Callable[[], Any] | None = None
        self._research_sessions: dict[tuple[Project, str], ResearchSession] = {}
        self._search_service_factory: Callable[[], Any] | None = None
        self._sidecar_capabilities: Callable[[], dict[str, Any]] = lambda: {}

    def configure_actions(
        self, *, sidecar_capabilities: Callable[[], dict[str, Any]]
    ) -> None:
        self._sidecar_capabilities = sidecar_capabilities

    def configure_search(self, factory: Callable[[], Any]) -> None:
        self._search_service_factory = factory

    def configure_research(
        self,
        *,
        authorize: Callable[[str, BackgroundAskContext], None],
        skills: Callable[[], Any] | None = None,
    ) -> None:
        """Supply edition-owned live authorization and an isolated skill library."""
        self._research_authorize = authorize
        self._skill_library = skills

    def _default_research_budget(self, project_id, project, context):
        from frisket.engine.store.execution_routes import (
            ConsentRegistry,
            instance_principal,
            standing_cost_threshold,
        )

        factory = self.workspace.executor_deps_factory
        coverage = factory(project_id, context).consent_coverage if factory else None
        if coverage is not None:
            return coverage.threshold_usd
        return standing_cost_threshold(
            ConsentRegistry(project).standing_consents(),
            principal=instance_principal(project),
        )

    def _authorize_run(self, project_id, context, snapshots=()):
        from frisket.execution.pricing_policy import (
            IdentityPricingPolicy,
            default_pricing_policy,
        )

        if self._research_authorize is None:
            raise PermissionError(
                "Automatic research is not configured for this server."
            )
        if not isinstance(default_pricing_policy(), IdentityPricingPolicy):
            raise PermissionError(
                "Automatic actions are not supported by this server's billing setup."
            )
        self._research_authorize(project_id, context)
        enabled = self._skill_library().enabled() if self._skill_library else []
        if {item["name"] for item in snapshots} - {item["name"] for item in enabled}:
            raise ResearchSkillsUnavailable(
                "A skill used by this research was disabled or removed. Enable it in Settings to continue, or stop this run."
            )

    async def research_options(self, project_id, *, request_context=None):
        context = BackgroundAskContext.capture(request_context, self.workspace)

        def describe():
            project = self.workspace.get(project_id)
            available = self._research_authorize is not None
            reason = (
                None
                if available
                else "Automatic actions are not available on this server."
            )
            if available:
                try:
                    self._authorize_run(project_id, context)
                except PermissionError as exc:
                    available = False
                    reason = str(exc)
            budget = self._default_research_budget(project_id, project, context)
            enabled = self._skill_library().enabled() if self._skill_library else []
            search = (
                self._search_service_factory() if self._search_service_factory else None
            )
            return {
                "available": available,
                "reason": reason,
                "budget_usd": str(budget) if budget is not None else None,
                "skills": [
                    {"name": item["name"], "description": item["description"]}
                    for item in enabled
                ],
                "web_provider": search.provider if search is not None else None,
            }

        return await await_thread_worker(describe)

    def _research_setup(self, project_id, project, options, context):
        if self._research_authorize is None:
            raise PermissionError(
                "Automatic research is not configured for this server."
            )
        self._authorize_run(project_id, context)
        requested = options.get("budget_usd")
        if requested is None:
            requested = self._default_research_budget(project_id, project, context)
            if requested is None:
                raise ValueError("Set a total research budget before starting.")
        selected = set(options["skills"]) if options.get("skills") is not None else None
        enabled = self._skill_library().enabled() if self._skill_library else []
        if selected is not None and selected - {item["name"] for item in enabled}:
            raise ValueError("One of the selected skills is no longer enabled.")
        snapshots = [
            {key: item[key] for key in ("name", "content", "revision")}
            for item in enabled
            if selected is None or item["name"] in selected
        ]
        return {
            "budget_micros": usd_to_micros(requested),
            "currency": "USD",
            "write_mode": options["write_mode"],
            "max_turns": options["max_turns"],
            "skills": snapshots,
        }

    @staticmethod
    def _research_projection(project, turn):
        if not turn or not turn.get("research"):
            return turn
        ledger = ProjectQAResearchStore(project)
        try:
            current = ledger.get_for_turn(turn["id"])
        except ProjectQAResearchNotFound:
            return turn
        budget = ledger.budget_summary(current["id"])
        state = {
            key: current[key]
            for key in (
                "id",
                "revision",
                "state",
                "currency",
                "write_mode",
                "max_turns",
                "turn_count",
                "pending_approval",
            )
        }
        state.update(
            {
                key: budget[key]
                for key in (
                    "budget_micros",
                    "reserved_micros",
                    "settled_micros",
                    "remaining_micros",
                )
            }
        )
        return {**turn, "research_state": state}

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
                await await_thread_worker(
                    ProjectQAResearchStore(project).interrupt_abandoned
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
            detail["active_turn"] = detail["history"]["active_turn"]
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
        return {
            **page,
            "events": events,
            "active_turn": self._research_projection(project, page.get("active_turn")),
        }

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
        request_context: Any = None,
    ) -> dict:
        # Once durable admission starts, a disconnected HTTP caller must not
        # strand a running row without its task or hosted runtime cleanup.
        admission = asyncio.create_task(
            self._submit_owned(
                project_id,
                thread_id,
                body,
                actor=actor,
                context=BackgroundAskContext.capture(request_context, self.workspace),
            )
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
        context: BackgroundAskContext,
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
                setup = (
                    self._research_setup(
                        project_id, project, values["research"], context
                    )
                    if body.research
                    else None
                )
                turn = store.submit_turn(thread_id, **values, submitted_by=actor)
                if setup is not None and turn["status"] == "running":
                    ledger = ProjectQAResearchStore(project)
                    try:
                        ledger.get_for_turn(turn["id"])
                    except ProjectQAResearchNotFound:
                        try:
                            ledger.create(turn_id=turn["id"], actor=actor, **setup)
                        except Exception:
                            store.finalize_turn(
                                turn["id"],
                                status="failed",
                                error_summary="This research could not be started. Please try again.",
                            )
                            raise
                return turn

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
                    await await_thread_worker(
                        ProjectQAResearchStore(project).interrupt_abandoned
                    )
                    raise
                research = None
                if turn.get("research"):
                    ledger = ProjectQAResearchStore(project)
                    record = await await_thread_worker(ledger.get_for_turn, turn["id"])
                    research = ResearchSession(
                        ledger,
                        record["id"],
                        authorize=lambda: self._authorize_run(
                            project_id, context, record["skills"]
                        ),
                        context=context,
                    )
                    self._research_sessions[key] = research
                task = asyncio.create_task(
                    self._run(
                        project,
                        turn,
                        store,
                        runtime,
                        research=research,
                        project_id=project_id,
                        context=context,
                    )
                )
                self._tasks[key] = task
                task.add_done_callback(
                    lambda completed: self._finished(key, completed, runtime)
                )
            return await await_thread_worker(self._research_projection, project, turn)

    async def _run(
        self,
        project: Project,
        turn: dict,
        store: ProjectQAStore,
        runtime: ProjectQATurnRuntime | None = None,
        *,
        research: ResearchSession | None = None,
        project_id: str = "",
        context: BackgroundAskContext | None = None,
    ) -> None:
        self._started.add((project, turn["id"]))
        status = "interrupted"
        error = None
        diagnostic = None
        model = turn["model"]
        budget = asyncio.timeout(None if research else 180)
        try:
            if (await await_thread_worker(store.get_turn, turn["id"]))[
                "status"
            ] == "stopping":
                return
            router = await await_thread_worker(self.workspace.router_for, project)
            model = model or default_project_ask_model(router)
            tracing = self._tracing

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
                    from frisket.server.services.project_qa_action_host import (
                        ProjectAskActionHost,
                    )

                    host = ProjectAskActionHost(
                        self.workspace,
                        project_id,
                        context_provider=lambda: (
                            research.context if research else context
                        ),
                        sidecar_capabilities_provider=self._sidecar_capabilities,
                    )
                    execution = None
                    if research is not None:
                        from frisket.server.services.project_qa_child_runs import (
                            ProjectQAChildRunService,
                        )
                        from frisket.server.services.project_qa_execution import (
                            ProjectQAExecutionService,
                        )
                        from frisket.server.services.project_qa_output_scope import (
                            derive_output_read_grants,
                            output_grant_allows,
                        )

                        child_runs = ProjectQAChildRunService(self.workspace)
                        execution = ProjectQAExecutionService(
                            project,
                            project_id,
                            turn,
                            store,
                            research,
                            catalog_payload_provider=host.catalog,
                            quote_provider=host.quote,
                            run_action=host.run,
                            wait_child=child_runs.wait,
                            rate_actual_cost=usd_to_micros,
                            output_grants_provider=lambda: derive_output_read_grants(
                                project,
                                research.store.get(research.id)["output_grants"],
                            ),
                            output_grant_allows=output_grant_allows,
                        )

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
                        instrumentation=(
                            tracing.instrumentation if tracing is not None else None
                        ),
                        research=research,
                        action_host=host,
                        execution=execution,
                        search_service=(
                            self._search_service_factory()
                            if self._search_service_factory
                            else None
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
        except PermissionError:
            status, error = (
                "interrupted",
                "Your access changed. Research has stopped; completed work is saved.",
            )
        except ProjectQACapabilityError as exc:
            status, error = "failed", str(exc)
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
            if research is not None:
                await await_thread_worker(
                    research.store.finish, research.id, completed=status == "completed"
                )
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
        self._research_sessions.pop(key, None)

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

    async def resume_research(
        self,
        project_id: str,
        thread_id: str,
        turn_id: str,
        body: AskResearchResume,
        *,
        actor: str | None,
        request_context: Any,
    ) -> dict:
        store = await self.store(project_id)
        project = await await_thread_worker(self.workspace.get, project_id)
        context = BackgroundAskContext.capture(request_context, self.workspace)
        if self._research_authorize is None:
            raise PermissionError(
                "Automatic research is not configured for this server."
            )
        await await_thread_worker(self._authorize_run, project_id, context)
        async with self._admission:
            session = self._research_sessions.get((project, turn_id))
            task = self._tasks.get((project, turn_id))
            if session is None or task is None or task.done():
                raise ProjectQAConflictError(
                    "This research run is no longer active. Start a new question to continue."
                )

            def resume():
                turn = store.get_turn(turn_id)
                if turn["thread_id"] != thread_id:
                    raise ProjectQANotFoundError("Turn not found in this conversation.")
                if turn["submitted_by"] != actor:
                    raise PermissionError(
                        "Only the person who started this run can approve it."
                    )
                if turn["status"] != "running":
                    raise ProjectQAConflictError("This research run has stopped.")
                current = session.store.get(session.id)
                self._authorize_run(project_id, context, current["skills"])
                approval = current["pending_approval"] or {}
                if (
                    current["state"] != "paused"
                    or approval.get("id") != body.approval_id
                ):
                    raise ProjectQAConflictError("This approval is no longer pending.")
                if approval.get("kind") == "unknown_cost":
                    raise ValueError(
                        "Cost is unavailable. Stop this run and choose a priced model or engine."
                    )
                if approval.get("kind") == "action" and body.decision not in {
                    "approve",
                    "skip",
                }:
                    raise ValueError("Approve or skip this action.")
                changes = {"state": "running", "pending_approval": None}
                if body.budget_usd is not None:
                    changes["budget_micros"] = usd_to_micros(body.budget_usd)
                if "max_turns" in body.model_fields_set:
                    changes["max_turns"] = body.max_turns
                if body.write_mode is not None:
                    changes["write_mode"] = body.write_mode
                session.store.update(
                    session.id, expected_revision=body.expected_revision, **changes
                )
                return turn

            turn = await await_thread_worker(resume)
            session.authorize = lambda: self._authorize_run(
                project_id, context, session.store.get(session.id)["skills"]
            )
            session.context = context
            session.wake(body.decision)
            return await await_thread_worker(self._research_projection, project, turn)

    async def shutdown(self) -> None:
        async with self._admission:
            self._closed = True
            tracing = self._tracing
            self._tracing = None
            tasks = list(self._tasks.values())
            for task in tasks:
                task.cancel()
        try:
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
                    ProjectQAStore(project).reconcile_abandoned_turns,
                    live_turn_ids=[],
                )
                await await_thread_worker(
                    ProjectQAResearchStore(project).interrupt_abandoned
                )
            self._tasks.clear()
        finally:
            if tracing is not None:
                try:
                    await asyncio.to_thread(tracing.shutdown)
                except Exception:
                    logger.error("Project Ask tracing shutdown failed")

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
