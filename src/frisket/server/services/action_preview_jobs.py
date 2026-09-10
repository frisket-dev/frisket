from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from frisket.ops.base import RECIPE_INVOCATION_HALT_CODES, RecipeInvocationHalt
from frisket.engine.sandbox.shim import SandboxTeardownError
from frisket.redaction import safe_error
from frisket.contracts.action import Receipt

LOG = logging.getLogger("frisket.server")

# Finished jobs (done/error/cancelled) are swept this long after they finish.
PREVIEW_JOB_TTL_SECONDS = 600.0

# The callable a caller hands to ``start``: it receives a per-row progress
# callback and the cancel Event, executes the preview, and returns whatever
# result object the caller wants stashed on the job.
PreviewRun = Callable[[Callable[[int, int | None], None], threading.Event], Any]
PreviewFinished = Callable[[], None]

# Recipe-invocation failures are an internal exception channel. Only this
# parent-owned vocabulary may cross the preview HTTP boundary; in particular,
# a child protocol's fatal code must never become a public error code merely
# because it was carried by RecipeInvocationHalt.
_PUBLIC_INVOCATION_HALT_CODES = RECIPE_INVOCATION_HALT_CODES


@dataclass
class PreviewJob:
    id: str
    project_id: str
    total: int | None
    status: str = "running"  # running | done | error | cancelled
    progress: dict[str, int | None] = field(
        default_factory=lambda: {"done": 0, "total": 0}
    )
    result: Any = None
    error: dict[str, Any] | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    created_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    receipt: Receipt | None = None
    on_finished: PreviewFinished | None = None


