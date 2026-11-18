"""Research decisions authorize one operation under the parent turn lifecycle."""

from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest

from frisket.engine.store.project_qa import ProjectQAStore
from frisket.engine.store.project_qa_research import (
    ProjectQAResearchConflictError,
    ProjectQAResearchStore,
)
from frisket.server.services.project_qa_research import (
    ResearchSession,
    ResearchSkillsUnavailable,
)
from frisket.server.thread_worker import await_thread_worker


@pytest.fixture
def owned_research(make_project):
    project = make_project("Research")
    qa = ProjectQAStore(project)
    thread = qa.create_thread(title="Investigation")
    turn = qa.submit_turn(
        thread["id"], request_id="one", question="Investigate", submitted_by="reporter"
    )
    ledger = ProjectQAResearchStore(project)
    run = ledger.create(
        turn_id=turn["id"],
        actor="reporter",
        budget_micros=100,
        currency="USD",
        write_mode="ask_each",
        max_turns=None,
    )
    session = ResearchSession(ledger, run["id"], authorize=lambda: None)
    try:
        yield project, qa, turn, ledger, run, session
    finally:
        project.close()


async def _pending(ledger, research_id, *, operation_id=None, kind=None):
    async with asyncio.timeout(5):
        while True:
            snapshot = await await_thread_worker(ledger.get, research_id)
            approval = snapshot["pending_approval"]
            if (
                approval
                and (
                    operation_id is None or approval.get("operation_id") == operation_id
                )
                and (kind is None or approval.get("kind") == kind)
            ):
                return snapshot
            await asyncio.sleep(0)


async def _resume(
    session, snapshot, decision="approve", *, authorize=lambda: None, **preferences
):
    return await session.resume(
        pause_id=snapshot["pending_approval"]["id"],
        expected_revision=snapshot["revision"],
        decision=decision,
        actor="reporter",
        authorize=authorize,
        context=None,
        **preferences,
    )


def _action(operation_id):
    return {
        "kind": "action",
        "operation_id": operation_id,
        "payload_identity": f"payload:{operation_id}",
        "title": f"Write {operation_id}",
    }