class ActionPreviewJobRegistry:
    """Process-local registry of running/finished preview jobs."""

    def __init__(self, *, ttl_seconds: float = PREVIEW_JOB_TTL_SECONDS) -> None:
        self._jobs: dict[str, PreviewJob] = {}
        self._by_project: dict[str, str] = {}
        self._threads: dict[str, threading.Thread] = {}
        # A replacement can spend meaningful time joining its predecessor, so
        # this serialization must be project-local rather than one global
        # start lock. Locks intentionally live for the registry lifetime: trying
        # to reap them while another caller is waiting on an old lock can create
        # two concurrent handoff domains for the same project.
        self._handoffs: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds
        self._closed = False
        self._teardown_error: SandboxTeardownError | None = None

    def start(
        self,
        project_id: str,
        total: int | None,
        run: PreviewRun,
        *,
        receipt: Receipt | None = None,
        on_finished: PreviewFinished | None = None,
    ) -> PreviewJob:
        """Cancel and join the prior same-project job, then start its successor.

        Joining is deliberately outside the registry lock but inside a
        project-scoped handoff lock. Thus an unrelated project's preview can
        still start, while concurrent replacements for one project cannot
        publish competing successors or overlap process-scoped engines. The
        cancelled predecessor stays queryable until TTL eviction.
        """
        self._evict_expired()
        handoff = self._handoff_for(project_id)
        with handoff:
            with self._lock:
                self._require_open()
                prior_id = self._by_project.get(project_id)
                prior = self._jobs.get(prior_id) if prior_id is not None else None
                prior_thread = (
                    self._threads.get(prior_id) if prior_id is not None else None
                )
                if prior is not None and prior.finished_at is None:
                    prior.cancel_event.set()

            # Never hold the registry lock while a process-owning preview tears
            # down. A timeout followed by starting anyway would violate the
            # no-overlap contract, so this is a real join rather than a bounded
            # best-effort wait.
            self._join_thread(prior_thread)

            job = PreviewJob(
                receipt=receipt,
                on_finished=on_finished,
                id=uuid.uuid4().hex,
                project_id=project_id,
                total=total,
                progress={"done": 0, "total": total},
            )
            thread = threading.Thread(
                target=self._run_job,
                args=(job, run),
                name=f"preview-{job.id[:8]}",
                daemon=True,
            )

            # Recheck after the predecessor join: shutdown may have begun while
            # this caller was waiting. Start before publishing, while holding
            # the lock, so shutdown can neither miss a live thread nor attempt
            # to join an as-yet-unstarted one. The new worker may run, but its
            # first registry-state mutation blocks on this same lock.
            with self._lock:
                self._require_open()
                thread.start()
                self._jobs[job.id] = job
                self._threads[job.id] = thread
                self._by_project[project_id] = job.id
            return job

    def _handoff_for(self, project_id: str) -> threading.Lock:
        with self._lock:
            self._require_open()
            return self._handoffs.setdefault(project_id, threading.Lock())

    def _require_open(self) -> None:
        if self._teardown_error is not None:
            # Unproved child death is not recoverable by starting another job.
            raise self._teardown_error
        if self._closed:
            raise RuntimeError("action preview registry is shut down")

    @staticmethod
    def _join_thread(thread: threading.Thread | None) -> None:
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    def _run_job(self, job: PreviewJob, run: PreviewRun) -> None:
        try:
            self._run_job_body(job, run)
        finally:
            if job.on_finished is not None:
                job.on_finished()

    def _run_job_body(self, job: PreviewJob, run: PreviewRun) -> None:
        def progress_cb(done: int, total: int | None) -> None:
            with self._lock:
                job.progress = {"done": done, "total": total}

        try:
            result = run(progress_cb, job.cancel_event)
        except RecipeInvocationHalt as exc:
            with self._lock:
                # Decide cancellation at the terminal-state write, not before
                # acquiring the lock. A replacement may set the event in the
                # narrow interval between this exception and publication.
                cancelled = _job_cancelled(job)
                if cancelled:
                    job.status = "cancelled"
                else:
                    job.status = "error"
                    job.error = _halt_error_payload(exc)
                job.finished_at = time.monotonic()
            if not cancelled:
                LOG.warning(
                    "action_preview_job_halted",
                    extra={
                        "event": "action_preview_job_halted",
                        "preview_id": job.id,
                        "halt_code": (
                            exc.code
                            if exc.code in _PUBLIC_INVOCATION_HALT_CODES
                            else "preview_failed"
                        ),
                    },
                )
            return
        except BaseException as exc:  # noqa: BLE001 - any escape terminates the job
            safe = safe_error("preview_failed", exc, max_chars=500)
            with self._lock:
                fatal_teardown = isinstance(exc, SandboxTeardownError)
                if fatal_teardown:
                    self._teardown_error = exc
                cancelled = _job_cancelled(job) and not fatal_teardown
                if cancelled:
                    job.status = "cancelled"
                else:
                    job.status = "error"
                    job.error = {"code": safe.code, "message": safe.detail}
                job.finished_at = time.monotonic()
            if not cancelled:
                LOG.warning(
                    "action_preview_job_failed",
                    exc_info=True,
                    extra={
                        "event": "action_preview_job_failed",
                        "preview_id": job.id,
                    },
                )
            return
        discard = False
        with self._lock:
            if _job_cancelled(job):
                job.status = "cancelled"
                discard = True
            else:
                job.status = "done"
                job.result = result
            job.finished_at = time.monotonic()
        if discard:
            _close_result(result, preview_id=job.id)

    def get(self, project_id: str, preview_id: str) -> PreviewJob | None:
        self._evict_expired()
        with self._lock:
            job = self._jobs.get(preview_id)
            if job is None or job.project_id != project_id:
                return None
            return job

    def cancel(self, project_id: str, preview_id: str) -> bool:
        """Signal cancellation. Idempotent: cancelling an already-finished or
        unknown job returns False, but the DELETE route treats both as a no-op
        204 (nothing left to cancel)."""
        with self._lock:
            job = self._jobs.get(preview_id)
            if (
                job is None
                or job.project_id != project_id
                or job.finished_at is not None
            ):
                return False
            job.cancel_event.set()
            return True

    def shutdown(self) -> None:
        """Stop accepting previews, cancel every worker, and join them all.

        The closed flag is set in the same critical section as the thread
        snapshot. ``start`` rechecks it immediately before starting a successor,
        so a replacement that was blocked joining its predecessor cannot launch
        after this snapshot and escape shutdown ownership.
        """
        with self._lock:
            self._closed = True
            for job in self._jobs.values():
                if job.finished_at is None:
                    job.cancel_event.set()
            threads = list(self._threads.values())
        for thread in threads:
            self._join_thread(thread)
        with self._lock:
            completed = [(job.id, job.result) for job in self._jobs.values()]
            for job in self._jobs.values():
                job.result = None
        for preview_id, result in completed:
            _close_result(result, preview_id=preview_id)

    def _evict_expired(self) -> None:
        now = time.monotonic()
        removed = []
        with self._lock:
            expired = [
                pid
                for pid, job in self._jobs.items()
                if job.finished_at is not None
                and (now - job.finished_at) > self._ttl
                and not self._threads[pid].is_alive()
            ]
            for pid in expired:
                job = self._jobs.pop(pid, None)
                self._threads.pop(pid, None)
                if job is not None:
                    removed.append(job)
                if job is not None and self._by_project.get(job.project_id) == pid:
                    self._by_project.pop(job.project_id, None)
        for job in removed:
            _close_result(job.result, preview_id=job.id)
            job.result = None


def _job_cancelled(job: PreviewJob) -> bool:
    # A late cancel during settlement cannot relabel already-completed paid
    # work or discard its ephemeral result. Receipt finalization owns this fact.
    if job.receipt is not None and job.receipt.status != "running":
        return job.receipt.status == "cancelled"
    return job.cancel_event.is_set()


def _close_result(result: Any, *, preview_id: str) -> None:
    close = getattr(result, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        LOG.warning(
            "action_preview_result_cleanup_failed",
            exc_info=True,
            extra={
                "event": "action_preview_result_cleanup_failed",
                "preview_id": preview_id,
            },
        )


def _halt_error_payload(exc: RecipeInvocationHalt) -> dict[str, str]:
    if exc.code not in _PUBLIC_INVOCATION_HALT_CODES:
        safe = safe_error(
            "preview_failed",
            "Local preview engine failed.",
            max_chars=500,
        )
        return {"code": safe.code, "message": safe.detail}
    safe = safe_error(exc.code, exc.detail, max_chars=500)
    return {"code": safe.code, "message": safe.detail}