@pytest.mark.anyio
async def test_parallel_operations_never_share_a_pause(owned_research):
    _, _, _, ledger, run, session = owned_research
    first = asyncio.create_task(session.pause(_action("A")))
    second = None
    try:
        approval_a = await _pending(ledger, run["id"], operation_id="A")
        second = asyncio.create_task(session.pause(_action("B")))
        # Let B enter the approval owner while A remains unanswered.
        await asyncio.sleep(0.02)
        assert (
            ledger.get(run["id"])["pending_approval"] == approval_a["pending_approval"]
        )
        await _resume(session, approval_a)
        assert await asyncio.wait_for(first, 5) == "approve"

        approval_b = await _pending(ledger, run["id"], operation_id="B")
        assert not second.done()
        assert (
            approval_b["pending_approval"]["id"] != approval_a["pending_approval"]["id"]
        )
        with pytest.raises(ProjectQAResearchConflictError):
            await _resume(session, approval_a)
        assert not second.done()
        assert (
            ledger.get(run["id"])["pending_approval"] == approval_b["pending_approval"]
        )

        await _resume(session, approval_b, "skip")
        assert await asyncio.wait_for(second, 5) == "skip"
    finally:
        tasks = [first, *([second] if second is not None else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.anyio
async def test_waiting_pause_rechecks_whether_it_is_still_needed(owned_research):
    _, _, _, ledger, run, session = owned_research
    needs_budget = True

    async def budget_still_needed():
        return needs_budget

    first = asyncio.create_task(session.pause(_action("A")))
    second = None
    try:
        approval = await _pending(ledger, run["id"], operation_id="A")
        second = asyncio.create_task(
            session.pause(
                {"kind": "budget", "operation_id": "B", "next_estimate_micros": 200},
                recheck=budget_still_needed,
            )
        )
        needs_budget = False
        await _resume(session, approval)
        assert await asyncio.wait_for(first, 5) == "approve"
        assert await asyncio.wait_for(second, 5) == "continue"
        assert ledger.get(run["id"])["pending_approval"] is None
    finally:
        tasks = [first, *([second] if second is not None else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.anyio
async def test_budget_increase_covers_a_waiting_operations_stale_pause(
    owned_research, monkeypatch
):
    _, _, _, ledger, run, session = owned_research
    published = []
    original_publish = ledger.publish_pause

    def publish(research_id, approval):
        published.append(approval["operation_id"])
        return original_publish(research_id, approval)

    monkeypatch.setattr(ledger, "publish_pause", publish)
    first = asyncio.create_task(
        session.pause(
            {"kind": "budget", "operation_id": "A", "next_estimate_micros": 200}
        )
    )
    second = None
    try:
        approval = await _pending(ledger, run["id"], operation_id="A")
        second = asyncio.create_task(
            session.pause(
                {"kind": "budget", "operation_id": "B", "next_estimate_micros": 150}
            )
        )
        await asyncio.sleep(0)
        await _resume(session, approval, "continue", budget_micros=300)
        assert await asyncio.wait_for(first, 5) == "continue"
        assert await asyncio.wait_for(second, 5) == "continue"
        assert published == ["A"]
        assert ledger.get(run["id"])["pending_approval"] is None
        assert ledger.budget_summary(run["id"])["budget_micros"] == 300
    finally:
        tasks = [first, *([second] if second is not None else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize("stopping", [False, True])
async def test_parent_stop_or_finish_refuses_new_pause_authority(
    owned_research, stopping
):
    _, qa, turn, ledger, run, session = owned_research
    waiter = asyncio.create_task(session.pause(_action("A")))
    try:
        approval = await _pending(ledger, run["id"], operation_id="A")
        if stopping:
            qa.request_stop(turn["id"])
        else:
            qa.finalize_turn(turn["id"], status="completed")
        with pytest.raises(ProjectQAResearchConflictError):
            await _resume(session, approval)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        with pytest.raises(ProjectQAResearchConflictError):
            await asyncio.wait_for(session.pause(_action("B")), 5)
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.anyio
async def test_authority_recheck_can_pause_after_a_decision_without_deadlocking(
    owned_research, monkeypatch
):
    _, _, _, ledger, run, session = owned_research
    skills_enabled = True

    def authorize():
        if not skills_enabled:
            raise ResearchSkillsUnavailable("Selected skill was disabled")

    original_resume = ledger.resume

    def disable_skills_after_consumption(*args, **kwargs):
        nonlocal skills_enabled
        result = original_resume(*args, **kwargs)
        skills_enabled = False
        return result

    monkeypatch.setattr(ledger, "resume", disable_skills_after_consumption)
    waiter = asyncio.create_task(session.pause(_action("A")))
    try:
        approval = await _pending(ledger, run["id"], operation_id="A")
        await _resume(session, approval, authorize=authorize)
        skills = await _pending(ledger, run["id"], kind="skills")
        assert not waiter.done()
        skills_enabled = True
        monkeypatch.setattr(ledger, "resume", original_resume)
        await _resume(session, skills, "continue", authorize=authorize)
        assert await asyncio.wait_for(waiter, 5) == "approve"
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.anyio
async def test_disconnected_resume_commits_and_wakes_once_despite_duplicate_resume(
    owned_research, monkeypatch
):
    _, _, _, ledger, run, session = owned_research
    consuming = threading.Event()
    release = threading.Event()
    original_resume = ledger.resume
    consumptions = []

    def blocked_resume(*args, **kwargs):
        consuming.set()
        assert release.wait(5), "test must release the real consumption transaction"
        result = original_resume(*args, **kwargs)
        consumptions.append(result["revision"])
        return result

    monkeypatch.setattr(ledger, "resume", blocked_resume)
    waiter = asyncio.create_task(session.pause(_action("A")))
    resume = duplicate = None
    try:
        approval = await _pending(ledger, run["id"], operation_id="A")
        resume = asyncio.create_task(_resume(session, approval))
        assert await asyncio.to_thread(consuming.wait, 5)
        resume.cancel()
        duplicate = asyncio.create_task(_resume(session, approval))
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await resume
        with pytest.raises(ProjectQAResearchConflictError):
            await duplicate
        assert await asyncio.wait_for(waiter, 5) == "approve"
        assert consumptions == [approval["revision"] + 1]
        assert ledger.get(run["id"])["pending_approval"] is None
    finally:
        release.set()
        tasks = [
            waiter,
            *([resume] if resume is not None else []),
            *([duplicate] if duplicate is not None else []),
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.anyio
async def test_closing_owner_rejects_both_pending_and_queued_decisions(owned_research):
    _, qa, turn, ledger, run, session = owned_research
    first = asyncio.create_task(session.pause(_action("A")))
    second = None
    try:
        approval = await _pending(ledger, run["id"], operation_id="A")
        second = asyncio.create_task(session.pause(_action("B")))
        await asyncio.sleep(0)
        qa.request_stop(turn["id"])
        session.close()
        outcomes = await asyncio.wait_for(
            asyncio.gather(first, second, return_exceptions=True), 5
        )
        assert isinstance(outcomes[0], asyncio.CancelledError)
        assert isinstance(outcomes[1], ProjectQAResearchConflictError)
        with pytest.raises(ProjectQAResearchConflictError):
            await _resume(session, approval)
        assert ledger.get(run["id"])["pending_approval"] is None
    finally:
        tasks = [first, *([second] if second is not None else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.anyio
async def test_closing_owner_during_authorization_cannot_return_live_authority(
    owned_research,
):
    _, _, _, _, _, session = owned_research
    authorizing = threading.Event()
    release = threading.Event()

    def authorize():
        authorizing.set()
        assert release.wait(5), "test must release the authorization worker"

    session.authorize = authorize
    checking = asyncio.create_task(session.check_authority())
    try:
        assert await asyncio.to_thread(authorizing.wait, 5)
        session.close()
        release.set()
        with pytest.raises(ProjectQAResearchConflictError):
            await asyncio.wait_for(checking, 5)
    finally:
        release.set()
        checking.cancel()
        await asyncio.gather(checking, return_exceptions=True)


def test_finalization_rolls_back_parent_research_and_events_together(owned_research):
    project, qa, turn, ledger, run, _ = owned_research
    before_events = project.db.execute(
        "SELECT COUNT(*) FROM project_qa_events WHERE turn_id=?", (turn["id"],)
    ).fetchone()[0]
    project.db.execute(
        "CREATE TRIGGER refuse_research_terminal BEFORE UPDATE ON project_qa_research_runs "
        "WHEN NEW.state IN ('completed','interrupted') "
        "BEGIN SELECT RAISE(ABORT,'simulated research write failure'); END"
    )
    project.db.commit()
    with pytest.raises(
        sqlite3.IntegrityError, match="simulated research write failure"
    ):
        qa.finalize_turn(turn["id"], status="completed")
    assert qa.get_turn(turn["id"])["status"] == "running"
    assert ledger.get(run["id"])["state"] == "running"
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM project_qa_events WHERE turn_id=?", (turn["id"],)
        ).fetchone()[0]
        == before_events
    )


def test_returned_paid_usage_settles_after_parent_stop(owned_research):
    _, qa, turn, ledger, run, _ = owned_research
    ledger.admit_operation(
        run["id"],
        operation_id="paid-call",
        payload_identity="exact-paid-payload",
        operation_kind="model",
        estimate_micros=70,
    )
    qa.request_stop(turn["id"])
    qa.finalize_turn(turn["id"], status="interrupted")
    ledger.settle_operation(
        run["id"],
        operation_id="paid-call",
        payload_identity="exact-paid-payload",
        actual_micros=90,
    )
    assert qa.get_turn(turn["id"])["status"] == "stopped"
    assert ledger.get(run["id"])["state"] == "interrupted"
    assert ledger.budget_summary(run["id"])["settled_micros"] == 90
    assert ledger.budget_summary(run["id"])["reserved_micros"] == 0
    with pytest.raises(ProjectQAResearchConflictError):
        ledger.admit_operation(
            run["id"],
            operation_id="new-paid-call",
            payload_identity="new-payload",
            operation_kind="model",
            estimate_micros=1,
        )
